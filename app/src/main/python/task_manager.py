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
        except (OSError, ValueError):
            pass
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
        _jclass(_SYSTEM_MONITOR).syncRegisteredEvents(json.dumps(names))
        print(f"[task_manager] 系统事件订阅同步: {names}")
    except Exception:
        print("[task_manager] 系统事件订阅同步失败:\n" + traceback.format_exc(limit=2))


# ============================ RPC：任务生命周期 ============================

# add_task 允许的 opts 键；出现其他键直接报错（不静默忽略）
ALLOWED = {"interval", "at", "repeat", "events", "debounce"}


def add_task(task_id, func_name=None, code=None, enabled=True, **opts):
    """新增/覆盖一个任务（**唯一的建任务入口**）。

    设计参考 APScheduler（``add_job(..., trigger=date|interval|cron,
    **trigger_args)``）与 matrix-planner（``every`` / ``cron`` / ``once``）：
    用一组关键字参数描述触发源，由本函数归一成任务字典，而不是为每种触发源
    各开一个入口。add_event_task / add_at_task / add_daily_task 只是兼容包装。

    opts 只允许下列 5 个键（未知键直接报错，不静默忽略）：
      - interval：秒，>=1 → kind="interval"；若同时给 at，interval 优先；
        到点以伪事件 interval 分发，执行完自动接力下一次。
      - at：``YYYY-MM-DD HH:MM[:SS]`` / ``HH:MM[:SS]``（Python 侧也接受
        ``datetime.datetime``）→ 一次性 kind="at"，触发后自动 enabled=False；
        配合 repeat=True（仅 ``HH:MM[:SS]``）→ 每日 kind="daily"。
      - events：系统事件名（字符串或列表），可与任意触发源叠加。
      - repeat：仅对 at=HH:MM[:SS] 生效，表示每日重复。
      - debounce：秒；同一任务两次触发的最小间隔，0 表示不节流。
    interval / at / events 三者都没有 → 报错。

    定时/时刻/每日到点后都以伪事件 interval / at / daily 走统一分发 _execute。

    :param func_name: 内置函数名（JOB_FUNCS）；与 code 二选一，code 优先
    :param code: 触发时执行的 Python 源码；可用 event/payload/task_id/jclass
    """
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
    seconds = None
    if interval is not None:
        try:
            seconds = max(1, int(interval))
        except (TypeError, ValueError):
            return {"ok": False, "error": "interval 必须是正整数秒",
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
    if not code and func_name not in JOB_FUNCS:
        return {"ok": False,
                "error": "必须提供 code，或使用已注册 func",
                "funcs": sorted(JOB_FUNCS)}
    try:
        gap = float(debounce or 0.0)
    except (TypeError, ValueError):
        gap = 0.0
    with _LOCK:
        t = {
            "kind": kind,
            "events": names,
            "func": func_name,
            "code": code,
            "debounce": gap,
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
        _save()
    if names:
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
            epoch, _norm, _abs = _parse_at(t.get("at", ""), now)
        except (ValueError, TypeError):
            return 0
        return max(1, int(round(epoch - now)))
    return 0


def _arm(task_id, t, delay=None):
    """计算/使用给定延迟交给 WorkManager，并记下 next_epoch。

    任何异常只降级（返回 False），绝不向外抛。
    """
    try:
        if delay is None:
            delay = _delay_of(t)
        delay = int(delay or 0)
        if delay <= 0:
            return False
        t["next_epoch"] = int(round(time.time() + delay))
        return _schedule(task_id, delay)
    except Exception:
        print(f"[task_manager] 排程 {task_id} 失败:\n"
              + traceback.format_exc(limit=2))
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
    if existed and _is_periodic(_kind(t)) and _jclass is not None:
        try:
            _jclass("com.kingzcheung.xime.task.TaskScheduler").cancel(task_id)
        except Exception:
            pass
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
    if periodic and _jclass is not None:
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
    with _LOCK:
        t = _STATE["tasks"].get(task_id)
        if not t:
            return {"ok": False, "error": "not found"}
        kind = _kind(t)
    try:
        if kind == "event":
            # 事件任务立即执行一次（无真实负载，payload 为空；多事件取第一个）
            events = _system_events_of(t)
            if events:
                _ensure_event_worker()
                _EVENT_Q.put((events[0], None, task_id))
        elif not _schedule(task_id, 0):
            # 非 Android（无 TaskScheduler）：直接入队伪事件分发一次
            _ensure_event_worker()
            _EVENT_Q.put((_pseudo_event(kind),
                          {"task_id": task_id, "source": "run_now"}, task_id))
    except Exception:
        print(f"[task_manager] run_now {task_id} 失败:\n"
              + traceback.format_exc(limit=2))
    return {"ok": True, "kind": kind}


def list_tasks():
    """全部任务及其状态（深拷贝）；补齐 events/at/next_epoch，不删旧键。"""
    with _LOCK:
        try:
            snap = json.loads(json.dumps(_STATE["tasks"], ensure_ascii=False))
        except (TypeError, ValueError):
            snap = {}
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

def fire_event(event_name, payload=None, source="system", record=True):
    """Kotlin 事件回调入口，也可被 RPC 模拟。

    必须快速返回：只记账并入队，匹配任务的执行在串行守护线程完成，
    避免阻塞 Android 网络回调线程 / Chaquopy 调用线程。

    record=False：高频事件（如 ime_keys 按键合批）不写 recent_events 环形
    缓冲、不 print，避免刷掉系统事件与拖慢调用方；任务派发完全不受影响。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else None
        except (ValueError, AttributeError):
            payload = {"raw": payload}
    if not isinstance(payload, dict):
        payload = None if payload is None else {"value": payload}

    if record:
        rec = {"ts": time.strftime("%H:%M:%S"),
               "epoch": round(time.time(), 3),
               "event": str(event_name),
               "source": source,
               "payload": payload}
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
    _ensure_event_worker()
    for tid in matches:
        _EVENT_Q.put((event_name, payload, tid))
    if record:
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
        # 任何异常都只降级：绝不能让守护线程退出
        try:
            with _LOCK:
                t = _STATE["tasks"].get(task_id)
            if t is None or not t.get("enabled", True):
                continue
            if event_name not in PSEUDO_EVENTS and event_name not in _system_events_of(t):
                continue
            # debounce 节流
            now = time.time()
            gap = float(t.get("debounce", 0.0) or 0.0)
            last = float(t.get("_last_fire_epoch", 0.0) or 0.0)
            if gap > 0 and now - last < gap:
                continue
            t["_last_fire_epoch"] = now
            _execute(task_id, t, event_name, payload)
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
        delay = None
        try:
            with _LOCK:
                t["run_count"] = int(t.get("run_count", 0)) + 1
                t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
                t["last_result"] = (str(result)[:500] if error is None
                                    else "ERROR: " + error.splitlines()[-1])
                if event_name is not None:
                    t["last_event"] = event_name
                    t["last_payload"] = payload
                kind = _kind(t)
                enabled = t.get("enabled", True)
                # 只有“排程到点”才接力下一次；系统事件不重复排程
                if kind == "interval" and event_name == "interval":
                    delay = max(1, int(t.get("interval", 60) or 60))
                elif kind == "daily" and event_name == "daily":
                    delay = _daily_delay(t)
                elif kind == "at" and event_name == "at":
                    # 一次性 at：触发后自动停用，保留 last_run/last_result
                    t["enabled"] = False
                    t["next_epoch"] = None
                    enabled = False
                _save()
            if enabled and delay:
                with _LOCK:
                    _arm(task_id, t, delay)
                    _save()
        except Exception:
            # 收尾（统计/持久化/接力）失败只降级，不影响事件 worker 线程
            print(f"[task_manager] task {task_id} 收尾失败:\n"
                  + traceback.format_exc(limit=2))
    return "ok" if error is None else "error"


def execute_task(task_id):
    """WorkManager（PythonTaskWorker）回调入口。

    按任务的排程类型决定伪事件名（interval / at / daily）后走统一分发
    _execute；interval 执行完接力下一次，一次性 at 自动停用，daily 排明天。
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
        return _execute(task_id, t, name, payload)
    except Exception:
        print(f"[task_manager] execute {task_id} 异常:\n"
              + traceback.format_exc(limit=2))
        return "error"

