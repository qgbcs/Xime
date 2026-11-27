# task_manager 使用说明

高可靠后台任务框架（Python 侧），支持**定时 interval**、**指定时刻 at（一次性）**、
**每日定时 daily**、**事件驱动 event**四类触发方式，并可在同一个任务上组合
（定时/时刻 + 系统事件）。
任务一律由 RPC 远程下发，APK 内不硬编码任何任务（`init` 只从 JSON 恢复），
状态持久化在 `filesDir/task_manager.json`（`{"version": 1, "tasks": {...}}`，紧凑 JSON，
`flush` + `fsync` + `os.replace` 原子替换）；文件损坏时先备份为 `task_manager.json.bad.<epoch>`
再重建，重启/进程被杀后自动恢复。

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
下一次排程在 `trigger()` 入队前就已完成，因此本次执行失败或进程被杀也不会断链。

执行模型：Kotlin 回调（`fire_event`）、WorkManager 到点（`execute_task`）、RPC `run_now`
全部汇到 `trigger()`，写入有界队列 `_EVENT_Q`（上限 256），由唯一守护线程
`PyTaskEvent` 串行取出执行；队列满时丢弃并累计 `_RUN_DROPPED`，绝不阻塞调用方。
每个任务带自增 `gen`，覆盖/删除后旧代际的已入队触发自动失效。

## 触发方式

所有触发方式都统一到 `add_task(task_id, func_name=None, code=None, enabled=True, **opts)`；
`opts` 只接受 `interval` / `at` / `repeat` / `events` / `debounce` / `misfire_grace` 六个键，出现未知键
直接返回 `{"ok": False, "error": "不支持的参数: [...]", "allowed": [...]}`。
`add_daily_task` / `add_at_task` / `add_event_task` 只是保留的兼容包装（内部转调 `add_task`）。
`interval` 优先于 `at`，`at` 优先于 `events`。

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

订阅 Kotlin 侧事件。`debounce` 是**任务级**最小触发间隔（秒，0 表示不节流），
对事件、定时、`run_now` 一视同仁，统一在 `trigger()` 里判定。

```python
r = task_manager.add_event_task("wifilog", "wifi_connected",
                                code='print("[事件] WiFi 已连接", payload)')
r = task_manager.add_event_task("npboot", "boot_completed",
                                code='print("[开机]", payload)')
```

### 4. 指定时刻 at（一次性）

`at` 支持两种写法，到点后以**伪事件 `at`** 分发，只触发一次，触发后自动置
`enabled=false`（`last_run` / `last_result` 保留）：

```python
# 绝对日期时间（只触发一次）
r = task_manager.add_at_task("once", "2026-12-01 08:30:00", code='print("一次性", event)')
# 当天 20:15（已过则顺延到明天），同样只触发一次
r = task_manager.add_at_task("tonight", "20:15", code='print("今晚", event)')
# 返回 {"ok": True, "id": ..., "kind": "at", "at": "2026-12-01 08:30:00",
#        "next_epoch": 1796..., "next_in": 秒数, "events": []}
```

`misfire_grace`（默认 60 秒，仅绝对一次性 `at` 生效）：设备休眠/进程被杀导致错过目标
时刻时，未超宽限则立即补跑一次；超过宽限则不再补跑，直接置 `enabled=false`、
`next_epoch=null`，`last_result="MISSED: 过期超过 misfire_grace"`。

### 组合：定时/时刻 + 系统事件

同一个任务可以既有排程又有系统事件订阅，`events` 始终记录系统事件：

```python
# 每 60 秒跑一次，同时 WiFi 一连上也跑一次（event 为 interval 或 wifi_connected）
r = task_manager.add_task("tick", interval=60, events=["wifi_connected"],
                          code='print("tick", event, payload)')

# 用 add_event_task 也能叠加排程
r = task_manager.add_event_task("mix", events=["boot_completed"], interval=300,
                                code='print("mix", event)')
```

`kind` 取主调度类型，优先级 `interval > at > daily > event`；`events` 不受影响。
Kotlin 只对 `events` 里列出的真实系统事件回调 Python（`interval`/`at`/`daily`
这三个伪事件名永远不会同步给 Kotlin）。

