package com.kingzcheung.xime.ui.keyboard

import com.kingzcheung.xime.R

import com.kingzcheung.xime.i18n.tr

import android.content.res.Configuration
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.positionInWindow
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import com.kingzcheung.xime.handwriting.HandwritingCandidate
import com.kingzcheung.xime.keyboard.KeyboardPage
import com.kingzcheung.xime.rime.RimeEngine
import com.kingzcheung.xime.keyboard.MainType
import com.kingzcheung.xime.keyboard.OverlayRoute
import com.kingzcheung.xime.keyboard.PanelType
import com.kingzcheung.xime.keyboard.ToolbarAction
import com.kingzcheung.xime.keyboard.ToolbarButton
import com.kingzcheung.xime.keyboard.ToolbarButtonItem
import com.kingzcheung.xime.keyboard.isBuiltinToolbarButtonAvailable
import com.kingzcheung.xime.keyboard.resolveToolbarButtonItem
import com.kingzcheung.xime.rime.T9InputController
import com.kingzcheung.xime.service.CandidateState
import com.kingzcheung.xime.service.ExpandedCandidatePager
import com.kingzcheung.xime.settings.KeysConfigHelper
import com.kingzcheung.xime.settings.SettingsPreferences
import com.kingzcheung.xime.ui.feedback.FeedbackActivity
import com.kingzcheung.xime.ui.menubar.ClipboardView
import com.kingzcheung.xime.ui.menubar.SchemaListView
import com.kingzcheung.xime.ui.menubar.SchemaSwitchListView
import com.kingzcheung.xime.ui.menubar.ToolbarCustomizeView
import com.kingzcheung.xime.ui.theme.KeyboardThemes
import com.kingzcheung.xime.ui.theme.keyboardBackground
import com.kingzcheung.xime.util.FileLogger
import com.kingzcheung.xime.util.PermissionHelper
import com.kingzcheung.xime.util.UiOverrides
import com.kingzcheung.xime.viewmodel.KeyboardUiState
import com.kingzcheung.xime.viewmodel.KeyboardViewModel
import kotlin.math.abs
import kotlin.math.roundToInt

val LocalStretchFactor = compositionLocalOf { 1f }
val LocalSuppressCursorMove = compositionLocalOf { mutableStateOf(false) }

/**
 * 父层光标手势是否已接管本次横向滑动。
 *
 * 父层激活光标手势后会 consume 横向位移（[KeyboardView] 内 activationThresholdPx），
 * 按键层的拖拽累积随之停止增长，仍可能小于点击取消阈值，导致"光标已移动、按键又打出字"。
 * 按键层据此放弃点击判定：横向滑动已被父层消费，就不该再算作点击。
 */
val LocalCursorGestureActive = compositionLocalOf { mutableStateOf(false) }

