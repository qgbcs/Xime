# task_manager 事件复合条件（AND）调研

> 状态：**设计调研，未实施**。日期 2026-11-19。
> 目标：判断 `task_manager` 是否支持“多个条件同时成立（AND）再触发”，给出不改代码与改代码两条路径的边界。
> 配套文档：`docs/task-manager.md`（使用说明与 API 参考）。

## 1. 结论

- **现状不支持 AND**。事件任务的 `events` 是 **OR 语义**（任一命中即执行），没有条件栈。
- **不改代码也能实现**，做法是：`events` 订阅全部事件源，在任务 `code` 内维护状态、自行求值、自行决定是否执行动作。代价见第 6 节。
- **只有长期复用才值得改代码**：在 `task_manager` 内新增 `kind="condition"`，把“触发（edge）”与“条件（state）”分离。条件不要编译进 Kotlin。
- 所有方案都绕不过一个硬前提：`ACCESS_BACKGROUND_LOCATION` 未授予时，锁屏后定位停更，位置类条件必然失效（见第 8 节实测）。

## 2. 现状：OR 语义与关键代码位置

| 能力 | 实现 | 位置 |
|---|---|---|
| 多事件订阅并集 | 遍历 `kind=="event"` 且 `enabled` 的任务取 `events` 并集 | `task_manager.py` `_sync_system_events()` |
| 事件匹配 | `event_name in _events_of(t)`，只要在集合内就选中 | `fire_event()` |
| 串行执行 | 单守护线程消费 `_EVENT_Q` | `_event_worker()` |
| 节流 | `debounce` 是**任务级、跨事件共享**的最小间隔 | `_event_worker()` |
| 动作执行 | `code` 优先，其次 `func` | `_execute()` / `_run_code()` |
| 状态持久化 | 整个 `_STATE` 原子写 `filesDir/task_manager.json` | `_save()`，`_execute()` 的 `finally` 每次都会调用 |
| 每日定时 | 仅支持 `HH:MM[:SS]`，无日期区间 | `add_daily_task()` / `_daily_delay()` |
| 显著运动 | 触发后 1s 自动 rearm | `LocationEventMonitor.significantMotionListener` / `rearmSignificantMotion()` |
| 定位状态快照 | `{running, first_fix, last{lat,lon,acc,ts,provider}, anchor{...}, distance_*, activity}` | `LocationEventMonitor.getState()` |

要点：

- `fire_event()` **不强制白名单**，`EVENTS` 只用于 RPC 提示；真正能收到什么由 Kotlin 事件源决定。
- `_run_code()` 的命名空间 `ns` **每次执行都是新建的**，代码里的普通全局变量不会跨次保留。
- `_LOCK` 是 `threading.RLock()`，任务代码里可以安全地 `with task_manager._LOCK:`。

## 3. 目标场景拆解

需求原话：每天【或指定日期 2026-11-18 ~ 2026-11-22】上午 8:00–12:00，GPS 位置限定，手机感知到移动才触发。

| 角色 | 内容 | 建议事件/来源 |
|---|---|---|
| 触发（edge） | “开始移动”这一刻 | `significant_motion`（`TYPE_SIGNIFICANT_MOTION`，唤醒型，0.025mA，one-shot，触发后自动 rearm） |
| 状态刷新 | 最近一次坐标 | `location_changed`（payload 含 `lat/lon/acc/dist_m/speed/bearing`，受 `changed_min_ms` 节流） |
| 条件 1 | 本地时间 ∈ [08:00, 12:00) | Python 谓词 |
| 条件 2 | 本地日期 ∈ 指定区间（或每天） | Python 谓词 |
| 条件 3 | 距围栏中心 ≤ radius_m | Python 谓词，用 payload 坐标自算 |
| 条件 4 | 定位新鲜度 `now - fix.ts ≤ max_age_s` | Python 谓词 |
| 去重 | 冷却窗口内不重复触发 | Python 状态 |
| 动作 | 业务代码 | `code` |

