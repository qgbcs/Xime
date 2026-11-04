# Android 无障碍服务实时屏幕控制项目调研

> 调研日期：2026-11-04
> 目的：评估「基于 AccessibilityService 的 Android 屏幕实时控制」开源方案，为现有 MQTT RPC 注入能力补充画面回传。
> 结论：**纯无障碍服务做不到真正的实时画面**（系统节流约 3 fps），真正流畅的方案都是「MediaProjection 采集 + 无障碍注入」混合架构。

## 一、能力边界

| 能力 | 关键 API | 最低版本 | 硬限制 |
|---|---|---|---|
| 读界面结构 | `getRootInActiveWindow()` / `AccessibilityNodeInfo` | API 14+ | 只有控件树，没有像素；自绘/Canvas/游戏画面拿不到 |
| 截图 | `AccessibilityService.takeScreenshot()` | **API 30 (Android 11)** | 系统节流，实测约 **3 帧/秒**，超频返回 `ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT`；需 `android:canTakeScreenshot="true"` |
| 单窗口截图 | `takeScreenshotOfWindow()` | API 34 | 同样受节流 |
| 手势注入 | `dispatchGesture(GestureDescription)` | API 24 | 只能离散手势，最多 10 条 stroke，不能注入原始 `MotionEvent` 流 |
| 控件操作 | `performAction(ACTION_CLICK/SCROLL/SET_TEXT)` | API 14+ | 依赖目标 App 暴露无障碍动作 |
| 全局动作 | `performGlobalAction(BACK/HOME/RECENTS/通知栏)` | API 16+ | 固定枚举 |

推论：无障碍服务天生是「事件驱动 + 结构感知」，不是「像素流」。要画面必须 `MediaProjection`，要低延迟必须 `MediaCodec` 硬编码。

## 二、A 档：纯无障碍方案（准实时，1~3 fps）

