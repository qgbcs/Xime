"""高可靠后台任务框架（Python 侧）—— 定时任务 + 事件驱动任务

架构：WorkManager 负责系统级调度（进程被杀、设备重启后仍能触发），
Kotlin NetworkEventMonitor 负责网络边沿事件（WiFi 连/断、网络通/断），
Python 负责任务注册表、状态持久化、执行统计与接力下一次调度。

任务一律由 RPC 远程下发，APK 内不硬编码任何任务（init 只从 JSON 恢复）。
任务支持两种触发方式：
  1) 定时（interval）：用 OneTimeWorkRequest 自接力突破 15 分钟下限；
  2) 每日定时（daily）：at="HH:MM"，按本地时区每天到点触发，执行后自接力；
  3) 事件（event/events）：订阅 Kotlin 上报的事件（网络边沿、开机完成），见
     EVENTS 说明；一个任务可绑定多个事件（events 列表），任一命中即执行。

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

RPC 示例：
  r=task_manager.add_daily_task("dailytest", "08:30",
  ...   code='print("[每日] 触发", task_id)')
  r=task_manager.add_event_task("wifilog", ["wifi_connected", "boot_completed"],
  ...   code='print("[事件] WiFi 已连接", payload)')
  r=task_manager.test_event("wifi_connected", {"wifi": True})   # 本地模拟
  r=task_manager.list_tasks()
  r=task_manager.recent_events()
  r=task_manager.system_events(100, True)   # 多行文本，每行一个事件（本地时间格式化）
  r=task_manager.network_state()            # 当前网络快照 {wifi, online, validated, initialized, ts}
"""
import io
import json
import os
import queue
import sys
import threading
import time
import traceback

try:
    from java import jclass
    _jclass = jclass
except Exception:  # pragma: no cover - 非 Android 环境（PC 侧单测）
    _jclass = None

_STATE_PATH = None
_LOCK = threading.RLock()
_STATE = {"tasks": {}}

# 事件队列串行执行：网络回调线程只入队立即返回，执行全部在该守护线程完成。
_EVENT_Q = queue.Queue()
_RECENT_EVENTS = []          # 环形缓冲，供 RPC 回读
_RECENT_MAX = 50

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
)

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
        except (OSError, ValueError):
            pass
        interval_n = daily_n = event_n = 0
        for task_id, t in _STATE["tasks"].items():
            if not t.get("enabled", True):
                continue
            kind = _kind(t)
            if kind == "interval":
                _schedule(task_id, t.get("interval", 60))
                interval_n += 1
            elif kind == "daily":
                _schedule(task_id, _daily_delay(t))
                daily_n += 1
            else:
                event_n += 1
        _LOADED = True
    _sync_system_events()
    _ensure_event_worker()
    _replay_pre_init_events()
    consume_boot_pending()
    print(f"[task_manager] init done, {len(_STATE['tasks'])} tasks, "
          f"{interval_n} interval + {daily_n} daily rescheduled, "
          f"{event_n} event tasks armed")
    return True


def _replay_pre_init_events():
    """把 init 之前缓存的事件补发给刚载入的任务表（只补发一次）。"""
    with _LOCK:
        items = list(_PRE_INIT_EVENTS)
        del _PRE_INIT_EVENTS[:]
    for name, payload, source in items:
        try:
            fire_event(name, payload, source=source)
        except Exception:
            print("[task_manager] 补发预初始化事件失败:\n"
                  + traceback.format_exc(limit=2))


def _save():
    if not _STATE_PATH:
        return
    tmp = _STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATE_PATH)
    except OSError as e:
        print(f"[task_manager] save failed: {e}")


def _kind(t):
    return t.get("kind", "interval")


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


# ============================ 系统广播（Kotlin SystemEventMonitor） ============================

_SYSTEM_MONITOR = "com.kingzcheung.xime.task.SystemEventMonitor"
_NETWORK_MONITOR = "com.kingzcheung.xime.task.NetworkEventMonitor"


def _sync_system_events():
    """把当前启用的事件任务的事件名集合同步给 Kotlin SystemEventMonitor。

    Kotlin 只对集合内的事件调用 fire_event；未订阅的系统广播仅写入 Kotlin
    环形缓冲，不产生任何 Chaquopy/Python 调用，避免无谓耗电。
    """
    if _jclass is None:
        return
    with _LOCK:
        names = set()
        for t in _STATE["tasks"].values():
            if _kind(t) == "event" and t.get("enabled", True):
                names.update(_events_of(t))
        names = sorted(names)
    try:
        _jclass(_SYSTEM_MONITOR).syncRegisteredEvents(json.dumps(names))
        print(f"[task_manager] 系统事件订阅同步: {names}")
    except Exception:
        print("[task_manager] 系统事件订阅同步失败:\n" + traceback.format_exc(limit=2))