**语义纠偏**：这里的“同时成立”不是“所有事件在同一瞬间到达”，而是“**触发发生的那一刻，其余条件的状态为真**”。这也是 Home Assistant 的规则模型：触发器只对变化作出反应，条件在触发后只求值一次；要表达“两件事同时为真”，做法是**多触发 + 多条件**，而不是把多个事件 AND 起来等。

## 4. 分层：条件放哪一层

| 层 | 职责 | 变更频率 |
|---|---|---|
| Kotlin（`LocationEventMonitor` / `SystemEventMonitor`） | 采样、精度过滤、抖动去重、阈值、**边沿检测** | 设备级常量，几乎不变 |
| Python（`task_manager`） | 时间窗、日期区间、围栏、新鲜度、冷却、多条件求值 | 远程 RPC 随时下发 |
| 用户 `code` / `func` | 业务动作 | 远程下发 |

条件不进 Kotlin 的三个理由：

1. 任务由 RPC 远程下发、APK 不硬编码任务是既有设计；条件写进 Kotlin 等于把业务策略烧进 APK，改窗口就要重新构建安装。
2. 条件前移到 Kotlin 后仍然要回调 Python 执行 `code`，省不掉跨语言调用，只是多了一层重复实现。
3. `LocationEventMonitor` 的阈值/围栏是**全局单例**（`move_meters`、`fences`、`min_time_ms` 等只有一份），多任务共享；把条件放 Kotlin 会互相污染。

行业对照：

| 体系 | 做法 | 对本项目的映射 |
|---|---|---|
| Home Assistant（trigger/condition/action） | 条件在触发后求值一次；多触发覆盖“同时为真” | Python 侧 `trigger` + `conditions[]` |
| eKuiper（边缘规则引擎） | 规则由 REST 动态提交，SQL 描述源→过滤→动作，热更新 | 任务 JSON 远程下发 + 通用求值器 |
| Microsoft Fabric Activator | 有状态规则求值 + 状态转换去重 | `cooldown_s` / `_last_fire_epoch` |
| CEP（事件序列/时间窗口） | 支持 `A and B within T` 的模式匹配 | 本场景事件率低，**不需要**引入 |

结论：采用 ECA/TCA 分层（Kotlin 传感器前端 + Python 声明式规则引擎 + WorkManager 窗口武装），不引入 CEP。

## 5. 路径 A：零改动方案

### 5.1 条件任务

```python
CODE = r'''
import math, time, task_manager

t = task_manager._STATE["tasks"].get(task_id) or {}
now = time.time()
st = t.setdefault("state", {})

CENTER_LAT, CENTER_LON, RADIUS_M = 28.2641, 112.9477, 300.0

# 只有“感知到移动”才是触发；定位事件只用来刷新状态
if event == "location_changed":
    if payload:
        with task_manager._LOCK:
            st["fix"] = {"lat": payload.get("lat"), "lon": payload.get("lon"),
                         "ts": payload.get("ts", now * 1000) / 1000.0,
                         "acc": payload.get("acc")}
    return

if event != "significant_motion":
    return

# 条件 1：本地时间窗
lt = time.localtime(now)
if not (8 <= lt.tm_hour < 12):
    return

# 条件 2：日期区间（要“每天”就删掉这三行）
d = time.strftime("%Y-%m-%d", lt)
if not ("2026-11-18" <= d <= "2026-11-22"):
    return

# 条件 3：定位新鲜度
fix = st.get("fix")
if not fix or now - fix["ts"] > 600:
    return

# 条件 4：围栏
def _dist(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

if _dist(fix["lat"], fix["lon"], CENTER_LAT, CENTER_LON) > RADIUS_M:
    return

# 条件 5：冷却
if now - st.get("last_fire", 0) < 1800:
    return
with task_manager._LOCK:
    st["last_fire"] = now

# 动作
print("[home_morning_move] fired at", time.strftime("%F %T"))
'''

r = task_manager.add_event_task(
    "home_morning_move",
    events=["significant_motion", "location_changed"],
    debounce=0,          # 必须 0，见 6.1
    code=CODE,
)
```

说明：

