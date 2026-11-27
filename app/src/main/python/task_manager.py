"""高可靠后台任务框架（Python 侧）—— 定时任务 + 事件驱动任务

架构：WorkManager 负责系统级调度（进程被杀、设备重启后仍能触发），
Kotlin NetworkEventMonitor 负责网络边沿事件（WiFi 连/断、网络通/断），
Python 负责任务注册表、状态持久化、执行统计与接力下一次调度。

任务一律由 RPC 远程下发，APK 内不硬编码任何任务（init 只从 JSON 恢复）。
触发方式统一为“事件”，可任意组合（定时/时刻 + 系统事件可同时绑定）：
  1) 定时（interval）：OneTimeWorkRequest 自接力突破 15 分钟下限，到点以
     伪事件 interval 分发；
  2) 指定时刻（at）：绝对 "YYYY-MM-DD HH:MM[:SS]" 一次性，或 "HH:MM[:SS]"
     表示下一次该时刻；到点以伪事件 at 分发，触发后自动停用；
  3) 每日定时（daily）：at="HH:MM"，按本地时区每天到点触发，伪事件 daily；
  4) 系统事件（events）：订阅 Kotlin 上报的事件（网络边沿、开机完成），见
     EVENTS 说明；一个任务可绑定多个事件（events 列表），任一命中即执行。
定时/时刻/每日到点后与系统事件走同一个分发函数 _execute，任务代码里的
event 变量就是 "interval" / "at" / "daily" 或真实系统事件名。

任务动作支持两种载体（二选一，code 优先）：
  - code：随任务下发的任意 Python 源码（运行在独立工作线程，print 既进 App
    日志窗口又会被捕获到 last_result 便于 RPC 回读校验），可用变量：
      event   当前事件名（事件任务）
      payload 事件负载 dict（事件任务）
      task_id 任务 id
      jclass Chaquopy jclass（Android 环境）
  - func：内置函数名（JOB_FUNCS，仅保留少量示例函数，非任务）。

事件清单（由 Kotlin 侧产生）：
  boot_completed     设备开机完成（BootEventReceiver 写标记，首次 init 补发）
  wifi_connected     默认网络切到 WiFi 且边沿进入
  wifi_disconnected  默认网络离开 WiFi
  network_online     默认网络具备 NET_CAPABILITY_INTERNET（可上网）
  network_offline    默认网络丢失/失效
  network_state      进程启动首帧的网络快照（非边沿，payload 带 initial=true）
  screen_on/screen_off 等  系统广播（Kotlin SystemEventMonitor，默认只记账不进 Python）
  screen_rotation_changed   屏幕旋转（无对应系统广播，由 DisplayManager 回调产生，
                            payload 含 rotation 0~3 与 degrees）
  call_state_changed        通话状态（Kotlin TelephonyCallback，不依赖广播），payload 含
                            state=idle/ringing/offhook、state_code 与
                            direction=incoming/outgoing（先去电则直接 offhook）

零成本系统状态（Kotlin 广播/回调，不需要新权限）：
  doze_mode_changed        Doze 进入/退出，payload idle=true/false
  power_save_mode_changed  省电模式，payload enabled
  dreaming_started/dreaming_stopped  屏保/充电屏保开始与结束
  storage_low/storage_ok   设备存储将满与恢复
  usb_state_changed        USB 连接/断开，payload connected/configured
  nfc_state_changed        NFC 开关，payload enabled
  location_mode_changed    定位开关，payload enabled
  hotspot_state_changed    热点 AP 状态，payload state（WIFI_STATE_*）
  sim_state_changed        SIM 状态变化，payload state（ABSENT/PIN_REQUIRED/READY 等）
  next_alarm_changed       下一个闹钟变化，payload next_alarm_ms/package
  time_tick                每分钟一次（ACTION_TIME_TICK）
  configuration_changed    字体缩放/密度等配置变化，payload font_scale/density_dpi
  wallpaper_changed        壁纸变化
  user_switched            多用户切换，payload user_id
  brightness_changed       屏幕亮度，payload value
  volume_changed           媒体音量，payload stream/value/max
  light_changed/proximity_changed/pressure_changed  光感/距离/气压，payload value/sensor（1 秒去抖）
  keyboard_shown/keyboard_hidden  输入法可见性，payload source
  clipboard_changed        剪切板变化（仅输入法可见时；payload 只有 has_text/length，不落内容）

RPC 示例（add_task 是唯一入口；add_daily_task/add_at_task/add_event_task 只是
兼容包装，内部均转调 add_task）：
  r=task_manager.add_task("dailytest", at="08:30", repeat=True,
  ...   code='print("[每日] 触发", task_id)')
  r=task_manager.add_at_task("once", "2026-12-01 08:30:00",
  ...   code='print("[一次性] 触发", event)')
  r=task_manager.add_task("tick", interval=60, events=["wifi_connected"],
  ...   code='print("tick", event, payload)')   # 定时 + 系统事件同一个任务
  r=task_manager.add_event_task("wifilog", ["wifi_connected", "boot_completed"],
  ...   code='print("[事件] WiFi 已连接", payload)')
  r=task_manager.test_event("wifi_connected", {"wifi": True})   # 本地模拟
  r=task_manager.list_tasks()
  r=task_manager.recent_events()
  r=task_manager.system_events(100, True)   # 多行文本，每行一个事件（本地时间格式化）
  r=task_manager.network_state()            # 当前网络快照 {wifi, online, validated, initialized, ts}
"""
import datetime
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

try:
    from java import jclass
    _jclass = jclass
except Exception:  # pragma: no cover - 非 Android 环境（PC 侧单测）
    _jclass = None

STATE_VERSION = 1
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

_STATE_PATH = None
_LOCK = threading.RLock()
_STATE = {"version": STATE_VERSION, "tasks": {}}

# 统一触发队列：WorkManager 回调、系统事件、RPC run_now 都只入队立即返回，
# 由唯一守护线程串行执行；队列有界，满了丢新并计数，绝不阻塞调用方。
_EVENT_Q = queue.Queue(maxsize=256)
_RUN_DROPPED = 0
_RECENT_EVENTS = []          # 环形缓冲，供 RPC 回读
_RECENT_MAX = 50

# 统计类字段（run_count/last_*）合并写盘；结构变更（add/remove/at 停用）
# 必须通过 _save() 立即落盘。
_DIRTY = False
_SOFT_SAVE_GAP = 3.0
_last_soft_save = 0.0
_BOOT_DONE = False

