package com.kingzcheung.xime.settings

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 每页候选词数：设备上存了就用设备值，没有/非法值回落 30。
 *
 * 回归背景：JNI 曾丢失 page_size 覆盖值重放逻辑，app 设置 30 也被方案自带的
 * PC 默认 5 盖掉；这里锁死「设置值优先、缺省 30」的 Kotlin 侧语义。
 */
class PageSizeSettingTest {

    @Test
    fun missingSettingFallsBackToThirty() {
        assertEquals(30, DEFAULT_CANDIDATE_PAGE_SIZE)
        assertEquals(30, effectivePageSize(0))
        assertEquals(30, effectivePageSize(-1))
    }

    @Test
    fun storedDeviceValueWins() {
        assertEquals(20, effectivePageSize(20))
        assertEquals(30, effectivePageSize(30))
        assertEquals(50, effectivePageSize(50))
    }
}
