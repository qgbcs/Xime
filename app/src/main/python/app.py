import os
import sys
import traceback
import threading
import json
import shutil
import tempfile
from pathlib import Path

MAX_LOG_BYTES = 100 * 1024
MAX_LOG_LINES = 1000

try:
    from java import jclass
    RpcUiController = jclass("com.kingzcheung.xime.util.RpcUiController")
except Exception:  # pragma: no cover - chaquopy/python-only test environments may not expose Java.
    RpcUiController = None

def load_rpc_config(config_path):
    defaults = {
        "http_enabled": False,
        "http_port": 1144,
        "http_host": "0.0.0.0",
        "http_key": "",
        "mqtt_enabled": True,
    }
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            values = json.load(config_file)
        if isinstance(values, dict):
            defaults.update(values)
    except (OSError, ValueError):
        pass
    return defaults


def _ensure_multi_mqtt_path():
    submodule_root = os.path.join(os.path.dirname(__file__), "multi_mqtt")
    if submodule_root not in sys.path:
        sys.path.insert(0, submodule_root)


def get_rpc_config_path(files_dir=None):
    _ensure_multi_mqtt_path()
    import server_mqtt
    _configure_rpc_config_path(server_mqtt, files_dir)
    return os.fspath(server_mqtt.RPC_CONFIG_PATH)


