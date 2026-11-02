package com.kingzcheung.xime.settings

import android.content.Context
import androidx.compose.runtime.mutableLongStateOf
import com.kingzcheung.xime.rime.RimeEngine
import com.kingzcheung.xime.util.FileLogger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * 用户候选词的"学习 + 管理"统一入口，分两条独立链路：
 *
 * 1) 自动学习词（回车原始上屏的英文串、完整选中的中英文候选）：
 *    由本管理器在内存中直管（[getAutoPhrases]/add/delete/moveToEnd），
 *    按输入编码前缀匹配，以共享频率分值注入候选栏；点击应用层词条时直接上屏。
 *    独立持久化到 app filesDir（auto_phrase/<schema>.txt），rime 完全不加载，
 *    因此增删改立即生效，且绝不销毁/重建 rime 会话、不持有引擎全局锁
 *    （根除重建会话导致的卡死、丢候选、第二次无候选）。
 *
 * 2) 手动 custom_phrase（设置页维护，db_class=stabledb）：
 *    仍由 rime table_translator 在会话启动时一次性载入，手动删除/降频后
 *    需要重建会话才生效（低频操作，沿用防抖重建）。
 */
object UserPhraseManager {
    private const val TAG = "UserPhraseManager"
    /** 原始回车学习的最小长度，避免 a、ok 这类短串污染词库。 */
    private const val MIN_AUTO_LEARN_LEN = 3
    /** 手动 custom_phrase 的沉底权重。 */
    private const val BOTTOM_WEIGHT = -100000
    private const val DEPLOY_DEBOUNCE_MS = 2000L
    /** 每段（前置/降频）最多注入候选栏的自动词数量。 */
    private const val MAX_INJECT_COUNT = 8
    /** 最小负分即可把学习词从前排移到当前 Rime 候选之后。 */
    private const val DEMOTED_SCORE = -1L
    /** 每点击上屏一次自动词，其分值加一；降频词再次选中一次即可恢复到非负分。 */
    private const val SELECT_BUMP = 1L

    private val ioScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val fileMutex = Mutex()
    @Volatile private var deployScheduled = false

    // ── 自动学习词：内存直管，独立存储，不经过 rime 会话 ──
    /** 中英文共用同一按输入编码关联的频率分值；负分词追加到 Rime 候选之后。 */
    /** 前缀匹配结果：[front] 为前排词，[tail] 为降频后置于当前 Rime 候选之后的词。 */
    class AutoMatch(val front: List<String>, val tail: List<String>)

    /** schemaId -> 词条（始终按分值降序、同分稳定保序）；所有读写在 object 监视器内同步。 */
    private val autoCache = HashMap<String, List<LearnedCandidateEntry>>()
    private val autoSaveMutex = Mutex()

    /**
     * 自定义词磁盘内容修订号：学习 / 删除 / 降频写盘成功后（在 [fileMutex] 内）递增。
     * 候选栏观察此值并重新加载词条集合，从而即时刷新长按菜单的可用性。
     * 用 Compose state 承载，可在任意线程写、在组合中读。
     */
    val phraseRevision = mutableLongStateOf(0L)
    private fun bumpPhraseRevision() { phraseRevision.longValue += 1L }

    /** 当前方案可长按管理的全部词条：手动 custom_phrase + 自动学习词。 */
    fun loadWords(context: Context, schemaId: String?): Set<String> {
        if (schemaId.isNullOrBlank()) return emptySet()
        return try {
            val manual = PersonalDictManager.loadCustomPhrases(context, schemaId).map { it.word }
            (manual + getAutoPhrases(context, schemaId)).toSet()
        } catch (_: Exception) {
            emptySet()
        }
    }

    /**
     * 回车未选词、原始上屏时学习该串。
     * 仅学习以字母为主、长度达标的串；直接写入内存并异步落盘，立即生效，不重建 rime 会话。
     * 返回 true 表示受理了学习。
     */
    fun learnRawCommit(context: Context, schemaId: String?, raw: String): Boolean {
        val word = raw.trim()
        if (schemaId.isNullOrBlank() || word.length < MIN_AUTO_LEARN_LEN) return false
        // 以英文字母/数字为主的串才学习（允许内部 - _ . 等连接符，但首字符须是字母）
        if (!word.first().isLetter() || word.count { it.isLetterOrDigit() } < word.length * 0.6f) return false

        addAutoPhrase(context, schemaId, word, word)
        return true
    }

