"""触摸手势 RPC 封装：直接调用 XimeAccessibilityService 的静态方法。

先决条件：系统设置里启用本输入法的“辅助功能”服务，或用 adb 写入
``enabled_accessibility_services``。所有函数返回 JSON 字符串。

调用失败时返回值里带 ``connected`` / ``last``（Kotlin 侧最近一次失败原因）
和 ``error``，可直接定位问题；整体体检用 ``diagnose()``。

用法（RPC 的 code 里）：

    import feature_touch
    print(feature_touch.status())
    print(feature_touch.tap(540, 1000))
    print(feature_touch.long_press(540, 1000, 800))
    print(feature_touch.swipe(540, 1600, 540, 400, 300))
    print(feature_touch.drag(300, 800, 900, 800, 600, 400))
    print(feature_touch.pinch(540, 1000, 134, 400, 400))
    print(feature_touch.diagnose())
"""

import json
import time

SERVICE_CLASS_NAME = "com.kingzcheung.xime.service.XimeAccessibilityService"

_service_class = None


def _service():
    global _service_class
    if _service_class is None:
        from java import jclass

        _service_class = jclass(SERVICE_CLASS_NAME)
    return _service_class


def _dump(op, ok, **extra):
    payload = {"ok": bool(ok), "op": op}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _last_result():
    try:
        return json.loads(_service().lastResult())
    except Exception as e:
        return {"error": str(e)}


def _failure(op, error=None, **extra):
    """统一失败返回：带连接状态和 Kotlin 侧最近一次失败原因。"""
    payload = {"ok": False, "op": op}
    if error:
        payload["error"] = error
    try:
        payload["connected"] = bool(_service().isConnected())
    except Exception as e:
        payload["connected"] = False
        payload.setdefault("error", str(e))
    payload["last"] = _last_result()
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _run(op, call, **extra):
    try:
        ok = bool(call())
    except Exception as e:
        return _failure(op, str(e), **extra)
    if ok:
        return _dump(op, True, **extra)
    return _failure(op, None, **extra)


