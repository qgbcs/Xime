# task_manager 使用说明

高可靠后台任务框架（Python 侧），支持**定时**、**每日定时**、**事件驱动**三类任务。
任务一律由 RPC 远程下发，APK 内不硬编码任何任务（`init` 只从 JSON 恢复），
状态持久化在 `filesDir/task_manager.json`，重启/进程被杀后自动恢复。

## 架构

| 层 | 职责 |
|---|---|
| Python `task_manager` | 任务注册表、状态持久化、执行统计、接力下一次调度 |
| WorkManager（Kotlin） | 系统级可靠调度，进程死亡/设备重启后仍能触发 |
| `NetworkEventMonitor`（Kotlin） | 网络边沿事件：WiFi 连/断、网络通/断 |
| `BootEventReceiver` / `BootEventWorker`（Kotlin） | 开机事件：写标记并等 Python 就绪后补发 |

定时任务的调度模型：Python 用 `OneTimeWorkRequest` **自接力**实现任意秒级周期
（`PeriodicWorkRequest` 最小 15 分钟，不满足需求），执行完在 Python 侧重新入队下一次。

## 触发方式

### 1. 定时 interval

```python
r = task_manager.add_task("heartbeat", interval=60, code='print("tick")')
r = task_manager.add_task("mqttstat", func_name="mqtt_stats", interval=300)
```

### 2. 每日定时 daily

`at="HH:MM"` 或 `"HH:MM:SS"`，按**设备本地时区**每天到点触发，执行后自动接力到明天。

```python
r = task_manager.add_daily_task("daily_report", "08:30", code='print("[每日] 触发")')
# 返回 {"ok": True, "id": ..., "kind": "daily", "at": "08:30", "next_in": 秒数}
```

### 3. 事件 event

订阅 Kotlin 侧事件，`debounce` 为同一任务两次触发的最小间隔（秒，0 表示不节流）。

```python
r = task_manager.add_event_task("wifilog", "wifi_connected",
                                code='print("[事件] WiFi 已连接", payload)')
r = task_manager.add_event_task("npboot", "boot_completed",
                                code='print("[开机]", payload)')
```

#### 事件清单

| 事件名 | 触发时机 |
|---|---|
| `boot_completed` | 设备开机完成（`BootEventReceiver` 在 Python 就绪后补发，含 `boot_epoch`） |
| `wifi_connected` | 默认网络切到 WiFi 的边沿进入 |
| `wifi_disconnected` | 默认网络离开 WiFi |
| `network_online` | 默认网络具备 INTERNET 能力位（连网） |
| `network_offline` | 默认网络失去 INTERNET（断网） |
| `network_state` | 进程启动首帧的网络快照（非边沿，payload 带 `initial: true`，与 `wifi_connected` 区分） |

> `network_state` 每次进程启动只发一次，用来解决“开机时 WiFi 早已连着、`wifi_connected`
> 永远等不到边沿”的场景：任务订阅它，再配合 `task_manager.network_state()` 判断
> “已是目标状态就跳过”，既不会重复执行也不会漏。需要严格的开机语义请用 `boot_completed`。

> 事件入口为 `fire_event()`，也可通过 `test_event()` 本地模拟，走完全相同的队列。

## RPC API

| 函数 | 说明 |
|---|---|
| `add_task(task_id, func_name=None, interval=60, code=None, enabled=True)` | 新增/覆盖定时任务 |
| `add_daily_task(task_id, at="00:00", code=None, func_name=None, enabled=True)` | 新增/覆盖每日定时任务 |
| `add_event_task(task_id, event, code=None, func_name=None, debounce=0.0, enabled=True)` | 新增/覆盖事件任务 |
| `remove_task(task_id)` | 删除任务并取消调度 |
| `pause_task(task_id)` / `resume_task(task_id)` | 暂停 / 恢复（恢复时重新接力下一次） |
| `run_now(task_id)` | 立即执行一次（定时任务按 0 延迟调度，事件任务直接入队） |
| `list_tasks()` | 全部任务及其状态 |
| `list_events()` | 可订阅事件名 |
| `recent_events(limit=20)` | 最近事件环形缓冲（最多 50 条） |
| `network_state()` | 当前网络快照 `{wifi, online, validated, initialized, ts}` |
| `test_event(event, payload=None)` | 本地模拟事件 |
| `consume_boot_pending()` | 幂等消费开机待发标记并派发 `boot_completed` |

同一 `task_id` 再次添加即为**覆盖**（`run_count` 等统计归零）。

## 任务动作（二选一，`code` 优先）

- **`code`**：随任务下发的任意 Python 源码，运行在独立守护线程。`print` 既进
  App 日志窗口，又被捕获进 `last_result` 便于 RPC 回读校验。
  - 可用变量：`event`（事件名）、`payload`（事件负载 dict）、`task_id`、`jclass`（Chaquopy）
- **`func`**：内置函数名，仅保留少量示例（`mqtt_stats`、`heartbeat`），可用 `register_func()` 扩展

## 任务状态字段

```json
{
  "kind": "daily", "at": "08:30", "code": "...", "enabled": true,
  "run_count": 3, "last_run": "2026-10-27 08:30:00",
  "last_result": "[每日] 触发",
  "last_event": "boot_completed", "last_payload": {...},
  "created": "2026-10-26 11:11:08", "_last_fire_epoch": 1793075961.7
}
```

## 示例：开机联网后同步标准时间

```python
CODE = '''
import email.utils, socket, subprocess, time, urllib.request

def online():
    try:
        socket.create_connection(("www.baidu.com", 80), 5).close()
        return True
    except OSError:
        return False

for _ in range(60):                    # 最多等 5 分钟
    if online():
        break
    time.sleep(5)
else:
    raise RuntimeError("无网络，跳过校时")

resp = urllib.request.urlopen("http://www.baidu.com", timeout=10)
ts = int(email.utils.parsedate_to_datetime(resp.headers["Date"]).timestamp())

def su(cmd):
    p = subprocess.run(["su", "-c", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=20)
    return p.returncode, p.stdout.decode().strip(), p.stderr.decode().strip()

print("[timesync] std=%d" % ts)
print("[timesync] auto_time", su("settings put global auto_time 0")[0])
print("[timesync] setdate", su("date -u @%d" % ts))
print("[timesync] now", su("date +%s"))
'''

r = task_manager.add_event_task("boot_timesync", "boot_completed", code=CODE)
```

## 注意事项

- 定时任务的真正触发依赖 WorkManager；系统省电策略可能带来轻微延迟。
- 事件任务需要 Kotlin 侧事件源正常工作（网络回调 / 开机广播），
  Python 未就绪期间的事件会排队补投，不丢。
- 修改设备时间是特权操作，`code` 内的 `su -c "date ..."` 需要设备已 root。
- 任务执行异常不会中断框架，异常信息写入 `last_result`（以 `ERROR:` 开头）。

## RPC 调用示例

```python
from client import client_mqtt

d = client_mqtt.rpc('import task_manager as tm; print(tm.list_tasks())',
                    request_topic="<设备主题>", timeout=30, k=<私钥>)
# 若设备时钟偏差超窗被 rejected=timestamp_window，用服务器时钟重发：
d = client_mqtt.rpc(CODE, request_topic="<设备主题>", timeout=30, k=<私钥>,
                    timestamp_ms=d["server_time"])
```