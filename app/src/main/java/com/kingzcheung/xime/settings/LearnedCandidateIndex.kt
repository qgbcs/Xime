package com.kingzcheung.xime.settings

internal data class LearnedCandidateEntry(
    val inputCode: String,
    val word: String,
    val score: Long,
)

internal object LearnedCandidateIndex {
    fun frontCandidatesForRimePage(front: List<String>, pageNo: Int): List<String> =
        if (pageNo == 0) front else emptyList()

    fun tailCandidatesForRimePage(
        tail: List<String>,
        pageNo: Int,
        hasNextPage: Boolean,
    ): List<String> =
        if (pageNo == 1 || (pageNo == 0 && !hasNextPage)) tail else emptyList()

    fun parse(line: String): LearnedCandidateEntry? {
        val lastTab = line.lastIndexOf('\t')
        if (lastTab > 0) {
            val score = line.substring(lastTab + 1).toLongOrNull()
            if (score != null) {
                val previousTab = line.lastIndexOf('\t', lastTab - 1)
                if (previousTab > 0) {
                    val code = line.substring(0, previousTab)
                    val word = line.substring(previousTab + 1, lastTab)
                    if (code.isNotBlank() && word.isNotBlank()) {
                        return LearnedCandidateEntry(code, word, score)
                    }
                }
                val word = line.substring(0, lastTab)
                if (word.isNotBlank()) return LearnedCandidateEntry(word, word, score)
            }
        }
        return line.takeIf { it.isNotBlank() }?.let {
            LearnedCandidateEntry(it, it, 0L)
        }
    }

    fun serialize(entry: LearnedCandidateEntry): String =
        "${entry.inputCode}\t${entry.word}\t${entry.score}"

    fun ordered(entries: List<LearnedCandidateEntry>): List<LearnedCandidateEntry> =
        entries.sortedByDescending { it.score }

    fun matching(
        entries: List<LearnedCandidateEntry>,
        input: String,
    ): List<LearnedCandidateEntry> {
        val key = input.trim().lowercase()
        if (key.isEmpty()) return emptyList()
        return ordered(entries)
            .filter { it.inputCode.lowercase().startsWith(key) }
            .distinctBy { it.word }
    }

    fun learn(
        entries: List<LearnedCandidateEntry>,
        inputCode: String,
        word: String,
        bumpExisting: Boolean,
    ): List<LearnedCandidateEntry> {
        val existingIndex = entries.indexOfFirst {
            it.inputCode.equals(inputCode, ignoreCase = true) && it.word == word
        }
        if (existingIndex >= 0) {
            if (!bumpExisting) return entries
            return ordered(entries.toMutableList().also { current ->
                val entry = current[existingIndex]
                current[existingIndex] = entry.copy(score = entry.score + 1L)
            })
        }

        val nextScore = (entries.maxOfOrNull { it.score } ?: 0L).coerceAtLeast(0L) + 1L
        return ordered(entries + LearnedCandidateEntry(inputCode, word, nextScore))
    }

    fun bumpWord(
        entries: List<LearnedCandidateEntry>,
        input: String,
        word: String,
        increment: Long = 1L,
    ): List<LearnedCandidateEntry> {
        val inputKey = input.trim().lowercase()
        val index = entries.indexOfFirst {
            it.word == word && (inputKey.isEmpty() || it.inputCode.lowercase().startsWith(inputKey))
        }.takeIf { it >= 0 } ?: entries.indexOfFirst { it.word == word }
        if (index < 0) return entries
        return ordered(entries.toMutableList().also { current ->
            val entry = current[index]
            current[index] = entry.copy(score = entry.score + increment)
        })
    }

    fun demoteWord(
        entries: List<LearnedCandidateEntry>,
        word: String,
        demotedScore: Long,
    ): List<LearnedCandidateEntry> {
        if (entries.none { it.word == word }) return entries
        return ordered(entries.map { entry ->
            if (entry.word == word) entry.copy(score = demotedScore) else entry
        })
    }
}