    /** 完整选词上屏后记录输入码与词条，中文与英文使用同一频率分值。 */
    fun learnCandidateSelection(
        context: Context,
        schemaId: String?,
        inputCode: String,
        word: String,
    ): Boolean {
        val code = inputCode.trim().lowercase()
        if (schemaId.isNullOrBlank() || !isLearnableInputCode(code) || !isLearnableWord(word)) return false
        updateLearnedPhrase(context, schemaId, code, word, bumpExisting = true)
        return true
    }

    /** 删除一个自定义词条；找不到则忽略。 */
    fun deleteWord(context: Context, schemaId: String?, word: String, onDone: () -> Unit = {}) {
        if (schemaId.isNullOrBlank()) { onDone(); return }
        val app = context.applicationContext
        ioScope.launch {
            var changed = false
            fileMutex.withLock {
                val entries = PersonalDictManager.loadCustomPhrases(app, schemaId).toMutableList()
                changed = entries.removeAll { it.word == word }
                if (changed) {
                    PersonalDictManager.saveCustomPhrases(app, schemaId, entries)
                    bumpPhraseRevision()
                }
            }
            if (changed) scheduleReload(app, schemaId)
            withContext(Dispatchers.Main) { onDone() }
        }
    }

    /** 把一个自定义词条的权重调到极小，使其在同码候选中稳定沉到末尾。 */
    fun moveToEnd(context: Context, schemaId: String?, word: String, onDone: () -> Unit = {}) {
        if (schemaId.isNullOrBlank()) { onDone(); return }
        val app = context.applicationContext
        ioScope.launch {
            var changed = false
            fileMutex.withLock {
                val entries = PersonalDictManager.loadCustomPhrases(app, schemaId).toMutableList()
                val idx = entries.indexOfFirst { it.word == word }
                if (idx >= 0) {
                    val e = entries[idx]
                    if (e.weight != BOTTOM_WEIGHT) {
                        entries[idx] = e.copy(weight = BOTTOM_WEIGHT)
                        PersonalDictManager.saveCustomPhrases(app, schemaId, entries)
                        changed = true
                        bumpPhraseRevision()
                    }
                }
            }
            if (changed) scheduleReload(app, schemaId)
            withContext(Dispatchers.Main) { onDone() }
        }
    }

    // ── 自动学习词内存操作（即时，不碰 rime 会话/全局锁）──

    private fun isLearnableInputCode(code: String): Boolean =
        code.isNotEmpty() && code.any(Char::isLetter) &&
            code.all { it.isLetterOrDigit() || it in "'’-_ ." }

    private fun isLearnableWord(word: String): Boolean {
        var offset = 0
        while (offset < word.length) {
            val codePoint = word.codePointAt(offset)
            if (Character.isLetter(codePoint) && Character.UnicodeScript.of(codePoint) in
                setOf(Character.UnicodeScript.LATIN, Character.UnicodeScript.HAN)
            ) {
                return true
            }
            offset += Character.charCount(codePoint)
        }
        return false
    }

    /** 读取当前方案的自动词条目（分值降序），首次访问从磁盘载入内存。 */
    @Synchronized
    private fun getEntries(context: Context, schemaId: String): List<LearnedCandidateEntry> {
        if (schemaId.isBlank()) return emptyList()
        autoCache[schemaId]?.let { return it }
        val loaded = PersonalDictManager.loadAutoPhrases(context.applicationContext, schemaId)
            .mapNotNull(LearnedCandidateIndex::parse)
            .let(LearnedCandidateIndex::ordered)
        autoCache[schemaId] = loaded
        return loaded
    }

    /** 读取当前方案的自动学习词（分值降序），首次访问从磁盘载入内存。 */
    @Synchronized
    fun getAutoPhrases(context: Context, schemaId: String): List<String> {
        if (schemaId.isBlank()) return emptyList()
        return getEntries(context, schemaId).map { it.word }.distinct()
    }

    /** 新增自动词：使用输入编码与词条组成身份；新词取共用频率最高分 +1。 */
    @Synchronized
    fun addAutoPhrase(context: Context, schemaId: String, word: String, inputCode: String = word) {
        updateLearnedPhrase(context, schemaId, inputCode.trim().lowercase(), word, bumpExisting = false)
    }

    @Synchronized
    private fun updateLearnedPhrase(
        context: Context,
        schemaId: String,
        inputCode: String,
        word: String,
        bumpExisting: Boolean,
    ) {
        val current = getEntries(context, schemaId)
        val updated = LearnedCandidateIndex.learn(current, inputCode, word, bumpExisting)
        if (updated == current) return
        autoCache[schemaId] = updated
        bumpPhraseRevision()
        persistAutoAsync(context, schemaId)
        val score = updated.first {
            it.inputCode.equals(inputCode, ignoreCase = true) && it.word == word
        }.score
        FileLogger.i(TAG, "learned candidate: $word code=$inputCode score=$score (schema=$schemaId)")
    }

