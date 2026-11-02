package com.kingzcheung.xime.ui.settings

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import android.view.inputmethod.InputMethodManager
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.twotone.LibraryBooks
import androidx.compose.material.icons.twotone.AutoAwesome
import androidx.compose.material.icons.twotone.Ballot

import androidx.compose.material.icons.twotone.Build
import androidx.compose.material.icons.twotone.CloudSync
import androidx.compose.material.icons.twotone.Description
import androidx.compose.material.icons.twotone.Extension
import androidx.compose.material.icons.twotone.GraphicEq
import androidx.compose.material.icons.twotone.Info
import androidx.compose.material.icons.twotone.Keyboard
import androidx.compose.material.icons.twotone.KeyboardAlt
import androidx.compose.material.icons.twotone.NetworkCheck
import androidx.compose.material.icons.twotone.Palette
import androidx.compose.material.icons.twotone.Refresh
import androidx.compose.material.icons.twotone.Security
import androidx.compose.material.icons.twotone.Storefront
import androidx.compose.material.icons.twotone.Straighten
import androidx.compose.material.icons.twotone.Sync
import androidx.compose.material.icons.twotone.TableChart
import androidx.compose.material.icons.twotone.ToggleOn
import androidx.compose.material.icons.twotone.TypeSpecimen
import androidx.compose.material.icons.twotone.Vibration
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MediumTopAppBar
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusEvent
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.chaquo.python.Python
import com.kingzcheung.xime.MainActivity
import com.kingzcheung.xime.settings.SettingItems
import com.kingzcheung.xime.settings.SettingsPreferences
import com.kingzcheung.xime.util.LauncherIconController
import com.kingzcheung.xime.util.RpcUiController
import org.json.JSONObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import com.kingzcheung.xime.BuildConfig

/** RPC 改动条目显隐时使用的状态键前缀，如 settings.item.rpc_settings.visible。 */
private const val SETTING_ITEM_STATE_PREFIX = "settings.item."

/** 连点标题的触发次数与相邻两次点击的最大间隔。 */
private const val TITLE_MULTI_TAP_COUNT = 4
private const val TITLE_MULTI_TAP_WINDOW_MS = 1500L

/**
 * 设置页条目显隐的统一读取入口，取代每个条目各自的 visible 状态变量。
 *
 * 优先级：本次运行期的临时显示（[reveal]，不落盘）> RPC 运行期状态 > 持久化偏好。
 * 持久化的默认可见性集中在 [SettingItems.hiddenByDefault]。
 */
private class SettingsVisibility(private val context: Context) {
    /** RPC 运行期覆盖，由调用方在每次重组时刷新。 */
    var remoteOverrides: Map<String, String> = emptyMap()

    private val runtimeVisible = mutableStateMapOf<String, Boolean>()

    fun isVisible(itemId: String): Boolean {
        runtimeVisible[itemId]?.let { return it }
        remoteOverrides["$SETTING_ITEM_STATE_PREFIX$itemId.visible"]
            ?.toBooleanStrictOrNull()
            ?.let { return it }
        return SettingsPreferences.isSettingItemVisible(context, itemId)
    }

