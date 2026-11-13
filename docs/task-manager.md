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
| `SystemEventMonitor`（Kotlin） | 系统广播 / DisplayManager / TelephonyCallback / ContentObserver / 传感器，**记账在 Kotlin，默认不调 Python** |
| `LocationEventMonitor`（Kotlin） | GPS / 围栏 / 活动 / GNSS 事件，配置存 `filesDir/location_events.json`，独立于系统广播 |

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

`list_events()` 返回 **59 个**可订阅事件（真机实测与代码完全一致），按来源分组如下。
GPS / 围栏 / 活动 / GNSS 另有 **20 个**事件，由 `LocationEventMonitor` 自己按
`ALL_EVENTS` 过滤，**不包含在 `list_events()` 返回值里**，但同样可以传给
`add_event_task`（见文末）。

| 分组 | 事件名 |
|---|---|
| 开机 / 网络（NetworkEventMonitor、BootEventReceiver） | `boot_completed`、`wifi_connected`、`wifi_disconnected`、`network_online`、`network_offline`、`network_state` |
| 屏幕 / 电源 / 电池（广播 + DisplayManager） | `screen_on`、`screen_off`、`user_present`、`screen_rotation_changed`、`battery_low`、`battery_okay`、`battery_changed`、`power_connected`、`power_disconnected`、`doze_mode_changed`、`power_save_mode_changed`、`dreaming_started`、`dreaming_stopped`、`brightness_changed` |
| 音频 / 输入 / 通话 | `ringer_mode_changed`、`volume_changed`、`headset_plug`、`call_state_changed`、`keyboard_shown`、`keyboard_hidden` |
| 网络 / 通信开关 | `wifi_state_changed`、`connectivity_changed`、`airplane_mode_changed`、`bluetooth_state_changed`、`hotspot_state_changed`、`sim_state_changed`、`nfc_state_changed` |
| 存储 / USB / 外设 | `media_mounted`、`media_unmounted`、`media_removed`、`media_bad_removal`、`media_eject`、`storage_low`、`storage_ok`、`usb_state_changed` |
| 系统 / 时间 / 配置 | `shutdown`、`locale_changed`、`time_changed`、`timezone_changed`、`time_tick`、`next_alarm_changed`、`configuration_changed`、`wallpaper_changed`、`user_switched`、`input_method_changed`、`location_mode_changed` |
| 应用 / 传感器 / 剪贴板 | `package_added`、`package_removed`、`package_replaced`、`light_changed`、`proximity_changed`、`pressure_changed`、`clipboard_changed` |

要点：

- `boot_completed`：`BootEventReceiver` 写标记，首次 `init` 后补发，payload 带 `boot_epoch`。
- `screen_rotation_changed` 与 `call_state_changed` **不是广播**：前者来自
  `DisplayManager.DisplayListener`（payload `rotation` 0~3、`degrees`），后者来自
  `TelephonyCallback`（payload `state=idle/ringing/offhook`、`state_code`、
  `direction=incoming/outgoing`，先去电时直接 offhook）。
- `time_tick`：`ACTION_TIME_TICK` 系统广播的**记账**，名义 60s 一次；实际观测到的
  间隔存在抖动，原因与读法见〈时间语义与观测陷阱〉。
- 传感器类（`light/proximity/pressure`）有 1 秒去抖；`clipboard_changed` 只在输入法
  可见时产生，payload 只有 `has_text`/`length`，**不落剪贴板内容**。
- `brightness/volume/keyboard` 来自 ContentObserver / 输入法回调；`time_tick`、
  `battery_changed` 这类高频事件默认只记账，不进 Python（见下节）。

#### GPS / 围栏 / 活动事件（20 个，不在 `list_events()` 中）

| 分组 | 事件名 |
|---|---|
| 定位开关 | `location_provider_enabled`、`location_provider_disabled` |
| 位置数据 | `location_changed`、`location_moved`、`location_still`、`location_first_fix`、`location_lost`、`location_accuracy_changed` |
| 围栏（本地圆形自算，不依赖 GMS） | `geofence_enter`、`geofence_exit`、`geofence_dwell` |
| 活动（按速度推断） | `activity_changed`、`activity_started`、`activity_stopped` |
| GNSS | `gnss_started`、`gnss_first_fix`、`gnss_stopped`、`satellite_status_changed` |
| 传感器 / 派生 | `significant_motion`、`distance_travelled` |

