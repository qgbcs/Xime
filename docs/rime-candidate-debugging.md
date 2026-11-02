# Rime 候选翻页故障排查与回归测试

本文记录简体拼音多页候选选择问题的排查过程、根因、修复方式与桌面回归测试范围。内容只描述程序行为，不包含任何测试设备识别信息。


## 案例 1：多页候选与选词状态不同步

## 问题表现

简体拼音输入 `fu` 后，候选词可以翻到第二页，但点击第二页靠后位置的候选时，可能选中第一页相同位置的词，而不是屏幕上点击的词。排查时以第二页候选“芙”作为可复现例子。这不是单纯的候选排序问题：画面仍显示第二页，而 Rime 内部候选菜单已经回到第一页，UI 与引擎状态不同步。

本次定位与修正的是这条多页选词路径。它不代表简体拼音的英文历史输入、直接推荐词、联想模型或所有候选来源都已逐一修复；这些需要按各自的数据来源另行验证。

## 排查经过与根因

1. 先通过输入、翻页、候选选择与状态快照接口，沿用正式 IME 按键/选词流程检查输入串、当前页码、候选清单、Rime 原始候选和最终提交文字。这使故障可以在不依赖触屏坐标的情况下观察和复现。
2. 对照界面候选与 Rime 当前页的候选身份，发现选词前还会执行“悬空尾码”探测：`trimDanglingTailAndPick()` 暂时通过 `setInput()` 试算去掉尾部字母后，候选菜单是否改变。
3. Rime 的 `set_input` 会重建输入和菜单，并将候选页重置到第 0 页。探测流程虽然恢复了原始输入，却没有恢复原候选页；因此界面第二页的“芙”仍被传入选词流程时，引擎实际上已经位于第一页。
4. 选词时的候选身份检查拒绝了当前页不存在的“芙”，避免直接把同一位置的其他词选上屏。诊断因此显示候选未被接受、没有对应的 Rime commit，而不是成功提交了错词。这个保护避免错选，但也暴露了页面状态没有还原的根因。

## 修复与防错机制

- **探测流程恢复页码**：选词前保存 Rime 的原始 `page_no`。每次用 `setInput()` 探测或重建输入后，通过 `pageDown()` 回到原页；如果无法恢复，就还原原始输入并退出这条探测路径，不把过期页面的候选索引交给 Rime。
- **按候选身份解析，不盲信 UI 索引**：JNI 读取当前 `RimeContext` 的页码、每页候选数及候选的文字/注释。存在预期候选身份时，先在引擎当前页寻找匹配项；找不到就拒绝选择。找到后以 `page_no * page_size + 当前页索引` 计算 Rime 全局候选索引。
- **保护 UI 过滤/重排情况**：应用层候选清单可能包含前置学习词或被过滤/重排的词；选词时按候选文字对照 Rime 原始候选，而不是假定 UI 索引必然等于原始索引。若 Rime 未接受已验证的候选，或提交内容与已验证候选不一致，则清除组字并提交该候选文字，同时在结果中标记 fallback。
- **诊断状态可观察**：`imeState()` 快照包含方案、方案显示名称、键盘布局、Rime 初始化状态、应用候选、原始 Rime 候选及页码/页大小。选词诊断包含接受状态、Rime commit、实际提交文字及 fallback 标记。`buildInfo()` 提供版本、Git hash 和确定性源码指纹，避免只用安装时间推断 APK 是否包含最新程序。
- **初始化和失败状态保持一致**：Rime session 就绪前，方案名称可由偏好设置中的方案 ID 与内置 schema 资源取得；输入模式切换失败时，在主线程显示提示，并将乐观更新的 UI 模式恢复到切换前状态。

## 排查心得

