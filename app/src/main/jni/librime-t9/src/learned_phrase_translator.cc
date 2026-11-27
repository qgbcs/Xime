#include "learned_phrase_translator.h"

#include <algorithm>
#include <cctype>
#include <mutex>
#include <set>
#include <unordered_map>
#include <utility>

#ifndef T9_ALGO_ONLY_BUILD
#include <rime/candidate.h>
#include <rime/context.h>
#include <rime/engine.h>
#include <rime/schema.h>
#include <rime/segmentation.h>
#endif

namespace rime {
namespace learned_phrase {
namespace {

// 学习词候选类型：翻译器注入的正常词 / 降频词分开标记，供 filter 识别。
constexpr const char* kLearnedType = "learned_phrase";
constexpr const char* kDemotedType = "learned_phrase_demoted";

// 三层质量基址，保证「用过 > 未用过 > 原生词典」：
//   - 用过的词（dee > 0）：1e9 + dee，dee 越大越靠前；
//   - 未用过（含旧数据迁移）：1e8 + base_weight；
//   - 降频（base_weight < 0）：极低质量，稳定沉到候选列表末尾。
constexpr double kUsedQualityBase = 1.0e9;
constexpr double kUnusedQualityBase = 1.0e8;
constexpr double kDemotedQuality = -1.0e12;
// 单次最多注入：用过 / 未用过 / 降频各 8 条。
constexpr size_t kMaxUsedCandidates = 8;
constexpr size_t kMaxUnusedCandidates = 8;
constexpr size_t kMaxDemotedCandidates = 8;

std::mutex& SnapshotMutex() {
  static std::mutex m;
  return m;
}

std::unordered_map<std::string, std::vector<Entry>>& SnapshotMap() {
  static std::unordered_map<std::string, std::vector<Entry>> m;
  return m;
}

std::string LowerAscii(std::string s) {
  for (auto& c : s) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return s;
}

bool StartsWith(const std::string& text, const std::string& prefix) {
  return text.size() >= prefix.size() &&
         text.compare(0, prefix.size(), prefix) == 0;
}

bool IsLearnedType(const std::string& type) {
  return type == kLearnedType || type == kDemotedType;
}

}  // namespace

void SetSnapshot(const std::string& schema_id, std::vector<Entry> entries) {
  std::lock_guard<std::mutex> lock(SnapshotMutex());
  if (entries.empty()) {
    SnapshotMap().erase(schema_id);
  } else {
    SnapshotMap()[schema_id] = std::move(entries);
  }
}

void ClearSnapshot() {
  std::lock_guard<std::mutex> lock(SnapshotMutex());
  SnapshotMap().clear();
}

std::vector<Entry> Matching(const std::string& schema_id,
                            const std::string& input) {
  const std::string key = LowerAscii(input);
  if (key.empty()) {
    return {};
  }
  std::vector<Entry> entries;
  {
    std::lock_guard<std::mutex> lock(SnapshotMutex());
    auto it = SnapshotMap().find(schema_id);
    if (it == SnapshotMap().end()) {
      return {};
    }
    entries = it->second;
  }

  // 用过的词优先 → dee 降序 → commits 降序 → base_weight 降序 → 编码稳定排序。
  std::sort(entries.begin(), entries.end(), [](const Entry& a, const Entry& b) {
    const bool a_used = a.dee > 0.0;
    const bool b_used = b.dee > 0.0;
    if (a_used != b_used) return a_used;
    if (a_used && a.dee != b.dee) return a.dee > b.dee;
    if (a.commits != b.commits) return a.commits > b.commits;
    if (a.base_weight != b.base_weight) return a.base_weight > b.base_weight;
    return a.code < b.code;
  });

  std::vector<Entry> matched;
  std::set<std::string> seen_words;
  for (const auto& entry : entries) {
    if (!StartsWith(LowerAscii(entry.code), key)) {
      continue;
    }
    if (!seen_words.insert(entry.word).second) {
      continue;
    }
    matched.push_back(entry);
  }
  return matched;
}

#ifndef T9_ALGO_ONLY_BUILD

// ── 流式去重 / 降频抑制翻译包装 ──
class LearnedPhraseTranslation : public Translation {
 public:
  LearnedPhraseTranslation(an<Translation> translation, set<string> demoted)
      : translation_(std::move(translation)), demoted_(std::move(demoted)) {
    Advance();
  }

