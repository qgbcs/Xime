package com.kingzcheung.xime.i18n

import android.content.Context
import androidx.annotation.StringRes
import com.kingzcheung.xime.XimeApplication
import com.kingzcheung.xime.settings.AppLocale

/**
 * 全局取词：Compose / ViewModel / Service 等任意位置都可调用 [tr]。
 * 语言切换时由 [AppLocale] 刷新为对应语言的 Context。
 * 未初始化（如纯 JVM 单测）时返回空串，避免抛异常。
 */
object UiText {
    @Volatile
    private var context: Context? = null

    /** 当前界面是否为香港繁体（用于内置数据文案转换）。 */
    @Volatile
    var isHongKong: Boolean = false
        private set

    fun init(context: Context) {
        this.context = context
        isHongKong = AppLocale.isHongKong()
    }

    fun get(@StringRes id: Int, args: Array<out Any?>): String {
        val ctx = context ?: XimeApplication.instance?.let { AppLocale.wrap(it) }
        if (ctx != null) {
            return if (args.isEmpty()) ctx.getString(id) else ctx.getString(id, *args)
        }
        // 纯 JVM 单测/预览没有 Context：返回简体原文兜底
        val raw = UiTextFallback.get(id) ?: return ""
        return if (args.isEmpty()) raw else String.format(raw, *args)
    }
}

/** 取当前界面语言的文案（简中 / 香港繁体）。 */
fun tr(@StringRes id: Int, vararg args: Any?): String = UiText.get(id, args)
