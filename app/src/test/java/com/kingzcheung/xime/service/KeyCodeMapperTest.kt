package com.kingzcheung.xime.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class KeyCodeMapperTest {

    @Test
    fun `shift符号英文模式转换`() {
        assertEquals("?", shiftedSymbol("/", chineseMode = false))
        assertEquals(":", shiftedSymbol(";", chineseMode = false))
        assertEquals("!", shiftedSymbol("1", chineseMode = false))
        assertEquals("(", shiftedSymbol("9", chineseMode = false))
        assertEquals("_", shiftedSymbol("-", chineseMode = false))
        assertEquals("~", shiftedSymbol("`", chineseMode = false))
    }

    @Test
    fun `shift符号中文模式转全角`() {
        assertEquals("？", shiftedSymbol("/", chineseMode = true))
        assertEquals("：", shiftedSymbol(";", chineseMode = true))
        assertEquals("！", shiftedSymbol("1", chineseMode = true))
        assertEquals("（", shiftedSymbol("9", chineseMode = true))
        assertEquals("——", shiftedSymbol("-", chineseMode = true))
    }

    @Test
    fun `中英文相同的符号保留半角`() {
        listOf("2" to "@", "3" to "#", "4" to "$", "5" to "%", "6" to "^", "7" to "&", "8" to "*", "=" to "+").forEach {
            assertEquals(it.second, shiftedSymbol(it.first, chineseMode = false))
            assertEquals(it.second, shiftedSymbol(it.first, chineseMode = true))
        }
    }

    @Test
    fun `字母键与无表项返回null走原逻辑`() {
        listOf("a", "z", "A", "space", "enter").forEach {
            assertNull(shiftedSymbol(it, chineseMode = false))
            assertNull(shiftedSymbol(it, chineseMode = true))
        }
    }
}
