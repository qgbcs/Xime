package com.kingzcheung.xime.scheme

import android.content.Context
import com.kingzcheung.xime.settings.SchemaManager
import com.kingzcheung.xime.settings.SettingsPreferences
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONObject
import java.io.File

/**
 * 方案集状态的唯一读写入口。
 *
 * 状态只有一份（prefs 的 `active_package_id` / `enabled_schemas` / `current_schema`）：
 * - [ensure] 负责迁移与一致性修复，把"prefs 里记的"对齐到"磁盘上真有的"；
 * - [writeEnabled] / [setCurrent] / [setActive] 是写入方，同步更新内存缓存；
 * - `default.yaml` 的 `schema_list` 不在状态里，它是 [SchemaManager.applyEnabledSchemasToDefaultYaml]
 *   按状态生成的派生物。
 *
 * 进程内缓存只在首次 [ensure] 时算一次，之后由写入方法维护；外部直接改 prefs
 * （备份恢复等）必须调用 [invalidate]。
 */
internal object SchemeStore {

    private const val TAG = "SchemeStore"
    private const val MANIFESTS_DIR = ".manifests"
    private const val KEY_INSTALL_STATE = "scheme_install_state"

    private val lock = Any()

    @Volatile
    private var cached: SchemeState? = null

    private val builtinId: String get() = SchemeFiles.BUILTIN_PACKAGE_ID

    /** 当前状态；未初始化时先 [ensure]。 */
    fun state(context: Context): SchemeState = cached ?: ensure(context)

    /**
     * 迁移 + 一致性修复（幂等）：
     * 1. 活动包：prefs 有就用，没有才按"磁盘内容与各包声明的重合度"推断一次；
     * 2. 启用列表：用户选择 ∩ 实际存在的方案；为空则用包声明的开箱集合；
     * 3. 当前方案：不在启用列表里就回落到第一个；
     * 4. 有变化才回写 prefs。
     */
    fun ensure(context: Context): SchemeState = synchronized(lock) {
        cached?.let { return it }

        val installed = SchemeFiles.installedSchemaIds(SchemeFiles.rimeDir(context))
        val storedActive = SettingsPreferences.getActivePackageId(context)?.takeIf { it.isNotBlank() }
        val active = storedActive ?: SchemeLogic.inferActivePackage(
            installedSchemaIds = installed,
            declared = legacyDeclaredPackages(context),
            builtinPackageId = builtinId,
        )
        val declared = declaredSchemas(context, active)

        // 一次性迁移：旧版本在没有 schema_list 声明的包里会自动猜第一个 schema，
        // 猜出来的可能是依赖 schema（如反查用的 pinyin）。这里作废这类旧选择，
        // 让用户重新在「输入方案」里启用。
        val storedEnabled = if (dropLegacySelection(context, active, declared)) {
            emptyList()
        } else {
            SettingsPreferences.getEnabledSchemaList(context)
        }

        var enabled = SchemeLogic.filterEnabled(storedEnabled, installed)
        if (enabled.isEmpty()) enabled = SchemeLogic.initialEnabled(declared, installed)
        val storedCurrent = SettingsPreferences.getCurrentSchema(context)
        val current = SchemeLogic.pickCurrent(storedCurrent, enabled)

        if (storedActive != active) SettingsPreferences.setActivePackageId(context, active)
        if (storedEnabled != enabled) SettingsPreferences.setEnabledSchemaList(context, enabled)
        if (storedCurrent != current) SettingsPreferences.setCurrentSchema(context, current)

        cleanupLegacyArtifacts(context)
        if (storedActive != active || storedEnabled != enabled || storedCurrent != current) {
            FileLogger.i(
                TAG,
                "ensure: active=$active, enabled=${enabled.joinToString()}, current=$current"
            )
        }
        SchemeState(active, enabled, current).also { cached = it }
    }

    /**
     * 清理旧模型的归属表与内置快照：`.manifests` / `.registry.json` / `.package-presets` /
     * `market/builtin/`。推断已经读完清单，留着只会误导后来者。
     */
    private fun cleanupLegacyArtifacts(context: Context) {
        val filesDir = context.filesDir
        val legacy = listOf(
            File(filesDir, MANIFESTS_DIR),
            File(filesDir, ".registry.json"),
            File(filesDir, ".package-presets"),
            File(SchemeFiles.marketDir(context), SchemeFiles.BUILTIN_PACKAGE_ID),
        )
        legacy.filter { it.exists() }.forEach { path ->
            val removed = path.deleteRecursively()
            FileLogger.i(TAG, "cleanup legacy ${path.name}: removed=$removed")
        }
    }

    /** 丢弃缓存，下次 [state] / [ensure] 重新推导（备份恢复、外部改 prefs 后调用）。 */
    fun invalidate() {
        synchronized(lock) { cached = null }
    }