| 项目 | 形态 | 协议/端口 | 亮点 | 短板 |
|---|---|---|---|---|
| [claw-use-android](https://github.com/claw-use/claw-use-android) | 单 APK，内置 HTTP Server | HTTP 7333，37 个端点，`X-Bridge-Token` 鉴权 | `/screen` 返回整棵 UI 树、`/screenshot` 返回 JPEG、`/tap` `/swipe` `/click` `/flow`、`/global`、`/launch`、`/clipboard`、`/sms` | 截图受 3fps 限制；端点权限极大（含相机、短信），暴露即等于交出手机 |
| [phone-for-ai-agent](https://github.com/ponikar/phone-for-ai-agent) | Kotlin App + Node 服务器 | WebSocket 8080(设备)/8081(脚本转发) | 12 种命令白名单，`click_text`/`click_description` 语义点击，前台服务保活，MIT | 只有指令级控制，无画面；README 要求仅可信局域网 |
| [android-a11y-mcp](https://github.com/benoberkfell/android-a11y-mcp) | Rust MCP 单二进制 + 内嵌 APK | MCP over stdio，ADB reverse tcp:38301 | 19 个工具、自动安装 APK、原生 ADB 协议不依赖 adb 二进制、截图走 API 30+ | 必须连 ADB；面向 AI 调试而非人肉遥控 |
| [OpenGUI](https://github.com/Core-Mate/OpenGUI) | 后端(Node+PG+Redis) + Android 客户端 | 后端 7777 + `adb reverse` + 常驻 WS | 1.8k star，`GestureService.kt` 走无障碍，截图+VLM 规划执行，Feishu/Telegram/REST 派发 | 重量级（Docker 依赖），BUSL-1.1，客户端要求 Android 11+ |
| [AutoJs6](https://github.com/SuperMonster003/AutoJs6) / AutoX.js | JS 脚本宿主 | 脚本内自建 HTTP/WS | 生态成熟，`click()`/`swipe()`/`captureScreen()`，可打包 APK，支持 Shizuku 提权 | 打包易报毒；截图同样受限；原版 Auto.js 2023 已归档 |

A 档共同点：**用「结构」换「帧率」**。UI 树是毫秒级、结构化、可寻址的，对 AI/自动化比画面更有用；代价是人眼看着卡。

## 三、B 档：混合方案（真正的实时屏幕控制）

| 项目 | 采集 | 注入 | 协议 | 典型帧率/延迟 | License |
|---|---|---|---|---|---|
| [droidVNC-NG](https://github.com/bk138/droidVNC-NG) | MediaProjection | **AccessibilityService** | RFB/VNC + 内置 Web 客户端，Zeroconf 发现 | 10~20 fps / 150~400ms | GPL-2.0 |
| [remote-phone](https://pypi.org/project/remote-phone/) | MediaProjection + MediaCodec **H.264** + PCM 音频 | **AccessibilityService** | WebSocket 8765，PyQt6 桌面端，自动发现/重连 | 30 fps / 100~300ms | 开源 PyPI 包 |
| [go-rdp-android](https://rcarmo.github.io/projects/go-rdp-android/) | MediaProjection → gomobile RDP 编码 | **AccessibilityService** | 标准 RDP，任意 RDP 客户端可连 | 实验级 | 开源 |
| scrcpy（对照） | ADB + app_process 服务端 | ADB `input` | 自定义 socket | 30~120 fps / 30~70ms | Apache-2.0 |

三者验证了同一件事：画面必须 `MediaProjection`，输入才交给无障碍。droidVNC-NG 是最成熟可用的一个：F-Droid 持续更新、免 root、自带浏览器访问、支持反向 VNC 与 UltraVNC repeater，可被 macOS 屏享直接连接。

## 四、绕不开的坑

1. **`FLAG_SECURE` 全黑**：银行、部分 IM 界面，MediaProjection 和无障碍截图都拿不到，无解。
2. **MediaProjection 必须每次授权**：Android 10 起不允许隐藏弹窗，Android 14 还要求前台服务声明 `mediaProjection` 类型。这是「无人值守」方案的致命点。
3. **Android 13+ 侧载限制**：ADB 安装的 APK 需在应用信息里「允许受限设置」才能开无障碍（remote-phone 文档专门写了这一节）。
4. **手势不是触摸流**：`dispatchGesture` 每次是一段完整手势，做不了「按住不放连续拖」的原始语义，只能多次 stroke 拼（xime 里 `continueStroke` 真机不生效，最后用 ±3px 锯齿造保持时间）。
5. **端口暴露**：7333/8080 这类明文 HTTP 一旦被扫到就是整机控制权。claw-use 加了 token，phone-for-ai-agent 直接标注仅可信局域网；OpenGUI 用 `adb reverse` 只绑本机，更安全。
6. **厂商 ROM 杀后台**：国产定制系统的无障碍/前台服务容易被清，需白名单 + 电池优化豁免。

## 五、选型建议

| 需求 | 推荐 |
|---|---|
| 人眼看着流畅操作 | droidVNC-NG（免 root，现成 VNC 客户端） |
| 自写客户端、要音频 | remote-phone（H.264 + PCM，协议干净） |
| AI/脚本语义化控制（点文本、读树） | claw-use-android 或 AutoJs6 |
| 接 MCP / Claude | android-a11y-mcp |
| 长任务 agent 编排 | OpenGUI |
| 最低延迟、能插 USB | scrcpy（ADB，非无障碍） |

## 六、与现有 MQTT + RPC 场景的取舍

现有 `XimeAccessibilityService` 已完成注入侧（tap/longPress/swipe/drag/pinch/gesture，`capabilities=32` 真机验证），缺的只是回传画面。四条路按改动成本排序：

| 路线 | 做法 | 帧率 | 代价 |
|---|---|---|---|
| 0（零改动） | 旁挂 droidVNC-NG 看画面，本端只负责注入 | 10~20 fps | 装个 APK，仅局域网 |
| 1 | 服务内加 `takeScreenshot`，RPC 返回 JPEG/PNG | 1~3 fps | 改服务端约 50 行；受节流 |
| 2 | 加 `takeScreenshotOfWindow` 只回传输入法窗口 | 1~3 fps | 同上，更省流量 |
| 3 | MediaProjection + MediaCodec H.264 + WS | 30 fps | 工程量大，且需人工授权 |

务实路线：先用路线 0 验证「值不值得做实时画面」；若只是「点完确认结果」，路线 1 的 1~3fps 已够用，且能直接挂在现有 MQTT RPC 上，不引入 MediaProjection 的授权死结。

## 参考来源

- [AccessibilityService API 参考（takeScreenshot / ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT）](https://developer.android.google.cn/reference/android/accessibilityservice/AccessibilityService)
- [AccessibilityServiceInfo（CAPABILITY_CAN_TAKE_SCREENSHOT / canTakeScreenshot）](https://developer.android.google.cn/reference/android/accessibilityservice/AccessibilityServiceInfo)
- [创建无障碍服务（Android Developers）](https://developer.android.google.cn/guide/topics/ui/accessibility/service?hl=zh-cn)
- [限制屏幕阅读（AOSP：Android 10 起取消静默截屏权限）](https://source.android.google.cn/docs/core/permissions/restricted-screen-reading?hl=zh-tw)
- [@fugood/react-native-screen-capture（平台限制约每秒三次）](https://www.npmjs.com/package/@fugood/react-native-screen-capture)
- [claw-use-android 官网（37 端点、7333、X-Bridge-Token）](https://claw-use.github.io/claw-use-android/)
- [claw-use/claw-use-android](https://github.com/claw-use/claw-use-android)
- [ponikar/phone-for-ai-agent README](https://raw.githubusercontent.com/ponikar/phone-for-ai-agent/main/README.md)
- [android-a11y-mcp（LobeHub）](https://lobehub.com/zh/mcp/benoberkfell-android-a11y-mcp)
- [Core-Mate/OpenGUI](https://github.com/Core-Mate/OpenGUI)
- [OpenGUI get-started（Android 11+、无障碍截图+手势）](https://raw.githubusercontent.com/Core-Mate/OpenGUI/master/docs/get-started.md)
- [bk138/droidVNC-NG](https://github.com/bk138/droidVNC-NG)
- [droidVNC-NG 功能说明（Fossdroid）](https://fossdroid.com/a/droidvncng.html)
- [DroidVNC-NG 无障碍 API 分析](https://www.alfray.com/ralf/blog/dev/2024-09-15_analysis_droidvnc-ng_vnc_server.html)
- [remote-phone（PyPI，MediaProjection+MediaCodec+无障碍）](https://pypi.org/project/remote-phone/1.2.0/)
- [rcarmo/go-rdp-android](https://rcarmo.github.io/projects/go-rdp-android/)
- [stream-droid 控制路径文档（scrcpy/ADB/元素级控制对比）](https://raw.githubusercontent.com/davidokonji/stream-droid/HEAD/docs/control-and-semantics.md)
- [AutoJs6](https://github.com/SuperMonster003/AutoJs6)
- [Auto.js 家族现状（2026）](https://developer.aliyun.com/article/1768481)
