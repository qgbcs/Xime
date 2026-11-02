package com.kingzcheung.xime.ui.keyboard

import com.kingzcheung.xime.BuildConfig
import com.kingzcheung.xime.service.PredictionManager
import android.annotation.SuppressLint
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
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
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.twotone.Image
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.composed
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.Paint
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.PopupProperties
import com.kingzcheung.xime.R
import com.kingzcheung.xime.clipboard.ClipboardItem
import com.kingzcheung.xime.keyboard.KeyboardPage
import com.kingzcheung.xime.keyboard.OverlayRoute
import com.kingzcheung.xime.keyboard.PanelType
import com.kingzcheung.xime.keyboard.ToolbarAction
import com.kingzcheung.xime.settings.SettingsPreferences
import com.kingzcheung.xime.speech.RecognitionState
import com.kingzcheung.xime.ui.rpc.UiBadgeHost
import com.kingzcheung.xime.util.UiOverrides

@Immutable
data class CandidateBarVisuals(
    val backgroundColor: Color,
    val textColor: Color,
    val dividerColor: Color,
    val accentColor: Color = Color(0xFF1A73E8),
    val selectedTextColor: Color = Color(0xFF1A73E8),
    val isDarkTheme: Boolean = false,
    /** 按键底色：深色下圆形图标按钮与按键同材质（半透明白叠在键盘背景上）。 */
    val keyBgColor: Color = Color.Unspecified,
)

data class CandidateBarCallbacks(
    val onCandidateSelect: (Int, String) -> Unit,
    val onLogoClick: (() -> Unit)? = null,
    val onBack: (() -> Unit)? = null,
    val onHideKeyboard: (() -> Unit)? = null,
    val onShowMoreCandidates: (() -> Unit)? = null,
    val onClearAssociation: (() -> Unit)? = null,
    val onInputTextClick: (() -> Unit)? = null,
    val onAssociationSelect: ((Int, String) -> Unit)? = null,
    // 长按用户自定义候选词：word=词条文本，action="delete"|"moveSecondPage"
    val onCustomPhraseAction: ((word: String, action: String) -> Unit)? = null,
    val onUiDiagnosticEvent: ((String, String) -> Unit)? = null
)