# init 载入任务表之前到达的事件先缓存，载入完成后按原顺序补发。
# 典型场景：NetworkEventMonitor 首帧的 network_state 快照（Kotlin 先于 Python
# 就绪产生），若直接匹配会撞上空任务表而被丢掉。
_LOADED = False
_PRE_INIT_EVENTS = []
_PRE_INIT_MAX = 64

# 事件名白名单（仅用于 RPC 提示；fire_event 不强制，以后加事件不用改 Python）
EVENTS = (
    "boot_completed",
    "wifi_connected",
    "wifi_disconnected",
    "network_online",
    "network_offline",
    "network_state",
    # 以下由 Kotlin SystemEventMonitor 动态注册的系统广播产生
    "screen_on",
    "screen_off",
    "user_present",
    "battery_low",
    "battery_okay",
    "battery_changed",
    "power_connected",
    "power_disconnected",
    "airplane_mode_changed",
    "headset_plug",
    "package_added",
    "package_removed",
    "package_replaced",
    "wifi_state_changed",
    "connectivity_changed",
    "ringer_mode_changed",
    "bluetooth_state_changed",
    "media_mounted",
    "media_unmounted",
    "media_removed",
    "media_bad_removal",
    "media_eject",
    "shutdown",
    "locale_changed",
    "time_changed",
    "timezone_changed",
    "input_method_changed",
    # DisplayManager 回调（非广播）：屏幕旋转
    "screen_rotation_changed",
    # TelephonyCallback 回调（非广播）：通话状态
    "call_state_changed",
    # 零成本系统状态（广播/回调，不需要新权限）
    "doze_mode_changed",
    "power_save_mode_changed",
    "dreaming_started",
    "dreaming_stopped",
    "storage_low",
    "storage_ok",
    "usb_state_changed",
    "nfc_state_changed",
    "location_mode_changed",
    "hotspot_state_changed",
    "sim_state_changed",
    "next_alarm_changed",
    "time_tick",
    "configuration_changed",
    "wallpaper_changed",
    "user_switched",
    "brightness_changed",
    "volume_changed",
    "light_changed",
    "proximity_changed",
    "pressure_changed",
    "keyboard_shown",
    "keyboard_hidden",
    "clipboard_changed",
    # 输入法事件（ImeEventBridge）：按键合批 / 剪贴板文本 / 语音录音文件
    # ime_keys        {"items": [[ts, key, shifted], ...], "dropped": n}
    # clipboard_text  {"ts": ms, "text": str}
    # speech_recorded {"ts": ms, "path": str, "text": str, "started_ms": ms, "duration_ms": ms}
    "ime_keys",
    "clipboard_text",
    "speech_recorded",
)

# 伪事件名：定时/时刻/每日到点后也以“事件”形式进入统一分发路径（_execute），
# 任务代码里的 event 变量即这三个名字之一。它们永远不会被同步给 Kotlin 订阅
# 集合（否则 Kotlin 会等一个永远不会有广播的“事件”）。
PSEUDO_EVENTS = ("interval", "at", "daily")

# ---- 任务函数注册表：name -> callable() -> object（少量内置示例函数） ----
JOB_FUNCS = {}


def register_func(name, func):
    JOB_FUNCS[name] = func
    return func


def _builtin_mqtt_stats():
    """示例任务：输出 MQTT 链路统计报告。"""
    import app
    return "ok" if app.mqtt_stats_report() else "mqtt_off"


def _builtin_heartbeat():
    return "beat %s" % time.strftime("%H:%M:%S")


register_func("mqtt_stats", _builtin_mqtt_stats)
register_func("heartbeat", _builtin_heartbeat)


# ============================ 初始化 / 持久化 ============================

def init(files_dir):
    """app.py start() 内调用：加载持久化状态，恢复定时任务调度。"""
    global _STATE_PATH, _LOADED
    _STATE_PATH = os.path.join(files_dir, "task_manager.json")
    with _LOCK:
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
                _STATE["tasks"] = data["tasks"]
                try:
                    _STATE["version"] = int(data.get("version") or STATE_VERSION)
                except (TypeError, ValueError):
                    _STATE["version"] = STATE_VERSION
        except (OSError, ValueError) as e:
            # 损坏文件先备份再继续：绝不静默用空表覆盖（否则无法人工恢复）
            if os.path.exists(_STATE_PATH):
                bad = "%s.bad.%d" % (_STATE_PATH, int(time.time()))
                try:
                    os.replace(_STATE_PATH, bad)
                    print(f"[task_manager] 状态文件损坏，已备份到 {bad}: {e}")
                except OSError:
                    print(f"[task_manager] 状态文件损坏且备份失败: {e}")
        interval_n = at_n = daily_n = event_n = 0
        for task_id, t in list(_STATE["tasks"].items()):
            # 单个旧/脏任务恢复失败只降级，不能拖垮 init 与主流程
            try:
                _normalize_task(t)
                if not t.get("enabled", True):
                    continue
                kind = _kind(t)
                if kind == "interval":
                    _arm(task_id, t)
                    interval_n += 1
                elif kind == "at":
                    # 重算 next_epoch；一次性 at 已过期（_delay_of 返回 1）
                    # 则 1 秒后立即执行一次，由 execute_task 置 enabled=False。
                    _arm(task_id, t)
                    at_n += 1
                elif kind == "daily":
                    _arm(task_id, t)
                    daily_n += 1
                else:
                    event_n += 1
            except Exception:
                print(f"[task_manager] 恢复任务 {task_id} 失败:\n"
                      + traceback.format_exc(limit=2))
        _save()
        _LOADED = True
    _sync_system_events()
    _ensure_event_worker()
    _replay_pre_init_events()
    consume_boot_pending()
    print(f"[task_manager] init done, {len(_STATE['tasks'])} tasks, "
          f"{interval_n} interval + {at_n} at + {daily_n} daily rescheduled, "
          f"{event_n} event tasks armed")
    return True


def _replay_pre_init_events():
    """把 init 之前缓存的事件补发给刚载入的任务表（只补发一次）。"""
    with _LOCK:
        items = list(_PRE_INIT_EVENTS)
        del _PRE_INIT_EVENTS[:]
    for name, payload, source in items:
        try:
            # 补发不再记账：这些事件在 init 前已经进过 _RECENT_EVENTS
            fire_event(name, payload, source=source, record=False)
        except Exception:
            print("[task_manager] 补发预初始化事件失败:\n"
                  + traceback.format_exc(limit=2))


