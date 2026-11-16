package com.kingzcheung.xime.viewmodel

import org.junit.Assert.assertEquals
import org.junit.Test

/** 「词库管理 → 学习词」列表的合并与搜索规则（3f9af3e 新增页签的回归测试）。 */
class LearnedWordEntryTest {

    private fun entry(word: String, code: String, score: Long, source: LearnedWordSource) =
        LearnedWordEntry(word, code, score, source)

    @Test
    fun `userdict entries come first and each source sorts by score`() {
        val userDict = listOf(
            entry("你好", "ni hao", 5, LearnedWordSource.UserDict),
            entry("伟光正", "wei guang zheng", 1, LearnedWordSource.UserDict),
        )
        val auto = listOf(
            entry("之后", "zh", 3, LearnedWordSource.AutoPhrase),
            entry("安装", "az", 9, LearnedWordSource.AutoPhrase),
        )

        assertEquals(
            listOf("你好", "伟光正", "安装", "之后"),
            mergeLearnedWordEntries(userDict, auto).map { it.word },
        )
    }

    @Test
    fun `search matches word or code ignoring case`() {
        val entries = listOf(
            entry("伟光正", "wei guang zheng", 1, LearnedWordSource.UserDict),
            entry("安装", "az", 9, LearnedWordSource.AutoPhrase),
        )

        assertEquals(listOf("伟光正"), filterLearnedWordEntries(entries, "光").map { it.word })
        assertEquals(listOf("安装"), filterLearnedWordEntries(entries, "AZ").map { it.word })
        assertEquals(entries, filterLearnedWordEntries(entries, ""))
    }
}