- 候选栏显示的是 UI 状态，真正选词发生在 Rime 会话中；调试时必须同时记录“用户看到的候选”和“引擎当前页的候选/页码”。只观察最终点击索引，很容易把状态不同步误判成点击坐标错误或候选排序错误。
- `setInput()` 看起来只是恢复同一串编码，但对 Rime 来说是重建输入状态的操作。凡是选词前会暂时改写输入的探测，都必须同时保存并恢复会话中依赖的其他状态（本问题中最重要的是页码）；否则即使输入文本一致，也不代表菜单状态一致。
- 候选索引只在对应的候选列表和页状态中有意义。候选经过 UI 过滤、注入或重排后，不应把展示位置直接当成 Rime 原始索引；传递候选身份并在引擎当前菜单中重新解析，可以将状态错误转化为明确拒绝，而不是静默地选中另一个词。
- 桌面回归测试把失败条件固定成候选内容、页码和实际 commit，运行不依赖 Android，也不依赖使用者词典。这让核心分页/索引问题可以快速复现；同时必须清楚它不能替代 Android 触控与完整输入体验的验证。

## 不依赖 Android 的手动回归测试

常规 `assembleDebug`、`assembleRelease` 及 `./debug_build_secexp.sh` 均不会执行此测试。测试需由开发者明确运行，不会在 Gradle 构建阶段自动触发：

```bash
./scripts/test_rime_keys.sh
```

Linux 环境需安装 `librime-dev`、`pkg-config`、CMake 与 C++17 编译器。脚本使用 `pkg-config` 找到系统 librime，在临时目录构建 C++ 测试程序，并将 Rime 用户数据放入同一临时目录；结束时会清理该目录。不需要 Android SDK、模拟器或 APK。

测试数据位于 `scripts/rime_keytest/data`，使用固定 schema 和候选，避免依赖用户词典或真实设备数据。测试会确认：

| 范围 | 验证内容 |
| --- | --- |
| 多页选词 | `aa` 的第 0、1、2 页；页码、每页 5 个候选及末页标记 |
| 候选身份 | 重复文字可用注释区分；当前页不存在的旧候选身份会被拒绝 |
| 全局索引 | 选择后将页码与当页索引转换为正确的 Rime 全局索引，提交文字与点选候选一致 |
| 实际回归 | `fu` 翻至第 2 页，确认“芙”存在；模拟 `set_input` 探测使页码归零，再恢复第 2 页并选中“芙” |

手动运行成功时，程序会输出 `PASS: key input, three-page candidate navigation, and identity-based selection`。失败时脚本会以非零状态结束并输出失败断言；不会以跳过或假成功代替测试。

## 测试边界

这是 **librime/JNI 共用选词规则的桌面回归测试**，不是完整 Android UI 自动化。它不覆盖 Compose 触控坐标、系统 IME 窗口、所有简体拼音词库数据、英文历史输入、直接推荐词/联想排序，也不能单独证明某个 APK 的 Android 集成正常。必要时可在已授权的运行环境中使用 `RpcUiController.imeType()`、`imePageDown()`、`imePick()`、`imeState()` 和 `buildInfo()` 检查正式输入路径；RPC 诊断同样不会由正常构建自动运行。

项目中的 Kotlin/JVM 单元测试和 Android 仪器测试是另一套测试：可按需通过 `./run_tests.sh unit`、`./run_tests.sh instrumented` 显式启动；完整测试工作流也有单独的手动触发入口。它们不等同于本文的 Rime 多页候选回归，也不属于 `assembleDebug` / `assembleRelease` 的依赖任务。

构建与测试是两个刻意分开的动作：构建用于生成 APK；只有明确运行上面的脚本，才会执行本文所述候选回归。

`source_fingerprint` 的哈希输入由 `app/build.gradle.kts` 明确列出：`app/src/main/java`、`app/src/main/jni/librime_jni`、`app/src/main/python`、`app/src/main/assets`、`app/src/main/res` 和 `app/src/main/AndroidManifest.xml`。它不含构建时间，也不覆盖本桌面测试目录、`plugin-core` 模块或构建脚本，因此适合区分这些应用源码输入的构建，不应视为整个仓库或全部 APK 依赖的完整校验和。

## 案例 3：`zhubozi` 点击显示候选“猪”没有上屏

### 真机证据与原因

通过当前真机 `RpcUiController.imeType()`、`imePageDown()`、`imePick()` 及 Rime 直接状态接口复现：

