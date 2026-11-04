package com.kingzcheung.xime.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

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

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

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
