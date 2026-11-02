package com.kingzcheung.xime.ui.keyboard

/**
 * 键盘调节的边界纯逻辑（无 Compose 依赖，可单测）。
 *
 * 两个可调维度：
 *  - 高度：键盘内容高度（候选栏 + 按键区），竖屏 25%~60%、横屏 30%~70% 屏高；
 *  - 左右边距：键盘两侧各自收窄的距离（宽度调节，左右独立、可整体偏移），
 *    每侧上限 = (屏宽 - 最小键盘宽)/2，即键盘可以收窄到 [MIN_KEYBOARD_WIDTH_DP]；
 *    不再额外限制屏宽百分比，否则宽屏上会被 25% 卡住（最窄也只能到半屏宽）。
 *
 * 底部手柄 = 底边跟手（顶边锚定）：高度与底部留白此消彼长、总和恒定，
 * 见 [bottomEdgeDragDelta]。
 */
object KeyboardResizeBounds {

    /**
     * 键盘最窄总宽度（dp）：低于此值按键过小不可用。
     * 曾为 240dp，正常手机上（≈360~420dp 屏宽）等于只能收到屏宽 57%~67%，
     * 用户反馈「最窄仍偏宽」，故放宽到 180dp，把收缩幅度交给用户自行取舍；
     * 180dp 同时也是调节层三个 56dp 操作按钮（重置/取消/确定）能排下的下限。
     */
    const val MIN_KEYBOARD_WIDTH_DP = 180

    /** 底部留白上限（dp）：底边最多抬离屏幕底部这么远。 */
    const val MAX_BOTTOM_PADDING_DP = 120

    /** 高度边界 [min, max]（dp）。 */
    fun heightBoundsDp(screenHeightDp: Int, isLandscape: Boolean): IntRange {
        val (minPercent, maxPercent) = if (isLandscape) (30 to 70) else (25 to 60)
        val min = (screenHeightDp * minPercent) / 100
        val max = (screenHeightDp * maxPercent) / 100
        return min..max.coerceAtLeast(min)
    }

    /** 左右边距上限（dp，每侧）：只受最小键盘宽约束，宽屏也能继续收窄。 */
    fun maxHorizontalMarginDp(screenWidthDp: Int): Int {
        return ((screenWidthDp - MIN_KEYBOARD_WIDTH_DP) / 2).coerceAtLeast(0)
    }

    /**
     * 底部手柄拖动的耦合增量（dp，向下为正）：高度 += delta、底部留白 -= delta。
     * 两者总和恒定 ⇒ 顶边严格不动，底边精确跟手。
     * 边界：留白收敛到 [0, MAX_BOTTOM_PADDING_DP]，高度收敛到 [heightBounds]；
     * 任一边到界后增量归零（如留白为 0 时往下拉 = 底边已贴屏幕底部，不再动作）。
     */
    fun bottomEdgeDragDelta(
        dyDp: Float,
        heightDp: Int,
        heightBounds: IntRange,
        bottomPaddingDp: Int,
    ): Float =
        dyDp.coerceIn(
            maxOf(bottomPaddingDp - MAX_BOTTOM_PADDING_DP, heightBounds.first - heightDp).toFloat(),
            minOf(bottomPaddingDp, heightBounds.last - heightDp).toFloat(),
        )
}
