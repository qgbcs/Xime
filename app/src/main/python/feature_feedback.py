"""反馈聊天页：全部界面元素由 Python 调用 Android 原生控件绘制。

约束：
- build_view 在 FeedbackActivity.onCreate 主线程执行，只建 View，不做耗时操作；
- 所有 JVM->Python 回调都包 try/except，异常只记日志，绝不穿透杀进程；
- 同一种接口的 dynamic_proxy 类全进程只建一次并缓存，避免二次建类导致回调崩溃；
- 消息仅写入 filesDir/feedback/messages.json，图片仅存入 filesDir/feedback/images，
  不向任何服务器发送。
"""
import json
import os
import threading
import time

_DATA_SUBDIR = "feedback"
_IMAGE_SUBDIR = "images"
_REQUEST_PICK_IMAGE = 1001

# 反馈信号：只发布“用户点了发送”这一事实，不含消息正文
_SIGNAL_SENDING = "sending"
_SIGNAL_SENT = "sent"
_SIGNAL_PENDING = "pending"
_SIGNAL_LOCK = threading.RLock()
_RETRY_LOCK = threading.Lock()
_FILES_DIR = None
_SIGNAL_SEQ = 0
_RETRY_NET_TASK = "ime_feedback_signal_net"
_RETRY_TICK_TASK = "ime_feedback_signal_tick"
_RETRY_INTERVAL_SECONDS = 300
_RETRY_DEBOUNCE_SECONDS = 3.0
_RETRY_STARTUP_WAIT_SECONDS = 20

_PAGE_BG = "#F5F6FA"
_BAR_BG = "#FFFFFF"
_INK = "#111827"
_MUTED = "#6B7280"
_ACCENT = "#1A73E8"
_BUBBLE_BG = "#1A73E8"
_BUBBLE_TEXT = "#FFFFFF"
_AUTHOR_BG = "#FFFFFF"
_AUTHOR_TEXT = "#111827"
_HINT_BG = "#EDE9FE"

_PROXIES = {}
_ACTIVE = {}


def _dp(context, value):
    return int(value * context.getResources().getDisplayMetrics().density + 0.5)


def _color(value):
    from android.graphics import Color
    return Color.parseColor(value)


def _log(where, error):
    detail = "%s failed: %s: %s" % (where, type(error).__name__, error)
    try:
        from android.util import Log
        Log.e("xime-feedback", detail[:500])
    except Exception:
        print(detail[:500])


def _click_class():
    cls = _PROXIES.get("click")
    if cls is None:
        from android.view import View
        from java import dynamic_proxy

        class OnClickProxy(dynamic_proxy(View.OnClickListener)):
            def __init__(self, fn=None):
                super().__init__()
                self.fn = fn

            def onClick(self, _view):
                if self.fn is not None:
                    try:
                        self.fn()
                    except BaseException as error:
                        _log("click", error)

        cls = _PROXIES["click"] = OnClickProxy
    return cls


def _runnable_class():
    cls = _PROXIES.get("runnable")
    if cls is None:
        from java import dynamic_proxy
        from java.lang import Runnable

        class RunnableProxy(dynamic_proxy(Runnable)):
            def __init__(self, fn=None):
                super().__init__()
                self.fn = fn

            def run(self):
                if self.fn is not None:
                    try:
                        self.fn()
                    except BaseException as error:
                        _log("runnable", error)

        cls = _PROXIES["runnable"] = RunnableProxy
    return cls


def _layout_class():
    cls = _PROXIES.get("layout")
    if cls is None:
        from android.view import View
        from java import dynamic_proxy

        class LayoutProxy(dynamic_proxy(View.OnLayoutChangeListener)):
            def __init__(self, fn=None):
                super().__init__()
                self.fn = fn

            def onLayoutChange(self, view, left, top, right, bottom,
                               old_left, old_top, old_right, old_bottom):
                if self.fn is not None:
                    try:
                        self.fn()
                    except BaseException as error:
                        _log("layout", error)

        cls = _PROXIES["layout"] = LayoutProxy
    return cls