定时到点后任务代码拿到的 `event` 分别是 `"interval"` / `"at"` / `"daily"`；
payload 里带 `task_id`、`scheduled_at`、`fired_at`，以及 `interval` 或 `at`。

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
| 输入法（`ImeEventBridge`，由 RPC 事件任务消费） | `ime_keys`、`clipboard_text`、`speech_recorded` |

要点：

- `boot_completed`：`BootEventReceiver` 写标记，首次 `init` 后补发，payload 带 `boot_epoch`。
  同一进程内最多派发一次（预初始化补发与 boot marker 撞车会自动去重）；
  `consume_boot_pending()` 在派发成功后才删除 marker，宁可崩溃重发一次也不静默丢。
- `screen_rotation_changed` 与 `call_state_changed` **不是广播**：前者来自
  `DisplayManager.DisplayListener`（payload `rotation` 0~3、`degrees`），后者来自
  `TelephonyCallback`（payload `state=idle/ringing/offhook`、`state_code`、
  `direction=incoming/outgoing`，先去电时直接 offhook）。
- `time_tick`：`ACTION_TIME_TICK` 系统广播的**记账**，名义 60s 一次；实际观测到的
  间隔存在抖动，原因与读法见〈时间语义与观测陷阱〉。
- 传感器类（`light/proximity/pressure`）有 1 秒去抖；`clipboard_changed` 只在输入法
  可见时产生，payload 只有 `has_text`/`length`，**不落剪贴板内容**。
- 输入法事件来自 `ImeEventBridge`，**主流程只做一次订阅判断 + O(1) 入队**，JSON 序列化、
  Python 调用与文件 I/O 全部在独立 `ime-event-bridge` 线程完成；无任务订阅时直接返回，零开销。
  - `ime_keys`：200ms 合批，payload `items=[[ts,key,shifted],...]` + `dropped`（丢弃的最旧条数）；
    单批上限 200 条。该事件**不写** `recent_events` 环形缓冲（`record=False`），避免刷掉系统事件。
  - `clipboard_text`：`ts`、`text`（剪贴板内容，由任务决定是否落盘）。
  - `speech_recorded`：`path`（App 私有 cache 下 `audio-archive/` 的 wav 临时文件）、`text`、
    `started_ms`、`duration_ms`；任务负责把文件 move 到最终位置，Kotlin 不再硬编码目录。
    无 `speech_recorded` 订阅时连临时文件都不创建。
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