def _configure_rpc_config_path(server_mqtt, files_dir, requested_path=None):
    if not files_dir:
        if requested_path is not None:
            raise ValueError("保存 RPC 配置路径需要应用私有目录")
        return

    files_path = Path(files_dir)
    path_state = files_path / "rpc_config_path.txt"
    previous_path = Path(server_mqtt.RPC_CONFIG_PATH)
    if requested_path is not None:
        path_value = str(requested_path).strip()
        if not path_value:
            raise ValueError("RPC 配置路径不能为空")
        config_path = Path(path_value)
    elif path_state.is_file():
        path_value = path_state.read_text(encoding="utf-8").strip()
        if not path_value:
            raise ValueError(f"RPC 配置路径记录为空: {path_state}")
        config_path = Path(path_value)
    else:
        config_path = files_path / "rpc.json"

    if not config_path.is_absolute():
        raise ValueError("RPC 配置路径必须是绝对路径")
    config_path = Path(os.path.abspath(os.fspath(config_path)))
    if config_path == path_state:
        raise ValueError("RPC 配置文件不能与路径记录文件相同")
    if config_path.exists() and not config_path.is_file():
        raise ValueError(f"RPC 配置路径不是文件: {config_path}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with server_mqtt._RPC_CONFIG_LOCK:
        if (
            config_path != previous_path
            and not config_path.exists()
            and previous_path.is_file()
        ):
            shutil.copy2(previous_path, config_path)

        fd, temp_path = tempfile.mkstemp(
            prefix=".rpc-config-path.", suffix=".tmp", dir=files_path
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as path_file:
                path_file.write(os.fspath(config_path))
                path_file.write("\n")
                path_file.flush()
                os.fsync(path_file.fileno())
            os.replace(temp_path, path_state)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

        server_mqtt.RPC_CONFIG_PATH = config_path


def get_rpc_settings(files_dir=None):
    _ensure_multi_mqtt_path()
    import server_mqtt
    _configure_rpc_config_path(server_mqtt, files_dir)

    stored = server_mqtt._read_rpc_config_document()
    try:
        effective = server_mqtt.rpc_config_get()
    except ValueError:
        fallback_stored = dict(stored)
        if not fallback_stored.get("request_topic"):
            fallback_stored.pop("request_topic", None)
        if not fallback_stored.get("pub_key"):
            fallback_stored.pop("pub_key", None)
        effective = server_mqtt._json_rpc_config(
            server_mqtt._normalize_rpc_config(fallback_stored)
        )
    server = globals().get("mqtt_server")
    runtime_pub_key = getattr(
        getattr(server, "mqtt_net", None), "server_public_key_bytes", None
    )
    if isinstance(runtime_pub_key, bytes):
        runtime_pub_key = runtime_pub_key.decode("utf-8", errors="replace")
    if not isinstance(runtime_pub_key, str) or not runtime_pub_key:
        runtime_pub_key = effective["pub_key"] or ""
    settings = {
        "http_enabled": False,
        "http_port": 1144,
        "http_host": "0.0.0.0",
        "http_key": "",
        "mqtt_enabled": bool(stored.get("mqtt_enabled", True)),
        "request_topic": effective["request_topic"],
        "mqtt_reply_topic": getattr(
            server, "reply_topic", server_mqtt.DEFAULT_REPLY_TOPIC
        ),
        "pub_key": runtime_pub_key,
    }
    settings.update({
        key: stored[key]
        for key in (
            "http_enabled",
            "http_port",
            "http_host",
            "http_key",
            "mqtt_reply_topic",
        )
        if key in stored
    })
    settings["request_topic"] = (
        stored.get("request_topic") or effective["request_topic"]
    )
    settings["pub_key"] = (
        (stored.get("pub_key") or "")
        if "pub_key" in stored
        else runtime_pub_key
    )
    return json.dumps(settings, ensure_ascii=False)


def save_rpc_settings(settings_json, files_dir=None, config_path=None):
    settings = json.loads(settings_json)
    if not isinstance(settings, dict):
        raise TypeError("RPC 设置必须是 JSON object")
    port = settings.get("http_port", 1144)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("HTTP 端口必须在 1 到 65535 之间")

    _ensure_multi_mqtt_path()
    import server_mqtt
    _configure_rpc_config_path(server_mqtt, files_dir, config_path)

    mqtt_updates = {}
    if settings.get("request_topic"):
        mqtt_updates["request_topic"] = settings["request_topic"]
    if "pub_key" in settings:
        pub_key = settings["pub_key"]
        if pub_key is not None and not isinstance(pub_key, str):
            raise ValueError("MQTT 公钥必须是字符串或 null")
        mqtt_updates["pub_key"] = (
            pub_key.strip() if pub_key is not None else None
        )
    if mqtt_updates:
        server_mqtt._write_rpc_config_updates(mqtt_updates)

    path = server_mqtt.RPC_CONFIG_PATH
    path = os.fspath(path)
    with server_mqtt._RPC_CONFIG_LOCK:
        stored = server_mqtt._read_rpc_config_document()
        for key in (
            "http_enabled",
            "http_port",
            "http_host",
            "http_key",
            "mqtt_enabled",
            "mqtt_reply_topic",
        ):
            if key in settings:
                stored[key] = settings[key]

        parent = os.path.dirname(path) or "."
        fd, temp_path = tempfile.mkstemp(
            prefix=".rpc-settings.", suffix=".json", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as config_file:
                json.dump(stored, config_file, ensure_ascii=False, indent=2)
                config_file.write("\n")
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
    return True


class LimitedLogFile:
    def __init__(self, path, max_bytes=MAX_LOG_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.file = open(path, "a+b", buffering=0)

    def write(self, value):
        data = value.encode("utf-8") if isinstance(value, str) else value
        with self.lock:
            self.file.seek(0, 2)
            if self.file.tell() + len(data) > self.max_bytes:
                keep = self.max_bytes // 2
                self.file.seek(-min(keep, self.file.tell()), 2)
                tail = self.file.read()
                self.file.seek(0)
                self.file.truncate()
                self.file.write(tail)
            self.file.seek(0, 2)
            self.file.write(data)

    def flush(self):
        with self.lock:
            self.file.flush()


class Tee:
    def __init__(self, stream, sinks):
        self.stream = stream
        if sinks is None:
            self.sinks = []
        elif isinstance(sinks, (list, tuple)):
            self.sinks = [sink for sink in sinks if sink is not None]
        else:
            self.sinks = [sinks]

    def write(self, value):
        self.stream.write(value)
        for sink in self.sinks:
            sink.write(value)
            sink.flush()

    def flush(self):
        self.stream.flush()
        for sink in self.sinks:
            sink.flush()


class MemoryLogProxy:
    def __init__(self):
        self.buffer = []

    def write(self, value):
        if not value:
            return
        text = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
        if RpcUiController is not None:
            try:
                RpcUiController.appendLog(text)
            except Exception:
                pass
        self.buffer.append(text)
        total_chars = sum(len(part) for part in self.buffer)
        if total_chars > MAX_LOG_BYTES:
            trimmed = ''.join(self.buffer)
            trimmed = trimmed[-MAX_LOG_BYTES:]
            self.buffer = trimmed.splitlines(keepends=True)
        if len(self.buffer) > MAX_LOG_LINES:
            self.buffer = self.buffer[-MAX_LOG_LINES:]

    def flush(self):
        pass


def start(log_path, files_dir=None):
    global mqtt_server,http_server,gms
    memory_sink = MemoryLogProxy()
    file_sink = None
    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        file_sink = LimitedLogFile(log_path)

    sinks = [memory_sink]
    if file_sink is not None:
        sinks.append(file_sink)

    sys.stdout = Tee(sys.__stdout__, sinks)
    sys.stderr = Tee(sys.__stderr__, sinks)
    print("Chaquopy started")
    try:
        import runtime_pip
        # 先加 multi_mqtt，再 configure：把 filesDir/site-packages 顶到 sys.path[0]，
        # 保证“放入 site-packages 的同名模块”优先于 APK assets 与 multi_mqtt。
        _ensure_multi_mqtt_path()
        runtime_pip.configure(files_dir)
        # Import after stdout/stderr redirection so logging.basicConfig in the
        # MQTT and HTTP modules writes into the same log shown by the settings UI.
        import logging
        import server_http,server_mqtt
        _configure_rpc_config_path(server_mqtt, files_dir)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            force=True,
        )
        config = load_rpc_config(server_mqtt.RPC_CONFIG_PATH)
        mqtt_config = server_mqtt.load_rpc_config()
        mqtt_server = None
        gms = None
        if config.get('http_enabled', False):
            http_server= server_http.start_rpc_server(
                port=int(config.get('http_port', 1144)),
                ip=str(config.get('http_host', '0.0.0.0')),
                key=str(config.get('http_key', '')),
                globals=globals(),
                locals=locals(),
            )
        else:
            http_server = None
            
        if config.get("mqtt_enabled", True):
            reply_topic = str(config.get("mqtt_reply_topic") or server_mqtt.DEFAULT_REPLY_TOPIC)
            mqtt_server = server_mqtt.MQTTServer(
                server_public_key_bytes=mqtt_config["pub_key"],
                reply_topic=reply_topic,
                globals=globals(),
            )
            gms = mqtt_server  # 设置页统计按钮使用此 MQTT 服务实例
            mqtt_server.start(block=False)

        import task_manager
        task_manager.init(files_dir or ".")

        # 反馈信号：注入私有目录并后台补发上次网络异常时没发成功的记录
        import feature_feedback
        feature_feedback.init(files_dir)

        print(f"[app.py] {config} loaded, HTTP={http_server} MQTT={mqtt_server} ")
        return True
    except Exception:
        traceback.print_exc()
        return False


def reload_module(name, files_dir=None):
    """按 sys.path 优先级重新导入模块，实现“放入 site-packages 即热更新”。

    RPC 用法：
      import app; app.reload_module("task_manager")

    先把 filesDir/site-packages 顶到 sys.path[0]，删除 sys.modules 中的旧
    模块后重新 import；task_manager 还会重新 init 恢复任务调度。
    """
    import importlib
    try:
        import runtime_pip
        base = files_dir or runtime_pip.FILES_DIR
    except Exception:
        base = files_dir
    if not base:
        return {"ok": False, "error": "files_dir 未知"}
    override = os.path.join(base, "site-packages")
    target = os.path.join(override, name.replace(".", os.sep) + ".py")
    pkg = os.path.join(override, *name.split("."))
    if not (os.path.isfile(target) or os.path.isdir(pkg)):
        return {"ok": False, "error": "site-packages 中没有该模块",
                "path": target}
    if not sys.path or sys.path[0] != override:
        while override in sys.path:
            sys.path.remove(override)
        sys.path.insert(0, override)
        sys.path_importer_cache.pop(override, None)
    old = sys.modules.pop(name, None)
    try:
        mod = importlib.import_module(name)
    except Exception:
        if old is not None:
            sys.modules[name] = old
        return {"ok": False, "error": traceback.format_exc(limit=3)}
    if name == "task_manager":
        mod.init(base)
    return {"ok": True, "module": name,
            "file": getattr(mod, "__file__", None),
            "override": target if os.path.isfile(target) else pkg}


def _py_fmt_ms(ms):
    """epoch 毫秒 -> 本地时间 'YYYY-MM-DD HH:MM:SS.mmm'；空值返回 None。"""
    if not ms:
        return None
    import time
    sec, milli = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec)) + ".%03d" % milli


def _py_file_stats(path, max_bytes=16 * 1024 * 1024):
    """单文件的大小/时间/sha256；超过 max_bytes 时退化为元信息 hash。"""
    import hashlib
    try:
        st = os.stat(path)
        if st.st_size > max_bytes:
            meta = "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
            return {"bytes": st.st_size,
                    "time": _py_fmt_ms(int(st.st_mtime * 1000)),
                    "sha256": "meta:" + hashlib.sha256(meta.encode()).hexdigest()}
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return {"bytes": st.st_size,
                "time": _py_fmt_ms(int(st.st_mtime * 1000)),
                "sha256": h.hexdigest()}
    except OSError:
        return {"error": "unreadable"}


def _py_tree_stats(path, max_bytes=16 * 1024 * 1024, max_files=5000):
    """目录递归统计：文件数/总字节/最新时间/内容 sha256（超限退化为元信息 hash）。"""
    import hashlib
    entries = []
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append((os.path.relpath(full, path), full, st.st_size, st.st_mtime))
    entries.sort()
    total = sum(e[2] for e in entries)
    newest = max([e[3] for e in entries] or [0])
    h = hashlib.sha256()
    for rel, full, size, mtime in entries:
        h.update(("%s|%d|%d\n" % (rel, size, int(mtime))).encode("utf-8", "replace"))
    if len(entries) <= max_files and total <= max_bytes:
        for rel, full, size, mtime in entries:
            try:
                with open(full, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                pass
        digest = h.hexdigest()
    else:
        digest = "meta:" + h.hexdigest()
    return {"files": len(entries), "bytes": total,
            "time": _py_fmt_ms(int(newest * 1000)) if newest else None,
            "sha256": digest}


def _py_builtin_summary():
    """汇总 APK 内置 Python 模块：读 assets/chaquopy/app.imy 里的 pyc。

    返回模块数量、总大小、APK 修改时间与整包 sha256，并附每个模块的

    字节数与 CRC32（无需解压运行）。

    """
    import io
    import zipfile
    import hashlib
    from java import jclass
    src = str(jclass("com.chaquo.python.Python")
              .getPlatform().getApplication().getApplicationInfo().sourceDir)
    with zipfile.ZipFile(src) as apk:
        data = apk.read("assets/chaquopy/app.imy")
    modules = []
    with zipfile.ZipFile(io.BytesIO(data)) as imy:
        for item in imy.infolist():
            if item.is_dir() or not item.filename.endswith(".pyc"):
                continue
            modules.append({"name": item.filename[:-4].replace("/", "."),
                            "bytes": item.file_size,
                            "crc32": "%08x" % item.CRC})
    modules.sort(key=lambda m: m["name"])
    try:
        apk_mtime = int(os.path.getmtime(src) * 1000)
    except OSError:
        apk_mtime = None
    return {"count": len(modules), "bytes": len(data),
            "time": _py_fmt_ms(apk_mtime),
            "sha256": hashlib.sha256(data).hexdigest(),
            "modules": modules}


def _py_site_packages(files_dir):
    """filesDir/site-packages 顶层每个包/模块的 版本/大小/时间/hash。"""
    sp = os.path.join(files_dir, "site-packages")
    result = {"path": sp, "exists": os.path.isdir(sp), "entries": []}
    if not result["exists"]:
        return result
    try:
        names = sorted(os.listdir(sp))
    except OSError:
        return result
    versions = {}
    for name in names:
        if not name.endswith(".dist-info"):
            continue
        base = name[:-len(".dist-info")]
        project = dist_version = None
        try:
            with open(os.path.join(sp, name, "METADATA"),
                      encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("Name: "):
                        project = line[6:].strip()
                    elif line.startswith("Version: "):
                        dist_version = line[9:].strip()
                    elif not line.strip():
                        break
        except OSError:
            pass
        if project:
            versions[project.lower()] = dist_version
        if "-" in base:
            guess, ver = base.rsplit("-", 1)
            versions.setdefault(guess.lower(), ver or dist_version)
    for name in names:
        if name == "__pycache__" or name.endswith(".dist-info"):
            continue
        full = os.path.join(sp, name)
        if os.path.isdir(full):
            stats = _py_tree_stats(full)
            kind = "package"
        else:
            stats = _py_file_stats(full)
            kind = "module"
        if "error" in stats:
            stats = {"bytes": None, "time": None, "sha256": None}
        result["entries"].append({
            "name": name, "kind": kind,
            "version": versions.get(name.lower()),
            "bytes": stats["bytes"], "time": stats["time"],
            "sha256": stats["sha256"],
        })
    return result


def build_extra():
    """供 Kotlin RpcUiController.buildInfo() 合并的 Python 侧信息。


    含内置 py 模块汇总与 filesDir/site-packages 各包的版本/大小/时间/hash。

    返回 JSON 字符串；任何异常只写入返回值，绝不抛出，避免拖垮 buildInfo。

    """
    try:
        import runtime_pip
        files_dir = runtime_pip.FILES_DIR
    except Exception:
        files_dir = None
    if not files_dir:
        try:
            from java import jclass
            files_dir = str(jclass("com.chaquo.python.Python")
                            .getPlatform().getApplication()
                            .getFilesDir().toString())
        except Exception:
            files_dir = None
    pymod = {}
    try:
        pymod["builtin"] = _py_builtin_summary()
    except Exception:
        pymod["builtin"] = {"error": traceback.format_exc(limit=2)}
    try:
        pymod["site_packages"] = (_py_site_packages(files_dir) if files_dir
                                   else {"exists": False, "entries": []})
    except Exception:
        pymod["site_packages"] = {"error": traceback.format_exc(limit=2)}
    try:
        return json.dumps({"python": pymod}, ensure_ascii=False)
    except Exception:
        return "{}"


def mqtt_stats_report():
    """设置页按钮入口：执行 gms.mqtt_net.stats.get_report() 并把报告打印进运行日志。"""
    server = globals().get("gms")
    if server is None:
        print("[app.py] MQTT 未启动，暂无连接质量统计")
        return False
    print(gms.mqtt_net.stats.get_report())
    return True