def _post(fn):
    """投递到主线程：RPC 线程直接改 View 会崩，必须走 Handler。"""
    from android.os import Handler, Looper
    Handler(Looper.getMainLooper()).post(_runnable_class()(fn))


def _files_root(context=None):
    """取 filesDir：优先当前页面 context，其次 app.py start 时注入的私有目录。"""
    if context is not None:
        try:
            return str(context.getFilesDir().getAbsolutePath())
        except Exception:
            pass
    return _FILES_DIR


def init(files_dir=None):
    """app.py start 时调用：记录私有目录并补发上次没发成功的信号。"""
    global _FILES_DIR
    if not files_dir:
        return False
    _FILES_DIR = os.fspath(files_dir)
    try:
        threading.Thread(target=resume_pending_signals, daemon=True).start()
    except Exception as error:
        _log("resume pending", error)
    return True


def _data_dir(context=None):
    root_dir = _files_root(context)
    if not root_dir:
        return None, None
    root = os.path.join(root_dir, _DATA_SUBDIR)
    images = os.path.join(root, _IMAGE_SUBDIR)
    if not os.path.isdir(images):
        os.makedirs(images)
    return root, images


def _messages_path(context=None):
    root, _ = _data_dir(context)
    if not root:
        return None
    return os.path.join(root, "messages.json")


def _load_messages(context=None):
    path = _messages_path(context)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception as error:
        _log("load messages", error)
        return []


def _save_messages(context, messages):
    path = _messages_path(context)
    if not path:
        return
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(messages, handle, ensure_ascii=False)
        os.replace(temp, path)
    except Exception as error:
        _log("save messages", error)


def _rounded(context, color, radius_dp, stroke_color=None, stroke_dp=0):
    from android.graphics.drawable import GradientDrawable
    drawable = GradientDrawable()
    drawable.setShape(GradientDrawable.RECTANGLE)
    drawable.setColor(_color(color))
    drawable.setCornerRadius(float(_dp(context, radius_dp)))
    if stroke_color is not None and stroke_dp:
        drawable.setStroke(_dp(context, stroke_dp), _color(stroke_color))
    return drawable


def _time_label(ts):
    try:
        return time.strftime("%H:%M", time.localtime(int(ts) / 1000.0))
    except Exception:
        return ""


def _bubble(context, message):
    from android.graphics import BitmapFactory
    from android.view import ViewGroup, Gravity
    from android.widget import ImageView, LinearLayout, TextView

    match = ViewGroup.LayoutParams.MATCH_PARENT
    wrap = ViewGroup.LayoutParams.WRAP_CONTENT

    # 反馈作者的消息靠左白底，用户自己的消息靠右蓝底
    is_author = str(message.get("sender") or "user") == "author"
    align = Gravity.START if is_author else Gravity.END
    bubble_bg = _AUTHOR_BG if is_author else _BUBBLE_BG
    bubble_text = _AUTHOR_TEXT if is_author else _BUBBLE_TEXT

    row = LinearLayout(context)
    row.setOrientation(LinearLayout.HORIZONTAL)
    row.setGravity(align)
    row.setPadding(0, 0, 0, _dp(context, 10))

    column = LinearLayout(context)
    column.setOrientation(LinearLayout.VERTICAL)
    column.setGravity(align)

    if message.get("type") == "image":
        image = ImageView(context)
        image.setAdjustViewBounds(True)
        image.setScaleType(ImageView.ScaleType.FIT_CENTER)
        image.setBackground(_rounded(context, "#FFFFFF", 12, "#E5E7EB", 1))
        image.setPadding(_dp(context, 4), _dp(context, 4), _dp(context, 4), _dp(context, 4))
        try:
            bitmap = BitmapFactory.decodeFile(str(message.get("path") or ""))
            if bitmap is not None:
                image.setImageBitmap(bitmap)
        except Exception as error:
            _log("decode image", error)
        column.addView(
            image,
            LinearLayout.LayoutParams(_dp(context, 200), wrap),
        )
    else:
        text = TextView(context)
        text.setText(str(message.get("text") or ""))
        text.setTextSize(15)
        text.setTextColor(_color(bubble_text))
        text.setBackground(_rounded(context, bubble_bg, 14))
        text.setPadding(_dp(context, 12), _dp(context, 8), _dp(context, 12), _dp(context, 8))
        text.setMaxWidth(_dp(context, 280))
        column.addView(text, LinearLayout.LayoutParams(wrap, wrap))

    stamp = TextView(context)
    stamp.setText(_time_label(message.get("ts") or 0))
    stamp.setTextSize(10)
    stamp.setTextColor(_color(_MUTED))
    stamp.setGravity(align)
    stamp.setPadding(0, _dp(context, 4), 0, 0)
    column.addView(stamp, LinearLayout.LayoutParams(wrap, wrap))

    # 用户点“发送”后，在时间戳下用小灰字显示信号投递状态
    signal = message.get("signal")
    if isinstance(signal, dict) and not is_author:
        status = TextView(context)
        status.setText(_signal_label(signal))
        status.setTextSize(10)
        status.setTextColor(_color(_MUTED))
        status.setGravity(align)
        status.setPadding(0, _dp(context, 2), 0, 0)
        column.addView(status, LinearLayout.LayoutParams(wrap, wrap))

    row.addView(column, LinearLayout.LayoutParams(wrap, wrap))
    return row