  bool Next() override {
    if (exhausted()) {
      return false;
    }
    if (!translation_->Next()) {
      cand_ = nullptr;
      set_exhausted(true);
      return false;
    }
    Advance();
    return !exhausted();
  }

  an<Candidate> Peek() override { return cand_; }

 private:
  void Advance() {
    while (!translation_->exhausted()) {
      auto cand = translation_->Peek();
      if (!cand) {
        break;
      }
      const string& text = cand->text();
      if (IsLearnedType(cand->type())) {
        const bool demoted_copy = cand->type() == kDemotedType;
        // 正常学习词若已有同名词（原生副本排在前面）则丢弃本条；
        // 降频副本必须保留，原生同名副本由下方 demoted_ 分支丢弃。
        if (!demoted_copy && !emitted_.insert(text).second) {
          if (!translation_->Next()) break;
          continue;
        }
        emitted_.insert(text);
        cand_ = cand;
        return;
      }
      if (demoted_.find(text) != demoted_.end()) {
        // 降频词的原生副本丢弃，只留翻译器注入的沉底副本。
        if (!translation_->Next()) break;
        continue;
      }
      if (!emitted_.insert(text).second) {
        // 与学习词同名的原生副本丢弃，避免候选栏出现重复词。
        if (!translation_->Next()) break;
        continue;
      }
      cand_ = cand;
      return;
    }
    cand_ = nullptr;
    set_exhausted(true);
  }

  an<Translation> translation_;
  an<Candidate> cand_;
  set<string> demoted_;
  set<string> emitted_;
};

LearnedPhraseTranslator::LearnedPhraseTranslator(const Ticket& ticket)
    : Translator(ticket) {
  if (ticket.schema) {
    schema_id_ = ticket.schema->schema_id();
  }
}

an<Translation> LearnedPhraseTranslator::Query(const string& input,
                                               const Segment& segment) {
  if (input.empty()) {
    return nullptr;
  }
  auto matched = learned_phrase::Matching(schema_id_, input);
  if (matched.empty()) {
    return nullptr;
  }

  auto result = New<FifoTranslation>();
  size_t used_count = 0;
  size_t unused_count = 0;
  size_t demoted_count = 0;
  for (const auto& entry : matched) {
    const bool demoted = entry.base_weight < 0;
    double quality = kDemotedQuality;
    if (demoted) {
      if (demoted_count >= kMaxDemotedCandidates) continue;
      ++demoted_count;
    } else if (entry.dee > 0.0) {
      if (used_count >= kMaxUsedCandidates) continue;
      ++used_count;
      quality = kUsedQualityBase + entry.dee;
    } else {
      if (unused_count >= kMaxUnusedCandidates) continue;
      ++unused_count;
      quality = kUnusedQualityBase + static_cast<double>(entry.base_weight);
    }
    auto cand = New<SimpleCandidate>(demoted ? kDemotedType : kLearnedType,
                                     segment.start, segment.end, entry.word);
    cand->set_quality(quality);
    result->Append(cand);
  }
  return result->exhausted() ? nullptr : result;
}

LearnedPhraseFilter::LearnedPhraseFilter(const Ticket& ticket)
    : Filter(ticket) {
  if (ticket.schema) {
    schema_id_ = ticket.schema->schema_id();
  }
}

an<Translation> LearnedPhraseFilter::Apply(an<Translation> translation,
                                           CandidateList* /*candidates*/) {
  if (!translation || translation->exhausted()) {
    return translation;
  }

  string input;
  if (engine_) {
    if (auto* context = engine_->context()) {
      input = context->input();
    }
  }
  if (input.empty()) {
    return translation;
  }

  auto matched = learned_phrase::Matching(schema_id_, input);
  if (matched.empty()) {
    return translation;
  }

  set<string> demoted;
  for (const auto& entry : matched) {
    if (entry.base_weight < 0) {
      demoted.insert(entry.word);
    }
  }
  return New<LearnedPhraseTranslation>(translation, std::move(demoted));
}

#endif  // T9_ALGO_ONLY_BUILD

}  // namespace learned_phrase
}  // namespace rime
