#ifndef RIME_LEARNED_PHRASE_TRANSLATOR_H_
#define RIME_LEARNED_PHRASE_TRANSLATOR_H_

#include <string>
#include <vector>

#ifndef T9_ALGO_ONLY_BUILD
#include <rime/common.h>
#include <rime/filter.h>
#include <rime/translator.h>
#include <rime/translation.h>
#endif

namespace rime {
namespace learned_phrase {

// 一条应用学习词记录，调频模型对齐 RIME userdb：
//   base_weight：未用过词的原始权重（<0 表示已降频）
//   commits    ：累计选中次数
//   dee        ：衰减权重（0 = 从未用过）
//   last_tick  ：最近一次更新的全局 tick
struct Entry {
  std::string code;
  std::string word;
  long long base_weight = 0;
  long long commits = 0;
  double dee = 0.0;
  long long last_tick = 0;
};

// ── 侧信道（Kotlin → native）──
// Kotlin 在加载 / 学习 / 删除 / 降频 / 切换方案时，把
// auto_phrase/<schema>.txt 的全量快照推送到进程内缓存（整体替换）。
// 空列表等价于清空该方案。
void SetSnapshot(const std::string& schema_id, std::vector<Entry> entries);
void ClearSnapshot();

// 返回当前输入前缀命中的学习词：按分值降序、按词去重。
std::vector<Entry> Matching(const std::string& schema_id,
                            const std::string& input);

#ifndef T9_ALGO_ONLY_BUILD

// 与 t9_filter 同级注册的原生翻译器：把学习词作为 Rime 候选注入，
// 参与 Menu 的统一排序（MergedTranslation 按 Candidate::quality 交错）。
class LearnedPhraseTranslator : public Translator {
 public:
  explicit LearnedPhraseTranslator(const Ticket& ticket);

  an<Translation> Query(const string& input,
                        const Segment& segment) override;

 private:
  std::string schema_id_;
};

// 学习词去重 / 降频抑制器：
//   - 原生词典里与学习词同名的候选只保留一条（学习词优先）；
//   - 已降频（score < 0）的词，丢弃原生词典里的同名候选，只保留翻译器
//     注入的极低质量副本，使其稳定沉到候选列表末尾。
class LearnedPhraseFilter : public Filter {
 public:
  explicit LearnedPhraseFilter(const Ticket& ticket);

  an<Translation> Apply(an<Translation> translation,
                        CandidateList* candidates) override;

 private:
  std::string schema_id_;
};

#endif  // T9_ALGO_ONLY_BUILD

}  // namespace learned_phrase
}  // namespace rime

#endif  // RIME_LEARNED_PHRASE_TRANSLATOR_H_