def _new_message_id():
    """消息唯一 id：毫秒时间戳 + 进程内自增，供补发线程按 id 回写状态。"""
    global _SIGNAL_SEQ
    with _SIGNAL_LOCK:
        _SIGNAL_SEQ += 1
        return "%d_%d" % (int(time.time() * 1000), _SIGNAL_SEQ)


def _signal_label(signal):
    state = str(signal.get("state") or "")
    if state == _SIGNAL_SENT:
        return "已发送"
    if state == _SIGNAL_SENDING:
        return "发送中…"
    if state == _SIGNAL_PENDING:
        return "待发送 · 网络恢复后自动重试"
    return ""


def _online_broker_count(server=None):
    """本机 MQTT 在线 broker 数；取不到实例或异常一律按离线处理。"""
    try:
        if server is None:
            import app
            server = getattr(app, "gms", None) or getattr(app, "mqtt_server", None)
        net = getattr(server, "mqtt_net", None) if server is not None else None
        snapshot = net.get_connection_snapshot() if net is not None else None
        if not isinstance(snapshot, dict):
            return 0
        return sum(1 for online in snapshot.values() if online)
    except Exception:
        return 0


def _publish_signal(confirm_timeout=2.0):
    """后台线程发布 ime_feedback 报告（不含正文），返回归一化发布状态。

    成功口径：QoS1 收到 PUBACK（acked>0）；旧版接口无 acked 时退回 queued>0。
    必须在后台线程调用：confirm_timeout 会阻塞等待确认。
    """
    import app
    server = getattr(app, "gms", None) or getattr(app, "mqtt_server", None)
    if server is None:
        return {"ok": False, "sent": False, "error": "mqtt_unavailable", "online": 0}
    online = _online_broker_count(server)
    if online <= 0:
        return {"ok": False, "sent": False, "error": "offline", "online": 0}
    try:
        publish_now = getattr(server, "publish_global_topic_list_now", None)
        if publish_now is not None:
            result = publish_now("ime_feedback", confirm_timeout=confirm_timeout)
        else:
            # 旧版 multi_mqtt 没有确认等待接口，退回原发布方法
            result = server._publish_global_topic_list("ime_feedback")
    except TypeError:
        result = server._publish_global_topic_list("ime_feedback")
    if not isinstance(result, dict):
        return {"ok": False, "sent": False, "error": "bad_result", "online": online}
    status = dict(result)
    status["online"] = online
    status.setdefault("sent", bool(status.get("acked") or status.get("queued")))
    return status