- `registered` 是 Python 写的订阅集合：`_sync_system_events()` 汇总**所有启用任务**
  （含 interval / at / daily）的 `events` 并集，排除 `interval`/`at`/`daily` 三个伪事件名
  后排序交给 `syncRegisteredEvents()`。增删/暂停/恢复/`init` 后都会重同步。
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
| `add_task(task_id, func_name=None, code=None, enabled=True, **opts)` | **统一建任务入口**。`opts` 只允许 `interval` / `at` / `repeat` / `events` / `debounce` / `misfire_grace`，出现未知键直接返回 `{"ok": false, "error": "不支持的参数: [...]", "allowed": [...]}`。`interval`（秒）→ interval；否则 `at`（`YYYY-MM-DD HH:MM[:SS]` / `HH:MM[:SS]`，Python 侧也接受 `datetime.datetime`）→ 一次性（`repeat=True` 且 `HH:MM[:SS]` 时为每日）；否则 `events` → 纯事件；三者至少一个。`interval` 优先于 `at`，`events` 可与任意 kind 叠加 |
| `add_daily_task(task_id, at="00:00", code=None, func_name=None, enabled=True)` | 兼容包装 → `add_task(..., at=at, repeat=True)` |
| `add_at_task(task_id, at, code=None, func_name=None, enabled=True, events=None, interval=None)` | 兼容包装 → `add_task(..., at=at)`（一次性时刻） |
| `add_event_task(task_id, event=None, code=None, func_name=None, debounce=0.0, enabled=True, events=None, interval=None, at=None)` | 兼容包装 → `add_task`（`event`/`events` 合并去重） |
| `remove_task(task_id)` | 删除任务并取消调度 |
| `pause_task(task_id)` / `resume_task(task_id)` | 暂停 / 恢复（恢复时重新接力下一次） |
| `run_now(task_id)` | 手动执行一次（`mode="manual"`）：不接力下一次、不消耗一次性 `at`、不改 `enabled`；返回 `trigger()` 的结构化结果 |
| `list_tasks()` | 全部任务及其状态 |
| `list_events()` | 59 个可订阅事件名（**不含** GPS 的 20 个） |
| `recent_events(limit=20)` | Python 侧最近事件环形缓冲（`_RECENT_MAX=50`，深拷贝；`limit` 取 1~200，非法回退默认 20） |
| `network_state()` | 当前网络快照 `{wifi, online, validated, initialized, ts}` |
| `system_events(limit=50, return_str=False)` | Kotlin 系统事件缓冲（最多 500 条），见下节 |
| `clear_system_events()` | 清空 Kotlin 系统事件缓冲，返回 `{ok: true}` |
| `test_event(event, payload=None)` | 本地模拟事件，走与系统事件相同的队列 |
| `consume_boot_pending()` | 幂等消费开机待发标记并派发 `boot_completed` |
| `is_initialized()` | Python 任务表是否已 `init` |
| `register_func(name, func)` | 注册内置函数供 `func_name` 引用 |
| `fire_event(event_name, payload=None, source="system")` | **内部入口**，Kotlin 回调专用，不对外 |
| `execute_task(task_id)` | **内部入口**，Worker 到点只做校验并入队后立即返回，不在 WorkManager 线程跑任务代码 |

同一 `task_id` 再次添加即为**覆盖**：`run_count` 等统计归零，`gen` 递增，旧的 WorkManager
排程会被取消，已入队的旧触发与执行结果按代际丢弃；订阅集合无条件重新同步
（即使覆盖成无 `events` 的任务，也会清掉 Kotlin 侧旧订阅）。

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
  "kind": "daily", "at": "08:30", "next_epoch": 1793075400,
  "events": ["wifi_connected"], "code": "...", "enabled": true,
  "run_count": 3, "last_run": "2026-10-27 08:30:00",
  "last_result": "[每日] 触发",
  "last_event": "daily", "last_payload": {...},
  "created": "2026-10-26 11:11:08", "_last_fire_epoch": 1793075961.7
}
```

- `kind`：`interval` / `at` / `daily` / `event`；混合时取主调度类型
  （优先级 `interval > at > daily > event`）。
- `events`：系统事件名列表，**每个任务都有**（无订阅为 `[]`），与排程互不影响。
- `interval`：定时秒数（仅 interval 类任务）。
- `at` / `next_epoch`：一次性或每日任务的目标时刻与下次触发 epoch 秒。
  `list_tasks()` 总会补齐 `at` / `next_epoch` / `events`（缺失为 `null` / `[]`），
  不删任何旧键。
- `last_payload` / `recent_events` 存的是 `_brief()` 摘要：字符串 >256 字符截断为
  `...<len=N>`，列表 >16 项、字典 >32 键截断，`bytes` 转 `<bytes len=N>`；
  任务 `code` 拿到的仍是原始 payload。
- 写盘分两档：结构变更（增删/覆盖/暂停/恢复/一次性 `at` 触发）立即 `_save()`；
  执行统计（`run_count` / `last_*`）走 `save_soft()`，最多每 3 秒合并一次。
- `_arm()` 只有 WorkManager 排程成功才更新 `next_epoch`；失败原因写入
  `last_schedule_error`，下一次成功排程时清除。
- `time_changed` / `timezone_changed` 到达时会 `reschedule_all()` 重排全部
  `daily` / `at` 任务，避免改时后错位。

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
- 热更新：把新版 `task_manager.py` 放到 `filesDir/site-packages` 后执行
  `import app; app.reload_module("task_manager")`，会按 sys.path 重新导入并 `init` 恢复任务调度。

## RPC 调用示例

```python
from client import client_mqtt