    /** 仅本次运行期显示，不写偏好，重启后仍按默认隐藏。 */
    fun reveal(itemId: String) {
        runtimeVisible[itemId] = true
    }
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun SettingsMainContent(
    onNavigateToSchema: () -> Unit,
    onNavigateToMarket: () -> Unit = {},
    onNavigateToTheme: () -> Unit,
    onNavigateToKeyEffect: () -> Unit,
    onNavigateToLayoutDisplay: () -> Unit,
    onNavigateToDictionary: () -> Unit,
    onNavigateToPlugins: () -> Unit,
    onNavigateToModelLocal: () -> Unit = {},
    onNavigateToSmartPrediction: () -> Unit,
    onNavigateToSpeechToText: () -> Unit,
    onNavigateToAbout: () -> Unit,
    onNavigateToWebDav: () -> Unit = {},
    onNavigateToClipboardSync: () -> Unit = {},
    onNavigateToRpc: () -> Unit = {},
    onNavigateToPermissions: () -> Unit = {}
) {
    val context = LocalContext.current
    val scrollBehavior = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()
    val settingItemOverrides = RpcUiController.state
        .map { state -> state.filterKeys { it.startsWith(SETTING_ITEM_STATE_PREFIX) } }
        .distinctUntilChanged()
        .collectAsState(initial = emptyMap())
        .value
    val supportIntentConfigJson = RpcUiController.state
        .map { it["settings.support_intent"] }
        .distinctUntilChanged()
        .collectAsState(initial = null)
        .value
    val supportItemDisplay = remember(supportIntentConfigJson) {
        RpcUiController.supportIntentDisplay(supportIntentConfigJson)
    }
    // 设置页条目显隐统一入口：替换原来每个条目各自的 visible 状态变量
    val settingVisibility = remember(context) { SettingsVisibility(context) }
    settingVisibility.remoteOverrides = settingItemOverrides
    // 连点标题：显示内嵌日志窗口，并临时显示默认隐藏的 RPC 设置项
    var titleTapCount by remember { mutableIntStateOf(0) }
    var titleLastTapAt by remember { mutableLongStateOf(0L) }
    
    Scaffold(
        modifier = Modifier.fillMaxSize(),
        containerColor = MaterialTheme.colorScheme.surface,
        topBar = {
            MediumTopAppBar(
                title = {
                    Text(
                        text = "${BuildConfig.APP_NAME}设置",
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable(
                                interactionSource = remember { MutableInteractionSource() },
                                indication = null
                            ) {
                                val now = SystemClock.uptimeMillis()
                                titleTapCount =
                                    if (now - titleLastTapAt <= TITLE_MULTI_TAP_WINDOW_MS) titleTapCount + 1 else 1
                                titleLastTapAt = now
                                if (titleTapCount >= TITLE_MULTI_TAP_COUNT) {
                                    RpcUiController.setState("log.height.override.dp", "360")
                                    settingVisibility.reveal(SettingItems.RPC)
                                    titleTapCount = 0
                                }
                            }
                    )
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    scrolledContainerColor = MaterialTheme.colorScheme.surface,
                    navigationIconContentColor = Color.Unspecified,
                    titleContentColor = MaterialTheme.colorScheme.onSurface,
                    actionIconContentColor = Color.Unspecified
                ),
                scrollBehavior = scrollBehavior
            )
        }
    ) { innerPadding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .nestedScroll(scrollBehavior.nestedScrollConnection)
                .consumeWindowInsets(innerPadding)
                .padding(horizontal = 16.dp)
                .imePadding(),
            contentPadding = innerPadding,
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            item {
                InlineLogView()
                PermissionRequestPanel(onNavigateToPermissions)
            }

            item {
                SettingsSection(title = "输入法", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.Keyboard,
                        title = "启用输入法",
                        subtitle = "在系统设置中启用${BuildConfig.APP_NAME}",
                        onClick = {
                            val intent = Intent(Settings.ACTION_INPUT_METHOD_SETTINGS)
                            context.startActivity(intent)
                        }
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.ToggleOn,
                        title = "选择输入法",
                        subtitle = "将${BuildConfig.APP_CHINESE_NAME}设为当前输入法",
                        onClick = {
                            val imm = context.getSystemService(android.content.Context.INPUT_METHOD_SERVICE) 
                                as InputMethodManager
                            imm.showInputMethodPicker()
                        }
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    var launcherIconHidden by remember { mutableStateOf(SettingsPreferences.isLauncherIconHidden(context)) }
                    SettingsToggleItem(
                        icon = Icons.TwoTone.ToggleOn,
                        title = "隐藏桌面图标",
                        subtitle = "关闭后重新显示启动图标",
                        checked = launcherIconHidden,
                        showArrow = true,
                        onCheckedChange = { hidden ->
                            launcherIconHidden = hidden
                            SettingsPreferences.setLauncherIconHidden(context, hidden)
                            LauncherIconController.setEnabled(context, !hidden)
                        }
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    var testText by remember { mutableStateOf("") }
                    var isFocused by remember { mutableStateOf(false) }
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp)
                    ) {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .onFocusEvent { isFocused = it.isFocused }
                                .clip(RoundedCornerShape(28.dp))
                                .background(
                                    if (isFocused) MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f)
                                    else MaterialTheme.colorScheme.onSurface.copy(alpha = 0.04f)
                                )
                                .padding(horizontal = 16.dp, vertical = 14.dp)
                        ) {
                            BasicTextField(
                                value = testText,
                                onValueChange = { testText = it },
                                modifier = Modifier.fillMaxWidth(),
                                textStyle = MaterialTheme.typography.bodyMedium.copy(
                                    color = MaterialTheme.colorScheme.onSurface
                                ),
                                singleLine = false,
                                maxLines = 3,
                                decorationBox = { innerTextField ->
                                    Box {
                                        if (testText.isEmpty() && !isFocused) {
                                            Text(
                                                "点击此处开始输入测试...",
                                                style = MaterialTheme.typography.bodyMedium,
                                                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f)
                                            )
                                        }
                                        innerTextField()
                                    }
                                }
                            )
                        }
                        if (testText.isNotEmpty()) {
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(top = 8.dp),
                                horizontalArrangement = Arrangement.End
                            ) {
                                TextButton(onClick = { testText = "" }) {
                                    Text(
                                        "清除",
                                        style = MaterialTheme.typography.bodyMedium
                                    )
                                }
                            }
                        }
                    }
                })
            }

            item {
                SettingsSection(title = "外观与交互", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.Palette,
                        title = "主题与定制",
                        subtitle = "自定义外观和样式",
                        onClick = onNavigateToTheme,
                        showArrow = true
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.Vibration,
                        title = "按键效果",
                        subtitle = "按键音效和振动反馈",
                        onClick = onNavigateToKeyEffect,
                        showArrow = true
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.TableChart,
                        title = "布局与显示",
                        subtitle = "候选词显示、键盘布局等",
                        onClick = onNavigateToLayoutDisplay,
                        showArrow = true
                    )
                })
            }

            item {
                SettingsSection(title = "方案与词库", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.KeyboardAlt,
                        title = "输入方案",
                        subtitle = "管理输入方案",
                        onClick = onNavigateToSchema,
                        showArrow = true
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.Ballot,
                        title = "词库管理",
                        subtitle = "管理个人词库和自定义短语",
                        onClick = onNavigateToDictionary,
                        showArrow = true
                    )
                })
            }

            item {
                SettingsSection(title = "扩展", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.Storefront,
                        title = "扩展商店",
                        subtitle = "下载输入方案 / 模型 / 插件",
                        onClick = onNavigateToMarket,
                        showArrow = true
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.Extension,
                        title = "插件管理",
                        subtitle = "管理已安装的插件",
                        onClick = onNavigateToPlugins,
                        showArrow = true
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.Straighten,
                        title = "模型管理",
                        subtitle = "管理本地下载的智能模型",
                        onClick = onNavigateToModelLocal,
                        showArrow = true
                    )
                })
            }

            item {
                SettingsSection(title = "智能", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.AutoAwesome,
                        title = "智能联想",
                        subtitle = "基于 AI 模型的智能联想词预测",
                        onClick = onNavigateToSmartPrediction,
                        showArrow = true
                    )
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    var sttEnabled by remember { mutableStateOf(SettingsPreferences.isSttEnabled(context)) }
                    SettingsToggleItem(
                        icon = Icons.TwoTone.GraphicEq,
                        title = "语音转文本",
                        subtitle = "在线 ASR 服务（需安装语音识别插件）",
                        checked = sttEnabled,
                        showArrow = true,
                        onClick = {
                            if (sttEnabled) onNavigateToSpeechToText()
                        },
                        onCheckedChange = { enabled ->
                            sttEnabled = enabled
                            SettingsPreferences.setSttEnabled(context, enabled)
                        }
                    )
                })
            }

            item {
                SettingsSection(                title = "同步与备份", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.CloudSync,
                        title = "WebDAV 同步",
                        subtitle = "通过 WebDAV 备份和恢复输入方案与配置",
                        onClick = onNavigateToWebDav,
                        showArrow = true
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.Sync,
                        title = "剪贴板同步",
                        subtitle = "通过插件将剪贴板与远端设备双向同步",
                        onClick = onNavigateToClipboardSync,
                        showArrow = true
                    )
                })
            }

            if (settingVisibility.isVisible(SettingItems.RPC)) {
                item {
                    SettingsSection(title = "开发与调试", content = {
                        SettingsItem(
                            icon = Icons.TwoTone.Build,
                            title = "RPC 设置",
                            subtitle = "HTTP 端口、MQTT Broker、Topic 和公钥",
                            onClick = onNavigateToRpc,
                            showArrow = true
                        )
                    })
                }
            }

            item {
                SettingsSection(title = "关于", content = {
                    SettingsItem(
                        icon = Icons.TwoTone.Description,
                        title = supportItemDisplay.title,
                        subtitle = supportItemDisplay.subtitle,
                        onClick = {
                            val result = JSONObject(RpcUiController.launchSupportIntent())
                            if (!result.optBoolean("ok")) {
                                RpcUiController.appendLog(
                                    "[快捷入口] 启动失败: ${result.optString("error")}\n"
                                )
                            }
                        },
                        showArrow = true
                    )
                    if (settingVisibility.isVisible(SettingItems.ABOUT_APP)) {
                        HorizontalDivider(
                            modifier = Modifier.padding(start = 56.dp),
                            thickness = 0.5.dp,
                            color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                        )
                        SettingsItem(
                            icon = Icons.TwoTone.Info,
                            title = "关于${BuildConfig.APP_CHINESE_NAME}",
                            subtitle = "版本信息、开发者、联系方式",
                            onClick = onNavigateToAbout,
                            showArrow = true
                        )
                    }
                    HorizontalDivider(
                        modifier = Modifier.padding(start = 56.dp),
                        thickness = 0.5.dp,
                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
                    )
                    SettingsItem(
                        icon = Icons.TwoTone.Refresh,
                        title = "重启应用",
                        subtitle = "重启后[或第一次打开] 中文输入引擎可能会暂时不可用几十秒。稍等会自动恢复，使需要重启的配置立即生效",
                        onClick = { restartApplication(context) },
                        showArrow = true
                    )
                })
            }
        }
    }
}

