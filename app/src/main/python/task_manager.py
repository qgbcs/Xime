"""高可靠后台任务框架（Python 侧）—— 定时 / 事件 / 依赖链 / 可观测

架构：WorkManager（或 AlarmManager 精确后端）负责系统级调度，Kotlin 事件监视器
负责系统事件边沿；Python 负责任务注册表、状态/运行/历史分离持久化、统一触发
队列、串行执行、重试与死信、健康指标。

不变式（改代码前先读）：
  1) 所有触发（WorkManager / 系统事件 / RPC / 重试 / 依赖链）只进入统一入口
     trigger()，投递到有界优先级队列，由唯一分发线程串行入池执行；
  2) 同一任务同一时刻最多一个执行体（每任务锁 + 代际 gen 双重校验），旧执行
     不能回写新任务状态，杜绝“幽灵接力”；
  3) 任何边缘功能（统计/历史/指标/事件持久化）失败只降级，绝不阻塞或抛到
     MQTT/HTTP RPC 主流程；
  4) 结构变更立即原子落盘；统计与历史按 3 秒合并写盘。

触发方式（kind）：
  interval  每 N 秒（最小间隔见 CONFIG min_interval）；
  at        绝对 "YYYY-MM-DD HH:MM[:SS]" 一次性，或 "HH:MM[:SS]" 下一次该时刻；
  daily     at="HH:MM[:SS]" + repeat=True，按本地时区每天；
  cron      5 段表达式 "分 时 日 月 周"（0=周日），支持 * , - 与步长；
  event     纯被动，只响应 events 列表中的系统事件。

任务动作：code（随任务下发的 Python 源码，可用 event / payload / task_id /
 jclass / cancel_event）或 func（JOB_FUNCS 内置函数，code 优先）。

可靠性参数（add_task 的 opts）：
  timeout         单次执行超时秒数（0=不限制，超时只记账并告警，无法强杀线程）
  retry           失败重试次数（指数退避），耗尽进死信队列
  retry_delay     首次重试延迟秒，默认 5；retry_max_delay 上限，默认 300
  misfire_grace   错过多久不再补跑，默认 60
  coalesce        同一任务堆积触发时合并（定时默认开启）
  coalesce_mode   "latest"（保留最新 payload）/ "count"（计数）
  catchup         停机错过的周期是否补跑一次
  depends_on      依赖任务列表，全部 last_status=ok 才执行
  then            成功后链式触发的任务列表（mode=chain）
  priority        执行优先级（越大越先），-1000~1000
  group           分组名，便于批量启停
  constraints     调度约束（network/charging/battery_not_low/storage_not_low/device_idle）
  backend         workmanager | alarm
  when            事件过滤条件（payload 字段比较）
  schema          payload 校验（字段->类型）
  dedupe / idem   幂等去重窗口（秒）与去重键（payload 字段名）

RPC 示例：
  task_manager.add_task("tick", interval=60, code='print(event)')
  task_manager.add_task("job", cron="*/5 * * * *", retry=2, timeout=20,
                        code='print(payload)')
  task_manager.add_task("once", at="2026-12-01 08:30:00", code='print(1)')
  task_manager.add_task("chain_a", interval=30, then=["chain_b"], code='print(1)')
  task_manager.trigger("chain_b", "chained", {"from": "chain_a"}, mode="chain")
  task_manager.run_now("tick")
  task_manager.health(); task_manager.metrics(); task_manager.history("tick")
  task_manager.list_tasks(page=1, page_size=20, group="ops")
  task_manager.export_tasks(); task_manager.import_tasks(data)
"""
import builtins
import datetime
import heapq
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

try:
    from java import jclass
    _jclass = jclass
except Exception:  # pragma: no cover - 非 Android 环境（PC 侧单测）
    _jclass = None

STATE_VERSION = 2
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# ------------------------------ 可配置项 ------------------------------
# 全部可在运行时 RPC set_config(**kw) 修改；改到队列/并发时立即生效。
CONFIG = {
    "min_interval": 1,        # 定时任务最小间隔（秒）
    "history_len": 20,        # 每任务保留的执行历史条数
    "output_max": 4096,       # 单次执行输出摘要上限（字符）
    "queue_max": 256,         # 触发队列上限（超过丢弃并计数）
    "workers": 4,             # 并发执行线程数（同任务仍串行）
    "task_timeout": 0,        # 默认单次执行超时（秒，0=不限制）
    "lock_wait": 10.0,        # 同任务已有执行体时最多等待多久
    "backend": "workmanager",  # 调度后端：workmanager | alarm
    "retry_max_delay": 300,   # 指数退避上限（秒）
    "dead_letter_max": 100,   # 死信队列上限
    "event_log_max": 500,     # 关键事件持久化上限
    "metrics_events": 0,      # >0 时每 N 秒发 task_metrics 事件（0=关）
    "watch_interval": 1.0,    # 看门狗轮询间隔（秒）
}

_STATE_PATH = None
_RUNTIME_PATH = None
_HISTORY_PATH = None
_LOCK = threading.RLock()

# 内存中的任务表同时容纳静态定义与运行统计，落盘时按 RUNTIME_KEYS 拆分。
_STATE = {"version": STATE_VERSION, "tasks": {}, "templates": {}}
_RUNTIME = {"tasks": {}}
_HISTORY = {"tasks": {}, "dead_letters": [], "alerts": [], "events": []}

# 运行统计字段（写入 task_runtime.json；其余写入 task_manager.json）
RUNTIME_KEYS = frozenset({
    "run_count", "fail_count", "timeout_count", "skip_count",
    "last_run", "last_result", "last_status", "last_error",
    "last_event", "last_payload", "last_duration_ms", "last_attempt",
    "next_epoch", "last_schedule_error", "consecutive_failures",
    "_last_fire_epoch", "_sched_retry_epoch", "sched_fail_count",
    "total_ms", "last_skip_reason",
})

# 统一触发队列：有界优先级队列（priority 越大越先），满了丢新并计数，
# 绝不阻塞 Kotlin 回调线程或 RPC 线程。
_EVENT_Q = queue.PriorityQueue(maxsize=int(CONFIG["queue_max"]))
_SEQ = 0
_RUN_DROPPED = 0

_RECENT_EVENTS = []          # 环形缓冲（供 RPC 回读）
_RECENT_MAX = 50
_EVENT_SEQ = 0
_EVENT_INDEX = {}            # event -> set(task_id)，结构变更时重建
_INDEX_DIRTY = True

_PENDING = {}                # tid -> {event,payload,count,ts}，合并用
_RETRY_HEAP = []             # (due, seq, item)
_RETRY_LOCK = threading.Lock()
_RETRY_SEQ = 0
_RUNNING = {}                # tid -> {start,event,attempt,timed_out}
_CANCEL = {}                 # tid -> threading.Event
_CODE_CACHE = {}             # tid -> (gen, code_hash, codeobj)
_TASK_LOCKS = {}             # tid -> threading.Lock
_RUN_POOL = None
_WORKER_ON = False
_WATCHDOG_ON = False
_HEARTBEAT = 0.0
_STARTED_AT = time.time()

# 热更新：新模块导入时给上一个实例的线程发停机信号，避免两个实例同时写
# 同一批状态文件 / 重复排程（旧实例的 daemon 线程本不会自动退出）。
_STOP = threading.Event()
_prev_stop = getattr(builtins, "_tm_stop_event", None)
if _prev_stop is not None and _prev_stop is not _STOP:
    try:
        _prev_stop.set()
    except Exception:
        pass
builtins._tm_stop_event = _STOP

_DIRTY = False
_HIST_DIRTY = False
_SOFT_SAVE_GAP = 3.0
_last_soft_save = 0.0
_last_metrics_event = 0.0

_BOOT_DONE = False
_LOADED = False

# init 载入任务表之前到达的事件先缓存，载入完成后按原顺序补发。
_PRE_INIT_EVENTS = []
_PRE_INIT_MAX = 64

COUNTERS = {
    "runs": 0, "ok": 0, "fail": 0, "timeout": 0, "skip": 0,
    "retry": 0, "dead": 0, "coalesced": 0, "deduped": 0,
    "dropped": 0, "sched_fail": 0, "chain": 0,
}

# 事件名白名单（仅用于 RPC 提示；fire_event 不强制，以后加事件不用改 Python）
EVENTS = (
    "boot_completed",
    "wifi_connected",
    "wifi_disconnected",
    "network_online",
    "network_offline",
    "network_state",
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
    "screen_rotation_changed",
    "call_state_changed",
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
    "ime_keys",
    "clipboard_text",
    "speech_recorded",
    "task_metrics",
)

# 伪事件名：定时/时刻/每日/cron 到点后也以“事件”形式进入统一分发路径。
# 它们永远不会同步给 Kotlin 订阅集合。
PSEUDO_EVENTS = ("interval", "at", "daily", "cron")

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


# ============================ 通用小工具 ============================

def _now():
    return time.time()


def _truncate(text, limit):
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "...<len=%d>" % len(text)


def _json_safe(value, depth=0):
    """把任意对象转成可 JSON 序列化的基本类型（带长度上限，只用于记账）。"""
    if depth > 4:
        return _truncate(repr(value), 120)
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