def _json_safe(value, depth=0):
    """把任意对象转成可 JSON 序列化的基本类型（带长度上限，只用于记账）。

    任务代码拿到的 payload 仍是原对象，不受影响；_RECENT_EVENTS / last_payload
    / 落盘只用这里净化后的副本，避免巨量文本或自定义对象撑大内存与磁盘。
    """
    if depth > 4:
        return repr(value)[:120]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= 256:
            return value
        return value[:256] + "...<len=%d>" % len(value)
    if isinstance(value, (bytes, bytearray)):
        return "<bytes len=%d>" % len(value)
    if isinstance(value, (list, tuple)):
        items = [_json_safe(v, depth + 1) for v in list(value)[:16]]
        if len(value) > 16:
            items.append("<+%d more>" % (len(value) - 16))
        return items
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 32:
                out["<+%d more>"] = len(value) - 32
                break
            out[str(k)[:64]] = _json_safe(v, depth + 1)
        return out
    try:
        text = repr(value)
    except Exception:
        text = "<unrepr>"
    return text[:120]


def _brief(payload):
    """事件 payload 的记账摘要（全量只给任务代码用）。"""
    return _json_safe(payload)


def _sanitized_state():
    """_STATE 的净化副本：即使内存里混进不可序列化对象也能落盘。"""
    with _LOCK:
        tasks = {}
        for task_id, t in _STATE.get("tasks", {}).items():
            if isinstance(t, dict):
                tasks[task_id] = {k: _json_safe(v) for k, v in t.items()}
        return {"version": _STATE.get("version", STATE_VERSION), "tasks": tasks}


def _save_file():
    """立即整文件落盘（原子替换 + fsync）；失败只降级，不向外抛。"""
    global _DIRTY
    if not _STATE_PATH:
        return False
    tmp = _STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STATE_PATH)
        _DIRTY = False
        return True
    except (TypeError, ValueError):
        print("[task_manager] save 序列化失败，净化后重试:\n"
              + traceback.format_exc(limit=3))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_sanitized_state(), f, ensure_ascii=False,
                          separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _STATE_PATH)
            _DIRTY = False
            return True
        except Exception as e:
            _DIRTY = True
            print(f"[task_manager] save 降级重试失败: {e}")
            return False
    except OSError as e:
        _DIRTY = True
        print(f"[task_manager] save failed: {e}")
        return False


def _save():
    """结构变更立即落盘（add/remove/pause/at 停用等）。"""
    return _save_file()


def save_soft():
    """统计类更新：标脏并最多每 _SOFT_SAVE_GAP 秒合并落盘一次。"""
    global _DIRTY, _last_soft_save
    _DIRTY = True
    now = time.time()
    if now - _last_soft_save < _SOFT_SAVE_GAP:
        return False
    _last_soft_save = now
    return _save_file()


def _kind(t):
    kind = t.get("kind")
    if kind in PSEUDO_EVENTS or kind == "event":
        return kind
    # 旧/脏数据缺 kind：有 at 视为每日，否则按历史默认当定时任务
    return "daily" if t.get("at") else "interval"


def _is_periodic(kind):
    """interval / at / daily 都由 WorkManager 排程，event 纯被动。"""
    return kind in PSEUDO_EVENTS


def _pseudo_event(kind):
    """kind -> 伪事件名；纯事件任务返回 None。"""
    return kind if kind in PSEUDO_EVENTS else None


def _normalize_task(t):
    """就地补齐旧任务字段（events 列表、kind），脏数据只降级不抛错。"""
    if not isinstance(t, dict):
        return t
    if not isinstance(t.get("events"), list):
        t["events"] = _system_events_of(t)
    if _kind(t) != t.get("kind"):
        t["kind"] = _kind(t)
    return t


def _norm_events(event=None, events=None):
    """归一化 RPC 显式传入的 event/events，返回去重事件名列表；非法返回 None。

    显式输入要求严格：元素不是字符串、或去重后为空，都由调用方报错；
    只有从 task_manager.json 读出来的旧/脏数据才走 _events_of 的宽松解析。
    """
    raw = []
    for item in (events, event):
        if item is None:
            continue
        if isinstance(item, str):
            raw.append(item)
        elif isinstance(item, (list, tuple)):
            raw.extend(item)
        else:
            return None
    names = []
    for name in raw:
        if not isinstance(name, str):
            return None
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def _events_of(t):
    """任务绑定的事件集合；兼容旧字段 event，脏数据只降级为空列表。"""
    if not isinstance(t, dict):
        return []
    names = []
    ev = t.get("events")
    if isinstance(ev, (list, tuple)):
        names = [e.strip() for e in ev if isinstance(e, str) and e.strip()]
    one = t.get("event")
    if isinstance(one, str) and one.strip() and one.strip() not in names:
        names.append(one.strip())
    return names


def _system_events_of(t):
    """任务订阅的系统事件（排除 interval/at/daily 伪事件名）。"""
    return [e for e in _events_of(t) if e not in PSEUDO_EVENTS]


# ============================ WorkManager 桥 ============================

def _schedule(task_id, delay_seconds):
    if _jclass is None:
        print("[task_manager] TaskScheduler 不可用（非 Android 环境）")
        return False
    try:
        _jclass("com.kingzcheung.xime.task.TaskScheduler") \
            .schedule(task_id, int(max(0, delay_seconds)))
        return True
    except Exception:
        print("[task_manager] TaskScheduler 调度失败:\n" + traceback.format_exc(limit=2))
        return False


def _cancel(task_id):
    """取消该任务的 WorkManager 排程；失败只降级。"""
    if _jclass is None:
        return False
    try:
        _jclass("com.kingzcheung.xime.task.TaskScheduler").cancel(task_id)
        return True
    except Exception:
        print(f"[task_manager] 取消排程 {task_id} 失败:\n"
              + traceback.format_exc(limit=2))
        return False


def reschedule_all():
    """改时/改时区后重算所有 daily / 一次性 at 的排程。"""
    with _LOCK:
        for task_id, t in _STATE["tasks"].items():
            if not isinstance(t, dict) or not t.get("enabled", True):
                continue
            if _kind(t) in ("daily", "at"):
                _arm(task_id, t)
        _save()
    print("[task_manager] 时间/时区变化，已重排 daily/at")


