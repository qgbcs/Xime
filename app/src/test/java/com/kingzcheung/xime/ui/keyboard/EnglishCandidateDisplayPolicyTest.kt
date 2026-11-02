package com.kingzcheung.xime.ui.keyboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EnglishCandidateDisplayPolicyTest {

    @Test
    fun `a single long english candidate shows its first ten code points and one ellipsis`() {
        val item = EnglishCandidateDisplayPolicy.prepare(listOf("internationalization")).single()

        assertEquals("internationalization", item.candidate)
        assertEquals("internatio…", item.displayText)
    }

    @Test
    fun `only two english candidates remain visible among first screen candidates`() {
        val items = EnglishCandidateDisplayPolicy.prepare(
            listOf("internationalization", "你好", "documentation", "characteristically"),
        )

        assertEquals(listOf("internationalization", "你好", "documentation"), items.map { it.candidate })
        assertEquals(2, items.count { EnglishCandidateDisplayPolicy.isEnglishWord(it.candidate) })
        assertEquals(0, items[0].index)
        assertEquals(1, items[1].index)
        assertEquals(2, items[2].index)
    }

    @Test
    fun `two english labels fit within ten code points by shortening the longer label first`() {
        val items = EnglishCandidateDisplayPolicy.prepare(listOf("abcdefgh", "uvwxyz"))
        val englishLabels = items.map { it.displayText }

        assertEquals(2, englishLabels.size)
        assertTrue(englishLabels.sumOf { it.codePointCount(0, it.length) } <= 10)
        assertTrue(englishLabels.first().endsWith("…"))
        assertTrue(englishLabels.last().endsWith("…"))
        assertEquals(listOf("abcdefgh", "uvwxyz"), items.map { it.candidate })
    }

    @Test
    fun `non-english candidates remain unchanged and mixed scripts are not classified as english`() {
        val mixed = "hello你好"
        assertFalse(EnglishCandidateDisplayPolicy.isEnglishWord(mixed))

        val items = EnglishCandidateDisplayPolicy.prepare(listOf("你好", "hello", mixed))
        assertEquals(listOf("你好", "hello", mixed), items.map { it.candidate })
        assertEquals("hello", items[1].displayText)
    }

    @Test
    fun `candidate matching permits common english word punctuation`() {
        assertTrue(EnglishCandidateDisplayPolicy.isEnglishWord("don't"))
        assertTrue(EnglishCandidateDisplayPolicy.isEnglishWord("well-known"))
        assertFalse(EnglishCandidateDisplayPolicy.isEnglishWord("中国"))
    }

    @Test
    fun `demoted candidates are hidden from first screen but retain their original selection index`() {
        val items = EnglishCandidateDisplayPolicy.prepare(
            listOf("你好", "世界", "deprioritized"),
            demotedCandidateCount = 1,
        )

        assertEquals(listOf("你好", "世界"), items.map { it.candidate })
        assertEquals(listOf(0, 1), items.map { it.index })
        assertEquals(
            listOf("你好", "世界", "deprioritized"),
            EnglishCandidateDisplayPolicy.prepare(listOf("你好", "世界", "deprioritized"))
                .map { it.candidate },
        )
    }
}