    /**
     * 一次性迁移（只跑一次）：活动包没有 `schema_list` 声明时，旧版本会猜"第一个
     * schema"作为启用方案，这个猜测结果不可信（可能选到依赖 schema），直接作废。
     *
     * @return true 表示调用方应把已存启用列表当成空。
     */
    private fun dropLegacySelection(context: Context, active: String, declared: List<String>): Boolean {
        if (SettingsPreferences.isSchemeSelectionMigrated(context)) return false
        SettingsPreferences.setSchemeSelectionMigrated(context, true)
        val drop = active != builtinId && declared.isEmpty()
        if (drop) FileLogger.i(TAG, "migrate: drop legacy selection of '$active' (no schema_list)")
        return drop
    }

    fun setActive(context: Context, packageId: String?) {
        synchronized(lock) {
            SettingsPreferences.setActivePackageId(context, packageId)
            cached = null
        }
    }

    /**
     * 写启用列表（只写状态，不碰文件），返回过滤后的有效列表。
     *
     * 过滤器保证不变量：启用列表里只有真实存在的方案；当前方案失效时同步回落。
     */
    fun writeEnabled(context: Context, schemaIds: List<String>): List<String> = synchronized(lock) {
        val installed = SchemeFiles.installedSchemaIds(SchemeFiles.rimeDir(context))
        val effective = SchemeLogic.filterEnabled(schemaIds, installed)
        // 以 prefs 为准而不是缓存：其它入口可能刚改过 current（如方案页点选）。
        val storedCurrent = SettingsPreferences.getCurrentSchema(context)
        val current = SchemeLogic.pickCurrent(storedCurrent, effective)

        SettingsPreferences.setEnabledSchemaList(context, effective)
        if (current != storedCurrent) SettingsPreferences.setCurrentSchema(context, current)
        cached = (cached ?: SchemeState(
            activePackageId = SettingsPreferences.getActivePackageId(context) ?: builtinId,
            enabled = effective,
            current = storedCurrent,
        )).copy(enabled = effective, current = current)
        effective
    }

    /** 安装流程的阶段标记：进程被杀后由 [SchemeInstaller.recoverIfNeeded] 用来判断要不要重解压。 */
    fun installState(context: Context): InstallState {
        val stored = SettingsPreferences.getPrefsPublic(context)
            .getString(KEY_INSTALL_STATE, null) ?: return InstallState.READY
        return runCatching { InstallState.valueOf(stored) }.getOrDefault(InstallState.READY)
    }

    fun setInstallState(context: Context, state: InstallState) {
        SettingsPreferences.getPrefsPublic(context).edit()
            .putString(KEY_INSTALL_STATE, state.name)
            .apply()
    }

    /**
     * 安装方在替换内容**之前**写入意图：此时磁盘上还是旧内容，不能按磁盘过滤，
     * 所以这里直接落 prefs。崩在中间时启动自检按这份意图重解压。
     */
    fun commitIntent(context: Context, active: String, enabled: List<String>, current: String) {
        synchronized(lock) {
            SettingsPreferences.setActivePackageId(context, active)
            SettingsPreferences.setEnabledSchemaList(context, enabled)
            SettingsPreferences.setCurrentSchema(context, current)
            cached = SchemeState(active, enabled, current)
        }
    }

    fun setCurrent(context: Context, schemaId: String) {
        synchronized(lock) {
            SettingsPreferences.setCurrentSchema(context, schemaId)
            cached = cached?.copy(current = schemaId)
        }
    }

    /** 活动包声明的开箱启用集合（内置包=内置清单；第三方包=包内 default.yaml）。 */
    private fun declaredSchemas(context: Context, active: String): List<String> =
        if (active == builtinId) SchemaManager.BUILTIN_SCHEMAS
        else SchemaManager.readPackagedSchemaList(context, active).orEmpty()

    /**
     * 旧模型留下的 `.manifests/` 清单只在迁移时读一次，用来推断活动包。
     * 内置包排第一：并列时兜底内置（内置内容永远可从 assets 重建）。
     */
    private fun legacyDeclaredPackages(context: Context): List<Pair<String, Set<String>>> {
        val result = mutableListOf<Pair<String, Set<String>>>(
            builtinId to SchemaManager.BUILTIN_SCHEMAS.toSet()
        )
        val dir = File(context.filesDir, MANIFESTS_DIR)
        dir.listFiles()?.filter { it.isFile && it.name.endsWith(".json") }?.forEach { file ->
            runCatching {
                val json = JSONObject(file.readText())
                val id = json.optString("schemeId")
                if (id.isBlank() || id == builtinId) return@runCatching
                val files = json.optJSONObject("files") ?: return@runCatching
                val schemaIds = files.keys().asSequence()
                    .filter { it.endsWith(".schema.yaml") && !it.contains('/') }
                    .map { it.removeSuffix(".schema.yaml") }
                    .toSet()
                if (schemaIds.isNotEmpty()) result += id to schemaIds
            }.onFailure { FileLogger.w(TAG, "Ignore invalid manifest ${file.name}: ${it.message}") }
        }
        return result
    }
}
