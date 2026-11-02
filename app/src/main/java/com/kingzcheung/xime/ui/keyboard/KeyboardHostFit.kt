package com.kingzcheung.xime.ui.keyboard

/**
 * 键盘内容与「宿主承载区」的尺寸协商（承载区 = IME 窗口 `android.R.id.inputArea` 实测高度）。
 *
 * 手机、平板上 IME 窗口是整屏：按屏高百分比算出的键盘期望高度（含底部留白、撑高面板）
 * 恒小于承载区，[fitContentHeightDp] 恒返回期望值本身 → 这些设备行为完全不变。
 *
 * 独立 IME 面板（如 Horizon OS 给输入法的悬浮面板）只给键盘一段区域，
 * 而 `configuration` 仍上报整块虚拟屏（Quest 3 实测 3302×1766dp、density 1.25，
 * 承载区却只有 1250×625px ≈ 1000×500dp）。按屏高百分比算出的 825dp 超出承载区后，
 * 容器贴底对齐会把顶边推到 y=-428px，只剩最后两行可见。
 *
 * 约束只有一条：**同一承载区内的所有内容（键盘本体 + 底部留白 + 撑高面板）共同服从
 * 承载区高度**。键盘本体装得下不能证明加上工具面板也装得下，所以不做分段豁免。
 */
object KeyboardHostFit {

    /**
     * 键盘本体最小可用高度（dp）。
     *
     * 空间实在不够时（例如小屏横屏同时开快捷发送 + 工具面板），宁可让撑高面板溢出，
     * 也不把按键压到没法点击 —— 物理限制只能降级，不能把键盘压没。
     */
    const val MIN_CONTENT_HEIGHT_DP = 120

    /**
     * 把键盘内容期望高度夹取到宿主可用预算内。
     *
     * @param requestedDp 键盘本体期望高度（不含底部留白与撑高面板）
     * @param hostHeightDp 承载区高度（dp）；`<= 0` 表示尚未测得，不做约束
     * @param reservedDp 键盘本体之外固定占用的高度：底部留白 + 系统 inset 留白（+ 浮动拖动条）
     * @param extraPanelDp 撑高面板高度（快捷发送 / 工具面板）
     */
    fun fitContentHeightDp(
        requestedDp: Int,
        hostHeightDp: Int,
        reservedDp: Int,
        extraPanelDp: Int = 0,
    ): Int {
        if (hostHeightDp <= 0) return requestedDp
        val budget = (hostHeightDp - reservedDp - extraPanelDp).coerceAtLeast(0)
        val floor = minOf(requestedDp, MIN_CONTENT_HEIGHT_DP)
        return requestedDp.coerceAtMost(budget).coerceAtLeast(floor)
    }

    /**
     * 承载区是否比键盘期望高度还小 —— 说明 `configuration` 上报的屏高不代表实际可用空间
     * （独立 IME 面板、极小窗口）。此时依赖屏幕尺寸的「浮动键盘」「键盘调节」都不可用。
     */
    fun isHostConstrained(requestedDp: Int, hostHeightDp: Int): Boolean =
        hostHeightDp > 0 && requestedDp > hostHeightDp
}
