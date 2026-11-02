package com.kingzcheung.xime.ui.keyboard

import com.kingzcheung.xime.settings.KeyboardRow
import com.kingzcheung.xime.settings.RowAlign
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 键盘行规范化测试：rows 行数动态化（4 行标准 / 5 行含数字行）、
 * 横屏纯字母布局的 shift/delete 注入，以及行宽/对齐判定（layout.columns / row_align）。
 */
class KeyboardRowLayoutTest {

    private val controlRow = listOf("mode_change", "comma", "space", "earth", "enter")
    private val digitRow = listOf("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")
    private val letterRow = listOf("a", "s", "d", "f", "g", "h", "j", "k", "l")

    private fun row(vararg keys: String) = KeyboardRow(keys.toList())

    @Test
    fun `四行配置原样保留`() {
        val rows = normalizeQwertyRows(
            listOf(
                row("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
                row("a", "s", "d", "f", "g", "h", "j", "k", "l"),
                row("shift", "z", "x", "c", "v", "b", "n", "m", "delete"),
                KeyboardRow(controlRow),
            )
        )
        assertEquals(4, rows.size)
        assertEquals(controlRow, rows[3].keys)
    }

    @Test
    fun `五行配置（含数字行）原样保留`() {
        val rows = normalizeQwertyRows(
            listOf(
                KeyboardRow(digitRow),
                row("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
                row("a", "s", "d", "f", "g", "h", "j", "k", "l"),
                row("shift", "z", "x", "c", "v", "b", "n", "m", "delete"),
                KeyboardRow(controlRow),
            )
        )
        assertEquals(5, rows.size)
        assertEquals(digitRow, rows[0].keys)
        assertEquals(controlRow, rows[4].keys)
    }

    @Test
    fun `不足四行时补出内置默认控制行`() {
        val rows = normalizeQwertyRows(
            listOf(
                row("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
                row("a", "s", "d", "f", "g", "h", "j", "k", "l"),
                row("z", "x", "c", "v", "b", "n", "m"),
            )
        )
        assertEquals(4, rows.size)
        assertEquals(controlRow, rows[3].keys)
    }

    @Test
    fun `超过最大行数的行被忽略`() {
        val six = (1..6).map { KeyboardRow(listOf("k$it")) }
        assertEquals(MAX_QWERTY_ROWS, normalizeQwertyRows(six).size)
    }

    @Test
    fun `空配置回退内置默认行`() {
        val rows = normalizeQwertyRows(emptyList())
        assertEquals(4, rows.size)
    }

    @Test
    fun `横屏纯字母布局在最后一个字母行补 shift 与 delete`() {
        val rows = landscapeQwertyRows(
            listOf(
                row("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
                row("a", "s", "d", "f", "g", "h", "j", "k", "l"),
                row("z", "x", "c", "v", "b", "n", "m"),
            )
        )
        assertEquals(4, rows.size)
        assertEquals("shift", rows[2].keys.first())
        assertEquals("delete", rows[2].keys.last())
    }

    @Test
    fun `横屏已含功能键的布局不注入`() {
        val source = listOf(
            KeyboardRow(digitRow),
            row("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
            row("a", "s", "d", "f", "g", "h", "j", "k", "l"),
            row("shift", "z", "x", "c", "v", "b", "n", "m", "delete"),
            KeyboardRow(controlRow),
        )
        val rows = landscapeQwertyRows(source)
        assertEquals(source, rows)
        assertTrue(rows[0].keys.first() == "1")
    }

    // ── 行宽判定（layout.columns / row_align / 行级 align|width）──

    @Test
    fun `uniform 下纯字母行按列数等比居中`() {
        assertEquals(0.9f, rowWidthFraction(KeyboardRow(letterRow), 10, RowAlign.UNIFORM), 0.0001f)
        assertEquals(0.7f, rowWidthFraction(row("z", "x", "c", "v", "b", "n", "m"), 10, RowAlign.UNIFORM), 0.0001f)
    }

    @Test
    fun `stretch 下纯字母行满宽`() {
        assertEquals(1f, rowWidthFraction(KeyboardRow(letterRow), 10, RowAlign.STRETCH), 0.0001f)
    }

    @Test
    fun `行级 align 覆盖键盘级策略`() {
        val stretchRow = KeyboardRow(letterRow, align = RowAlign.STRETCH)
        assertEquals(1f, rowWidthFraction(stretchRow, 10, RowAlign.UNIFORM), 0.0001f)
        val uniformRow = KeyboardRow(letterRow, align = RowAlign.UNIFORM)
        assertEquals(0.9f, rowWidthFraction(uniformRow, 10, RowAlign.STRETCH), 0.0001f)
    }

    @Test
    fun `含功能键的行始终满宽`() {
        val withFunction = row("shift", "z", "x", "c", "v", "b", "n", "m", "delete")
        assertEquals(1f, rowWidthFraction(withFunction, 10, RowAlign.UNIFORM), 0.0001f)
    }

    @Test
    fun `行级 width 优先并夹取到 0 到 1`() {
        val explicit = KeyboardRow(letterRow, width = 0.7f)
        assertEquals(0.7f, rowWidthFraction(explicit, 10, RowAlign.STRETCH), 0.0001f)
        assertEquals(1f, rowWidthFraction(KeyboardRow(letterRow, width = 1.5f), 10, RowAlign.UNIFORM), 0.0001f)
        assertEquals(0f, rowWidthFraction(KeyboardRow(letterRow, width = -1f), 10, RowAlign.UNIFORM), 0.0001f)
    }

    // ── 横向滑动接管（左右滑 vs 父层光标手势）──

    @Test
    fun `未配置左右滑的键不接管横向滑动`() {
        assertFalse(
            shouldSuppressCursorMove(
                hasHorizontalSwipe = false,
                dragOffsetX = 100f, dragOffsetY = 2f, horizontalThreshold = 30f,
            )
        )
    }

    @Test
    fun `配置左右滑且横向主导越过阈值时接管`() {
        assertTrue(
            shouldSuppressCursorMove(
                hasHorizontalSwipe = true,
                dragOffsetX = -40f, dragOffsetY = 5f, horizontalThreshold = 30f,
            )
        )
    }

    @Test
    fun `配置左右滑但纵向主导时不接管`() {
        assertFalse(
            shouldSuppressCursorMove(
                hasHorizontalSwipe = true,
                dragOffsetX = 40f, dragOffsetY = -80f, horizontalThreshold = 30f,
            )
        )
    }

    @Test
    fun `配置左右滑但未越过接管阈值时不接管`() {
        assertFalse(
            shouldSuppressCursorMove(
                hasHorizontalSwipe = true,
                dragOffsetX = 20f, dragOffsetY = 1f, horizontalThreshold = 30f,
            )
        )
    }
}