# ============================ 系统广播（Kotlin SystemEventMonitor） ============================

_SYSTEM_MONITOR = "com.kingzcheung.xime.task.SystemEventMonitor"
_NETWORK_MONITOR = "com.kingzcheung.xime.task.NetworkEventMonitor"


def _subscribed_events():
    """所有启用任务订阅的系统事件并集（已排除伪事件名），供 Kotlin 订阅用。"""
    with _LOCK:
        names = set()
        for t in _STATE["tasks"].values():
            if not isinstance(t, dict) or not t.get("enabled", True):
                continue
            names.update(_system_events_of(t))
        return sorted(names)


def _sync_system_events():
    """把所有启用任务订阅的系统事件集合同步给 Kotlin SystemEventMonitor。

    定时（interval）、一次性 at、每日 daily 等任务也可以同时订阅系统事件，
    因此这里不再按 kind 过滤，而是汇总全部任务的 events（排除伪事件名）。
    Kotlin 只对集合内的事件调用 fire_event；未订阅的系统广播仅写入 Kotlin
    环形缓冲，不产生任何 Chaquopy/Python 调用，避免无谓耗电。
    """
    if _jclass is None:
        return
    try:
        names = _subscribed_events()
        # 改时/改时区必须总能到达 Python，否则 daily/at 排程错位无人修正
        for extra in ("time_changed", "timezone_changed"):
            if extra not in names:
                names.append(extra)
        _jclass(_SYSTEM_MONITOR).syncRegisteredEvents(json.dumps(names))
        print(f"[task_manager] 系统事件订阅同步: {names}")
    except Exception:
        print("[task_manager] 系统事件订阅同步失败:\n" + traceback.format_exc(limit=2))


# ============================ RPC：任务生命周期 ============================

# add_task 允许的 opts 键；出现其他键直接报错（不静默忽略）
ALLOWED = {"interval", "at", "repeat", "events", "debounce", "misfire_grace"}


def add_task(task_id, func_name=None, code=None, enabled=True, **opts):
    """新增/覆盖一个任务（**唯一的建任务入口**）。

    设计参考 APScheduler（``add_job(..., trigger=date|interval|cron,
    **trigger_args)``）与 matrix-planner（``every`` / ``cron`` / ``once``）：
    用一组关键字参数描述触发源，由本函数归一成任务字典，而不是为每种触发源
    各开一个入口。add_event_task / add_at_task / add_daily_task 只是兼容包装。

    opts 只允许下列 6 个键（未知键直接报错，不静默忽略）：
      - interval：秒，>=1 → kind="interval"；若同时给 at，interval 优先；
        到点以伪事件 interval 分发，执行完自动接力下一次。
      - at：``YYYY-MM-DD HH:MM[:SS]`` / ``HH:MM[:SS]``（Python 侧也接受
        ``datetime.datetime``）→ 一次性 kind="at"，触发后自动 enabled=False；
        配合 repeat=True（仅 ``HH:MM[:SS]``）→ 每日 kind="daily"。
      - events：系统事件名（字符串或列表），可与任意触发源叠加。
      - repeat：仅对 at=HH:MM[:SS] 生效，表示每日重复。
      - debounce：秒；同一任务两次触发的最小间隔（定时/事件/手动全部生效），
        0 表示不节流。
      - misfire_grace：秒，仅一次性 at 生效；错过超过该时长不再补跑，默认 60。
    interval / at / events 三者都没有 → 报错。

    定时/时刻/每日到点后都以伪事件 interval / at / daily 走统一分发 _execute。

    :param func_name: 内置函数名（JOB_FUNCS）；与 code 二选一，code 优先
    :param code: 触发时执行的 Python 源码；可用 event/payload/task_id/jclass
    """
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        return {"ok": False,
                "error": "task_id 必须是 1~64 位字母/数字/_/-/. 组成的字符串",
                "task_id": task_id}
    unknown = sorted(k for k in opts if k not in ALLOWED)
    if unknown:
        return {"ok": False,
                "error": "不支持的参数: %s" % unknown,
                "allowed": sorted(ALLOWED)}
    interval = opts.get("interval")
    at = opts.get("at")
    repeat = bool(opts.get("repeat", False))
    events = opts.get("events")
    debounce = opts.get("debounce", 0.0)
    names = _norm_events(None, events)
    if names is None:
        return {"ok": False, "error": "events 必须是字符串或字符串列表",
                "events": events}
    bad_events = [n for n in names if n in PSEUDO_EVENTS]
    if bad_events:
        return {"ok": False,
                "error": "events 不能使用伪事件名 %s（定时请用 interval/at 参数）"
                         % bad_events,
                "events": names}
    seconds = None
    if interval is not None:
        try:
            seconds = int(interval)
        except (TypeError, ValueError):
            return {"ok": False, "error": "interval 必须是正整数秒",
                    "interval": interval}
        if seconds < 1:
            return {"ok": False, "error": "interval 必须 >= 1 秒",
                    "interval": interval}
    at_epoch, at_norm, at_abs = None, None, False
    has_at = at is not None and (isinstance(at, datetime.datetime)
                                 or str(at).strip())
    if has_at:
        try:
            at_epoch, at_norm, at_abs = _parse_at(at)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e), "at": at}
        if repeat and at_abs:
            return {"ok": False,
                    "error": "repeat=True 只支持 HH:MM[:SS]，绝对日期时间请用 repeat=False",
                    "at": at}
    if seconds is not None:
        kind = "interval"
    elif at_norm is not None:
        kind = "daily" if repeat else "at"
    elif names:
        kind = "event"
    else:
        return {"ok": False,
                "error": "需要 interval / at / events 至少一个",
                "interval": interval, "at": at, "events": events}
    if func_name is not None and not isinstance(func_name, str):
        return {"ok": False, "error": "func_name 必须是字符串",
                "func_name": func_name}
    if code is not None and not isinstance(code, str):
        return {"ok": False, "error": "code 必须是字符串",
                "code_type": type(code).__name__}
    if code and len(code) > 65536:
        return {"ok": False, "error": "code 超过 64KB 上限", "bytes": len(code)}
    if not code and func_name not in JOB_FUNCS:
        return {"ok": False,
                "error": "必须提供 code，或使用已注册 func",
                "funcs": sorted(JOB_FUNCS)}
    try:
        gap = float(debounce or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "debounce 必须是数字（秒）",
                "debounce": debounce}
    if gap < 0:
        return {"ok": False, "error": "debounce 不能为负", "debounce": debounce}
    try:
        grace = float(opts.get("misfire_grace", 60) or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "misfire_grace 必须是数字（秒）",
                "misfire_grace": opts.get("misfire_grace")}
    if grace < 0:
        return {"ok": False, "error": "misfire_grace 不能为负",
                "misfire_grace": grace}
    with _LOCK:
        old = _STATE["tasks"].get(task_id)
        # 任务代际：覆盖后队列里旧触发的快照会自动失效
        gen = int((old or {}).get("gen", 0) or 0) + 1
        t = {
            "gen": gen,
            "kind": kind,
            "events": names,
            "func": func_name,
            "code": code,
            "debounce": gap,
            "misfire_grace": grace,
            "enabled": bool(enabled),
            "run_count": 0,
            "last_run": None,
            "last_result": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if names:
            t["event"] = names[0]
            t["last_event"] = None
            t["last_payload"] = None
        if seconds is not None:
            t["interval"] = seconds
        if at_norm is not None:
            t["at"] = at_norm
            t["next_epoch"] = (int(round(time.time() + _daily_delay(t)))
                               if kind == "daily" else int(at_epoch))
        _STATE["tasks"][task_id] = t
        delay = None
        if enabled and _is_periodic(kind):
            delay = _delay_of(t)
            _arm(task_id, t, delay)
        elif old is not None:
            # 覆盖成事件任务 / 停用：必须取消旧 WorkManager 排程，否则会以旧
            # kind 再触发一次新任务（如旧 daily work 打到新的 event 任务）
            _cancel(task_id)
        _save()
    # 无条件同步：覆盖成无 events 的任务时也要清掉 Kotlin 侧旧订阅
    _sync_system_events()
    print(f"[task_manager] add {kind} {task_id} interval={seconds} "
          f"at={at_norm} events={names} debounce={gap}s enabled={enabled}")
    return {"ok": True, "id": task_id, "kind": kind,
            "interval": seconds, "at": at_norm,
            "next_epoch": t.get("next_epoch"), "next_in": delay,
            "events": names, "event": names[0] if names else None}


def add_event_task(task_id, event=None, code=None, func_name=None,
                   debounce=0.0, enabled=True, events=None,
                   interval=None, at=None):
    """兼容包装，等价于 add_task(..., events=...)：event/events 合并去重后转调。

    :param event: 事件名，或事件名列表，见 EVENTS（也可自定义，以后端实际派发为准）
    :param events: 事件名列表，与 event 合并去重
    :param debounce: 秒；同一任务两次触发的最小间隔，0 表示不节流
    :param interval: 可选，同时作为定时任务（秒）
    :param at: 可选，同时作为一次性时刻任务
    """
    names = _norm_events(event, events)
    if names is None:
        return {"ok": False, "error": "event/events 必须是字符串或字符串列表",
                "event": event, "events": events}
    return add_task(task_id, func_name=func_name, interval=interval, code=code,
                    enabled=enabled, events=names, at=at, debounce=debounce)


def _parse_hms(at):
    """把 "HH:MM" / "HH:MM:SS" 解析成 (时, 分, 秒)；非法格式抛 ValueError。"""
    parts = str(at).strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS")
    try:
        hh, mm = int(parts[0]), int(parts[1])
        ss = int(parts[2]) if len(parts) == 3 else 0
    except (TypeError, ValueError):
        raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS")
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
        raise ValueError("时间超出范围")
    return hh, mm, ss


# 绝对一次性 at 支持的格式（归一到 "YYYY-MM-DD HH:MM:SS"）
_AT_ABS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _at_tod_epoch(hh, mm, ss, now=None):
    """HH:MM:SS 的下一次出现（本地时区）epoch；当天已过则顺延到明天。"""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, 0, -1))
    if target <= now:
        target += 86400
    return target