@Composable
fun CandidateBar(
    state: CandidateBarState,
    page: KeyboardPage = KeyboardPage.Main(com.kingzcheung.xime.keyboard.MainType.FULL),
    candidatePageExpanded: Boolean = false,
    toolbarActions: List<ToolbarAction> = emptyList(),
    visuals: CandidateBarVisuals,
    callbacks: CandidateBarCallbacks,
    inlineSuggestions: List<*> = listOf<Any>(),
    @SuppressLint("ModifierParameter") modifier: Modifier = Modifier,
    isFloatingMode: Boolean = false,
    isVoiceSticky: Boolean = false,
    voiceAmplitude: Float = 0f,
    voiceSpectrum: FloatArray = FloatArray(16),
    voiceRecognitionState: RecognitionState = RecognitionState.IDLE,
    voicePluginName: String = "",
    // 当前方案用户自定义词条；命中的候选长按可删除或降频到第二屏
    customPhraseWords: Set<String> = emptySet(),
) {
    val configuration = LocalConfiguration.current
    val isLandscape = !isFloatingMode && configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE
    // 统一 8dp 靠边；横屏挖孔/导航栏的物理避让由服务层内容容器的边衬区 padding 统一处理，
    // 不再叠加手机横屏专用的 50dp 缩进
    val horizontalPadding = 8.dp
    val context = LocalContext.current

    // 圆形图标按钮底色（logo / 返回 / 收起）：深色下与按键同材质——按键就是半透明白叠在
    // 键盘背景上；若仍用 surface 与 primary 混色，surface 在深色下只有 6% 亮度，混出的圆底
    // 与键盘背景几乎同色，圆底等于不存在。亮色下按键是纯白，图标按钮改用混合色更耐看。
    val iconButtonContainer = if (visuals.isDarkTheme && visuals.keyBgColor != Color.Unspecified) {
        visuals.keyBgColor
    } else {
        androidx.compose.ui.graphics.lerp(
            MaterialTheme.colorScheme.surface,
            MaterialTheme.colorScheme.primary,
            0.15f
        )
    }
    val iconButtonTint = MaterialTheme.colorScheme.onSurfaceVariant
    val showComments = SettingsPreferences.showCandidateComments(context)
    val inputTextLocation = SettingsPreferences.getInputTextLocation(context)
    val showInputBoxStyle = inputTextLocation == SettingsPreferences.INPUT_TEXT_INPUT_BOX
    val candidateTextSize = SettingsPreferences.getCandidateTextSize(context)
    val candidateFontFamily = AppFonts.candidateFontFamily
    val commentFontFamily = AppFonts.commentFontFamily

    val density = LocalDensity.current
    // 长按自定义候选词弹出操作菜单：null=菜单关闭，否则为被长按的词条文本
    var longPressedWord by remember { mutableStateOf<String?>(null) }
    val textMeasurer = rememberTextMeasurer()
    val itemPaddingPx = with(density) { 8.dp.toPx() }
    val spacingPx = with(density) { 4.dp.toPx() }

    val screenWidthPx = with(density) { LocalConfiguration.current.screenWidthDp.dp.toPx() }
    val rowPaddingPx = with(density) { 16.dp.toPx() }
    val rightSidePx = with(density) {
        val moreBtn = if (callbacks.onShowMoreCandidates != null) 38.dp.toPx() else 0f
        val clearBtn = if (callbacks.onClearAssociation != null) 38.dp.toPx() else 0f
        val hideBtn = if (callbacks.onHideKeyboard != null) 28.dp.toPx() else 0f
        rowPaddingPx + maxOf(moreBtn, clearBtn) + hideBtn + 8.dp.toPx()
    }

    // 候选行滚动状态：需在 state 分支前声明——ChineseCandidates 的 hasAnyMore
    // 叠加 canScrollForward 判断（见分支内注释）
    val candidateListState = rememberLazyListState()

    val displayCandidates: List<String>
    val candidateDisplayItems: List<CandidateDisplayItem>
    val displayAssociation: List<String>
    val displayComments: List<String>
    /** 与 [displayCandidates] 等长：图片候选条目（非图片位为 null）。 */
    val displayImages: List<ClipboardItem?>
    val hasAnyMore: Boolean
    val showInputTextRow: Boolean
    val showLeftIcon: Boolean

    when (val s = state) {
        is CandidateBarState.Idle -> {
            displayCandidates = emptyList()
            candidateDisplayItems = emptyList()
            displayAssociation = emptyList()
            displayComments = emptyList()
            displayImages = emptyList()
            hasAnyMore = false
            showLeftIcon = true
        }
        is CandidateBarState.ChineseCandidates -> {
            candidateDisplayItems = EnglishCandidateDisplayPolicy.prepare(
                s.candidates,
                demotedCandidateCount = s.demotedCandidateCount,
            )
            val taken = candidateDisplayItems.map { it.candidate }
            displayCandidates = taken
            displayComments = s.comments
            hasAnyMore = s.hasMore || candidateDisplayItems.size < s.candidates.size
            showLeftIcon = false
            displayAssociation = remember(s.associationCandidates, taken, s.inputText, textMeasurer) {
                if (taken.isEmpty()) {
                    s.associationCandidates.take(PredictionManager.MAX_ASSOCIATION_COUNT)
                } else {
                    val measureText = { text: String ->
                        textMeasurer.measure(
                            text = AnnotatedString(text),
                            style = TextStyle(fontSize = candidateTextSize.sp)
                        ).size.width.toFloat()
                    }
                    val leftSidePx = with(density) { rowPaddingPx + 32.dp.toPx() }
                    val lazyRowWidthPx = screenWidthPx - leftSidePx - rightSidePx
                    val regularWidthPx = candidateDisplayItems.sumOf { item ->
                        measureText(item.displayText).toDouble() + itemPaddingPx
                    }.toFloat()
                    val dividerWidthPx = with(density) { 9.dp.toPx() }
                    val availablePx = rowWidthPx - regularWidthPx - dividerWidthPx

                    var used = 0f
                    val result = mutableListOf<String>()
                    for (c in s.associationCandidates) {
                        val w =
                            measureText(c) + itemPaddingPx + (if (result.isEmpty()) 0f else spacingPx)
                        if (used + w <= availablePx) {
                            used += w
                            result.add(c)
                        } else break
                    }
                    result
                }
            }
        }
        is CandidateBarState.AssociationOnly -> {
            displayCandidates = emptyList()
            candidateDisplayItems = emptyList()
            displayAssociation = s.candidates.take(PredictionManager.MAX_ASSOCIATION_COUNT)
            hasAnyMore = s.hasMore
            showLeftIcon = false
            displayComments = s.comments
            displayImages = emptyList()
        }
        is CandidateBarState.EnglishCandidates -> {
            displayCandidates = s.candidates.take(20)
            candidateDisplayItems = displayCandidates.mapIndexed { index, candidate ->
                CandidateDisplayItem(index, candidate, candidate)
            }
            displayComments = s.comments
            displayAssociation = emptyList()
            hasAnyMore = false
            showLeftIcon = false
            displayImages = emptyList()
        }
        is CandidateBarState.ClipboardDisplay -> {
            // images 与 candidates 由服务层按同一列表同序构造，take 必须同源同步，否则索引错位
            displayCandidates = s.candidates.take(20)
            candidateDisplayItems = displayCandidates.mapIndexed { index, candidate ->
                CandidateDisplayItem(index, candidate, candidate)
            }
            displayComments = emptyList()
            displayAssociation = emptyList()
            hasAnyMore = false
            showLeftIcon = true
        }
        is CandidateBarState.Calculator -> {
            displayCandidates = s.candidates.take(20)
            candidateDisplayItems = displayCandidates.mapIndexed { index, candidate ->
                CandidateDisplayItem(index, candidate, candidate)
            }
            displayComments = s.comments
            displayAssociation = emptyList()
            hasAnyMore = false
            showLeftIcon = false
            displayImages = emptyList()
        }
    }
    showInputTextRow = when (page) {
        is KeyboardPage.Overlay -> page.route !is OverlayRoute.Clipboard
        else -> true
    }

    val candidateListState = rememberLazyListState()
    LaunchedEffect(candidateDisplayItems) {
        candidateListState.scrollToItem(0)
    }

    // 编码气泡：候选栏内计算编码文本后回写此状态，供 Column 的 drawBehind 读取绘制。
    // drawBehind 在下一帧读取最新值，无需同步；初始值取自当前 state 保证首帧即显示。
    var preeditBubbleText by remember(state) {
        mutableStateOf((state as? CandidateBarState.ChineseCandidates)?.preeditText
            ?: (state as? CandidateBarState.ChineseCandidates)?.inputText ?: "")
    }
    var preeditBubbleCaret by remember(state) {
        mutableStateOf((state as? CandidateBarState.ChineseCandidates)?.preeditCaretPos ?: -1)
    }
    val showPreeditBubble = showInputTextRow && preeditBubbleText.isNotEmpty() && !showInputBoxStyle

    Column(
        modifier = modifier
            .fillMaxWidth()
            .height(IconButtonRowHeight)
            .drawPreeditBubble(
                text = preeditBubbleText,
                caretOffset = preeditBubbleCaret,
                enabled = showPreeditBubble,
                bubbleColor = visuals.backgroundColor,
                textColor = visuals.textColor
            )
            .background(visuals.backgroundColor)
            .padding(horizontal = horizontalPadding),
        verticalArrangement = Arrangement.Center
    ) {
        if (isVoiceSticky) {
            // 常驻语音模式：候选栏显示语音引擎名 + 频谱。
            // 整条候选栏可点：轻触频谱即结束识别，不必非去点空格。
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .fillMaxHeight()
                    .clickable(enabled = callbacks.onVoiceStop != null) {
                        callbacks.onVoiceStop?.invoke()
                    },
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                if (voicePluginName.isNotEmpty()) {
                    Text(
                        text = voicePluginName,
                        color = visuals.textColor.copy(alpha = 0.7f),
                        fontSize = 10.sp,
                        maxLines = 1,
                        textAlign = TextAlign.Center
                    )
                }
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .weight(1f)
                ) {
                    AudioSpectrumAnimation(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(horizontal = 24.dp, vertical = 2.dp),
                        isActive = voiceRecognitionState == RecognitionState.LISTENING ||
                            voiceRecognitionState == RecognitionState.PROCESSING,
                        amplitude = voiceAmplitude,
                        spectrum = voiceSpectrum,
                        barWidthFactor = 4f,
                        barCount = 16,
                        spacingRatio = 1.6f,
                        heightScale = 0.6f
                    )
                }
            }
            return@Column
        }

        val displayText = (state as? CandidateBarState.ChineseCandidates)?.preeditText
            ?: (state as? CandidateBarState.ChineseCandidates)?.inputText ?: ""
        // 编码显示已改为候选栏顶部的悬浮气泡（drawBehind 绘制，见 drawPreeditBubble），
        // 栏内不再为编码保留布局空间——打字态与联想态的候选行共用同一垂直位置。
        preeditBubbleText = displayText
        preeditBubbleCaret = (state as? CandidateBarState.ChineseCandidates)?.preeditCaretPos ?: -1

        Row(
            modifier = Modifier
                .fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically
        ) {
            if (showLeftIcon) {
                when (state) {
                    is CandidateBarState.Idle -> {
                        if (page is KeyboardPage.Overlay && page.route is OverlayRoute.SchemaList && callbacks.onBack != null) {
                            Box(
                                modifier = Modifier
                                    .size(IconButtonSize)
                                    .clip(CircleShape)
                                    .background(iconButtonContainer)
                                    .clickable { callbacks.onBack() },
                                contentAlignment = Alignment.Center
                            ) {
                                Icon(
                                    imageVector = Icons.AutoMirrored.Filled.KeyboardArrowLeft,
                                    contentDescription = tr(R.string.s_fanhuicaidan),
                                    tint = visuals.accentColor,
                                    modifier = Modifier.size(IconButtonGlyphSize)
                                )
                            }
                        } else {
                            // RPC 槽位：主图标色调由 ime.candidate.logo#tint 下发，角标由 #badge 下发
                            val logoTint = UiOverrides.get(UiOverrides.TARGET_CANDIDATE_LOGO, "tint")
                                ?.let { runCatching { Color(android.graphics.Color.parseColor(it)) }.getOrNull() }
                                ?: Color.Unspecified
                            UiBadgeHost(
                                target = UiOverrides.TARGET_CANDIDATE_LOGO,
                                modifier = Modifier.size(28.dp)
                            ) {
                                Box(
                                    modifier = Modifier
                                        .size(28.dp)
                                        .clip(RoundedCornerShape(14.dp))
                                        .clickable { callbacks.onLogoClick?.invoke() },
                                    contentAlignment = Alignment.Center
                                ) {
                                    Icon(
                                        painter = painterResource(R.drawable.ic_launcher_foreground),
                                        contentDescription = "${BuildConfig.APP_CHINESE_NAME} Logo",
                                        tint = logoTint,
                                        modifier = Modifier.size(18.dp)
                                    )
                                }
                            }
                        }
                        Spacer(modifier = Modifier.width(2.dp))
                    }
                    is CandidateBarState.ClipboardDisplay -> {
                        Row(
                            modifier = Modifier.padding(end = 4.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Icon(
                                imageVector = Icons.Default.ContentCopy,
                                contentDescription = tr(R.string.s_jianqieban),
                                tint = visuals.accentColor,
                                modifier = Modifier.size(16.dp)
                            )
                        }
                    }
                    else -> {}
                }
            }

            if (inlineSuggestions.isNotEmpty()) {
                BoxWithConstraints(modifier = Modifier.weight(1f)) {
                    // 建议宽度不超过本行实际分到的栏宽（独占候选栏时仍为 180dp）：
                    // 与候选/剪贴板同显时本行只有一半栏宽，固定 180dp 会越界——
                    // InlineContentView 是独立 surface 渲染，clipToBounds 裁不住，
                    // 越界部分会压到右侧候选上（issue #1136）。
                    val suggestionWidth = minOf(maxWidth, 180.dp)
                    LazyRow(
                        // 占满配额：内容少时建议靠左、右侧留白到收起按钮（收起按钮
                        // 因此固定最右）；内容超出配额时占满并可横向滑动查看后续建议；
                        // clipToBounds：滑动时滑出边界的建议裁剪掉，避免与左侧 logo 重叠
                        modifier = Modifier
                            .fillMaxWidth()
                            .clipToBounds(),
                    ) {
                        itemsIndexed(inlineSuggestions, key = { index, _ -> index }) { _, suggestion ->
                            Box(modifier = Modifier.fillMaxHeight()) {
                                InlineSuggestionView(
                                    suggestion = suggestion,
                                    modifier = Modifier
                                        .fillMaxHeight()
                                        .width(suggestionWidth),
                                )
                                // 每条尾部 1dp 分隔线：条目之间为间隔，最后一条的尾线
                                // 同时充当与候选词区的分界（与旧平铺布局视觉一致）
                                InlineSuggestionDivider(
                                    modifier = Modifier.align(Alignment.CenterEnd),
                                    color = visuals.dividerColor,
                                )
                            }
                        }
                    }
                }
            }

            LazyRow(
                modifier = if (state is CandidateBarState.Idle) Modifier else Modifier.weight(1f),
                state = candidateListState,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                // 图片候选芯片比文字候选高：居中对齐，避免同排文字候选被顶到上沿
                verticalAlignment = Alignment.CenterVertically,
            ) {
                itemsIndexed(candidateDisplayItems, key = { _, item -> item.index }) { _, item ->
                    val candidate = item.candidate
                    val isCustom = callbacks.onCustomPhraseAction != null &&
                        candidate in customPhraseWords
                    Box {
                        CandidateItem(
                            text = item.displayText,
                            index = item.index,
                            onClick = { callbacks.onCandidateSelect(item.index, candidate) },
                            textColor = visuals.textColor,
                            comment = if (showComments) {
                                when (val s = state) {
                                    is CandidateBarState.ChineseCandidates -> s.comments.getOrElse(item.index) { "" }
                                    is CandidateBarState.EnglishCandidates -> s.comments.getOrElse(item.index) { "" }
                                    else -> ""
                                }
                            } else "",
                            isSelected = item.index == 0,
                            accentColor = visuals.accentColor,
                            selectedTextColor = visuals.selectedTextColor,
                            fontSize = candidateTextSize.sp,
                            onLongClick = if (isCustom) {
                                {
                                    callbacks.onUiDiagnosticEvent?.invoke(
                                        "candidate_long_press",
                                        "source=candidate_bar;candidate=$candidate"
                                    )
                                    longPressedWord = candidate
                                }
                            } else null
                        )
                        if (isCustom) {
                            DropdownMenu(
                                expanded = longPressedWord == candidate,
                                properties = PopupProperties(focusable = false),
                                onDismissRequest = {
                                    callbacks.onUiDiagnosticEvent?.invoke(
                                        "candidate_menu_dismissed",
                                        "source=candidate_bar;candidate=$candidate;popup_focusable=false"
                                    )
                                    longPressedWord = null
                                }
                            ) {
                                LaunchedEffect(longPressedWord == candidate) {
                                    if (longPressedWord == candidate) {
                                        callbacks.onUiDiagnosticEvent?.invoke(
                                            "candidate_menu_shown",
                                            "source=candidate_bar;candidate=$candidate;popup_focusable=false"
                                        )
                                    }
                                }
                                DropdownMenuItem(
                                    text = { Text("移到第二屏") },
                                    onClick = {
                                        val w = candidate
                                        callbacks.onUiDiagnosticEvent?.invoke(
                                            "candidate_menu_selected",
                                            "source=candidate_bar;candidate=$w;action=moveSecondPage"
                                        )
                                        longPressedWord = null
                                        callbacks.onCustomPhraseAction?.invoke(w, "moveSecondPage")
                                    }
                                )
                                DropdownMenuItem(
                                    text = { Text("删除该词") },
                                    onClick = {
                                        val w = candidate
                                        callbacks.onUiDiagnosticEvent?.invoke(
                                            "candidate_menu_selected",
                                            "source=candidate_bar;candidate=$w;action=delete"
                                        )
                                        longPressedWord = null
                                        callbacks.onCustomPhraseAction?.invoke(w, "delete")
                                    }
                                )
                            }
                        }
                    }
                }

                // 仅当左侧存在打字候选时才需要分隔线；纯联想态（无打字候选）下
                // 该竖线会孤悬列表最左缘，属多余元素。
                // 注意：分隔线在条件内，联想词 items 必须在条件外——纯联想态
                // displayCandidates 为空，若一并包进条件会导致联想词整个不渲染。
                if (displayCandidates.isNotEmpty() && displayAssociation.isNotEmpty()) {
                    item(key = "divider") {
                        Box(
                            modifier = Modifier
                                .width(1.dp)
                                .height(20.dp)
                                .background(visuals.dividerColor.copy(alpha = 0.5f))
                                .padding(horizontal = 4.dp)
                        )
                    }
                }

                    itemsIndexed(displayAssociation, key = { index, _ -> "assoc-$index" }) { index, candidate ->
                        val assocState = state as? CandidateBarState.AssociationOnly
                        CandidateItem(
                            text = candidate,
                            index = -1,
                            onClick = { callbacks.onAssociationSelect?.invoke(index, candidate) },
                            textColor = visuals.textColor,
                            comment = assocState?.comments?.getOrElse(index) { "" } ?: "",
                            isSelected = assocState?.highlightIndex == index,
                            accentColor = visuals.accentColor,
                            selectedTextColor = visuals.selectedTextColor,
                            fontSize = candidateTextSize.sp
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.width(8.dp))

            when {
                state is CandidateBarState.Idle -> {
                    // 工具栏按钮按可用宽度等分：窄屏（如 360dp）也保证配置的图标全部放得下，
                    // 不再用固定的 42dp 单按钮宽度把最后一个图标挤出屏幕。
                    BoxWithConstraints(
                        modifier = Modifier.weight(1f, fill = true)
                    ) {
                        val actionCount = toolbarActions.size
                        val slot = if (actionCount > 0) maxWidth / actionCount else 32.dp
                        val iconSize = (slot * 0.68f).coerceIn(14.dp, 22.dp)
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.End,
                        ) {
                            toolbarActions.forEach { action ->
                                val interactionSource = remember { MutableInteractionSource() }
                                val isPressed by interactionSource.collectIsPressedAsState()
                                Box(
                                    modifier = Modifier
                                        .weight(1f, fill = true)
                                        .fillMaxHeight()
                                        .clickable(
                                            interactionSource = interactionSource,
                                            indication = null,
                                            onClick = action.onClick
                                        ),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Icon(
                                        imageVector = action.button.icon,
                                        contentDescription = action.button.label,
                                        tint = if (isPressed) iconButtonTint.copy(alpha = 0.6f) else iconButtonTint,
                                        modifier = Modifier.size(iconSize)
                                    )
                                }
                            }
                        }
                    }

                    if (callbacks.onHideKeyboard != null) {
                        val hideKeyboardInteractionSource = remember { MutableInteractionSource() }
                        val isHideKeyboardPressed by hideKeyboardInteractionSource.collectIsPressedAsState()

                        Spacer(modifier = Modifier.width(4.dp))

                        Box(
                            modifier = Modifier
                                .size(IconButtonSize)
                                .clickable(
                                    interactionSource = hideKeyboardInteractionSource,
                                    indication = null,
                                    onClick = { callbacks.onHideKeyboard() }
                                ),
                            contentAlignment = Alignment.Center
                        ) {
                            Icon(
                                imageVector = Icons.Default.KeyboardArrowDown,
                                contentDescription = tr(R.string.s_shouqijianpan),
                                tint = if (isHideKeyboardPressed) iconButtonTint.copy(alpha = 0.6f) else iconButtonTint,
                                modifier = Modifier.size(IconButtonGlyphSize)
                            )
                        }
                    }
                }
                candidatePageExpanded -> {
                    if (callbacks.onBack != null) {
                        Box(
                            modifier = Modifier
                                .size(IconButtonSize)
                                .clip(CircleShape)
                                .background(iconButtonContainer)
                                .clickable { callbacks.onBack() },
                            contentAlignment = Alignment.Center
                        ) {
                            Icon(
                                imageVector = Icons.Default.KeyboardArrowUp,
                                contentDescription = tr(R.string.s_fanhuijianpan),
                                tint = visuals.accentColor,
                                modifier = Modifier.size(IconButtonGlyphSize)
                            )
                        }
                    }
                }
                displayAssociation.isNotEmpty() && callbacks.onClearAssociation != null -> {
                    val clearInteractionSource = remember { MutableInteractionSource() }
                    val isClearPressed by clearInteractionSource.collectIsPressedAsState()

                    Spacer(modifier = Modifier.width(4.dp))
                    Box(
                        modifier = Modifier
                            .width(1.dp)
                            .height(28.dp)
                            .background(visuals.dividerColor).padding(end = 1.dp)
                    )
                    Box(
                        modifier = Modifier
                            .width(30.dp)
                            .height(24.dp)
                            .clip(CircleShape)
                            .background(
                                if (isClearPressed) (if (visuals.isDarkTheme) Color.White.copy(alpha = 0.15f) else Color.Black.copy(
                                    alpha = 0.1f
                                ))
                                else Color.Transparent
                            )
                            .clickable(
                                interactionSource = clearInteractionSource,
                                indication = null,
                                onClick = { callbacks.onClearAssociation() }
                            ),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            text = tr(R.string.s_qingkong),
                            color = if (isClearPressed) visuals.textColor.copy(alpha = 0.6f) else visuals.textColor,
                            fontSize = 11.sp
                        )
                    }
                }
                hasAnyMore && callbacks.onShowMoreCandidates != null -> {
                    val moreInteractionSource = remember { MutableInteractionSource() }
                    val isMorePressed by moreInteractionSource.collectIsPressedAsState()

                    Spacer(modifier = Modifier.width(4.dp))
                    Box(
                        modifier = Modifier
                            .width(30.dp)
                            .height(24.dp)
                            .clip(CircleShape)
                            .background(
                                if (isMorePressed) (if (visuals.isDarkTheme) Color.White.copy(alpha = 0.15f) else Color.Black.copy(
                                    alpha = 0.1f
                                ))
                                else Color.Transparent
                            )
                            .clickable(
                                interactionSource = moreInteractionSource,
                                indication = null,
                                onClick = { callbacks.onShowMoreCandidates() }
                            ),
                        contentAlignment = Alignment.Center
                    ) {
                        Icon(
                            imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
                            contentDescription = tr(R.string.s_gengduohouxuan),
                            tint = if (isMorePressed) visuals.textColor.copy(alpha = 0.6f) else visuals.textColor,
                            modifier = Modifier.size(22.dp)
                        )
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun CandidateItem(
    text: String,
    index: Int,
    onClick: () -> Unit,
    textColor: Color,
    comment: String = "",
    isSelected: Boolean = false,
    accentColor: Color = Color(0xFF1A73E8),
    selectedTextColor: Color = Color(0xFF1A73E8),
    @SuppressLint("ModifierParameter") modifier: Modifier = Modifier,
    fontSize: androidx.compose.ui.unit.TextUnit = 19.sp,
    onLongClick: (() -> Unit)? = null
) {
    Row(
        modifier = modifier
            .clip(RoundedCornerShape(5.dp))
            .background(
                if (isSelected) accentColor.copy(alpha = 0.2f)
                else Color.Transparent
            )
            .combinedClickable(onClick = onClick, onLongClick = onLongClick)
            .padding(horizontal = 4.dp, vertical = 2.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            text = text,
            color = if (isSelected) selectedTextColor else textColor,
            fontSize = fontSize,
            fontWeight = if (isSelected) FontWeight.Medium else FontWeight.Normal,
            maxLines = 1,
            fontFamily = candidateFontFamily
        )
        if (comment.isNotEmpty()) {
            Spacer(modifier = Modifier.width(3.dp))
            Text(
                text = comment,
                color = if (isSelected) selectedTextColor.copy(alpha = 0.6f) else textColor.copy(alpha = 0.5f),
                fontSize = (fontSize.value * 11f / 19f).sp,
                fontWeight = FontWeight.Normal,
                maxLines = 1,
                fontFamily = commentFontFamily
            )
        }
    }
}

/**
 * 编码悬浮气泡：在候选栏顶部之上（栏外）绘制一个圆角胶囊气泡显示当前拼音编码。
 *
 * 参考 SwipeBubble 的锚定方式，但为纯绘制实现：
 * - 锚定宿主（候选栏 Column）左上角，气泡体向上悬浮于栏外空间；
 * - drawBehind 绘制不参与布局、不拦截触摸事件——候选栏上方的快捷发送表单/
 *   手写区等 UI 不受任何布局影响；
 * - IME 窗口为 MATCH_PARENT 全屏（onConfigureWindow），栏外绘制不会被窗口裁剪。
 *
 * 视觉：浅色模式近白底/深色模式深灰底的圆角胶囊（92% 不透明），无边框无阴影；
 * 编码文字在气泡内垂直居中；气泡与候选栏顶部之间留 2dp 间隙；宽度自适应，
 * 超出宿主右缘时左移钳制。
 *
 * 注意：不能使用传入的主题背景色——CandidateBarVisuals.backgroundColor 为
 * Color.Transparent（真实背景由外层绘制），以其合成会导致气泡无底色、
 * 文字与 app 内容混叠不可读。此处以候选文字亮度推断深浅模式取对比底色。
 */
private fun Modifier.drawPreeditBubble(
    text: String,
    enabled: Boolean,
    bubbleColor: Color,
    textColor: Color,
    caretOffset: Int = -1
): Modifier = composed {
    val density = LocalDensity.current
    val cornerRadiusPx = with(density) { 4.dp.toPx() }
    val horizontalPaddingPx = with(density) { 8.dp.toPx() }
    val verticalPaddingPx = with(density) { 3.dp.toPx() }
    val bubbleBottomGapPx = with(density) { 2.dp.toPx() }
    val screenMarginPx = with(density) { 4.dp.toPx() }
    val textSizePx = with(density) { 12.sp.toPx() }
    val caretWidthPx = with(density) { 1.5.dp.toPx() }

    // 气泡基色：优先用传入的主题背景色；其为全透明（CandidateBarVisuals 传
    // Color.Transparent，真实背景由外层绘制）时按候选文字亮度推导，
    // 保证浅色模式近白/深色模式深灰的可读对比。
    val isDarkTheme = textColor.luminance() > 0.5f
    val bubbleBaseColor = if (bubbleColor.alpha > 0.01f) {
        bubbleColor
    } else {
        if (isDarkTheme) Color(0xFF2D2F31) else Color(0xFFFAFAFA)
    }
    // 半透明：62% 不透明度
    val bubbleBgColor = bubbleBaseColor.copy(alpha = 0.62f)

    // 文本画笔：与 SwipeBubble 同款 nativeCanvas 绘制方式
    val bubbleTextPaint = remember(textColor, textSizePx) {
        android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
            textSize = textSizePx
            color = textColor.copy(alpha = 0.9f).toArgb()
        }
    }

    drawBehind {
        if (!enabled || text.isEmpty()) return@drawBehind

        val fontMetrics = bubbleTextPaint.fontMetrics
        val textWidth = bubbleTextPaint.measureText(text)
        // 文本实际渲染高度以可见字形区间（ascent..descent）计，避免 lineHeight 参与导致偏移
        val textHeight = fontMetrics.descent - fontMetrics.ascent
        val bubbleWidth = textWidth + horizontalPaddingPx * 2
        val bubbleHeight = textHeight + verticalPaddingPx * 2

        // 气泡贴候选栏左缘，右向延伸；超出宿主右缘时整体左移钳制。
        val clampedLeft = maxOf(
            screenMarginPx,
            minOf(0f, size.width - bubbleWidth - screenMarginPx).coerceAtLeast(screenMarginPx / 4f)
        )
        // 底部间隙：气泡底缘距候选栏顶缘 1dp
        val top = -bubbleBottomGapPx - bubbleHeight

        // 气泡主体
        drawRoundRect(
            color = bubbleBgColor,
            topLeft = Offset(clampedLeft, top),
            size = Size(bubbleWidth, bubbleHeight),
            cornerRadius = CornerRadius(cornerRadiusPx)
        )

        // 文本垂直居中：基线 = 气泡顶 + (气泡高 - (ascent + descent)) / 2，
        // ascent/descent 均为负/正相对基线的偏移，该式把字形区中点对准气泡中点。
        drawIntoCanvas { composeCanvas ->
            val baselineY = top + (bubbleHeight - (fontMetrics.ascent + fontMetrics.descent)) / 2f
            composeCanvas.nativeCanvas.drawText(text, clampedLeft + horizontalPaddingPx, baselineY, bubbleTextPaint)
        }

        // 编码编辑光标：caret 在中间（offset < 文本长度）时在对应字符边界画竖线；
        // 末尾/未知（-1）不画——默认打字态光标恒在编码末尾，保持观感不变
        if (caretOffset in 0 until text.length) {
            val caretCharIndex = caretOffset.coerceIn(0, text.length)
            val caretX = clampedLeft + horizontalPaddingPx +
                bubbleTextPaint.measureText(text, 0, caretCharIndex)
            drawLine(
                color = textColor,
                start = Offset(caretX, top + verticalPaddingPx),
                end = Offset(caretX, top + bubbleHeight - verticalPaddingPx),
                strokeWidth = caretWidthPx
            )
        }
    }
}

