package com.kingzcheung.xime.rime

import org.junit.Assert.assertEquals
import org.junit.Test

class CandidateIndexResolverTest {

    @Test
    fun `candidate reorder resolves by text`() {
        assertEquals(
            1,
            resolveRimeCandidateIndex(0, "target", listOf("other", "target"))
        )
    }

    @Test
    fun `missing candidate does not fall back to another page position`() {
        assertEquals(
            -1,
            resolveRimeCandidateIndex(2, "previous-page-word", listOf("current-page-word"))
        )
    }

    @Test
    fun `accepted candidate without a commit remains in Rime composition`() {
        assertEquals(true, candidateSelectionLeavesComposition(true, ""))
        assertEquals(false, candidateSelectionLeavesComposition(false, ""))
        assertEquals(false, candidateSelectionLeavesComposition(true, "猪"))
    }

    @Test
    fun `missing preview disables stateful dangling tail probing`() {
        assertEquals(false, canProbeDanglingTail(null))
        assertEquals(
            true,
            canProbeDanglingTail(RimeCandidatePreview("", "猪", "bozi"))
        )
    }

    @Test
    fun `remaining input and candidates identify partial candidate selection`() {
        assertEquals(true, hasRemainingCandidateComposition("zhubozi", 20))
        assertEquals(false, hasRemainingCandidateComposition("", 0))
        assertEquals(false, hasRemainingCandidateComposition("zhubozi", 0))
        assertEquals(false, hasRemainingCandidateComposition("", 20))
    }

    @Test
    fun `missing identity preserves index based selection`() {
        assertEquals(2, resolveRimeCandidateIndex(2, null, listOf("candidate")))
    }

    @Test
    fun `partial candidate preview preserves remaining pinyin`() {
        assertEquals(
            CandidateInputPrefix("ksm", "kan", "看什么"),
            resolveCandidateInputPrefix(
                "ksmkan",
                RimeCandidatePreview("", "看什么", "kan"),
                "看什么"
            )
        )
        assertEquals(
            CandidateInputPrefix("yimu", "di", "一亩"),
            resolveCandidateInputPrefix(
                "yimudi",
                RimeCandidatePreview("", "一亩", "di"),
                "一亩"
            )
        )
    }

    @Test
    fun `full candidate preview is not treated as a partial selection`() {
        assertEquals(
            null,
            resolveCandidateInputPrefix(
                "kan",
                RimeCandidatePreview("", "看", ""),
                "看"
            )
        )
    }
}