private fun restartApplication(context: Context) {
    // 用户开启"隐藏桌面图标"后 LAUNCHER 入口挂在 activity-alias 上且被禁用，
    // getLaunchIntentForPackage() 会返回 null —— 必须回退为显式指向 MainActivity，
    // 否则整个重启流程在这里被静默 return 吞掉，表现为"点了没反应"。
    val launchIntent = (
        context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?: Intent(context, MainActivity::class.java).apply {
                setPackage(context.packageName)
            }
        ).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
    try {
        val restartIntent = PendingIntent.getActivity(
            context,
            1001,
            launchIntent,
            PendingIntent.FLAG_CANCEL_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        alarmManager.set(
            AlarmManager.ELAPSED_REALTIME,
            SystemClock.elapsedRealtime() + 300L,
            restartIntent
        )
        RpcUiController.appendLog("[系统] 正在重启应用……\n")
        android.widget.Toast.makeText(context, "正在重启应用…", android.widget.Toast.LENGTH_SHORT).show()
        Process.killProcess(Process.myPid())
    } catch (e: Throwable) {
        RpcUiController.appendLog("[系统] 重启失败: ${e.message ?: e.javaClass.simpleName}\n")
        android.widget.Toast.makeText(context, "重启失败: ${e.message}", android.widget.Toast.LENGTH_LONG).show()
    }
}

@Composable
private fun PermissionRequestPanel(onNavigateToPermissions: () -> Unit) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    Column(modifier = Modifier.fillMaxWidth().padding(top = 12.dp)) {
        SettingsItem(
            icon = Icons.TwoTone.Security,
            title = "权限设置",
            subtitle = "请授予所有权限",
            onClick = onNavigateToPermissions,
            showArrow = true
        )
        HorizontalDivider(
            thickness = 0.5.dp,
            color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
        )
        SettingsItem(
            icon = Icons.TwoTone.NetworkCheck,
            title = "链路统计",
            subtitle = "stats.get_report() 报告",
            onClick = {
                // 点击瞬间先在日志窗口给即时反馈，避免 Python 取模块/采集期间界面无响应感
                RpcUiController.appendLog("\n[MQTT] 正在采集链路统计报告……\n")
                coroutineScope.launch(Dispatchers.IO) {
                    try {
                        Python.getInstance()
                            .getModule("app")
                            .callAttr("mqtt_stats_report")
                    } catch (e: Throwable) {
                        RpcUiController.appendLog(
                            "[MQTT] 统计报告执行失败: ${e.message ?: e.javaClass.simpleName}\n"
                        )
                    }
                }
            },
            showArrow = false
        )
    }
}

