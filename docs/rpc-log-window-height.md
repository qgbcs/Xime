# RPC 运行日志窗口高度

设置页运行日志卡片总高度以 dp 为单位，范围从日志正文顶部到“自动换行/滚到底”控制行底部；卡片不再显示“运行日志”标题，日志正文占据剩余空间并可滚动。

- 新安装或尚未通过 RPC 设置时，卡片总高度默认值为 `1 dp`。
- 通过 `RpcUiController.setState("log.height.dp", value)` 设置的高度会写入应用私有 SharedPreferences，应用进程重启后仍保留。
- 高度允许 `0` 到 `1000 dp`；负数、非数字和非有限数会被拒绝。
- 连续 1.5 秒内点击最顶部的“输入法设置”标题 4 次，会临时把整个日志卡片高度恢复为 `360 dp`。此操作不改写持久化值；应用重启后恢复为最近一次 RPC 设置值。

## 查询高度

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print({
    "runtime_height_dp": RpcUiController.getState("log.height.dp"),
    "temporary_override_dp": RpcUiController.getState("log.height.override.dp"),
})
'''
```

`runtime_height_dp` 是持久化高度。标题四击后，`temporary_override_dp` 会显示为 `"360"`；未触发临时覆盖时该值为 `None`。

## 持久设置高度

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController

RpcUiController.setState("log.height.dp", "20")
print(RpcUiController.getState("log.height.dp"))
'''
```

传入 `"1"` 会将整个日志卡片设为 `1 dp` 高，日志正文和控制行都包含在这个总高度内；新安装默认值也是 `1 dp`。如果只想临时恢复可读高度，不要通过 RPC 改写配置，直接四击页面顶部标题即可。