def _json_copy(value, depth=0):
    """可落盘/可导出的完整拷贝：保留长字符串与全量列表（只处理不可序列化对象）。

    与 _json_safe 的区别：_json_safe 用于记账摘要，会截断长文本；任务 code、
    模板、导入导出必须用本函数，否则源码会被截成 256 字符。
    """
    if depth > 8:
        return _truncate(repr(value), 200)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return "<bytes len=%d>" % len(value)
    if isinstance(value, (list, tuple)):
        return [_json_copy(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_copy(v, depth + 1) for k, v in value.items()}
    try:
        return _truncate(repr(value), 200)
    except Exception:
        return "<unrepr>"


def _brief(payload):
    return _json_safe(payload)


def _task(tid):
    """读取任务对象（调用方需持有 _LOCK 或接受快照语义）。"""
    return _STATE["tasks"].get(tid)


def _task_lock(tid):
    with _LOCK:
        lock = _TASK_LOCKS.get(tid)
        if lock is None:
            lock = threading.Lock()
            _TASK_LOCKS[tid] = lock
        return lock


def _next_seq():
    global _SEQ
    with _LOCK:
        _SEQ += 1
        return _SEQ


# ============================ 持久化 ============================

def _atomic_write(path, obj, fallback=None):
    """原子写 JSON（紧凑 + fsync + os.replace）；失败只降级，返回 False。"""
    if not path:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except (TypeError, ValueError):
        print("[task_manager] 序列化失败，净化后重试:\n"
              + traceback.format_exc(limit=3))
        if fallback is None:
            return False
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_json_copy(fallback), f, ensure_ascii=False,
                          separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except Exception as e:
            print(f"[task_manager] 净化落盘仍失败: {e}")
            return False
    except OSError as e:
        print(f"[task_manager] 写盘失败 {path}: {e}")
        return False


def _sanitized_static():
    tasks = {}
    for tid, t in _STATE.get("tasks", {}).items():
        if not isinstance(t, dict):
            continue
        tasks[tid] = {k: _json_copy(v) for k, v in t.items()
                      if k not in RUNTIME_KEYS}
    return {"version": STATE_VERSION, "tasks": tasks,
            "templates": _json_copy(_STATE.get("templates", {}))}


def _sanitized_runtime():
    tasks = {}
    for tid, t in _STATE.get("tasks", {}).items():
        if not isinstance(t, dict):
            continue
        rt = {k: _json_copy(v) for k, v in t.items() if k in RUNTIME_KEYS}
        if rt:
            tasks[tid] = rt
    with _RETRY_LOCK:
        retries = [_json_copy(it) for (_due, _seq, it) in list(_RETRY_HEAP)]
    return {"version": STATE_VERSION, "tasks": tasks, "retries": retries}


def _sanitized_history():
    return {"version": STATE_VERSION,
            "tasks": _json_copy(_HISTORY.get("tasks", {})),
            "dead_letters": _json_copy(_HISTORY.get("dead_letters", [])),
            "alerts": _json_copy(_HISTORY.get("alerts", [])),
            "events": _json_copy(_HISTORY.get("events", []))}


def _save():
    """结构变更立即落盘（tasks + runtime 两个文件）。"""
    global _DIRTY
    if _STOP.is_set():
        return False
    if not _STATE_PATH:
        return False
    ok1 = _atomic_write(_STATE_PATH, _sanitized_static(), _sanitized_static())
    ok2 = _atomic_write(_RUNTIME_PATH, _sanitized_runtime(), _sanitized_runtime())
    if ok1 and ok2:
        _DIRTY = False
    else:
        _DIRTY = True
    return ok1 and ok2


def _save_history():
    global _HIST_DIRTY
    if _STOP.is_set():
        return False
    if not _HISTORY_PATH:
        return False
    ok = _atomic_write(_HISTORY_PATH, _sanitized_history(), _sanitized_history())
    _HIST_DIRTY = not ok
    return ok


def save_soft():
    """统计/历史类更新：标脏并最多每 _SOFT_SAVE_GAP 秒合并落盘一次。"""
    global _DIRTY, _HIST_DIRTY, _last_soft_save
    if _STOP.is_set():
        return False
    _DIRTY = True
    _HIST_DIRTY = True
    now = _now()
    if now - _last_soft_save < _SOFT_SAVE_GAP:
        return False
    _last_soft_save = now
    ok = _save()
    _save_history()
    return ok


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        if os.path.exists(path):
            bad = "%s.bad.%d" % (path, int(_now()))
            try:
                os.replace(path, bad)
                print(f"[task_manager] 状态文件损坏，已备份到 {bad}: {e}")
            except OSError:
                print(f"[task_manager] 状态文件损坏且备份失败: {e}")
        return default


def init(files_dir):
    """app.py start() 内调用：加载持久化状态，恢复定时任务调度。

    幂等：同一模块实例重复调用直接返回，不会重复起线程/重复排程。
    """
    global _STATE_PATH, _RUNTIME_PATH, _HISTORY_PATH, _LOADED
    if _LOADED:
        return True
    _STATE_PATH = os.path.join(files_dir, "task_manager.json")
    _RUNTIME_PATH = os.path.join(files_dir, "task_runtime.json")
    _HISTORY_PATH = os.path.join(files_dir, "task_history.json")
    catchups = []
    with _LOCK:
        data = _load_json(_STATE_PATH, {})
        tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        _STATE["tasks"] = tasks
        tpl = data.get("templates")
        _STATE["templates"] = tpl if isinstance(tpl, dict) else {}
        try:
            _STATE["version"] = int(data.get("version") or STATE_VERSION)
        except (TypeError, ValueError):
            _STATE["version"] = STATE_VERSION
        rt = _load_json(_RUNTIME_PATH, {})
        rt_tasks = rt.get("tasks") if isinstance(rt.get("tasks"), dict) else {}
        for tid, extra in rt_tasks.items():
            t = _STATE["tasks"].get(tid)
            if isinstance(t, dict) and isinstance(extra, dict):
                t.update(extra)
        hist = _load_json(_HISTORY_PATH, {})
        for key in ("tasks", "dead_letters", "alerts", "events"):
            val = hist.get(key)
            if isinstance(val, type(_HISTORY.get(key))):
                _HISTORY[key] = val
        retries = rt.get("retries")
        if isinstance(retries, list):
            for it in retries:
                if isinstance(it, dict) and it.get("task_id"):
                    due = float(it.get("due", _now()) or _now())
                    _push_retry_item(it, due, persist=False)

        interval_n = at_n = daily_n = cron_n = event_n = 0
        now = _now()
        for tid, t in list(_STATE["tasks"].items()):
            try:
                if not isinstance(tid, str) or not _TASK_ID_RE.match(tid):
                    print(f"[task_manager] 忽略非法任务 id: {tid!r}")
                    continue
                _normalize_task(t)
                if not t.get("enabled", True):
                    continue
                kind = _kind(t)
                if not _is_periodic(kind):
                    event_n += 1
                    continue
                nxt = t.get("next_epoch")
                grace = float(t.get("misfire_grace", 60) or 0)
                if nxt and now > float(nxt) + grace:
                    missed = max(1, int((now - float(nxt)) / max(1, _period_of(t))))
                    if t.get("catchup"):
                        catchups.append((tid, kind, missed))
                    else:
                        t["last_skip_reason"] = "missed:%d" % missed
                _arm(tid, t)
                if kind == "interval":
                    interval_n += 1
                elif kind == "at":
                    at_n += 1
                elif kind == "daily":
                    daily_n += 1
                else:
                    cron_n += 1
            except Exception:
                print(f"[task_manager] 恢复任务 {tid} 失败:\n"
                      + traceback.format_exc(limit=2))
        _save()
        _LOADED = True
    _rebuild_index()
    _sync_system_events()
    _ensure_workers()
    _replay_pre_init_events()
    consume_boot_pending()
    for tid, kind, missed in catchups:
        try:
            trigger(tid, _pseudo_event(kind),
                    {"task_id": tid, "missed": missed, "catchup": True},
                    source="boot_catchup", mode="catchup")
        except Exception:
            print("[task_manager] 补跑失败:\n" + traceback.format_exc(limit=2))
    print(f"[task_manager] init done, {len(_STATE['tasks'])} tasks, "
          f"{interval_n} interval + {at_n} at + {daily_n} daily + {cron_n} cron "
          f"rescheduled, {event_n} event tasks armed, {len(catchups)} catchup")
    return True


def _replay_pre_init_events():
    with _LOCK:
        items = list(_PRE_INIT_EVENTS)
        del _PRE_INIT_EVENTS[:]
    for name, payload, source in items:
        try:
            fire_event(name, payload, source=source, record=False)
        except Exception:
            print("[task_manager] 补发预初始化事件失败:\n"
                  + traceback.format_exc(limit=2))


# ============================ 任务模型 ============================

def _kind(t):
    kind = t.get("kind")
    if kind in PSEUDO_EVENTS or kind == "event":
        return kind
    return "daily" if t.get("at") else "interval"


def _is_periodic(kind):
    return kind in PSEUDO_EVENTS


def _pseudo_event(kind):
    return kind if kind in PSEUDO_EVENTS else None


def _normalize_task(t):
    """就地补齐旧任务字段与默认值，脏数据只降级不抛错。"""
    if not isinstance(t, dict):
        return t
    t.setdefault("gen", 1)
    t["kind"] = _kind(t)
    if not isinstance(t.get("events"), list):
        t["events"] = _system_events_of(t)
    t.setdefault("debounce", 0.0)
    t.setdefault("misfire_grace", 60.0)
    t.setdefault("enabled", True)
    t.setdefault("run_count", 0)
    t.setdefault("fail_count", 0)
    t.setdefault("timeout_count", 0)
    t.setdefault("skip_count", 0)
    t.setdefault("consecutive_failures", 0)
    t.setdefault("priority", 0)
    t.setdefault("timeout", 0)
    t.setdefault("retry", 0)
    t.setdefault("retry_delay", 5.0)
    t.setdefault("version", 1)
    if t.get("coalesce_mode") is None:
        t["coalesce_mode"] = "latest" if t.get("coalesce") else None
    if t.get("coalesce") is None:
        t["coalesce"] = _kind(t) in ("interval", "daily", "cron")
    return t


def _norm_events(event=None, events=None):
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
    return [e for e in _events_of(t) if e not in PSEUDO_EVENTS]


def _period_of(t):
    kind = _kind(t)
    if kind == "interval":
        return max(1, int(t.get("interval", 60) or 60))
    if kind == "daily":
        return 86400
    if kind == "cron":
        nxt = _cron_next_epoch(t.get("cron"), _now())
        nxt2 = _cron_next_epoch(t.get("cron"), (nxt or _now()) + 60)
        if nxt and nxt2:
            return max(1, int(nxt2 - nxt))
        return 60
    return 60


# ============================ cron 表达式 ============================

def _cron_field(part, lo, hi):
    out = set()
    for piece in str(part).split(","):
        piece = piece.strip()
        if not piece:
            raise ValueError("空的 cron 字段")
        step = 1
        base = piece
        if "/" in piece:
            base, st = piece.split("/", 1)
            step = int(st)
            if step <= 0:
                raise ValueError("cron 步长必须为正")
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            a_s, b_s = base.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = int(base)
            b = a
        if a > b:
            a, b = b, a
        if a < lo or b > hi:
            raise ValueError("cron 字段超出范围 %s" % piece)
        out.update(range(a, b + 1, step))
    return out


def _cron_parse(expr):
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise ValueError("cron 必须是 5 段：分 时 日 月 周")
    minute = _cron_field(parts[0], 0, 59)
    hour = _cron_field(parts[1], 0, 23)
    dom = _cron_field(parts[2], 1, 31)
    month = _cron_field(parts[3], 1, 12)
    dow = _cron_field(parts[4], 0, 7)
    if 7 in dow:
        dow = set(0 if d == 7 else d for d in dow)
    return minute, hour, dom, month, dow


def _cron_next_epoch(expr, now=None):
    """下一次命中的 epoch（按本地时区逐日推进，DST 由 timestamp 处理）。"""
    try:
        minute, hour, dom, month, dow = _cron_parse(expr)
    except (ValueError, TypeError):
        return None
    now = _now() if now is None else float(now)
    base = datetime.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    start = base + datetime.timedelta(minutes=1)
    hours = sorted(hour)
    mins = sorted(minute)
    day = start
    for _ in range(366 * 6):
        if (day.month in month and day.day in dom
                and ((day.weekday() + 1) % 7) in dow):
            for h in hours:
                if h < day.hour:
                    continue
                for m in mins:
                    if h == day.hour and m < day.minute:
                        continue
                    cand = day.replace(hour=h, minute=m, second=0, microsecond=0)
                    if cand >= start:
                        return int(cand.timestamp())
        day = (day + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return None


def validate_cron(expr):
    try:
        _cron_parse(expr)
        return {"ok": True, "cron": str(expr),
                "next_epoch": _cron_next_epoch(expr)}
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e), "cron": expr}