def _pending_signal_ids():
    with _SIGNAL_LOCK:
        active = _ACTIVE.get("state")
        messages = active["messages"] if active is not None else _load_messages()
        return [str(message.get("id"))
                for message in messages
                if isinstance(message.get("signal"), dict)
                and message["signal"].get("state") in (_SIGNAL_SENDING, _SIGNAL_PENDING)]


def _set_signal_state(message_id, state, status=None):
    """按 id 回写 signal 状态并落盘；页面打开时切主线程重绘气泡。"""
    status = status or {}
    patch = {"state": state, "updated": int(time.time() * 1000)}
    for key in ("ok", "queued", "acked", "timeout", "online", "error", "reason"):
        if key in status:
            patch[key] = status.get(key)
    payload = status.get("payload")
    if isinstance(payload, dict) and payload.get("stime"):
        patch["stime"] = payload.get("stime")

    with _SIGNAL_LOCK:
        active = _ACTIVE.get("state")
        context = _ACTIVE.get("context") if active is not None else None
        messages = active["messages"] if active is not None else _load_messages()
        target = None
        for message in messages:
            if str(message.get("id") or "") == message_id:
                signal = message.get("signal")
                if not isinstance(signal, dict):
                    signal = {}
                    message["signal"] = signal
                if state == _SIGNAL_SENT:
                    # 成功后清掉上一次失败的离线/超时错误，避免状态污染
                    signal.pop("error", None)
                signal.update(patch)
                target = message
                break
        if target is None:
            return False
        _save_messages(context, messages)

    if active is not None:
        def refresh():
            render = _ACTIVE.get("render")
            if render is not None:
                render()
            scroller = _ACTIVE.get("scroll_bottom")
            if scroller is not None:
                scroller()
        _post(refresh)
    return True


def _dispatch_signal(message_id):
    """发送点击入口：离线直接挂起，在线则后台发布并回写状态。"""
    if _online_broker_count() <= 0:
        _set_signal_state(message_id, _SIGNAL_PENDING,
                          {"ok": False, "sent": False, "error": "offline", "online": 0})
        _ensure_retry_tasks()
        return

    def worker():
        try:
            status = _publish_signal()
        except Exception as error:
            _log("publish signal", error)
            status = {"ok": False, "sent": False, "error": str(error)}
        if status.get("sent"):
            _set_signal_state(message_id, _SIGNAL_SENT, status)
            _drop_retry_tasks_if_idle()
        else:
            _set_signal_state(message_id, _SIGNAL_PENDING, status)
            _ensure_retry_tasks()

    threading.Thread(target=worker, daemon=True).start()


def _ensure_retry_tasks():
    """登记网络恢复事件任务与 5 分钟兜底任务（幂等）。"""
    try:
        import task_manager
        existing = task_manager.list_tasks()
        code = ("import feature_feedback\n"
                "print(feature_feedback.retry_pending_signals())")
        if _RETRY_NET_TASK not in existing:
            task_manager.add_event_task(
                _RETRY_NET_TASK, "network_online", code=code,
                debounce=_RETRY_DEBOUNCE_SECONDS,
            )
        if _RETRY_TICK_TASK not in existing:
            task_manager.add_task(
                _RETRY_TICK_TASK, interval=_RETRY_INTERVAL_SECONDS, code=code,
            )
    except Exception as error:
        _log("ensure retry tasks", error)


def _clear_retry_tasks():
    try:
        import task_manager
        existing = task_manager.list_tasks()
        for task_id in (_RETRY_NET_TASK, _RETRY_TICK_TASK):
            if task_id in existing:
                task_manager.remove_task(task_id)
    except Exception as error:
        _log("clear retry tasks", error)


def _drop_retry_tasks_if_idle():
    if not _pending_signal_ids():
        _clear_retry_tasks()


