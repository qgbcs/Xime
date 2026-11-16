package com.kingzcheung.xime.rime

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 原生分段选词的组合态展示规则（3f9af3e 修复的回归测试）。
 *
 * 回归背景：保留已确认片段时 preedit 形如“舟qu”“伟guang zheng”，
 * 旧实现用 setInput(剩余码) 重建会话后既看不到已确认汉字，也不应再写 app 学习表。
 */
class CompositionDisplayRuleTest {

    @Test
    fun `preedit with confirmed han segment is detected`() {
        assertTrue(preeditHasConfirmedHan("舟qu"))
        assertTrue(preeditHasConfirmedHan("伟guang zheng"))
        assertTrue(preeditHasConfirmedHan("你好"))
    }

    @Test
    fun `plain pinyin preedit is not a confirmed han segment`() {
        assertFalse(preeditHasConfirmedHan(""))
        assertFalse(preeditHasConfirmedHan("zhouqu"))
        assertFalse(preeditHasConfirmedHan("qu"))
    }

    @Test
    fun `partial candidate text wins over preedit`() {
        assertEquals("舟", resolveComposingDisplayText("舟qu", "zhouqu", "舟"))
    }

    @Test
    fun `preedit with confirmed han is displayed instead of input`() {
        assertEquals(
            "伟guang zheng",
            resolveComposingDisplayText("伟guang zheng", "weiguangzheng", ""),
        )
    }

    @Test
    fun `plain speller preedit falls back to input`() {
        assertEquals("zhouqu", resolveComposingDisplayText("zhouqu", "zhouqu", ""))
        assertEquals("nihao", resolveComposingDisplayText("ni hao", "nihao", ""))
        assertEquals("", resolveComposingDisplayText("", "", ""))
    }

    @Test
    fun `native segmented commit text is trusted instead of falling back`() {
        assertFalse(
            shouldCommitDisplayedCandidate(
                selectedCandidate = "去",
                rimeCommitText = "舟去",
                preeditHasConfirmedHan = true,
                previewTextAfterSelection = "",
            )
        )
    }

    @Test
    fun `displayed candidate fallback stays for genuine commit mismatch`() {
        assertTrue(shouldCommitDisplayedCandidate("猪", "", false, ""))
        assertFalse(shouldCommitDisplayedCandidate("猪", "猪", false, ""))
        assertFalse(shouldCommitDisplayedCandidate("猪", "", false, null))
        assertFalse(shouldCommitDisplayedCandidate("猪", "", false, "zi"))
    }
}