def cron_next(expr, count=1):
    try:
        n = max(1, min(20, int(count)))
    except (TypeError, ValueError):
        n = 1
    out = []
    cur = _now()
    for _ in range(n):
        nxt = _cron_next_epoch(expr, cur)
        if not nxt:
            break
        out.append({"epoch": nxt,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(nxt))})
        cur = nxt + 60
    if not out:
        return {"ok": False, "error": "无法计算下一次 cron 时间", "cron": expr}
    return {"ok": True, "cron": expr, "next": out}


# ============================ 时间计算 ============================

def _parse_hms(at):
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


_AT_ABS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _at_tod_epoch(hh, mm, ss, now=None):
    now = _now() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, 0, -1))
    if target <= now:
        target += 86400
    return target


def _parse_at(at, now=None):
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
    hh, mm, ss = _parse_hms(t.get("at", "00:00"))
    now = _now() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, 0, -1))
    if target <= now:
        target += 86400
    return int(max(1, target - now))


def _delay_of(t, now=None):
    now = _now() if now is None else now
    kind = _kind(t)
    if kind == "interval":
        return max(int(CONFIG["min_interval"]),
                   int(t.get("interval", 60) or 60))
    if kind == "daily":
        return _daily_delay(t, now)
    if kind == "cron":
        nxt = _cron_next_epoch(t.get("cron"), now)
        if not nxt:
            return 0
        return max(1, int(nxt - now))
    if kind == "at":
        try:
            epoch, _norm, absolute = _parse_at(t.get("at", ""), now)
        except (ValueError, TypeError):
            return 0
        if absolute:
            grace = float(t.get("misfire_grace", 60) or 0)
            if epoch < now - grace:
                return 0
        return max(1, int(round(epoch - now)))
    return 0


# ============================ WorkManager / Alarm 桥 ============================

_TASK_SCHEDULER = "com.kingzcheung.xime.task.TaskScheduler"


def _schedule(task_id, delay_seconds, t=None):
    """按任务配置选择后端与约束；任何失败返回 False（由调用方记错误）。"""
    if _jclass is None:
        return False
    t = t or {}
    backend = t.get("backend") or CONFIG["backend"]
    constraints = t.get("constraints")
    delay = int(max(0, delay_seconds))
    try:
        cls = _jclass(_TASK_SCHEDULER)
    except Exception:
        print("[task_manager] TaskScheduler 不可用:\n" + traceback.format_exc(limit=2))
        return False
    if backend == "alarm":
        try:
            cls.scheduleExact(task_id, delay)
            return True
        except Exception:
            print("[task_manager] Alarm 后端不可用，回退 WorkManager:\n"
                  + traceback.format_exc(limit=2))
    if constraints:
        try:
            cls.scheduleWithConstraints(task_id, delay,
                                        json.dumps(_json_safe(constraints)))
            return True
        except Exception:
            print("[task_manager] 带约束调度失败，回退普通 WorkManager:\n"
                  + traceback.format_exc(limit=2))
    try:
        cls.schedule(task_id, delay)
        return True
    except Exception:
        print("[task_manager] TaskScheduler 调度失败:\n"
              + traceback.format_exc(limit=2))
        return False


def _cancel(task_id):
    if _jclass is None:
        return False
    ok = False
    try:
        cls = _jclass(_TASK_SCHEDULER)
    except Exception:
        return False
    try:
        cls.cancel(task_id)
        ok = True
    except Exception:
        print(f"[task_manager] 取消排程 {task_id} 失败:\n"
              + traceback.format_exc(limit=2))
    try:
        cls.cancelExact(task_id)
        ok = True
    except Exception:
        pass
    return ok


def _arm(task_id, t, delay=None):
    """交给调度后端并记 next_epoch；成功才清 last_schedule_error。"""
    try:
        if delay is None:
            delay = _delay_of(t)
        delay = int(delay or 0)
        if delay <= 0:
            if _kind(t) == "at":
                t["enabled"] = False
                t["next_epoch"] = None
                t["last_status"] = "missed"
                t["last_result"] = "MISSED: 过期超过 misfire_grace"
            return False
        if _schedule(task_id, delay, t):
            t["next_epoch"] = int(round(_now() + delay))
            t.pop("last_schedule_error", None)
            t["sched_fail_count"] = 0
            return True
        t["last_schedule_error"] = "调度后端返回失败"
        t["sched_fail_count"] = int(t.get("sched_fail_count", 0) or 0) + 1
        COUNTERS["sched_fail"] += 1
        return False
    except Exception:
        print(f"[task_manager] 排程 {task_id} 失败:\n"
              + traceback.format_exc(limit=2))
        t["last_schedule_error"] = "排程异常"
        t["sched_fail_count"] = int(t.get("sched_fail_count", 0) or 0) + 1
        COUNTERS["sched_fail"] += 1
        return False


def reschedule_all():
    """改时/改时区/改后端后重算所有周期性任务。"""
    with _LOCK:
        for task_id, t in _STATE["tasks"].items():
            if not isinstance(t, dict) or not t.get("enabled", True):
                continue
            if _is_periodic(_kind(t)):
                _arm(task_id, t)
        _save()
    print("[task_manager] 已重排全部周期性任务")
    return {"ok": True}


# ============================ 系统事件订阅 ============================

_SYSTEM_MONITOR = "com.kingzcheung.xime.task.SystemEventMonitor"
_NETWORK_MONITOR = "com.kingzcheung.xime.task.NetworkEventMonitor"


def _subscribed_events():
    with _LOCK:
        names = set()
        for t in _STATE["tasks"].values():
            if not isinstance(t, dict) or not t.get("enabled", True):
                continue
            names.update(_system_events_of(t))
        return sorted(names)


def _rebuild_index():
    global _INDEX_DIRTY
    index = {}
    with _LOCK:
        for tid, t in _STATE["tasks"].items():
            if not isinstance(t, dict) or not t.get("enabled", True):
                continue
            for name in _system_events_of(t):
                index.setdefault(name, set()).add(tid)
    _EVENT_INDEX.clear()
    _EVENT_INDEX.update(index)
    _INDEX_DIRTY = False


def _sync_system_events():
    """把所有启用任务订阅的系统事件集合同步给 Kotlin SystemEventMonitor。"""
    global _INDEX_DIRTY
    _INDEX_DIRTY = True
    _rebuild_index()
    if _jclass is None:
        return
    try:
        names = _subscribed_events()
        for extra in ("time_changed", "timezone_changed"):
            if extra not in names:
                names.append(extra)
        _jclass(_SYSTEM_MONITOR).syncRegisteredEvents(json.dumps(names))
    except Exception:
        print("[task_manager] 系统事件订阅同步失败:\n"
              + traceback.format_exc(limit=2))


# ============================ 配置 RPC ============================

def _config_value(key, value):
    if key in ("min_interval", "history_len", "output_max", "queue_max",
               "workers", "dead_letter_max", "event_log_max", "metrics_events"):
        value = int(value)
    elif key in ("task_timeout", "lock_wait", "retry_max_delay", "watch_interval"):
        value = float(value)
    elif key == "backend":
        value = str(value)
    else:
        raise ValueError("未知配置项")
    if key == "min_interval" and value < 1:
        raise ValueError("min_interval 必须 >= 1")
    if key == "history_len" and not (1 <= value <= 500):
        raise ValueError("history_len 范围 1~500")
    if key == "output_max" and not (128 <= value <= 1048576):
        raise ValueError("output_max 范围 128~1048576")
    if key == "queue_max" and not (8 <= value <= 10000):
        raise ValueError("queue_max 范围 8~10000")
    if key == "workers" and not (1 <= value <= 16):
        raise ValueError("workers 范围 1~16")
    if key == "dead_letter_max" and not (1 <= value <= 1000):
        raise ValueError("dead_letter_max 范围 1~1000")
    if key == "event_log_max" and not (0 <= value <= 5000):
        raise ValueError("event_log_max 范围 0~5000")
    if key == "metrics_events" and value < 0:
        raise ValueError("metrics_events 不能为负")
    if key in ("task_timeout", "lock_wait", "metrics_events") and value < 0:
        raise ValueError("%s 不能为负" % key)
    if key == "retry_max_delay" and value < 1:
        raise ValueError("retry_max_delay 必须 >= 1")
    if key == "watch_interval" and not (0.2 <= value <= 10):
        raise ValueError("watch_interval 范围 0.2~10")
    if key == "backend" and value not in ("workmanager", "alarm"):
        raise ValueError("backend 只能是 workmanager 或 alarm")
    return value


def get_config():
    with _LOCK:
        out = dict(CONFIG)
    out["queue_size"] = _EVENT_Q.qsize()
    out["queue_max_actual"] = _EVENT_Q.maxsize
    return {"ok": True, "config": out}


def set_config(**kw):
    changes = {}
    try:
        for key, value in kw.items():
            if key not in CONFIG:
                return {"ok": False, "error": "未知配置项: %s" % key,
                        "allowed": sorted(CONFIG)}
            changes[key] = _config_value(key, value)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    with _LOCK:
        CONFIG.update(changes)
        if "queue_max" in changes:
            _EVENT_Q.maxsize = int(changes["queue_max"])
        _save()
    if "backend" in changes:
        reschedule_all()
    return {"ok": True, "changed": changes, "config": dict(CONFIG)}


# ============================ RPC：任务生命周期 ============================