def retry_pending_signals():
    """补发入口：所有待发送消息共发一次信号（不含正文），成功后收敛状态并清任务。"""
    if not _RETRY_LOCK.acquire(False):
        return {"ok": True, "skipped": "busy"}
    try:
        pending = _pending_signal_ids()
        if not pending:
            _clear_retry_tasks()
            return {"ok": True, "pending": 0, "sent": 0}
        _wait_online()
        try:
            status = _publish_signal()
        except Exception as error:
            _log("retry signals", error)
            status = {"ok": False, "sent": False, "error": str(error)}
        state = _SIGNAL_SENT if status.get("sent") else _SIGNAL_PENDING
        for message_id in pending:
            _set_signal_state(message_id, state, status)
        if status.get("sent"):
            _drop_retry_tasks_if_idle()
        else:
            _ensure_retry_tasks()
        return {"ok": bool(status.get("ok")),
                "pending": len(pending),
                "sent": len(pending) if status.get("sent") else 0,
                "online": status.get("online"),
                "acked": status.get("acked"),
                "queued": status.get("queued"),
                "error": status.get("error")}
    finally:
        _RETRY_LOCK.release()


def _wait_online(wait_seconds=10):
    """等待 MQTT 至少一个 broker 在线；network_online 往往早于 MQTT 重连完成。"""
    deadline = time.time() + max(0, int(wait_seconds))
    while time.time() < deadline:
        if _online_broker_count() > 0:
            return True
        time.sleep(1.0)
    return _online_broker_count() > 0


def resume_pending_signals(wait_seconds=_RETRY_STARTUP_WAIT_SECONDS):
    """应用启动后补发一次：先等 MQTT 上线（最多 wait_seconds），再交给重试入口。"""
    _wait_online(wait_seconds)
    return retry_pending_signals()


def publish_signal_now(timeout=2.0):
    """RPC 入口：立即发布一次信号，返回 JSON（不改变消息状态）。"""
    try:
        status = _publish_signal(confirm_timeout=timeout)
    except Exception as error:
        _log("publish signal now", error)
        status = {"ok": False, "sent": False, "error": str(error)}
    return json.dumps(status, ensure_ascii=False)


def signal_status():
    """RPC 入口：返回在线数、待补发消息与重试任务概况。"""
    try:
        pending = _pending_signal_ids()
        try:
            import task_manager
            existing = task_manager.list_tasks()
        except Exception:
            existing = {}
        tasks = {}
        for task_id in (_RETRY_NET_TASK, _RETRY_TICK_TASK):
            task = existing.get(task_id)
            if isinstance(task, dict):
                tasks[task_id] = {
                    "kind": task.get("kind"),
                    "run_count": task.get("run_count"),
                    "last_run": task.get("last_run"),
                    "last_result": task.get("last_result"),
                }
        return json.dumps({"ok": True, "online": _online_broker_count(),
                           "pending": pending, "retry_tasks": tasks},
                          ensure_ascii=False)
    except Exception as error:
        _log("signal status", error)
        return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)