d = client_mqtt.rpc('import task_manager as tm; print(tm.list_tasks())',
                    request_topic="<设备主题>", timeout=30, k=<私钥>)
# 若设备时钟偏差超窗被 rejected=timestamp_window，用服务器时钟重发：
d = client_mqtt.rpc(CODE, request_topic="<设备主题>", timeout=30, k=<私钥>,
                    timestamp_ms=d["server_time"])
```

## 高级能力（v2）

v2 在保持 add_task/add_event_task/add_at_task/add_daily_task 旧写法完全兼容的前提
下，补齐了可靠性、事件、编排与可观测性能力。全部能力都只通过 RPC 配置，APK 内
不硬编码任何任务。

### 1. cron 触发

```python
r = tm.add_task("qa_cron", cron="*/5 * * * *", code='print("tick", event)')
r = tm.validate_cron("*/5 9-18 * * 1-5")   # 合法返回 {ok:True, next_epoch}
```

- 5 段标准格式：分 时 日 月 周；支持 `*` `*/n` `a-b` `a,b` 与 `?`。
- 每次触发后按当前时间推进到下一个匹配点再交给调度器接力，kind 为 `cron`。
- `interval` > `cron` > `at`/`repeat` 的优先级不变；旧 daily/at/interval 定义照常可用。

### 2. 可靠性

| 能力 | 配置 | 说明 |
| --- | --- | --- |
| 超时 | `timeout=秒`（任务级）或全局 `task_timeout` | 到点标记 timeout、置 cancel_event、记 timeout_count；Python 线程无法强杀，超时后返回的结果会被丢弃 |
| 重试 | `retry=次数`、`retry_delay=基础秒`、`retry_max_delay=上限秒` | 指数退避 `base*2^attempt`，耗尽进死信队列 |
| 死信 | - | `tm.dead_letters()` 读取；`tm.clear_dead_letters()` 清理 |
| 看门狗 | `watch_interval` | 心跳、到期重试回投、排程失败自动重排（30 秒间隔）、指标事件 |
| 并发控制 | `workers`、`lock_wait`、`queue_max` | 同一任务 per-task 锁串行；队列满丢新并计入 dropped |
| 调度失败 | - | 失败写 `last_schedule_error`，看门狗重排；连续失败进 alerts |

### 3. 触发语义：misfire / coalesce / catchup

| 配置 | 含义 |
| --- | --- |
| `misfire_grace=秒` | 过期超过宽限不再补跑（一次性 at 标记 MISSED） |
| `coalesce=True` | 同一任务排队期间多次触发只保留一次，payload 取最新；`coalesce_mode` 可选 |
| `catchup=True` | 进程重启恢复时，按错过的周期数补跑（上限由 missed 计数控制） |

### 4. 事件增强（任务侧）

```python
r = tm.add_task("qa_evt", events=["wifi_connected"],
                when={"wifi": True},                  # 字段过滤，支持 eq/ne/in/contains/regex/gt/gte/lt/lte/exists
                schema={"ssid": "str", "rssi": "int"}, # payload 类型校验，缺字段/类型错直接 skip
                dedupe=30, idem="id",                  # 幂等键 + 去重窗口（秒）
                persist_events=True,                   # 关键事件落盘，可用 replay_events 重放
                code='print(event, payload)')