ALLOWED = {
    "interval", "at", "repeat", "events", "debounce", "misfire_grace",
    "cron", "timeout", "retry", "retry_delay", "retry_max_delay",
    "priority", "group", "depends_on", "then", "coalesce", "coalesce_mode",
    "catchup", "when", "schema", "dedupe", "idem", "constraints",
    "backend", "tags", "template", "persist_events",
}


def _clean_id_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    out = []
    for item in value:
        if not isinstance(item, str) or not _TASK_ID_RE.match(item.strip()):
            return None
        item = item.strip()
        if item not in out:
            out.append(item)
    return out


def add_task(task_id, func_name=None, code=None, enabled=True, **opts):
    """新增/覆盖任务（唯一建任务入口）。opt 详见模块说明。"""
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        return {"ok": False,
                "error": "task_id 必须是 1~64 位字母/数字/_/-/. 组成的字符串",
                "task_id": task_id}
    tpl_name = opts.get("template")
    if tpl_name:
        tpl = _STATE.get("templates", {}).get(str(tpl_name))
        if not isinstance(tpl, dict):
            return {"ok": False, "error": "模板不存在: %s" % tpl_name,
                    "templates": sorted(_STATE.get("templates", {}))}
        merged = dict(tpl)
        merged.update({k: v for k, v in opts.items() if v is not None})
        if not code:
            code = merged.get("code")
        if not func_name:
            func_name = merged.get("func")
        # 模板里带的是任务定义字段（kind/code/func 等），只保留 add_task 认识的 opts
        opts = {k: v for k, v in merged.items() if k in ALLOWED}
        opts["template"] = tpl_name
    unknown = sorted(k for k in opts if k not in ALLOWED)
    if unknown:
        return {"ok": False, "error": "不支持的参数: %s" % unknown,
                "allowed": sorted(ALLOWED)}
    interval = opts.get("interval")
    at = opts.get("at")
    cron = opts.get("cron")
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
                "error": "events 不能使用伪事件名 %s（定时请用 interval/at/cron）"
                         % bad_events, "events": names}
    seconds = None
    if interval is not None:
        try:
            seconds = int(interval)
        except (TypeError, ValueError):
            return {"ok": False, "error": "interval 必须是整数秒", "interval": interval}
        if seconds < int(CONFIG["min_interval"]):
            return {"ok": False,
                    "error": "interval 必须 >= %d 秒" % CONFIG["min_interval"],
                    "interval": interval}
    cron_norm = None
    if cron is not None:
        try:
            _cron_parse(cron)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": "cron 非法: %s" % e, "cron": cron}
        if not _cron_next_epoch(cron):
            return {"ok": False, "error": "cron 永远不会命中", "cron": cron}
        cron_norm = " ".join(str(cron).split())
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
    elif cron_norm is not None:
        kind = "cron"
    elif at_norm is not None:
        kind = "daily" if repeat else "at"
    elif names:
        kind = "event"
    else:
        return {"ok": False,
                "error": "需要 interval / cron / at / events 至少一个",
                "interval": interval, "cron": cron, "at": at, "events": events}
    if kind != "event" and not names:
        names = []
    if func_name is not None and not isinstance(func_name, str):
        return {"ok": False, "error": "func_name 必须是字符串"}
    if code is not None and not isinstance(code, str):
        return {"ok": False, "error": "code 必须是字符串"}
    if code and len(code) > 65536:
        return {"ok": False, "error": "code 超过 64KB 上限", "bytes": len(code)}
    if not code and func_name not in JOB_FUNCS:
        return {"ok": False, "error": "必须提供 code，或使用已注册 func",
                "funcs": sorted(JOB_FUNCS)}
    if code:
        try:
            compile(code, "<task:%s>" % task_id, "exec")
        except SyntaxError as e:
            return {"ok": False, "error": "code 语法错误: %s" % e,
                    "line": e.lineno}
    try:
        gap = float(debounce or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "debounce 必须是数字（秒）"}
    if gap < 0:
        return {"ok": False, "error": "debounce 不能为负"}
    try:
        grace = float(opts.get("misfire_grace", 60) or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "misfire_grace 必须是数字（秒）"}
    if grace < 0:
        return {"ok": False, "error": "misfire_grace 不能为负"}
    try:
        timeout = float(opts.get("timeout", 0) or 0)
        if timeout < 0 or timeout > 86400:
            return {"ok": False, "error": "timeout 范围 0~86400 秒"}
        retry = int(opts.get("retry", 0) or 0)
        if not (0 <= retry <= 10):
            return {"ok": False, "error": "retry 范围 0~10"}
        retry_delay = float(opts.get("retry_delay", 5) or 0)
        if retry_delay < 0:
            return {"ok": False, "error": "retry_delay 不能为负"}
        priority = int(opts.get("priority", 0) or 0)
        if not (-1000 <= priority <= 1000):
            return {"ok": False, "error": "priority 范围 -1000~1000"}
        dedupe = float(opts.get("dedupe", 0) or 0)
        if dedupe < 0:
            return {"ok": False, "error": "dedupe 不能为负"}
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout/retry/retry_delay/priority/dedupe 必须是数字"}
    group = opts.get("group")
    if group is not None and (not isinstance(group, str) or len(group) > 64):
        return {"ok": False, "error": "group 必须是不超过 64 字符的字符串"}
    depends = _clean_id_list(opts.get("depends_on"))
    then = _clean_id_list(opts.get("then"))
    if depends is None or then is None:
        return {"ok": False, "error": "depends_on/then 必须是任务 id 列表"}
    if task_id in depends or task_id in then:
        return {"ok": False, "error": "depends_on/then 不能包含自身"}
    tags = opts.get("tags")
    if tags is not None and (not isinstance(tags, (list, tuple))
                             or any(not isinstance(x, str) for x in tags)):
        return {"ok": False, "error": "tags 必须是字符串列表"}
    when = opts.get("when")
    schema = opts.get("schema")
    constraints = opts.get("constraints")
    for key, val in (("when", when), ("schema", schema), ("constraints", constraints)):
        if val is not None and not isinstance(val, dict):
            return {"ok": False, "error": "%s 必须是字典" % key}
    mode = opts.get("coalesce_mode")
    if mode is not None and mode not in ("latest", "count"):
        return {"ok": False, "error": "coalesce_mode 只能是 latest/count"}
    backend = opts.get("backend")
    if backend is not None and backend not in ("workmanager", "alarm"):
        return {"ok": False, "error": "backend 只能是 workmanager/alarm"}
    idem = opts.get("idem")
    if idem is not None and (not isinstance(idem, str) or len(idem) > 64):
        return {"ok": False, "error": "idem 必须是不超过 64 字符的字符串"}
    with _LOCK:
        old = _STATE["tasks"].get(task_id)
        gen = int((old or {}).get("gen", 0) or 0) + 1
        version = int((old or {}).get("version", 0) or 0) + 1
        t = {
            "gen": gen,
            "version": version,
            "kind": kind,
            "events": names,
            "func": func_name,
            "code": code,
            "debounce": gap,
            "misfire_grace": grace,
            "timeout": timeout,
            "retry": retry,
            "retry_delay": retry_delay,
            "retry_max_delay": float(opts.get("retry_max_delay",
                                               CONFIG["retry_max_delay"])
                                     or CONFIG["retry_max_delay"]),
            "priority": priority,
            "group": group,
            "depends_on": depends,
            "then": then,
            "tags": list(tags or []),
            "when": when,
            "schema": schema,
            "constraints": constraints,
            "backend": backend,
            "dedupe": dedupe,
            "idem": idem,
            "catchup": bool(opts.get("catchup", False)),
            "persist_events": bool(opts.get("persist_events", False)),
            "template": tpl_name,
            "enabled": bool(enabled),
            "created": (old or {}).get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_count": 0,
            "fail_count": 0,
            "timeout_count": 0,
            "skip_count": 0,
            "consecutive_failures": 0,
            "last_run": None,
            "last_result": None,
            "last_status": None,
            "last_error": None,
            "last_event": None,
            "last_payload": None,
            "last_duration_ms": None,
            "last_attempt": 0,
            "next_epoch": None,
            "last_schedule_error": None,
            "sched_fail_count": 0,
            "total_ms": 0,
        }
        coalesce = opts.get("coalesce")
        if coalesce is None:
            coalesce = kind in ("interval", "daily", "cron")
        t["coalesce"] = bool(coalesce)
        t["coalesce_mode"] = mode or ("latest" if coalesce else None)
        if names:
            t["event"] = names[0]
        if seconds is not None:
            t["interval"] = seconds
        if cron_norm is not None:
            t["cron"] = cron_norm
        if at_norm is not None:
            t["at"] = at_norm
            t["next_epoch"] = (int(round(_now() + _daily_delay(t)))
                               if kind == "daily" else int(at_epoch))
        _STATE["tasks"][task_id] = t
        delay = None
        if enabled and _is_periodic(kind):
            delay = _delay_of(t)
            _arm(task_id, t, delay)
        elif old is not None:
            _cancel(task_id)
        _PENDING.pop(task_id, None)
        _save()
    _sync_system_events()
    print(f"[task_manager] add {kind} {task_id} interval={seconds} "
          f"cron={cron_norm} at={at_norm} events={names} debounce={gap}s "
          f"priority={priority} enabled={enabled}")
    return {"ok": True, "id": task_id, "kind": kind, "version": version,
            "interval": seconds, "cron": cron_norm, "at": at_norm,
            "next_epoch": t.get("next_epoch"), "next_in": delay,
            "events": names, "event": names[0] if names else None}


def add_event_task(task_id, event=None, code=None, func_name=None,
                   debounce=0.0, enabled=True, events=None,
                   interval=None, at=None):
    """兼容包装：等价 add_task(..., events=...)。"""
    names = _norm_events(event, events)
    if names is None:
        return {"ok": False, "error": "event/events 必须是字符串或字符串列表"}
    return add_task(task_id, func_name=func_name, interval=interval, code=code,
                    enabled=enabled, events=names, at=at, debounce=debounce)


def add_at_task(task_id, at, code=None, func_name=None, enabled=True,
                events=None, interval=None):
    """兼容包装：等价 add_task(..., at=at)。"""
    return add_task(task_id, func_name=func_name, interval=interval, code=code,
                    enabled=enabled, events=events, at=at)


def add_daily_task(task_id, at="00:00", code=None, func_name=None, enabled=True):
    """兼容包装：等价 add_task(..., at=at, repeat=True)。"""
    return add_task(task_id, func_name=func_name, code=code, enabled=enabled,
                    at=at, repeat=True)