def _parse_at(at, now=None):
    """解析 at 表达式，返回 (epoch, 规范化字符串, 是否绝对时间)；非法抛 ValueError。

    支持两种写法：
      - 绝对一次性：``2026-12-01 08:30:00`` / ``2026-12-01 08:30``
      - 时刻一次性：``HH:MM`` / ``HH:MM:SS``（今天已过则取明天）
    也接受 ``datetime.datetime``（按绝对一次性处理，忽略 now）。
    """
    if isinstance(at, datetime.datetime):
        return (int(at.timestamp()),
                at.strftime("%Y-%m-%d %H:%M:%S"), True)
    s = str(at).strip()
    for fmt in _AT_ABS_FORMATS:
        try:
            st = time.strptime(s, fmt)
        except ValueError:
            continue
        return time.mktime(st), time.strftime("%Y-%m-%d %H:%M:%S", st), True
    hh, mm, ss = _parse_hms(s)
    return _at_tod_epoch(hh, mm, ss, now), s, False


def _daily_delay(t, now=None):
    """距下一次 at 时刻的秒数（本地时区；当天已过则顺延到明天）。"""
    hh, mm, ss = _parse_hms(t.get("at", "00:00"))
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, 0, -1))
    if target <= now:
        target += 86400
    return int(max(1, target - now))


def _delay_of(t, now=None):
    """按 kind 计算距下次触发的秒数（>=1）；无排程来源返回 0。

    一次性 at 已过期时返回 1，即“立即执行一次”。
    """
    now = time.time() if now is None else now
    kind = _kind(t)
    if kind == "interval":
        return max(1, int(t.get("interval", 60) or 60))
    if kind == "daily":
        return _daily_delay(t, now)
    if kind == "at":
        try:
            epoch, _norm, absolute = _parse_at(t.get("at", ""), now)
        except (ValueError, TypeError):
            return 0
        if absolute:
            grace = float(t.get("misfire_grace", 60) or 0)
            if epoch < now - grace:
                return 0          # 过期太久：不补跑（由 _arm 标记 MISSED）
        return max(1, int(round(epoch - now)))
    return 0


