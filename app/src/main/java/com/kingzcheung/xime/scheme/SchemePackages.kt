package com.kingzcheung.xime.scheme

import android.content.Context
import com.kingzcheung.xime.settings.MarketVersionStore
import com.kingzcheung.xime.settings.SettingsPreferences
import org.json.JSONObject

/**
 * 方案包列表：UI 只需要"我有哪些包、哪个在用"。
 *
 * 状态全部现算，不落清单文件：
 * - 可切换 = `market/<id>/` 里有压缩包；
 * - 使用中 = prefs 的 active（同一时刻只有一个）；
 * - 名称/版本 = 安装时记在 prefs 的 `scheme_package_info`（离线可用），
 *   缺失时退回 market 版本记录 / 包 id。
 */
object SchemePackages {

    private const val KEY_PACKAGE_INFO = "scheme_package_info"

    data class Entry(
        val id: String,
        val name: String,
        val version: String,
        val downloaded: Boolean,
        val active: Boolean,
        val schemaCount: Int,
    )

    fun list(context: Context): List<Entry> {
        val active = SettingsPreferences.getActivePackageId(context)
        val infos = readInfo(context)
        val downloaded = SchemeFiles.downloadedPackageIds(context).toSet()
        val ids = (downloaded + infos.keys + listOfNotNull(active))
            .filter { it != SchemeFiles.BUILTIN_PACKAGE_ID }
            // 压缩包已删、又非当前使用中的包不再列出：历史版本删除时可能遗留
            // scheme_package_info，这类条目既不能安装也不能删除，只会变成「未知」僵尸卡片
            .filter { it in downloaded || it == active }
            .sorted()
        val activeSchemaCount = SchemeFiles.installedSchemaIds(SchemeFiles.rimeDir(context)).size
        return ids.map { id ->
            Entry(
                id = id,
                name = infos[id]?.first ?: id,
                version = infos[id]?.second
                    ?: MarketVersionStore.getSchemeVersion(context, id).orEmpty(),
                downloaded = id in downloaded,
                active = id == active,
                schemaCount = if (id == active) activeSchemaCount else 0,
            )
        }
    }

    fun remember(context: Context, packageId: String, name: String, version: String) {
        if (packageId == SchemeFiles.BUILTIN_PACKAGE_ID) return
        val infos = readInfo(context).toMutableMap()
        if (name.isBlank() && version.isBlank()) return
        infos[packageId] = name.ifBlank { packageId } to version
        val json = JSONObject()
        infos.forEach { (id, info) ->
            json.put(id, JSONObject().put("n", info.first).put("v", info.second))
        }
        SettingsPreferences.getPrefsPublic(context).edit()
            .putString(KEY_PACKAGE_INFO, json.toString())
            .apply()
    }

    fun forget(context: Context, packageId: String) {
        val infos = readInfo(context).toMutableMap()
        if (infos.remove(packageId) == null) return
        val json = JSONObject()
        infos.forEach { (id, info) ->
            json.put(id, JSONObject().put("n", info.first).put("v", info.second))
        }
        SettingsPreferences.getPrefsPublic(context).edit()
            .putString(KEY_PACKAGE_INFO, json.toString())
            .apply()
    }

    fun nameOf(context: Context, packageId: String): String =
        readInfo(context)[packageId]?.first ?: packageId

    fun versionOf(context: Context, packageId: String): String =
        readInfo(context)[packageId]?.second
            ?: MarketVersionStore.getSchemeVersion(context, packageId).orEmpty()

    private fun readInfo(context: Context): Map<String, Pair<String, String>> {
        val raw = SettingsPreferences.getPrefsPublic(context).getString(KEY_PACKAGE_INFO, null)
            ?: return emptyMap()
        return runCatching {
            val json = JSONObject(raw)
            json.keys().asSequence().associate { id ->
                val entry = json.optJSONObject(id)
                id to ((entry?.optString("n").orEmpty()) to (entry?.optString("v").orEmpty()))
            }
        }.getOrDefault(emptyMap())
    }
}