- 设备构建信息为 `20260919SECEXP=1长度17混合`，`git_hash=f796558`。重置后输入 `zhubozi`：第 0 页为“住脖子、主播……”；翻到第 1 页后，UI 与 Rime 当前页一致地显示“猪、主、祝……”（`page_no=1`、`page_size=20`、`front=0`、`tail=0`）。
- 安装上一修复 APK 后执行 `imePick(0, "猪")`，RPC 明确返回 `selection_accepted=true`、`fallback_used=true`、`committed_text="猪"`、`partial_selection=false`，之后 `input=""`。这证明尾码 `bozi` 被回退提交路径清掉了，不是 Rime 原始菜单没有“猪”。
- 同设备读取 Rime preview 对“猪”返回 `null`。直接在页 1 选择“猪”时，Rime 接受选择，原输入仍为 `zhubozi`，后续候选为“脖子、拨……”；在未消费 commit 的 composition 读取中，预编辑为“猪bo zi”。这证明该候选是有效的部分选词，应保留后续音节，不能当完整候选提交。
- 前一次修复虽在空 commit 的探测分支补了 `clearComposition()`，却仍在 preview 缺失时执行有副作用的 `trimDanglingTailAndPick()`；恢复后又无条件调用 `commit()`，并允许 mismatch fallback 清空全部组字态、只上屏“猪”。测试只覆盖了一个布尔条件，没有覆盖 RPC 选词完整路径和“尾码仍可继续选择”，因此漏掉了这种数据丢失回归。

### 修复与回归

现在 preview 不可用时完全跳过尾码探测；正常候选选择成功后，先用不消费 pending commit 的 composition 查询检查是否仍有输入与后续候选。若有，立即刷新组合态并标记 `partial_selection=true`，不调用 `commit()`，也不允许只提交当前部分词的 mismatch fallback。只有 preview 明确显示没有剩余输入时，才允许完整候选 fallback。测试覆盖 preview 缺失时禁止有副作用探测，以及“仍有输入和候选”的部分选词条件。

本次针对上一修复 APK 的真机复现已确认其实际回归：`committed_text="猪"`、`fallback_used=true`、之后输入清空。新的修复还需在新 APK 安装后重复同一 RPC；预期点“猪”后不产生 committed text，`partial_selection=true`，输入保持 composing，尾码候选为“脖子、拨……”。需再选择后续候选才完成上屏。

## 案例 2：点击部分音节候选后保留未选尾码

### 问题表现与真机复现

- 输入 `yimudi` 时，候选栏中能看到“一亩”，但点选后 `selection_accepted=false`，组字串仍是 `yimudi`，没有任何文字上屏。
- 输入 `ksmkan` 时，候选栏中有“看什么”。旧路径点选后返回 `selection_accepted=true`、`fallback_used=true`，只提交“看什么”，然后清空引擎输入，尾部 `kan` 丢失。

复现使用 `RpcUiController.imeType()`、`imePick()` 和 `imeState()` 走生产候选路径，不通过屏幕坐标推断。真机版本快照的 `git_hash` 为 `e84d188`。重连时第一次 RPC 超时，随后两次成功；上述状态是在成功 RPC 会话中读取到的。

### 期望行为与修复

点击候选时调用 Rime 的 candidate preview，按候选身份临时高亮并读取候选覆盖的输入范围及其后的未选输入。若候选只覆盖前面的音节，且尾码自身仍有有效候选，就只把该候选暂存为 composing 文本、以尾码重建 Rime 输入并刷新候选栏；不向宿主提交，也不清除尾码。最终选完余下候选时，才把暂存文本和最终 commit 一起提交。

例如 `ksmkan` 点“看什么”后，预编辑显示“看什么”，Rime 继续以 `kan` 提供候选；再点选尾码候选后才整体上屏。退格到尾码为空时会恢复前一段编码，清空/换输入会话时则清除暂存候选。若候选已覆盖全部输入，沿用既有完整提交路径。

`resolveCandidateInputPrefix()` 的单元测试覆盖 `ksmkan`/“看什么”→`kan`、`yimudi`/“一亩”→`di`，以及完整输入不误判成部分候选。Android/JNI 构建和在包含修复源码的 APK 上通过 RPC 重复以上两例，仍是设备集成验证；旧设备快照本身不能验证尚未安装的修复。

## 案例 4：统一中英文应用学习频率与英文首屏显示