- `_execute()` 的 `finally` 一定会 `_save()`，所以代码里改过的 `t["state"]` 会自动落盘，**不需要额外调用 `_save()`**；显式调用只是多一次写盘。
- 状态放在任务字典里会出现在 `list_tasks()` 输出中；不想污染可以改写到 `filesDir` 下的独立 JSON（目录用 `os.path.dirname(task_manager._STATE_PATH)` 取）。

### 5.2 时间窗武装（省电）

```python
task_manager.add_daily_task("win_on",  at="08:00",
    code="task_manager.resume_task('home_morning_move')")
task_manager.add_daily_task("win_off", at="12:00",
    code="task_manager.pause_task('home_morning_move')")
```

- `pause_task()` 把 `enabled` 置 False，`fire_event()` 不再匹配，同时 `_sync_system_events()` 重算订阅集合，`LocationEventMonitor` 在集合里没有 GPS 事件时**注销监听并退出线程**。
- 边界由 WorkManager（`daily` → `OneTimeWorkRequest`）保证，不依赖 `time_tick`。
- 注意：`pause` 只影响本任务的订阅；如果**其他 enabled 任务**仍订阅 GPS 事件，传感器不会停（见 6.6）。

### 5.3 调试手段

```python
task_manager.test_event("significant_motion", {"sensor": "significant_motion"})  # 走同一队列
task_manager.run_now("home_morning_move")   # 事件任务以 events[0] + payload=None 入队
task_manager.recent_events(20)              # Python 侧最近 50 条
task_manager.system_events(50, True)         # Kotlin 侧最近 500 条，含 registered 集合
```

## 6. 路径 A 的坑（全部有代码依据）

### 6.1 `debounce` 是任务级、跨事件共享

`_event_worker()` 用同一个 `debounce` 对所有事件节流。若设成 1800，一次 `location_changed` 就会把紧随其后的 `significant_motion` 挡掉。**必须 `debounce=0`，冷却自己记。**

### 6.2 `run_count` / `last_run` 不再代表“真正触发”

`code` 在每次命中事件时都会执行，包括“只刷新状态、不动作”的分支；`_execute()` 每次都递增 `run_count`、覆盖 `last_run` / `last_result`。诊断应看自定义 `state`，不要看 `run_count`。

### 6.3 代码命名空间不持久

`_run_code()` 每次新建 `ns`。需要持久的状态只能写进 `_STATE["tasks"][task_id]` 或独立文件。

### 6.4 没有新鲜度保护

框架不会判断坐标是否过期。定位停更时可能拿到几小时前的坐标，必须自己判 `now - fix.ts`。

### 6.5 不要用 `location_moved` 做条件

`move_meters` / `min_time_ms` / `min_distance_m` 是全局配置，多任务共享（真机当前 `move_meters=200`）。用 `location_moved` 会把全局阈值混进业务条件。用 `location_changed` 收坐标自算。

### 6.6 省电取决于整个订阅集合

`_sync_system_events()` 取的是所有 enabled 事件任务的并集。只要还有别的任务订阅 GPS 事件，`pause_task('home_morning_move')` 之后传感器仍在线。

### 6.7 日志噪音

`location_changed` 会写 Python 的 `_RECENT_EVENTS`（上限 50）并 `print`，几分钟就能把环形缓冲刷满，挤掉其他诊断信息。

### 6.8 `time_tick` 不是调度器

`ACTION_TIME_TICK` 在 Doze / 进程冻结下会延迟、合并、跳变。时间窗边界只能用 WorkManager 或 `interval` 任务。

### 6.9 进程被杀与 Doze

任务表与订阅在 `init` 恢复；但进程死亡期间发生的 `significant_motion` 不会补发。OPPO 等厂商的省电策略可能直接杀进程。

### 6.10 后台定位权限

见第 8 节：`ACCESS_BACKGROUND_LOCATION = -1` 时锁屏后定位停更，第 6.4 条的“新鲜度”会把绝大多数触发判掉。

## 7. 路径 B：原生条件（未实施，仅记录改造面）

若未来多个场景复用，则在 `task_manager.py` 内新增：

