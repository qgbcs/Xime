package com.kingzcheung.xime.util

import androidx.compose.runtime.mutableStateMapOf

/**
 * RPC 可下发的通用 UI 覆盖存储。
 *
 * key 由 target（UI 槽位）+ prop（属性）组成，value 为字符串或 JSON 文本。
 * 使用 Compose 快照状态，RPC 线程写入后，组合中读取该 key 的组件会自动重组。
 */
object UiOverrides {
    const val TARGET_CANDIDATE_LOGO = "ime.candidate.logo"
    const val TARGET_MENU_FEEDBACK = "ime.menu.feedback"

    private val values = mutableStateMapOf<String, String>()

    private fun key(target: String, prop: String): String = "$target#$prop"

    fun set(target: String, prop: String, value: String) {
        values[key(target, prop)] = value
    }

    fun get(target: String, prop: String): String? = values[key(target, prop)]

    fun clear(target: String, prop: String) {
        values.remove(key(target, prop))
    }

    fun clearTarget(target: String) {
        values.keys.toList()
            .filter { it.startsWith("$target#") }
            .forEach { values.remove(it) }
    }

    fun clearAll() {
        values.clear()
    }

    fun snapshot(): Map<String, String> = values.toMap()

    /** 反馈红点链路：候选栏图标与菜单反馈项共享同一枚角标。 */
    fun clearBadgeChain() {
        clear(TARGET_CANDIDATE_LOGO, "badge")
        clear(TARGET_MENU_FEEDBACK, "badge")
    }
}
