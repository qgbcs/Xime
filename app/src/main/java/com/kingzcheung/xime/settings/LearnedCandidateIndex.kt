package com.kingzcheung.xime.settings

import java.util.Locale
import kotlin.math.exp

/**
 * 应用学习词条目，调频模型对齐 RIME userdb（commits + dee 衰减 + tick）：
 *   - [commits]：累计选中次数；
 *   - [dee]：衰减权重，再次选中时按 formula_d 累积；0 表示从未用过；
 *   - [lastTick]：最近一次更新所处的全局 tick；
 *   - [baseWeight]：未用过词的原始权重，降频态固定为 -1。
 *
 * 再次选中：dee = 1.0 + dee * exp((lastTick - tick) / 200)，commits + 1
 * 降频：baseWeight = -1、commits = 0、dee = 0、lastTick = 0
 */
internal data class LearnedCandidateEntry(
    val inputCode: String,
    val word: String,
    val baseWeight: Long = 0L,
    val commits: Long = 0L,
    val dee: Double = 0.0,
    val lastTick: Long = 0L,
) {
    /** 兼容只读视图：用过的词取 commits，未用过取 baseWeight。 */
    val score: Long get() = if (dee > 0.0) commits else baseWeight
}

internal object LearnedCandidateIndex {
    /** 磁盘格式列数：code / word / baseWeight / commits / dee / lastTick。 */
    private const val COLUMNS = 6

    /** 衰减半衰期尺度，与 RIME formula_d 的 /200 一致。 */
    private const val DECAY_PERIOD = 200.0

    private const val DECIMAL_FORMAT = "%.6f"

    /** 解析一行；兼容旧 3 列（code/word/score）与 2 列（code/word）。 */
    fun parse(line: String): LearnedCandidateEntry? {
        if (line.isBlank()) return null
        val parts = line.split('\t')
        if (parts.size >= COLUMNS) {
            val code = parts[0]
            val word = parts[1]
            if (code.isBlank() || word.isBlank()) return null
            return LearnedCandidateEntry(
                inputCode = code,
                word = word,
                baseWeight = parts[2].toLongOrNull() ?: 0L,
                commits = parts[3].toLongOrNull() ?: 0L,
                dee = parts[4].toDoubleOrNull() ?: 0.0,
                lastTick = parts[5].toLongOrNull() ?: 0L,
            )
        }
        if (parts.size == 3) {
            val score = parts[2].toLongOrNull() ?: return null
            if (parts[0].isBlank() || parts[1].isBlank()) return null
            return LearnedCandidateEntry(parts[0], parts[1], baseWeight = score)
        }
        if (parts.size == 2) {
            if (parts[0].isBlank() || parts[1].isBlank()) return null
            return LearnedCandidateEntry(parts[0], parts[1])
        }
        return LearnedCandidateEntry(line, line, 0L)
    }

    fun serialize(entry: LearnedCandidateEntry): String =
        "${entry.inputCode}\t${entry.word}\t${entry.baseWeight}\t${entry.commits}\t" +
            String.format(Locale.ROOT, DECIMAL_FORMAT, entry.dee) + "\t${entry.lastTick}"

    /** 用过的词优先 → dee 降序 → commits 降序 → baseWeight 降序 → 编码稳定排序。 */
    fun ordered(entries: List<LearnedCandidateEntry>): List<LearnedCandidateEntry> =
        entries.sortedWith(
            compareByDescending<LearnedCandidateEntry> { it.dee > 0.0 }
                .thenByDescending { it.dee }
                .thenByDescending { it.commits }
                .thenByDescending { it.baseWeight }
                .thenBy { it.inputCode.lowercase(Locale.ROOT) }
                .thenBy { it.word },
        )

    fun matching(
        entries: List<LearnedCandidateEntry>,
        input: String,
    ): List<LearnedCandidateEntry> {
        val key = input.trim().lowercase(Locale.ROOT)
        if (key.isEmpty()) return emptyList()
        return ordered(entries)
            .filter { it.inputCode.lowercase(Locale.ROOT).startsWith(key) }
            .distinctBy { it.word }
    }

    fun learn(
        entries: List<LearnedCandidateEntry>,
        inputCode: String,
        word: String,
        bumpExisting: Boolean,
    ): List<LearnedCandidateEntry> {
        val tick = (entries.maxOfOrNull { it.lastTick } ?: 0L) + 1L
        val existingIndex = entries.indexOfFirst {
            it.inputCode.equals(inputCode, ignoreCase = true) && it.word == word
        }
        if (existingIndex >= 0) {
            if (!bumpExisting) return entries
            return ordered(entries.toMutableList().also { current ->
                val entry = current[existingIndex]
                current[existingIndex] = entry.copy(
                    baseWeight = entry.baseWeight.coerceAtLeast(0L),
                    commits = entry.commits + 1L,
                    dee = 1.0 + entry.dee * exp((entry.lastTick - tick) / DECAY_PERIOD),
                    lastTick = tick,
                )
            })
        }

        return ordered(
            entries + LearnedCandidateEntry(
                inputCode = inputCode,
                word = word,
                commits = 1L,
                dee = 1.0,
                lastTick = tick,
            ),
        )
    }

    /** 降频：清空使用记录并置 baseWeight=-1，再次选中会自动恢复。 */
    fun demoteWord(
        entries: List<LearnedCandidateEntry>,
        word: String,
    ): List<LearnedCandidateEntry> {
        if (entries.none { it.word == word }) return entries
        return ordered(entries.map { entry ->
            if (entry.word == word) {
                entry.copy(baseWeight = -1L, commits = 0L, dee = 0.0, lastTick = 0L)
            } else {
                entry
            }
        })
    }
}
