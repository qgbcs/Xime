# feature_touch 与 MQTT 唤起无障碍权限

本文说明 `feature_touch` 的功能边界、`service_not_connected` 的成因，以及如何只用 MQTT RPC 把设备带到「开启无障碍」的位置并验证。

## 1. 结论

- `feature_touch` 只是 `XimeAccessibilityService` 的 Python 封装，**所有能力都要求无障碍服务已连接**。
- 无障碍权限**不能被应用自己授予**：没有「弹窗授权」API，只能跳系统页面由用户手动打开开关（或由 adb 写 Secure 设置）。
- 远程能做的三件事：诊断状态、打开对应的系统页面、打开应用详情页（应对 Android 13+ 侧斗包的「允许受限制的设置」）。
- 开启后所有 RPC（`dump_ui` / `tap_text` / `screenshot` / `ui_html(p)` 等）立即恢复，无需重启应用。

## 2. feature_touch 是什么

文件：`app/src/main/python/feature_touch.py`；底层：`app/src/main/java/com/kingzcheung/xime/service/XimeAccessibilityService.kt`。

| 函数 | 作用 |
|---|---|
| `status()` | 服务是否连接、能力上限、最近一次结果 |
| `diagnose()` | 体检：类是否在、是否连接、系统开关、启用列表、提示 |
| `dump_ui(max_nodes=400, include_empty=False)` | 读当前界面节点树（可跳应用） |
| `find_text(text, exact=False)` | 按文本找节点 |
| `tap_text(text, index=0, exact=False)` | 按文本点击 |
| `wait_text(text, timeout=10.0)` | 等文本出现 |
| `tap(x, y, duration_ms=80)` / `long_press` | 坐标点击 / 长按 |
| `swipe(...)` / `drag(...)` / `pinch(...)` | 滑动 / 拖拽 / 双指缩放 |
| `gesture(strokes)` | 原始多段手势 |
| `global_action(action)` | 全局动作（返回/主页/通知栏等） |
| `start_activity(action)` | 以 action 拉起页面 |
| `perform_action(node_ref, action, text="")` | 对节点执行无障碍动作 |
| `set_text(node_ref, text)` / `paste_text(node_ref)` | 写入 / 粘贴文本 |
| `windows()` | 当前窗口列表 |
| `event_capture(enabled, ttl_ms)` / `event_state()` | 开启/读取无障碍事件捕获 |
| `screenshot(window_id=-1)` | 无障碍截图（受 FLAG_SECURE 限制） |

所有函数默认返回 JSON 字符串；`return_json=False`（别名 `json` / `j`）返回 Python 原生 `dict` / `list`。

## 3. service_not_connected 是什么意思

`XimeAccessibilityService.instance == null`，即系统没有把服务绑定起来。实测 `diagnose()` 两种典型输出：

```json
// 情况 A：开关和启用列表都是空的
{"connected": false, "class_present": true,
 "accessibility_enabled": 0, "enabled_accessibility_services": "",
 "component_enabled": false}

// 情况 B：已写入启用列表但未绑定
{"connected": false, "component_enabled": true,
 "accessibility_enabled": 0}
```

- A 通常是用户没开（或侧斗包被「受限设置」拦住）。
- B 通常是被系统/省电策略杀掉，或无障碍总开关为 0；重新开关一次或重启服务。
- `class_present: false` 说明当前 APK 根本没有这个类，需要装新包。

## 4. 用 MQTT RPC 唤起权限页

以下代码都通过原有 MQTT RPC 通道执行（请求 topic 以设备 rpc.json 为准，例中为 `14c`）。`app` 是启动时运行的模块，`RpcUiController` 就在它的 globals 里。

### 4.1 诊断

```python
import feature_touch
print(feature_touch.diagnose())
```

Python 客户端：

```python
d = client_mqtt.rpc('import feature_touch\nprint(feature_touch.diagnose())',
                    '14c', timeout=30, k=2**222)
print(d['stdout'])
```

### 4.2 打开系统「无障碍」页

```python
import json, app
print(app.RpcUiController.startIntent(json.dumps({
    "action": "android.settings.ACCESSIBILITY_SETTINGS"
})))
```

成功返回 `{"ok":true,"action":"android.settings.ACCESSIBILITY_SETTINGS"}`，手机上会直接弹出无障碍页。

### 4.3 打开应用详情页（受限设置 / 自启动 / 耗电白名单）

Android 13+ 侧斗安装的包，无障碍开关可能是置灰的，需要先去应用信息里「允许受限制的设置」：

```python
import json, app
print(app.RpcUiController.startIntent(json.dumps({
    "action": "android.settings.APPLICATION_DETAILS_SETTINGS",
    "data": "package:com.qgb.xime"
})))
```

### 4.4 直接打开本服务的详情页（部分 ROM 支持）

```python
import json, app
print(app.RpcUiController.startIntent(json.dumps({
    "action": "android.settings.ACCESSIBILITY_DETAILS_SETTINGS",
    "data": "package:com.qgb.xime/com.kingzcheung.xime.service.XimeAccessibilityService"
})))
```

该 action 的入参形态 ROM 差异较大，失败时退回 4.2 的列表页。

### 4.5 UI 入口

设置 → 权限设置 → 特殊权限 → **辅助功能（无障碍）**，状态取自 `XimeAccessibilityService.isConnected()`，点击跳无障碍页（实现见 `ui/settings/PermissionSettingsScreen.kt`）。

## 5. adb 备用方案

有 adb 通道时可直接写 Secure 设置（无需人工点击）：

```bash
adb shell settings put secure enabled_accessibility_services \
  com.qgb.xime/com.kingzcheung.xime.service.XimeAccessibilityService
adb shell settings put secure accessibility_enabled 1
```

注意：包名/组件名以设备实际安装为准（`feature_touch.diagnose()` 会回显 `component` 字段）。Android 13+ 可能仍需先允许受限设置。

## 6. 验证

```python
import feature_touch
print(feature_touch.status())      # 期望 connected: true
print(feature_touch.dump_ui(20))   # 期望 nodes 非空
```

`connected: true` 后，`http://<host>:5555/ui_html(p)` 的元素视图、`tap_text`、`screenshot` 等全部可用。

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `service_not_connected` + `component_enabled:false` | 无障碍未启用 | 4.2 跳页手动开；或 5 的 adb |
| 开关置灰 | Android 13+ 受限设置 | 4.3 应用信息 → ⋮ → 允许受限制的设置 |
| `component_enabled:true` 但 `connected:false` | 进程被系统回收/开关为 0 | 重新关开一次，检查 `accessibility_enabled` |
| 刚开就断 | 国产 ROM 省电策略 | 加入电池白名单、允许自启动 |
| `class_present:false` | 旧 APK 无此服务类 | 安装新版 APK |
| `screenshot` 全黑 | 目标窗口 `FLAG_SECURE` | 无解，改用 `dump_ui` |

## 8. 安全提示

- 开启无障碍后，任何能访问 MQTT/HTTP RPC 的人都能读屏、点击、输入。
- 建议同时给 HTTP RPC 配 `http_key` 或 `basic_auth`，并避免把 1883/1144 直接映射到公网。
- 不用时及时关闭无障碍开关。

## 9. 相关文件

- `app/src/main/python/feature_touch.py`
- `app/src/main/java/com/kingzcheung/xime/service/XimeAccessibilityService.kt`
- `app/src/main/java/com/kingzcheung/xime/ui/settings/PermissionSettingsScreen.kt`
- `app/src/main/java/com/kingzcheung/xime/util/RpcUiController.kt`
- `docs/android-无障碍实时屏幕控制调研.md`
