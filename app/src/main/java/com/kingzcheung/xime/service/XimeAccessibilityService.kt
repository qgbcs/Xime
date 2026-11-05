package com.kingzcheung.xime.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/**
 * RPC 驱动的辅助功能服务：用 GestureDescription 注入触摸手势。
 *
 * 支持点击、长按、滑动、拖拽、双指缩放，以及任意多指/多段连续手势。
 * 启用方式：系统设置里打开本服务，或用 adb 写 secure 设置：
 * `settings put secure enabled_accessibility_services <pkg>/com.kingzcheung.xime.service.XimeAccessibilityService`
 *
 * 所有对外入口都是 companion 里的 @JvmStatic，Python 侧可直接 jclass 调用。
 */
class XimeAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    /**
     * 事件监听默认关闭。只有 RPC/MCP 显式调用 setEventCapture(true, ttl) 后才记录；
     * 回调里只做几次原子/volatile 写入，绝不 dump、不拼 JSON、不联网，避免拖慢主线程。
     */
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null || !eventsEnabled) return
        val now = System.currentTimeMillis()
        if (now > eventsUntilMs) {
            eventsEnabled = false
            return
        }
        eventVersion.incrementAndGet()
        lastEventType.set(event.eventType)
        lastEventPkg = event.packageName?.toString().orEmpty()
        lastEventClass = event.className?.toString().orEmpty()
        lastEventAtMs.set(now)
        lastEventWindowId.set(event.windowId)
    }

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    companion object {
        @Volatile
        private var instance: XimeAccessibilityService? = null

        /** 最近一次手势的失败原因，成功时清空，供 RPC 诊断用。 */
        @Volatile
        private var lastError: String = ""

        @Volatile
        private var lastAtMs: Long = 0L

        // 事件监听开关：默认全关，只有显式开启且在 TTL 内才记录，常态零开销。
        @Volatile
        private var eventsEnabled = false

        @Volatile
        private var eventsUntilMs = 0L

        private val eventVersion = AtomicLong(0L)
        private val lastEventType = AtomicInteger(0)
        private val lastEventAtMs = AtomicLong(0L)
        private val lastEventWindowId = AtomicInteger(-1)

        @Volatile
        private var lastEventPkg: String = ""

        @Volatile
        private var lastEventClass: String = ""

        // 截图回调在同一后台线程执行，避免在主线程上等待回调造成死锁。
        private val shotExecutor: ExecutorService by lazy { Executors.newSingleThreadExecutor() }

        private val nodeRefPattern = Regex("w(\\d+):p([0-9.]*)")

        private val mainHandler: Handler by lazy { Handler(Looper.getMainLooper()) }

        private const val DEFAULT_TAP_MS = 80L
        private const val DEFAULT_LONG_PRESS_MS = 600L
        private const val DEFAULT_SWIPE_MS = 300L
        private const val DEFAULT_DRAG_HOLD_MS = 600L
        private const val CALLBACK_EXTRA_MS = 1500L

        @JvmStatic
        fun isConnected(): Boolean = instance != null

        @JvmStatic
        fun status(): String {
            val connected = instance != null
            return JSONObject()
                .put("ok", connected)
                .put("connected", connected)
                .put("maxStrokes", maxStrokeCount())
                .put("maxDurationMs", maxGestureDuration())
                .toString()
        }

        /** 最近一次手势的执行结果，用于定位“返回 false 但不知道原因”的问题。 */
        @JvmStatic
        fun lastResult(): String = JSONObject()
            .put("connected", instance != null)
            .put("lastError", lastError)
            .put("lastAtMs", lastAtMs)
            .put("maxStrokes", maxStrokeCount())
            .put("maxDurationMs", maxGestureDuration())
            .toString()

        @JvmStatic
        fun tap(x: Float, y: Float): Boolean = tap(x, y, DEFAULT_TAP_MS)

        @JvmStatic
        fun tap(x: Float, y: Float, durationMs: Long): Boolean =
            perform(listOf(strokeOf(listOf(x to y), 0L, durationMs, false)))

        @JvmStatic
        fun longPress(x: Float, y: Float): Boolean = longPress(x, y, DEFAULT_LONG_PRESS_MS)

        @JvmStatic
        fun longPress(x: Float, y: Float, durationMs: Long): Boolean =
            perform(listOf(strokeOf(listOf(x to y), 0L, durationMs, false)))

        @JvmStatic
        fun swipe(x1: Float, y1: Float, x2: Float, y2: Float): Boolean =
            swipe(x1, y1, x2, y2, DEFAULT_SWIPE_MS)

        @JvmStatic
        fun swipe(x1: Float, y1: Float, x2: Float, y2: Float, durationMs: Long): Boolean =
            perform(listOf(strokeOf(listOf(x1 to y1, x2 to y2), 0L, durationMs, false)))

        /**
         * 拖拽：先长按 holdMs 再在 durationMs 内移动到目标点，全程不松手。
         * 手势时间按路径长度分配，所以长按用起点附近的小幅折返凑长度：
         * 位移始终在 2 倍 amplitude 内（远小于 touch slop），不会触发取消。
         */
        @JvmStatic
        fun drag(x1: Float, y1: Float, x2: Float, y2: Float): Boolean =
            drag(x1, y1, x2, y2, DEFAULT_DRAG_HOLD_MS, DEFAULT_SWIPE_MS)

        @JvmStatic
        fun drag(x1: Float, y1: Float, x2: Float, y2: Float, holdMs: Long, durationMs: Long): Boolean {
            val hold = holdMs.coerceAtLeast(1L)
            val move = durationMs.coerceAtLeast(1L)
            val dx = x2 - x1
            val dy = y2 - y1
            val distance = Math.sqrt((dx * dx + dy * dy).toDouble()).toFloat().coerceAtLeast(1f)
            val amplitude = 3f
            val holdLength = distance * hold.toFloat() / move.toFloat()
            val cycles = (holdLength / (2f * amplitude)).toInt().coerceIn(1, 300)
            val points = ArrayList<Pair<Float, Float>>(cycles * 2 + 2)
            points.add(x1 to y1)
            for (i in 0 until cycles) {
                points.add((x1 + amplitude) to y1)
                points.add((x1 - amplitude) to y1)
            }
            points.add(x2 to y2)
            return perform(listOf(strokeOf(points, 0L, hold + move, false)))
        }

        /** 双指缩放：两指从 startRadius 对称移动到 endRadius。 */
        @JvmStatic
        fun pinch(centerX: Float, centerY: Float, startRadius: Float, endRadius: Float): Boolean =
            pinch(centerX, centerY, startRadius, endRadius, 400L)

        @JvmStatic
        fun pinch(
            centerX: Float,
            centerY: Float,
            startRadius: Float,
            endRadius: Float,
            durationMs: Long,
        ): Boolean =
            perform(
                listOf(
                    strokeOf(
                        listOf((centerX - startRadius) to centerY, (centerX - endRadius) to centerY),
                        0L,
                        durationMs,
                        false,
                    ),
                    strokeOf(
                        listOf((centerX + startRadius) to centerY, (centerX + endRadius) to centerY),
                        0L,
                        durationMs,
                        false,
                    ),
                )
            )

        /**
         * 通用手势，JSON 格式：
         * {"strokes":[{"points":[[x,y],[x,y]],"start":0,"duration":300,
         *               "willContinue":false,"continuePrevious":false}]}
         * points 至少一个点；continuePrevious=true 时链接到上一条 stroke（同一根手指，不抬指），
         * 此时上一条的 willContinue 必须为 true。
         * 返回 {"ok":bool,"completed":bool,"strokes":n} 或 {"ok":false,"error":...}
         */
        @JvmStatic
        fun gesture(json: String): String =
            try {
                val root = JSONObject(json)
                val strokesJson = root.optJSONArray("strokes") ?: JSONArray()
                if (strokesJson.length() == 0) {
                    JSONObject().put("ok", false).put("error", "empty_strokes").toString()
                } else {
                    val strokes = ArrayList<GestureDescription.StrokeDescription>(strokesJson.length())
                    var previous: GestureDescription.StrokeDescription? = null
                    for (i in 0 until strokesJson.length()) {
                        val obj = strokesJson.optJSONObject(i)
                            ?: return JSONObject()
                                .put("ok", false)
                                .put("error", "stroke_${i}_not_object")
                                .toString()
                        val pointsJson = obj.optJSONArray("points")
                        if (pointsJson == null || pointsJson.length() == 0) {
                            return JSONObject()
                                .put("ok", false)
                                .put("error", "stroke_${i}_missing_points")
                                .toString()
                        }
                        val points = ArrayList<Pair<Float, Float>>(pointsJson.length())
                        for (p in 0 until pointsJson.length()) {
                            val pt = pointsJson.optJSONArray(p)
                            if (pt == null || pt.length() < 2) {
                                return JSONObject()
                                    .put("ok", false)
                                    .put("error", "stroke_${i}_point_${p}_invalid")
                                    .toString()
                            }
                            points.add(pt.getDouble(0).toFloat() to pt.getDouble(1).toFloat())
                        }
                        val start = obj.optLong("start", 0L)
                        val duration = obj.optLong("duration", DEFAULT_SWIPE_MS)
                        val willContinue = obj.optBoolean("willContinue", false)
                        val continuePrevious = obj.optBoolean("continuePrevious", false)
                        val prev = previous
                        val stroke = if (continuePrevious && prev != null) {
                            prev.continueStroke(pathOf(points), start, duration.coerceAtLeast(1L), willContinue)
                        } else {
                            strokeOf(points, start, duration, willContinue)
                        }
                        strokes.add(stroke)
                        previous = stroke
                    }
                    val completed = perform(strokes)
                    JSONObject()
                        .put("ok", completed)
                        .put("completed", completed)
                        .put("strokes", strokes.size)
                        .toString()
                }
            } catch (e: Throwable) {
                JSONObject()
                    .put("ok", false)
                    .put("error", e.toString())
                    .toString()
            }

        /**
         * 读取当前界面节点树（需 canRetrieveWindowContent）。
         *
         * 返回 {"ok","pkg","count","nodes":[{"t" 文本,"d" 描述,"id" 资源 ID,
         * "c" 类名,"b" [left,top,right,bottom],"k" 可点击,"e" 可编辑,"f" 聚焦,"s" 可滚动}]}。
         * 按窗口顺序广度优先遍历，最多 maxNodes 个；includeEmpty=false 时丢弃无文本且不可交互的空节点。
         */
        @JvmStatic
        fun dumpUi(maxNodes: Int, includeEmpty: Boolean): String {
            val service = instance ?: return JSONObject()
                .put("ok", false).put("error", "service_not_connected").toString()
            val limit = maxNodes.coerceIn(1, 2000)
            return try {
                val nodesJson = JSONArray()
                var counted = 0
                var pkg = ""
                data class RootRef(val index: Int, val windowId: Int, val root: AccessibilityNodeInfo)
                val roots = ArrayList<RootRef>()
                try {
                    service.windows?.forEachIndexed { index, window ->
                        window.root?.let { roots.add(RootRef(index, window.id, it)) }
                    }
                } catch (e: Throwable) {
                    // 个别 ROM 不让读多窗口，退化到当前活动窗口。
                }
                if (roots.isEmpty()) {
                    service.rootInActiveWindow?.let { roots.add(RootRef(0, it.windowId, it)) }
                }
                if (roots.isEmpty()) {
                    JSONObject().put("ok", false).put("error", "no_window_root").toString()
                } else {
                    val queue = ArrayDeque<Triple<AccessibilityNodeInfo, String, RootRef>>()
                    roots.forEach { entry -> queue.addLast(Triple(entry.root, "", entry)) }
                    while (queue.isNotEmpty() && counted < limit) {
                        val (node, path, entry) = queue.removeFirst()
                        val text = node.text?.toString()?.trim().orEmpty()
                        val desc = node.contentDescription?.toString()?.trim().orEmpty()
                        val keep = includeEmpty || text.isNotEmpty() || desc.isNotEmpty() ||
                            node.isClickable || node.isEditable
                        if (keep) {
                            val rect = Rect()
                            node.getBoundsInScreen(rect)
                            val bounds = JSONArray()
                            bounds.put(rect.left)
                            bounds.put(rect.top)
                            bounds.put(rect.right)
                            bounds.put(rect.bottom)
                            nodesJson.put(
                                JSONObject()
                                    .put("t", text)
                                    .put("d", desc)
                                    .put("id", node.viewIdResourceName.orEmpty())
                                    .put("c", node.className?.toString().orEmpty())
                                    .put("b", bounds)
                                    .put("k", node.isClickable)
                                    .put("e", node.isEditable)
                                    .put("f", node.isFocused)
                                    .put("s", node.isScrollable)
                                    .put("w", entry.windowId)
                                    .put("r", "w" + entry.index + ":p" + path)
                            )
                            if (pkg.isEmpty()) pkg = node.packageName?.toString().orEmpty()
                            counted++
                        }
                        for (i in 0 until node.childCount) {
                            val child = node.getChild(i) ?: continue
                            queue.addLast(Triple(child, if (path.isEmpty()) "$i" else "$path.$i", entry))
                        }
                    }
                    JSONObject()
                        .put("ok", true)
                        .put("pkg", pkg)
                        .put("count", counted)
                        .put("nodes", nodesJson)
                        .toString()
                }
            } catch (e: Throwable) {
                JSONObject().put("ok", false).put("error", "$e").toString()
            }
        }

        /** 全局动作（AccessibilityService 常量）：1=返回 2=Home 3=最近任务 4=通知栏 5=快捷设置 6=电源菜单 8=锁屏 9=截图。 */
        @JvmStatic
        fun globalAction(action: Int): Boolean {
            val service = instance ?: return fail("service_not_connected")
            return try {
                if (service.performGlobalAction(action)) succeed() else fail("global_action_rejected:$action")
            } catch (e: Throwable) {
                fail("global_action_failed:$e")
            }
        }

        /** 用 Intent 换起界面（action 如 android.settings.SETTINGS），后台启动可能被系统拦截。 */
        @JvmStatic
        fun startActivityAction(action: String): Boolean {
            val service = instance ?: return fail("service_not_connected")
            return try {
                val intent = Intent(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                service.startActivity(intent)
                succeed()
            } catch (e: Throwable) {
                fail("start_activity_failed:$e")
            }
        }

        /**
         * 开启/关闭事件监听。TtlMs 到点自动关闭，避免客户端异常退出后一直记录耗电。
         * 默认关闭，只有 RPC/MCP 显式调用才会打开。
         */
        @JvmStatic
        fun setEventCapture(enabled: Boolean, ttlMs: Long): Boolean {
            if (enabled) {
                eventsUntilMs = System.currentTimeMillis() + ttlMs.coerceIn(1000L, 600000L)
                eventsEnabled = true
            } else {
                eventsEnabled = false
                eventsUntilMs = 0L
            }
            return true
        }

        /** 读取事件版本号与最近事件；监听关闭时 version 保持不变。 */
        @JvmStatic
        fun eventState(): String {
            val enabled = eventsEnabled && System.currentTimeMillis() <= eventsUntilMs
            return JSONObject()
                .put("ok", true)
                .put("enabled", enabled)
                .put("version", eventVersion.get())
                .put("eventType", lastEventType.get())
                .put("eventTypeName", eventTypeName(lastEventType.get()))
                .put("pkg", lastEventPkg)
                .put("class", lastEventClass)
                .put("windowId", lastEventWindowId.get())
                .put("atMs", lastEventAtMs.get())
                .put("remainingMs", if (enabled) (eventsUntilMs - System.currentTimeMillis()).coerceAtLeast(0L) else 0L)
                .toString()
        }

        /** 多窗口元信息；index 与 dump_ui 的 r 字段（w0/w1）保持一致。 */
        @JvmStatic
        fun windows(): String {
            val service = instance ?: return errorJson("windows", "service_not_connected")
            val array = JSONArray()
            try {
                service.windows?.forEachIndexed { index, window ->
                    val bounds = Rect()
                    window.getBoundsInScreen(bounds)
                    val root = window.root
                    array.put(
                        JSONObject()
                            .put("index", index)
                            .put("id", window.id)
                            .put("type", window.type)
                            .put("layer", window.layer)
                            .put("active", window.isActive)
                            .put("focused", window.isFocused)
                            .put("title", window.title?.toString().orEmpty())
                            .put("bounds", rectJson(bounds))
                            .put("pkg", root?.packageName?.toString().orEmpty())
                    )
                    root?.recycle()
                }
                return JSONObject().put("ok", true).put("op", "windows")
                    .put("count", array.length()).put("windows", array).toString()
            } catch (e: Throwable) {
                return errorJson("windows", "windows_failed:$e")
            }
        }

        /**
         * 按 dump_ui 返回的 r 字段（如 w0:p1.2）执行节点动作。
         * 每次调用都按路径重新定位节点，不缓存 AccessibilityNodeInfo。
         */
        @JvmStatic
        fun performNodeAction(ref: String, action: String, text: String): String {
            val node = resolveNode(ref) ?: return errorJson("perform_action", "node_ref_not_found:$ref")
            try {
                val wanted = action.trim().lowercase()
                val args = Bundle()
                val code = when (wanted) {
                    "click" -> AccessibilityNodeInfo.ACTION_CLICK
                    "long_click", "longclick" -> AccessibilityNodeInfo.ACTION_LONG_CLICK
                    "scroll_forward" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                    "scroll_backward" -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                    "select" -> AccessibilityNodeInfo.ACTION_SELECT
                    "focus" -> AccessibilityNodeInfo.ACTION_FOCUS
                    "clear_focus" -> AccessibilityNodeInfo.ACTION_CLEAR_FOCUS
                    "expand" -> AccessibilityNodeInfo.ACTION_EXPAND
                    "collapse" -> AccessibilityNodeInfo.ACTION_COLLAPSE
                    "dismiss" -> AccessibilityNodeInfo.ACTION_DISMISS
                    "paste" -> AccessibilityNodeInfo.ACTION_PASTE
                    "set_text" -> {
                        args.putCharSequence(
                            AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text
                        )
                        AccessibilityNodeInfo.ACTION_SET_TEXT
                    }
                    else -> return errorJson("perform_action", "unknown_action:$action", nodeInfo(node))
                }

                // PASTE 只在已聚焦的输入框上生效，先补一次 ACTION_FOCUS。
                if (wanted == "paste") {
                    node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
                }
                val ok = node.performAction(code, args)
                val resolved = nodeInfo(node)
                if (!ok) {
                    return JSONObject().put("ok", false).put("op", "perform_action")
                        .put("action", wanted).put("ref", ref)
                        .put("error", "action_rejected:$wanted")
                        .put("resolved", resolved).toString()
                }
                if (wanted == "set_text") {
                    val actual = verifyText(ref, text)
                    if (actual != text) {
                        return JSONObject().put("ok", false).put("op", "perform_action")
                            .put("action", wanted).put("ref", ref)
                            .put("error", "text_verify_failed")
                            .put("expected", text).put("actual", actual)
                            .put("resolved", resolved).toString()
                    }
                }
                if (wanted == "paste") {
                    val actual = readText(ref)
                    return JSONObject().put("ok", true).put("op", "perform_action")
                        .put("action", wanted).put("ref", ref).put("text_after", actual)
                        .put("resolved", resolved).toString()
                }
                return JSONObject().put("ok", true).put("op", "perform_action")
                    .put("action", wanted).put("ref", ref).put("resolved", resolved).toString()
            } catch (e: Throwable) {
                return errorJson("perform_action", "action_failed:$e")
            } finally {
                node.recycle()
            }
        }

        /**
         * 按需截图，仅显式调用时执行。windowId<0 截整屏；结果只返回本地 PNG 路径与尺寸。
         * Android 10 及以下没有 takeScreenshot，直接返回 sdk_unsupported。
         */
        @JvmStatic
        fun screenshot(windowId: Int): String {
            val service = instance ?: return errorJson("screenshot", "service_not_connected")
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
                return errorJson("screenshot", "sdk_unsupported:${Build.VERSION.SDK_INT}")
            }
            val latch = CountDownLatch(1)
            var bitmap: Bitmap? = null
            var failure = ""
            val callback = object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                    try {
                        val buffer = result.hardwareBuffer
                        try {
                            bitmap = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace)
                                ?.copy(Bitmap.Config.ARGB_8888, false)
                        } finally {
                            buffer.close()
                        }
                    } catch (e: Throwable) {
                        failure = "decode_failed:$e"
                    } finally {
                        latch.countDown()
                    }
                }

                override fun onFailure(errorCode: Int) {
                    failure = "screenshot_failed:$errorCode"
                    latch.countDown()
                }
            }
            try {
                if (windowId >= 0 && Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    service.takeScreenshotOfWindow(windowId, shotExecutor, callback)
                } else {
                    service.takeScreenshot(android.view.Display.DEFAULT_DISPLAY, shotExecutor, callback)
                }
            } catch (e: Throwable) {
                return errorJson("screenshot", "screenshot_exception:$e")
            }
            val waited = try {
                latch.await(8, TimeUnit.SECONDS)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                false
            }
            if (!waited) return errorJson("screenshot", "screenshot_timeout")
            val image = bitmap ?: return errorJson("screenshot", failure.ifEmpty { "screenshot_empty" })
            return try {
                val dir = File(service.filesDir, "shots").apply { mkdirs() }
                val name = "shot_${System.currentTimeMillis()}" + if (windowId >= 0) "_w$windowId.png" else ".png"
                val file = File(dir, name)
                FileOutputStream(file).use { out ->
                    if (!image.compress(Bitmap.CompressFormat.PNG, 100, out)) {
                        return errorJson("screenshot", "encode_failed")
                    }
                }
                JSONObject().put("ok", true).put("op", "screenshot")
                    .put("path", file.absolutePath)
                    .put("width", image.width).put("height", image.height)
                    .put("bytes", file.length()).put("windowId", windowId).toString()
            } catch (e: Throwable) {
                errorJson("screenshot", "save_failed:$e")
            } finally {
                image.recycle()
            }
        }

        private fun eventTypeName(type: Int): String = when (type) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> "window_state_changed"
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> "window_content_changed"
            AccessibilityEvent.TYPE_VIEW_FOCUSED -> "view_focused"
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> "view_scrolled"
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> "view_text_changed"
            else -> "unknown"
        }

        /** 按 ref 定位节点；中间节点用完立即 recycle，只把最终节点交给调用方。 */
        private fun resolveNode(ref: String): AccessibilityNodeInfo? {
            val match = nodeRefPattern.matchEntire(ref.trim()) ?: return null
            val index = match.groupValues[1].toIntOrNull() ?: return null
            val service = instance ?: return null
            val windows = try { service.windows } catch (e: Throwable) { null } ?: return null
            if (index < 0 || index >= windows.size) return null
            val root = windows[index].root ?: return null
            val segments = match.groupValues[2]
            if (segments.isEmpty()) return root
            val chain = ArrayList<AccessibilityNodeInfo>()
            chain.add(root)
            var node = root
            for (part in segments.split('.')) {
                val childIndex = part.toIntOrNull()
                if (childIndex == null) {
                    chain.forEach { it.recycle() }
                    return null
                }
                val child = try { node.getChild(childIndex) } catch (e: Throwable) { null }
                if (child == null) {
                    chain.forEach { it.recycle() }
                    return null
                }
                chain.add(child)
                node = child
            }
            chain.dropLast(1).forEach { it.recycle() }
            return node
        }

        /** 重新按 ref 定位并读取当前 text，供 PASTE 回读校验。 */
        private fun readText(ref: String): String {
            val node = resolveNode(ref) ?: return ""
            return try {
                node.text?.toString().orEmpty()
            } finally {
                node.recycle()
            }
        }

        private fun verifyText(ref: String, expected: String): String {
            repeat(3) { attempt ->
                val node = resolveNode(ref)
                if (node != null) {
                    try {
                        val current = node.text?.toString().orEmpty()
                        if (current == expected) return current
                    } finally {
                        node.recycle()
                    }
                }
                if (attempt < 2) Thread.sleep(80L)
            }
            return ""
        }

        private fun nodeInfo(node: AccessibilityNodeInfo): JSONObject {
            val rect = Rect()
            node.getBoundsInScreen(rect)
            return JSONObject()
                .put("text", node.text?.toString().orEmpty())
                .put("desc", node.contentDescription?.toString().orEmpty())
                .put("id", node.viewIdResourceName.orEmpty())
                .put("class", node.className?.toString().orEmpty())
                .put("bounds", rectJson(rect))
                .put("clickable", node.isClickable)
                .put("editable", node.isEditable)
                .put("scrollable", node.isScrollable)
        }

        private fun rectJson(rect: Rect): JSONArray =
            JSONArray().put(rect.left).put(rect.top).put(rect.right).put(rect.bottom)

        private fun errorJson(op: String, error: String, extra: JSONObject? = null): String {
            val payload = JSONObject().put("ok", false).put("op", op)
                .put("error", error).put("connected", instance != null)
            if (extra != null) payload.put("resolved", extra)
            return payload.toString()
        }

        private fun maxStrokeCount(): Int =
            try {
                GestureDescription.getMaxStrokeCount()
            } catch (e: Throwable) {
                0
            }

        private fun maxGestureDuration(): Long =
            try {
                GestureDescription.getMaxGestureDuration()
            } catch (e: Throwable) {
                0L
            }

        private fun pathOf(points: List<Pair<Float, Float>>): Path {
            val path = Path()
            if (points.isNotEmpty()) {
                path.moveTo(points[0].first, points[0].second)
                for (i in 1 until points.size) {
                    path.lineTo(points[i].first, points[i].second)
                }
            }
            return path
        }

        private fun strokeOf(
            points: List<Pair<Float, Float>>,
            startTimeMs: Long,
            durationMs: Long,
            willContinue: Boolean,
        ): GestureDescription.StrokeDescription {
            val path = pathOf(points)
            val duration = durationMs.coerceAtLeast(1L)
            val start = startTimeMs.coerceAtLeast(0L)
            return if (willContinue) {
                GestureDescription.StrokeDescription(path, start, duration, true)
            } else {
                GestureDescription.StrokeDescription(path, start, duration)
            }
        }

        private fun fail(reason: String): Boolean {
            lastError = reason
            lastAtMs = System.currentTimeMillis()
            return false
        }

        private fun succeed(): Boolean {
            lastError = ""
            lastAtMs = System.currentTimeMillis()
            return true
        }

        private fun perform(strokes: List<GestureDescription.StrokeDescription>): Boolean {
            val service = instance ?: return fail("service_not_connected")
            if (strokes.isEmpty()) return fail("empty_strokes")
            val maxStrokes = maxStrokeCount()
            if (maxStrokes > 0 && strokes.size > maxStrokes) return fail("too_many_strokes:${strokes.size}/$maxStrokes")

            val builder = GestureDescription.Builder()
            val endTimeMs = strokes.maxOf { it.startTime + it.duration }
            strokes.forEach { builder.addStroke(it) }
            val gesture = try {
                builder.build()
            } catch (e: Throwable) {
                return fail("build_failed:$e")
            }

            val latch = CountDownLatch(1)
            val completed = booleanArrayOf(false)
            val callback = object : AccessibilityService.GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) {
                    completed[0] = true
                    latch.countDown()
                }

                override fun onCancelled(gestureDescription: GestureDescription?) {
                    completed[0] = false
                    latch.countDown()
                }
            }

            val dispatch = Runnable { service.dispatchGesture(gesture, callback, null) }
            if (Looper.myLooper() == Looper.getMainLooper()) {
                dispatch.run()
            } else {
                mainHandler.post(dispatch)
            }

            return try {
                val finished = latch.await(endTimeMs + CALLBACK_EXTRA_MS, TimeUnit.MILLISECONDS)
                when {
                    !finished -> fail("gesture_timeout:${endTimeMs + CALLBACK_EXTRA_MS}ms")
                    completed[0] -> succeed()
                    else -> fail("gesture_cancelled")
                }
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                fail("interrupted")
            }
        }
    }
}