# ============================ RPC：任务生命周期 ============================

def add_task(task_id, func_name=None, interval=60, code=None, enabled=True):
    """新增/覆盖一个【定时】任务。interval 秒；code 为任意 Python 源码。"""
    if not code and func_name not in JOB_FUNCS:
        return {"ok": False,
                "error": "必须提供 code，或使用已注册 func",
                "funcs": sorted(JOB_FUNCS)}
    with _LOCK:
        _STATE["tasks"][task_id] = {
            "kind": "interval",
            "func": func_name,
            "code": code,
            "interval": int(interval),
            "enabled": bool(enabled),
            "run_count": 0,
            "last_run": None,
            "last_result": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save()
        if enabled:
            _schedule(task_id, int(interval))
    print(f"[task_manager] add interval {task_id} interval={interval}s")
    return {"ok": True, "id": task_id, "kind": "interval"}


def add_event_task(task_id, event=None, code=None, func_name=None,
                   debounce=0.0, enabled=True, events=None):
    """新增/覆盖一个【事件】任务，支持一个任务绑定多个事件。

    :param event: 事件名，或事件名列表，见 EVENTS（也可自定义，以后端实际派发为准）
    :param events: 事件名列表，与 event 合并去重；二者任意一个即可
    :param code:  事件触发时执行的 Python 源码；可用 event/payload/task_id/jclass
    :param debounce: 秒；同一任务两次触发的最小间隔，0 表示不节流
    """
    names = _norm_events(event, events)
    if not names:
        return {"ok": False,
                "error": "必须提供至少一个合法事件名（event 或 events）",
                "event": event, "events": events}
    if not code and func_name not in JOB_FUNCS:
        return {"ok": False,
                "error": "必须提供 code，或使用已注册 func",
                "funcs": sorted(JOB_FUNCS)}
    with _LOCK:
        _STATE["tasks"][task_id] = {
            "kind": "event",
            "event": names[0],
            "events": names,
            "func": func_name,
            "code": code,
            "debounce": float(debounce or 0.0),
            "enabled": bool(enabled),
            "run_count": 0,
            "last_run": None,
            "last_result": None,
            "last_event": None,
            "last_payload": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save()
    _sync_system_events()
    print(f"[task_manager] add event {task_id} on={names} debounce={debounce}s "
          f"enabled={enabled}")
    return {"ok": True, "id": task_id, "kind": "event", "event": names[0],
            "events": names}


def _parse_hms(at):
    """把 "HH:MM" / "HH:MM:SS" 解析成 (时, 分, 秒)；非法格式抛 ValueError。"""
    parts = str(at).strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS")
    hh, mm = int(parts[0]), int(parts[1])
    ss = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
        raise ValueError("时间超出范围")
    return hh, mm, ss


def _daily_delay(t):
    """距下一次 at 时刻的秒数（本地时区；当天已过则顺延到明天）。"""
    hh, mm, ss = _parse_hms(t.get("at", "00:00"))
    now = time.time()
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, 0, -1))
    if target <= now:
        target += 86400
    return int(max(1, target - now))


def add_daily_task(task_id, at="00:00", code=None, func_name=None, enabled=True):
    """新增/覆盖一个【每天定时】任务。at 为本地时间 "HH:MM" 或 "HH:MM:SS"。"""
    if not code and func_name not in JOB_FUNCS:
        return {"ok": False,
                "error": "必须提供 code，或使用已注册 func",
                "funcs": sorted(JOB_FUNCS)}
    try:
        _parse_hms(at)
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e), "at": at}
    at = str(at).strip()
    with _LOCK:
        _STATE["tasks"][task_id] = {
            "kind": "daily",
            "at": at,
            "func": func_name,
            "code": code,
            "enabled": bool(enabled),
            "run_count": 0,
            "last_run": None,
            "last_result": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save()
        delay = _daily_delay(_STATE["tasks"][task_id])
    if enabled:
        _schedule(task_id, delay)
    print(f"[task_manager] add daily {task_id} at={at} next={delay}s")
    return {"ok": True, "id": task_id, "kind": "daily", "at": at,
            "next_in": delay}


def remove_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].pop(task_id, None)
        existed = t is not None
        _save()
    if existed and _kind(t) == "interval" and _jclass is not None:
        try:
            _jclass("com.kingzcheung.xime.task.TaskScheduler").cancel(task_id)
        except Exception:
            pass
    if existed:
        _sync_system_events()
    return {"ok": existed}


