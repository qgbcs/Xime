# 通过 RPC 显示/隐藏剪贴板「清空」按钮

本文说明如何通过应用内置的 Python RPC 控制剪贴板面板右上角「清空剪贴板」按钮的显隐。该开关只影响界面渲染，不会删除或修改任何剪贴板条目；`showClearConfirm` 确认弹窗与 `clearAll()` 逻辑保持不变。

## 行为

- 目标槽位：`ime.clipboard.clearall`；可下发属性：`visible`；合法值：`true` / `false`（仅小写，其它值返回 `visible_must_be_true_or_false`）。
- 默认显示。`RPC` 下发后当前键面立即重组；同时写入应用私有 `kime_settings`，进程/输入法重启后仍保持。
- 持久化键：`clipboard_clear_all_visible`，默认 `true`。该键属于内置 `kime_settings`，会随「外置设置镜像」一起落盘到 `/sdcard/Alarms/xime/prefs/kime_settings.xml`；启动时 `SettingsMirror.reconcile` 以外部为准覆盖内置。因此清除应用数据/卸载重装后，只要外置镜像仍在，就会恢复上次的值；外置镜像也不存在时按默认 `true`（显示）。
- 生效优先级：进程内 UI 覆盖（`UiOverrides`）优先；没有覆盖时回落到持久化设置。
- `uiClear` / `uiClearTarget` / `uiClearAll` 会移除内存覆盖，并把持久化值恢复为 `true`（即默认显示）。

## 代码位置

| 文件 | 职责 |
| --- | --- |
| `app/src/main/java/com/kingzcheung/xime/util/UiOverrides.kt` | 定义槽位常量 `TARGET_CLIPBOARD_CLEAR_ALL = "ime.clipboard.clearall"` 与内存覆盖存储 |
| `app/src/main/java/com/kingzcheung/xime/util/RpcUiController.kt` | `UI_TARGET_PROPS` 白名单（仅 `visible`）；`uiSet` 持久化；`uiClear*` 恢复默认 |
| `app/src/main/java/com/kingzcheung/xime/settings/SettingsPreferences.kt` | `isClipboardClearAllVisible` / `setClipboardClearAllVisible`，键 `clipboard_clear_all_visible` |
| `app/src/main/java/com/kingzcheung/xime/ui/menubar/ClipboardView.kt` | 读取覆盖，缺省回落持久化值；仅包裹右上角按钮的渲染条件 |

## 隐藏按钮

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
r = RpcUiController.uiSet('ime.clipboard.clearall', 'visible', 'false')
'''
```

## 显示按钮

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
r = RpcUiController.uiSet('ime.clipboard.clearall', 'visible', 'true')
'''
```

## 恢复默认

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
r = RpcUiController.uiClear('ime.clipboard.clearall', 'visible')
'''
```

## 查询

查询当前进程内覆盖（重启后为空，属正常）：

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
r = RpcUiController.uiList()
'''
```

查询持久化设置（重启后以此为准）：

```python
code = r'''
from com.kingzcheung.xime import XimeApplication
prefs = XimeApplication.getInstance().getSharedPreferences('kime_settings', 0)
r = prefs.getBoolean('clipboard_clear_all_visible', True)
'''
```

## 注意

- 未知槽位返回 `unknown_target:<target>`，未知属性返回 `unknown_prop:<target>#<prop>`。
- 按钮只在剪贴板列表非空时才会渲染，所以空库时本就不可见，与本开关无关。
- 持久化值在 `ClipboardView` 首次组合时通过 `remember` 读入；若面板已展开，`uiClear*` 移除内存覆盖后需关闭并重新打开面板（或重启输入法）才会按新的持久化值渲染。
