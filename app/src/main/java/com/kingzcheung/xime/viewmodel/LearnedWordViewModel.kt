package com.kingzcheung.xime.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.kingzcheung.xime.settings.SchemaManager
import com.kingzcheung.xime.settings.UserDictIoManager
import com.kingzcheung.xime.settings.UserPhraseManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

/** 学习词来源：Rime 用户词典（userdb）或 App 自动学习表（auto_phrase）。 */
enum class LearnedWordSource(val label: String) {
    UserDict("用户词典"),
    AutoPhrase("自动学习"),
}

/** 一条学习词。分值随来源不同：userdb 是词频，auto_phrase 是学习分。 */
data class LearnedWordEntry(
    val word: String,
    val code: String,
    val score: Long,
    val source: LearnedWordSource,
)

data class LearnedWordUiState(
    val isLoading: Boolean = false,
    val schemaId: String = "",
    val userDictName: String? = null,
    val entries: List<LearnedWordEntry> = emptyList(),
    val filteredEntries: List<LearnedWordEntry> = emptyList(),
    val searchQuery: String = "",
)

/**
 * 「词库管理 → 学习词」页签：只读汇总输入过程中产生的词。
 *
 * - `<词典名>.userdb`：librime 用户词典（原生分段选词提交后由引擎写入）；
 * - `auto_phrase/<方案>.txt`：App 自己的学习表（候选栏前置/沉底注入用）。
 *
 * 读取失败一律降级为空列表（只影响本页展示，不影响输入主流程）。
 */
class LearnedWordViewModel(application: Application) : AndroidViewModel(application) {
    private val context = application.applicationContext

    private val _uiState = MutableStateFlow(LearnedWordUiState())
    val uiState: StateFlow<LearnedWordUiState> = _uiState.asStateFlow()

    private var schemaId: String = ""

    /** 切到本页签或切换方案时调用；[force] 为 true 时即便方案未变也重读（刚学完新词）。 */
    fun setSchema(id: String?, force: Boolean = false) {
        if (id.isNullOrBlank()) return
        if (!force && id == schemaId) return
        schemaId = id
        reload()
    }

    fun reload() {
        val sid = schemaId
        if (sid.isBlank()) return
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, schemaId = sid) }
            val loaded = withContext(Dispatchers.IO) { readAll(sid) }
            _uiState.update {
                if (it.schemaId != sid) {
                    it
                } else {
                    it.copy(
                        isLoading = false,
                        userDictName = loaded.second,
                        entries = loaded.first,
                        filteredEntries = filterLearnedWordEntries(loaded.first, it.searchQuery),
                    )
                }
            }
        }
    }

    /** 返回（词条列表，当前方案引用的用户词典名）。userdb 优先，各自按分值降序。 */
    private fun readAll(sid: String): Pair<List<LearnedWordEntry>, String?> {
        val auto = runCatching {
            UserPhraseManager.getAutoPhraseEntries(context, sid).map {
                LearnedWordEntry(it.word, it.inputCode, it.score, LearnedWordSource.AutoPhrase)
            }
        }.getOrDefault(emptyList())
        val dictName = runCatching { SchemaManager.getReferencedDictName(context, sid) }.getOrNull()
        val fromUserDb = if (dictName == null) {
            emptyList()
        } else {
            runCatching {
                UserDictIoManager.readEntries(dictName).map {
                    LearnedWordEntry(it.word, it.code, (it.weight ?: 0).toLong(), LearnedWordSource.UserDict)
                }
            }.getOrDefault(emptyList())
        }
        return mergeLearnedWordEntries(fromUserDb, auto) to dictName
    }

    fun setSearchQuery(query: String) {
        _uiState.update { it.copy(searchQuery = query, filteredEntries = filterLearnedWordEntries(it.entries, query)) }
    }

    fun clearSearch() = setSearchQuery("")

}

/** 学习词合并顺序：userdb 在前、auto 在后，各自按分值降序。 */
fun mergeLearnedWordEntries(
    userDictEntries: List<LearnedWordEntry>,
    autoEntries: List<LearnedWordEntry>,
): List<LearnedWordEntry> =
    userDictEntries.sortedByDescending { it.score } + autoEntries.sortedByDescending { it.score }

/** 学习词搜索：词或编码（忽略大小写）包含查询串即命中；空查询返回全部。 */
fun filterLearnedWordEntries(
    entries: List<LearnedWordEntry>,
    query: String,
): List<LearnedWordEntry> {
    if (query.isEmpty()) return entries
    val lower = query.lowercase(Locale.ROOT)
    return entries.filter {
        it.word.contains(query) || it.code.contains(query, ignoreCase = true) ||
            it.code.lowercase(Locale.ROOT).contains(lower)
    }
}