@Composable
fun KeyboardView(
    viewModel: KeyboardViewModel,
    state: KeyboardUiState,
    callbacks: KeyboardCallbacks,
    modifier: Modifier = Modifier,
    inlineSuggestions: List<*> = listOf<Any>(),
    onCardPositioned: (left: Int, top: Int, right: Int, bottom: Int) -> Unit = { _: Int, _: Int, _: Int, _: Int -> },
    candidateState: State<CandidateState> = remember { mutableStateOf(CandidateState()) },
    voiceAmplitudeState: State<Float> = remember { mutableFloatStateOf(0f) },
    voiceSpectrumState: State<FloatArray> = remember { mutableStateOf(FloatArray(16)) },
    /**
     * 非按键交互（符号/表情面板、菜单栏、候选栏按钮等）的振动钩子。
     * 按键本身的反馈走 onKeyPressDown 回调（服务层 FeedbackManager），
     * 此处只覆盖没有经过按键回调链路的点击交互，宿主按按键振动同款语义实现。
     */
    onHapticFeedback: (() -> Unit)? = null,
) {
    val isShifted by viewModel.isShifted.collectAsStateWithLifecycle()
    val keyboardState by viewModel.keyboardState.collectAsStateWithLifecycle()
    val page by viewModel.page.collectAsStateWithLifecycle()
    val candidatePageExpanded by viewModel.candidatePageExpanded.collectAsStateWithLifecycle()
    val singleCharFilter by viewModel.singleCharFilter.collectAsStateWithLifecycle()

    // 候选展开页自动收起：编码删空（无候选也无联想）时不留空页。
    // 展开态是候选栏的在位扩展（非 Overlay），删除实时更新页内候选，删空即回到键盘。
    LaunchedEffect(
        candidatePageExpanded,
        candidateState.value.candidates.size,
        candidateState.value.associationCandidates.size
    ) {
        if (candidatePageExpanded &&
            candidateState.value.candidates.isEmpty() &&
            candidateState.value.associationCandidates.isEmpty()
        ) {
            viewModel.setCandidatePageExpanded(false)
        }
    }
    val viewState by viewModel.viewState.collectAsStateWithLifecycle()
    val isLandscape = if (state.isFloatingMode) false
        else LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE

    SideEffect {
        val isHandwriting = page is KeyboardPage.Main && (page as KeyboardPage.Main).type == MainType.HANDWRITING
        val active = isHandwriting || (
            (keyboardState is KeyboardLayoutState.Chinese || keyboardState is KeyboardLayoutState.Stroke || keyboardState is KeyboardLayoutState.T9Pinyin)
            && page is KeyboardPage.Main && (page as KeyboardPage.Main).type == MainType.FULL
        )
        callbacks.onKeyboardModeChange?.invoke(active)
    }

    val context = LocalContext.current
    val t9Controller = remember {
        T9InputController(
            context = context,
            onCompositionRefresh = { composition ->
                callbacks.onT9RefreshComposition?.invoke(composition)
            },
            onRightCommitUndone = callbacks.onT9RightCommitUndone,
            candidateTransform = callbacks.onTCandidateTransform,
        )
    }

    LaunchedEffect(state.inputSessionId) {
        t9Controller.reset()
        FileLogger.i("XimeKeyboard", "InputSessionStarted: isAsciiMode=${state.isAsciiMode}, schemaId=${state.currentSchemaId}, kb=$keyboardState, vs=$viewState, page=$page, restarting=${state.isInputSessionRestarting}")
        // restarting（同一编辑框刷新，如聊天发送消息后 app restartInput）不是新会话：
        // 不清 ascii 记忆——否则"主键盘=中文"的记忆丢失，退出符号/数字面板时
        // targetFor(MAIN) 会 fallback 到引擎的面板态（面板首入默认英文），主键盘被误留英文。
        if (!state.isInputSessionRestarting) {
            viewModel.asciiStateMachine.reset()
        }
        viewModel.dispatch(
            KeyboardDispatchAction.InputSessionStarted(state.isAsciiMode, state.currentSchemaId)
        )
    }

    LaunchedEffect(state.isAsciiMode, state.currentSchemaId) {
        FileLogger.i("XimeKeyboard", "AsciiModeChanged: isAsciiMode=${state.isAsciiMode}, schemaId=${state.currentSchemaId}, kb=$keyboardState, vs=$viewState, page=$page")
        viewModel.dispatch(
            KeyboardDispatchAction.AsciiModeChanged(state.isAsciiMode, state.currentSchemaId)
        )
    }

    // 键盘 ascii 同步点（attach/detach）：键盘上下文切换时，
    // 先保存离开键盘的记忆，再按进入键盘的记忆同步引擎。
    var lastAsciiContext by remember { mutableStateOf<AsciiKeyboardContext?>(null) }
    LaunchedEffect(viewState) {
        val prevContext = lastAsciiContext
        val curContext = viewState.asciiContext()
        lastAsciiContext = curContext
        // curContext 为 null（符号面板）时不参与同步：不进/退该面板都不碰引擎中英态
        if (prevContext == null || curContext == null || prevContext == curContext) return@LaunchedEffect
        viewModel.asciiStateMachine.saveMemory(prevContext, state.isAsciiMode)
        val target = viewModel.asciiStateMachine.targetFor(curContext, state.isAsciiMode)
        if (target != null) {
            FileLogger.i("XimeKeyboard", "ascii sync: ${prevContext.name}(${state.isAsciiMode}) -> ${curContext.name}($target)")
            // 面板上下文同步走 ime_switch_panel（PANEL_SYNC）：切引擎但不写 user.yaml，
            // 临时态不污染用户显式中英选择
            callbacks.onKeyPress("ime_switch_panel", false)
        }
    }

    SideEffect {
        callbacks.onT9RightCandidateWillBeSelected = { pinyin, text, textLength ->
            // 返回 C++ T9RightCommitHandler 的 full_commit 权威标志，
            // 不依赖 RIME 引擎 input（full_commit 后引擎 input 可能残留，判断会失真）
            if (pinyin.isNullOrBlank()) {
                t9Controller.onRightCandidateSelectedByDirectCommit()
            } else {
                t9Controller.onRightCandidateSelected(pinyin, text, textLength)
            }
        }
        callbacks.onT9ForceSendToRime = {
            t9Controller.forceSendToRime()
        }
        callbacks.onFilterT9Candidates = { candidates, comments ->
            Pair(candidates, comments)  // no-op: t9_processor handles filtering
        }
    }

    LaunchedEffect(state.t9ResetSignal) {
        t9Controller.reset()
    }

    LaunchedEffect(keyboardState) {
        FileLogger.i("XimeKeyboard", "keyboardState switched: $keyboardState, vs=$viewState, page=$page, ascii=${state.isAsciiMode}")
    }

    val kbColors = KeysConfigHelper.getKeyboardColors()
    val kbShadow = KeysConfigHelper.getKeyboardShadow()
    val kbKey = KeysConfigHelper.getKeyboardKeyConfig()
    val longToColor: (Long) -> androidx.compose.ui.graphics.Color = { if (it > 0xFFFFFF) androidx.compose.ui.graphics.Color(it) else androidx.compose.ui.graphics.Color(0xFF000000 or it) }
    val keyboardBgColor = KeyboardThemes.getKeyboardBackgroundColor(state.themeId, state.isDarkTheme)
    val keyBgColor = KeyboardThemes.getKeyBgColorOverride(state.themeId, state.isDarkTheme)
        ?: if (state.isDarkTheme) longToColor(kbColors.keyBgColorDark)
        else longToColor(kbColors.keyBgColor)
    val keyTextColor = KeyboardThemes.getKeyTextColorOverride(state.themeId, state.isDarkTheme)
        ?: if (state.isDarkTheme) longToColor(kbColors.keyTextColorDark)
        else longToColor(kbColors.keyTextColor)
    val accentColor = KeyboardThemes.getAccentColor(state.themeId, state.isDarkTheme)
    val themeScheme = KeyboardThemes.getThemeById(state.themeId)
    val themeSpecialKeyColor = KeyboardThemes.getSpecialKeyColor(state.themeId, state.isDarkTheme)
    val specialKeyBgColor = if (state.isDarkTheme) kbColors.specialKeyBgColorDark?.let { longToColor(it) }
        ?: themeSpecialKeyColor
        else kbColors.specialKeyBgColor?.let { longToColor(it) } ?: themeSpecialKeyColor
    val specialKeyTextColor = KeyboardThemes.getSpecialKeyTextColorForBackground(
        specialKeyBgColor, keyTextColor
    )
    val candidateTextColor = KeyboardThemes.getCandidateTextColorOverride(state.themeId, state.isDarkTheme)
        ?: if (state.isDarkTheme) longToColor(kbColors.candidateTextColorDark)
        else longToColor(kbColors.candidateTextColor)
    val candidateSelectedTextColor = KeyboardThemes.getCandidateSelectedTextColorOverride(state.themeId, state.isDarkTheme)
        ?: KeyboardThemes.getCandidateSelectedTextColor(state.themeId, state.isDarkTheme)
    val dividerColor = if (state.isDarkTheme) androidx.compose.ui.graphics.Color(0xFF3C4043) else androidx.compose.ui.graphics.Color(0xFFDADCE0)

    val clipboardTab = (page as? KeyboardPage.Overlay)?.let {
        (it.route as? OverlayRoute.Clipboard)?.tab
    } ?: 0
    val screenW = LocalConfiguration.current.screenWidthDp
    val screenH = LocalConfiguration.current.screenHeightDp
    val portraitScreenWidth = minOf(screenW, screenH)
    val cardWidthDp = FloatingCardGeometry.cardWidthDp(portraitScreenWidth)
    val floatScaleFactor = if (state.isFloatingMode) FloatingCardGeometry.widthFraction(portraitScreenWidth, screenW)
        else FloatingCardGeometry.CARD_SCALE
    val floatFontScale = if (state.isFloatingMode) FloatingCardGeometry.CARD_SCALE else 1f
    // 键盘调节支持左右收窄后，实际渲染宽 ≠ 屏宽（屏幕宽度只作首帧兜底）：
    // 展开候选页的行宽/九键左栏宽度按实测宽计算，收窄后不高估每行容量
    val densityForMeasure = LocalDensity.current
    var measuredWidthDp by remember { mutableIntStateOf(0) }

    val contentModifier = if (state.isFloatingMode) {
        modifier.keyboardBackground(themeScheme.keyboardBackground, state.isDarkTheme, keyboardBgColor)
    } else {
        // 非浮动模式：渐变背景由 XimeInputMethodService 外层 Box 统一绘制（含导航栏区，
        // 保证延伸到屏幕底部时渐变连续），此处不再叠加第二层背景。
        modifier
    }
    FloatingKeyboardContainer(
        isFloatingMode = state.isFloatingMode,
        scaleFactor = floatScaleFactor,
        fontScaleFactor = floatFontScale,
        offsetX = state.floatingOffsetX,
        offsetY = state.floatingOffsetY,
        backgroundColor = keyboardBgColor,
        onDrag = { dx, dy -> callbacks.onFloatingKeyboardDrag?.invoke(dx, dy) },
        onDragEnd = { callbacks.onFloatingKeyboardDragEnd?.invoke() },
        onCardPositioned = onCardPositioned,
    ) {
    Box(modifier = contentModifier) {
        // 当前方案用户自定义候选词集合，供普通和展开候选栏的长按菜单判断；
        // 切换方案，或自动学习 / 删除 / 降频后（UserPhraseManager.phraseRevision
        // 递增）重新从磁盘加载。
        val customPhraseContext = LocalContext.current
        val customPhraseRevision =
            com.kingzcheung.xime.settings.UserPhraseManager.phraseRevision.longValue
        var customPhraseWords by remember(state.currentSchemaId) {
            mutableStateOf(emptySet<String>())
        }
        LaunchedEffect(state.currentSchemaId, customPhraseRevision) {
            customPhraseWords = withContext(Dispatchers.IO) {
                com.kingzcheung.xime.settings.UserPhraseManager
                    .loadWords(customPhraseContext, state.currentSchemaId)
            }
        }
        // 剪贴板历史搜索态：页面为剪贴板覆盖层且搜索开启时，IME 窗口撑满全屏，
        // 候选栏下方用全部剩余高度展示剪贴板面板（搜索结果），下方保留 QWERTY 键盘，
        // 按键走 RIME 引擎可输中文，上屏文本由服务层重定向写入搜索框。
        // 定义在 Box 层：Column 内主键盘区与 Column 外的 Overlay 分支都要访问。
        val clipboardOverlayRoute = (page as? KeyboardPage.Overlay)?.route as? OverlayRoute.Clipboard
        val showClipboardSearchPanel = clipboardOverlayRoute?.tab == 0 && state.clipboardSearchActive
        // 切到快捷发送 tab、关闭覆盖层或跳转其他页面时，自动退出搜索态并恢复窗口高度
        LaunchedEffect(page, state.clipboardSearchActive) {
            if (state.clipboardSearchActive &&
                ((page as? KeyboardPage.Overlay)?.route as? OverlayRoute.Clipboard)?.tab != 0) {
                callbacks.onClipboardSearchToggle?.invoke(false)
            }
        }
        val clipboardPanel: @Composable (Modifier) -> Unit = { panelModifier ->
            ClipboardView(
                clipboardItems = state.clipboardItems,
                quickSendItems = state.quickSendItems,
                selectedTab = clipboardOverlayRoute?.tab ?: 0,
                backgroundColor = keyboardBgColor,
                keyTextColor = keyTextColor,
                keyBgColor = keyBgColor,
                viewModel = viewModel,
                onSelectItem = { text ->
                    // 必须先同步退出搜索态：否则 commitText 重定向会把待粘贴文本吞进搜索框
                    if (state.clipboardSearchActive) {
                        callbacks.onClipboardSearchToggle?.invoke(false)
                    }
                    callbacks.onClipboardSelect?.invoke(text)
                    viewModel.closeOverlay()
                },
                onSplitWords = { text, _ -> viewModel.pushOverlay(OverlayRoute.SplitWords(text)) },
                onBack = { viewModel.closeOverlay() },
                onClipboardTabChange = { viewModel.pushOverlay(OverlayRoute.Clipboard(it)) },
                // 搜索态面板下方紧接自研键盘（键盘自带底部留白），面板内不再重复留白
                bottomPaddingDp = if (state.clipboardSearchActive) 0 else state.keyboardBottomPaddingDp,
                modifier = panelModifier,
                onQuickSendAddClick = {
                    viewModel.closeOverlay()
                    callbacks.onShowQuickSendForm?.invoke()
                },
                onQuickSendEditItem = { id, text ->
                    viewModel.closeOverlay()
                    callbacks.onQuickSendEditItem?.invoke(id, text)
                },
                onPullRemote = callbacks.onClipboardPullRemote,
                pullRemoteAvailable = state.clipboardSyncEnabled,
                searchActive = state.clipboardSearchActive,
                searchQuery = state.clipboardSearchQuery,
                onSearchActiveChange = callbacks.onClipboardSearchToggle,
                onSearchQueryChange = callbacks.onClipboardSearchQueryChange,
            )
        }
        Column(
            modifier = Modifier
                .fillMaxWidth()
        ) {
            var handwritingCandidates by remember { mutableStateOf<List<String>>(emptyList()) }
            var handwritingComments by remember { mutableStateOf<List<String>>(emptyList()) }
            var handwritingClearSignal by remember { mutableIntStateOf(0) }
            var isHandwritingLookup by remember { mutableStateOf(false) }
            // 手写叠写状态真源：tailText=屏上手写会话尾部文本（含固化+活动字）；
            // activeLen=其中活动部分长度（可被识别结果替换/固化/撤销）；
            // lastSegLen=最后一次识别的段长（停顿定型不清零）——点选替换据此定位
            // "最后上屏的字"（停顿后再点候选仍可替换，而非追加）
            var handwritingTail by remember { mutableStateOf("") }
            var handwritingActiveLen by remember { mutableStateOf(0) }
            var handwritingLastSegLen by remember { mutableStateOf(0) }

            // 数据源（展开与否即切换点）：展开态下候选栏与展开页同吃全量列表
            // （expandedCandidates，展开时服务层重新拉取）；非展开态候选栏保持
            // 引擎当前页（"每页候选词数"）
            val expandedDataMode = candidatePageExpanded &&
                candidateState.value.expandedCandidates.isNotEmpty()

            val isHandwritingPage = page is KeyboardPage.Main && (page as KeyboardPage.Main).type == MainType.HANDWRITING
            val showHandwritingCandidates = (isHandwritingPage || isHandwritingLookup) && handwritingCandidates.isNotEmpty()

            val cs = candidateState.value
            // 展开态候选栏数据源：全量列表（筛选态取单字子列表，仍是同源关系）。
            // 每项携带全量原索引：筛选态过滤掉词组后位置索引 ≠ 全局索引，
            // 点选/长按必须经 first 换算回全局索引，与展开页 globalIndices 同口径
            val railExpanded = if (expandedDataMode) {
                if (singleCharFilter) {
                    cs.expandedCandidates.withIndex()
                        .mapNotNull { (i, c) -> if (c.text.length == 1) i to c else null }
                } else {
                    cs.expandedCandidates.withIndex().map { it.index to it.value }
                }
            } else {
                emptyList()
            }
            val candidateBarState = remember(
                cs.candidates, cs.candidateComments, cs.inputText, cs.preeditText, cs.isComposing,
                cs.associationCandidates, cs.pendingEnglishText, cs.isShowingRecentClipboard, cs.hasNextPage,
                cs.demotedPhraseCount, state.isCalculatorMode, handwritingCandidates, handwritingComments,
                showHandwritingCandidates,
            ) {
                if (showHandwritingCandidates) {
                    CandidateBarState.AssociationOnly(
                        candidates = handwritingCandidates,
                        comments = handwritingComments,
                        highlightIndex = 0,
                    )
                } else {
                    CandidateBarState.from(
                        candidates = if (railExpanded.isNotEmpty()) railExpanded.map { it.second.text } else cs.candidates,
                        candidateComments = if (railExpanded.isNotEmpty()) railExpanded.map { it.second.comment } else cs.candidateComments,
                        inputText = cs.inputText,
                        preeditText = cs.preeditText,
                        isComposing = cs.isComposing,
                        associationCandidates = if (cs.pendingEnglishText.isNotEmpty() && cs.englishReplaceSupported) {
                            listOf(cs.pendingEnglishText) + cs.associationCandidates
                        } else {
                            cs.associationCandidates
                        },
                        isShowingRecentClipboard = cs.isShowingRecentClipboard,
                        hasNextPage = cs.hasNextPage,
                        isCalculatorActive = state.isCalculatorMode,
                        demotedCandidateCount = cs.demotedPhraseCount,
                    )
                }
            }

            if (state.showQuickSendForm) {
                QuickSendFormArea(
                    backgroundColor = Color.Transparent,
                    textColor = keyTextColor,
                    accentColor = accentColor,
                    isFocused = state.quickSendFormFocused,
                    initialText = state.quickSendEditingItemText,
                    initialCode = state.quickSendEditingItemCode,
                    cardBgColor = keyBgColor,
                    editingItemId = state.quickSendEditingItemId,
                    onClose = { text: String, code: String ->
                        android.util.Log.d("QuickSendForm", "onClose: textLen=${text.length}, editingId=${state.quickSendEditingItemId}, showForm=${state.showQuickSendForm}")
                        if (text.isNotBlank()) {
                            val editingId = state.quickSendEditingItemId
                            if (editingId != null) {
                                viewModel.updateQuickSendItem(editingId, text, code)
                            } else {
                                viewModel.addQuickSendText(text, code)
                            }
                        }
                        callbacks.onHideQuickSendForm?.invoke()
                    },
                    onFocusChange = { focused: Boolean ->
                        callbacks.onQuickSendFormFocusChange?.invoke(focused)
                    },
                    onCodeFocusChange = { focused: Boolean ->
                        callbacks.onQuickSendCodeFocusChange?.invoke(focused)
                    },
                )
            }

            // 剪贴板搜索态：面板占满键盘上方全部空间，候选栏放到面板下面紧贴按键区。
            // 否则候选栏位于 IME 窗口最顶部（搜索框上方、屏幕顶边处），中文候选字看不见。
            if (showClipboardSearchPanel) {
                val searchPanelHeightDp = state.clipboardSearchExtraDp.coerceAtLeast(200)
                clipboardPanel(Modifier.fillMaxWidth().height(searchPanelHeightDp.dp))
            }

            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .onGloballyPositioned { coords ->
                        // 键盘区（候选栏+按键）顶部绝对位置：搜索面板在其上方展开时此位置
                        // 不变，上报给服务层作为稳定的 contentTopInsets，防止宿主被重排。
                        callbacks.onKeyboardTopPositioned
                            ?.invoke(coords.positionInWindow().y.toInt())
                    }
            ) {
            CandidateBar(
                state = candidateBarState,
                page = page,
                candidatePageExpanded = candidatePageExpanded,
                isFloatingMode = state.isFloatingMode,
                // 候选栏图片候选的缩略图文件（不存在返回 null → 渲染占位图标）
                clipboardImageFileOf = { item -> viewModel.clipboardManager.imageFileOf(item) },
                isVoiceSticky = state.voiceSticky,
                voiceAmplitude = voiceAmplitudeState.value,
                voiceSpectrum = voiceSpectrumState.value,
                voiceRecognitionState = state.voiceRecognitionState,
                voicePluginName = state.voicePluginName,
                customPhraseWords = customPhraseWords,
                toolbarActions = state.toolbarButtons.mapNotNull { id ->
                    val item = resolveToolbarButtonItem(id, state.toolbarPluginButtons) ?: return@mapNotNull null
                    // 承载区受限（独立 IME 面板）：浮动键盘没有意义也放不下，隐藏入口
                    if (state.isHostConstrained &&
                        item is ToolbarButtonItem.Builtin &&
                        item.button == ToolbarButton.FLOAT
                    ) {
                        return@mapNotNull null
                    }
                    // 与定制列表同一口径（关掉语音转文本 / 未装手写模型则不显示）
                    if (item is ToolbarButtonItem.Builtin &&
                        !isBuiltinToolbarButtonAvailable(
                            item.button,
                            isSttEnabled = state.isSttEnabled,
                            hasHandwritingModel = {
                                com.kingzcheung.xime.handwriting.HandwritingEngine.hasModel(toolbarContext)
                            },
                        )
                    ) {
                        return@mapNotNull null
                    }
                    val onClick: () -> Unit = when (item) {
                        is ToolbarButtonItem.Builtin -> when (item.button) {
                            ToolbarButton.EMOJI -> ({ viewModel.showOverlay(OverlayRoute.Emoji) })
                            ToolbarButton.CLIPBOARD -> ({ viewModel.showOverlay(OverlayRoute.Clipboard(0)) })
                            ToolbarButton.SCHEMA -> ({ viewModel.showOverlay(OverlayRoute.SchemaList) })
                            ToolbarButton.SWITCHES -> ({ viewModel.showOverlay(OverlayRoute.SchemaSwitches) })
                            ToolbarButton.QUICK_PHRASE -> ({ viewModel.showOverlay(OverlayRoute.Clipboard(1)) })
                            ToolbarButton.SYMBOL -> ({ viewModel.showOverlay(OverlayRoute.Symbol) })
                            ToolbarButton.SELECT_ALL -> ({ callbacks.onToolbarEditingAction?.invoke("select_all") })
                            ToolbarButton.COPY -> ({ callbacks.onToolbarEditingAction?.invoke("copy") })
                            ToolbarButton.PASTE -> ({ callbacks.onToolbarEditingAction?.invoke("paste") })
                            ToolbarButton.HOME -> ({ callbacks.onToolbarEditingAction?.invoke("home") })
                            ToolbarButton.END -> ({ callbacks.onToolbarEditingAction?.invoke("end") })
                            ToolbarButton.FLOAT -> ({ callbacks.onFloatingModeChange?.invoke(!state.isFloatingMode) })
                            ToolbarButton.HANDWRITING_LOOKUP -> ({ isHandwritingLookup = !isHandwritingLookup })
                            ToolbarButton.EDIT -> ({ viewModel.showOverlay(OverlayRoute.Edit) })
                            ToolbarButton.VOICE -> ({
                                if (PermissionHelper.hasRecordAudioPermission(toolbarContext)) {
                                    callbacks.onVoiceStickyToggle?.invoke()
                                } else {
                                    android.widget.Toast.makeText(toolbarContext, tr(R.string.s_xuyaomaikefengquanxiancainengs_3b6e4), android.widget.Toast.LENGTH_SHORT).show()
                                    PermissionHelper.requestRecordAudioPermission(toolbarContext)
                                }
                            })
                        }
                        is ToolbarButtonItem.Plugin -> ({
                            if (item.action == "open_panel") {
                                callbacks.onOpenToolPanel?.invoke(item.pluginId)
                            }
                        })
                    }
                    ToolbarAction(item) {
                        onHapticFeedback?.invoke()
                        onClick()
                    }
                },
                visuals = CandidateBarVisuals(
                    backgroundColor = Color.Transparent,
                    textColor = candidateTextColor,
                    dividerColor = dividerColor,
                    accentColor = accentColor,
                    selectedTextColor = candidateSelectedTextColor,
                    isDarkTheme = state.isDarkTheme,
                    keyBgColor = keyBgColor
                ),
                callbacks = CandidateBarCallbacks(
                    onCandidateSelect = { index, candidate ->
                        if (showHandwritingCandidates && index in handwritingCandidates.indices) {
                            callbacks.onCommitText?.invoke(candidate)
                            handwritingCandidates = emptyList()
                            handwritingComments = emptyList()
                            handwritingClearSignal++
                        } else {
                            callbacks.onCandidateSelect(index, candidate)
                        }
                    },
                    onCandidateLongPress = { index ->
                        val railEntry = if (expandedDataMode) railExpanded.getOrNull(index) else null
                        val word = railEntry?.second?.text
                            ?: candidateState.value.candidates.getOrNull(index)
                        if (!word.isNullOrEmpty()) {
                            deletePending = DeletePendingWord(word) {
                                if (railEntry != null) {
                                    callbacks.onGlobalCandidateDelete?.invoke(railEntry.first)
                                } else {
                                    callbacks.onCandidateDelete?.invoke(index)
                                }
                            }
                        }
                    },
                    onClearAssociation = {
                        onHapticFeedback?.invoke()
                        if (showHandwritingCandidates) {
                            handwritingCandidates = emptyList()
                            handwritingComments = emptyList()
                            handwritingClearSignal++
                        } else {
                            callbacks.onClearAssociation?.invoke()
                        }
                    },
                    onLogoClick = { viewModel.showOverlay(OverlayRoute.Menu) },
                    onBack = {
                        onHapticFeedback?.invoke()
                        if (showHandwritingCandidates) {
                            handwritingCandidates = emptyList()
                            handwritingComments = emptyList()
                            handwritingClearSignal++
                        } else {
                            when (page) {
                                is KeyboardPage.Overlay -> viewModel.closeOverlay()
                                is KeyboardPage.Panel -> viewModel.exitPanel()
                                is KeyboardPage.Main -> viewModel.setCandidatePageExpanded(false)
                            }
                        }
                    },
                    onHideKeyboard = {
                        onHapticFeedback?.invoke()
                        callbacks.onHideKeyboard?.invoke()
                        viewModel.resetKeyboard(state.isAsciiMode, state.currentSchemaId)
                    },
                    onShowMoreCandidates = {
                        onHapticFeedback?.invoke()
                        viewModel.setCandidatePageExpanded(true)
                        // 拉取跨页全量候选（展开页数据源，含首次展开）
                        callbacks.onRequestExpandedCandidates?.invoke()
                    },
                    onInputTextClick = {
                        val rawInput = candidateState.value.inputText
                        if (rawInput.isNotEmpty()) {
                            if (state.clipboardSearchActive) {
                                // 搜索态：点原始编码把字母并入搜索框（不走剪贴板选中/复制副作用）
                                callbacks.onClipboardSearchQueryChange?.invoke(
                                    state.clipboardSearchQuery + rawInput
                                )
                                callbacks.onKeyPress?.invoke("clear_composition", false)
                            } else {
                                callbacks.onClipboardSelect?.invoke(rawInput)
                            }
                        }
                    },
                    onAssociationSelect = { index, candidate ->
                        val handwritingIndex = handwritingCandidates.indexOf(candidate)
                        if (showHandwritingCandidates && handwritingIndex >= 0) {
                            val ch = handwritingCandidates[handwritingIndex]
                            callbacks.onCommitText?.invoke(ch)
                            handwritingCandidates = emptyList()
                            handwritingComments = emptyList()
                            handwritingClearSignal++
                        } else {
                            callbacks.onAssociationSelect?.invoke(index, candidate)
                        }
                    },
                    onCustomPhraseAction = { word, action ->
                        // 服务层写文件；UserPhraseManager 写盘成功后递增 phraseRevision，
                        // 本组件观察到变化会自动重新加载词条集合，无需在此手动刷新。
                        callbacks.onCustomPhraseAction?.invoke(word, action)
                    },
                    onUiDiagnosticEvent = callbacks.onUiDiagnosticEvent,
                ),
                inlineSuggestions = inlineSuggestions,
            )
            }

            val isMainKeyboard = page is KeyboardPage.Main || showClipboardSearchPanel
            if (isMainKeyboard) {
                val mainType = (page as? KeyboardPage.Main)?.type ?: MainType.FULL
                when (mainType) {
                    MainType.FULL -> {
                        val currentOnCursorMove = rememberUpdatedState(callbacks.onCursorMove)
                        val suppressCursorMove = remember { mutableStateOf(false) }
                        val cursorGestureActive = remember { mutableStateOf(false) }
                        val cursorMod = if (callbacks.onCursorMove != null) {
                            Modifier.pointerInput(Unit) {
                                val stepThresholdPx = 25.dp.toPx()
                                val activationThresholdPx = 60.dp.toPx()
                                awaitEachGesture {
                                    suppressCursorMove.value = false
                                    cursorGestureActive.value = false
                                    val down = awaitFirstDown(requireUnconsumed = false)
                                    var isCursorGesture = false
                                    var lastSteps = 0
                                    var activationAnchorX = down.position.x

                                    // 手势结束即清标志，避免跨手势残留（26 键已改按本次手势峰值判定）
                                    try {
                                        do {
                                            val event = awaitPointerEvent()
                                            val change = event.changes.firstOrNull { it.id == down.id } ?: break
                                            val dx = change.position.x - down.position.x
                                            val dy = change.position.y - down.position.y

                                            if (!change.pressed) {
                                                if (isCursorGesture) {
                                                    event.changes.forEach { it.consume() }
                                                }
                                                break
                                            }
                                            if (suppressCursorMove.value) break
                                            if (abs(dx) > abs(dy) * 4f) {

                                                if (!isCursorGesture && abs(dx) > activationThresholdPx) {
                                                    isCursorGesture = true
                                                    activationAnchorX = change.position.x
                                                    cursorGestureActive.value = true
                                                }

                                                if (isCursorGesture) {
                                                    event.changes.forEach { it.consume() }
                                                    val dxFromAnchor = change.position.x - activationAnchorX
                                                    val steps = (dxFromAnchor / stepThresholdPx).toInt()
                                                    if (steps != lastSteps) {
                                                        val delta = steps - lastSteps
                                                        currentOnCursorMove.value?.invoke(delta)
                                                        lastSteps = steps
                                                    }
                                                }
                                            }
                                        } while (true)
                                    } finally {
                                        cursorGestureActive.value = false
                                    }
                                }
                            }
                        } else {
                            Modifier
                        }

                        val context = LocalContext.current

                        var modeChangeTarget: KeyboardLayoutAction by remember {
                            mutableStateOf(
                                if (SettingsPreferences.getModeChangeTargetIsNumber(context))
                                    KeyboardLayoutAction.SwitchToNumber
                                else
                                    KeyboardLayoutAction.SwitchToCommonSymbol
                            )
                        }

                        val fullScreenOnKeyPress: (String) -> Unit = { key ->
                            when (key) {
                                "shift" -> viewModel.toggleShift()
                                "shift_single" -> viewModel.singleTapShift()
                                "shift_caps" -> viewModel.doubleTapShift()
                                "mode_change" -> {
                                    callbacks.onCommitCandidateBeforeModeChange?.invoke()
                                    viewModel.setKeyboardState(keyboardState.transition(
                                        modeChangeTarget, state.isAsciiMode
                                    ))
                                    callbacks.onKeyPress("clear_composition", false)
                                }
                                "mode_change_symbol" -> viewModel.showOverlay(OverlayRoute.Symbol)
                                "mode_change_number" -> {
                                    callbacks.onCommitCandidateBeforeModeChange?.invoke()
                                    modeChangeTarget = KeyboardLayoutAction.SwitchToNumber
                                    SettingsPreferences.setModeChangeTargetIsNumber(context, true)
                                    viewModel.setKeyboardState(KeyboardLayoutState.Number)
                                }
                                "mode_change_common_symbol" -> {
                                    callbacks.onCommitCandidateBeforeModeChange?.invoke()
                                    modeChangeTarget = KeyboardLayoutAction.SwitchToCommonSymbol
                                    SettingsPreferences.setModeChangeTargetIsNumber(context, false)
                                    viewModel.setKeyboardState(keyboardState.transition(
                                        KeyboardLayoutAction.SwitchToCommonSymbol, state.isAsciiMode
                                    ))
                                }
                                "emoji" -> viewModel.showOverlay(OverlayRoute.Emoji)
                                // 与 emoji 同档：command: symbol / 自定义键发 "symbol" 时打开符号面板
                                "symbol" -> viewModel.showOverlay(OverlayRoute.Symbol)
                                else -> {
                                    callbacks.onKeyPress(key, isShifted)
                                    viewModel.onCharacterTyped()
                                }
                            }
                        }
                        val numberOnKeyPress: (String) -> Unit = { key ->
                            when (key) {
                                // 面板上的 shift/caps 键（caps 功能键 id 发 "shift_caps"）：
                                // 接入 26 键同款 shift 状态机，避免字符串落到引擎被当成编码
                                "shift", "shift_single", "shift_caps" -> viewModel.consumeShiftKey(key)
                                "abc" -> {
                                    callbacks.onKeyPress("abc", false)
                                    viewModel.returnToMain(state.isAsciiMode, state.currentSchemaId)
                                }
                                "symbol" -> {
                                    viewModel.showOverlay(OverlayRoute.Symbol)
                                }
                                "emoji" -> {
                                    viewModel.showOverlay(OverlayRoute.Emoji)
                                }
                                else -> callbacks.onKeyPress(key, false)
                            }
                        }
                        val symbolOnKeyPress: (String) -> Unit = { key ->
                            when (key) {
                                "abc" -> viewModel.returnToMain(state.isAsciiMode, state.currentSchemaId)
                                "?123" -> {
                                    callbacks.onCommitCandidateBeforeModeChange?.invoke()
                                    viewModel.setKeyboardState(keyboardState.transition(
                                        KeyboardLayoutAction.SwitchToNumber, state.isAsciiMode
                                    ))
                                    callbacks.onKeyPress("clear_composition", false)
                                }
                                else -> callbacks.onKeyPress(key, false)
                            }
                        }
                        val commonSymbolOnKeyPress: (String) -> Unit = { key ->
                            when (key) {
                                "shift", "shift_single", "shift_caps" -> viewModel.consumeShiftKey(key)
                                // 返回主键盘：符号/数字页不碰引擎中英态，布局跟随引擎当前状态
                                "abc" -> viewModel.returnToMain(state.isAsciiMode, state.currentSchemaId)
                                "number" -> {
                                    viewModel.setKeyboardState(keyboardState.transition(
                                        KeyboardLayoutAction.SwitchToNumber, state.isAsciiMode
                                    ))
                                }
                                "symbol" -> {
                                    viewModel.showOverlay(OverlayRoute.Symbol)
                                }
                                "emoji" -> {
                                    viewModel.showOverlay(OverlayRoute.Emoji)
                                }
                                else -> callbacks.onKeyPress(key, false)
                            }
                        }
                        val strokeOnKeyPress: (String) -> Unit = { key ->
                            when (key) {
                                "abc" -> viewModel.setKeyboardState(keyboardState.transition(
                                    KeyboardLayoutAction.SwitchToFull, state.isAsciiMode
                                ))
                                "number" -> viewModel.setKeyboardState(keyboardState.transition(
                                    KeyboardLayoutAction.SwitchToNumber, state.isAsciiMode
                                ))
                                "symbol" -> viewModel.showOverlay(OverlayRoute.Symbol)
                                "emoji" -> viewModel.showOverlay(OverlayRoute.Emoji)
                                else -> callbacks.onKeyPress(key, false)
                            }
                        }
                        val t9OnKeyPress: (String) -> Unit = { key ->
                            when (key) {
                                "abc" -> viewModel.setKeyboardState(
                                    initialKeyboardLayoutState(state.isAsciiMode, state.currentSchemaId)
                                )
                                "number" -> {
                                    callbacks.onT9SwitchAway?.invoke()
                                    viewModel.setKeyboardState(keyboardState.transition(
                                        KeyboardLayoutAction.SwitchToNumber, state.isAsciiMode
                                    ))
                                }
                                "symbol" -> viewModel.showOverlay(OverlayRoute.Symbol)
                                "emoji" -> viewModel.showOverlay(OverlayRoute.Emoji)
                                "ime_switch" -> {
                                    callbacks.onT9SwitchAway?.invoke()
                                    callbacks.onKeyPress(key, false)
                                }
                                else -> callbacks.onKeyPress(key, false)
                            }
                        }
                        val currentOnKeyPress = when (keyboardState) {
                            is KeyboardLayoutState.Chinese,
                            is KeyboardLayoutState.English -> fullScreenOnKeyPress
                            is KeyboardLayoutState.Number -> numberOnKeyPress
                            is KeyboardLayoutState.CommonSymbol -> commonSymbolOnKeyPress
                            is KeyboardLayoutState.Stroke -> strokeOnKeyPress
                            is KeyboardLayoutState.T9Pinyin -> t9OnKeyPress
                            is KeyboardLayoutState.Symbol -> symbolOnKeyPress
                        }
                        CompositionLocalProvider(
                            LocalSuppressCursorMove provides suppressCursorMove,
                            LocalCursorGestureActive provides cursorGestureActive,
                        ) {
                            KeyboardLayoutScreen(
                                keyboardState = keyboardState,
                                uiState = state,
                                candidateState = candidateState,
                                viewModel = viewModel,
                                callbacks = callbacks,
                                onKeyPress = currentOnKeyPress,
                                modifier = Modifier.weight(1f).then(cursorMod),
                                isHandwritingLookup = isHandwritingLookup,
                                onHandwritingCandidates = { candidates ->
                                    val chars = candidates.map { it.char }
                                    handwritingCandidates = chars
                                    handwritingComments = chars.map { RimeEngine.getInstance().lookupText(it) }
                                },
                                onHandwritingButtonFeedback = { key -> callbacks.onKeyPressDown?.invoke(key) },
                                handwritingClearSignal = handwritingClearSignal,
                                onHandwritingLookupExit = { isHandwritingLookup = false },
                                t9Controller = t9Controller,
                            )
                            if (state.keyboardBottomPaddingDp > 0) {
                                Spacer(modifier = Modifier.height(state.keyboardBottomPaddingDp.dp))
                            }
                        }
                    }

                    MainType.HANDWRITING -> {
                        HandwritingKeyboardLayout(
                            onKeyPress = { key ->
                                when (key) {
                                    "delete" -> {
                                        when {
                                            handwritingActiveLen > 0 -> {
                                                // 撤销当前字：删屏上活动区文本，清笔画重写
                                                callbacks.onDeleteText?.invoke(handwritingActiveLen)
                                                handwritingTail = handwritingTail.dropLast(handwritingActiveLen)
                                                handwritingActiveLen = 0
                                                handwritingLastSegLen = 0
                                                handwritingCandidates = emptyList()
                                                handwritingComments = emptyList()
                                                handwritingClearSignal++
                                            }
                                            handwritingTail.isNotEmpty() -> {
                                                // 无活动字：退格删固化尾字，tail 乐观修剪（下次替换校验兜底）。
                                                // 候选栏同步失效（tail 已变，点选替换无意义）
                                                callbacks.onDeleteText?.invoke(1)
                                                handwritingTail = handwritingTail.dropLast(1)
                                                handwritingLastSegLen = 0
                                                handwritingCandidates = emptyList()
                                                handwritingComments = emptyList()
                                            }
                                            else -> callbacks.onKeyPress("delete", false)
                                        }
                                    }
                                    "symbol" -> viewModel.enterPanel(PanelType.COMMON_SYMBOL)
                                    "number" -> viewModel.enterPanel(PanelType.NUMBER)
                                    "ime_switch" -> {
                                        // 离开手写页即卸载手写模型（回到手写页时布局重建重载）
                                        com.kingzcheung.xime.handwriting.HandwritingEngine.release()
                                        viewModel.switchMain(MainType.FULL)
                                        callbacks.onKeyPress("ime_switch", false)
                                    }
                                    "space" -> {
                                        // 当前字已自动上屏：空格=固化 + 上屏空格（全量 commitText 触发联想）。
                                        // 选择期结束：清候选栏
                                        handwritingActiveLen = 0
                                        handwritingLastSegLen = 0
                                        handwritingCandidates = emptyList()
                                        handwritingComments = emptyList()
                                        callbacks.onCommitText?.invoke(" ")
                                    }
                                    "enter" -> {
                                        // 换行定稿：选择期结束
                                        handwritingActiveLen = 0
                                        handwritingLastSegLen = 0
                                        handwritingCandidates = emptyList()
                                        handwritingComments = emptyList()
                                        callbacks.onKeyPress("enter", false)
                                    }
                                    else -> {
                                        // 标点等直接上屏：固化活动字 + 选择期结束
                                        handwritingActiveLen = 0
                                        handwritingLastSegLen = 0
                                        handwritingCandidates = emptyList()
                                        handwritingComments = emptyList()
                                        callbacks.onCommitText?.invoke(key)
                                    }
                                }
                            },
                            onRecognition = { segments ->
                                val segText = segments.mapNotNull { seg ->
                                    seg.candidates.firstOrNull()?.char
                                }.joinToString("")
                                if (segText.isNotEmpty()) {
                                    // 替换式上屏：活动区整体重写为最新识别结果（边写边上屏）。
                                    // 校验失败（光标漂移）时重置尾部状态，后续识别以追加模式重建
                                    val newTail = handwritingTail.dropLast(handwritingActiveLen) + segText
                                    val ok = callbacks.onHandwritingAutoCommit?.invoke(newTail, handwritingTail) ?: false
                                    handwritingTail = if (ok || handwritingTail.isEmpty()) newTail else ""
                                    handwritingActiveLen = segText.length
                                    handwritingLastSegLen = segText.length
                                    handwritingCandidates = segments.last().candidates.map { it.char }
                                    handwritingComments = emptyList()
                                }
                            },
                            onSegmentSettled = { ch ->
                                // 叠写满上限：最早段固化（屏上不动，退出活动区）
                                handwritingActiveLen = (handwritingActiveLen - ch.length).coerceAtLeast(0)
                            },
                            onUndoActive = {
                                if (handwritingActiveLen > 0) {
                                    callbacks.onDeleteText?.invoke(handwritingActiveLen)
                                    handwritingTail = handwritingTail.dropLast(handwritingActiveLen)
                                    handwritingActiveLen = 0
                                    handwritingCandidates = emptyList()
                                    handwritingComments = emptyList()
                                    handwritingClearSignal++
                                }
                            },
                            onButtonFeedback = { key ->
                                callbacks.onKeyPressDown?.invoke(key)
                            },
                            clearSignal = handwritingClearSignal,
                            keyBackgroundColor = keyBgColor,
                            keyTextColor = keyTextColor,
                            specialKeyBackgroundColor = specialKeyBgColor,
                            specialKeyTextColor = specialKeyTextColor,
                            modifier = Modifier.weight(1f),
                        )
                        if (state.keyboardBottomPaddingDp > 0) {
                            Spacer(modifier = Modifier.height(state.keyboardBottomPaddingDp.dp))
                        }
                    }

                    MainType.STROKE -> {
                        // Stroke is handled via keyboardState within FULL for now
                    }

                    MainType.VOICE -> {
                        VoiceKeyboardLayout(
                            keyBackgroundColor = keyBgColor,
                            keyTextColor = keyTextColor,
                            specialKeyBackgroundColor = specialKeyBgColor,
                            keyboardBackgroundColor = keyboardBgColor,
                            modifier = Modifier.weight(1f),
                            isDarkTheme = state.isDarkTheme,
                            themeId = state.themeId,
                            bottomActive = state.voiceBottomActive,
                            leftActive = state.voiceLeftActive,
                            rightActive = state.voiceRightActive,
                            muteActive = state.voiceMuteActive,
                            muteOthersEnabled = state.sttMuteOthers,
                            pluginName = state.voicePluginName,
                            recognitionState = state.voiceRecognitionState,
                            recognizedText = state.voiceRecognizedText,
                            amplitude = voiceAmplitudeState.value,
                            spectrum = voiceSpectrumState.value
                        )
                    }
                }
            }

            val isPanelKeyboard = page is KeyboardPage.Panel
            if (isPanelKeyboard) {
                val panelType = (page as KeyboardPage.Panel).type
                // 面板圆角与平板 26 键共用同一条规则（yaml 按键盘覆盖优先 → 设备档默认，
                // 平板用大键面档）：xime.yaml 写 keyboard.key.number/symbol.corner_radius 可单独调
                val numberPanelRadius = keyCornerRadiusFor(kbKey, "number", isTablet())
                val symbolPanelRadius = keyCornerRadiusFor(kbKey, "symbol", isTablet())
                when (panelType) {
                    PanelType.NUMBER -> PanelKeyboardLayout(
                        section = "number",
                        onKeyPress = { key ->
                            when (key) {
                                "shift", "shift_single", "shift_caps" -> viewModel.consumeShiftKey(key)
                                "abc" -> viewModel.exitPanel()
                                "symbol" -> {
                                    viewModel.showOverlay(OverlayRoute.Symbol)
                                }
                                "emoji" -> {
                                    viewModel.showOverlay(OverlayRoute.Emoji)
                                }
                                else -> callbacks.onKeyPress(key, false)
                            }
                        },
                        viewModel = viewModel,
                        callbacks = callbacks,
                        uiState = state,
                        isAsciiMode = state.isAsciiMode,
                        keyBackgroundColor = keyBgColor,
                        keyTextColor = keyTextColor,
                        specialKeyBackgroundColor = specialKeyBgColor,
                        bubbleBackgroundColor = themeSpecialKeyColor,
                        keyboardBackgroundColor = keyboardBgColor,
                        shadowEnabled = kbShadow.enabled,
                        shadowElevation = kbShadow.elevation.dp,
                        // 平板参数档：阴影圆角不小于键圆角（与主键盘同规则）
                        shadowShapeRadius = maxOf(kbShadow.shapeRadius.dp, numberPanelRadius),
                        keyCornerRadius = numberPanelRadius,
                        keySpacingX = kbKey.spacingFor("number").first?.dp ?: if (isTablet()) 4.dp else null,
                        keySpacingY = kbKey.spacingFor("number").second?.dp ?: if (isTablet()) 6.dp else null,
                        onKeyPressDown = callbacks.onKeyPressDown,
                        isFloatingMode = state.isFloatingMode,
                        specialKeyTextColor = specialKeyTextColor,
                        landscapeSplit = SettingsPreferences.isLandscapeSplitKeyboardEnabled(LocalContext.current),
                        modifier = Modifier.weight(1f).fillMaxWidth()
                    )

                    PanelType.COMMON_SYMBOL -> PanelKeyboardLayout(
                        section = "common_symbols",
                        onKeyPress = { key ->
                            when (key) {
                                "shift", "shift_single", "shift_caps" -> viewModel.consumeShiftKey(key)
                                "abc" -> {
                                    viewModel.exitPanel()
                                }
                                "number" -> {
                                    viewModel.enterPanel(PanelType.NUMBER)
                                }
                                "symbol" -> {
                                    viewModel.showOverlay(OverlayRoute.Symbol)
                                }
                                "emoji" -> {
                                    viewModel.showOverlay(OverlayRoute.Emoji)
                                }
                                else -> callbacks.onKeyPress(key, false)
                            }
                        },
                        viewModel = viewModel,
                        callbacks = callbacks,
                        uiState = state,
                        isAsciiMode = state.isAsciiMode,
                        keyBackgroundColor = keyBgColor,
                        keyTextColor = keyTextColor,
                        specialKeyBackgroundColor = specialKeyBgColor,
                        bubbleBackgroundColor = themeSpecialKeyColor,
                        keyboardBackgroundColor = keyboardBgColor,
                        shadowEnabled = kbShadow.enabled,
                        shadowElevation = kbShadow.elevation.dp,
                        shadowShapeRadius = maxOf(kbShadow.shapeRadius.dp, symbolPanelRadius),
                        keyCornerRadius = symbolPanelRadius,
                        keySpacingX = kbKey.spacingFor("symbol").first?.dp ?: if (isTablet()) 4.dp else null,
                        keySpacingY = kbKey.spacingFor("symbol").second?.dp ?: if (isTablet()) 6.dp else null,
                        onKeyPressDown = callbacks.onKeyPressDown,
                        isFloatingMode = state.isFloatingMode,
                        specialKeyTextColor = specialKeyTextColor,
                        landscapeSplit = SettingsPreferences.isLandscapeSplitKeyboardEnabled(LocalContext.current),
                        modifier = Modifier.weight(1f).fillMaxWidth()
                    )

                }
            }
            } // candidatePageExpanded else

            val configuration = LocalConfiguration.current
            val isLandscapeBottom = configuration.orientation == Configuration.ORIENTATION_LANDSCAPE
        }

        // 语音状态悬浮标签：半透明覆盖在输入工具栏上方，只显示文本状态，
        // 不拦截触摸（无 pointerInput/clickable，事件穿透到工具栏与语音按钮区）。
        // 长按空格时键盘切到 MainType.VOICE 语音页（频谱+松开结束），标签同样
        // 浮在该页工具栏上方；非语音态状态为 IDLE，标签自动隐藏，不影响其他页面。
        // 系统悬浮窗生效时（开关开+已授权）状态由键盘外悬浮卡片展示，键盘内胶囊让位避免重复
        if (page is KeyboardPage.Main && !state.floatingVoiceLabel) {
            VoiceStatusLabel(
                recognitionState = state.voiceRecognitionState,
                recognizedText = state.voiceRecognizedText,
                modifier = Modifier.align(Alignment.TopCenter)
            )
        }

        if (state.isDeploying) {
            val isError = state.deploymentMessage.contains("超时") || state.deploymentMessage.contains("失败")
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .fillMaxHeight()
                    .background(keyboardBgColor.copy(alpha = 0.9f))
                    .clickable(enabled = isError && callbacks.onDismissDeploying != null) {
                        callbacks.onDismissDeploying?.invoke()
                    },
                contentAlignment = Alignment.Center
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Text(
                        text = state.deploymentMessage.ifEmpty { tr(R.string.s_zhengzaichushihua) },
                        color = keyTextColor,
                        style = androidx.compose.material3.MaterialTheme.typography.bodyLarge
                    )
                    if (isError) {
                        Spacer(modifier = Modifier.height(12.dp))
                        Text(
                            text = tr(R.string.s_dianjiguanbi),
                            color = keyTextColor.copy(alpha = 0.5f),
                            style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                            textAlign = TextAlign.Center
                        )
                    } else {
                        Text(
                            text = tr(R.string.s_qingshaohou),
                            color = keyTextColor.copy(alpha = 0.7f),
                            style = androidx.compose.material3.MaterialTheme.typography.bodyMedium
                        )
                    }
                }
            }
        }

        if (page is KeyboardPage.Overlay && !showClipboardSearchPanel) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .fillMaxHeight()
                    .clickable(
                    interactionSource = remember { MutableInteractionSource() },
                    indication = null
                ) { }
            ) {
            when (val p = page) {
                is KeyboardPage.Overlay -> when (p.route) {
                    is OverlayRoute.Menu -> MenuBar(
                        state = MenuBarState(
                            isVisible = true,
                            isDarkTheme = state.isDarkTheme,
                            darkMode = state.darkMode,
                            backgroundColor = keyboardBgColor,
                            keyBgColor = keyBgColor,
                            keyTextColor = keyTextColor,
                            isFloatingMode = state.isFloatingMode,
                            canResizeKeyboard = !state.isHostConstrained,
                            canFloatKeyboard = !state.isHostConstrained,
                        ),
                        callbacks = MenuBarCallbacks(
                            onDismiss = { viewModel.closeOverlay() },
                            onClipboard = { viewModel.showOverlay(OverlayRoute.Clipboard(0)); callbacks.onClipboard?.invoke() },
                            onQuickSend = { viewModel.showOverlay(OverlayRoute.Clipboard(1)); callbacks.onQuickSend?.invoke() },
                            onKeyboardResize = { callbacks.onKeyboardResize?.invoke(); viewModel.closeOverlay() },
                            onEmoji = { viewModel.showOverlay(OverlayRoute.Emoji) },
                            onFeedback = {
                                // 点击反馈入口即消耗红点链路，然后打开独立的 Python 自绘页面
                                UiOverrides.clearBadgeChain()
                                viewModel.closeOverlay()
                                FeedbackActivity.start(context)
                            },
                            onReloadConfig = { callbacks.onReloadConfig?.invoke(); viewModel.closeOverlay() },
                            onSettings = { callbacks.onSettings?.invoke(); viewModel.closeOverlay() },
                            onSchemaList = { viewModel.pushOverlay(OverlayRoute.SchemaList) },
                            onToggleDarkMode = { callbacks.onToggleDarkMode?.invoke() },
                            onToolbarCustomize = { viewModel.showOverlay(OverlayRoute.ToolbarCustomize) },
                            onFloatingModeToggle = { callbacks.onFloatingModeChange?.invoke(!state.isFloatingMode); viewModel.closeOverlay() },
                            onToggleSchemaSwitch = { sw -> callbacks.onToggleSchemaSwitch?.invoke(sw); viewModel.closeOverlay() },
                        ),
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.SchemaSwitches -> SchemaSwitchListView(
                        switches = state.schemaSwitches,
                        backgroundColor = keyboardBgColor,
                        keyTextColor = keyTextColor,
                        keyBgColor = keyBgColor,
                        onToggleSwitch = { sw -> onHapticFeedback?.invoke(); callbacks.onToggleSchemaSwitch?.invoke(sw) },
                        onBack = { viewModel.closeOverlay() },
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.SchemaList -> SchemaListView(
                        schemas = state.schemas,
                        currentSchemaId = state.currentSchemaId,
                        backgroundColor = keyboardBgColor,
                        accentColor = accentColor,
                        keyTextColor = keyTextColor,
                        keyBgColor = keyBgColor,
                        onSelectSchema = { schemaId ->
                            callbacks.onSwitchSchema?.invoke(schemaId)
                            viewModel.closeOverlay()
                        },
                        onBack = { viewModel.closeOverlay() },
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.Clipboard -> clipboardPanel(
                        Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.ToolbarCustomize -> ToolbarCustomizeView(
                        toolbarButtons = state.toolbarButtons,
                        pluginButtons = state.toolbarPluginButtons,
                        isSttEnabled = state.isSttEnabled,
                        keyTextColor = keyTextColor,
                        backgroundColor = keyboardBgColor,
                        accentColor = accentColor,
                        keyBgColor = keyBgColor,
                        onUpdateToolbarButtons = callbacks.onUpdateToolbarButtons,
                        onDismiss = { viewModel.closeOverlay() },
                        bottomPaddingDp = state.keyboardBottomPaddingDp,
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.Edit -> {
                        val editAction: (String) -> Unit = { action ->
                            when (action) {
                                "delete" -> callbacks.onKeyPress("delete", false)
                                "enter" -> callbacks.onKeyPress("enter", false)
                                else -> callbacks.onToolbarEditingAction?.invoke(action)
                            }
                        }
                        EditKeyboardLayout(
                            onAction = editAction,
                            onBack = { viewModel.closeOverlay() },
                            backgroundColor = keyboardBgColor,
                            textColor = keyTextColor,
                            accentColor = accentColor,
                            keyBgColor = keyBgColor,
                            bottomPaddingDp = state.keyboardBottomPaddingDp,
                            keyCornerRadius = overlayKeyCornerRadius(kbKey, isTablet()),
                            shadowEnabled = kbShadow.enabled,
                            shadowElevation = kbShadow.elevation.dp,
                            shadowShapeRadius = kbShadow.shapeRadius.dp,
                            modifier = Modifier.fillMaxWidth().fillMaxHeight()
                        )
                    }
                    is OverlayRoute.Emoji -> EmojiKeyboardLayout(
                        onEmojiSelect = { emoji ->
                            onHapticFeedback?.invoke()
                            if (emoji == "delete") {
                                callbacks.onKeyPress("delete", false)
                            } else {
                                callbacks.onCommitText?.invoke(emoji)
                            }
                        },
                        onImageEmojiSelect = callbacks.onCommitImage,
                        onBack = { viewModel.closeOverlay() },
                        backgroundColor = keyboardBgColor,
                        textColor = keyTextColor,
                        accentColor = accentColor,
                        bottomPaddingDp = state.keyboardBottomPaddingDp,
                        modifier = Modifier.fillMaxWidth().fillMaxHeight(),
                        onHapticFeedback = onHapticFeedback,
                    )
                    is OverlayRoute.Symbol -> SymbolKeyboardLayout(
                        onSelect = { symbol ->
                            onHapticFeedback?.invoke()
                            if (symbol == "delete") {
                                callbacks.onKeyPress("delete", false)
                            } else {
                                callbacks.onCommitText?.invoke(symbol)
                            }
                        },
                        onBack = { viewModel.closeOverlay() },
                        backgroundColor = keyboardBgColor,
                        textColor = keyTextColor,
                        accentColor = accentColor,
                        keyBgColor = keyBgColor,
                        specialKeyBgColor = specialKeyBgColor,
                        specialKeyTextColor = specialKeyTextColor,
                        bottomPaddingDp = state.keyboardBottomPaddingDp,
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.CandidatePage -> CandidatePage(
                        state = CandidatePageState(
                            candidates = candidateState.value.candidates.toList(),
                            candidateComments = candidateState.value.candidateComments.toList(),
                            associationCandidates = candidateState.value.associationCandidates.toList(),
                            backgroundColor = keyboardBgColor,
                            textColor = candidateTextColor,
                            hasNextPage = candidateState.value.hasNextPage,
                            hasPrevPage = candidateState.value.hasPrevPage,
                            bottomPaddingDp = state.keyboardBottomPaddingDp,
                        ),
                        customPhraseWords = customPhraseWords,
                        callbacks = CandidatePageCallbacks(
                            onCandidateSelect = { index, candidate ->
                                callbacks.onCandidateSelect(index, candidate)
                                viewModel.closeOverlay()
                            },
                            onCustomPhraseAction = { word, action ->
                                callbacks.onCustomPhraseAction?.invoke(word, action)
                            },
                            onUiDiagnosticEvent = callbacks.onUiDiagnosticEvent,
                            onAssociationSelect = { index, candidate ->
                                callbacks.onAssociationSelect?.invoke(index, candidate)
                                viewModel.closeOverlay()
                            },
                            onPageDown = callbacks.onPageDown,
                            onPageUp = callbacks.onPageUp,
                            onBack = { viewModel.closeOverlay() },
                        ),
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.SplitWords -> SplitWordsView(
                        text = p.route.text,
                        backgroundColor = keyboardBgColor,
                        viewModel = viewModel,
                        onBack = { viewModel.closeOverlay() },
                        onNavigateToQuickSend = { viewModel.pushOverlay(OverlayRoute.Clipboard(1)) },
                        onSelectChar = { char -> callbacks.onCommitText?.invoke(char) },
                        onDeleteText = { count -> callbacks.onDeleteText?.invoke(count) },
                        bottomPaddingDp = state.keyboardBottomPaddingDp,
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                    is OverlayRoute.ToolPanel -> InfoPanel(
                        title = state.toolPanelTitle,
                        nodes = state.toolPanelUiNodes ?: emptyList(),
                        items = state.toolPanelItems,
                        isLoading = state.toolPanelLoading,
                        backgroundColor = keyboardBgColor,
                        textColor = keyTextColor,
                        accentColor = accentColor,
                        itemBgColor = keyBgColor,
                        bottomPaddingDp = state.keyboardBottomPaddingDp,
                        onClose = { callbacks.onToolPanelClose?.invoke() },
                        onAction = { actionId -> callbacks.onToolPanelAction?.invoke(actionId) },
                        onItemClick = { item -> callbacks.onToolPanelItemClick?.invoke(item) },
                        modifier = Modifier.fillMaxWidth().fillMaxHeight()
                    )
                }
                else -> {}
            }
        }
        }
    }
}
}
}

/**
 * 语音输入过程状态悬浮标签（临时可观测性 UI）：
 * 半透明覆盖在输入工具栏上方，仅显示文本；不设置任何点击/手势修饰符，
 * 不参与触摸命中，工具栏按钮点击完全不受影响。
 * 抬手后 UI 保持 PROCESSING（service 层 finalizing 标志驱动）直到最终结果上屏，
 * 冷按等模型的数秒内也持续可见，消除"录音→计算→上屏"黑箱。
 */
@androidx.compose.runtime.Composable
private fun VoiceStatusLabel(
    recognitionState: com.kingzcheung.xime.speech.RecognitionState,
    recognizedText: String,
    modifier: Modifier = Modifier
) {
    var heldText by remember { mutableStateOf("") }
    // 本次会话是否已进入过聆听态：区分"按下初始准备"与"抬手后计算中"两种 PROCESSING
    var seenListening by remember { mutableStateOf(false) }
    SideEffect {
        when (recognitionState) {
            com.kingzcheung.xime.speech.RecognitionState.LISTENING -> {
                seenListening = true
                if (recognizedText.isNotEmpty()) heldText = recognizedText
            }
            com.kingzcheung.xime.speech.RecognitionState.IDLE -> seenListening = false
            else -> {}
        }
    }
    LaunchedEffect(recognitionState) {
        if (recognitionState == com.kingzcheung.xime.speech.RecognitionState.IDLE) {
            kotlinx.coroutines.delay(800)
            heldText = ""
        }
    }

    val visible = recognitionState == com.kingzcheung.xime.speech.RecognitionState.PROCESSING ||
        recognitionState == com.kingzcheung.xime.speech.RecognitionState.LISTENING ||
        (recognitionState == com.kingzcheung.xime.speech.RecognitionState.IDLE && heldText.isNotEmpty())
    if (!visible) return

    val label = when (recognitionState) {
        com.kingzcheung.xime.speech.RecognitionState.PROCESSING ->
            if (seenListening) {
                if (heldText.isNotEmpty()) "识别中：$heldText" else "识别中…"
            } else {
                "正在准备录音…"
            }
        com.kingzcheung.xime.speech.RecognitionState.LISTENING ->
            if (recognizedText.isEmpty()) "正在聆听…" else "录音中：$recognizedText"
        else -> "识别中：$heldText"
    }

    androidx.compose.material3.Surface(
        modifier = modifier
            .padding(horizontal = 24.dp, vertical = 2.dp),
        shape = RoundedCornerShape(16.dp),
        color = Color(0xB3000000)
    ) {
        Text(
            text = label,
            color = Color.White,
            fontSize = 12.sp,
            maxLines = 1,
            overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 5.dp)
        )
    }
}
