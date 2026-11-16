package com.kingzcheung.xime.settings

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 分段选词整串学习判定：整串编码 + 整串中文词条才写 app 学习表，
 * 防止「只剩最后一个片段的码」污染学习表（qu -> 舟去 / weiguangzheng -> 正）。
 */
class NativeSegmentLearningTest {

    @Test
    fun fullCodeAndFullPhraseIsLearned() {
        assertTrue(shouldLearnNativeSegmentPhrase("zhouqu", "舟去"))
        assertTrue(shouldLearnNativeSegmentPhrase("weiguangzheng", "伟光正"))
        // 大写编码按小写处理
        assertTrue(shouldLearnNativeSegmentPhrase("ZhouQu", "舟去"))
    }

    @Test
    fun singleSegmentOrSingleCharIsRejected() {
        assertFalse(shouldLearnNativeSegmentPhrase("q", "舟去"))
        assertFalse(shouldLearnNativeSegmentPhrase("", "舟去"))
        assertFalse(shouldLearnNativeSegmentPhrase("zhouqu", "舟"))
    }

    @Test
    fun nonHanOrMixedPhraseIsRejected() {
        assertFalse(shouldLearnNativeSegmentPhrase("hello", "hello"))
        assertFalse(shouldLearnNativeSegmentPhrase("zhouqu", "舟qu"))
        assertFalse(shouldLearnNativeSegmentPhrase("zhouqu", "舟 去"))
    }

    @Test
    fun nonAsciiCodeIsRejected() {
        assertFalse(shouldLearnNativeSegmentPhrase("zhou;qu", "舟去"))
        assertFalse(shouldLearnNativeSegmentPhrase("1234", "舟去"))
        assertFalse(shouldLearnNativeSegmentPhrase("zhou qu", "舟去"))
    }
}
