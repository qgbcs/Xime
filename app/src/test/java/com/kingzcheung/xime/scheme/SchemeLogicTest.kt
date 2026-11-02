package com.kingzcheung.xime.scheme

import org.junit.Assert.assertEquals
import org.junit.Test

/** 状态修复规则：迁移推断、启用列表过滤、当前方案回落。 */
class SchemeLogicTest {

    private val builtinId = "builtin"

    private val builtinInstalled = listOf("pinyin_simp", "t9_pinyin", "wubi86", "wubi86_pinyin")

    @Test
    fun `磁盘上是内置内容时活动包为内置`() {
        val declared = listOf(
            builtinId to setOf("wubi86", "wubi86_pinyin", "pinyin_simp", "t9_pinyin"),
            "rime-frost" to setOf("rime_frost", "rime_frost_t9"),
        )
        assertEquals(
            builtinId,
            SchemeLogic.inferActivePackage(builtinInstalled, declared, builtinId),
        )
    }

    @Test
    fun `磁盘上只有第三方包内容时选中该包`() {
        val installed = listOf("rime_frost", "rime_frost_t9", "stroke")
        val declared = listOf(
            builtinId to setOf("wubi86", "wubi86_pinyin", "pinyin_simp", "t9_pinyin"),
            "rime-frost" to setOf("rime_frost", "rime_frost_t9"),
            "other" to setOf("x1", "x2"),
        )
        assertEquals(
            "rime-frost",
            SchemeLogic.inferActivePackage(installed, declared, builtinId),
        )
    }

    @Test
    fun `多个包都匹配时取重合最多者`() {
        val installed = listOf("rime_frost", "rime_frost_t9", "a1")
        val declared = listOf(
            builtinId to emptySet(),
            "pkg-a" to setOf("a1"),
            "pkg-b" to setOf("rime_frost", "rime_frost_t9"),
        )
        assertEquals(
            "pkg-b",
            SchemeLogic.inferActivePackage(installed, declared, builtinId),
        )
    }

    @Test
    fun `没有任何重合时兜底内置`() {
        val declared = listOf(
            builtinId to setOf("wubi86"),
            "gone" to setOf("x1"),
        )
        assertEquals(
            builtinId,
            SchemeLogic.inferActivePackage(listOf("ghost"), declared, builtinId),
        )
    }

    @Test
    fun `磁盘上没有方案时兜底内置`() {
        assertEquals(
            builtinId,
            SchemeLogic.inferActivePackage(emptyList(), emptyList(), builtinId),
        )
    }

    @Test
    fun `启用列表保留用户顺序并剔除已不存在的方案`() {
        val stored = listOf("t9_pinyin", "wubi86", "gone", "pinyin_simp")
        assertEquals(
            listOf("t9_pinyin", "wubi86", "pinyin_simp"),
            SchemeLogic.filterEnabled(stored, builtinInstalled),
        )
    }

    @Test
    fun `从未写过启用列表时为空`() {
        assertEquals(emptyList<String>(), SchemeLogic.filterEnabled(null, builtinInstalled))
    }

    @Test
    fun `启用列表为空时用包声明的开箱集合`() {
        assertEquals(
            listOf("wubi86", "t9_pinyin"),
            SchemeLogic.initialEnabled(listOf("wubi86", "wubi86", "t9_pinyin"), builtinInstalled),
        )
    }

    @Test
    fun `包没有声明时一个都不启用，等用户手动选择`() {
        assertEquals(
            emptyList<String>(),
            SchemeLogic.initialEnabled(emptyList(), builtinInstalled),
        )
    }

    @Test
    fun `当前方案不在启用列表时回落到第一个`() {
        assertEquals("b", SchemeLogic.pickCurrent("z", listOf("b", "c")))
        assertEquals("c", SchemeLogic.pickCurrent("c", listOf("b", "c")))
        assertEquals("", SchemeLogic.pickCurrent("x", emptyList()))
    }

    @Test
    fun `包声明里混入不存在的方案时只取真实存在的`() {
        assertEquals(
            listOf("wubi86"),
            SchemeLogic.initialEnabled(listOf("wubi86", "not_installed"), builtinInstalled),
        )
    }
}