> `network_state` 每次进程启动只发一次，用来解决“开机时 WiFi 早已连着、`wifi_connected`
> 永远等不到边沿”的场景：任务订阅它，再配合 `task_manager.network_state()` 判断
> “已是目标状态就跳过”，既不会重复执行也不会漏。需要严格的开机语义请用 `boot_completed`。

> 事件入口为 `fire_event()`，也可通过 `test_event()` 本地模拟，走完全相同的队列。

## 事件源与投递模型

事件由四个 Kotlin 源产生，统一汇到 `task_manager.fire_event()`：

| 事件源 | 产生方式 | 备注 |
|---|---|---|
| `NetworkEventMonitor` | `ConnectivityManager` 回调 | 只发边沿；首帧 `network_state` 快照 |
| `BootEventReceiver` / `BootEventWorker` | 开机广播 + 写标记 | Python 就绪后才补发 `boot_completed` |
| `SystemEventMonitor` | 动态注册的系统广播 + `DisplayManager` / `TelephonyCallback` / `ContentObserver` / 传感器 | 59 个事件里的 53 个；单 `HandlerThread` 串行 |
| `LocationEventMonitor` | `LocationManager` / `GnssStatus` / `SensorManager` | 20 个 GPS 事件；自带 `PENDING_MAX=64` 队列，未就绪时 5s 重试 |

### 记账与投递是两条轨

`SystemEventMonitor` 收到广播后先**无条件记账**，再按订阅集合决定是否调 Python：

```kotlin
synchronized(lock) {
    log.addLast("$now | $name | $payloadJson")     // ① 写 500 条环形缓冲，不受订阅影响
    while (log.size > LOG_MAX) log.removeFirst()
    deliver = registered.contains(name)            // ② 只有已订阅才投递
}
if (!deliver) return                               //    未订阅：到此结束，零 Python 开销
SystemEventMonitor.deliverToPython(name, payloadJson)
```

- `registered` 是 Python 写的订阅集合：`_sync_system_events()` 遍历所有
  `kind=="event"` 且 `enabled` 的任务，把它们的 `events` 并集、排序后交给
  `syncRegisteredEvents()`。增删/暂停/恢复/`init` 后都会重同步。
- 因此：**没订阅的事件不会进 Python**，只进 Kotlin 缓冲。`time_tick`、
  `battery_changed` 这类高频事件默认就是这种状态，不需要担心耗电。
- 反过来，即便没订阅，也能用 `system_events()` 回读历史，不丢。
- 订阅后 `deliverToPython()` 是**同步调用**，跑在 `sys-event-monitor` 这同一个
  HandlerThread 上；Python 侧慢（持 GIL 的长任务）会把这个线程堵住，但这只影响
  后续投递，记账依旧发生在它前面。
- Python 未 `init` 时到达的事件先进 `_PRE_INIT_EVENTS`（最多 64 条），`init` 后
  按原顺序补发；任务表一旦就绑，队列就由 `_EVENT_Q` 串行消费。

## RPC API

| 函数 | 说明 |
|---|---|
| `add_task(task_id, func_name=None, interval=60, code=None, enabled=True)` | 新增/覆盖定时任务；`interval` 单位秒 |
| `add_daily_task(task_id, at="00:00", code=None, func_name=None, enabled=True)` | 新增/覆盖每日任务；返回带 `next_in` 秒数 |
| `add_event_task(task_id, event=None, code=None, func_name=None, debounce=0.0, enabled=True, events=None)` | 新增/覆盖事件任务；`event` 与 `events` 可同时给，去重合并 |
| `remove_task(task_id)` | 删除任务并取消调度 |
| `pause_task(task_id)` / `resume_task(task_id)` | 暂停 / 恢复（恢复时重新接力下一次） |
| `run_now(task_id)` | 立即执行一次（定时任务按 0 延迟调度，事件任务直接入队） |
| `list_tasks()` | 全部任务及其状态 |
| `list_events()` | 59 个可订阅事件名（**不含** GPS 的 20 个） |
| `recent_events(limit=20)` | Python 侧最近事件环形缓冲（最多 50 条） |
| `network_state()` | 当前网络快照 `{wifi, online, validated, initialized, ts}` |
| `system_events(limit=50, return_str=False)` | Kotlin 系统事件缓冲（最多 500 条），见下节 |
| `clear_system_events()` | 清空 Kotlin 系统事件缓冲，返回 `{ok: true}` |
| `test_event(event, payload=None)` | 本地模拟事件，走与系统事件相同的队列 |
| `consume_boot_pending()` | 幂等消费开机待发标记并派发 `boot_completed` |
| `is_initialized()` | Python 任务表是否已 `init` |
| `register_func(name, func)` | 注册内置函数供 `func_name` 引用 |
| `fire_event(event_name, payload=None, source="system")` | **内部入口**，Kotlin 回调专用，不对外 |
| `execute_task(task_id)` | **内部入口**，Worker 触发单次执行 |

