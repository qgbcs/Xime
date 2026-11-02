package com.kingzcheung.xime.ui.keyboard

import java.lang.Character.UnicodeScript

internal data class CandidateDisplayItem(
    val index: Int,
    val candidate: String,
    val displayText: String,
)

internal object EnglishCandidateDisplayPolicy {
    const val MAX_ENGLISH_CANDIDATES = 2
    const val MAX_COMBINED_ENGLISH_CODE_POINTS = 10
    private const val MAX_SINGLE_ENGLISH_PREFIX_CODE_POINTS = 10
    private const val ELLIPSIS = "…"

    fun prepare(
        candidates: List<String>,
        demotedCandidateCount: Int = 0,
        maxCandidates: Int = 20,
    ): List<CandidateDisplayItem> {
        val primaryCount = candidates.size - demotedCandidateCount.coerceIn(0, candidates.size)
        val indexed = candidates.take(primaryCount).take(maxCandidates).mapIndexed { index, candidate ->
            CandidateDisplayItem(index, candidate, candidate)
        }
        val english = indexed.filter { isEnglishWord(it.candidate) }
        if (english.isEmpty()) return indexed

        val visibleEnglish = english.take(MAX_ENGLISH_CANDIDATES)
        val visibleIndices = visibleEnglish.mapTo(mutableSetOf()) { it.index }
        val labels = abbreviate(visibleEnglish.map { it.candidate })
        val labelByIndex = visibleEnglish.indices.associate { visibleEnglish[it].index to labels[it] }

        return indexed.asSequence()
            .filter { !isEnglishWord(it.candidate) || it.index in visibleIndices }
            .map { item -> item.copy(displayText = labelByIndex[item.index] ?: item.candidate) }
            .toList()
    }

    fun isEnglishWord(candidate: String): Boolean {
        if (candidate.isEmpty()) return false
        var hasLatinLetter = false
        var offset = 0
        while (offset < candidate.length) {
            val codePoint = candidate.codePointAt(offset)
            val type = Character.getType(codePoint)
            val isLatinLetter = Character.isLetter(codePoint) &&
                UnicodeScript.of(codePoint) == UnicodeScript.LATIN
            if (isLatinLetter) {
                hasLatinLetter = true
            } else if (!Character.isDigit(codePoint) && codePoint !in WORD_PUNCTUATION &&
                !Character.isWhitespace(codePoint) && type != Character.NON_SPACING_MARK.toInt()
            ) {
                return false
            }
            offset += Character.charCount(codePoint)
        }
        return hasLatinLetter
    }

    private fun abbreviate(words: List<String>): List<String> {
        val prefixes = words.map { word ->
            val length = word.codePointCount(0, word.length)
            PrefixLabel(
                word = word,
                prefixLength = minOf(length, MAX_SINGLE_ENGLISH_PREFIX_CODE_POINTS),
                ellipsized = length > MAX_SINGLE_ENGLISH_PREFIX_CODE_POINTS,
            )
        }.toMutableList()

        if (prefixes.size == 2) {
            while (prefixes.sumOf { it.displayLength } > MAX_COMBINED_ENGLISH_CODE_POINTS) {
                val next = prefixes.indices
                    .filter { prefixes[it].prefixLength > 1 }
                    .maxWithOrNull(
                        compareBy<Int> { prefixes[it].displayLength }
                            .thenBy { -it }
                    ) ?: break
                prefixes[next] = prefixes[next].copy(
                    prefixLength = prefixes[next].prefixLength - 1,
                    ellipsized = true,
                )
            }
        }
        return prefixes.map { it.render() }
    }

    private data class PrefixLabel(
        val word: String,
        val prefixLength: Int,
        val ellipsized: Boolean,
    ) {
        val displayLength: Int
            get() = prefixLength + if (ellipsized) 1 else 0

        fun render(): String {
            val end = word.offsetByCodePoints(0, prefixLength)
            return word.substring(0, end) + if (ellipsized) ELLIPSIS else ""
        }
    }

    private val WORD_PUNCTUATION = setOf(
        '\''.code,
        '’'.code,
        '-'.code,
        '_'.code,
        '.'.code,
    )
}
