package com.kingzcheung.xime.settings

import android.content.Context

/**
 * app 内置的共享预设文件（`symbols.yaml`、`default.yaml` 等，加文件进 assets/rime 即自动纳入）：
 * 第三方方案用 `import_preset` / `__include` 引用，缺失时部署会编译失败。
 */
object RimeSharedFiles {
    private const val ASSETS_RIME_DIR = "rime"

    /** 内置共享文件名集合（按 assets 现算）。 */
    fun names(context: Context): Set<String> {
        val entries = context.assets.list(ASSETS_RIME_DIR) ?: return emptySet()
        return entries.filter { entry ->
            val isDir = (context.assets.list("$ASSETS_RIME_DIR/$entry") ?: emptyArray()).isNotEmpty()
            isSharedEntry(entry, isDir)
        }.toSet()
    }

    /** 只看预设/词表类文件，排除目录、隐藏项与方案/词典/补丁/文档。 */
    internal fun isSharedEntry(name: String, isDirectory: Boolean): Boolean {
        if (isDirectory || name.startsWith(".")) return false
        if (name.endsWith(".schema.yaml") || name.endsWith(".dict.yaml") || name.endsWith(".custom.yaml")) return false
        return name.endsWith(".yaml") || name.endsWith(".yml") || name.endsWith(".txt")
    }
}
