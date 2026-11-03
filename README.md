# Xime（qgb 分支）

这是 [qgbcs/Xime](https://github.com/qgbcs/Xime) 的 `qgb` 分支：基于上游 [ximeiorg/Xime](https://github.com/ximeiorg/Xime) 的个人自用构建。

分支目标：**在保留个人改动（Chaquopy Python 运行时 + 输入法修复）的同时，把补丁压到尽可能小，方便持续跟进上游。**

| 项 | 值 |
|---|---|
| 上游基线 | `002006ea`（tag `nightly`，即上游 `main`）|
| 包名 | `com.qgb.xime` |
| 构建脚本 | `debug_build_secexp.sh`（参数集中在文件开头“构建参数”区）|
| 产物 | `out/com.qgb.xime-<VERSION_CODE>-arm64-v8a.apk` |
| 真机回归 | `scripts/qgb_device_test.py` |

---

## 一、本分支改了什么

设计原则：**上游默认行为一律不改**；Kotlin 侧除了 Chaquopy 构建配置，只有 `KeyboardViewModel` 一处 22 行的小补丁，其余都是配置、资源、脚本。

| 位置 | 改动 | 目的 |
|---|---|---|
| `app/build.gradle.kts`、`app/src/main/python/`、子模块 `multi_mqtt` | 接入 Chaquopy，Python 3.13 | 个人业务逻辑跑 Python；构建机无 python3.13 时由 `scripts/install-python313.sh` 安装 |
| `app/src/main/assets/xime.yaml` | 取上游 `002006ea` 全量（补齐 `keyboard.number`、`keyboard.common_symbols`、`keyboard.t9` 等段），再重放个人配置 | 缺段会让 `?123` 面板整块空白（见下）；个人配置只在既有键上改值 |
| 同上 `common_symbols` 段 | 恢复旧版紧凑 4 行：`10/10/9/6` 键，符号用半角 ASCII | 旧版观感更紧凑；上游全角字形 + 4 键底行显得“太大” |
| `.../viewmodel/KeyboardViewModel.kt` | 只加 22 行：import `KeysConfigHelper`、补 `applySchemaLayout`、`dispatch` 开头调用 | 切 `t9_pinyin` 时先刷新键布局缓存，修“拼音九键切不过去” |
| `app/src/main/res/values*/strings.xml` | app 名统一引用 `@string/app_name` 等 | 由构建脚本 `APP_CHINESE_NAME` 注入，不再到处改字面量 |
| `debug_build_secexp.sh` | 个人构建参数区：`VERSION_CODE`、`APP_CHINESE_NAME`、图标、ABI、输出目录 | 一次改完，版本/名字不用改 Java 或资源 |
| `scripts/qgb_device_test.py` | 真机 e2e 用例集（详见第四节） | 把历史问题固化成可重复执行、可筛选的断言 |
| `scripts/backfill-gitcode.sh`、`merge_base_dict.py`、`sync-release-to-gitcode.py` 等 | 个人脚本 | 发布/词库/镜像同步辅助，与主流程无关 |
| `docs/`（`sdk/`、`schemas_examples/`、`plugin-*.md` 等） | 个人文档与示例 | 与上游文档并行维护 |

### 个人配置重放要点（`xime.yaml`）

- 上游把 `swipe_up` 破成了 `long_press.values` 结构，旧写法已不生效；个人 27 行划线配置按新结构重写。
- 主题 `lavender_purple` 改为 `ocean_blue`。
- `common_symbols` 最终形态：

```yaml
common_symbols:
  type: full
  layout:
    rows:
      - [1, 2, 3, 4, 5, 6, 7, 8, 9, 0]
      - ["@", "#", "$", "&", "_", "-", "+", "(", ")", "/"]
      - [open_symbols, "*", "\"", "'", ":", ";", "!", "?", delete]
      - [back, number, ",", space_key, ".", enter]
```

---

## 二、构建

```bash
./debug_build_secexp.sh          # 默认 VERSION_CODE=20261103，APP_CHINESE_NAME=随手
adb install -r out/com.qgb.xime-20261103-arm64-v8a.apk
```

要改版本号 / app 名 / 图标 / ABI，只改脚本开头的“构建参数”区，不要改 `strings.xml` 字面量。

---

## 三、与上游同步

```bash
git remote add upstream https://github.com/ximeiorg/Xime.git   # 只需一次
git fetch upstream --tags
git switch qgb
git merge upstream/main        # 或合并最近的 nightly tag
```

### 冲突处理顺序（重要）

1. **`xime.yaml` 冲突：先取上游整份，再重放个人差异。** 只补三处：`swipe_up → long_press.values`、主题名、`common_symbols` 紧凑段。
   **千万不要保留缺段的旧配置**：`PanelKeyboardLayout` 取不到 `number` / `common_symbols` / `t9` 段时直接渲染空 `Box`，表现就是 `?123` 面板一片空白。
2. **Kotlin 冲突：以上游为准**，只在 `KeyboardViewModel.dispatch` 保留 `applySchemaLayout(action, schemaId)` 那几行。
3. **`strings.xml` 冲突：以上游为准**，只保留 `app_name` 引用与 subtype 标签的变量化。
4. 除个人独有文件（`qgb_device_test.py`、个人脚本/文档）外，**不要整文件“用我的版本”覆盖**——这是以后每次同步都能顺利的关键。

### 同步后必做三步

```bash
python3 -c "import yaml;yaml.safe_load(open('app/src/main/assets/xime.yaml'))"   # 1. 语法校验
./debug_build_secexp.sh                                                         # 2. 构建
python3 scripts/qgb_device_test.py --skip-install --serial <序列号> --expect-version <VERSION_CODE>  # 3. 真机回归
```

---

## 四、真机 e2e 用例集（`scripts/qgb_device_test.py`）

把“真实翻过车的问题”写成可筛选、可重复执行的用例；每次改动或同步上游后跑一遍。

### 用例与历史问题对应

| 用例 | 历史问题 | 断言方式 |
|---|---|---|
| `panel_26_ok` | 20261102：26 键中文按 `?123` 整块空白 | 区域边缘占比 + 与主键盘 diff |
| `panel_en_ok` | 同版本：切英文后 `?123` 空白 / 未真正切过去 | 语言切换 diff + 面板 diff + 每行键数 |
| `panel_compact` | “符号显示太大，旧版更紧凑” | 自动定位 4 行，每行键数必须是 `10/10/9/6` |
| `t9_ok` / `t9_symbol_ok` | 拼音九键方案无法切换显示 | 与 26 键 diff + 九键符号面板非空白 |
| `kb_26_ok` / `back_26_ok` | 主键盘、返回后空白 | 区域边缘占比 |
| `symbol_page_ok` | 全集符号页空白 / 列数被改 | 非空白 + 竖屏 8 列 |
| `version` / `logcat` | 装错版本、运行期崩溃 | `versionName` 子串 + `FATAL / AndroidRuntime` |

### 判定手法（不依赖第三方库）

- 纯 Python 解码 screencap PNG（RGB/RGBA、非隔行），带解码缓存，一张图只解一次。
- `edge_ratio`：区域内横向相邻差 > 20 的像素占比。空白页约 1%，键盘 4.5%–7.5%，阈值 0.02。
- `region_diff`：两张图同区域的平均差，用来证明“真的切过去了”，而不是停在原页面。
- `count_key_runs` + `detect_key_rows`：横向扫描键面色块 + 合并抗锯齿间隙，自动定位按键行中心；键盘整体上下位移、候选栏高度变化都不影响断言。
- 每步截图存到 `--out`（默认 `out/device_test`），失败时直接对照 png 定位。

### 用法

```bash
python3 scripts/qgb_device_test.py --list-cases                                   # 看用例与说明
python3 scripts/qgb_device_test.py --apk out/com.qgb.xime-20261103-arm64-v8a.apk \
    --serial ENU7N16322378460 --expect-version 20261103                        # 装包 + 全量
python3 scripts/qgb_device_test.py --skip-install --cases panel_26,panel_compact # 只跑指定用例
python3 scripts/qgb_device_test.py --skip-install --skip-cases launch,ime        # 跳过指定用例
```

退出码：`0` 全通过，`1` 有失败项，`2` 环境错误（找不到 adb 等）。

坐标全部是屏幕比例，默认值按 Nexus 6P（1440x2560，API27）校准；换机型先 `--list-cases`，再按截图微调参数（如 `--rowcounts`、`--panelx/--panely`），不要把像素写死。

---

## 五、注意点

1. **`app/src/main/assets/rime` 是 submodule**。本机构建环境用软链指向共享 rime 数据，`git status` 会报 `expected submodule path ... not to be a symbolic link`；用 `git status --ignore-submodules=all` 看改动，**不要把软链或数据提交进仓库**。
2. **构建产物不入库**：`out/`、`app/src/main/jniLibs/`、`app/src/main/jni/third_party/`、`__pycache__/`。
3. **`xime.yaml` 是面板的唯一来源**：非 `qwerty` 段忽略 `isAsciiMode`，符号字形完全由 yaml 值决定（全角→半角就是紧凑的关键）。改面板只改 yaml，不要改 Kotlin。
4. **改键数/行数要同步改用例期望**：`--rowcounts` 默认 `10,10,9,6`。
5. **全集符号页默认英文分类、竖屏 8 列是预期行为**（“点进去是默认英语”不是 bug）。
6. **Chaquopy 构建机依赖 python3.13**；`app/src/main/python/multi_mqtt` 是构建时同步的权威副本，不要手改。
7. **真机测试会拉起短信 App 并切换输入法**，测试完设备停在拼音九键方案，属正常现象。

---

## 六、`docs/` 与 `scripts/` 的收录情况

已核对旧仓库 `build_xime_home/xime` 与本分支：

- 旧的 `docs/`、`scripts/` **已全部合并进 qgb**，`diff -rq` 没有任何“仅旧仓库存在”的条目。
- 少量同名文件内容不同（`docs/config_examples/*/xime.custom.yaml`、`scripts/build-plugins.*`），是上游 `002006ea` 更新后的版本，不是丢失。
- 本分支额外多出：`docs/icon_light.png`、`plugin-capability-registry.md`、`plugin-system-audit.md`、`schemas_examples/`、`sdk/`、`Screenshot/0-2.jpg`，以及 `scripts/qgb_device_test.py`、`backfill-gitcode.sh`、`merge_base_dict.py`、`sync-release-to-gitcode.py`。
- 上游已删除/取代的历史运行时文件（`plugin-core` 的 Lua Kotlin 运行时、`patches/lua.patch`、旧 web 包 `index-*.js`）**没有回填**：上游已改 QuickJS，回填会拧断构建。

---

## 七、常见问题

**Q：按 `?123` 一片空白？**
→ 先看 `xime.yaml` 是否有 `keyboard.number` / `keyboard.common_symbols` 段；在 Android 端加载的是 `assets/xime.yaml`，改错了位置（例如只看根目录副本）不会生效。

**Q：拼音九键点了没反应？**
→ 确认 `KeyboardViewModel.dispatch` 开头调用了 `applySchemaLayout(action, schemaId)`；同步上游时这条最容易被冲突吃掉。

**Q：符号还是很大？**
→ 看 `common_symbols` 的 `rows` 是否还是 4 行、符号是否半角 ASCII；跑 `--cases panel_compact` 验证每行 `10/10/9/6`。

**Q：e2e 个别用例失败但界面看起来正常？**
→ 截图在 `--out` 目录，先看图；常见原因是坐标比例与该机型不符（换机型后必调）或动画没结束（`tap` 的 wait 不够）。