@Composable
private fun InlineLogView() {
    val context = LocalContext.current
    var logText by remember { mutableStateOf("") }
    val scrollState = rememberScrollState()
    val horizontalScrollState = rememberScrollState()
    val coroutineScope = rememberCoroutineScope()
    val uiState = RpcUiController.state.collectAsState().value
    val logVisible = uiState["log.visible"]?.toBooleanStrictOrNull() ?: true
    //日志高度默认值另有一处：SettingsPreferences.getRpcLogHeightDp() 在 SettingsPreferences.kt 中用 1f 作为 SharedPreferences 未存值时的默认值。 优先使用标题四击的临时高度，其次使用 RPC 持久高度；无效或缺失时回退到 1 dp，最大限制为 1000 dp。
    val logHeight = (uiState["log.height.override.dp"] ?: uiState["log.height.dp"])
        ?.toFloatOrNull()
        ?.takeIf { it.isFinite() && it >= 0f }
        ?.coerceAtMost(1000f)
        ?.dp
        ?: 1.dp
    val logBackground = uiState["log.background"]
        ?.let { value -> runCatching { Color(android.graphics.Color.parseColor(value)) }.getOrNull() }
        ?: MaterialTheme.colorScheme.surfaceVariant
    val logToDisk = SettingsPreferences.isRpcLogToDiskEnabled(context)
    val memoryLog = uiState["log.memory"].orEmpty()
    var autoWrap by remember { mutableStateOf(SettingsPreferences.isRpcLogAutoWrapEnabled(context)) }

    if (!logVisible) return

    LaunchedEffect(logToDisk, memoryLog) {
        while (true) {
            val content = withContext(Dispatchers.IO) {
                if (logToDisk) {
                    File(context.filesDir, "rpc.log")
                        .let { file -> runCatching { if (file.isFile) file.readText() else "" }.getOrDefault("") }
                } else {
                    memoryLog
                }
            }
            if (content != logText) {
                logText = content
            }
            delay(1000)
        }
    }

    LaunchedEffect(logText, logHeight) {
        snapshotFlow { scrollState.maxValue }
            .first { it > 0 }
        scrollState.scrollTo(scrollState.maxValue)
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .height(logHeight)
            .clip(RoundedCornerShape(12.dp))
            .background(logBackground)
            .padding(12.dp)
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
        ) {
            SelectionContainer(modifier = Modifier.fillMaxSize()) {
                Text(
                    text = logText.ifEmpty { "暂无日志" },
                    modifier = Modifier
                        .fillMaxSize()
                        .verticalScroll(scrollState)
                        .then(if (autoWrap) Modifier else Modifier.horizontalScroll(horizontalScrollState)),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                    softWrap = autoWrap
                )
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = androidx.compose.ui.Alignment.CenterVertically
        ) {
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Text("自动换行", style = MaterialTheme.typography.labelSmall)
                Switch(
                    checked = autoWrap,
                    onCheckedChange = {
                        autoWrap = it
                        SettingsPreferences.setRpcLogAutoWrapEnabled(context, it)
                    }
                )
            }
            TextButton(
                onClick = {
                    coroutineScope.launch {
                        scrollState.scrollTo(scrollState.maxValue)
                    }
                }
            ) {
                Text("滚到底")
            }
        }
    }
}
