package com.kingzcheung.xime.service

import com.kingzcheung.xime.R

import com.kingzcheung.xime.i18n.tr

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.inputmethod.InputConnection
import android.widget.Toast
import com.kingzcheung.xime.plugin.ExtensionManager
import com.kingzcheung.xime.speech.AsrBackendFactory
import com.kingzcheung.xime.speech.RecognitionState
import com.kingzcheung.xime.speech.SpeechRecognitionManager
import com.kingzcheung.xime.settings.SettingsPreferences
import com.kingzcheung.xime.util.FileLogger

class VoiceRecognitionHandler(
    private val context: Context,
    private val onStateChanged: (InputUIState) -> Unit,
    private val getState: () -> InputUIState,
    private val getInputConnection: () -> InputConnection?,
    private val onVoiceComplete: () -> Unit = {},
    private val onAmplitudeChanged: (Float) -> Unit = {},
    private val onSpectrumChanged: (FloatArray) -> Unit = {},
    /** 语音向输入框写入 composing 文本时回调（标记 composing 区域存在，供 endComposingInputBox 判断）。 */
    private val onComposingWritten: () -> Unit = {},
    /** manager 工厂，供测试注入 mock。 */
    private val managerFactory: (Context) -> SpeechRecognitionManager = { SpeechRecognitionManager(it) },
    /** 超时调度器，供测试注入并手动推进。 */
    private val mainHandler: Handler = Handler(Looper.getMainLooper())
) {
    companion object {
        private const val TAG = "VoiceRecognition"
        // 抬手后等待完整 final 的时长：热按实测 final 约 1.2s 到（含 400ms 尾音 +
        // 500ms 尾静音 + finalize）。超时未到才兜底提交最后 partial，防止 IPC 丢结果
        private const val RELEASE_COMMIT_FALLBACK_MS = 2_000L
        // 兜底已提交后，允许迟到 final 安全替换的时间窗
        private const val REPLACE_WINDOW_MS = 3_000L
    }

    private lateinit var speechRecognitionManager: SpeechRecognitionManager

    var textBeforeVoiceInput = ""
    var textLengthBeforeVoiceInput = 0

    fun initialize() {
        FileLogger.i(TAG, "Initializing speech recognition system")

        speechRecognitionManager = managerFactory(context)

        speechRecognitionManager.setCallbacks(
            onResult = { text ->
                handleSpeechResult(text)
            },
            onPartialResult = { text ->
                handlePartialResult(text)
            },
            onStateChange = { state ->
                handleSpeechStateChange(state)
            },
            onError = { error, userVisible ->
                handleSpeechError(error, userVisible)
            },
            onAmplitude = { amplitude ->
                handleAmplitudeUpdate(amplitude)
            },
            onSpectrum = { spectrum ->
                handleSpectrumUpdate(spectrum)
            }
        )

        val providerName = resolveProviderName()

        onStateChanged(getState().copy(voicePluginName = providerName))
        FileLogger.i(TAG, "STT provider: $providerName")

        // 若"使用本地模型"开关已开启，启动时即加载模型并常驻，
        // 保证语音时绝不现场加载模型（避免丢开头音频）。
        // 注意：keep-alive 预热也走这里（AsrSupport.warmup 注册常驻后端），
        // 不要再走 manager.preload——那会经 AsrSupport.create 造一个临时后端，
        // 与本 warmup 并发时双重绑定 :asr、双重加载模型（日志曾见两次 initialize）
        if (SettingsPreferences.isSttUseLocal(context) &&
            AsrBackendFactory.getLocalName(context) != null
        ) {
            Thread {
                AsrBackendFactory.warmup(context)
            }.start()
        }
    }

    private val delayedPreStartRunnable = Runnable {
        if (::speechRecognitionManager.isInitialized) {
            speechRecognitionManager.startPreStart()
        }
    }

    fun startDelayedPreStart(delayMs: Long = 150) {
        mainHandler.removeCallbacks(delayedPreStartRunnable)
        mainHandler.postDelayed(delayedPreStartRunnable, delayMs)
    }

    fun cancelPreStart() {
        mainHandler.removeCallbacks(delayedPreStartRunnable)
        if (::speechRecognitionManager.isInitialized) {
            speechRecognitionManager.cancelPreStart()
        }
    }

    fun startRecognition() {
        if (!::speechRecognitionManager.isInitialized) {
            Log.e(TAG, "speechRecognitionManager not initialized")
            onStateChanged(getState().copy(
                isVoiceMode = false,
                voiceSticky = false,
                voiceRecognitionState = RecognitionState.ERROR
            ))
            return
        }

        // 新链路下录音线程在 startRecognition() 中已立即开麦；先前排队的预启动
        // 回调若再执行会重复创建第二个 AudioRecord，两个录音实例并发抢占输入流，
        // 会造成实时音频异常（冷模型走缓存回放时不易暴露，热模型实时推送时识别为空）
        mainHandler.removeCallbacks(delayedPreStartRunnable)
        speechRecognitionManager.cancelPreStart()

        textBeforeVoiceInput = getInputConnection()?.getTextBeforeCursor(1000, 0)?.toString() ?: ""
        textLengthBeforeVoiceInput = textBeforeVoiceInput.length

        val providerName = resolveProviderName()
        onStateChanged(getState().copy(voicePluginName = providerName))

        speechRecognitionManager.startRecognition()
    }

    fun stopRecognition() {
        if (::speechRecognitionManager.isInitialized) {
            speechRecognitionManager.stopRecognition()
        }
        // handleFinalResult is now called from within handleSpeechResult
        // when the final stopRecognition result arrives
    }

    fun release() {
        if (::speechRecognitionManager.isInitialized) {
            speechRecognitionManager.release()
        }
    }

    fun isInitialized(): Boolean = ::speechRecognitionManager.isInitialized

    private fun resolveProviderName(): String {
        // 用户开启"本地识别"且当前构建支持离线语音时，优先显示本地引擎名
        if (SettingsPreferences.isSttUseLocal(context)) {
            // 本地引擎显示当前生效模型名；未下载任何模型时用通用名（不默认指定某个模型）
            return AsrBackendFactory.getLocalName(context) ?: tr(R.string.s_bendilixianshibie)
        }
        val enabledPlugins = ExtensionManager.getEnabledAsrPlugins(context)
        if (enabledPlugins.isNotEmpty()) {
            val selectedId = SettingsPreferences.getSttOnlinePluginId(context)
            val selected = enabledPlugins.firstOrNull { it.first == selectedId }
                ?: enabledPlugins.firstOrNull()
            if (selected != null) {
                return ExtensionManager.getAllInstalledPlugins()
                    .firstOrNull { it.id == selected.first }?.name ?: selected.first
            }
        }
        return tr(R.string.s_weipeizhi)
    }

    private var lastPartialText = ""
    // 本会话实际写入输入框的文本（无外部消费时等于 lastPartialText）。最终结果的增量对齐、
    // "删除已上屏部分"的长度都按它算，保证删除量对得上输入框里真实存在的内容。
    private var lastWrittenText = ""
    // 已被外部消费的引擎累计文本前缀（应用在输入法不知情时取走了上屏内容，如微信发送后清空输入框）
    private var consumedEnginePrefix = ""
    // 是否核对成功过"写入的文本确实回显在输入框里"。应用不回传文本时不得误判为"被清空"，否则丢字。
    private var writtenTextVerified = false
    // 本会话是否已做过"回显核对"探测（每次写入都探测会多出同步 IPC，只需首次）
    private var inputBoxEchoProbed = false
    private var lastAmplitudeUpdate = 0L
    private var smoothedAmplitude = 0f
    private var smoothedSpectrum = FloatArray(16)
    // 抬手后进入"等完整 final"状态：partial 回调忽略，final 到则一次性上屏完整句
    private var suppressDuplicateFinal = false
    // 抬手后正在等待 final（兜底提交尚未执行）
    private var releaseAwaitingFinal = false
    // 超时兜底已把 partial 提交上屏
    private var releaseCommitted = false
    // 兜底提交的完整文本（含标点）与时间：极少数 final 晚于兜底到时，仅在能安全
    // 校验光标前缀的普通 app 内替换；Termux 等终端读不到前缀也删不准，一律不替换
    private var committedOnReleaseFull = ""
    private var committedOnReleaseAtMs = 0L
    // 输入法窗口隐藏等场景：丢弃本会话，迟到结果不得写入任何输入框
    private var sessionAbandoned = false
    // 抬手后录音线程仍在 finalize（采尾音/等模型/等 final 回调）：UI 保持"识别中"
    @Volatile
    private var releaseFinalizing = false

    fun isFinalizing(): Boolean = releaseFinalizing

    private var errorToast: Toast? = null

    private val releaseFallbackRunnable = Runnable {
        if (!releaseAwaitingFinal) return@Runnable
        val partial = lastPartialText
        val ic = getInputConnection()
        if (ic != null && partial.isNotEmpty()) {
            val punctuated = addPunctuation(partial)
            commitFinal(ic, punctuated, partial)
            committedOnReleaseFull = punctuated
            committedOnReleaseAtMs = System.currentTimeMillis()
            Log.d(TAG, "Release fallback committed partial after timeout: '$punctuated'")
        }
        releaseCommitted = true
        releaseAwaitingFinal = false
    }

    private fun resetReleaseState() {
        mainHandler.removeCallbacks(releaseFallbackRunnable)
        suppressDuplicateFinal = false
        releaseAwaitingFinal = false
        releaseCommitted = false
        releaseFinalizing = false
        committedOnReleaseFull = ""
    }

    /** 输入法隐藏/切换输入框时调用：丢弃当前会话的未识别文本，忽略迟到的最终结果 */
    fun abandonSession() {
        sessionAbandoned = true
        finishing = false
        mainHandler.removeCallbacks(finishTimeoutRunnable)
        lastPartialText = ""
        resetReleaseState()
    }

    // 语音按钮长按抬起时调用：不立即上屏 partial，而是等待完整 final（实测约 1.2s）
    // 一次性提交，避免"先上屏半句→删除重提"在 Termux 等终端删不干净产生重复文本；
    // final 超时丢失时由兜底 Runnable 提交最后 partial
    fun commitPendingOnRelease() {
        if (sessionAbandoned) return
        val partial = lastPartialText
        Log.d(TAG, "commitPendingOnRelease: ic=${getInputConnection() != null}, partial='$partial', suppress=$suppressDuplicateFinal")
        // 抬手后进入 finalize 等待（冷按 partial 为空也要等：final 是唯一上屏路径，
        // 录音线程有界等待模型 20s）；UI 据此保持"识别中"直到 final/错误
        releaseFinalizing = true
        if (partial.isEmpty()) return
        suppressDuplicateFinal = true
        releaseAwaitingFinal = true
        releaseCommitted = false
        committedOnReleaseFull = ""
        mainHandler.removeCallbacks(releaseFallbackRunnable)
        mainHandler.postDelayed(releaseFallbackRunnable, RELEASE_COMMIT_FALLBACK_MS)
    }

    private fun handleSpeechResult(text: String) {
        Log.d(TAG, "Speech result (final): $text")

        val wasFinishing = finishing
        if (finishing) {
            // 收尾中收到最终结果：取消超时兜底，正常提交完整结果
            finishing = false
            mainHandler.removeCallbacks(finishTimeoutRunnable)
        }

        if (sessionAbandoned) {
            sessionAbandoned = false
            lastPartialText = ""
            resetReleaseState()
            onVoiceComplete()
            return
        }

        if (releaseAwaitingFinal || releaseCommitted) {
            mainHandler.removeCallbacks(releaseFallbackRunnable)
            val cleanText = text.replace(" ", "").trim()
            if (!releaseCommitted) {
                // final 在兜底超时前到达：直接一次性提交完整句，不做任何删除操作，
                // Termux 等终端也安全
                val ic = getInputConnection()
                if (ic != null && cleanText.isNotEmpty() && !cleanText.startsWith("错误:")) {
                    commitFinal(ic, addPunctuation(cleanText), lastPartialText)
                }
            } else {
                // 兜底已提交 partial，final 更晚才到：仅在能读光标前缀的普通 app 替换
                replaceCommittedIfVerified(cleanText)
            }
            lastPartialText = ""
            resetReleaseState()
            onVoiceComplete()
            return
        }

        val cleanText = normalizeAsrText(text)
        val ic = getInputConnection()
        if (ic != null && cleanText.isNotEmpty() && !cleanText.startsWith("错误:")) {
            // 收尾期间应用可能已把我们上屏的内容取走（发送/清空输入框）：先对账，再按可见增量提交
            reconcileWithInputBox(ic)
            val visibleText = visibleEngineText(cleanText)
            if (visibleText.isNotEmpty()) {
                commitFinal(ic, addPunctuation(visibleText), lastWrittenText)
            } else {
                Log.d(TAG, "handleSpeechResult: 内容已被外部消费，跳过提交")
            }
        }
        lastPartialText = ""
        resetInputBoxBookkeeping()

        if (!wasFinishing) {
            // 用户尚未松手就收到 final：流式在线插件按句回调属正常行为，该句已上屏，
            // 会话继续，等松手才结束。绝不能触发 onVoiceComplete——它会把
            // isVoiceMode/voiceRecordingStarted 清零，松手时容器的停止条件
            // （isVoiceMode || isRecording）全部失效，录音线程会一直在后台运行。
            return
        }
        onVoiceComplete()
    }

    /**
     * 兜底已上屏 partial 后迟到的更完整 final：只在 InputConnection 能读回光标前缀、
     * 且前缀确实以已提交文本结尾时才删除重提。Termux 等终端 getTextBeforeCursor
     * 返回空串且 deleteSurroundingText 行为不可靠，直接放弃替换（保留兜底文本，
     * 宁可丢尾字也绝不产生重复乱序文本）。
     */
    private fun replaceCommittedIfVerified(cleanFinal: String) {
        val committed = committedOnReleaseFull
        if (committed.isEmpty()) return
        if (System.currentTimeMillis() - committedOnReleaseAtMs > REPLACE_WINDOW_MS) {
            Log.d(TAG, "Fuller final arrived too late; skip replace")
            return
        }
        if (cleanFinal.isEmpty() || cleanFinal.startsWith("错误:")) return
        val committedCore = committed.trimEnd(*"。！？，、；：,.!?;:".toCharArray())
        if (cleanFinal.length <= committedCore.length) return

        val ic = getInputConnection() ?: return
        val before = ic.getTextBeforeCursor(committed.length + 4, 0)?.toString()
        if (before.isNullOrEmpty() || !before.endsWith(committed)) {
            Log.d(TAG, "Fuller final but prefix unverifiable (beforeLen=${before?.length ?: -1}); skip replace")
            return
        }
        ic.finishComposingText()
        ic.deleteSurroundingText(committed.length, 0)
        val replacement = addPunctuation(cleanFinal)
        ic.commitText(replacement, 1)
        Log.d(TAG, "Replaced fallback text with verified final: '$committed' -> '$replacement'")
    }
    
    // 增量语音模式：先结束 composing，再只提交增量，避免重复与整段重写。
    private fun commitFinal(ic: InputConnection, finalText: String, partial: String) {
        ic.finishComposingText()
        if (partial.isNotEmpty() && finalText.startsWith(partial)) {
            val remainder = finalText.substring(partial.length)
            if (remainder.isNotEmpty()) {
                ic.commitText(remainder, 1)
            } else {
                Log.d(TAG, "commitFinal: remainder empty, only finished composing")
            }
        } else {
            // 最终结果与部分结果不一致：删除已上屏的部分，再提交完整结果
            if (partial.isNotEmpty()) {
                ic.deleteSurroundingText(partial.length, 0)
            }
            ic.commitText(finalText, 1)
        }
        Log.d(TAG, "commitFinal: final='$finalText', partial='$partial'")
    }

    /** 会话切换/提交结束时清理与输入框的记账。 */
    private fun resetInputBoxBookkeeping() {
        lastWrittenText = ""
        consumedEnginePrefix = ""
        writtenTextVerified = false
        inputBoxEchoProbed = false
    }

    /**
     * 首次写入后核对一次"应用是否真的回显我们的 composing 文本"，决定本会话是否允许
     * "被外部消费"判定：应用不回传文本（[getTextBeforeCursor] 拿不到我们写的内容）时，
     * 若还按"没读到 = 被清空"处理，会把后续识别内容全部丢弃。
     */
    private fun verifyInputBoxEcho(ic: InputConnection, written: String) {
        if (inputBoxEchoProbed || written.isEmpty()) return
        inputBoxEchoProbed = true
        val onScreen = runCatching {
            ic.getTextBeforeCursor(written.length, 0)?.toString()
        }.getOrNull() ?: return
        if (onScreen == written) {
            writtenTextVerified = true
        } else {
            Log.d(TAG, "输入框未回显语音 composing 文本，本会话不做外部消费判定")
        }
    }

    /**
     * 写入/提交前与输入框对账。
     *
     * 应用可以在输入法完全不知情的情况下取走已经上屏的语音文本（最典型：微信点发送后清空
     * 输入框）。而引擎的部分结果/最终结果是**整段累计**的，此时若继续整段写入，就会把已经
     * 发出去的内容连同新内容一起灌回输入框——真机现象即"点发送后继续说话，已发送文本又回到
     * 输入框"（2026-09-29 日志：单个会话内 partial 从"一个人"变为"一个人，两个人"，
     * 整段 setComposingText 写进已被清空的框）。
     *
     * 判定：光标前的文本比我们上次写入的更短，即"我们写的内容被外部移除"→ 把本会话已识别的
     * 引擎文本整段记为 [consumedEnginePrefix]，后续只写增量。仅在曾经核对成功过
     * （[writtenTextVerified]，证明该应用确实回显我们的文本）时才判定，避免应用不回传文本时误判丢字。
     */
    private fun reconcileWithInputBox(ic: InputConnection) {
        if (lastWrittenText.isEmpty()) return
        val onScreen = runCatching {
            ic.getTextBeforeCursor(lastWrittenText.length, 0)?.toString()
        }.getOrNull() ?: return
        if (onScreen == lastWrittenText) {
            writtenTextVerified = true
            return
        }
        if (!writtenTextVerified) return
        // 只有"输入框内容变短"（被清空/删除）才判定为被外部消费；变长或等长（用户编辑、
        // 应用替换成等长文本）时保守不动，避免误判丢字。
        if (onScreen.length >= lastWrittenText.length) return
        consumedEnginePrefix = lastPartialText
        // "撤销语音输入"按会话开始时的框长度计算删除量，外部清空后基线要跟着落到当前框内容
        textLengthBeforeVoiceInput = onScreen.length
        lastWrittenText = ""
        Log.d(TAG, "输入框已被外部清空/取走内容，本会话已识别文本记为已消费: '$consumedEnginePrefix'")
    }

    /**
     * 引擎结果按"已消费前缀"裁剪出真正该写进输入框的可见文本。
     * 同时去掉开头标点：被消费的那句已经带着标点发出去了，新句子不该以"，"开头。
     */
    private fun visibleEngineText(engineText: String): String {
        if (consumedEnginePrefix.isEmpty()) return engineText
        val visible = if (engineText.startsWith(consumedEnginePrefix)) {
            engineText.substring(consumedEnginePrefix.length)
        } else {
            // 引擎改写了已消费部分，无法可靠切分：整段写入以免丢内容（记日志便于真机排查）
            Log.d(TAG, "引擎结果不再以已消费前缀开头，按整段写入: '$engineText'")
            engineText
        }
        return visible.trimStart('，', '。', '、', ',', '.', '！', '？', '；', '：', ' ', '\n')
    }
    
    /**
     * 规整 ASR 文本：中日韩字符之间不使用空格；英文保留单词间空格，
     * 并补齐标点后缺失的空格；连续空白折叠为一个。
     */
    internal fun normalizeAsrText(text: String): String {
        if (text.isEmpty()) return ""
        var result = MULTI_SPACE_REGEX.replace(text, " ").trim()
        if (result.isEmpty()) return ""
        result = CJK_SPACE_REGEX.replace(result, "")
        result = CJK_PUNCT_SPACE_REGEX.replace(result, "")
        result = SPACE_BEFORE_PUNCT_REGEX.replace(result, "\$1")
        result = ASCII_PUNCT_FOLLOWED_LATIN_REGEX.replace(result, "\$1 ")
        result = PERIOD_FOLLOWED_UPPER_REGEX.replace(result, ". ")
        return result
    }

    private fun addPunctuation(text: String): String {
        val cleanText = normalizeAsrText(text)
        if (cleanText.isEmpty()) return text

        val last = cleanText.last()
        if (CJK_CHAR_REGEX.containsMatchIn(cleanText)) {
            // 中文/日文：沿用中文句读；末尾已有标点则不再追加，避免"。。"
            if (last in "。！？；：，、,.!?;:") return cleanText
            return "$cleanText${heuristicPunctuation(cleanText)}"
        }

        // 英文：模型若给出中文句末标点则转成半角；已有标点不重复追加
        if (last in "。！？，；：") {
            return cleanText.dropLast(1) + when (last) {
                '。' -> "."
                '！' -> "!"
                '？' -> "?"
                '，' -> ","
                '；' -> ";"
                else -> ":"
            }
        }
        if (last in ",.!?;:") return cleanText
        return cleanText + heuristicLatinPunctuation(cleanText)
    }

    private fun heuristicPunctuation(text: String): String {
        return when {
            text.any { it in "吗呢么吧" } || text.contains("什么") || text.contains("怎么") || text.contains("为什么") || text.contains("如何") || text.contains("哪") -> "？"
            text.length < 4 -> "，"
            else -> "。"
        }
    }

    private fun heuristicLatinPunctuation(text: String): String {
        val firstWord = text.trimStart()
            .substringBefore(' ')
            .trim('"', '\'', '(', '[')
            .lowercase()
        return if (firstWord in LATIN_QUESTION_STARTERS) "?" else "."
    }

    private fun handlePartialResult(text: String) {
        // 等待 final 期间继续让尾音 partial 刷新 composing：最终 final 多以 partial 为
        // 前缀，只需补提交增量（Termux 等终端 composing 已落盘时同样正确）；
        // 兜底已提交或会话丢弃后才屏蔽，避免污染已上屏文本
        if (sessionAbandoned || releaseCommitted) return
        if (text == lastPartialText) return
        lastPartialText = text
        Log.d(TAG, "Speech result (partial): $text")

        // 中文去掉字符间空格，英文保留词间空格；已被外部消费的前缀（如已发送内容）不再回写
        val cleanText = visibleEngineText(normalized)
        if (cleanText.isEmpty()) return
        
        if (ic != null) {
            onComposingWritten()
            ic.setComposingText(cleanText, 1)
            lastWrittenText = cleanText
            // 首次写入后回读一次，确认该应用确实回显 composing 文本（之后才允许做"被外部消费"判定）
            verifyInputBoxEcho(ic, cleanText)
        }
        onStateChanged(getState().copy(voiceRecognizedText = cleanText))
    }

    private fun handleSpeechStateChange(state: RecognitionState) {
        Log.d(TAG, "Speech state changed: $state")
        if (state == RecognitionState.LISTENING) {
            lastPartialText = ""
            sessionAbandoned = false
            resetReleaseState()
        } else if (state == RecognitionState.IDLE) {
            // AsrStop 线程保证 IDLE 在 final 回调之后：收尾完成，复位抬手等待态
            releaseFinalizing = false
        }
        // 收尾等待最终结果期间，引擎 stop 产生的 IDLE 不覆盖"正在识别..."显示
        if (finishing && state == RecognitionState.IDLE) return
        onStateChanged(getState().copy(voiceRecognitionState = state))
    }

    private fun handleSpeechError(error: String, userVisible: Boolean) {
        Log.e(TAG, "Speech error: $error")
        FileLogger.e(TAG, "Speech error: $error")
        val wasFinishing = finishing
        finishing = false
        mainHandler.removeCallbacks(finishTimeoutRunnable)
        lastPartialText = ""
        resetReleaseState()
        if (userVisible && error.isNotBlank()) {
            errorToast?.cancel()
            errorToast = Toast.makeText(context, error, Toast.LENGTH_LONG)
            errorToast?.show()
        }
        onVoiceComplete()
    }

    private fun handleAmplitudeUpdate(amplitude: Float) {
        val now = System.currentTimeMillis()
        if (now - lastAmplitudeUpdate < 80) return
        lastAmplitudeUpdate = now
        smoothedAmplitude = smoothedAmplitude * 0.45f + amplitude * 0.55f
        onAmplitudeChanged(smoothedAmplitude)
    }

    private fun handleSpectrumUpdate(spectrum: FloatArray) {
        val smoothed = smoothedSpectrum
        for (i in spectrum.indices) {
            smoothed[i] = smoothed[i] * 0.5f + spectrum[i] * 0.5f
        }
        onSpectrumChanged(smoothed.copyOf())
    }
}