    /** 删除自动词：内存立即移除并异步落盘。 */
    @Synchronized
    fun deleteAutoWord(context: Context, schemaId: String, word: String) {
        val cur = getEntries(context, schemaId)
        if (cur.none { it.word == word }) return
        autoCache[schemaId] = cur.filterNot { it.word == word }
        bumpPhraseRevision()
        persistAutoAsync(context, schemaId)
    }

    /**
     * 降低自动词频率：分值置为 [DEMOTED_SCORE]（负分），排到 Rime 当前页候选之后。
     * 以后点击该词每次加 [SELECT_BUMP]，分值回到 0 即重新前置注入（多次使用逐步提升）。
     */
    @Synchronized
    fun moveAutoWordToSecondPage(context: Context, schemaId: String, word: String) {
        val cur = getEntries(context, schemaId)
        if (cur.none { it.word == word && it.score != DEMOTED_SCORE }) return
        val next = LearnedCandidateIndex.demoteWord(cur, word, DEMOTED_SCORE)
        autoCache[schemaId] = next
        bumpPhraseRevision()
        persistAutoAsync(context, schemaId)
    }

    /** 点击上屏自动词后调用：分值 +[SELECT_BUMP] 并重排（落盘异步），使常用词逐步靠前。 */
    @Synchronized
    fun bumpAutoWordOnPick(context: Context, schemaId: String, word: String, input: String = "") {
        val cur = getEntries(context, schemaId)
        val next = LearnedCandidateIndex.bumpWord(cur, input, word, SELECT_BUMP)
        if (next == cur) return
        autoCache[schemaId] = next
        persistAutoAsync(context, schemaId)
    }

    /** 判断某词是否为自动学习词（供长按动作路由）。 */
    @Synchronized
    fun isAutoWord(context: Context, schemaId: String, word: String): Boolean =
        getEntries(context, schemaId).any { it.word == word }

    /**
     * 前缀匹配：
     * - front：分值 >= 0 的词，按分值降序，注入候选栏前部；
     * - tail：负分（降频）词，按分值升序，追加到当前 Rime 候选之后。
     */
    @Synchronized
    fun matchAutoPhrases(context: Context, schemaId: String, input: String): AutoMatch {
        val matched = LearnedCandidateIndex.matching(getEntries(context, schemaId), input)
        val front = matched.filter { it.score >= 0L }.take(MAX_INJECT_COUNT).map { it.word }
        // 缓存按降序存储，负分段反转即得升序（-1 在前、DEMOTED_SCORE 在最后）。
        val tail = matched.filter { it.score < 0L }.asReversed().take(MAX_INJECT_COUNT).map { it.word }
        return AutoMatch(front, tail)
    }

    data class LearnedCandidateDiagnostic(val inputCode: String, val word: String, val score: Long)

    @Synchronized
    fun matchingLearnedCandidates(
        context: Context,
        schemaId: String,
        input: String,
    ): List<LearnedCandidateDiagnostic> =
        LearnedCandidateIndex.matching(getEntries(context, schemaId), input).map {
            LearnedCandidateDiagnostic(it.inputCode, it.word, it.score)
        }

    /** 异步落盘：锁内保存内存中的最新快照（word<TAB>score 每行），保证快速连续修改后最终一致。 */
    private fun persistAutoAsync(context: Context, schemaId: String) {
        val app = context.applicationContext
        ioScope.launch {
            autoSaveMutex.withLock {
                val snapshot = synchronized(this@UserPhraseManager) {
                    autoCache[schemaId].orEmpty().map(LearnedCandidateIndex::serialize)
                }
                PersonalDictManager.saveAutoPhrases(app, schemaId, snapshot)
            }
        }
    }

    /**
     * 合并重载：把多次文件改动收敛为一次 Rime 会话重建。
     * 先确保 custom_phrase 翻译器补丁已注入当前方案，再重建会话让 translator
     * 重新打开纯文本 custom_phrase.txt（stabledb 无需编译部署）。
     */
    private fun scheduleReload(app: Context, schemaId: String) {
        if (deployScheduled) return
        deployScheduled = true
        ioScope.launch {
            delay(DEPLOY_DEBOUNCE_MS)
            deployScheduled = false
            try {
                PersonalDictManager.ensureSchemaPack(app, schemaId)
                val ok = if (RimeEngine.isInitialized()) RimeEngine.getInstance().reloadStableUserDict() else false
                FileLogger.i(TAG, "custom phrase reload schema=$schemaId ok=$ok")
            } catch (e: Exception) {
                FileLogger.w(TAG, "custom phrase reload failed: ${e.message}")
            }
        }
    }
}