完整上屏的 Rime 中英文候选会把当前输入码、词条和使用频率记录到同一应用学习表；中文候选仅在完整提交后学习，部分选词不会把尚未完成的组合误记为完整词。回车原样上屏的英文仍按“输入码等于词条”学习。旧版 `word<TAB>score` 和裸词文件会在读取时兼容，下一次写入时迁移为 `input_code<TAB>word<TAB>score`。

下次输入时，应用按学习记录的输入码前缀匹配，已学习词根据同一套频率分值排序并注入候选栏；未学习的 Rime 候选之间仍完全保留 Rime 原生顺序。因此这是“统一应用学习词频率”，不是把所有内置词典候选的 Rime 权重重新标定成同一数值体系。

主候选栏首屏最多显示两个英文候选。单个英文词超过 10 个 Unicode 码点时保留前缀并用单字符 `…` 省略；若两词显示长度合计超过 10，则优先缩短较长词，必要时再缩短另一词。仅改变显示标签和首屏可见集合：候选状态仍保留原始词条，点击与 RPC 选词使用原始完整词；“更多候选”页面也仍可检查完整候选列表。

新增只读 RPC `RpcUiController.imeDiagnostics()`，返回当前输入下匹配到的学习词与分数、每个候选的来源和输入码、是否显示在首屏及实际显示标签。它只报告当前匹配结果，不导出完整用户词表，也不调用会改写 Rime composition 的探测操作。真机可按以下方式沿生产路径核验：

```python
from com.kingzcheung.xime.util import RpcUiController
RpcUiController.imeReset()
RpcUiController.imeType("international")
RpcUiController.imeDiagnostics()
```

Kotlin/JVM 回归测试覆盖旧数据格式迁移、中英文频率共用、输入码前缀匹配、英文候选数量限制、Unicode 安全截短、总显示长度限制以及 UI 标签与原词身份分离。设备验证还应检查 diagnostics 中的 `candidate_diagnostics` 与实际点选后的提交文本一致。

候选长按菜单中的“移到第二屏”会降低应用学习分数：自动学习词设为最小负分 `-1`，从普通候选栏首屏隐藏，并移到第二个 Rime 分页末尾，后续分页不重复。如果 Rime 只有一个分页，则追加在唯一页末尾。再次选中后加一，恢复为非负分并回到前排。此分数是应用内前置/后置分段的状态，不是 Rime 词典权重或 Rime 计算出的真实词频。手动 custom_phrase 继续通过 Rime 权重降低排序。自动词与手动词在普通候选栏和展开列表中均可长按删除；展开列表的长按菜单也提供相同的降频和删除操作。

## 案例 7：降频学习词重复出现在每一页末尾

真机输入 `c` 并将自动学习词 `cmd` 移到第二屏后，`imeDiagnostics()` 显示其分值为 `-1` 且 `tail=1`，但它在展开候选的首屏和后续页都重复出现在末尾。原因是候选合并每次翻页都会无条件把负分词追加到“当前页”Rime 候选之后，误把“降频并排在全部正常候选之后”实现成“每页都追加一次”。

修复后仍会在每页过滤被降频的学习词，避免它因同时存在于 Rime 词典而提前出现；有多个分页时只在第二页末尾追加一次，只有一页时才追加在该页末尾。自动词分数及其持久化格式不变。

## 案例 5：已学习英文词在每个 Rime 翻页后重复前置

真机 RPC 对 `c` 执行 `imeKey("c")` 并连续翻页，确认每页 `front=8`，候选都重复以 `cad、curev、cmd、chacuopy、cbg、claude、com、code` 开头；Rime 页码从 0 增至 1、2、3 时，应用仍把同一组学习词拼在该页中文候选之前。对照 `x` 是 `front=2`，同样重复出现在每个 Rime 页。这是合并逻辑每次刷新都无条件前置学习词造成的分页错误，与词频数值大小无关。

修复后，应用学习词只在 Rime 第 0 页前置；后续页保留该页 Rime 候选，不再重复前置英语词。前置计数随页变为 0，点击后续页候选仍按原索引交给 Rime。英文和中文应用学习词共用的是应用学习表内部的频率分数；未经应用学习的 Rime 字典候选并没有可直接比较的同尺度分数，因此不能称为全量中英文统一排序。当前选择是保留 Rime 自身顺序，而不是擅自重排所有字典候选。

