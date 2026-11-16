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
    /** candidates 前 N 个为 app 层注入的正常自动学习词（点击直接上屏，不经过 rime）。 */
    val customPhraseCount: Int = 0,
    /** candidates 末尾 N 个为降频自动学习词（负分，追加在 Rime 当前页之后，展开列表仍可选）。 */
    val demotedPhraseCount: Int = 0,
    /** 与 candidates 等长的候选来源集合，供长按删除/降频按来源路由。 */
    val candidateSources: List<Set<CandidateSource>> = emptyList()
)
