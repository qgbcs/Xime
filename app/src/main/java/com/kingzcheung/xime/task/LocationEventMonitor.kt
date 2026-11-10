@file:Suppress("DEPRECATION")

package com.kingzcheung.xime.task

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.GnssStatus
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * GPS 六大类事件（位置数据 / 围栏 / 活动 / GNSS / 传感器 / 开关权限）。
 *
 * 控耗电与“不拖慢系统”的关键：默认什么都不做。
 *  - [init] 只保存 applicationContext，不建线程、不注册任何监听、不读文件。
 *  - 只有 Python 侧存在启用了下列事件的任务（由 SystemEventMonitor.syncRegisteredEvents
 *    转发订阅集合）时，才创建 HandlerThread 并注册定位/GNSS/传感器。
 *  - 订阅集合一旦清空，立即注销全部监听并退出线程。
 *  - 任何异常只打日志降级，绝不影响 MQTT/HTTP 主流程。
 *
 * 所有参数都可通过 [configure] 动态修改（JSON 合并后持久化到 filesDir，运行中重启监听）。
 * 事件统一经 task_manager.fire_event(name, payload) 投递给 Python，
 * 未订阅的事件在 Kotlin 侧直接丢弃，不进入 Python。
 */
object LocationEventMonitor {

    private const val TAG = "LocEventMonitor"
    private const val PY_MODULE = "task_manager"
    private const val PY_METHOD = "fire_event"
    private const val RETRY_DELAY_MS = 5000L
    private const val PENDING_MAX = 64
    private const val CONFIG_FILE = "location_events.json"

    /** 全部 GPS 事件名（供订阅过滤与文档提示）。 */
    private val ALL_EVENTS = setOf(
        // 1. 开关/权限
        "location_provider_enabled",
        "location_provider_disabled",
        // 2. 位置数据
        "location_changed",
        "location_moved",
        "location_still",
        "location_first_fix",
        "location_lost",
        "location_accuracy_changed",
        // 3. 围栏（本地圆形自算，不依赖 GMS）
        "geofence_enter",
        "geofence_exit",
        "geofence_dwell",
        // 4. 活动（按速度推断，不依赖 GMS）
        "activity_changed",
        "activity_started",
        "activity_stopped",
        // 5. GNSS 卫星
        "gnss_started",
        "gnss_first_fix",
        "gnss_stopped",
        "satellite_status_changed",
        // 6. 传感器/派生
        "significant_motion",
        "distance_travelled"
    )

    private val PROVIDER_EVENTS = setOf("location_provider_enabled", "location_provider_disabled")

    private val LOCATION_EVENTS = setOf(
        "location_changed", "location_moved", "location_still", "location_first_fix",
        "location_lost", "location_accuracy_changed",
        "geofence_enter", "geofence_exit", "geofence_dwell",
        "activity_changed", "activity_started", "activity_stopped",
        "distance_travelled"
    )

    private val GNSS_EVENTS = setOf(
        "gnss_started", "gnss_first_fix", "gnss_stopped", "satellite_status_changed"
    )

    /** 需要周期 tick 才能产生的事件（静止/丢失/围栏停留）。 */
    private val TICK_EVENTS = setOf("location_still", "location_lost", "geofence_dwell")

    private val lock = Any()

    /** 待发事件：投递失败保留，5s 后重投。 */
    private val pending = ArrayDeque<Ev>()

    @Volatile
    private var retryScheduled = false

    @Volatile
    private var running = false

    @Volatile
    private var subscribed: Set<String> = emptySet()

    @Volatile
    private var stateJson: String = "{}"

    @Volatile
    private var handler: Handler? = null

    private var handlerThread: HandlerThread? = null
    private var lmRef: LocationManager? = null
    private var smRef: SensorManager? = null
    private var gnssCallback: GnssStatus.Callback? = null
    private var smListener: SensorEventListener? = null
    private var providerReceiverRegistered = false

    private var appContext: Context? = null

    /** 配置：默认值 + 文件/运行期合并；仅在锁内替换整个 JSONObject。 */
    private var config: JSONObject = defaultConfig()
    private var configLoaded = false

    // ---- 以下运行态只在 loc-event-monitor 线程读写 ----
    private var startedAt = 0L
    private var firstFixDone = false
    private var lostFired = false
    private var stillSince = 0L
    private var stillFired = false

    private var lastFixLat = 0.0
    private var lastFixLon = 0.0
    private var lastFixAcc = 0.0
    private var lastFixTs = 0L
    private var lastProvider = ""

    private var anchorLat = 0.0
    private var anchorLon = 0.0
    private var anchorAcc = 0.0
    private var lastAcceptedTs = 0L
    private var lastChangedEmitTs = 0L
    private var lastAccEmit = 0.0

    private var distanceAccum = 0.0
    private var totalDistance = 0.0

