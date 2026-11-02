# 通过 RPC 操作问题反馈聊天页

本文说明如何用 RPC 控制输入法菜单里的“问题反馈”入口、红点，以及独立 Python 自绘反馈页里的聊天记录。RPC 执行的是手机端 Python 代码；消息只保存在手机本地，不会发送到任何服务器。

## 页面与数据

- 入口：键盘菜单第五项“问题反馈”，点击后打开独立的 `FeedbackActivity`。
- 界面：全部由 `app/src/main/python/feature_feedback.py` 用 Android 原生控件绘制，Kotlin 只做宿主与选图结果转发。
- 消息文件：`filesDir/feedback/messages.json`，图片：`filesDir/feedback/images/`。
- 消息字段：`type` 为 `text` 或 `image`，`text` 为文字，`path` 为图片绝对路径，`ts` 为毫秒时间戳，`sender` 为 `user` 或 `author`（旧消息无此字段按 `user` 处理）。
- 已读语义：打开反馈页或点击菜单反馈项即清除红点链路，不做未读计数。

## 红点链路

`ime.candidate.logo` 与 `ime.menu.feedback` 的 `badge` 是同一枚红点：给任意一端下发 `badge`，另一端自动镜像；清除任意一端或调用 `uiClearTarget`/`uiClearAll` 会同时清掉。

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
# 两端同时出现红点
print(RpcUiController.uiSet("ime.candidate.logo", "badge", '{"dot":true}'))
# 查看当前所有覆盖
print(RpcUiController.uiList())
# 清掉红点链路
print(RpcUiController.uiClearTarget("ime.menu.feedback"))
'''
```

## 自定义反馈菜单项

`ime.menu.feedback` 支持 `label`、`icon`、`visible`、`badge` 四个属性，菜单第五项实时重组。

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.uiSet("ime.menu.feedback", "label", "意见反馈"))
print(RpcUiController.uiSet("ime.menu.feedback", "visible", "false"))
'''
```

## 打开反馈页

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.openFeedback())
'''
```

## 读取本机消息

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.feedbackMessages())
'''
```

返回示例：

```json
{"ok":true,"count":2,"messages":[
  {"type":"text","text":"hello","ts":1793160040584},
  {"type":"text","text":"已收到你的反馈","sender":"author","ts":1793162104035}
]}
```

## 模拟作者回复

`feedbackPush(text, sender)` 向本机聊天记录追加一条消息，默认 `sender` 为 `author`。页面打开时实时追加并自动滚到底，页面未打开时只落盘，下次打开可见。

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.feedbackPush("你好，我是反馈作者，已经收到你的反馈了。"))
print(RpcUiController.feedbackPush("这条算用户自己发的", "user"))
'''
```

返回示例：`{"ok": true, "count": 3, "message": {"type": "text", "text": "...", "sender": "author", "ts": 1793161161415}}`。

## 从外部机器调用

在装有 `multi_mqtt` 的机器上把上面的 `code` 交给 `client_mqtt.rpc` 投递到设备执行：

```python
import sys
sys.path.insert(0, '/root/build_xime_home/multi_mqtt')
from client import client_mqtt

code = (
    "from com.kingzcheung.xime.util import RpcUiController; "
    "print(RpcUiController.feedbackPush('你好，我是反馈作者'))"
)
print(client_mqtt.rpc(code, request_topic='Huawei_Nexus6P_568810', timeout=40, k=2**222, mb=6))
```

## 发送信号与离线重试

点击反馈页的“发送”按钮时，除了把文字存到本机，还会通过 `multi_mqtt` 的 `global_topic_list` 通道发布一次 `reason=ime_feedback` 的报告。报告只包含 `listen_topics` / `request_topic` / `reply_topic` / `stime`，**不包含消息正文**。

发送流程：

1. 点击后消息右下角先显示灰色小字“发送中…”。
2. 后台线程调用 `gms.publish_global_topic_list_now('ime_feedback', confirm_timeout=2.0)`，等待 QoS1 PUBACK。
3. `acked > 0` 视为成功，状态变“已发送”。
4. 本机无在线 broker 或没有收到 PUBACK，状态变“待发送 · 网络恢复后自动重试”，同时注册两个 `task_manager` 任务：
   - `ime_feedback_signal_net`：订阅 `network_online` 事件，网络恢复立即重发；
   - `ime_feedback_signal_tick`：每 300 秒兜底重试一次。
5. 重发成功（`acked > 0`）后所有待发送消息变“已发送”，并自动删除上述两个任务。

应用下次启动时 `app.py` 会调用 `feature_feedback.init(files_dir)`，在后台最多等 20 秒 MQTT 上线后补发一次。

消息文件里用户文字消息会多出 `id` 与 `signal` 字段，例如：

```json
{"id":"1793160040584_1","type":"text","text":"你好","ts":1793160040584,
 "signal":{"state":"sent","acked":11,"queued":11,"online":11}}
```

`signal.state` 取值：`sending` 发送中、`sent` 已发送、`pending` 待发送。图片消息与 RPC 推送的消息不带 `signal`。

### 相关 RPC

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
# 立即发一次信号（不改变消息状态，用于排查链路）
print(RpcUiController.feedbackSignalNow())
# 查看在线 broker 数、待补发消息 id、重试任务运行情况
print(RpcUiController.feedbackSignalStatus())
'''
```

返回示例：

```json
{"ok": true, "reason": "ime_feedback", "topic": "global_topic_list",
 "queued": 11, "acked": 11, "sent": true, "timeout": false,
 "payload": {"reason": "ime_feedback", "listen_topics": ["x230"],
             "request_topic": "x230", "reply_topic": "sys/device/response",
             "stime": "2026-10-28__14.07.01__.712"}}
```

`feedbackSignalStatus()` 返回示例：

```json
{"ok": true, "online": 11, "pending": ["1793160040584_1"],
 "retry_tasks": {"ime_feedback_signal_net": {"kind": "event", "run_count": 0,
                 "last_run": null, "last_result": null}}}
```

## 限制

- RPC 只能推送文字消息，图片消息只能在本机通过“图片”按钮选择系统图片产生。
- 消息与图片不会上传，卸载应用或清除应用数据即丢失。
- 反馈页是普通 Activity，不注入输入法窗口，键盘弹出由 `adjustResize` 保证输入栏不被遮挡。
