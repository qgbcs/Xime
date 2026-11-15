package com.kingzcheung.xime.task

import android.os.Handler
import android.os.HandlerThread
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONArray
import org.json.JSONObject

/**
 * 输入法侧通用事件桥：按键 / 剪贴板 / 语音文件 → Python task_manager 事件任务。
 *
 * 铁律：RPC 任务（含归档落盘）绝不能拖慢输入法主流程。因此：
 *  - 主流程只做一次订阅判断 + O(1) 入队，不碰文件 I/O，也不直接调用 Python；
 *  - 没有事件任务订阅时零额外开销（一次 volatile Set 查询即返回）；
 *  - 投递在独立 HandlerThread 上完成，任何异常只降级（丢事件），不向调用方抛出。
 *
 * 事件与 payload（具体落盘策略全部在 Python 任务里，Kotlin 不认识任何目录）：
 *  ime_keys        {"items":[[ts,key,shifted],...],"dropped":n}（200ms 合批）
 *  clipboard_text  {"ts":..., "text":"..."}
 *  speech_recorded {"ts":..., "path":"...","text":"...","started_ms":...,"duration_ms":...}
 */
object ImeEventBridge {

    private const val TAG = "ImeEventBridge"
    private const val PY_MODULE = "task_manager"
    private const val PY_METHOD = "fire_event"

    const val EVENT_KEYS = "ime_keys"
    const val EVENT_CLIPBOARD = "clipboard_text"
    const val EVENT_SPEECH = "speech_recorded"

    /** 单批最多缓存的按键数；超出丢最旧，避免异常场景无限堆积。 */
    private const val MAX_PENDING_KEYS = 200
    private const val MAX_PENDING_EVENTS = 64

    /** 按键合批窗口：一次开文件写多行，文件 I/O 次数从「每键一次」降到约 5 次/秒。 */
    private const val KEY_FLUSH_DELAY_MS = 200L

    private class KeyItem(val ts: Long, val key: String, val shifted: Boolean)

    private val lock = Any()
    private val pendingKeys = ArrayDeque<KeyItem>()
    private var droppedKeys = 0
    private val pendingEvents = ArrayDeque<Pair<String, String>>()
    private var flushScheduled = false

    private val handler: Handler by lazy {
        val thread = HandlerThread("ime-event-bridge").apply { start() }
        Handler(thread.looper)
    }
    private val flushTask = Runnable { flush() }

    /** 按键（输入法主线程高频调用）：只入队。 */
    fun key(key: String, shifted: Boolean) {
        if (!SystemEventMonitor.hasSubscriber(EVENT_KEYS)) return
        val item = KeyItem(System.currentTimeMillis(), key, shifted)
        val schedule = synchronized(lock) {
            if (pendingKeys.size >= MAX_PENDING_KEYS) {
                pendingKeys.removeFirst()
                droppedKeys++
            }
            pendingKeys.addLast(item)
            if (flushScheduled) {
                false
            } else {
                flushScheduled = true
                true
            }
        }
        if (schedule) postFlush(KEY_FLUSH_DELAY_MS)
    }

    /** 剪贴板文本（每次复制一次，低频）：只入队。 */
    fun clipboard(text: String) {
        if (!SystemEventMonitor.hasSubscriber(EVENT_CLIPBOARD)) return
        val payload = JSONObject()
            .put("ts", System.currentTimeMillis())
            .put("text", text)
        enqueue(EVENT_CLIPBOARD, payload.toString())
    }

    /** 语音录音文件（已在 App 私有目录）：目标位置由 Python 任务决定。 */
    fun speech(filePath: String, text: String?, startedAtMs: Long, durationMs: Long) {
        if (!SystemEventMonitor.hasSubscriber(EVENT_SPEECH)) return
        val payload = JSONObject()
            .put("ts", System.currentTimeMillis())
            .put("path", filePath)
            .put("text", text ?: "")
            .put("started_ms", startedAtMs)
            .put("duration_ms", durationMs)
        enqueue(EVENT_SPEECH, payload.toString())
    }

    private fun enqueue(name: String, payloadJson: String) {
        synchronized(lock) {
            if (pendingEvents.size >= MAX_PENDING_EVENTS) pendingEvents.removeFirst()
            pendingEvents.addLast(name to payloadJson)
        }
        postFlush(0L)
    }

    private fun postFlush(delayMs: Long) {
        try {
            handler.postDelayed(flushTask, delayMs)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "事件投递线程不可用: ${t.javaClass.simpleName}")
        }
    }

    private fun flush() {
        val snapshot = synchronized(lock) {
            flushScheduled = false
            val events = pendingEvents.toList()
            pendingEvents.clear()
            val keys = pendingKeys.toList()
            pendingKeys.clear()
            val dropped = droppedKeys
            droppedKeys = 0
            Triple(events, keys, dropped)
        }
        for ((name, payload) in snapshot.first) deliver(name, payload)
        val keys = snapshot.second
        if (keys.isNotEmpty()) {
            val items = JSONArray()
            for (item in keys) {
                items.put(JSONArray().put(item.ts).put(item.key).put(item.shifted))
            }
            val payload = JSONObject().put("items", items).put("dropped", snapshot.third)
            // 按键批次高频，不写 Python 侧 recent_events 环形缓冲，免得刷掉系统事件。
            deliver(EVENT_KEYS, payload.toString(), record = false)
        }
    }

    private fun deliver(name: String, payloadJson: String, record: Boolean = true) {
        try {
            if (!Python.isStarted()) return
            Python.getInstance().getModule(PY_MODULE)
                .callAttr(PY_METHOD, name, payloadJson, "ime", record)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "fire_event 投递失败 $name: ${t.javaClass.simpleName}")
        }
    }
}