def pause_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        t["enabled"] = False
        _save()
        is_interval = _kind(t) == "interval"
    if is_interval and _jclass is not None:
        try:
            _jclass("com.kingzcheung.xime.task.TaskScheduler").cancel(task_id)
        except Exception:
            pass
    _sync_system_events()
    return {"ok": True}


def resume_task(task_id):
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        t["enabled"] = True
        _save()
        kind = _kind(t)
        delay = t.get("interval", 60)
        if kind == "daily":
            delay = _daily_delay(t)
        events = _events_of(t)
        _save()
    _sync_system_events()
    if kind in ("interval", "daily"):
        _schedule(task_id, delay)
    return {"ok": True, "kind": kind,
            "event": events[0] if events else None, "events": events}


def run_now(task_id):
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        kind = _kind(t)
    if kind == "event":
        # 事件任务立即执行一次（当前无真实负载，payload 为空；多事件取第一个）
        events = _events_of(t)
        if events:
            _EVENT_Q.put((events[0], None, task_id))
    else:
        _schedule(task_id, 0)
    return {"ok": True, "kind": kind}


def list_tasks():
    with _LOCK:
        return json.loads(json.dumps(_STATE["tasks"], ensure_ascii=False))


def list_events():
    """可订阅事件名（提示用）。"""
    return list(EVENTS)


def recent_events(limit=20):
    """最近发生的事件环形缓冲，供 RPC 校验事件链路。"""
    with _LOCK:
        return list(_RECENT_EVENTS[-max(1, int(limit)):])


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

def fire_event(event_name, payload=None, source="system"):
    """Kotlin NetworkEventMonitor 的回调入口，也可被 RPC 模拟。

    必须快速返回：只记账并入队，匹配任务的执行在串行守护线程完成，
    避免阻塞 Android 网络回调线程 / Chaquopy 调用线程。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else None
        except (ValueError, AttributeError):
            payload = {"raw": payload}
    if not isinstance(payload, dict):
        payload = None if payload is None else {"value": payload}

    rec = {"ts": time.strftime("%H:%M:%S"),
           "epoch": round(time.time(), 3),
           "event": str(event_name),
           "source": source,
           "payload": payload}
    with _LOCK:
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
            matches = [
                tid for tid, t in _STATE["tasks"].items()
                if _kind(t) == "event" and t.get("enabled", True)
                and event_name in _events_of(t)
            ]
    _ensure_event_worker()
    for tid in matches:
        _EVENT_Q.put((event_name, payload, tid))
    print(f"[task_manager] event={event_name} source={source} "
          f"payload={payload} matched={matches}")
    return {"ok": True, "event": event_name, "matched": matches,
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
    try:
        os.remove(marker)
    except OSError:
        pass
    payload = {"boot": True,
               "boot_epoch": int(raw) if raw.isdigit() else None,
               "app_init_epoch": round(time.time(), 3)}
    r = fire_event("boot_completed", payload, source="boot")
    return {"ok": True, "fired": True, "payload": payload,
            "matched": r.get("matched", [])}


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
        event_name, payload, task_id = _EVENT_Q.get()
        with _LOCK:
            t = _STATE["tasks"].get(task_id)
        if t is None or not t.get("enabled", True) or event_name not in _events_of(t):
            continue
        # debounce 节流
        now = time.time()
        gap = float(t.get("debounce", 0.0) or 0.0)
        last = float(t.get("_last_fire_epoch", 0.0) or 0.0)
        if gap > 0 and now - last < gap:
            continue
        t["_last_fire_epoch"] = now
        _execute(task_id, t, event_name, payload)


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


def _execute(task_id, t, event_name=None, payload=None):
    result, error = None, None
    captured = ""
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
        with _LOCK:
            t["run_count"] = int(t.get("run_count", 0)) + 1
            t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            t["last_result"] = (str(result)[:500] if error is None
                                else "ERROR: " + error.splitlines()[-1])
            if event_name is not None:
                t["last_event"] = event_name
                t["last_payload"] = payload
            kind = _kind(t)
            delay = t.get("interval", 60)
            if kind == "daily":
                delay = _daily_delay(t)
            enabled = t.get("enabled", True)
            _save()
        # 定时/每日任务执行后接力下一次；事件任务不调度
        if kind in ("interval", "daily") and enabled:
            _schedule(task_id, delay)
    return "ok" if error is None else "error"


def execute_task(task_id):
    """WorkManager（PythonTaskWorker）回调入口：执行定时任务并接力。"""
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
    if t is None:
        print(f"[task_manager] execute {task_id}: 未注册，忽略")
        return "not_found"
    if _kind(t) not in ("interval", "daily"):
        return "not_periodic_task"
    return _execute(task_id, t)
