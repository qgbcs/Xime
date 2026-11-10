@file:Suppress("DEPRECATION")

package com.kingzcheung.xime.task

import android.app.AlarmManager
import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.database.ContentObserver
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.display.DisplayManager
import android.location.LocationManager
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.Uri
import android.net.wifi.WifiManager
import android.nfc.NfcAdapter
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.PowerManager
import android.provider.Settings
import android.telephony.PhoneStateListener
import android.telephony.TelephonyCallback
import android.telephony.TelephonyManager
import android.view.Display
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executor

/**
 * 系统广播监听（主进程动态注册一次，无清单接收器）。
 *
 * 与 NetworkEventMonitor 分工：后者只产生网络边沿语义事件，这里覆盖其余系统广播。
 * 控耗电的关键是“默认不进 Python”：
 *  - 收到广播只做两件事：写 Kotlin 侧环形缓冲；仅当 Python 侧有启用的事件任务
 *    订阅了该事件时，才调用 task_manager.fire_event。否则完全是 Java 侧记账。
 *  - 经典广播由系统派发，不轮询、不持 wakelock、不起前台服务；回调跑在独立
 *    HandlerThread 上，不占主线程。
 *
 * 缓冲：上限 500 行，每行 `epoch_ms | event_name | payload_json`，
 * 通过 RPC `task_manager.system_events(limit)` 读取。
 */
object SystemEventMonitor {

    private const val TAG = "SysEventMonitor"
    private const val PY_MODULE = "task_manager"
    private const val PY_METHOD = "fire_event"
    private const val LOG_MAX = 500

    /** 屏幕旋转事件名：Android 没有旋转广播，只能由 DisplayManager 回调产生。 */
    private const val ROTATION_EVENT = "screen_rotation_changed"

    /** 通话状态事件名：响铃/接听/挂断；方向由状态序列推导（来电先响铃，去电直接 offhook）。 */
    private const val CALL_EVENT = "call_state_changed"

    private val lock = Any()
    private val log = ArrayDeque<String>()

    @Volatile
    private var registered: Set<String> = emptySet()
    private var started = false
    private lateinit var handler: Handler

    /** 最近一次默认屏幕旋转值（Surface.ROTATION_*）；-1 表示不可得。 */
    private var lastRotation = -1

    /** 通话状态机：上一次状态、是否经过响铃、当前方向（incoming/outgoing）。 */
    private var lastCallState = TelephonyManager.CALL_STATE_IDLE
    private var ringingSeen = false
    private var callDirection = ""

    /** 输入法是否可见：键盘弹出/收起与剪切板事件都依赖它。 */
    @Volatile
    private var keyboardVisible = false

    /** 传感器/设置项的最近值与上次上报时间，用于 1 秒 + 阈值去抖。 */
    private val lastSensorEmit = HashMap<String, Long>()
    private val lastSensorValue = HashMap<String, Float>()
    private var lastBrightness = -1
    private var lastVolume = -1