    private var activity = ""
    private var activitySince = 0L
    private var activityCandidate = ""
    private var activityCandidateSince = 0L

    private var lastSatEmitTs = 0L
    private var lastSatUsed = -1
    private var lastSatVisible = -1
    private var gnssFirstFixDone = false

    private val providerStates = HashMap<String, Boolean>()
    private val fences = ArrayList<Fence>()

    private data class Ev(val name: String, val payload: String)

    private class Fence(
        val id: String,
        val lat: Double,
        val lon: Double,
        val radius: Double,
        val dwellSec: Long
    ) {
        var inside = false
        var dwellStart = 0L
        var dwellEmitted = false
    }

    // ------------------------------------------------------------------ //
    // 生命周期
    // ------------------------------------------------------------------ //

    /** 进程启动时调用：只保存 context，不做任何监听（零开销）。 */
    @JvmStatic
    fun init(context: Context) {
        try {
            appContext = context.applicationContext
        } catch (t: Throwable) {
            FileLogger.w(TAG, "init 失败: ${t.javaClass.simpleName}")
        }
    }

    /** Python 事件任务变更后，把当前订阅集合转进来（由 SystemEventMonitor 转发）。 */
    @JvmStatic
    fun syncRegisteredEvents(events: Set<String>) {
        try {
            val gps = HashSet<String>()
            for (e in events) if (ALL_EVENTS.contains(e)) gps.add(e)
            applySubscriptions(gps)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "syncRegisteredEvents 失败: ${t.javaClass.simpleName}")
        }
    }

    /** 同上，JSON 数组字符串版本（方便 RPC 直接调用）。 */
    @JvmStatic
    fun syncRegisteredEventsJson(jsonArray: String?) {
        try {
            val arr = JSONArray(jsonArray ?: "[]")
            val set = HashSet<String>()
            for (i in 0 until arr.length()) {
                val name = arr.optString(i, "")
                if (name.isNotEmpty()) set.add(name)
            }
            syncRegisteredEvents(set)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "syncRegisteredEventsJson 失败: ${t.javaClass.simpleName}")
        }
    }

    @JvmStatic
    fun isRunning(): Boolean = running

    // ------------------------------------------------------------------ //
    // 动态配置
    // ------------------------------------------------------------------ //

    /**
     * 合并配置并持久化；运行中会重启监听使阈值/围栏立即生效。
     * 参数写错只影响对应键（回落默认值），不会崩溃。
     */
    @JvmStatic
    fun configure(json: String?): String {
        return try {
            if (json.isNullOrBlank()) return getConfig()
            val patch = JSONObject(json)
            synchronized(lock) {
                loadConfigLocked()
                config = mergeConfig(config, patch)
            }
            persistConfig()
            FileLogger.i(TAG, "配置已更新: ${getConfig()}")
            restartIfRunning()
            getConfig()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "configure 失败（保持原配置）: ${t.javaClass.simpleName}: ${t.message}")
            getConfig()
        }
    }

    @JvmStatic
    fun getConfig(): String {
        return try {
            synchronized(lock) {
                loadConfigLocked()
                config.toString()
            }
        } catch (t: Throwable) {
            FileLogger.w(TAG, "getConfig 失败: ${t.javaClass.simpleName}")
            defaultConfig().toString()
        }
    }

    @JvmStatic
    fun getState(): String {
        return try {
            val o = JSONObject(stateJson)
            o.put("running", running)
            o.put("subscribed", JSONArray(subscribed.sorted()))
            o.put("pending", synchronized(lock) { pending.size })
            o.toString()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "getState 失败: ${t.javaClass.simpleName}")
            "{}"
        }
    }

    /** 清零累计里程（distance_travelled 用）。 */
    @JvmStatic
    fun resetDistance() {
        try {
            val block = Runnable {
                distanceAccum = 0.0
                totalDistance = 0.0
                publishState()
            }
            val h = synchronized(lock) { handler }
            if (h != null) h.post(block) else block.run()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "resetDistance 失败: ${t.javaClass.simpleName}")
        }
    }

    private fun defaultConfig(): JSONObject = JSONObject()
        .put("min_time_ms", 60000L)
        .put("min_distance_m", 1000.0)
        .put("tick_ms", 30000L)
        .put("providers", JSONArray().put("gps").put("network"))
        .put("move_meters", 1000.0)
        .put("still_meters", 50.0)
        .put("still_seconds", 300L)
        .put("lost_seconds", 600L)
        .put("max_accuracy_m", 500.0)
        .put("accuracy_delta_m", 50.0)
        .put("changed_min_ms", 60000L)
        .put("distance_travelled_interval_m", 5000.0)
        .put("satellite_interval_ms", 60000L)
        .put("significant_motion", true)
        .put(
            "activity",
            JSONObject()
                .put("walking_speed", 1.0)
                .put("running_speed", 3.0)
                .put("bicycle_speed", 6.0)
                .put("vehicle_speed", 15.0)
                .put("still_speed", 0.5)
                .put("hold_ms", 30000L)
        )
        .put("fences", JSONArray())

    private fun mergeConfig(base: JSONObject, patch: JSONObject): JSONObject {
        val out = JSONObject(base.toString())
        val keys = patch.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val value = patch.opt(key)
            val old = out.opt(key)
            if (value is JSONObject && old is JSONObject) {
                out.put(key, mergeConfig(old, value))
            } else if (value != null) {
                out.put(key, value)
            }
        }
        return out
    }

    private fun loadConfigLocked() {
        if (configLoaded) return
        val ctx = appContext ?: return
        configLoaded = true
        try {
            val file = File(ctx.filesDir, CONFIG_FILE)
            if (!file.isFile) return
            val patch = JSONObject(file.readText())
            config = mergeConfig(config, patch)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "读取配置失败，使用默认值: ${t.javaClass.simpleName}")
        }
    }

    private fun persistConfig() {
        try {
            val ctx = appContext ?: return
            val snapshot = synchronized(lock) { config.toString() }
            File(ctx.filesDir, CONFIG_FILE).writeText(snapshot)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "保存配置失败: ${t.javaClass.simpleName}")
        }
    }

    private fun restartIfRunning() {
        val block = Runnable {
            if (running) {
                stopLocked()
                startLocked()
            }
        }
        val h = synchronized(lock) { handler }
        if (h != null) h.post(block) else block.run()
    }

    // ------------------------------------------------------------------ //
    // 订阅驱动启停
    // ------------------------------------------------------------------ //

    private fun applySubscriptions(gps: Set<String>) {
        synchronized(lock) {
            if (gps == subscribed) return
            subscribed = gps
        }
        FileLogger.i(TAG, "GPS 事件订阅集合: $gps")
        if (gps.isEmpty()) {
            val h = synchronized(lock) { handler }
            val block = Runnable { if (running) stopLocked() }
            if (h != null) h.post(block) else block.run()
            return
        }
        val block = Runnable {
            if (running) {
                // 订阅集合变化：重建监听，保证 GNSS/传感器按需启停
                stopLocked()
                startLocked()
            } else {
                startLocked()
            }
        }
        val h = synchronized(lock) { handler }
        if (h != null) h.post(block) else block.run()
    }

    private fun hasLocationPermission(ctx: Context): Boolean {
        return ctx.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
            ctx.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
    }

    private fun startLocked() {
        if (running) return
        val ctx = appContext ?: return
        val sub = subscribed
        if (sub.isEmpty()) return
        synchronized(lock) { loadConfigLocked() }

        val needLocation = sub.any { LOCATION_EVENTS.contains(it) }
        val needProvider = sub.any { PROVIDER_EVENTS.contains(it) }
        val needGnss = sub.any { GNSS_EVENTS.contains(it) }
        val needMotion = sub.contains("significant_motion") &&
            synchronized(lock) { config.optBoolean("significant_motion", true) }
        val needTick = sub.any { TICK_EVENTS.contains(it) }

        if (!hasLocationPermission(ctx)) {
            FileLogger.w(TAG, "缺少定位权限，GPS 事件保持关闭")
            return
        }
        val lm = try {
            ctx.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
        } catch (t: Throwable) {
            null
        } ?: return

        val thread = HandlerThread("loc-event-monitor").apply { start() }
        val h = Handler(thread.looper)
        handlerThread = thread
        handler = h
        lmRef = lm
        running = true
        startedAt = System.currentTimeMillis()
        resetRuntimeState()
        parseFences()
        snapshotProviders(lm)

        if (needLocation) registerLocationUpdates(lm, h)
        if (needProvider) registerProviderReceiver(ctx, h)
        if (needGnss) registerGnss(lm, h)
        if (needMotion) registerSignificantMotion(ctx, h)
        if (needTick) h.postDelayed(tickRunnable, tickMs())

        publishState()
        drainPending()
        FileLogger.i(
            TAG,
            "GPS 监听已启动: loc=$needLocation provider=$needProvider gnss=$needGnss motion=$needMotion tick=$needTick"
        )
    }

    private fun stopLocked() {
        if (!running) {
            cleanupThreadLocked()
            return
        }
        running = false
        try {
            lmRef?.removeUpdates(locationListener)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "removeUpdates 失败: ${t.javaClass.simpleName}")
        }
        try {
            gnssCallback?.let { lmRef?.unregisterGnssStatusCallback(it) }
        } catch (t: Throwable) {
            FileLogger.w(TAG, "unregisterGnss 失败: ${t.javaClass.simpleName}")
        }
        try {
            smListener?.let { smRef?.unregisterListener(it) }
        } catch (t: Throwable) {
            FileLogger.w(TAG, "unregisterSensor 失败: ${t.javaClass.simpleName}")
        }
        try {
            if (providerReceiverRegistered) {
                appContext?.unregisterReceiver(providerReceiver)
            }
        } catch (t: Throwable) {
            FileLogger.w(TAG, "unregisterReceiver 失败: ${t.javaClass.simpleName}")
        }
        providerReceiverRegistered = false
        synchronized(lock) { pending.clear() }
        cleanupThreadLocked()
        publishState()
        FileLogger.i(TAG, "GPS 监听已停止（无订阅事件）")
    }

    private fun cleanupThreadLocked() {
        handler?.removeCallbacksAndMessages(null)
        retryScheduled = false
        handler = null
        try {
            handlerThread?.quitSafely()
        } catch (_: Throwable) {
        }
        handlerThread = null
        lmRef = null
        smRef = null
        gnssCallback = null
        smListener = null
    }

    private fun resetRuntimeState() {
        firstFixDone = false
        lostFired = false
        stillSince = 0L
        stillFired = false
        lastFixLat = 0.0
        lastFixLon = 0.0
        lastFixAcc = 0.0
        lastFixTs = 0L
        lastProvider = ""
        anchorLat = 0.0
        anchorLon = 0.0
        anchorAcc = 0.0
        lastAcceptedTs = 0L
        lastChangedEmitTs = 0L
        lastAccEmit = 0.0
        distanceAccum = 0.0
        totalDistance = 0.0
        activity = ""
        activitySince = 0L
        activityCandidate = ""
        activityCandidateSince = 0L
        lastSatEmitTs = 0L
        lastSatUsed = -1
        lastSatVisible = -1
        gnssFirstFixDone = false
        providerStates.clear()
        fences.clear()
    }

    // ------------------------------------------------------------------ //
    // 定位监听
    // ------------------------------------------------------------------ //

    private val locationListener = object : LocationListener {
        override fun onLocationChanged(location: Location) {
            if (!running) return
            try {
                onFix(location)
            } catch (t: Throwable) {
                FileLogger.w(TAG, "处理定位失败: ${t.javaClass.simpleName}")
            }
        }

        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}

        override fun onProviderEnabled(provider: String) {
            if (!running) return
            try {
                onProviderState(provider, true)
            } catch (t: Throwable) {
                FileLogger.w(TAG, "provider enabled 处理失败: ${t.javaClass.simpleName}")
            }
        }

        override fun onProviderDisabled(provider: String) {
            if (!running) return
            try {
                onProviderState(provider, false)
            } catch (t: Throwable) {
                FileLogger.w(TAG, "provider disabled 处理失败: ${t.javaClass.simpleName}")
            }
        }
    }

    private fun registerLocationUpdates(lm: LocationManager, h: Handler) {
        val cfg = synchronized(lock) { config }
        val minTime = cfg.optLong("min_time_ms", 60000L).coerceAtLeast(0L)
        val minDistance = cfg.optDouble("min_distance_m", 1000.0).toFloat()
        val wanted = ArrayList<String>()
        val arr = cfg.optJSONArray("providers")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val p = arr.optString(i, "")
                if (p.isNotEmpty()) wanted.add(p)
            }
        }
        if (wanted.isEmpty()) {
            wanted.add("gps")
            wanted.add("network")
        }
        for (provider in wanted) {
            try {
                if (!lm.isProviderEnabled(provider)) {
                    FileLogger.w(TAG, "provider 不可用，跳过: $provider")
                    continue
                }
                lm.requestLocationUpdates(provider, minTime, minDistance, locationListener, h.looper)
            } catch (t: Throwable) {
                FileLogger.w(TAG, "注册 $provider 定位失败: ${t.javaClass.simpleName}")
            }
        }
    }

    private fun onFix(loc: Location) {
        val now = System.currentTimeMillis()
        val cfg = synchronized(lock) { config }
        val maxAcc = cfg.optDouble("max_accuracy_m", 500.0)
        val acc = if (loc.hasAccuracy()) loc.accuracy.toDouble() else Double.MAX_VALUE
        if (maxAcc > 0 && acc > maxAcc) {
            // 精度太差不产生任何事件，避免静止随机跳动
            return
        }
        val provider = loc.provider ?: ""
        val lat = loc.latitude
        val lon = loc.longitude

        val prevFixTs = lastFixTs
        lastFixTs = now
        if (lostFired) lostFired = false

        if (!firstFixDone) {
            firstFixDone = true
            lastFixLat = lat
            lastFixLon = lon
            lastFixAcc = acc
            lastProvider = provider
            anchorLat = lat
            anchorLon = lon
            anchorAcc = acc
            lastAcceptedTs = now
            lastChangedEmitTs = now
            lastAccEmit = acc
            emit(
                "location_first_fix",
                JSONObject()
                    .put("lat", lat).put("lon", lon)
                    .put("acc", acc).put("provider", provider)
                    .put("since_start_ms", now - startedAt)
            )
            checkFences(lat, lon, now)
            publishState()
            return
        }

        val step = FloatArray(1)
        Location.distanceBetween(lastFixLat, lastFixLon, lat, lon, step)
        val stepM = step[0].toDouble()
        val dt = now - prevFixTs
        val sinceLastFix = if (dt > 0) dt else 0L

        // 多 provider 抖动去重：5s 内位移 < 50m 视为同一位置
        if (sinceLastFix in 0L..5000L && stepM < 50.0) {
            publishState()
            return
        }

        lastFixLat = lat
        lastFixLon = lon
        lastFixAcc = acc
        lastProvider = provider

        distanceAccum += stepM
        totalDistance += stepM

        val between = FloatArray(1)
        Location.distanceBetween(anchorLat, anchorLon, lat, lon, between)
        val distAnchor = between[0].toDouble()
        val speed = if (loc.hasSpeed()) loc.speed.toDouble() else computeSpeed(stepM, sinceLastFix)
        val bearing = if (loc.hasBearing()) loc.bearing.toDouble() else 0.0

        val moveMeters = cfg.optDouble("move_meters", 1000.0)
        val stillMeters = cfg.optDouble("still_meters", 50.0)
        val moveThreshold = maxOf(moveMeters, anchorAcc + acc)

        if (distAnchor >= moveThreshold) {
            emit(
                "location_moved",
                JSONObject()
                    .put("lat", lat).put("lon", lon)
                    .put("acc", acc).put("provider", provider)
                    .put("dist_m", distAnchor)
                    .put("since_ts", lastAcceptedTs)
                    .put("speed", speed)
                    .put("bearing", bearing)
                    .put("distance_total_m", totalDistance)
            )
            anchorLat = lat
            anchorLon = lon
            anchorAcc = acc
            lastAcceptedTs = now
            stillSince = 0L
            stillFired = false
        } else {
            if (distAnchor < stillMeters) {
                if (stillSince == 0L) stillSince = now
            } else {
                stillSince = 0L
                stillFired = false
            }
            val changedMin = cfg.optLong("changed_min_ms", 60000L)
            if (changedMin <= 0 || now - lastChangedEmitTs >= changedMin) {
                emit(
                    "location_changed",
                    JSONObject()
                        .put("lat", lat).put("lon", lon)
                        .put("acc", acc).put("provider", provider)
                        .put("dist_m", distAnchor)
                        .put("speed", speed)
                        .put("bearing", bearing)
                )
                lastChangedEmitTs = now
            }
        }

        val accDelta = cfg.optDouble("accuracy_delta_m", 50.0)
        if (accDelta > 0 && Math.abs(acc - lastAccEmit) >= accDelta) {
            emit(
                "location_accuracy_changed",
                JSONObject()
                    .put("lat", lat).put("lon", lon)
                    .put("acc", acc)
                    .put("previous_acc", lastAccEmit)
                    .put("delta_m", acc - lastAccEmit)
                    .put("provider", provider)
            )
        }
        lastAccEmit = acc

        checkFences(lat, lon, now)
        updateActivity(speed, now, cfg)
        checkDistanceTravelled(cfg)
        publishState()
    }

    private fun computeSpeed(stepM: Double, dtMs: Long): Double {
        if (dtMs <= 0) return 0.0
        return stepM / (dtMs / 1000.0)
    }

    private fun onProviderState(provider: String, enabled: Boolean) {
        val prev = providerStates[provider]
        providerStates[provider] = enabled
        if (prev == enabled) return
        emit(
            if (enabled) "location_provider_enabled" else "location_provider_disabled",
            JSONObject().put("provider", provider).put("enabled", enabled)
        )
    }

    private fun snapshotProviders(lm: LocationManager) {
        providerStates.clear()
        for (provider in listOf("gps", "network", "passive", "fused")) {
            try {
                providerStates[provider] = lm.isProviderEnabled(provider)
            } catch (_: Throwable) {
            }
        }
    }

    private fun checkProviderChanges() {
        val lm = lmRef ?: return
        for (provider in listOf("gps", "network", "passive", "fused")) {
            val enabled = try {
                lm.isProviderEnabled(provider)
            } catch (t: Throwable) {
                continue
            }
            val prev = providerStates[provider]
            if (prev != null && prev != enabled) {
                onProviderState(provider, enabled)
            } else if (prev == null) {
                providerStates[provider] = enabled
            }
        }
    }

    private val providerReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val h = handler ?: return
            h.post {
                if (running) {
                    try {
                        checkProviderChanges()
                    } catch (t: Throwable) {
                        FileLogger.w(TAG, "provider 广播处理失败: ${t.javaClass.simpleName}")
                    }
                }
            }
        }
    }

    private fun registerProviderReceiver(ctx: Context, h: Handler) {
        try {
            ctx.registerReceiver(providerReceiver, IntentFilter(LocationManager.PROVIDERS_CHANGED_ACTION))
            providerReceiverRegistered = true
        } catch (t: Throwable) {
            FileLogger.w(TAG, "注册 provider 广播失败: ${t.javaClass.simpleName}")
        }
    }

    // ------------------------------------------------------------------ //
    // 围栏
    // ------------------------------------------------------------------ //

    private fun parseFences() {
        fences.clear()
        val arr = synchronized(lock) { config.optJSONArray("fences") } ?: return
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val id = o.optString("id", "fence$i")
            val lat = o.optDouble("lat", Double.NaN)
            val lon = o.optDouble("lon", Double.NaN)
            if (lat.isNaN() || lon.isNaN()) {
                FileLogger.w(TAG, "围栏 $id 缺少 lat/lon，已跳过")
                continue
            }
            fences.add(
                Fence(
                    id,
                    lat,
                    lon,
                    o.optDouble("radius", 200.0),
                    o.optLong("dwell_s", 300L)
                )
            )
        }
    }

    private fun checkFences(lat: Double, lon: Double, now: Long) {
        if (fences.isEmpty()) return
        val sub = subscribed
        if (!sub.contains("geofence_enter") && !sub.contains("geofence_exit") &&
            !sub.contains("geofence_dwell")
        ) return
        val res = FloatArray(1)
        for (fence in fences) {
            Location.distanceBetween(lat, lon, fence.lat, fence.lon, res)
            val distance = res[0].toDouble()
            val inside = distance <= fence.radius
            if (inside != fence.inside) {
                fence.inside = inside
                fence.dwellStart = now
                fence.dwellEmitted = false
                emit(
                    if (inside) "geofence_enter" else "geofence_exit",
                    JSONObject()
                        .put("fence_id", fence.id)
                        .put("lat", lat).put("lon", lon)
                        .put("distance_m", distance)
                        .put("radius_m", fence.radius)
                )
            }
        }
    }

    // ------------------------------------------------------------------ //
    // 活动识别（按速度推断）
    // ------------------------------------------------------------------ //

    private fun updateActivity(speed: Double, now: Long, cfg: JSONObject) {
        val sub = subscribed
        if (!sub.contains("activity_changed") && !sub.contains("activity_started") &&
            !sub.contains("activity_stopped")
        ) return
        val a = cfg.optJSONObject("activity") ?: JSONObject()
        val candidate = classifyActivity(speed, a)
        if (candidate != activityCandidate) {
            activityCandidate = candidate
            activityCandidateSince = now
        }
        val holdMs = a.optLong("hold_ms", 30000L)
        if (activityCandidate == activity && activity.isNotEmpty()) return
        if (activity.isNotEmpty() && now - activityCandidateSince < holdMs) return

        val previous = activity
        activity = activityCandidate
        val durationSec = if (activitySince > 0) (now - activitySince) / 1000 else 0L
        activitySince = now

        emit(
            "activity_changed",
            JSONObject()
                .put("activity", activity)
                .put("previous", previous)
                .put("speed", speed)
                .put("duration_s", durationSec)
        )
        if (activity == "still") {
            if (previous.isNotEmpty() && previous != "still") {
                emit(
                    "activity_stopped",
                    JSONObject()
                        .put("activity", previous)
                        .put("speed", speed)
                        .put("duration_s", durationSec)
                )
            }
        } else if (previous.isEmpty() || previous == "still") {
            emit(
                "activity_started",
                JSONObject().put("activity", activity).put("speed", speed)
            )
        }
    }

    private fun classifyActivity(speed: Double, a: JSONObject): String {
        val still = a.optDouble("still_speed", 0.5)
        val walking = a.optDouble("walking_speed", 1.0)
        val running = a.optDouble("running_speed", 3.0)
        val bicycle = a.optDouble("bicycle_speed", 6.0)
        val vehicle = a.optDouble("vehicle_speed", 15.0)
        return when {
            speed < still -> "still"
            speed < walking -> "walking"
            speed < running -> "running"
            speed < bicycle -> "bicycle"
            speed < vehicle -> "vehicle"
            else -> "vehicle"
        }
    }

    // ------------------------------------------------------------------ //
    // 里程
    // ------------------------------------------------------------------ //

    private fun checkDistanceTravelled(cfg: JSONObject) {
        if (!subscribed.contains("distance_travelled")) return
        val interval = cfg.optDouble("distance_travelled_interval_m", 5000.0)
        if (interval <= 0) return
        while (distanceAccum >= interval) {
            emit(
                "distance_travelled",
                JSONObject()
                    .put("distance_m", interval)
                    .put("interval_m", interval)
                    .put("distance_total_m", totalDistance)
            )
            distanceAccum -= interval
        }
    }

    // ------------------------------------------------------------------ //
    // GNSS 卫星
    // ------------------------------------------------------------------ //

    private fun registerGnss(lm: LocationManager, h: Handler) {
        try {
            val callback = object : GnssStatus.Callback() {
                override fun onStarted() {
                    if (!running) return
                    gnssFirstFixDone = false
                    emit("gnss_started", JSONObject().put("provider", "gps"))
                }

                override fun onStopped() {
                    if (!running) return
                    gnssFirstFixDone = false
                    emit("gnss_stopped", JSONObject().put("provider", "gps"))
                }

                override fun onFirstFix(ttffMillis: Int) {
                    if (!running || gnssFirstFixDone) return
                    gnssFirstFixDone = true
                    emit("gnss_first_fix", JSONObject().put("ttff_ms", ttffMillis))
                }

                override fun onSatelliteStatusChanged(status: GnssStatus) {
                    if (!running) return
                    try {
                        onSatelliteStatus(status)
                    } catch (t: Throwable) {
                        FileLogger.w(TAG, "卫星状态处理失败: ${t.javaClass.simpleName}")
                    }
                }
            }
            gnssCallback = callback
            lm.registerGnssStatusCallback(callback, h)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "注册 GNSS 回调失败: ${t.javaClass.simpleName}")
        }
    }

    private fun onSatelliteStatus(status: GnssStatus) {
        if (!subscribed.contains("satellite_status_changed")) return
        val now = System.currentTimeMillis()
        val visible = status.satelliteCount
        var used = 0
        var sum = 0.0
        var max = 0.0
        for (i in 0 until visible) {
            if (status.usedInFix(i)) {
                used++
                val cn0 = status.getCn0DbHz(i).toDouble()
                sum += cn0
                if (cn0 > max) max = cn0
            }
        }
        val avg = if (used > 0) sum / used else 0.0
        val cfg = synchronized(lock) { config }
        val interval = cfg.optLong("satellite_interval_ms", 60000L)
        val changed = used != lastSatUsed || visible != lastSatVisible
        if (!changed && (interval <= 0 || now - lastSatEmitTs < interval)) return
        lastSatUsed = used
        lastSatVisible = visible
        lastSatEmitTs = now
        emit(
            "satellite_status_changed",
            JSONObject()
                .put("used_in_fix", used)
                .put("visible", visible)
                .put("avg_cn0", avg)
                .put("max_cn0", max)
        )
    }

    // ------------------------------------------------------------------ //
    // 传感器 / 周期 tick
    // ------------------------------------------------------------------ //

    private val significantMotionListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            if (!running || event.sensor.type != Sensor.TYPE_SIGNIFICANT_MOTION) return
            try {
                emit("significant_motion", JSONObject().put("sensor", "significant_motion"))
            } catch (t: Throwable) {
                FileLogger.w(TAG, "significant_motion 投递失败: ${t.javaClass.simpleName}")
            }
            rearmSignificantMotion()
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    private fun registerSignificantMotion(ctx: Context, h: Handler) {
        try {
            val sm = ctx.getSystemService(Context.SENSOR_SERVICE) as? SensorManager ?: return
            val sensor = sm.getDefaultSensor(Sensor.TYPE_SIGNIFICANT_MOTION)
            if (sensor == null) {
                FileLogger.i(TAG, "设备不支持 TYPE_SIGNIFICANT_MOTION，忽略该事件")
                return
            }
            smRef = sm
            smListener = significantMotionListener
            sm.registerListener(significantMotionListener, sensor, SensorManager.SENSOR_DELAY_NORMAL, h)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "注册显著运动传感器失败: ${t.javaClass.simpleName}")
        }
    }

    private fun rearmSignificantMotion() {
        val sm = smRef ?: return
        val h = handler ?: return
        val sensor = try {
            sm.getDefaultSensor(Sensor.TYPE_SIGNIFICANT_MOTION)
        } catch (t: Throwable) {
            null
        } ?: return
        h.postDelayed(
            {
                if (!running) return@postDelayed
                try {
                    sm.registerListener(
                        significantMotionListener,
                        sensor,
                        SensorManager.SENSOR_DELAY_NORMAL,
                        h
                    )
                } catch (t: Throwable) {
                    FileLogger.w(TAG, "重新武装显著运动传感器失败: ${t.javaClass.simpleName}")
                }
            },
            1000L
        )
    }

    private fun tickMs(): Long {
        val cfg = synchronized(lock) { config }
        return cfg.optLong("tick_ms", 30000L).coerceIn(5000L, 300000L)
    }

    private val tickRunnable = object : Runnable {
        override fun run() {
            if (!running) return
            try {
                onTick()
            } catch (t: Throwable) {
                FileLogger.w(TAG, "tick 处理失败: ${t.javaClass.simpleName}")
            }
            val h = handler ?: return
            if (running) h.postDelayed(this, tickMs())
        }
    }

    private fun onTick() {
        val now = System.currentTimeMillis()
        val cfg = synchronized(lock) { config }

        if (firstFixDone && !lostFired) {
            val lostMs = cfg.optLong("lost_seconds", 600L) * 1000L
            if (lostMs > 0 && now - lastFixTs >= lostMs) {
                lostFired = true
                emit(
                    "location_lost",
                    JSONObject()
                        .put("last_ts", lastFixTs)
                        .put("ago_s", (now - lastFixTs) / 1000)
                        .put("lat", lastFixLat)
                        .put("lon", lastFixLon)
                        .put("acc", lastFixAcc)
                        .put("provider", lastProvider)
                )
            }
        }

        if (stillSince > 0L && !stillFired) {
            val stillMs = cfg.optLong("still_seconds", 300L) * 1000L
            if (stillMs > 0 && now - stillSince >= stillMs) {
                stillFired = true
                emit(
                    "location_still",
                    JSONObject()
                        .put("lat", lastFixLat)
                        .put("lon", lastFixLon)
                        .put("acc", lastFixAcc)
                        .put("still_seconds", (now - stillSince) / 1000)
                        .put("since_ts", stillSince)
                )
            }
        }

        for (fence in fences) {
            if (!fence.inside || fence.dwellEmitted) continue
            val dwellMs = fence.dwellSec * 1000L
            if (dwellMs <= 0 || now - fence.dwellStart < dwellMs) continue
            fence.dwellEmitted = true
            emit(
                "geofence_dwell",
                JSONObject()
                    .put("fence_id", fence.id)
                    .put("lat", lastFixLat)
                    .put("lon", lastFixLon)
                    .put("dwell_s", (now - fence.dwellStart) / 1000)
                    .put("radius_m", fence.radius)
            )
        }

        publishState()
    }

    // ------------------------------------------------------------------ //
    // 状态与投递
    // ------------------------------------------------------------------ //

    private fun publishState() {
        try {
            val o = JSONObject()
            o.put("running", running)
            o.put("first_fix", firstFixDone)
            o.put(
                "last",
                JSONObject()
                    .put("lat", lastFixLat)
                    .put("lon", lastFixLon)
                    .put("acc", lastFixAcc)
                    .put("ts", lastFixTs)
                    .put("provider", lastProvider)
            )
            o.put(
                "anchor",
                JSONObject()
                    .put("lat", anchorLat)
                    .put("lon", anchorLon)
                    .put("since_ts", lastAcceptedTs)
            )
            o.put("distance_total_m", totalDistance)
            o.put("distance_pending_m", distanceAccum)
            o.put("activity", activity)
            var inside = 0
            for (fence in fences) if (fence.inside) inside++
            o.put("fences_inside", inside)
            stateJson = o.toString()
        } catch (_: Throwable) {
        }
    }

    private fun emit(name: String, payload: JSONObject) {
        try {
            if (!subscribed.contains(name)) return
            if (!payload.has("ts")) payload.put("ts", System.currentTimeMillis())
            val ev = Ev(name, payload.toString())
            synchronized(lock) {
                pending.addLast(ev)
                while (pending.size > PENDING_MAX) pending.removeFirst()
            }
            drainPending()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "事件入队失败 $name: ${t.javaClass.simpleName}")
        }
    }

    private fun drainPending() {
        val first = synchronized(lock) { pending.firstOrNull() } ?: return
        if (!Python.isStarted()) {
            scheduleRetry()
            return
        }
        val py = try {
            Python.getInstance()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "Python 实例不可用，${first.name} 等待补投")
            scheduleRetry()
            return
        }
        while (true) {
            val ev = synchronized(lock) { pending.firstOrNull() } ?: return
            try {
                py.getModule(PY_MODULE).callAttr(PY_METHOD, ev.name, ev.payload)
                synchronized(lock) { pending.removeFirst() }
            } catch (t: Throwable) {
                FileLogger.w(TAG, "fire_event 投递失败，稍后重试: ${t.javaClass.simpleName}")
                scheduleRetry()
                return
            }
        }
    }

    private fun scheduleRetry() {
        var target: Handler? = null
        synchronized(lock) {
            if (retryScheduled) return
            retryScheduled = true
            target = handler
        }
        val h = target
        if (h == null) {
            retryScheduled = false
            return
        }
        h.postDelayed(
            {
                retryScheduled = false
                try {
                    drainPending()
                } catch (_: Throwable) {
                }
            },
            RETRY_DELAY_MS
        )
    }
}
