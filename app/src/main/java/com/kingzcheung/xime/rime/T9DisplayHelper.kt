package com.kingzcheung.xime.rime

import com.kingzcheung.xime.util.PreeditMergeHelper

/** T9 模式下 UI 展示状态：包含展示文本、候选列表和 composing 标志 */
data class T9DisplayState(
    val displayText: String,
    val displayCandidates: List<String>,
    val displayComments: List<String>,
    val isComposing: Boolean,
)

/**
 * 构建 T9 模式下的 UI 展示状态。
 *
 * 三种情形：
 * 1. 无 partial commit：优先 preedit（t9_preedit.lua 处理后的拼音），回退 input
 * 2. RightCommit 展示态（有 partial + preedit 为空）：
 *    displayText=partialTexts 拼接，候选列表=最近一次已提交文本
 * 3. 常规：mergePartialCommitText 合并
 */
fun buildT9DisplayState(
    partialTexts: List<String>,
    preeditText: String,
    inputText: String,
    candidates: List<String>,
    comments: List<String>,
): T9DisplayState {
    if (partialTexts.isEmpty()) {
        val text = if (preeditText.isNotEmpty()) preeditText else inputText
        return T9DisplayState(
            displayText = text,
            displayCandidates = candidates,
            displayComments = comments,
            isComposing = inputText.isNotEmpty(),
        )
    }
    // RightCommit 展示态：preedit 为空（composition 已清除），input 可能为残留值
    if (preeditText.isEmpty()) {
        val last = partialTexts.last()
        return T9DisplayState(
            displayText = partialTexts.joinToString(""),
            displayCandidates = listOf(last),
            displayComments = comments.firstOrNull()?.let { listOf(it) } ?: emptyList(),
            isComposing = true,
        )
    }
    return T9DisplayState(
        displayText = PreeditMergeHelper.mergePartialCommitText(partialTexts, preeditText),
        displayCandidates = candidates,
        displayComments = comments,
        isComposing = inputText.isNotEmpty() || partialTexts.isNotEmpty(),
    )
}

/**
 * 解析 RIME 原始候选词列表中与用户选中候选词对应的 index。
 *
 * UI 候选词列表经过 filterCandidatesBySelectionHistory 过滤/重排序后，index 可能与
 * RIME 原始候选词 index 不对应。通过候选词文本查找 RIME 原始候选词的真实 index。
 *
 * @param uiIndex 原始 fallback index
 * @param selectedCandidate 用户选中的候选词文本
 * @param rawCandidates RIME 原始候选词列表（字符串文本）
 * @return 原始候选词列表中匹配的 index，未找到返回 -1，避免错选同位置的其他候选
 */
fun resolveRimeCandidateIndex(
    uiIndex: Int,
    selectedCandidate: String?,
    rawCandidates: List<String>,
): Int {
    if (selectedCandidate == null) return uiIndex
    val rawIndex = rawCandidates.indexOf(selectedCandidate)
    return rawIndex
}

data class CandidateInputPrefix(
    val selectedInput: String,
    val remainingInput: String,
    val selectedText: String,
)

fun candidateSelectionLeavesComposition(
    selectionAccepted: Boolean,
    committedText: String,
): Boolean = selectionAccepted && committedText.isEmpty()

fun canProbeDanglingTail(preview: RimeCandidatePreview?): Boolean = preview != null

fun hasRemainingCandidateComposition(input: String, candidateCount: Int): Boolean =
    input.isNotEmpty() && candidateCount > 0

fun resolveCandidateInputPrefix(
    input: String,
    preview: RimeCandidatePreview,
    expectedText: String,
): CandidateInputPrefix? {
    val remainingInput = preview.textAfterSelection
    if (preview.selectedText != expectedText || remainingInput.isEmpty() ||
        !input.endsWith(remainingInput)
    ) {
        return null
    }
    val selectedInput = input.removeSuffix(remainingInput)
    val selectedText = preview.textBeforeSelection + preview.selectedText
    if (selectedInput.isEmpty() || selectedInput == input || selectedText.isEmpty()) {
        return null
    }
    return CandidateInputPrefix(selectedInput, remainingInput, selectedText)
}

/** 组合态 preedit 是否已含 Rime 原生确认的中文片段（如“伟guang zheng”里的“伟”）。 */
fun preeditHasConfirmedHan(preeditText: String): Boolean =
    preeditText.any { it.code in 0x4E00..0x9FFF }

/**
 * 非 T9 组合态要展示的文本：部分选词 > 含已确认中文的 preedit > 原始输入。
 *
 * preedit 只是 speller 展开的拼音时不展示（与原输入等价）；一旦包含输入里没有的字符
 * （原生分段选中后残留的汉字），展示 preedit 才能看到已选中文字与剩余编码的边界。
 */
fun resolveComposingDisplayText(
    preeditText: String,
    inputText: String,
    partialCandidateText: String,
): String {
    if (partialCandidateText.isNotEmpty()) return partialCandidateText
    val lowerInput = inputText.lowercase()
    val hasExtraContent = preeditText.any { c ->
        !c.isWhitespace() && c != '\'' && !lowerInput.contains(c.lowercaseChar())
    }
    return if (preeditText.isNotEmpty() && hasExtraContent) preeditText else inputText
}

/**
 * Rime 提交文本与所点候选不一致时，是否用“已展示候选”兜底上屏。
 *
 * 原生分段（preedit 已含中文）时 commit 文本本身就是完整整词（如“舟去”），
 * 兜底会丢掉已确认前缀并写错学习词，必须交由 Rime 的提交文本直接上屏。
 */
fun shouldCommitDisplayedCandidate(
    selectedCandidate: String?,
    rimeCommitText: String,
    preeditHasConfirmedHan: Boolean,
    previewTextAfterSelection: String?,
): Boolean =
    selectedCandidate != null &&
        rimeCommitText != selectedCandidate &&
        !preeditHasConfirmedHan &&
        previewTextAfterSelection != null &&
        previewTextAfterSelection.isEmpty()