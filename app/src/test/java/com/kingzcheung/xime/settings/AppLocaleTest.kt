package com.kingzcheung.xime.settings

import android.os.LocaleList
import org.junit.Assert.assertEquals
import org.junit.Test
import java.util.Locale

class AppLocaleTest {

    @Test
    fun `zh-MO 归一化为 zh-HK`() {
        assertEquals("zh-HK", AppLocale.normalize(Locale.forLanguageTag("zh-MO")).toLanguageTag())
    }

    @Test
    fun `其他语言保持不变`() {
        listOf("zh-Hans", "zh-TW", "en-US", "ja-JP").forEach { tag ->
            assertEquals(tag, AppLocale.normalize(Locale.forLanguageTag(tag)).toLanguageTag())
        }
    }

    @Test
    fun `override 配置仅携带 locale，不固化 uiMode 等系统字段`() {
        // 显式注入语言列表：JVM 单测里 Resources.getSystem() 为 null，不能取系统语言
        val cfg = AppLocale.localeOverrideConfig(LocaleList(Locale.SIMPLIFIED_CHINESE))
        assertEquals("uiMode 被 override 固化会导致深浅色不跟随系统", 0, cfg.uiMode)
        assertEquals("屏幕尺寸被 override 固化会导致旋转/折叠后尺寸过期", 0, cfg.screenWidthDp)
        assertEquals(0, cfg.screenHeightDp)
        assertEquals(0f, cfg.fontScale)
    }
}
