package com.kingzcheung.xime.i18n

/**
 * App 内置分类标签的港繁映射（表情分类、符号分类）。
 *
 * 只用于代码内置的纯展示分类名；主题名、方案名、模型名、键面标签等来自
 * xime.yaml / schema YAML / 索引的数据一律原样显示，不做运行时转换。
 */
internal object DataTextMap {
    val map: Map<String, String> = mapOf(
        "笑脸" to "笑臉",
        "手势" to "手勢",
        "动物" to "動物",
        "食物" to "食物",
        "旅行" to "旅行",
        "活动" to "活動",
        "物品" to "物品",
        "符号" to "符號",
        "旗帜" to "旗幟",
        "中" to "中",
        "英" to "英",
        "数" to "數",
    )
}

/** 内置分类标签按当前界面语言取词；未命中时原样返回。 */
fun localizeData(value: String): String =
    if (UiText.isHongKong) DataTextMap.map[value] ?: value else value
