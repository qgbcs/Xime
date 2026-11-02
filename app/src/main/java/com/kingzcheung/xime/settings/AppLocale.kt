package com.kingzcheung.xime.settings

import android.content.Context
import android.content.ContextWrapper
import android.content.res.Configuration
import android.os.LocaleList
import java.util.Locale

/**
 * 界面语言始终跟随系统语言。
 *
 * - 不提供 App 内语言开关，也不使用系统级 per-app locale（避免残留设置锁死语言）。
 * - 译文只维护 `values-zh-rHK`；zh-MO 与 zh-HK 用词一致，统一归一化为 zh-HK。
 * - Activity 与 IME Service 通过 [wrap] 覆盖 Context，保证设置页与键盘同语言。
 */
object AppLocale {

    /**
     * 当前生效的 LocaleList：始终取系统语言（忽略 per-app locale）。
     * App 内已不提供语言开关，避免残留的系统级应用语言把界面锁在旧语言。
     */
    fun currentLocales(): LocaleList =
        normalizeLocales(android.content.res.Resources.getSystem().configuration.locales)

    /** 是否香港繁体（zh-HK / zh-MO）。 */
    fun isHongKong(): Boolean {
        val locales = currentLocales()
        if (locales.size() == 0) return false
        val first = locales[0]
        return first.language == "zh" && first.country.equals("HK", ignoreCase = true)
    }

    /** zh-MO 与 zh-HK 文案一致，统一走香港资源。 */
    fun normalize(locale: Locale): Locale =
        if (locale.language == "zh" && locale.country.equals("MO", ignoreCase = true)) {
            Locale.forLanguageTag("zh-HK")
        } else {
            locale
        }

    /**
     * 包装 Context，仅把 locale 覆盖为系统语言。
     * 其余字段保持 UNSET 让系统配置继续生效；禁止拷贝完整配置快照，
     * 否则创建时刻的 uiMode/屏幕尺寸被固化为 override，深浅色不再跟随系统。
     */
    fun wrap(base: Context): Context =
        ContextWrapper(base.createConfigurationContext(localeOverrideConfig()))

    internal fun localeOverrideConfig(locales: LocaleList = currentLocales()): Configuration {
        val cfg = Configuration()
        cfg.setLocales(locales)
        return cfg
    }

    private fun normalizeLocales(list: LocaleList): LocaleList {
        val normalized = (0 until list.size()).map { normalize(list[it]) }
        val allSame = normalized.indices.all { normalized[it] == list[it] }
        return if (allSame) list else LocaleList(*normalized.toTypedArray())
    }
}
