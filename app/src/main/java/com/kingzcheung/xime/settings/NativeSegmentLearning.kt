package com.kingzcheung.xime.settings

/**
 * 分段选词整串写 app 学习表的判定：纯小写字母编码 + 纯中文词条（>=2 字）。
 *
 * 背景：zhouqu 先选「舟」再选「去」是 Rime 原生分段选词，词条由 librime 写进
 * user_dict，app 学习表（auto_phrase）没有它，候选栏因此不会给它长按删除/降频
 * 菜单。提交时补记一条整串学习词（zhouqu -> 舟去）即可与英文自动联想词统一；
 * 但必须排除「只剩最后一个片段的码/词」，否则会把 qu -> 舟去 这类错误映射写进
 * 学习表（历史上 weiguangzheng -> 正 就是这样产生的）。
 */
internal fun shouldLearnNativeSegmentPhrase(inputCode: String, phrase: String): Boolean {
    val code = inputCode.trim().lowercase()
    val word = phrase.trim()
    if (code.length < 2 || word.length < 2) return false
    if (!code.all { it in 'a'..'z' }) return false
    return word.all { it in '\u4e00'..'\u9fff' }
}