def _arm(task_id, t, delay=None):
    """计算/使用给定延迟交给 WorkManager，并记下 next_epoch。

    只有调度成功才写 next_epoch，失败记 last_schedule_error 供 RPC/心跳发现；
    任何异常只降级（返回 False），绝不向外抛。
    """
    try:
        if delay is None:
            delay = _delay_of(t)
        delay = int(delay or 0)
        if delay <= 0:
            if _kind(t) == "at":
                # 一次性 at 过期超过 misfire_grace：不再补跑，明确标记
                t["enabled"] = False
                t["next_epoch"] = None
                t["last_result"] = "MISSED: 过期超过 misfire_grace"
            return False
        if _schedule(task_id, delay):
            t["next_epoch"] = int(round(time.time() + delay))
            t.pop("last_schedule_error", None)
            return True
        t["last_schedule_error"] = "WorkManager 排程失败"
        return False
    except Exception:
        print(f"[task_manager] 排程 {task_id} 失败:\n"
              + traceback.format_exc(limit=2))
        t["last_schedule_error"] = "排程异常"
        return False


def add_at_task(task_id, at, code=None, func_name=None, enabled=True,
                events=None, interval=None):
    """兼容包装，等价于 add_task(..., at=at)（一次性时刻任务）。

    :param at: "YYYY-MM-DD HH:MM[:SS]" 绝对一次性；或 "HH:MM[:SS]" 表示下一次该时刻
    :param events: 可选，同时订阅的系统事件（字符串或列表）
    :param interval: 可选，给定时 kind 取 interval（interval > at 优先级）
    触发后一次性 at 自动置 enabled=False，并保留 last_run/last_result。
    """
    return add_task(task_id, func_name=func_name, interval=interval, code=code,
                    enabled=enabled, events=events, at=at)


def add_daily_task(task_id, at="00:00", code=None, func_name=None, enabled=True):
    """兼容包装，等价于 add_task(..., at=at, repeat=True)（每日任务）。"""
    return add_task(task_id, func_name=func_name, code=code, enabled=enabled,
                    at=at, repeat=True)


def remove_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].pop(task_id, None)
        existed = t is not None
        _save()
    if existed and _is_periodic(_kind(t)):
        _cancel(task_id)
    if existed:
        _sync_system_events()
    return {"ok": existed}
del_task=remove_task

def pause_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        t["enabled"] = False
        _save()
        periodic = _is_periodic(_kind(t))
    if periodic:
        _cancel(task_id)
    _sync_system_events()
    return {"ok": True}


def resume_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        t["enabled"] = True
        kind = _kind(t)
        events = _system_events_of(t)
        if _is_periodic(kind):
            # 重算下一次（at 过期则 1 秒后补发一次）
            _arm(task_id, t)
        _save()
    _sync_system_events()
    return {"ok": True, "kind": kind,
            "event": events[0] if events else None, "events": events}


def run_now(task_id):
    """立即执行一次（manual 模式：不接力、不消耗一次性 at、不改 enabled）。"""
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        kind = _kind(t)
    if kind == "event":
        events = _system_events_of(t)
        if not events:
            return {"ok": False, "error": "任务未订阅任何系统事件"}
        r = trigger(task_id, events[0], None, source="run_now", mode="manual")
    else:
        r = trigger(task_id, _pseudo_event(kind),
                    {"task_id": task_id, "source": "run_now"}, source="run_now",
                    mode="manual")
    r["kind"] = kind
    return r


def list_tasks():
    """全部任务及其状态（深拷贝 + JSON 净化）；补齐 events/at/next_epoch。"""
    with _LOCK:
        try:
            snap = json.loads(json.dumps(_STATE["tasks"], ensure_ascii=False))
        except (TypeError, ValueError):
            # 净化失败只降级：逐任务标准化，绝不返回空表掩盖问题
            snap = {tid: {k: _json_safe(v) for k, v in t.items()}
                    for tid, t in _STATE["tasks"].items()
                    if isinstance(t, dict)}
    for t in snap.values():
        if isinstance(t, dict):
            t.setdefault("events", [])
            t.setdefault("at", None)
            t.setdefault("next_epoch", None)
    return snap


def list_events():
    """可订阅事件名（提示用）。"""
    return list(EVENTS)


def recent_events(limit=20):
    """最近发生的事件环形缓冲（深拷贝），供 RPC 校验事件链路。"""
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 20
    with _LOCK:
        items = _RECENT_EVENTS[-n:]
        try:
            return json.loads(json.dumps(items, ensure_ascii=False))
        except (TypeError, ValueError):
            return [dict(e) for e in items]


def network_state():
    """当前默认网络快照（Kotlin NetworkEventMonitor.getState()）。

    返回 dict：wifi / online（INTERNET 能力位）/ validated / initialized /
    ts（毫秒）。进程刚起、首个网络回调未到之前 initialized=False 且字段全零，
    任务应视为“未知”而不是“未联网”。
    """
    if _jclass is None:
        return {"ok": False, "error": "非 Android 环境"}
    try:
        snap = json.loads(_jclass(_NETWORK_MONITOR).getState())
        snap["ok"] = True
        return snap
    except Exception:
        return {"ok": False, "error": traceback.format_exc(limit=2)}


def _fmt_epoch_ms(ms):
    """epoch 毫秒 -> 本地时间 'YYYY-MM-DD HH:MM:SS.mmm'；None 返回 '-'。"""
    if ms is None:
        return "-"
    sec, milli = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec)) + ".%03d" % milli


def system_events(limit=50, return_str=False):
    """读取 Kotlin SystemEventMonitor 的系统广播缓冲。

    每行格式 ``epoch_ms | event_name | payload_json``；默认返回结构化字典。
    :param return_str: True 时直接返回多行文本，每行一个事件，时间为
                       ``YYYY-MM-DD HH:MM:SS.mmm``（本地时区）。
    limit<=0 表示取全部（上限 500）。
    """
    if _jclass is None:
        return "非 Android 环境" if return_str else {"ok": False, "error": "非 Android 环境"}
    try:
        raw = _jclass(_SYSTEM_MONITOR).getEventLog(int(limit))
        registered = _jclass(_SYSTEM_MONITOR).getRegisteredEvents()
    except Exception:
        err = traceback.format_exc(limit=2)
        return err if return_str else {"ok": False, "error": err}
    try:
        lines = json.loads(raw) if raw else []
    except ValueError:
        lines = []
    events = []
    for line in lines:
        parts = str(line).split(" | ", 2)
        try:
            ts = int(parts[0]) if parts and parts[0].isdigit() else None
        except (ValueError, IndexError):
            ts = None
        item = {"ts_ms": ts,
                "time": _fmt_epoch_ms(ts),
                "event": parts[1] if len(parts) > 1 else "",
                "payload": {}}
        if len(parts) > 2:
            try:
                item["payload"] = json.loads(parts[2])
            except ValueError:
                item["payload"] = {"raw": parts[2]}
        events.append(item)
    if return_str:
        # 文本模式：时间为可读本地时间，事件名左对齐到固定列宽，payload 去掉
        # 与行前两列重复的 ts（毫秒时间戳）和 action（Intent action）。
        out = []
        for e in events:
            payload = dict(e["payload"])
            payload.pop("ts", None)
            payload.pop("action", None)
            line = "%s | %-24s" % (e["time"], e["event"])
            if payload:
                line += " | " + json.dumps(payload, ensure_ascii=False)
            out.append(line)
        return "\n".join(out)
    try:
        registered_names = json.loads(registered) if registered else []
    except ValueError:
        registered_names = []
    return {"ok": True, "count": len(events),
            "registered": registered_names, "events": events}


