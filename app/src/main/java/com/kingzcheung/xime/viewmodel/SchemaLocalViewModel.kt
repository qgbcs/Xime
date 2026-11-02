package com.kingzcheung.xime.viewmodel

import com.kingzcheung.xime.R

import com.kingzcheung.xime.i18n.tr

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.kingzcheung.xime.scheme.SchemeFiles
import com.kingzcheung.xime.scheme.SchemeInstaller
import com.kingzcheung.xime.scheme.SchemePackages
import com.kingzcheung.xime.settings.MarketUpdateChecker
import com.kingzcheung.xime.settings.SchemaManager
import com.kingzcheung.xime.settings.SchemaManager.InstallFromDirResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class LocalPackageItem(
    val packageId: String,
    val displayName: String,
    val version: String,
    val downloaded: Boolean,
    /** 使用中（同一时刻只有一个包是"已安装"）。 */
    val installed: Boolean,
    val schemaCount: Int = 0,
    val fileCount: Int = 0,
) {
    val isImport: Boolean get() = packageId.startsWith("import_")
    val statusLabel: String
        get() = when {
            installed -> tr(R.string.s_yianzhuang)
            downloaded -> tr(R.string.s_yixiazai)
            else -> ""
        }
}

/** 本地包操作阶段（安装与部署分开显示）。 */
enum class LocalPackagePhase { INSTALLING, DEPLOYING }

data class SchemaLocalUiState(
    val packages: List<LocalPackageItem> = emptyList(),
    val isLoading: Boolean = false,
    val installingId: String? = null,
    val phase: LocalPackagePhase? = null,
    val errorMessage: String? = null,
    val toastMessage: String? = null,
)

class SchemaLocalViewModel(application: Application) : AndroidViewModel(application) {
    private val context = application.applicationContext

    private val _uiState = MutableStateFlow(SchemaLocalUiState())
    val uiState: StateFlow<SchemaLocalUiState> = _uiState.asStateFlow()

    init {
        loadLocalPackages()
    }

    fun loadLocalPackages() {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, errorMessage = null) }
            val packages = withContext(Dispatchers.IO) {
                SchemePackages.list(context).map { entry ->
                    LocalPackageItem(
                        packageId = entry.id,
                        displayName = entry.name,
                        version = entry.version,
                        downloaded = entry.downloaded,
                        installed = entry.active,
                        schemaCount = entry.schemaCount,
                        fileCount = SchemeFiles.marketDir(context, entry.id)
                            .listFiles()?.count { it.isFile } ?: 0,
                    )
                }
            }
            _uiState.update { it.copy(packages = packages, isLoading = false) }
        }
    }

    /** 安装即切换：当前方案集的内容会被替换，白名单用户数据保留。 */
    fun installPackage(item: LocalPackageItem) {
        if (_uiState.value.installingId != null || item.installed) return
        viewModelScope.launch {
            _uiState.update { it.copy(installingId = item.packageId, phase = LocalPackagePhase.INSTALLING) }
            val result = try {
                withContext(Dispatchers.IO) {
                    SchemaManager.installPackageFromMarketDir(
                        context = context,
                        packageId = item.packageId,
                        displayName = item.displayName,
                        version = item.version,
                        fromMarket = !item.isImport,
                    )
                }
            } catch (e: Exception) {
                InstallFromDirResult(success = false, failureReason = tr(R.string.s_anzhuangyichang, e.message))
            }
            _uiState.update { it.copy(installingId = null, phase = null) }
            showToast(installMessage(item, result))
            if (result.success) MarketUpdateChecker.refreshNow(context)
            loadLocalPackages()
        }
    }

    private fun installMessage(item: LocalPackageItem, result: InstallFromDirResult): String = buildString {
        if (result.success) {
            if (result.needsSelection) {
                append(tr(R.string.s_yianzhuang_qingxuanzefangan, item.displayName))
            } else {
                append(tr(R.string.s_yianzhuang_bingwanchengbushu, item.displayName))
            }
            if (result.parseFailures.isNotEmpty()) {
                append(tr(R.string.s_nyixiafanganjiexishibai))
                result.parseFailures.forEach { append("\n  · $it") }
            }
        } else {
            append(result.failureReason ?: tr(R.string.s_anzhuangshibai))
        }
    }

    /** 删除已下载的压缩包。使用中的包不允许删档（删了就切不回来）。 */
    fun deleteDownloaded(item: LocalPackageItem) {
        if (item.packageId == SchemeFiles.BUILTIN_PACKAGE_ID) {
            showToast(tr(R.string.s_neizhifanganbukeshanchu))
            return
        }
        if (item.installed) {
            showToast(tr(R.string.s_shiyongzhong_bunengshanchu_yasuobao))
            return
        }
        viewModelScope.launch {
            val ok = withContext(Dispatchers.IO) { SchemaManager.deleteSchemeArchive(context, item.packageId) }
            showToast(if (ok) tr(R.string.s_yishanchu_2) else tr(R.string.s_shanchushibai))
            if (ok) MarketUpdateChecker.refreshNow(context)
            loadLocalPackages()
        }
    }

    /** 移除方案集：使用中的包会先切回内置方案集。 */
    fun uninstall(item: LocalPackageItem) {
        viewModelScope.launch {
            val ok = withContext(Dispatchers.IO) { SchemaManager.deleteSchemaFiles(context, item.packageId) }
            if (ok) {
                withContext(Dispatchers.IO) { SchemaManager.deleteSchemeArchive(context, item.packageId) }
            }
            showToast(if (ok) tr(R.string.s_yixiezai) else tr(R.string.s_xiezaishibai, item.displayName))
            MarketUpdateChecker.refreshNow(context)
            loadLocalPackages()
        }
    }

    /** 恢复内置方案集（不删任何压缩包，之后仍可切回第三方包）。 */
    fun restoreBuiltin() {
        viewModelScope.launch {
            val ok = withContext(Dispatchers.IO) { SchemeInstaller.restoreBuiltin(context).success }
            showToast(if (ok) tr(R.string.s_fanganbushuwancheng) else tr(R.string.s_anzhuangshibai))
            if (ok) MarketUpdateChecker.refreshNow(context)
            loadLocalPackages()
        }
    }

    fun clearToast() = _uiState.update { it.copy(toastMessage = null) }

    private fun showToast(message: String) = _uiState.update { it.copy(toastMessage = message) }
}
