# 通过 RPC 查询和修改输入法候选栏按钮

本文说明如何通过应用已有的 Python RPC 查询、设置候选栏工具按钮。RPC 执行的是手机端 Python 代码；下面的命令通过应用 `SharedPreferences` 读写正式设置，不是临时 UI 状态。

## 按钮配置的行为

- 设置保存在应用私有 `kime_settings` SharedPreferences 中，键名为 `toolbar_buttons`。
- 值是逗号分隔的按钮 ID；顺序就是自定义按钮的显示顺序。
- 全新安装且尚未修改过设置时，默认顺序为 `emoji`、`edit`、`home`、`end`、`copy`、`paste`、`clipboard`。
- 手机当前配置（RPC 查询结果）：`edit`、`home`、`end`、`copy`、`paste`、`clipboard`。
- RPC 写入 SharedPreferences 后会持久保存，应用/进程重启后仍保留；清除应用数据或卸载应用会清除该设置。
- 目前可用 ID：

| ID | 按钮 |
| --- | --- |
| `emoji` | 表情 |
| `clipboard` | 剪贴板 |
| `schema` | 方案选择 |
| `quick_phrase` | 快捷发送 |
| `symbol` | 符号 |
| `float` | 悬浮 |
| `select_all` | 全选 |
| `copy` | 复制 |
| `paste` | 粘贴 |
| `home` | 段首 |
| `end` | 段尾 |
| `handwriting_lookup` | 手写找字 |
| `edit` | 编辑 |
| `voice` | 语音 |

手写找字按钮只有在设备已具备手写模型时才会出现在可选列表中。设置该 ID 不会安装模型或绕过这一条件。

## 查询当前配置
code = r'''
from com.kingzcheung.xime import XimeApplication
from com.kingzcheung.xime.keyboard import ToolbarButton

app = XimeApplication.getInstance()
prefs = app.getSharedPreferences("kime_settings", 0)
raw = prefs.getString("toolbar_buttons", "")
buttons = [item for item in str(raw).split(",") if item]
r=buttons
'''



## 修改按钮

将 `buttons` 改为需要显示的 ID 列表。以下示例设置表情、剪贴板、方案选择，并按该顺序显示：

code = r'''
from com.kingzcheung.xime import XimeApplication
from com.kingzcheung.xime.keyboard import ToolbarButton

app = XimeApplication.getInstance()
prefs = app.getSharedPreferences("kime_settings", 0)
buttons = ["emoji",'edit', 'home', 'end', 'copy', 'paste', 'clipboard']
available = {str(button.getId()) for button in ToolbarButton.values()}
unknown = [button for button in buttons if button not in available]
if unknown:
    raise ValueError("未知候选栏按钮 ID: " + ", ".join(unknown))

prefs.edit().putString("toolbar_buttons", ",".join(buttons)).apply()
'''
 设置以后必须重启 才能有效



 
 ## 实现位置

- 按钮 ID、名称和默认集合：`app/src/main/java/com/kingzcheung/xime/keyboard/ToolbarButton.kt`
- 偏好键及读写：`app/src/main/java/com/kingzcheung/xime/settings/SettingsPreferences.kt`
- 候选栏按钮自定义 UI：`app/src/main/java/com/kingzcheung/xime/ui/menubar/ToolbarCustomizeView.kt`
- 自定义面板的持久化和即时 UI 更新：`app/src/main/java/com/kingzcheung/xime/service/ImeKeyboardCallbacks.kt`
- 输入会话读取已保存按钮：`app/src/main/java/com/kingzcheung/xime/service/XimeInputMethodService.kt`

当前 `RpcUiController` 没有专门的候选栏按钮 get/set RPC 方法；本文命令使用 `XimeApplication` 提供的应用上下文访问同一份偏好设置。