def clear_system_events():
    """清空 Kotlin 侧系统广播缓冲。"""
    if _jclass is None:
        return {"ok": False, "error": "非 Android 环境"}
    try:
        _jclass(_SYSTEM_MONITOR).clearEventLog()
        return {"ok": True}
    except Exception:
        return {"ok": False, "error": traceback.format_exc(limit=2)}


def test_event(event, payload=None):
    """RPC 本地模拟一个事件（无需真的开关 WiFi），走与系统事件完全相同的队列。"""
    fire_event(event, payload, source="rpc_test")
    return {"ok": True, "event": event, "queued": _EVENT_Q.qsize()}


# ============================ 事件入口（Kotlin 回调） ============================

def fire_event(event_name, payload=None, source="system", record=True,
               event_ts=None):
    """Kotlin 事件回调入口，也可被 RPC 模拟。

    必须快速返回：只记账并交给 trigger 入队，匹配任务的执行在串行守护线程
    完成，避免阻塞 Android 网络回调线程 / Chaquopy 调用线程。

    record=False：高频/大负载事件（如 ime_keys 按键合批）不写 recent_events
    环形缓冲、不 print；任务派发完全不受影响。
    """
    global _BOOT_DONE
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else None
        except (ValueError, AttributeError):
            payload = {"raw": payload}
    if not isinstance(payload, dict):
        payload = None if payload is None else {"value": payload}

    if event_name == "boot_completed":
        # 同一进程生命周期内只允许一次（预初始化补发 + boot_pending 可能撞车）
        if _BOOT_DONE:
            return {"ok": True, "event": event_name, "matched": [],
                    "duplicate": True}
        _BOOT_DONE = True

    if event_name in ("time_changed", "timezone_changed"):
        # 改时/改时区后 daily/at 的 next_epoch 会错位，重算并重排
        reschedule_all()

    if record:
        rec = {"ts": time.strftime("%H:%M:%S"),
               "epoch": round(time.time(), 3),
               "event": str(event_name),
               "source": source,
               "payload": _brief(payload)}
    with _LOCK:
        if record:
            _RECENT_EVENTS.append(rec)
            if len(_RECENT_EVENTS) > _RECENT_MAX:
                del _RECENT_EVENTS[:-_RECENT_MAX]
        if not _LOADED:
            # init 还没载入任务表：先缓存，等 init 末尾按原顺序补发，避免丢事件
            _PRE_INIT_EVENTS.append((str(event_name), payload, source))
            if len(_PRE_INIT_EVENTS) > _PRE_INIT_MAX:
                del _PRE_INIT_EVENTS[:-_PRE_INIT_MAX]
            matches = []
        else:
            # 定时/时刻/每日任务也可以同时订阅系统事件，故不按 kind 过滤
            matches = [
                tid for tid, t in _STATE["tasks"].items()
                if isinstance(t, dict) and t.get("enabled", True)
                and event_name in _system_events_of(t)
            ]
    for tid in matches:
        trigger(tid, event_name, payload, source=source, event_ts=event_ts)
    if record:
        print(f"[task_manager] event={event_name} source={source} "
              f"payload={rec['payload']} matched={matches}")
    return {"ok": True, "event": event_name, "matched": matches,
            "queue": _EVENT_Q.qsize()}


def trigger(task_id, event, payload=None, source="system", event_ts=None,
            mode="normal"):
    """统一触发入口：WorkManager / 系统事件 / RPC run_now 都走这里。

    统一做：存在性与 enabled 校验、事件绑定校验、debounce（定时也生效）、
    任务代际快照、周期任务接力排程，最后入队由唯一执行线程串行执行。
    返回结构化结果，ok=False 时 reason 说明被拒原因。
    """
    global _RUN_DROPPED
    task_id = str(task_id)
    now = time.time()
    ts = now if event_ts is None else float(event_ts)
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if t is None:
            return {"ok": False, "task_id": task_id, "reason": "not_found"}
        if not t.get("enabled", True):
            return {"ok": False, "task_id": task_id, "reason": "disabled"}
        if event not in PSEUDO_EVENTS and event not in _system_events_of(t):
            return {"ok": False, "task_id": task_id, "reason": "not_subscribed"}
        gap = float(t.get("debounce", 0.0) or 0.0)
        last = float(t.get("_last_fire_epoch", 0.0) or 0.0)
        periodic = event in PSEUDO_EVENTS and event == _pseudo_event(_kind(t))
        if gap > 0 and ts - last < gap:
            # 定时触发被节流也必须接力下一次，否则整条链断掉
            if periodic and event in ("interval", "daily") and mode != "manual":
                _arm(task_id, t, _delay_of(t))
            save_soft()
            return {"ok": False, "task_id": task_id, "reason": "debounced",
                    "gap": gap}
        t["_last_fire_epoch"] = ts
        gen = int(t.get("gen", 0) or 0)
        # 周期任务先接力下一次：本次执行失败或进程被杀也不会断链
        if periodic and event in ("interval", "daily") and mode != "manual":
            _arm(task_id, t, _delay_of(t))
    save_soft()
    item = (task_id, gen, event, payload, source, ts, mode)
    try:
        _EVENT_Q.put_nowait(item)
    except queue.Full:
        _RUN_DROPPED += 1
        print(f"[task_manager] 触发队列已满，丢弃 {task_id}/{event}")
        return {"ok": False, "task_id": task_id, "reason": "queue_full"}
    _ensure_event_worker()
    return {"ok": True, "task_id": task_id, "event": event,
            "queue": _EVENT_Q.qsize()}