def status():
    """返回服务连接状态、系统支持上限和最近一次手势结果。"""
    try:
        return json.dumps(
            {
                "ok": bool(_service().isConnected()),
                "status": json.loads(_service().status()),
                "last": _last_result(),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return _failure("status", str(e))


def diagnose():
    """环境体检：类是否存在、服务是否连接、无障碍开关、已启用服务、最近失败原因。"""
    info = {"ok": True, "op": "diagnose", "hints": []}
    try:
        cls = _service()
        info["class_present"] = True
        info["connected"] = bool(cls.isConnected())
        info["status"] = json.loads(cls.status())
        info["last"] = _last_result()
    except Exception as e:
        info["ok"] = False
        info["class_present"] = False
        info["connected"] = False
        info["error"] = str(e)
        info["hints"].append("当前 APK 不含辅助功能服务类，需要安装最新构建的包")

    try:
        from java import jclass

        ctx = jclass("android.app.ActivityThread").currentApplication()
        pkg = str(ctx.getPackageName())
        info["package"] = pkg
        info["sdk"] = int(jclass("android.os.Build$VERSION").SDK_INT)
        component = pkg + "/" + SERVICE_CLASS_NAME
        info["component"] = component

        Secure = jclass("android.provider.Settings$Secure")
        resolver = ctx.getContentResolver()
        enabled = Secure.getString(resolver, "enabled_accessibility_services") or ""
        acc_on = int(Secure.getInt(resolver, "accessibility_enabled"))
        info["accessibility_enabled"] = acc_on
        info["enabled_accessibility_services"] = enabled
        info["component_enabled"] = component in enabled

        if not info.get("class_present", False):
            pass
        elif not info.get("connected", False):
            if not info["component_enabled"]:
                info["hints"].append(
                    "辅助功能服务未启用：设置-无障碍-已安装的服务里打开，或执行 adb shell settings put secure "
                    "enabled_accessibility_services " + component
                )
            else:
                info["hints"].append(
                    "已写入启用列表但服务未绑定：可能被系统/省电策略拦截，或无障碍开关为 0（accessibility_enabled="
                    + str(acc_on) + "）"
                )
        elif info.get("last", {}).get("lastError"):
            info["hints"].append("服务已连接但手势失败，lastError=" + str(info["last"].get("lastError")))
    except Exception as e:
        info["secure_error"] = str(e)

    return json.dumps(info, ensure_ascii=False)


def gesture(strokes):
    """通用手势。strokes 可为 JSON 字符串或 dict：

    {"strokes": [{"points": [[x, y], ...], "start": 0, "duration": 300,
                   "willContinue": false, "continuePrevious": false}]}

    points 至少一个点；continuePrevious=True 表示链接到上一条 stroke（同一根手指不抬指），
    此时上一条的 willContinue 必须为 true。
    """
    try:
        raw = strokes if isinstance(strokes, str) else json.dumps(strokes)
        result = json.loads(_service().gesture(raw))
        result["op"] = "gesture"
        result["connected"] = bool(_service().isConnected())
        if not result.get("ok"):
            result["last"] = _last_result()
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return _failure("gesture", str(e))


def dump_ui(max_nodes=400, include_empty=False):
    """读取当前界面节点树（可跨应用），返回 {"ok","pkg","count","nodes":[...]}。"""
    try:
        result = json.loads(_service().dumpUi(int(max_nodes), bool(include_empty)))
        result["op"] = "dump_ui"
        result["connected"] = bool(_service().isConnected())
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return _failure("dump_ui", str(e))


def find_text(text, max_nodes=400, exact=False, include_empty=False):
    """按文本或内容描述查找节点，返回命中列表（含点击用的中心点 x/y）。"""
    try:
        data = json.loads(dump_ui(max_nodes, include_empty))
        if not data.get("ok"):
            return json.dumps(data, ensure_ascii=False)
        wanted = str(text)
        hits = []
        for node in data.get("nodes") or []:
            node_text = node.get("t") or ""
            node_desc = node.get("d") or ""
            if exact:
                matched = wanted in (node_text, node_desc)
            else:
                matched = wanted in node_text or wanted in node_desc
            if not matched:
                continue
            left, top, right, bottom = node.get("b") or (0, 0, 0, 0)
            hits.append(
                {
                    "text": node_text,
                    "desc": node_desc,
                    "id": node.get("id"),
                    "class": node.get("c"),
                    "bounds": node.get("b"),
                    "x": int((left + right) // 2),
                    "y": int((top + bottom) // 2),
                    "clickable": node.get("k"),
                    "editable": node.get("e"),
                }
            )
        return json.dumps(
            {"ok": True, "op": "find_text", "query": wanted, "count": len(hits), "hits": hits},
            ensure_ascii=False,
        )
    except Exception as e:
        return _failure("find_text", str(e))


def tap_text(text, index=0, max_nodes=400, exact=False, duration_ms=80):
    """找到文本命中的节点并点击其中心点；index 用于同名节点排序。"""
    found = json.loads(find_text(text, max_nodes, exact))
    if not found.get("ok"):
        return json.dumps(found, ensure_ascii=False)
    hits = found.get("hits") or []
    if not hits:
        return _failure("tap_text", "text_not_found:" + str(text), query=str(text))
    if index < 0 or index >= len(hits):
        return _failure("tap_text", "index_out_of_range:%s/%s" % (index, len(hits)))
    hit = hits[index]
    result = json.loads(tap(hit["x"], hit["y"], duration_ms))
    result["op"] = "tap_text"
    result["query"] = str(text)
    result["hit"] = hit
    return json.dumps(result, ensure_ascii=False)


def wait_text(text, timeout=10.0, interval=0.3, max_nodes=400, exact=False):
    """轮询等待文本出现，命中直接返回节点，超时返回失败。"""
    start = time.time()
    deadline = start + float(timeout)
    while True:
        found = json.loads(find_text(text, max_nodes, exact))
        if found.get("ok") and found.get("hits"):
            found["op"] = "wait_text"
            found["waited_ms"] = int((time.time() - start) * 1000)
            return json.dumps(found, ensure_ascii=False)
        if time.time() >= deadline:
            return _failure("wait_text", "text_not_found:" + str(text), timeout=timeout)
        time.sleep(float(interval))


def global_action(action):
    """全局动作：1=返回 2=Home 3=最近任务 4=通知栏 5=快捷设置 6=电源菜单 8=锁屏 9=截图。"""
    return _run("global_action", lambda: _service().globalAction(int(action)), action=int(action))


def start_activity(action):
    """用 Intent 拉起界面，如 android.settings.SETTINGS；后台启动可能被系统拦截。"""
    return _run("start_activity", lambda: _service().startActivityAction(str(action)), action=str(action))


def tap(x, y, duration_ms=80):
    """点击 / 短按，duration_ms 建议 50~150。"""
    return _run(
        "tap",
        lambda: _service().tap(float(x), float(y), int(duration_ms)),
        x=float(x),
        y=float(y),
        duration_ms=int(duration_ms),
    )


def long_press(x, y, duration_ms=600):
    """长按，duration_ms 建议 500~1500。"""
    return _run(
        "long_press",
        lambda: _service().longPress(float(x), float(y), int(duration_ms)),
        x=float(x),
        y=float(y),
        duration_ms=int(duration_ms),
    )


def swipe(x1, y1, x2, y2, duration_ms=300):
    """直线滑动，duration_ms 越小滑动越快。"""
    return _run(
        "swipe",
        lambda: _service().swipe(float(x1), float(y1), float(x2), float(y2), int(duration_ms)),
        from_point=[float(x1), float(y1)],
        to_point=[float(x2), float(y2)],
        duration_ms=int(duration_ms),
    )


def drag(x1, y1, x2, y2, hold_ms=600, duration_ms=300):
    """拖拽：先在起点长按 hold_ms，再在 duration_ms 内移动到目标，全程不松手。"""
    return _run(
        "drag",
        lambda: _service().drag(
            float(x1), float(y1), float(x2), float(y2), int(hold_ms), int(duration_ms)
        ),
        from_point=[float(x1), float(y1)],
        to_point=[float(x2), float(y2)],
        hold_ms=int(hold_ms),
        duration_ms=int(duration_ms),
    )


def pinch(center_x, center_y, start_radius, end_radius, duration_ms=400):
    """双指缩放：start_radius < end_radius 为放大，反之为缩小。"""
    return _run(
        "pinch",
        lambda: _service().pinch(
            float(center_x),
            float(center_y),
            float(start_radius),
            float(end_radius),
            int(duration_ms),
        ),
        center=[float(center_x), float(center_y)],
        start_radius=float(start_radius),
        end_radius=float(end_radius),
        duration_ms=int(duration_ms),
    )
