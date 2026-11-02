package com.kingzcheung.xime.scheme

/** 安装流程的阶段标记（跨进程恢复用）：EXTRACTING=内容可能残缺，需重解压。 */
internal enum class InstallState { READY, EXTRACTING, DEPLOY_FAILED }

/**
 * 方案集状态：活动包 + 启用列表 + 当前方案。
 *
 * 唯一写入者是 [SchemeStore]；`default.yaml` 的 `schema_list` 是它的派生物。
 */
internal data class SchemeState(
    val activePackageId: String,
    val enabled: List<String>,
    val current: String,
)

/**
 * 纯函数：迁移与一致性修复的全部规则（不碰磁盘、不碰 prefs）。
 *
 * 这些规则只依赖「rime 里实际有哪些方案」与「prefs 里记了什么」，因此可单测。
 */
internal object SchemeLogic {

    /**
     * 推断活动包：`rime` 里只有一套内容，所以它和哪个包声明的方案重合最多，就是哪个。
     *
     * 找不到任何重合（旧状态残缺、包声明与实际不符）时返回内置包 —— 内置内容
     * 永远能从 assets 重建，是最安全的兜底。
     *
     * @param declared 候选包及其声明的方案 id，**顺序有用**：并列时取靠前者
     *   （调用方把内置包排第一，即并列兜底内置）。
     */
    fun inferActivePackage(
        installedSchemaIds: List<String>,
        declared: List<Pair<String, Set<String>>>,
        builtinPackageId: String,
    ): String {
        val installed = installedSchemaIds.toSet()
        if (installed.isEmpty()) return builtinPackageId
        var bestId = builtinPackageId
        var bestOverlap = 0
        for ((packageId, schemaIds) in declared) {
            val overlap = schemaIds.count { it in installed }
            if (overlap > bestOverlap) {
                bestOverlap = overlap
                bestId = packageId
            }
        }
        return if (bestOverlap > 0) bestId else builtinPackageId
    }

    /**
     * 启用列表 = 用户选择 ∩ 实际存在的方案，保持用户顺序。
     *
     * 不在这里补任何"内置方案"：补齐是迁移期的一次性动作，见 [SchemeStore]。
     */
    fun filterEnabled(stored: List<String>?, installedSchemaIds: List<String>): List<String> {
        if (stored == null) return emptyList()
        val installed = installedSchemaIds.toSet()
        return stored.filter { it in installed }.distinct()
    }

    /**
     * 启用列表为空时的初始集合：**只**用包声明的开箱方案。
     *
     * 不猜"第一个可用方案"：没有 `schema_list` 声明的包一律视为未启用，
     * 交给用户在「输入方案」页手动启用；否则包内排序靠前的依赖 schema
     * （如反查用的 `pinyin`）会被误当成用户方案。
     */
    fun initialEnabled(declared: List<String>, installedSchemaIds: List<String>): List<String> {
        val installed = installedSchemaIds.toSet()
        return declared.filter { it in installed }.distinct()
    }

    /** 当前方案必须落在启用列表里，否则回落到第一个启用方案（可能为空串）。 */
    fun pickCurrent(current: String, enabled: List<String>): String =
        if (current in enabled) current else enabled.firstOrNull().orEmpty()
}