def is_initialized():
    """Kotlin BootEventWorker 轮询用：init 是否已完成（状态路径已确定）。"""
    return _STATE_PATH is not None


def consume_boot_pending():
    """消费开机待发标记并派发 boot_completed（幂等）。

    标记由 Kotlin BootEventReceiver 写入；init 恢复完任务后调用，
    BootEventWorker 在 Python 已就绪时也会补调一次，保证不丢事件。
    """
    if not _STATE_PATH:
        return {"ok": False, "error": "not_initialized"}
    marker = os.path.join(os.path.dirname(_STATE_PATH), "boot_pending")
    if not os.path.exists(marker):
        return {"ok": True, "fired": False}
    try:
        with open(marker, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        raw = ""
    payload = {"boot": True,
               "boot_epoch": int(raw) if raw.isdigit() else None,
               "app_init_epoch": round(time.time(), 3)}
    r = fire_event("boot_completed", payload, source="boot")
    if r.get("duplicate"):
        print("[task_manager] boot_completed 已由预初始化补发，跳过重复")
    # 入队成功后再删标记：崩溃时宁可重发一次，也不静默丢失
    try:
        os.remove(marker)
    except OSError:
        pass
    return {"ok": True, "fired": not r.get("duplicate", False),
            "payload": payload, "matched": r.get("matched", [])}


def _ensure_event_worker():
    if getattr(_ensure_event_worker, "_on", False):
        return
    with _LOCK:
        if getattr(_ensure_event_worker, "_on", False):
            return
        t = threading.Thread(target=_event_worker, daemon=True,
                             name="PyTaskEvent")
        t.start()
        _ensure_event_worker._on = True


def _event_worker():
    while True:
        task_id, gen, event_name, payload, source, event_ts, mode = _EVENT_Q.get()
        # 任何异常都只降级：绝不能让守护线程退出
        try:
            with _LOCK:
                t = _STATE["tasks"].get(task_id)
            if t is None or not t.get("enabled", True):
                continue
            # 代际校验：覆盖/删除后旧触发自动失效
            if int(t.get("gen", 0) or 0) != gen:
                continue
            if event_name not in PSEUDO_EVENTS and event_name not in _system_events_of(t):
                continue
            # debounce 已在 trigger 统一处理，这里只负责执行
            _execute(task_id, t, event_name, payload, mode=mode)
        except Exception:
            print(f"[task_manager] 事件分发 {task_id} 异常:\n"
                  + traceback.format_exc(limit=2))


# ============================ 任务执行 ============================

def _run_code(code, event_name, payload, task_id):
    """执行随任务下发的 Python 源码；print 同时进 App 日志窗口和捕获缓冲。"""
    real_stdout = sys.stdout
    buf = io.StringIO()

    def tprint(*args, **kwargs):
        kwargs.setdefault("flush", False)
        do_flush = kwargs.pop("flush")
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        msg = sep.join(str(a) for a in args) + end
        try:
            real_stdout.write(msg)
            if do_flush:
                real_stdout.flush()
        except Exception:
            pass
        buf.write(msg)

    ns = {
        "__builtins__": __builtins__,
        "print": tprint,
        "event": event_name,
        "payload": payload,
        "task_id": task_id,
    }
    if _jclass is not None:
        ns["jclass"] = _jclass
    exec(compile(code, f"<task:{task_id}>", "exec"), ns)
    return buf.getvalue().strip()


def _execute(task_id, t, event_name=None, payload=None, mode="normal"):
    result, error = None, None
    gen = int(t.get("gen", 0) or 0)
    try:
        code = t.get("code")
        if code:
            captured = _run_code(code, event_name, payload, task_id)
            result = captured or "(无输出)"
        else:
            func = JOB_FUNCS.get(t.get("func"))
            if func is None:
                raise RuntimeError(f"函数 {t.get('func')} 未注册")
            result = func()
    except Exception:
        error = traceback.format_exc(limit=3)
        print(f"[task_manager] task {task_id} 异常:\n{error}")
    finally:
        try:
            with _LOCK:
                cur = _STATE["tasks"].get(task_id)
                if cur is not t or int(cur.get("gen", 0) or 0) != gen:
                    # 执行期间被 remove/覆盖：不写旧对象、不接力、不落盘
                    print(f"[task_manager] task {task_id} 执行期间已被删除/覆盖，丢弃结果")
                else:
                    t["run_count"] = int(t.get("run_count", 0)) + 1
                    t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    t["last_result"] = (str(result)[:500] if error is None
                                        else "ERROR: " + error.splitlines()[-1])
                    if event_name is not None:
                        t["last_event"] = event_name
                        t["last_payload"] = _brief(payload)
                    if (_kind(t) == "at" and event_name == "at"
                            and mode != "manual"):
                        # 一次性 at：触发后自动停用（结构变更，立即落盘）
                        t["enabled"] = False
                        t["next_epoch"] = None
                        _save()
                    else:
                        save_soft()
        except Exception:
            # 收尾（统计/持久化）失败只降级，不影响事件 worker 线程
            print(f"[task_manager] task {task_id} 收尾失败:\n"
                  + traceback.format_exc(limit=2))
    return "ok" if error is None else "error"


def execute_task(task_id):
    """WorkManager（PythonTaskWorker）回调入口。

    ★ 只做校验与入队后立即返回，绝不在这里跑任务代码：WorkManager 线程
    不应被任意 Python 长任务占住。周期任务的接力已在 trigger 内完成，
    因此入队后进程被杀也不会断链。
    """
    try:
        with _LOCK:
            t = _STATE["tasks"].get(task_id)
        if t is None:
            print(f"[task_manager] execute {task_id}: 未注册，忽略")
            return "not_found"
        if not t.get("enabled", True):
            return "disabled"
        kind = _kind(t)
        name = _pseudo_event(kind)
        if name is None:
            return "not_periodic_task"
        payload = {"task_id": task_id,
                   "scheduled_at": t.get("next_epoch"),
                   "fired_at": int(time.time())}
        if kind == "interval":
            payload["interval"] = t.get("interval")
        else:
            payload["at"] = t.get("at")
        r = trigger(task_id, name, payload, source="workmanager")
        return "ok" if r.get("ok") else r.get("reason", "error")
    except Exception:
        print(f"[task_manager] execute {task_id} 异常:\n"
              + traceback.format_exc(limit=2))
        return "error"