    /** 主进程应用上下文，供广播 payload 查询系统服务状态。 */
    private var appContext: Context? = null

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            record(intent)
        }
    }

    /** Python 事件任务变更后回写订阅集合（JSON 数组字符串，如 ["screen_on"]）。 */
    @JvmStatic
    fun syncRegisteredEvents(jsonArray: String?) {
        val set = try {
            val arr = JSONArray(jsonArray ?: "[]")
            (0 until arr.length())
                .map { arr.optString(it) }
                .filter { it.isNotEmpty() }
                .toSet()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "syncRegisteredEvents 解析失败: ${t.javaClass.simpleName}")
            emptySet()
        }
        synchronized(lock) { registered = set }
        FileLogger.i(TAG, "Python 订阅的系统事件: $set")
        // 把订阅集合转发给 GPS 事件监听器：集合内没有 GPS 事件时它会立即停止监听（零开销）
        try {
            LocationEventMonitor.syncRegisteredEvents(set)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "转发 GPS 事件订阅失败: ${t.javaClass.simpleName}")
        }
    }

    /** RPC 回读缓冲：返回 JSON 数组，每个元素是一行文本（含毫秒时间戳）。 */
    @JvmStatic
    fun getEventLog(limit: Int): String {
        val n = if (limit <= 0) LOG_MAX else minOf(limit, LOG_MAX)
        val out = JSONArray()
        synchronized(lock) {
            val from = maxOf(0, log.size - n)
            log.drop(from).forEach { out.put(it) }
        }
        return out.toString()
    }

    @JvmStatic
    fun clearEventLog() {
        synchronized(lock) { log.clear() }
    }

    /** 当前生效的 Python 订阅集合（调试用，JSON 数组字符串）。 */
    @JvmStatic
    fun getRegisteredEvents(): String {
        return JSONArray(synchronized(lock) { registered.sorted() }).toString()
    }

    @JvmStatic
    fun start(context: Context) {
        if (started) return
        started = true
        val thread = HandlerThread("sys-event-monitor").apply { start() }
        handler = Handler(thread.looper)
        val ctx = context.applicationContext
        appContext = ctx

        val base = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_USER_PRESENT)
            addAction(Intent.ACTION_BATTERY_LOW)
            addAction(Intent.ACTION_BATTERY_OKAY)
            addAction(Intent.ACTION_BATTERY_CHANGED)
            addAction(Intent.ACTION_POWER_CONNECTED)
            addAction(Intent.ACTION_POWER_DISCONNECTED)
            addAction(Intent.ACTION_AIRPLANE_MODE_CHANGED)
            addAction(Intent.ACTION_HEADSET_PLUG)
            addAction(WifiManager.WIFI_STATE_CHANGED_ACTION)
            addAction(ConnectivityManager.CONNECTIVITY_ACTION)
            addAction(AudioManager.RINGER_MODE_CHANGED_ACTION)
            addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
            addAction(Intent.ACTION_SHUTDOWN)
            addAction(Intent.ACTION_LOCALE_CHANGED)
            addAction(Intent.ACTION_TIME_CHANGED)
            addAction(Intent.ACTION_TIMEZONE_CHANGED)
            addAction(Intent.ACTION_INPUT_METHOD_CHANGED)
            // 零成本系统状态（广播/回调，不需要新权限）
            addAction(PowerManager.ACTION_DEVICE_IDLE_MODE_CHANGED)
            addAction(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED)
            addAction("android.intent.action.DREAMING_STARTED")
            addAction("android.intent.action.DREAMING_STOPPED")
            addAction("android.intent.action.DEVICE_STORAGE_LOW")
            addAction("android.intent.action.DEVICE_STORAGE_OK")
            addAction("android.hardware.usb.action.USB_STATE")
            addAction(NfcAdapter.ACTION_ADAPTER_STATE_CHANGED)
            addAction(LocationManager.MODE_CHANGED_ACTION)
            addAction("android.net.wifi.WIFI_AP_STATE_CHANGED")
            addAction("android.intent.action.SIM_STATE_CHANGED")
            addAction(AlarmManager.ACTION_NEXT_ALARM_CLOCK_CHANGED)
            addAction(Intent.ACTION_TIME_TICK)
            addAction(Intent.ACTION_CONFIGURATION_CHANGED)
            addAction(Intent.ACTION_WALLPAPER_CHANGED)
            addAction("android.intent.action.USER_SWITCHED")
        }
        val pkg = IntentFilter().apply {
            addAction(Intent.ACTION_PACKAGE_ADDED)
            addAction(Intent.ACTION_PACKAGE_REMOVED)
            addAction(Intent.ACTION_PACKAGE_REPLACED)
            addDataScheme("package")
        }
        val media = IntentFilter().apply {
            addAction(Intent.ACTION_MEDIA_MOUNTED)
            addAction(Intent.ACTION_MEDIA_UNMOUNTED)
            addAction(Intent.ACTION_MEDIA_REMOVED)
            addAction(Intent.ACTION_MEDIA_BAD_REMOVAL)
            addAction(Intent.ACTION_MEDIA_EJECT)
            addDataScheme("file")
        }

        register(ctx, base)
        register(ctx, pkg)
        register(ctx, media)
        startRotationListener(ctx)
        startCallStateListener(ctx)
        startSettingsObservers(ctx)
        startSensors(ctx)
        startClipboardListener(ctx)
        FileLogger.i(TAG, "系统广播监听已注册，缓冲上限 $LOG_MAX")
    }

    private fun register(context: Context, filter: IntentFilter) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.registerReceiver(
                    receiver, filter, null, handler, Context.RECEIVER_EXPORTED,
                )
            } else {
                context.registerReceiver(receiver, filter, null, handler)
            }
        } catch (t: Throwable) {
            FileLogger.e(TAG, "注册广播失败: ${filter.countActions()} actions", t)
        }
    }

    private fun record(intent: Intent) {
        val action = intent.action ?: return
        val name = nameOf(action) ?: return
        append(name, buildPayload(intent, action))
    }

    /**
     * 屏幕旋转监听：旋转不是广播，只能用 DisplayManager 回调。
     *
     * onDisplayChanged 在很多场景（刷新率、HDR、分辨率）都会回调，所以只在
     * 默认屏幕的 rotation 真正变化时记一行；180° 翻转也能被捕获。
     */
    private fun startRotationListener(context: Context) {
        try {
            val dm = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
            lastRotation = rotationOf(dm)
            val listener = object : DisplayManager.DisplayListener {
                override fun onDisplayAdded(displayId: Int) {}
                override fun onDisplayRemoved(displayId: Int) {}

                override fun onDisplayChanged(displayId: Int) {
                    if (displayId != Display.DEFAULT_DISPLAY) return
                    val rotation = rotationOf(dm)
                    if (rotation < 0 || rotation == lastRotation) return
                    lastRotation = rotation
                    append(
                        ROTATION_EVENT,
                        JSONObject()
                            .put("ts", System.currentTimeMillis())
                            .put("rotation", rotation)
                            .put("degrees", rotation * 90),
                    )
                }
            }
            dm.registerDisplayListener(listener, handler)
            FileLogger.i(TAG, "屏幕旋转监听已注册，初始 rotation=$lastRotation")
        } catch (t: Throwable) {
            FileLogger.w(TAG, "屏幕旋转监听注册失败: ${t.javaClass.simpleName}")
        }
    }

    private fun rotationOf(dm: DisplayManager): Int = try {
        dm.getDisplay(Display.DEFAULT_DISPLAY)?.rotation ?: -1
    } catch (t: Throwable) {
        -1
    }

    /**
     * 通话状态监听：PHONE_STATE 广播受后台限制且号码需要更多权限，这里直接用
     * TelephonyCallback（API 31+）/ PhoneStateListener（旧系统）订阅状态变化。
     * 只记录 idle/ringing/offhook 与方向，不读号码、不读联系人；
     * 无 READ_PHONE_STATE 或设备无电话能力时只打日志，不影响主流程。
     */
    private fun startCallStateListener(context: Context) {
        try {
            val tm = context.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager
            if (tm == null) {
                FileLogger.w(TAG, "通话状态监听跳过：无 TelephonyManager")
                return
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val cb = object : TelephonyCallback(), TelephonyCallback.CallStateListener {
                    override fun onCallStateChanged(state: Int) {
                        onCallState(state)
                    }
                }
                tm.registerTelephonyCallback(
                    Executor { command -> handler.post(command) },
                    cb,
                )
            } else {
                val listener = object : PhoneStateListener() {
                    override fun onCallStateChanged(state: Int, phoneNumber: String?) {
                        handler.post { onCallState(state) }
                    }
                }
                tm.listen(listener, PhoneStateListener.LISTEN_CALL_STATE)
            }
            FileLogger.i(TAG, "通话状态监听已注册, 当前 state=${tm.callState}")
        } catch (t: Throwable) {
            FileLogger.w(TAG, "通话状态监听注册失败: ${t.javaClass.simpleName}")
        }
    }

    /** 状态机推导方向：RINGING 标记来电；未响铃直接 OFFHOOK 视为去电。 */
    private fun onCallState(state: Int) {
        var direction = ""
        synchronized(lock) {
            if (state == lastCallState) return
            if (state == TelephonyManager.CALL_STATE_RINGING) {
                ringingSeen = true
                callDirection = "incoming"
            } else if (state == TelephonyManager.CALL_STATE_OFFHOOK && !ringingSeen) {
                callDirection = "outgoing"
            }
            lastCallState = state
            direction = callDirection
            if (state == TelephonyManager.CALL_STATE_IDLE) {
                ringingSeen = false
                callDirection = ""
            }
        }
        append(
            CALL_EVENT,
            JSONObject()
                .put("ts", System.currentTimeMillis())
                .put("state", callStateName(state))
                .put("state_code", state)
                .put("direction", direction),
        )
    }

    private fun callStateName(state: Int): String = when (state) {
        TelephonyManager.CALL_STATE_IDLE -> "idle"
        TelephonyManager.CALL_STATE_RINGING -> "ringing"
        TelephonyManager.CALL_STATE_OFFHOOK -> "offhook"
        else -> "unknown_$state"
    }

    /**
     * 亮度/音量不是广播，只能观察 Settings.System 的变更。读取系统设置不需要权限；
     * 音量统一用 STREAM_MUSIC 的档位表示，同时给出该流的最大档位。
     */
    private fun startSettingsObservers(context: Context) {
        try {
            val resolver = context.contentResolver
            val audio = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            lastBrightness = readBrightness(resolver)
            lastVolume = audio?.getStreamVolume(AudioManager.STREAM_MUSIC) ?: -1
            val observer = object : ContentObserver(handler) {
                override fun onChange(selfChange: Boolean, uri: Uri?) {
                    val key = uri?.lastPathSegment ?: return
                    when (key) {
                        Settings.System.SCREEN_BRIGHTNESS -> {
                            val value = readBrightness(resolver)
                            if (value == lastBrightness) return
                            lastBrightness = value
                            append(
                                "brightness_changed",
                                JSONObject()
                                    .put("ts", System.currentTimeMillis())
                                    .put("value", value),
                            )
                        }

                        "volume_music" -> {
                            val value = audio?.getStreamVolume(AudioManager.STREAM_MUSIC) ?: -1
                            if (value == lastVolume) return
                            lastVolume = value
                            append(
                                "volume_changed",
                                JSONObject()
                                    .put("ts", System.currentTimeMillis())
                                    .put("stream", "music")
                                    .put("value", value)
                                    .put("max", audio?.getStreamMaxVolume(AudioManager.STREAM_MUSIC) ?: -1),
                            )
                        }
                    }
                }
            }
            resolver.registerContentObserver(Settings.System.CONTENT_URI, true, observer)
            FileLogger.i(TAG, "亮度/音量监听已注册, brightness=$lastBrightness volume=$lastVolume")
        } catch (t: Throwable) {
            FileLogger.w(TAG, "亮度/音量监听注册失败: ${t.javaClass.simpleName}")
        }
    }

    private fun readBrightness(resolver: android.content.ContentResolver): Int = try {
        Settings.System.getInt(resolver, Settings.System.SCREEN_BRIGHTNESS)
    } catch (t: Throwable) {
        -1
    }

    /**
     * 光感/距离/气压：低频采样 + 1 秒与阈值去抖，避免传感器高频回调把缓冲刷满。
     * 没有对应传感器的设备直接跳过；注册失败只降级。
     */
    private fun startSensors(context: Context) {
        try {
            val sm = context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager ?: return
            val listener = object : SensorEventListener {
                override fun onSensorChanged(event: SensorEvent) {
                    val name = when (event.sensor.type) {
                        Sensor.TYPE_LIGHT -> "light_changed"
                        Sensor.TYPE_PROXIMITY -> "proximity_changed"
                        Sensor.TYPE_PRESSURE -> "pressure_changed"
                        else -> return
                    }
                    val value = event.values.firstOrNull() ?: return
                    val now = System.currentTimeMillis()
                    val shouldEmit = synchronized(lock) {
                        val lastTs = lastSensorEmit[name] ?: 0L
                        val last = lastSensorValue[name]
                        val deltaOk = when (name) {
                            "light_changed" -> last == null || kotlin.math.abs(value - last) >= 5f
                            "pressure_changed" -> last == null || kotlin.math.abs(value - last) >= 0.5f
                            else -> last == null || value != last
                        }
                        if (now - lastTs < 1000L || !deltaOk) {
                            false
                        } else {
                            lastSensorEmit[name] = now
                            lastSensorValue[name] = value
                            true
                        }
                    }
                    if (!shouldEmit) return
                    append(
                        name,
                        JSONObject()
                            .put("ts", now)
                            .put("value", value.toDouble())
                            .put("sensor", event.sensor.name),
                    )
                }

                override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
            }
            var count = 0
            for (type in listOf(Sensor.TYPE_LIGHT, Sensor.TYPE_PROXIMITY, Sensor.TYPE_PRESSURE)) {
                val sensor = sm.getDefaultSensor(type) ?: continue
                sm.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_NORMAL, handler)
                count++
            }
            FileLogger.i(TAG, "传感器监听已注册, 数量=$count")
        } catch (t: Throwable) {
            FileLogger.w(TAG, "传感器监听注册失败: ${t.javaClass.simpleName}")
        }
    }

    /**
     * 剪切板变化：Android 10+ 后台无法读剪切板，这里只在输入法可见时记录；
     * 只上报是否有文本与长度，不落内容，避免隐私泄漏。
     */
    private fun startClipboardListener(context: Context) {
        try {
            val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
            cm.addPrimaryClipChangedListener {
                if (!keyboardVisible) return@addPrimaryClipChangedListener
                val text = try {
                    cm.primaryClip?.getItemAt(0)?.coerceToText(context)?.toString() ?: ""
                } catch (t: Throwable) {
                    ""
                }
                append(
                    "clipboard_changed",
                    JSONObject()
                        .put("ts", System.currentTimeMillis())
                        .put("has_text", text.isNotEmpty())
                        .put("length", text.length),
                )
            }
            FileLogger.i(TAG, "剪切板监听已注册（仅在输入法可见时上报）")
        } catch (t: Throwable) {
            FileLogger.w(TAG, "剪切板监听注册失败: ${t.javaClass.simpleName}")
        }
    }

    /** 输入法可见性由 XimeInputMethodService 回调；相同状态不重复上报。 */
    @JvmStatic
    fun notifyKeyboard(shown: Boolean, source: String) {
        if (!started) return
        val changed = synchronized(lock) {
            if (keyboardVisible == shown) {
                false
            } else {
                keyboardVisible = shown
                true
            }
        }
        if (!changed) return
        append(
            if (shown) "keyboard_shown" else "keyboard_hidden",
            JSONObject()
                .put("ts", System.currentTimeMillis())
                .put("source", source),
        )
    }

    /** 写环形缓冲；仅当 Python 侧有启用的事件任务订阅了该事件时才投递。 */
    private fun append(name: String, payload: JSONObject) {
        val line = "${System.currentTimeMillis()} | $name | $payload"
        val deliver: Boolean
        synchronized(lock) {
            log.addLast(line)
            while (log.size > LOG_MAX) log.removeFirst()
            deliver = registered.contains(name)
        }
        if (!deliver) return
        deliverToPython(name, payload.toString())
    }

    private fun deliverToPython(name: String, payload: String) {
        if (!Python.isStarted()) return
        try {
            Python.getInstance().getModule(PY_MODULE).callAttr(PY_METHOD, name, payload)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "fire_event 投递失败 $name: ${t.javaClass.simpleName}")
        }
    }

    private fun nameOf(action: String): String? = when (action) {
        Intent.ACTION_SCREEN_ON -> "screen_on"
        Intent.ACTION_SCREEN_OFF -> "screen_off"
        Intent.ACTION_USER_PRESENT -> "user_present"
        Intent.ACTION_BATTERY_LOW -> "battery_low"
        Intent.ACTION_BATTERY_OKAY -> "battery_okay"
        Intent.ACTION_BATTERY_CHANGED -> "battery_changed"
        Intent.ACTION_POWER_CONNECTED -> "power_connected"
        Intent.ACTION_POWER_DISCONNECTED -> "power_disconnected"
        Intent.ACTION_AIRPLANE_MODE_CHANGED -> "airplane_mode_changed"
        Intent.ACTION_HEADSET_PLUG -> "headset_plug"
        Intent.ACTION_PACKAGE_ADDED -> "package_added"
        Intent.ACTION_PACKAGE_REMOVED -> "package_removed"
        Intent.ACTION_PACKAGE_REPLACED -> "package_replaced"
        WifiManager.WIFI_STATE_CHANGED_ACTION -> "wifi_state_changed"
        ConnectivityManager.CONNECTIVITY_ACTION -> "connectivity_changed"
        AudioManager.RINGER_MODE_CHANGED_ACTION -> "ringer_mode_changed"
        BluetoothAdapter.ACTION_STATE_CHANGED -> "bluetooth_state_changed"
        Intent.ACTION_MEDIA_MOUNTED -> "media_mounted"
        Intent.ACTION_MEDIA_UNMOUNTED -> "media_unmounted"
        Intent.ACTION_MEDIA_REMOVED -> "media_removed"
        Intent.ACTION_MEDIA_BAD_REMOVAL -> "media_bad_removal"
        Intent.ACTION_MEDIA_EJECT -> "media_eject"
        Intent.ACTION_SHUTDOWN -> "shutdown"
        Intent.ACTION_LOCALE_CHANGED -> "locale_changed"
        Intent.ACTION_TIME_CHANGED -> "time_changed"
        Intent.ACTION_TIMEZONE_CHANGED -> "timezone_changed"
        Intent.ACTION_INPUT_METHOD_CHANGED -> "input_method_changed"
        PowerManager.ACTION_DEVICE_IDLE_MODE_CHANGED -> "doze_mode_changed"
        PowerManager.ACTION_POWER_SAVE_MODE_CHANGED -> "power_save_mode_changed"
        "android.intent.action.DREAMING_STARTED" -> "dreaming_started"
        "android.intent.action.DREAMING_STOPPED" -> "dreaming_stopped"
        "android.intent.action.DEVICE_STORAGE_LOW" -> "storage_low"
        "android.intent.action.DEVICE_STORAGE_OK" -> "storage_ok"
        "android.hardware.usb.action.USB_STATE" -> "usb_state_changed"
        NfcAdapter.ACTION_ADAPTER_STATE_CHANGED -> "nfc_state_changed"
        LocationManager.MODE_CHANGED_ACTION -> "location_mode_changed"
        "android.net.wifi.WIFI_AP_STATE_CHANGED" -> "hotspot_state_changed"
        "android.intent.action.SIM_STATE_CHANGED" -> "sim_state_changed"
        AlarmManager.ACTION_NEXT_ALARM_CLOCK_CHANGED -> "next_alarm_changed"
        Intent.ACTION_TIME_TICK -> "time_tick"
        Intent.ACTION_CONFIGURATION_CHANGED -> "configuration_changed"
        Intent.ACTION_WALLPAPER_CHANGED -> "wallpaper_changed"
        "android.intent.action.USER_SWITCHED" -> "user_switched"
        else -> null
    }

    private fun buildPayload(intent: Intent, action: String): JSONObject {
        val p = JSONObject().put("ts", System.currentTimeMillis()).put("action", action)
        when (action) {
            Intent.ACTION_BATTERY_CHANGED -> p
                .put("level", intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1))
                .put("scale", intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1))
                .put("status", intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1))
                .put("plugged", intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1))
                .put("temperature", intent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1))
                .put("voltage", intent.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1))
            Intent.ACTION_AIRPLANE_MODE_CHANGED ->
                p.put("state", intent.getBooleanExtra("state", false))
            Intent.ACTION_HEADSET_PLUG -> p
                .put("state", intent.getIntExtra("state", -1))
                .put("microphone", intent.getIntExtra("microphone", -1))
            WifiManager.WIFI_STATE_CHANGED_ACTION ->
                p.put("state", intent.getIntExtra(WifiManager.EXTRA_WIFI_STATE, -1))
            ConnectivityManager.CONNECTIVITY_ACTION ->
                p.put(
                    "no_connectivity",
                    intent.getBooleanExtra(ConnectivityManager.EXTRA_NO_CONNECTIVITY, false),
                )
            AudioManager.RINGER_MODE_CHANGED_ACTION ->
                p.put("mode", intent.getIntExtra(AudioManager.EXTRA_RINGER_MODE, -1))
            BluetoothAdapter.ACTION_STATE_CHANGED ->
                p.put("state", intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1))
            Intent.ACTION_PACKAGE_ADDED,
            Intent.ACTION_PACKAGE_REMOVED,
            Intent.ACTION_PACKAGE_REPLACED -> p
                .put("package", intent.data?.schemeSpecificPart ?: "")
                .put("replacing", intent.getBooleanExtra(Intent.EXTRA_REPLACING, false))
            Intent.ACTION_MEDIA_MOUNTED,
            Intent.ACTION_MEDIA_UNMOUNTED,
            Intent.ACTION_MEDIA_REMOVED,
            Intent.ACTION_MEDIA_BAD_REMOVAL,
            Intent.ACTION_MEDIA_EJECT -> p.put("path", intent.dataString ?: "")
            PowerManager.ACTION_DEVICE_IDLE_MODE_CHANGED ->
                p.put("idle", powerManager()?.isDeviceIdleMode ?: false)
            PowerManager.ACTION_POWER_SAVE_MODE_CHANGED ->
                p.put("enabled", powerManager()?.isPowerSaveMode ?: false)
            "android.hardware.usb.action.USB_STATE" -> p
                .put("connected", intent.getBooleanExtra("connected", false))
                .put("configured", intent.getBooleanExtra("configured", false))
            NfcAdapter.ACTION_ADAPTER_STATE_CHANGED ->
                p.put("enabled", nfcAdapter()?.isEnabled ?: false)
            LocationManager.MODE_CHANGED_ACTION ->
                p.put("enabled", locationEnabled())
            "android.net.wifi.WIFI_AP_STATE_CHANGED" ->
                p.put("state", intent.getIntExtra(WifiManager.EXTRA_WIFI_STATE, -1))
            "android.intent.action.SIM_STATE_CHANGED" ->
                p.put("state", intent.getStringExtra("ss") ?: "")
            AlarmManager.ACTION_NEXT_ALARM_CLOCK_CHANGED -> {
                val clock = alarmManager()?.nextAlarmClock
                p.put("next_alarm_ms", clock?.triggerTime ?: -1L)
                p.put("package", clock?.showIntent?.creatorPackage ?: "")
            }
            Intent.ACTION_CONFIGURATION_CHANGED -> p
                .put("font_scale", (appContext?.resources?.configuration?.fontScale ?: -1f).toDouble())
                .put("density_dpi", appContext?.resources?.configuration?.densityDpi ?: -1)
            "android.intent.action.USER_SWITCHED" ->
                p.put("user_id", intent.getIntExtra("android.intent.extra.user_handle", -1))
        }
        return p
    }

    private fun powerManager(): PowerManager? =
        appContext?.getSystemService(Context.POWER_SERVICE) as? PowerManager

    private fun alarmManager(): AlarmManager? =
        appContext?.getSystemService(Context.ALARM_SERVICE) as? AlarmManager

    private fun nfcAdapter(): NfcAdapter? = try {
        appContext?.let { NfcAdapter.getDefaultAdapter(it) }
    } catch (t: Throwable) {
        null
    }

    private fun locationEnabled(): Boolean {
        val lm = appContext?.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            ?: return false
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                lm.isLocationEnabled
            } else {
                lm.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                    lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
            }
        } catch (t: Throwable) {
            false
        }
    }
}
