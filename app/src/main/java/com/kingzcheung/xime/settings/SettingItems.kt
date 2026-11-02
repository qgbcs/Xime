package com.kingzcheung.xime.settings

/**
 * 设置页可动态显隐的条目，统一在此登记。
 *
 * 条目的持久化键为 `setting_item_visible_<id>`，RPC 状态键为
 * `settings.item.<id>.visible`，均由 [id] 派生；新增隐藏条目只需在
 * [hiddenByDefault] 中加入其 id，设置页无需再单独声明状态变量。
 */
object SettingItems {
    const val ABOUT_APP = "about_app"
    const val RPC = "rpc_settings"

    /** 默认隐藏的条目；未列出的条目默认显示。 */
    val hiddenByDefault: Set<String> = setOf(ABOUT_APP, RPC)
}