def remove_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].pop(task_id, None)
        existed = t is not None
        _PENDING.pop(task_id, None)
        _TASK_LOCKS.pop(task_id, None)
        _CODE_CACHE.pop(task_id, None)
        _HISTORY["tasks"].pop(task_id, None)
        _save()
        _save_history()
    if existed:
        _cancel(task_id)
        _sync_system_events()
    return {"ok": existed}


del_task = remove_task


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
            _arm(task_id, t)
        _save()
    _sync_system_events()
    return {"ok": True, "kind": kind,
            "event": events[0] if events else None, "events": events}


def batch_tasks(action, ids=None, group=None):
    """批量操作：action=enable|disable|remove|run；可用 ids 或 group 选择。"""
    action = str(action or "").lower()
    if action not in ("enable", "disable", "remove", "run"):
        return {"ok": False, "error": "action 只能是 enable/disable/remove/run"}
    with _LOCK:
        if isinstance(ids, str):
            ids = [ids]
        targets = list(ids or [])
        if not targets and group is not None:
            targets = [tid for tid, t in _STATE["tasks"].items()
                       if isinstance(t, dict) and t.get("group") == group]
        targets = [str(x) for x in targets if str(x) in _STATE["tasks"]]
    results = {}
    for tid in targets:
        try:
            if action == "enable":
                results[tid] = resume_task(tid)
            elif action == "disable":
                results[tid] = pause_task(tid)
            elif action == "remove":
                results[tid] = remove_task(tid)
            else:
                results[tid] = run_now(tid)
        except Exception:
            results[tid] = {"ok": False, "error": traceback.format_exc(limit=2)}
    return {"ok": True, "action": action, "count": len(targets), "results": results}


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
                    {"task_id": task_id, "source": "run_now"},
                    source="run_now", mode="manual")
    r["kind"] = kind
    return r


def cancel_running(task_id):
    """请求取消正在执行的任务（协作式：任务代码可读 cancel_event）。"""
    with _LOCK:
        ev = _CANCEL.get(task_id)
        running = task_id in _RUNNING
    if ev is not None:
        ev.set()
    return {"ok": bool(running), "task_id": task_id, "running": running}


cancel_task = cancel_running


# ============================ 查询 / 导出 ============================

def _task_summary(tid, t, include_code=False):
    out = {
        "id": tid, "kind": _kind(t), "gen": int(t.get("gen", 0) or 0),
        "version": int(t.get("version", 1) or 1),
        "enabled": bool(t.get("enabled", True)),
        "group": t.get("group"), "priority": t.get("priority", 0),
        "interval": t.get("interval"), "at": t.get("at"),
        "cron": t.get("cron"), "events": _events_of(t),
        "timeout": t.get("timeout"), "retry": t.get("retry"),
        "depends_on": t.get("depends_on") or [],
        "then": t.get("then") or [],
        "run_count": int(t.get("run_count", 0) or 0),
        "fail_count": int(t.get("fail_count", 0) or 0),
        "timeout_count": int(t.get("timeout_count", 0) or 0),
        "last_run": t.get("last_run"),
        "last_status": t.get("last_status"),
        "last_duration_ms": t.get("last_duration_ms"),
        "next_epoch": t.get("next_epoch"),
        "next_time": (time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(int(t["next_epoch"])))
                      if t.get("next_epoch") else None),
        "last_schedule_error": t.get("last_schedule_error"),
        "consecutive_failures": int(t.get("consecutive_failures", 0) or 0),
        "code_bytes": len(t.get("code") or ""),
        "func": t.get("func"),
    }
    if include_code:
        out["code"] = t.get("code")
    return out


def list_tasks(page=1, page_size=0, group=None, enabled=None,
               include_code=False, detail=False):
    """任务列表（默认摘要，不回传全量 code）。

    page_size<=0 返回全部（保持旧调用兼容）；否则返回分页结构
    {page,page_size,total,tasks}。include_code=True 才带 code。
    """
    with _LOCK:
        items = []
        for tid, t in _STATE["tasks"].items():
            if not isinstance(t, dict):
                continue
            if group is not None and t.get("group") != group:
                continue
            if enabled is not None and bool(t.get("enabled", True)) != bool(enabled):
                continue
            items.append((tid, t))
        items.sort(key=lambda kv: (-int(kv[1].get("priority", 0) or 0), kv[0]))
        total = len(items)
        try:
            page = max(1, int(page))
            page_size = max(0, int(page_size))
        except (TypeError, ValueError):
            page, page_size = 1, 0
        if page_size:
            start = (page - 1) * page_size
            items = items[start:start + page_size]
        snap = {}
        for tid, t in items:
            if detail:
                try:
                    snap[tid] = json.loads(json.dumps(t, ensure_ascii=False))
                except (TypeError, ValueError):
                    snap[tid] = {k: _json_safe(v) for k, v in t.items()}
                if not include_code:
                    snap[tid].pop("code", None)
                    snap[tid]["code_bytes"] = len(t.get("code") or "")
            else:
                snap[tid] = _task_summary(tid, t, include_code)
    if page_size:
        return {"ok": True, "page": page, "page_size": page_size,
                "total": total, "count": len(snap), "tasks": snap}
    return snap


def get_task(task_id, include_code=True):
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not isinstance(t, dict):
            return {"ok": False, "error": "not found", "task_id": task_id}
        try:
            out = json.loads(json.dumps(t, ensure_ascii=False))
        except (TypeError, ValueError):
            out = {k: _json_safe(v) for k, v in t.items()}
        if not include_code:
            out.pop("code", None)
            out["code_bytes"] = len(t.get("code") or "")
    return {"ok": True, "task_id": task_id, "task": out}


def list_groups():
    with _LOCK:
        groups = {}
        for t in _STATE["tasks"].values():
            if not isinstance(t, dict):
                continue
            name = t.get("group") or ""
            g = groups.setdefault(name, {"group": name, "total": 0, "enabled": 0})
            g["total"] += 1
            if t.get("enabled", True):
                g["enabled"] += 1
    return {"ok": True, "groups": sorted(groups.values(), key=lambda g: g["group"])}


def list_events():
    return list(EVENTS)


def export_tasks(ids=None, include_code=True):
    """导出任务定义（可原样传给 import_tasks）。"""
    with _LOCK:
        out = {}
        for tid, t in _STATE["tasks"].items():
            if ids and tid not in ids:
                continue
            if not isinstance(t, dict):
                continue
            item = {k: _json_copy(v) for k, v in t.items()
                    if k not in RUNTIME_KEYS and k not in ("_last_fire_epoch",)}
            if not include_code:
                item.pop("code", None)
            out[tid] = item
        return {"ok": True, "version": STATE_VERSION,
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(out), "tasks": out}


def import_tasks(data, overwrite=False, enabled=None):
    """导入 export_tasks 结构；overwrite=False 时跳过已存在任务。"""
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, dict):
        return {"ok": False, "error": "数据格式错误，缺少 tasks 字典"}
    added, skipped, failed = [], [], {}
    for tid, spec in tasks.items():
        if not isinstance(tid, str) or not _TASK_ID_RE.match(tid):
            failed[str(tid)] = "非法 id"
            continue
        with _LOCK:
            exists = tid in _STATE["tasks"]
        if exists and not overwrite:
            skipped.append(tid)
            continue
        if not isinstance(spec, dict):
            failed[tid] = "定义不是字典"
            continue
        try:
            opts = {k: v for k, v in spec.items()
                    if k in ALLOWED and v is not None}
            opts.pop("template", None)
            code = spec.get("code")
            func_name = spec.get("func")
            en = spec.get("enabled", True) if enabled is None else enabled
            r = add_task(tid, func_name=func_name, code=code, enabled=en, **opts)
            if r.get("ok"):
                added.append(tid)
            else:
                failed[tid] = r.get("error")
        except Exception:
            failed[tid] = traceback.format_exc(limit=2)
    return {"ok": not failed, "added": added, "skipped": skipped,
            "failed": failed, "count": len(added)}


# ============================ 模板 ============================

def save_template(name, task_id=None, definition=None):
    """把一个任务（或显式定义）存为模板。"""
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        return {"ok": False, "error": "模板名必须是非空字符串（<=64）"}
    if definition is None:
        with _LOCK:
            t = _STATE["tasks"].get(task_id)
            if not isinstance(t, dict):
                return {"ok": False, "error": "任务不存在: %s" % task_id}
            definition = {k: _json_copy(v) for k, v in t.items()
                          if k not in RUNTIME_KEYS
                          and k not in ("gen", "version", "created", "updated",
                                        "enabled", "next_epoch")}
    if not isinstance(definition, dict):
        return {"ok": False, "error": "definition 必须是字典"}
    with _LOCK:
        _STATE["templates"][name] = dict(definition)
        _save()
    return {"ok": True, "name": name, "template": definition}


def list_templates():
    with _LOCK:
        return {"ok": True, "templates": sorted(_STATE.get("templates", {}))}


def delete_template(name):
    with _LOCK:
        existed = _STATE.get("templates", {}).pop(name, None) is not None
        _save()
    return {"ok": existed}


# ============================ 事件匹配 ============================

_DEDUPE = {}   # tid -> {key: ts}


def _path_get(obj, path):
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _cmp_value(got, want):
    try:
        if isinstance(want, dict) and "op" in want:
            op = str(want.get("op"))
            val = want.get("value")
            if op == "exists":
                return got is not None
            if got is None:
                return False
            if op == "eq":
                return got == val
            if op == "ne":
                return got != val
            if op == "in":
                return got in (val or [])
            if op == "contains":
                return val in got
            if op == "regex":
                return re.search(str(val), str(got)) is not None
            if op == "gt":
                return got > val
            if op == "gte":
                return got >= val
            if op == "lt":
                return got < val
            if op == "lte":
                return got <= val
            return False
        if isinstance(want, (list, tuple, set)):
            return got in want
        return got == want
    except Exception:
        return False


def _match_when(t, payload):
    spec = t.get("when")
    if not spec:
        return True
    data = payload if isinstance(payload, dict) else {}
    for key, want in spec.items():
        if not _cmp_value(_path_get(data, key), want):
            return False
    return True

_TYPE_MAP = {
    "str": str, "string": str, "int": int, "float": (int, float),
    "num": (int, float), "number": (int, float), "bool": bool,
    "list": list, "dict": dict, "any": object,
}


def _schema_ok(t, payload):
    spec = t.get("schema")
    if not spec:
        return True, None
    data = payload if isinstance(payload, dict) else {}
    for field, rule in spec.items():
        got = _path_get(data, field)
        required = True
        type_name = None
        if isinstance(rule, dict):
            required = bool(rule.get("required", True))
            type_name = rule.get("type")
        elif isinstance(rule, str):
            type_name = rule
        if got is None:
            if required:
                return False, "缺少字段 %s" % field
            continue
        if type_name:
            py = _TYPE_MAP.get(str(type_name).lower())
            if py is not None and not isinstance(got, py):
                return False, "字段 %s 类型应为 %s" % (field, type_name)
    return True, None


