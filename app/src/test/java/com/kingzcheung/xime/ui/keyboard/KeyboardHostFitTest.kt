package com.kingzcheung.xime.ui.keyboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 键盘高度与宿主承载区的协商（[KeyboardHostFit]）。
 *
 * 回归背景（issue #1152）：Horizon OS 给输入法的独立面板只有 1250×625px（density 1.25
 * ⇒ 1000×500dp），而 `configuration` 上报 3302×1766dp。按屏高百分比算出的横屏键盘
 * 高度 825dp 塞进 500dp 面板，容器贴底后顶边落到 y=-428px，只剩最后两行可见。
 */
class KeyboardHostFitTest {

    @Test
    fun `normal phone keyboard keeps its preferred height`() {
        // 竖屏 800dp 屏、键盘期望 280dp、底部留白 18dp：承载区是整屏，不夹取
        assertEquals(
            280,
            KeyboardHostFit.fitContentHeightDp(requestedDp = 280, hostHeightDp = 800, reservedDp = 18),
        )
    }

    @Test
    fun `landscape phone with tool panel keeps whole content inside the window`() {
        // 横屏 411dp 屏、键盘 228dp、留白 18dp、工具面板 180dp
        // ⇒ 228 + 18 + 180 = 426dp 本来就越界（改动前工具面板顶部被裁掉 15dp）。
        // 所有内容共同服从预算：留白与面板不变，键盘本体压到 411 - 18 - 180 = 213dp。
        assertEquals(
            213,
            KeyboardHostFit.fitContentHeightDp(
                requestedDp = 228,
                hostHeightDp = 411,
                reservedDp = 18,
                extraPanelDp = 180,
            ),
        )
    }

    @Test
    fun `tool panel is counted even when keyboard body alone fits`() {
        // 用户指出的漏洞：键盘本体 400 <= 500 - 18，但加上 180 面板后 598 > 500
        val fitted = KeyboardHostFit.fitContentHeightDp(
            requestedDp = 400,
            hostHeightDp = 500,
            reservedDp = 18,
            extraPanelDp = 180,
        )
        assertEquals(302, fitted)
        assertEquals(500, fitted + 18 + 180)
    }

    @Test
    fun `quest panel shrinks keyboard to exactly fit the host area`() {
        // 625px / 1.25 = 500dp 面板；期望 825dp；预留 18dp 底部留白
        val fitted = KeyboardHostFit.fitContentHeightDp(
            requestedDp = 825,
            hostHeightDp = 500,
            reservedDp = 18,
        )
        assertEquals(482, fitted)
        // 键盘内容 + 预留 = 承载区，容器贴底后顶边正好落回 y=0，不再被裁
        assertEquals(500, fitted + 18)
    }

    @Test
    fun `unknown host height does not constrain`() {
        // 首帧前 inputArea 尚未测量（0）：保持原期望值，不能把键盘压成 0
        assertEquals(
            825,
            KeyboardHostFit.fitContentHeightDp(requestedDp = 825, hostHeightDp = 0, reservedDp = 18),
        )
    }

    @Test
    fun `impossible budget keeps a usable keyboard instead of squashing it to nothing`() {
        // 留白+面板就把承载区吃光了：键盘不低于 120dp（宁可面板溢出，也不让按键消失）
        assertEquals(
            KeyboardHostFit.MIN_CONTENT_HEIGHT_DP,
            KeyboardHostFit.fitContentHeightDp(requestedDp = 200, hostHeightDp = 500, reservedDp = 700),
        )
        // 期望值本来就比下限还小时，保期望值，不反向放大
        assertEquals(
            80,
            KeyboardHostFit.fitContentHeightDp(requestedDp = 80, hostHeightDp = 500, reservedDp = 700),
        )
    }

    @Test
    fun `host smaller than the keyboard means the reported screen is not the real space`() {
        // Quest：屏高 1766dp 算出 825dp 键盘，承载区只有 500dp → 配置不可信
        assertTrue(KeyboardHostFit.isHostConstrained(requestedDp = 825, hostHeightDp = 500))
        // 横屏手机：228dp 键盘装进 411dp 承载区 → 正常设备
        assertFalse(KeyboardHostFit.isHostConstrained(requestedDp = 228, hostHeightDp = 411))
        // 尚未测得承载区 → 不做判断
        assertFalse(KeyboardHostFit.isHostConstrained(requestedDp = 825, hostHeightDp = 0))
    }
}
