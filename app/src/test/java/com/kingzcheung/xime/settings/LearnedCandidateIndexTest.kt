package com.kingzcheung.xime.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LearnedCandidateIndexTest {

    @Test
    fun `learned front candidates are only injected on the first Rime page`() {
        val learnedFront = listOf("cad", "curev")

        assertEquals(
            learnedFront,
            LearnedCandidateIndex.frontCandidatesForRimePage(learnedFront, 0),
        )
        assertTrue(LearnedCandidateIndex.frontCandidatesForRimePage(learnedFront, 1).isEmpty())
        assertTrue(LearnedCandidateIndex.frontCandidatesForRimePage(learnedFront, 3).isEmpty())
    }

    @Test
    fun `demoted learned candidates move to the second Rime page`() {
        val learnedTail = listOf("cmd")

        assertTrue(LearnedCandidateIndex.tailCandidatesForRimePage(learnedTail, 0, true).isEmpty())
        assertEquals(learnedTail, LearnedCandidateIndex.tailCandidatesForRimePage(learnedTail, 1, true))
        assertTrue(LearnedCandidateIndex.tailCandidatesForRimePage(learnedTail, 2, false).isEmpty())
    }

    @Test
    fun `demoted learned candidates go to the end when Rime has only one page`() {
        val learnedTail = listOf("cmd")

        assertEquals(learnedTail, LearnedCandidateIndex.tailCandidatesForRimePage(learnedTail, 0, false))
    }

    @Test
    fun `legacy english records migrate with their word as input code`() {
        assertEquals(
            LearnedCandidateEntry("hello world", "hello world", 7L),
            LearnedCandidateIndex.parse("hello world\t7"),
        )
        assertEquals(
            LearnedCandidateEntry("hello", "hello", 0L),
            LearnedCandidateIndex.parse("hello"),
        )
    }

    @Test
    fun `english and chinese learned candidates share one frequency scale`() {
        val chinese = LearnedCandidateIndex.learn(emptyList(), "zhongguo", "中国", false)
        val english = LearnedCandidateIndex.learn(chinese, "international", "international", false)

        assertEquals(2L, english.first { it.word == "international" }.score)
        assertEquals(1L, english.first { it.word == "中国" }.score)
        assertEquals(listOf("中国"), LearnedCandidateIndex.matching(english, "zh").map { it.word })
        assertEquals(
            listOf("international"),
            LearnedCandidateIndex.matching(english, "INTER").map { it.word },
        )
    }

    @Test
    fun `reselecting a learned candidate increases its shared score`() {
        val learned = LearnedCandidateIndex.learn(emptyList(), "nihao", "你好", false)
        val selectedAgain = LearnedCandidateIndex.learn(learned, "nihao", "你好", true)

        assertEquals(2L, selectedAgain.single().score)
        assertEquals(
            learned,
            LearnedCandidateIndex.learn(learned, "nihao", "你好", false),
        )
    }

    @Test
    fun `prefix matching uses input code and deduplicates words`() {
        val entries = listOf(
            LearnedCandidateEntry("zhongguo", "中国", 4L),
            LearnedCandidateEntry("zhong", "中国", 2L),
            LearnedCandidateEntry("zhu", "猪", 3L),
        )

        assertEquals(
            listOf("中国", "猪"),
            LearnedCandidateIndex.matching(entries, "zh").map { it.word },
        )
        assertTrue(LearnedCandidateIndex.matching(entries, "sh").isEmpty())
    }

    @Test
    fun `serialized records preserve input code word and score`() {
        val entry = LearnedCandidateEntry("nihao", "你好", 9L)

        assertEquals(entry, LearnedCandidateIndex.parse(LearnedCandidateIndex.serialize(entry)))
    }

    @Test
    fun `demoting a learned word uses the minimum negative score to move behind normal matches`() {
        val entries = listOf(
            LearnedCandidateEntry("nihao", "你好", 12L),
            LearnedCandidateEntry("nihao", "拟好", 5L),
            LearnedCandidateEntry("nihao", "泥豪", 2L),
        )

        val demoted = LearnedCandidateIndex.demoteWord(entries, "你好", -1L)

        assertEquals(-1L, demoted.first { it.word == "你好" }.score)
        assertEquals(
            listOf("拟好", "泥豪", "你好"),
            LearnedCandidateIndex.matching(demoted, "nihao").map { it.word },
        )
    }
}