def _dedupe_ok(t, tid, payload, ts):
    window = float(t.get("dedupe", 0) or 0)
    if window <= 0:
        return True
    name = t.get("idem")
    data = payload if isinstance(payload, dict) else {}
    if name:
        key = data.get(name)
        if key is None:
            return True
        key = str(key)
    else:
        try:
            key = json.dumps(_brief(payload), sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            key = repr(_brief(payload))
    with _LOCK:
        cache = _DEDUPE.setdefault(tid, {})
        for old in [k for k, v in cache.items() if ts - v >= window]:
            cache.pop(old, None)
        last = cache.get(key)
        if last is not None and ts - last < window:
            COUNTERS["deduped"] += 1
            return False
        cache[key] = ts
        if len(cache) > 256:
            for old in list(cache)[:128]:
                cache.pop(old, None)
    return True


def _deps_ok(t):
    deps = t.get("depends_on") or []
    for dep in deps:
        with _LOCK:
            d = _STATE["tasks"].get(dep)
        if not isinstance(d, dict):
            return False, "dependency_missing:%s" % dep
        if d.get("last_status") != "ok":
            return False, "dependency_failed:%s" % dep
    return True, None


# ============================ 事件入口 ============================

def fire_event(event_name, payload=None, source="system", record=True,
               event_ts=None, persist=None):
    """Kotlin 事件回调入口，也可被 RPC 模拟；必须快速返回。"""
    global _BOOT_DONE, _EVENT_SEQ
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else None
        except (ValueError, AttributeError):
            payload = {"raw": payload}
    if not isinstance(payload, dict):
        payload = None if payload is None else {"value": payload}
    if event_name == "boot_completed":
        if _BOOT_DONE:
            return {"ok": True, "event": event_name, "matched": [],
                    "duplicate": True}
        _BOOT_DONE = True
    if event_name in ("time_changed", "timezone_changed"):
        reschedule_all()
    rec = None
    if record:
        with _LOCK:
            _EVENT_SEQ += 1
            rec = {"seq": _EVENT_SEQ, "ts": time.strftime("%H:%M:%S"),
                   "epoch": round(_now(), 3), "event": str(event_name),
                   "source": source, "payload": _brief(payload)}
    with _LOCK:
        if record:
            _RECENT_EVENTS.append(rec)
            if len(_RECENT_EVENTS) > _RECENT_MAX:
                del _RECENT_EVENTS[:-_RECENT_MAX]
        if not _LOADED:
            _PRE_INIT_EVENTS.append((str(event_name), payload, source))
            if len(_PRE_INIT_EVENTS) > _PRE_INIT_MAX:
                del _PRE_INIT_EVENTS[:-_PRE_INIT_MAX]
            matches = []
        else:
            if _INDEX_DIRTY:
                _rebuild_index()
            matches = sorted(_EVENT_INDEX.get(str(event_name), ()))
    should_persist = persist
    if should_persist is None:
        should_persist = any(
            isinstance(_STATE["tasks"].get(tid), dict)
            and _STATE["tasks"][tid].get("persist_events") for tid in matches)
    for tid in matches:
        trigger(tid, event_name, payload, source=source, event_ts=event_ts)
    if should_persist:
        with _LOCK:
            arr = _HISTORY.setdefault("events", [])
            arr.append({"seq": _EVENT_SEQ, "epoch": round(_now(), 3),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "event": str(event_name), "source": source,
                        "payload": _brief(payload)})
            cap = int(CONFIG["event_log_max"] or 0)
            if cap and len(arr) > cap:
                del arr[:-cap]
        save_soft()
    if record:
        print(f"[task_manager] event={event_name} source={source} "
              f"payload={rec['payload']} matched={matches}")
    return {"ok": True, "event": event_name, "matched": matches,
            "queue": _EVENT_Q.qsize()}


def _enqueue(item):
    global _RUN_DROPPED
    pri = int(item.get("priority", 0) or 0)
    try:
        _EVENT_Q.put_nowait((-pri, _next_seq(), item))
    except queue.Full:
        _RUN_DROPPED += 1
        COUNTERS["dropped"] += 1
        print(f"[task_manager] 触发队列已满，丢弃 {item.get('task_id')}/"
              f"{item.get('event')}")
        return False
    _ensure_workers()
    return True


def trigger(task_id, event, payload=None, source="system", event_ts=None,
            mode="normal"):
    """统一触发入口：WorkManager / 系统事件 / RPC / 重试 / 链式都走这里。"""
    task_id = str(task_id)
    now = _now()
    ts = now if event_ts is None else float(event_ts)
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if t is None:
            return {"ok": False, "task_id": task_id, "reason": "not_found"}
        if not t.get("enabled", True) and mode != "manual":
            return {"ok": False, "task_id": task_id, "reason": "disabled"}
        if (event not in PSEUDO_EVENTS and event not in _system_events_of(t)
                and mode not in ("manual", "chain", "retry", "catchup")):
            return {"ok": False, "task_id": task_id, "reason": "not_subscribed"}
        if not _match_when(t, payload):
            return {"ok": False, "task_id": task_id, "reason": "filtered"}
        ok, why = _schema_ok(t, payload)
        if not ok:
            t["last_skip_reason"] = "schema:%s" % why
            COUNTERS["skip"] += 1
            return {"ok": False, "task_id": task_id, "reason": "schema",
                    "detail": why}
        if not _dedupe_ok(t, task_id, payload, ts):
            return {"ok": False, "task_id": task_id, "reason": "duplicate"}
        gap = float(t.get("debounce", 0.0) or 0.0)
        last = float(t.get("_last_fire_epoch", 0.0) or 0.0)
        periodic = event in PSEUDO_EVENTS and event == _pseudo_event(_kind(t))
        throttled_modes = ("manual", "retry", "catchup", "chain")
        if (mode not in throttled_modes and gap > 0 and ts - last < gap):
            if periodic and event in ("interval", "daily", "cron"):
                _arm(task_id, t, _delay_of(t))
            save_soft()
            return {"ok": False, "task_id": task_id, "reason": "debounced",
                    "gap": gap}
        t["_last_fire_epoch"] = ts
        if periodic and event in ("interval", "daily", "cron") and mode != "manual":
            _arm(task_id, t, _delay_of(t))
        gen = int(t.get("gen", 0) or 0)
        coalesce = bool(t.get("coalesce_mode")) or bool(t.get("coalesce"))
        if coalesce and mode == "normal":
            pend = _PENDING.get(task_id)
            if pend is not None:
                pend["event"] = event
                pend["payload"] = payload
                pend["count"] = int(pend.get("count", 1)) + 1
                pend["ts"] = ts
                COUNTERS["coalesced"] += 1
                save_soft()
                return {"ok": True, "task_id": task_id, "event": event,
                        "reason": "coalesced", "count": pend["count"]}
            _PENDING[task_id] = {"event": event, "payload": payload,
                                 "ts": ts, "count": 1}
        item = {
            "task_id": task_id, "gen": gen, "event": event,
            "payload": payload, "source": source, "ts": ts, "mode": mode,
            "attempt": 0,
            "priority": int(t.get("priority", 0) or 0),
            "coalesced": coalesce,
        }
    save_soft()
    if not _enqueue(item):
        return {"ok": False, "task_id": task_id, "reason": "queue_full"}
    return {"ok": True, "task_id": task_id, "event": event,
            "queue": _EVENT_Q.qsize()}


def test_event(event, payload=None):
    fire_event(event, payload, source="rpc_test")
    return {"ok": True, "event": event, "queued": _EVENT_Q.qsize()}


def is_initialized():
    return _STATE_PATH is not None


# ============================ 分发 / 执行 ============================

def _ensure_workers():
    global _WORKER_ON, _WATCHDOG_ON
    if _STOP.is_set():
        return
    if not (_WORKER_ON and _WATCHDOG_ON):
        with _LOCK:
            if not _WORKER_ON:
                threading.Thread(target=_event_worker, daemon=True,
                                 name="PyTaskEvent").start()
                _WORKER_ON = True
            if not _WATCHDOG_ON:
                threading.Thread(target=_watchdog, daemon=True,
                                 name="PyTaskWatchdog").start()
                _WATCHDOG_ON = True


def _pool():
    global _RUN_POOL
    with _LOCK:
        if _RUN_POOL is None:
            _RUN_POOL = ThreadPoolExecutor(
                max_workers=int(CONFIG["workers"]), thread_name_prefix="PyTaskWork")
        return _RUN_POOL


def _event_worker():
    while not _STOP.is_set():
        try:
            _pri, _seq, item = _EVENT_Q.get(timeout=1.0)
        except queue.Empty:
            continue
        except BaseException:
            continue
        try:
            _dispatch(item)
        except BaseException:
            print(f"[task_manager] 事件分发异常:\n{traceback.format_exc(limit=3)}")


def _dispatch(item):
    if _STOP.is_set():
        return
    tid = item["task_id"]
    with _LOCK:
        t = _STATE["tasks"].get(tid)
    if not isinstance(t, dict):
        return
    if int(t.get("gen", 0) or 0) != item["gen"]:
        return
    if not t.get("enabled", True) and item["mode"] != "manual":
        return
    ev = item["event"]
    if (ev not in PSEUDO_EVENTS and ev not in _system_events_of(t)
            and item["mode"] not in ("manual", "chain", "retry", "catchup")):
        return
    if item.get("coalesced"):
        with _LOCK:
            pend = _PENDING.pop(tid, None)
        if pend:
            item["payload"] = pend.get("payload")
            item["event"] = pend.get("event") or ev
            item["count"] = int(pend.get("count", 1))
        if int(item.get("count", 1) or 1) > 1:
            payload = item.get("payload")
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["_coalesced"] = item["count"]
            else:
                payload = {"_coalesced": item["count"], "value": payload}
            item["payload"] = payload
    ok, why = _deps_ok(t)
    if not ok:
        _record_skip(t, tid, why)
        return
    try:
        _pool().submit(_run_guarded, item)
    except Exception:
        print(f"[task_manager] 提交执行失败:\n{traceback.format_exc(limit=2)}")


def _record_skip(t, tid, why):
    if isinstance(t, dict):
        t["skip_count"] = int(t.get("skip_count", 0) or 0) + 1
        t["last_skip_reason"] = why
    COUNTERS["skip"] += 1
    print(f"[task_manager] skip {tid}: {why}")


def _run_guarded(item):
    if _STOP.is_set():
        return
    tid = item["task_id"]
    with _LOCK:
        t = _STATE["tasks"].get(tid)
    if not isinstance(t, dict) or int(t.get("gen", 0) or 0) != item["gen"]:
        return
    if not t.get("enabled", True) and item["mode"] != "manual":
        return
    lock = _task_lock(tid)
    try:
        got = lock.acquire(timeout=float(CONFIG["lock_wait"] or 0))
    except TypeError:
        got = lock.acquire(False)
    if not got:
        _record_skip(t, tid, "busy")
        return
    cancel = threading.Event()
    state = {"timed_out": False}
    timeout = float(t.get("timeout") or CONFIG["task_timeout"] or 0)
    timer = None
    start = _now()
    with _LOCK:
        _CANCEL[tid] = cancel
        _RUNNING[tid] = {"start": start, "event": item["event"],
                         "attempt": item.get("attempt", 0), "timed_out": False}
    if timeout > 0:
        timer = threading.Timer(timeout, _on_timeout, args=(tid, item, state, cancel))
        timer.daemon = True
        timer.start()
    status, result, error = "ok", None, None
    try:
        if cancel.is_set():
            status = "cancelled"
        else:
            result = _execute_once(t, tid, item["event"], item["payload"], cancel)
            if cancel.is_set():
                status = "cancelled"
    except SystemExit as e:
        status, error = "error", "SystemExit: %r" % (getattr(e, "code", None),)
    except BaseException:
        status, error = "error", traceback.format_exc(limit=5)
    finally:
        dur = int((_now() - start) * 1000)
        if timer is not None:
            timer.cancel()
        with _LOCK:
            _RUNNING.pop(tid, None)
            _CANCEL.pop(tid, None)
        lock.release()
    if state["timed_out"]:
        print(f"[task_manager] task {tid} 超时后仍返回，结果丢弃 "
              f"status={status} dur={dur}ms")
        return
    _finalize(tid, item, t, status, result, error, dur)


def _on_timeout(tid, item, state, cancel):
    if _STOP.is_set() or state.get("timed_out"):
        state["timed_out"] = True
        cancel.set()
        return
    state["timed_out"] = True
    cancel.set()
    with _LOCK:
        t = _STATE["tasks"].get(tid)
        if isinstance(t, dict) and int(t.get("gen", 0) or 0) == item["gen"]:
            t["timeout_count"] = int(t.get("timeout_count", 0) or 0) + 1
            t["last_status"] = "timeout"
            t["last_event"] = item["event"]
            t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            t["last_error"] = "timeout after %ss" % (t.get("timeout") or CONFIG["task_timeout"])
            t["consecutive_failures"] = int(t.get("consecutive_failures", 0) or 0) + 1
        running = _RUNNING.get(tid)
        if running is not None:
            running["timed_out"] = True
    COUNTERS["timeout"] += 1
    _record_history(tid, {
        "ts": int(_now()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": item["event"], "status": "timeout",
        "attempt": item.get("attempt", 0), "duration_ms": None,
        "output": None, "error": "执行超时",
    })
    _alert("timeout", f"任务 {tid} 执行超时（{item['event']}）", tid)
    save_soft()


def _execute_once(t, tid, event, payload, cancel):
    code = t.get("code")
    if code:
        captured = _run_code(code, event, payload, tid, cancel)
        return captured or "(无输出)"
    func = JOB_FUNCS.get(t.get("func"))
    if func is None:
        raise RuntimeError(f"函数 {t.get('func')} 未注册")
    return func()


def _compiled(code, tid, gen):
    key = hash(code)
    cached = _CODE_CACHE.get(tid)
    if cached and cached[0] == gen and cached[1] == key:
        return cached[2]
    obj = compile(code, f"<task:{tid}>", "exec")
    _CODE_CACHE[tid] = (gen, key, obj)
    if len(_CODE_CACHE) > 256:
        _CODE_CACHE.clear()
    return obj


def _run_code(code, event_name, payload, task_id, cancel_event=None):
    """执行任务源码；print 同时进日志窗口和捕获缓冲（有上限）。"""
    real_stdout = sys.stdout
    buf = io.StringIO()
    limit = int(CONFIG["output_max"])
    state = {"truncated": False}

    def tprint(*args, **kwargs):
        do_flush = kwargs.pop("flush", False)
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        msg = sep.join(str(a) for a in args) + end
        try:
            real_stdout.write(msg)
            if do_flush:
                real_stdout.flush()
        except Exception:
            pass
        if not state["truncated"]:
            if buf.tell() + len(msg) > limit:
                buf.write(msg[:max(0, limit - buf.tell())])
                buf.write("...<output truncated>")
                state["truncated"] = True
            else:
                buf.write(msg)

    ns = {
        "__builtins__": __builtins__,
        "print": tprint,
        "event": event_name,
        "payload": payload,
        "task_id": task_id,
        "cancel_event": cancel_event or threading.Event(),
        "config": dict(CONFIG),
    }
    if _jclass is not None:
        ns["jclass"] = _jclass
    with _LOCK:
        gen = int((_STATE["tasks"].get(task_id) or {}).get("gen", 0) or 0)
    exec(_compiled(code, task_id, gen), ns)
    return buf.getvalue().strip()


def _record_history(tid, entry):
    global _HIST_DIRTY
    with _LOCK:
        arr = _HISTORY.setdefault("tasks", {}).setdefault(tid, [])
        arr.append(entry)
        cap = int(CONFIG["history_len"])
        if len(arr) > cap:
            del arr[:-cap]
        _HIST_DIRTY = True


def _alert(kind, message, task_id=None):
    global _HIST_DIRTY
    with _LOCK:
        arr = _HISTORY.setdefault("alerts", [])
        arr.append({"ts": int(_now()), "kind": kind, "task_id": task_id,
                    "message": _truncate(message, 300)})
        if len(arr) > 200:
            del arr[:-200]
        _HIST_DIRTY = True


def _dead_letter(tid, item, attempts, error):
    with _LOCK:
        arr = _HISTORY.setdefault("dead_letters", [])
        arr.append({"ts": int(_now()),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "task_id": tid, "event": item.get("event"),
                    "source": item.get("source"), "attempts": attempts,
                    "error": _truncate(error, 500),
                    "payload": _brief(item.get("payload"))})
        cap = int(CONFIG["dead_letter_max"])
        if len(arr) > cap:
            del arr[:-cap]
    COUNTERS["dead"] += 1
    _alert("dead_letter", f"任务 {tid} 重试耗尽进入死信", tid)
    _save_history()


def _finalize(tid, item, t, status, result, error, dur):
    if _STOP.is_set():
        return
    with _LOCK:
        cur = _STATE["tasks"].get(tid)
        if cur is not t or int(t.get("gen", 0) or 0) != item["gen"]:
            print(f"[task_manager] task {tid} 执行期间已被删除/覆盖，丢弃结果")
            return
        output = _truncate(result, int(CONFIG["output_max"])) if result is not None else None
        t["last_duration_ms"] = dur
        t["last_attempt"] = item.get("attempt", 0)
        t["last_event"] = item["event"]
        t["last_payload"] = _brief(item.get("payload"))
        if status == "ok":
            t["run_count"] = int(t.get("run_count", 0) or 0) + 1
            t["last_status"] = "ok"
            t["last_result"] = output or "(无输出)"
            t["last_error"] = None
            t["consecutive_failures"] = 0
            t["total_ms"] = int(t.get("total_ms", 0) or 0) + dur
            t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            COUNTERS["ok"] += 1
        elif status == "cancelled":
            t["last_status"] = "cancelled"
            t["skip_count"] = int(t.get("skip_count", 0) or 0) + 1
            t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            COUNTERS["skip"] += 1
        else:
            t["run_count"] = int(t.get("run_count", 0) or 0) + 1
            t["fail_count"] = int(t.get("fail_count", 0) or 0) + 1
            t["consecutive_failures"] = int(t.get("consecutive_failures", 0) or 0) + 1
            t["last_status"] = "error"
            t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            last_line = (error or "").strip().splitlines()
            t["last_error"] = _truncate(last_line[-1] if last_line else "error", 300)
            t["last_result"] = "ERROR: " + t["last_error"]
            COUNTERS["fail"] += 1
        COUNTERS["runs"] += 1
        one_shot = (_kind(t) == "at" and item["event"] == "at"
                    and item["mode"] != "manual")
        if one_shot and status == "ok":
            t["enabled"] = False
            t["next_epoch"] = None
    _record_history(tid, {
        "ts": int(_now()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": item["event"], "status": status,
        "attempt": item.get("attempt", 0), "duration_ms": dur,
        "output": _truncate(result, 512) if result is not None else None,
        "error": _truncate(error, 512) if error else None,
    })
    if status == "ok":
        _run_chain(tid, t, item)
    elif status == "error":
        _handle_failure(tid, t, item, error)
    if _kind(t) == "at" and item["event"] == "at" and status == "ok" \
            and item["mode"] != "manual":
        _save()
    else:
        save_soft()


def _run_chain(tid, t, item):
    for nxt in (t.get("then") or []):
        try:
            trigger(nxt, "chained",
                    {"from": tid, "event": item["event"], "status": "ok"},
                    source="chain", mode="chain")
            COUNTERS["chain"] += 1
        except Exception:
            print(f"[task_manager] 链式触发 {nxt} 失败:\n"
                  + traceback.format_exc(limit=2))


def _handle_failure(tid, t, item, error):
    try:
        retry_max = int(t.get("retry", 0) or 0)
    except (TypeError, ValueError):
        retry_max = 0
    attempt = int(item.get("attempt", 0) or 0)
    if retry_max <= 0:
        return
    if attempt < retry_max:
        base = float(t.get("retry_delay", 5) or 0)
        cap = float(t.get("retry_max_delay", CONFIG["retry_max_delay"]) or 0)
        delay = min(cap, base * (2 ** max(0, attempt))) if base > 0 else 0
        nxt = dict(item)
        nxt["attempt"] = attempt + 1
        nxt["mode"] = "retry"
        nxt["source"] = "retry"
        _push_retry_item(nxt, _now() + max(0.0, delay))
        COUNTERS["retry"] += 1
        print(f"[task_manager] task {tid} 第 {attempt + 1} 次重试将在 {delay:.1f}s 后")
    else:
        _dead_letter(tid, item, attempt + 1, error)


# ============================ 重试队列 ============================

def _push_retry_item(item, due, persist=True):
    global _RETRY_SEQ
    with _RETRY_LOCK:
        _RETRY_SEQ += 1
        heapq.heappush(_RETRY_HEAP, (float(due), _RETRY_SEQ, dict(item)))
    if persist:
        save_soft()


def _pop_due_retries(now):
    out = []
    with _RETRY_LOCK:
        while _RETRY_HEAP and _RETRY_HEAP[0][0] <= now:
            out.append(heapq.heappop(_RETRY_HEAP)[2])
    return out


# ============================ 看门狗 ============================

def _watchdog():
    global _HEARTBEAT, _last_metrics_event
    while not _STOP.is_set():
        try:
            now = _now()
            _HEARTBEAT = now
            for item in _pop_due_retries(now):
                with _LOCK:
                    t = _STATE["tasks"].get(item.get("task_id"))
                if not isinstance(t, dict):
                    continue
                if int(t.get("gen", 0) or 0) != item.get("gen"):
                    continue
                _enqueue(item)
            _retry_schedule_failures(now)
            interval = float(CONFIG["metrics_events"] or 0)
            if interval > 0 and now - _last_metrics_event >= interval:
                _last_metrics_event = now
                try:
                    fire_event("task_metrics", metrics(), source="metrics",
                               record=False)
                except Exception:
                    print("[task_manager] 指标事件发放失败:\n"
                          + traceback.format_exc(limit=2))
            if _DIRTY or _HIST_DIRTY:
                save_soft()
        except BaseException:
            print(f"[task_manager] watchdog 异常:\n{traceback.format_exc(limit=3)}")
        _STOP.wait(float(CONFIG["watch_interval"]))


def _retry_schedule_failures(now):
    with _LOCK:
        items = [(tid, t) for tid, t in _STATE["tasks"].items()
                 if isinstance(t, dict) and t.get("last_schedule_error")
                 and t.get("enabled", True) and _is_periodic(_kind(t))]
    for tid, t in items:
        last = float(t.get("_sched_retry_epoch", 0) or 0)
        if now - last < 30:
            continue
        t["_sched_retry_epoch"] = now
        if _arm(tid, t):
            _alert("schedule_recovered", f"任务 {tid} 排程已恢复", tid)
            save_soft()
        elif (int(t.get("sched_fail_count", 0) or 0) >= 3
              and int(t.get("sched_fail_count", 0) or 0) % 3 == 0):
            _alert("schedule_failed",
                   f"任务 {tid} 连续排程失败: {t.get('last_schedule_error')}", tid)
            save_soft()


# ============================ 可观测性 ============================

def metrics():
    with _LOCK:
        tasks = [t for t in _STATE["tasks"].values() if isinstance(t, dict)]
        enabled = sum(1 for t in tasks if t.get("enabled", True))
        sched_err = sum(1 for t in tasks if t.get("last_schedule_error"))
        max_consec = max([int(t.get("consecutive_failures", 0) or 0)
                          for t in tasks] or [0])
        dead = len(_HISTORY.get("dead_letters", []))
        alerts = len(_HISTORY.get("alerts", []))
        hist = sum(len(v) for v in _HISTORY.get("tasks", {}).values())
        persisted = len(_HISTORY.get("events", []))
        running = len(_RUNNING)
        tasks_total = len(tasks)
    with _RETRY_LOCK:
        retries = len(_RETRY_HEAP)
    return {
        "now": int(_now()),
        "uptime_s": int(_now() - _STARTED_AT),
        "heartbeat": int(_HEARTBEAT) if _HEARTBEAT else None,
        "heartbeat_age_s": (round(_now() - _HEARTBEAT, 1) if _HEARTBEAT else None),
        "loaded": _LOADED,
        "backend": CONFIG["backend"],
        "queue": {"size": _EVENT_Q.qsize(), "max": _EVENT_Q.maxsize,
                  "dropped": _RUN_DROPPED},
        "tasks": {"total": tasks_total, "enabled": enabled, "running": running},
        "retries_pending": retries,
        "dead_letters": dead,
        "alerts": alerts,
        "history_entries": hist,
        "persisted_events": persisted,
        "max_consecutive_failures": max_consec,
        "schedule_errors": sched_err,
        "counters": dict(COUNTERS),
    }


def metrics_json():
    try:
        return json.dumps(metrics(), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(_json_safe(metrics()), ensure_ascii=False)


def export_metrics():
    """指标导出：返回 JSON 字符串，可由 MQTT/HTTP RPC 直接回传或转发。"""
    return metrics_json()


def health():
    m = metrics()
    problems = []
    if not m["loaded"]:
        problems.append("not_initialized")
    if m["heartbeat_age_s"] is None or m["heartbeat_age_s"] > 15:
        problems.append("heartbeat_stale")
    if m["queue"]["max"] and m["queue"]["size"] >= m["queue"]["max"] * 0.9:
        problems.append("queue_near_full")
    if m["schedule_errors"] > 0:
        problems.append("schedule_errors")
    if m["dead_letters"] > 0:
        problems.append("dead_letters")
    if m["max_consecutive_failures"] >= 3:
        problems.append("consecutive_failures")
    m["problems"] = problems
    m["ok"] = not problems
    return m


def heartbeat():
    return {"ok": True, "epoch": round(_HEARTBEAT or _now(), 3),
            "age_s": (round(_now() - _HEARTBEAT, 1) if _HEARTBEAT else None),
            "queue": _EVENT_Q.qsize(), "running": len(_RUNNING)}


def history(task_id=None, limit=20):
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 20
    with _LOCK:
        if task_id is not None:
            arr = list(_HISTORY.get("tasks", {}).get(task_id, []))[-n:]
            return {"ok": True, "task_id": task_id, "count": len(arr),
                    "entries": _json_safe(arr)}
        out = {tid: _json_safe(list(arr)[-n:])
               for tid, arr in _HISTORY.get("tasks", {}).items()}
    return {"ok": True, "count": len(out), "history": out}


def dead_letters(limit=50):
    try:
        n = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        n = 50
    with _LOCK:
        arr = _json_safe(list(_HISTORY.get("dead_letters", []))[-n:])
    return {"ok": True, "count": len(arr), "entries": arr}


def clear_dead_letters():
    with _LOCK:
        n = len(_HISTORY.get("dead_letters", []))
        _HISTORY["dead_letters"] = []
    _save_history()
    return {"ok": True, "cleared": n}


def alerts(limit=50):
    try:
        n = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        n = 50
    with _LOCK:
        arr = _json_safe(list(_HISTORY.get("alerts", []))[-n:])
    return {"ok": True, "count": len(arr), "entries": arr}


def clear_alerts():
    with _LOCK:
        n = len(_HISTORY.get("alerts", []))
        _HISTORY["alerts"] = []
    _save_history()
    return {"ok": True, "cleared": n}


def recent_events(limit=20, since_seq=None):
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 20
    with _LOCK:
        items = list(_RECENT_EVENTS)
    if since_seq is not None:
        try:
            seq = int(since_seq)
            items = [e for e in items if int(e.get("seq", 0)) > seq]
        except (TypeError, ValueError):
            pass
    items = items[-n:]
    try:
        return json.loads(json.dumps(items, ensure_ascii=False))
    except (TypeError, ValueError):
        return [dict(e) for e in items]


def persisted_events(limit=50):
    try:
        n = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        n = 50
    with _LOCK:
        arr = _json_safe(list(_HISTORY.get("events", []))[-n:])
    return {"ok": True, "count": len(arr), "events": arr}


def replay_events(limit=10, since_seq=None, event=None):
    """重放关键事件（source=replay，不记账、不重复持久化）。"""
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 10
    with _LOCK:
        arr = list(_HISTORY.get("events", []))
    if since_seq is not None:
        try:
            seq = int(since_seq)
            arr = [e for e in arr if int(e.get("seq", 0)) > seq]
        except (TypeError, ValueError):
            pass
    if event:
        arr = [e for e in arr if e.get("event") == event]
    arr = arr[-n:]
    replayed = []
    for e in arr:
        try:
            fire_event(e.get("event"), e.get("payload"), source="replay",
                       record=False, persist=False)
            replayed.append(e.get("event"))
        except Exception:
            print("[task_manager] 重放事件失败:\n"
                  + traceback.format_exc(limit=2))
    return {"ok": True, "count": len(replayed), "events": replayed}


def clear_persisted_events():
    with _LOCK:
        n = len(_HISTORY.get("events", []))
        _HISTORY["events"] = []
    _save_history()
    return {"ok": True, "cleared": n}


def network_state():
    if _jclass is None:
        return {"ok": False, "error": "非 Android 环境"}
    try:
        snap = json.loads(_jclass(_NETWORK_MONITOR).getState())
        snap["ok"] = True
        return snap
    except Exception:
        return {"ok": False, "error": traceback.format_exc(limit=2)}


def _fmt_epoch_ms(ms):
    if ms is None:
        return "-"
    sec, milli = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec)) + ".%03d" % milli


def system_events(limit=50, return_str=False):
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
        item = {"ts_ms": ts, "time": _fmt_epoch_ms(ts),
                "event": parts[1] if len(parts) > 1 else "", "payload": {}}
        if len(parts) > 2:
            try:
                item["payload"] = json.loads(parts[2])
            except ValueError:
                item["payload"] = {"raw": parts[2]}
        events.append(item)
    if return_str:
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
    if _jclass is None:
        return {"ok": False, "error": "非 Android 环境"}
    try:
        _jclass(_SYSTEM_MONITOR).clearEventLog()
        return {"ok": True}
    except Exception:
        return {"ok": False, "error": traceback.format_exc(limit=2)}


# ============================ 入口 ============================

def consume_boot_pending():
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
               "app_init_epoch": round(_now(), 3)}
    r = fire_event("boot_completed", payload, source="boot")
    if r.get("duplicate"):
        print("[task_manager] boot_completed 已由预初始化补发，跳过重复")
    try:
        os.remove(marker)
    except OSError:
        pass
    return {"ok": True, "fired": not r.get("duplicate", False),
            "payload": payload, "matched": r.get("matched", [])}


def execute_task(task_id):
    """WorkManager / AlarmManager 回调入口：只校验并入队后立即返回。"""
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
                   "fired_at": int(_now())}
        if kind == "interval":
            payload["interval"] = t.get("interval")
        elif kind == "cron":
            payload["cron"] = t.get("cron")
        else:
            payload["at"] = t.get("at")
        r = trigger(task_id, name, payload, source="workmanager")
        return "ok" if r.get("ok") else r.get("reason", "error")
    except Exception:
        print(f"[task_manager] execute {task_id} 异常:\n"
              + traceback.format_exc(limit=2))
        return "error"