def build_view(context):
    from android.view import ViewGroup, Gravity
    from android.widget import Button, EditText, LinearLayout, ScrollView, TextView
    from android.text import InputType
    from android.graphics import Typeface

    match = ViewGroup.LayoutParams.MATCH_PARENT
    wrap = ViewGroup.LayoutParams.WRAP_CONTENT

    global _FILES_DIR
    if not _FILES_DIR:
        _FILES_DIR = _files_root(context)
    state = {"messages": _load_messages(context)}

    root = LinearLayout(context)
    root.setOrientation(LinearLayout.VERTICAL)
    root.setBackgroundColor(_color(_PAGE_BG))
    root.setLayoutParams(ViewGroup.LayoutParams(match, match))

    # ---- 顶部栏 ----
    bar = LinearLayout(context)
    bar.setOrientation(LinearLayout.HORIZONTAL)
    bar.setGravity(Gravity.CENTER_VERTICAL)
    bar.setBackgroundColor(_color(_BAR_BG))
    bar.setPadding(_dp(context, 10), 0, _dp(context, 12), 0)
    root.addView(bar, LinearLayout.LayoutParams(match, _dp(context, 52)))

    back = TextView(context)
    back.setText("\u2190")
    back.setTextSize(20)
    back.setTextColor(_color(_INK))
    back.setGravity(Gravity.CENTER)
    back.setBackground(_rounded(context, "#E8EAF6", 16))
    back.setLayoutParams(LinearLayout.LayoutParams(_dp(context, 32), _dp(context, 32)))
    bar.addView(back)

    title = TextView(context)
    title.setText("问题反馈")
    title.setTextSize(17)
    title.setTypeface(Typeface.DEFAULT, Typeface.BOLD)
    title.setTextColor(_color(_INK))
    title.setPadding(_dp(context, 10), 0, 0, 0)
    bar.addView(title, LinearLayout.LayoutParams(0, wrap, 1.0))

    # ---- 消息区 ----
    scroll = ScrollView(context)
    scroll.setFillViewport(True)
    root.addView(scroll, LinearLayout.LayoutParams(match, 0, 1.0))

    list_box = LinearLayout(context)
    list_box.setOrientation(LinearLayout.VERTICAL)
    list_box.setPadding(_dp(context, 12), _dp(context, 12), _dp(context, 12), _dp(context, 12))
    scroll.addView(list_box, ViewGroup.LayoutParams(match, wrap))
    # 键盘弹出/收起会改变 ScrollView 高度，布局一变化就重新贴底
    scroll.addOnLayoutChangeListener(
        _layout_class()(lambda: scroll.scrollTo(0, list_box.getHeight()))
    )

    # ---- 输入区 ----
    input_bar = LinearLayout(context)
    input_bar.setOrientation(LinearLayout.HORIZONTAL)
    input_bar.setGravity(Gravity.CENTER_VERTICAL)
    input_bar.setBackgroundColor(_color(_BAR_BG))
    input_bar.setPadding(_dp(context, 8), _dp(context, 8), _dp(context, 8), _dp(context, 8))
    root.addView(input_bar, LinearLayout.LayoutParams(match, wrap))

    pick = Button(context)
    pick.setText("图片")
    pick.setTextSize(13)
    input_bar.addView(pick, LinearLayout.LayoutParams(wrap, _dp(context, 42)))

    editor = EditText(context)
    editor.setHint("输入反馈内容...")
    editor.setTextSize(15)
    editor.setTextColor(_color(_INK))
    editor.setHintTextColor(_color(_MUTED))
    editor.setSingleLine(True)
    editor.setInputType(InputType.TYPE_CLASS_TEXT)
    editor.setBackground(_rounded(context, "#F1F3F5", 10))
    editor.setPadding(_dp(context, 12), _dp(context, 8), _dp(context, 12), _dp(context, 8))
    editor_params = LinearLayout.LayoutParams(0, _dp(context, 42), 1.0)
    editor_params.setMargins(_dp(context, 8), 0, _dp(context, 8), 0)
    input_bar.addView(editor, editor_params)

    send = Button(context)
    send.setText("发送")
    send.setTextSize(13)
    input_bar.addView(send, LinearLayout.LayoutParams(wrap, _dp(context, 42)))

    def scroll_bottom():
        try:
            def to_bottom():
                # fullScroll 依赖可聚焦子控件，气泡列表里没有，只能用内容高度直接贴底
                scroll.scrollTo(0, list_box.getHeight())
            scroll.post(_runnable_class()(to_bottom))
            # 首次 post 时布局还没量完，图片留言会停在半路；再补一帧确保真正贴底。
            scroll.postDelayed(_runnable_class()(to_bottom), 80)
        except Exception as error:
            _log("scroll bottom", error)

    def render():
        list_box.removeAllViews()
        messages = state["messages"]
        if not messages:
            hint = TextView(context)
            hint.setText("还没有反馈记录。消息只保存在本机，不会发送。")
            hint.setTextSize(13)
            hint.setTextColor(_color(_MUTED))
            hint.setGravity(Gravity.CENTER)
            hint.setBackground(_rounded(context, _HINT_BG, 10))
            hint.setPadding(_dp(context, 12), _dp(context, 12), _dp(context, 12), _dp(context, 12))
            params = LinearLayout.LayoutParams(match, wrap)
            params.setMargins(0, _dp(context, 16), 0, 0)
            list_box.addView(hint, params)
            return
        for message in messages:
            list_box.addView(_bubble(context, message), LinearLayout.LayoutParams(match, wrap))

    def append(message):
        with _SIGNAL_LOCK:
            state["messages"].append(message)
            _save_messages(context, state["messages"])
        render()
        scroll_bottom()

    def send_text():
        text = str(editor.getText().toString()).strip()
        if not text:
            return
        editor.setText("")
        message = {
            "id": _new_message_id(),
            "type": "text",
            "text": text,
            "ts": int(time.time() * 1000),
            "signal": {"state": _SIGNAL_SENDING},
        }
        append(message)
        _dispatch_signal(message["id"])

    def pick_image():
        try:
            from android.content import Intent
            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("image/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            context.startActivityForResult(intent, _REQUEST_PICK_IMAGE)
        except Exception as error:
            _log("pick image", error)

    back.setOnClickListener(_click_class()(lambda: context.finish()))
    send.setOnClickListener(_click_class()(send_text))
    pick.setOnClickListener(_click_class()(pick_image))

    render()
    scroll_bottom()

    _ACTIVE["context"] = context
    _ACTIVE["append"] = append
    _ACTIVE["state"] = state
    _ACTIVE["render"] = render
    _ACTIVE["scroll_bottom"] = scroll_bottom
    return root


def on_activity_result(context, request_code, result_code, data):
    """Activity 回调入口：把用户选择的系统图片复制到本地并追加为图片消息。"""
    if int(request_code) != _REQUEST_PICK_IMAGE:
        return
    if data is None or int(result_code) != -1:
        return
    try:
        from android.graphics import Bitmap, BitmapFactory
        from java.io import FileOutputStream

        uri = data.getData()
        if uri is None:
            return
        stream = context.getContentResolver().openInputStream(uri)
        bitmap = BitmapFactory.decodeStream(stream)
        stream.close()
        if bitmap is None:
            return
        _, images_dir = _data_dir(context)
        path = os.path.join(images_dir, "img_%d.jpg" % int(time.time() * 1000))
        output = FileOutputStream(path)
        bitmap.compress(Bitmap.CompressFormat.JPEG, 88, output)
        output.close()
        append = _ACTIVE.get("append")
        if append is not None:
            append({"type": "image", "path": path, "ts": int(time.time() * 1000)})
    except Exception as error:
        _log("activity result", error)


def push_message(context, sender, text):
    """RPC 入口：向本机聊天记录追加一条消息（默认来自反馈作者），不联网发送。

    页面未打开时只落盘，下次打开可见；页面打开时投递主线程追加气泡并滚到底。
    """
    try:
        text = str(text or "").strip()
        if not text:
            return json.dumps({"ok": False, "error": "empty_text"})
        sender = str(sender or "author")
        message = {
            "type": "text",
            "text": text,
            "sender": sender,
            "ts": int(time.time() * 1000),
        }
        with _SIGNAL_LOCK:
            messages = _load_messages(context)
            messages.append(message)
            _save_messages(context, messages)
        state = _ACTIVE.get("state")
        if state is not None:
            def update():
                state["messages"] = messages
                render = _ACTIVE.get("render")
                if render is not None:
                    render()
                scroller = _ACTIVE.get("scroll_bottom")
                if scroller is not None:
                    scroller()
            _post(update)
        return json.dumps(
            {"ok": True, "count": len(messages), "message": message},
            ensure_ascii=False,
        )
    except Exception as error:
        _log("push message", error)
        return json.dumps({"ok": False, "error": str(error)})


def release_page(context=None):
    """Activity 销毁时解绑活跃页面，避免 RPC 写进已销毁的 View。"""
    _ACTIVE.clear()
