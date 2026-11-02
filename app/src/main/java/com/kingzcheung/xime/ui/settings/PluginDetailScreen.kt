package com.kingzcheung.xime.ui.settings

import com.kingzcheung.xime.R

import com.kingzcheung.xime.i18n.tr

import android.widget.Toast
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import com.kingzcheung.xime.plugin.ExtensionManager
import com.kingzcheung.xime.plugin.core.api.EmojiPlugin
import com.kingzcheung.xime.plugin.core.config.IPluginConfigurable

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PluginSettingsContent(
    pluginId: String,
    onBack: () -> Unit
) {
    val context = LocalContext.current
    val pluginInstance = remember(pluginId) { ExtensionManager.getPluginById(pluginId) }
    val pluginInfo = remember(pluginId) { ExtensionManager.getAllInstalledPlugins().find { it.id == pluginId } }

    if (pluginInfo == null) {
        PluginDetailFrame(title = tr(R.string.s_chajianshezhi), onBack = onBack) {
            Text(tr(R.string.s_chajianweizhaodao))
        }
        return
    }

    if (pluginInstance == null) {
        PluginDetailFrame(title = pluginInfo.name, onBack = onBack) {
            Text(tr(R.string.s_chajianweijiazai_qingzaichajia_b7dd6))
        }
        return
    }

    // schema 在 IO 读取：组合期不进插件运行时（主线程不再被 JS 执行阻塞，
    // 也不会与插件卸载/重载的 close() 抢引擎）
    val configurable = pluginInstance as? IPluginConfigurable
    val schemaState = rememberPluginSettingsSchema(configurable, pluginId)
    val hasCustomSettings = pluginInstance is EmojiPlugin && pluginInstance.hasSettings()

    when {
        configurable != null && schemaState.schema.isNotEmpty() -> {
            PluginConfigFormScreen(
                pluginId = pluginId,
                plugin = configurable,
                pluginName = pluginInfo.name,
                schema = schemaState.schema,
                onBack = onBack
            )
        }

        // 读取中：与"该插件没有设置界面"区分，避免闪一下空态文案
        !schemaState.loaded -> {
            PluginDetailFrame(title = pluginInfo.name, onBack = onBack) {
                CircularProgressIndicator()
            }
        }

        hasCustomSettings -> {
            PluginDetailFrame(title = pluginInfo.name, onBack = onBack) {
                LaunchedEffect(Unit) {
                    try {
                        (pluginInstance as EmojiPlugin).openSettings(context)
                        onBack()
                    } catch (e: Exception) {
                        Toast.makeText(context, tr(R.string.s_wufadakaichajianshezhi, e.message), Toast.LENGTH_LONG).show()
                        onBack()
                    }
                }
            }
        }

        else -> {
            PluginDetailFrame(title = pluginInfo.name, onBack = onBack) {
                Text(tr(R.string.s_gaichajianmeiyoushezhijiemian))
            }
        }
    }
}

/** 插件设置页统一外框（返回键 + 标题），各分支共用，避免重复 TopAppBar。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PluginDetailFrame(
    title: String,
    onBack: () -> Unit,
    content: @Composable () -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(title) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = tr(R.string.s_fanhui)
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    titleContentColor = MaterialTheme.colorScheme.onSurface
                )
            )
        }
    ) {
        Box(
            modifier = Modifier.fillMaxSize(),
            contentAlignment = Alignment.Center
        ) {
            content()
        }
    }
}