```json
{
  "kind": "condition",
  "trigger": ["significant_motion"],
  "conditions": [
    {"type": "time_window", "start": "08:00", "end": "12:00"},
    {"type": "date_range", "start": "2026-11-18", "end": "2026-11-22"},
    {"type": "geo_fence", "lat": 28.2641, "lon": 112.9477, "radius_m": 300},
    {"type": "freshness", "source": "location", "max_age_s": 600}
  ],
  "cooldown_s": 1800,
  "code": "..."
}
```

改造面：

1. `add_condition_task()` + `_conditions_ok(t, event, payload)` 通用求值器；
2. `_event_worker()` 在 `_execute()` 前加条件门槛，不通过只更新状态；
3. `_sync_system_events()` 收集 `trigger ∪ 条件依赖事件源`，现有订阅链不用改；
4. `eval_task(task_id)` 干跑，返回每个谓词的 pass/fail 与原因；
5. 窗口武装可复用第 5.2 节的 `daily` 任务，不必改 Kotlin。

开销评估：每个订阅事件仍会跨一次 Chaquopy 边界，但本项目事件率是“每分钟级 / 每次显著移动一次”，跨语言开销为毫秒级；真正的功耗在 GPS/传感器硬件与进程唤醒，不在条件求值。

## 8. 真机基线（2026-11-18 实测）

| 项 | 值 |
|---|---|
| 设备主题 | `OPPO_PJT110_2b855b` |
| 任务表 | `gps_moved_200`（`location_moved`，`run_count=0`）；`ime_archive`（`ime_keys` / `clipboard_text` / `speech_recorded` 三事件 OR，`run_count=3304`） |
| `LocationEventMonitor` | `running=true`，`subscribed=["location_moved"]`，`first_fix=true` |
| 最近定位 | `lat=28.26411556`，`lon=112.94766273`，`acc=84.87`，`provider=gps` |
| 锚点 / 里程 | `anchor.since_ts=1794939662265`，`distance_total_m=281.65`，`distance_pending_m=281.65` |
| 全局配置 | `move_meters=200`，`min_time_ms=30000`，`min_distance_m=50`，`fences=[]` |
| 权限 | `ACCESS_FINE_LOCATION=0`，`ACCESS_COARSE_LOCATION=0`，`ACCESS_BACKGROUND_LOCATION=-1` |
| 传感器 | 加速度 `icm456xx Accelerometer Non-wakeup`，陀螺仪 `icm456xx Gyroscope Non-wakeup`，显著运动 `sns_smd Wakeup`（0.025mA） |

解读：

- 只有 `significant_motion` 是 **wakeup** 型，锁屏后仍能唤醒；加速度计/陀螺仪是 **non-wakeup**，锁屏后不可靠。所以“感知移动”只能用 `significant_motion`，不能用原始陀螺仪采样。
- `ACCESS_BACKGROUND_LOCATION=-1` 意味着锁屏后无法持续定位；Android 8.0+ 后台定位更新被限制为每小时数次。

## 9. 待确认

1. 后台定位：引导用户授予“始终允许”，还是在 08:00–12:00 窗口内启动 `FOREGROUND_SERVICE_TYPE_LOCATION` 前台服务？（现有 `BackgroundCaptureService` 只声明 camera / microphone / mediaPlayback）
2. `significant_motion` 触发但条件不通过时，是否继续保持武装（继续监听）？
3. 迟到事件宽限：12:00:05 到达的定位事件是否算窗口内？
4. 日期区间是“固定日期范围”还是“每天”，还是两者都要支持？

## 10. 参考

- Android `Sensor.TYPE_SIGNIFICANT_MOTION`：<https://developer.android.com/reference/android/hardware/Sensor#TYPE_SIGNIFICANT_MOTION>
- Android 8.0 后台定位限制：<https://developer.android.com/about/versions/oreo/background-location-limits>
- Home Assistant 自动化（trigger / condition / action）：<https://www.home-assistant.io/docs/automation/>
- eKuiper 边缘规则引擎：<https://ekuiper.org/docs/en/latest/>
- Microsoft Fabric Activator（有状态规则求值）：learn.microsoft.com / fabric / data-activator