同一真机输入 RPC 显示 `c` 原始键处理后组合状态和候选仍有效，`x` 也正常；键位配置中 `c` 的 tap 为 `"c"`，其已定义上滑/下滑动作不包含收起键盘。源码中实际收起键盘的路径是候选栏专用向下按钮触发 `requestHideSelf()`，并非字母 `c` 的普通 tap。RPC 直接按键不等同于触屏事件，无法据此排除 Compose/触控层的设备现场问题；真机 UI 仍需复验长按菜单操作后窗口是否保持显示。

## RPC：键盘窗口与 UI 事件追踪

新增只读接口用于现场手动复现后取证：

```python
from com.kingzcheung.xime.util import RpcUiController
RpcUiController.imeUiDiagnostics() # 一次读取当前窗口快照及最近 50 条 UI 事件，不清空记录
RpcUiController.imeUiSnapshot()   # 当前窗口、编辑器和输入/候选摘要
# 在设备上操作：长按候选、选择菜单项，再按 c 和 x
RpcUiController.imeUiTrace()      # 读取完整环形记录，不清空
```

`imeUiSnapshot()` 报告 `input_view_shown`、窗口 decor 可见状态、窗口焦点、IME 窗口 flags/soft-input mode、输入连接和编辑器元数据，以及当前输入、候选数量和前 8 个候选。`imeUiDiagnostics()` 将快照与 trace 容量、总条数和最近 50 个事件合并返回，适合先读现场而不清除记录。`imeUiTrace(clear)` 为最多 300 条的环形记录；每条包含 wall-clock 与 elapsed-realtime 时间戳，以及当时的窗口/焦点/编辑器/输入状态。记录事件包含 `key_touch_down`、`key_touch_up`、`candidate_long_press`、`candidate_menu_shown`、`candidate_menu_dismissed`、`candidate_menu_selected`、`onWindowFocusChanged`、`hideKeyboard`、`onStartInputView`、`onFinishInputView`、`onWindowShown` 和 `onWindowHidden`。菜单事件的 `detail` 标明来源是 `candidate_bar` 还是 `candidate_page`，并报告 Popup 是否可聚焦；`hideKeyboard` 事件的 `detail` 标明调用来源。

这些接口不模拟触摸，不会主动触发隐藏窗口或修改输入状态。建议保留当前记录并读取 `imeUiDiagnostics()` 或 `imeUiTrace()`；若需要从头捕获新一轮复现，才调用 `imeUiTrace(True)` 清空旧记录。若窗口隐藏但没有 `hideKeyboard` 事件，生命周期和焦点事件仍可帮助判断是否由系统/宿主隐藏。`imeKey()` 仍仅用于 Rime 按键管线验证，不等同于真实 UI 触摸。

## 案例 6：候选长按菜单导致 IME 窗口被系统隐藏

此前保留的设备 trace 中，横栏英文候选 `claude` 和 `com` 的长按菜单出现后分别在 62 ms、71 ms 收到 `onFinishInputView(false)` 与 `onWindowHidden`，没有 `hideKeyboard` 或菜单选择事件。`onWindowHidden()` 随后清理 composing 与候选状态，因此键盘恢复时输入已丢失。源码中横栏和展开候选页菜单都使用 Compose `DropdownMenu`，其 Popup 默认可获取焦点；焦点 Popup 出现在 IME 上方时会让 Android 结束输入视图。

修复在横栏和展开页两个菜单上都显式设置 `PopupProperties(focusable = false)`，使菜单仍可触摸操作但不争夺 IME 窗口焦点。RPC trace 同时记录菜单来源、焦点状态和 IME 窗口 flags，并由 `imeUiDiagnostics()` 提供不清空记录的一次性现场摘要。安装包含修复的 APK 后，应分别长按横栏候选和展开页候选，确认菜单可见、可点选/点击外部关闭，且 trace 不再出现紧随 `candidate_menu_shown` 的 `onFinishInputView`/`onWindowHidden`；设备端验证仍需在实际 Android 宿主上完成。
