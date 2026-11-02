package com.kingzcheung.xime.task

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Handler
import android.os.HandlerThread
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONObject

/**
 * 全局网络边沿事件监听（仅主进程注册一次）。
 *
 * 事件（边沿触发，去抖；首帧只记录状态、不发边沿，避免每次进程启动都误触发任务）：
 *   wifi_connected / wifi_disconnected
 *   network_online  / network_offline   （NET_CAPABILITY_INTERNET）
 *   network_state   进程启动首帧的网络快照（非边沿，payload 带 initial=true）
 *
 * 回调全部串行在单 HandlerThread 上；事件通过 Chaquopy 投给 Python
 * task_manager.fire_event(name, payloadJson)。Python 未启动或调用失败时进入
 * 待发队列，由 5s 定时滴答补投，保证启动瞬间的事件不丢。
 *
 * 另提供 getState() 给 Python task_manager.network_state() 读当前快照
 * （wifi / online / validated / initialized / ts），任务可据此“已是目标状态就跳过”。
 *
 * 任务本身不在 APK 内硬编码：这里只负责“发生了什么”，收到事件后执行什么
 * Python 代码完全由 RPC 下发的事件任务决定。
 */
object NetworkEventMonitor {

    private const val TAG = "NetEventMonitor"
    private const val PY_MODULE = "task_manager"
    private const val PY_METHOD = "fire_event"
    private const val RETRY_DELAY_MS = 5000L
    private const val PENDING_MAX = 64

    /** 尚未收到任何网络回调时的快照：initialized=false，其余字段全零。 */
    private const val EMPTY_STATE =
        """{"wifi":false,"online":false,"validated":false,"initialized":false,"ts":0}"""

    private lateinit var cm: ConnectivityManager
    private lateinit var handler: Handler

    // 仅在 handler 线程访问
    private val pending = ArrayDeque<Ev>()
    private var started = false
    private var initialized = false
    private var defaultNet: Network? = null
    private var defaultIsWifi = false
    private var wifiOn = false
    private var online = false
    private var validated = false

    /** 最近一次网络快照 JSON：handler 线程写，任意线程读（getState 用）。 */
    @Volatile
    private var stateJson: String = EMPTY_STATE

    private data class Ev(val name: String, val payload: String)

    @JvmStatic
    fun start(context: Context) {
        if (started) return
        started = true
        cm = context.applicationContext
            .getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val thread = HandlerThread("net-event-monitor").apply { start() }
        handler = Handler(thread.looper)

        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                recompute(network, null)
            }

            override fun onCapabilitiesChanged(
                network: Network,
                caps: NetworkCapabilities,
            ) {
                recompute(network, caps)
            }