```

- `when` 支持点路径（如 `network.wifi`）与 `{"op": ..., "value": ...}` 形式。
- 去重窗口内同一 idem 键（缺省为 payload 指纹）重复事件计 `deduped` 并跳过。
- 落盘事件存 `task_history.json` 的 `events`，受 `config: persist_events_max` 限制。

### 5. 任务编排

- `depends_on=["a", "b"]`：所依赖任务最近一次 `last_status` 必须为 ok，否则 skip 并记原因
  （`dependency_failed:<id>` / `dependency_missing:<id>`）。
- `then=["c"]`：本任务成功（ok）后链式触发，payload 带 `{from, event, status}`。
- `priority=数字`：事件队列按优先级出队（大者先）。
- `group="名字"`、`tags=[...]`：用于 `list_groups()` 与批量操作。

### 6. 观测与运维

| RPC | 作用 |
| --- | --- |
| `metrics()` / `metrics_json()` | 队列、任务、计数、死信、告警、持久事件等完整指标 |
| `export_metrics()` | 指标包装成可 MQTT/HTTP 下发的 JSON 文本 |
| `health()` | 健康检查：`problems` 列出 dead_letters/consecutive_failures/queue_full/schedule_errors 等 |
| `heartbeat()` | 最近心跳（epoch 秒） |
| `history(task_id, limit)` | 最近 N 次执行（耗时、错误、输出摘要） |
| `dead_letters(limit)` / `clear_dead_letters()` | 死信队列 |
| `alerts(limit)` / `clear_alerts()` | 告警（timeout / dead_letter / schedule_failed / schedule_recovered） |
| `persisted_events()` / `replay_events()` / `clear_persisted_events()` | 落盘事件读取与重放 |
| `recent_events(limit, since_seq)` | 环形缓冲按 seq 增量读取 |
| `list_tasks(page, page_size, group, enabled, include_code, detail)` | 分页与摘要；`page_size>0` 返回 `{page,page_size,total,count,tasks}`，默认仍返回旧 dict |
- 列表默认不回传全量 `code`，只给 `code_bytes`；需要源码时 `get_task(id, include_code=True)`。

### 7. 批量 / 模板 / 导入导出

```python
tm.batch_tasks("disable", ids=["a", "b"])   # action: enable/disable/remove/run
tm.save_template("netlog", task_id="a")      # 或 definition={...}
tm.add_task("a2", template="netlog")         # 模板 + 局部覆盖
exp = tm.export_tasks(ids=["a"], include_code=True)
tm.import_tasks(exp, overwrite=True)
```

导出/导入保留长代码原文（内部 `_json_copy`），不会像状态记账那样截断到 256 字符。

### 8. 调度后端与约束（需要 v2 Kotlin 桥）

```python
tm.add_task("qa_alarm", interval=10, backend="alarm", ...)          # AlarmManager 精确闹钟
tm.add_task("qa_net", interval=60, constraints={"network": "connected"}, ...)
```

- `backend="alarm"`：走 `TaskScheduler.scheduleExact`（setExactAndAllowWhileIdle；
  无精确闹钟权限时自动降级 setAndAllowWhileIdle），到点由 `TaskAlarmReceiver` 拉起 Python。
- `constraints`：`network`(connected/unmetered/not_roaming/metered/not_required)、
  `charging`、`battery_not_low`、`storage_not_low`、`device_idle`，映射到 WorkManager Constraints。
- Kotlin 侧不可用时自动回退普通 WorkManager 排程，只降级不影响主流程。

### 9. 持久化：三文件拆分与迁移

| 文件 | 内容 |
| --- | --- |
| `task_manager.json` | 任务静态定义 + 模板（结构变更立即落盘） |
| `task_runtime.json` | 运行统计、next_epoch、待重试项（3 秒合并写） |
| `task_history.json` | 每任务执行历史、死信、告警、持久事件（3 秒合并写） |

- 旧版单文件 `task_manager.json` 启动时自动迁移，缺失的 runtime/history 文件按需创建。
- 解析失败会备份为 `*.bad.<epoch>` 并从空表启动，绝不阻塞启动。
- 写盘统一 `_atomic_write`：临时文件 + fsync + os.replace，并对不可序列化对象做降级。

### 10. 可配置项（set_config）

```python
tm.set_config(queue_max=512, workers=6, min_interval=5,
              history_max=100, output_max=32768,
              task_timeout=120, retry_max_delay=600,
              watch_interval=5, metrics_events=60, persist_events_max=500)
```

`get_config()` 读取当前值；所有键都有安全上下限，非法值被拒绝并返回错误。

### 11. 热更新与停机开关

`app.reload_module("task_manager")` 导入新实例时，会通过 `builtins._tm_stop_event`
给旧实例的守护线程发停机信号：旧的事件分发线程、看门狗、写盘、单次执行收尾都会
检测到并立即退出，不会出现两个实例同时写状态文件或重复排程。
