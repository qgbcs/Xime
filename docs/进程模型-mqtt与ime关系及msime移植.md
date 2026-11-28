# xime 进程模型、MQTT/IME 关系与 msime 移植评估

> 本文回顾一轮对话的结论与依据，记录 xime 的实际进程模型、MQTT 上线时机，以及把该机制移植到 msime 时的可行方案与取舍。结论以仓库代码为准（xime 分支 qgb，commit cabfd4e4；msime 分支 qgb，commit 1e31bfb）。

## 1. 起点问题

- 用户刚安装 xime，打开时停在初始设置向导（SetupWizardScreen），尚未启用/切换系统键盘，此时 MQTT 是否已经连接？
- 若把 xime 的 MQTT/Python 机制移植到 msime，Python/MQTT 应该挂在主进程还是 :ime 进程？

## 2. xime 的进程模型（实测）

### 2.1 只有一个主进程

`app/src/main/AndroidManifest.xml` 中 `android:process` 只出现两处：`:inference`、`:asr`。以下组件全部运行在主进程：

| 组件 | 进程 |
| --- | --- |
| `XimeInputMethodService`（键盘） | 主进程 |
| `MainActivity` / `SetupWizardScreen` | 主进程 |
| `XimeAccessibilityService` | 主进程 |
| `BackgroundCaptureService` | 主进程 |
| `TaskAlarmReceiver` / `BootEventReceiver` | 主进程 |

因此「键盘与 RPC 同进程」不是设计选择，而是 manifest 的结果。

### 2.2 Python 与 MQTT 的启动链

`XimeApplication.onCreate()` → `isMainProcess()`（进程名等于包名）→ `startPythonRpc()` → `Python.start()` → `app.start(logPath, filesDir)`。

`app.py` 的 `start()` 顺序：

1. 重定向 stdout/stderr，`runtime_pip.configure()` 只调整 sys.path，不做网络安装；
2. `http_enabled` 默认 false，默认不起 HTTP；
3. `mqtt_enabled` 默认 true，`server_mqtt.MQTTServer(...).start(block=False)`；
4. 之后才是 `task_manager.init()`、`feature_feedback.init()`。

结论：MQTT 在应用主进程启动时就异步连接 13~14 个公共 broker，通常秒级上线；与键盘是否启用、词库是否部署、向导是否完成都无关。`:inference`/`:asr` 侧进程被 `isMainProcess()` 明确排除，避免抢端口和重复应答。

### 2.3 为什么 xime 不需要前台服务

xime 没有为 RPC 做前台服务；唯一的 `startForeground` 在 `BackgroundCaptureService`（相机/麦克风/定位/媒体采集）。「任意时刻可执行」成立的原因是：

- IME service 在主进程；
- xime 作为默认输入法被系统长期 bind；
- 系统为维持输入法而保活主进程，Python/MQTT 顺带常驻。

`BootEventReceiver` 只用于补发 boot_completed 事件（借 WorkManager 拉起进程），不是常驻保活手段。

### 2.4 能力分层

1. MQTT/HTTP RPC 与任意 Python 执行：主进程活着即可用；
2. `RpcUiController.imeType/imeKey/imePick/...`：由 `XimeInputMethodService.onCreate()` 调 `attachIme` 挂载 `ImeTestBridge` 后才可用，否则返回 `{"ok":false,"error":"ime_not_active"}`；
3. 触摸注入 `feature_touch`：走无障碍服务，需用户开启无障碍，与 MQTT 启动无关。

## 3. msime 的进程模型（实测）

- `MSIMEInputService` 与 `OcrCaptureActivity` 声明在 `:ime`；主进程承载 `HomeActivity`/`OnboardingActivity`、资源包下载、更新、账号。
- 全仓库没有 AIDL/Messenger/bindService；唯一跨进程通道是 `AccountSessionProvider`（`ContentProvider.call`，`exported=false`，同 uid 可用），主进程向 `:ime` 发放短时 access token。
- 没有 Chaquopy/Python/paho/MQTT；Rust 原生库 `msime_android` 经 `NativeClient` 加载，约 190 个 JSON 入/JSON 出的 JNI 方法，主进程与 `:ime` 都在使用。
- 没有 Application 子类（manifest 的 `<application>` 未写 `android:name`），新增 Python 引导必须新建并加进程守卫。
- `crates/mcp-server` 是 `rmcp` 的 stdio 二进制，工具面为偏好/词典/快捷短语/皮肤/插件/统计/诊断；`session` 子命令只是 `LocalSession` 引擎模拟，不能上屏到前台应用。
- `:ime` 并非不联网：在线候选/AI 由 `MSIMEInputService` 直接发起 HTTP。

## 4. 方案取舍

| 方案 | 结论 |
| --- | --- |
| 只挂 `:ime` | 键盘未启用时 Python 不存在，违背「无论是否启动键盘」；Python 运行时进键盘进程内存/包体代价大；进程回收即丢状态 |
| 主进程 + 跨进程桥 | 启动即在线；需自建 IPC，可复用 ContentProvider 模式 |
| 只挂主进程 | msime 主进程不被键盘保活，不加前台服务只能「App 打开时在线」 |
| 推荐 | Python/MQTT 主进程 + 前台服务(specialUse，勿用 dataSync) + BOOT_COMPLETED；`:ime` 新增不导出 `RpcImeProvider`，暴露 type/pressKey/pick/pageDown/state，主线程复用 onKeyDown/command/apply；主进程先查 Settings.Secure 与 attach 状态，不可用即返回 ime_not_active，不拉起 :ime |

MCP 不作为输入通道：stdio 在 Android 无对端，工具面不含输入驱动。要复用 Rust 能力应包装 `host-api`/`NativeClient`。

## 5. 错误尝试与澄清

- 曾把 xime 的「任意时刻可执行」理解为依赖专门保活；实际没有前台服务，靠同进程 + 默认 IME 绑定。该保活要求只对 msime 成立。
- 曾以为 msime 的 `:ime` 不联网；manifest 注释只针对资源包下载，在线候选仍从 `:ime` 发出。
- Chaquopy 版本需对齐：xime 用 17.0.0 + Python 3.13 + Gradle 9.8.2；msime 为 Gradle 9.5.1/AGP 8.13.2，需先做空工程接入实测。

## 6. 本次改动

- 新增本文档。
- `debug_build_secexp.sh`：默认中文名由「随手」改为「讯飞」（工作区既有未提交改动，一并提交）。

## 7. 风险清单

- Chaquopy 与 msime 构建链兼容性；
- 16 KB page size（targetSdk 36）；
- APK 体积增加，建议仅保留 arm64-v8a；
- R8：msime 已有 `-dontobfuscate` 与 `-keep class app.msime.android.**`，jclass 按名查找安全。