            override fun onLost(network: Network) {
                handleLost(network)
            }
        }
        try {
            cm.registerDefaultNetworkCallback(callback, handler)
            FileLogger.i(TAG, "默认网络回调已注册")
        } catch (t: Throwable) {
            FileLogger.e(TAG, "注册网络回调失败", t)
        }

        // Python 未就绪期间的事件补投滴答
        handler.postDelayed(object : Runnable {
            override fun run() {
                drainPending()
                handler.postDelayed(this, RETRY_DELAY_MS)
            }
        }, RETRY_DELAY_MS)
    }

    private fun recompute(network: Network, caps0: NetworkCapabilities?) {
        val caps = caps0 ?: try {
            cm.getNetworkCapabilities(network)
        } catch (t: Throwable) {
            null
        } ?: return
        // registerDefaultNetworkCallback 理论上只回调默认网络，这里再兜底过滤一次
        if (defaultNet != null && network != defaultNet) {
            defaultNet = network
        }
        defaultIsWifi = caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
        val isWifi = defaultIsWifi
        // 注意：不能用 NET_CAPABILITY_VALIDATED 判在线——部分华为 ROM 即便网络完全
        // 可用也长期置 NOT_VALIDATED/CAPTIVE_PORTAL，会导致 online 事件永不触发。
        // INTERNET 能力位在网络连接时即具备、onLost/失效时消失，更贴合“断网/连网”。
        val isOnline = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        val isValidated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)

        if (!initialized) {
            // 首帧只同步当前状态，不发“边沿”事件（进程重启不应被当成“刚连上 WiFi”）
            initialized = true
            wifiOn = isWifi
            online = isOnline
            validated = isValidated
            publishState(isWifi, isOnline, isValidated)
            FileLogger.i(TAG, "初始网络状态: wifi=$isWifi online=$isOnline")
            // 但额外投一次“快照事件”：名字与 wifi_connected 的纯边沿语义区分，
            // payload 带 initial=true，供“开机/进程启动后要确保某状态”的任务订阅。
            emit("network_state", isWifi, isOnline, isValidated, initial = true)
            return
        }

        validated = isValidated
        publishState(isWifi, isOnline, isValidated)
        if (isWifi && !wifiOn) {
            wifiOn = true
            emit("wifi_connected", isWifi, isOnline, isValidated)
        } else if (!isWifi && wifiOn) {
            wifiOn = false
            emit("wifi_disconnected", isWifi, isOnline, isValidated)
        }
        if (isOnline && !online) {
            online = true
            emit("network_online", isWifi, isOnline, isValidated)
        } else if (!isOnline && online) {
            online = false
            emit("network_offline", isWifi, isOnline, isValidated)
        }
    }

    private fun handleLost(network: Network) {
        if (defaultNet != null && network != defaultNet) return
        val wasWifi = defaultIsWifi
        defaultNet = null
        defaultIsWifi = false
        if (!initialized) {
            initialized = true
            validated = false
            publishState(false, false, false)
            return
        }
        if (wifiOn && wasWifi) {
            wifiOn = false
            emit("wifi_disconnected", false, false, false)
        }
        if (online) {
            online = false
            emit("network_offline", wifiOn, false, false)
        }
        validated = false
        publishState(wifiOn, online, false)
    }

    /**
     * 把当前网络状态写成 JSON 快照，供 getState()（Python `network_state()`）读。
     * 只在网络回调 HandlerThread 上写，读侧只读 @Volatile 的整串，不做字段级并发读。
     */
    private fun publishState(wifi: Boolean, online: Boolean, valid: Boolean) {
        stateJson = JSONObject()
            .put("wifi", wifi)
            .put("online", online)
            .put("validated", valid)
            .put("initialized", true)
            .put("ts", System.currentTimeMillis())
            .toString()
    }

    /**
     * 当前默认网络快照（Python `task_manager.network_state()` 的底层）。
     * 进程刚起、首个网络回调未到之前返回 initialized=false 的全零快照。
     */
    @JvmStatic
    fun getState(): String = stateJson

    private fun emit(
        name: String,
        wifi: Boolean,
        internet: Boolean,
        validated: Boolean,
        initial: Boolean = false,
    ) {
        val json = JSONObject()
            .put("ts", System.currentTimeMillis())
            .put("wifi", wifi)
            .put("internet", internet)
            .put("validated", validated)
        if (initial) json.put("initial", true)
        val payload = json.toString()
        pending.addLast(Ev(name, payload))
        while (pending.size > PENDING_MAX) pending.removeFirst()
        FileLogger.i(TAG, "事件入队: $name payload=$payload 待发=${pending.size}")
        drainPending()
    }

    /** 尽力把待发事件投给 Python；任何一个失败就保留剩余事件等下次滴答。 */
    private fun drainPending() {
        if (pending.isEmpty()) return
        if (!Python.isStarted()) return
        val py = try {
            Python.getInstance()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "Python 实例暂不可用，${pending.size} 条事件等待补投")
            return
        }
        var delivered = 0
        while (pending.isNotEmpty()) {
            val ev = pending.first()
            try {
                py.getModule(PY_MODULE).callAttr(PY_METHOD, ev.name, ev.payload)
                pending.removeFirst()
                delivered++
            } catch (t: Throwable) {
                FileLogger.w(TAG, "fire_event 投递失败，剩余 ${pending.size} 条稍后重试: ${t.javaClass.simpleName}")
                break
            }
        }
        if (delivered > 0) FileLogger.i(TAG, "已投递 $delivered 条网络事件到 Python")
    }
}
