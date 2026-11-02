package com.kingzcheung.xime.service

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
    val demotedPhraseCount: Int = 0
)
