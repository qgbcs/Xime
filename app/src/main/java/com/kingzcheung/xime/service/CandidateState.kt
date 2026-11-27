package com.kingzcheung.xime.service

/** 候选来源：app 自动学习词 / 手动 custom_phrase / rime 用户词典（同一候选可属于多份）。 */
enum class CandidateSource { AUTO, CUSTOM, RIME }

data class CandidateState(
    val candidates: List<String> = emptyList(),
    val candidateComments: List<String> = emptyList(),
    val inputText: String = "",
    val preeditText: String = "",
    val isComposing: Boolean = false,
    val hasNextPage: Boolean = false,
    val hasPrevPage: Boolean = false,
    val associationCandidates: List<String> = emptyList(),
    val pendingEnglishText: String = "",
    val isShowingRecentClipboard: Boolean = false,
    /** 与 candidates 等长的候选来源集合，供长按删除/降频按来源路由。 */
    val candidateSources: List<Set<CandidateSource>> = emptyList()
)