同一 `task_id` 再次添加即为**覆盖**（`run_count` 等统计归零）。

## 事件缓冲与读取

两级缓冲，别搞混：

| 缓冲 | 位置 | 容量 | 读取方式 |
|---|---|---|---|
| Kotlin 系统事件缓冲 | `SystemEventMonitor` 内存 | 500 条（`LOG_MAX`） | `system_events(limit)` / `clear_system_events()` |
| Python 最近事件 | `_RECENT_EVENTS` | 50 条（`_RECENT_MAX`） | `recent_events(limit)` |

- Kotlin 缓冲存**已发生的事件**，与是否订阅无关；每条一行
  `epoch_ms | event | payload_json`，`ts` 写于广播到达 receiver 的那一刻。
- `system_events(limit=50, return_str=False)`：
  - `limit<=0` 取全量（上限 500）；
  - 默认返回
    `{"ok": true, "count": N, "registered": [...], "events": [{"ts_ms", "time", "event", "payload"}, ...]}`，
    `time` 为设备本地时间；
  - `return_str=True` 返回多行文本，每行 `时间 | 事件名(左对齐 24) | payload`，
    已去掉与行首重复的 `ts` / `action` 字段，适合直接打印。
  - `registered` 就是当前投递集合，用它确认“某事件到底有没有订上”。
- `clear_system_events()` 只清 Kotlin 缓冲（Python 的 50 条不受影响）。
- 读取是**拉快照**：Python 不参与记账，读多读少、什么时候读，都不会改变
  事件本身的时间戳与内容。

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

## 时间语义与观测陷阱

`time_tick` 就是 `ACTION_TIME_TICK`：`system_server` 名义上每分钟整分发出，带
`FLAG_RECEIVER_REGISTERED_ONLY`，只能动态注册（manifest 收不到）。周期不可配、
精度不保证，**它不是调度器**。

记录到的 `ts` 是广播**派发到 receiver** 的时刻（记账时取 `System.currentTimeMillis()`），
不是 `system_server` 排程时刻，也不是 Python 读取时刻。所以两次 `ts` 之差是
`60 + (δₙ₊₁ − δₙ)`，δ 是链路任意一级的抖动。真机实测的异常都对应得上：

| 现象 | 根因 |
|---|---|
| 90s（另 43/47/12/11/16s 等零散值） | 灭屏后 Doze / 进程被冻结约 30s，醒来补投一条 |
| 1.13s / 1.72s / 2.61s 连发 | 冻结或广播排队期间积压的多条 tick，解冻后被一次性 flush |
| 整分不对齐（实测落在 `:47.9`） | 该机型不是墙钟整分对齐，或派发被系统性推迟 |
| 跳变（实测 `06:13:47.982 → 06:14:03.760`） | 墙钟被 NTP / 手动校时，这段统计作废 |

当前 payload 只有 `action` 与 `ts`（墙钟毫秒），单位不足以区分“设备睡了”“进程被冻”
“时钟被校”。要定性就补两个单调时钟：`elapsedRealtime()` 含深睡、`uptimeMillis()` 不含。

| 墙钟 gap | elapsed gap | uptime gap | 结论 |
|---|---|---|---|
| 90 | 90 | 60 | 深睡 30s，源周期仍是 60s |
| 90 | 90 | 90 | 进程冻结 / 广播排队，源周期仍是 60s |
| 90 | 90 | 90 且 `dumpsys` 同样显示 90 | 厂家真改了源（少见） |
| 跳变 | 稳定 | 稳定 | 墙钟被校时，数据不可信 |

排查方向在系统状态，不在本框架代码：

```bash
adb shell dumpsys activity processes | grep -i frozen
adb shell am get-standby-bucket <包名>
adb shell dumpsys deviceidle
```

另外注意统计口径：Kotlin 缓冲只有 500 条，且只保留已记录的事件，丢掉的 tick 不会留痕；
用约 2 小时的窗口算间隔时，不要把它当成全天分布。

结论：要秒级、可配的节奏就用 `interval` 任务（`OneTimeWorkRequest` 自接力）或自己的
前台服务 / `setExactAndAllowWhileIdle`；`time_tick` 只适合“墙上时钟走过一分钟”的观测。

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