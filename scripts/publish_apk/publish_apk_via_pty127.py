#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_via_pty127.py

一句话流程：
  1) 通过本机 http://127.0.0.1:1188 的 /r=<表达式> RPC，在远端后台执行
     publish_apk_all.py --no-gitee，上传 npm + GitHub git + pythonanywhere 三个渠道；
  2) 轮询远端日志，等 git 通道推送成功并打印 ghfast 加速链接（通常最快，约几秒）；
  3) 拿到链接立刻在本地下载 APK，按远端 sha256/size 校验（此时远端其它渠道继续跑）；
  4) 本地通过 Gitee API 上传发行版附件（避开美国远端到 Gitee 的慢链路）；
  5) 等远端 publish_all 全部结束 rc=0，再执行 check_apk_all.py 校验 6 条渠道。

自动接手（本地窗口关掉也不丢任务）：
  - 每次运行先扫远端 /tmp/apk_publish_flow/publish_all_* 与 check_*；
  - 最近 24 小时内已 rc=0、日志 sha 与当前 APK 一致的 publish_all 直接复用链接，
    不重新上传；仍在跑的则按字节偏移接手日志，只等它结束；
  - 最近 24 小时内已完成且 6 渠道都与当前 APK 一致的 check 直接复用结果，
    不重复下载校验；仍在跑的同样接手等待；
  - 本地 APK 大小/sha 与远端一致时跳过下载；Gitee 同名附件大小一致时跳过上传。
  所以“窗口误关”后重跑只会补做尚未完成的那一步，不会重复上传。

远端任务在哪跑：
  - 命令经 1188 写进远端登录 shell（tmux pane），但脚本用
    `setsid sh -c '<cmd>; echo $? > run.rc' > run.log 2>&1 < /dev/null &` 把它
    彻底脱离前台作业，所以人在远端 `jobs` 里看不到、也没有前台任务；
    `ps -ef | grep publish_apk_all.py` 能看到，日志在
    /tmp/apk_publish_flow/<名字>_<token>/run.log。
  - 本地脚本每 4~5 秒发一次 1188 RPC（轮询），按字节偏移取日志增量，
    每条 RPC 都是一次「取一段日志」的小命令，不是长驻连接。
  - base64 只是 ai_bridge 在**命令本身含控制字符**时对命令行的转义（把任意
    字节安全地敲进远端 shell），返回值 `out` 一直是 `_strip_ansi` 之后的
    纯文本。旧版本脚本自己额外把日志 base64 化再传回，已去掉：现在远端先把
    增量写临时文件、用 `wc -c` 报真实字节数再 `cat` 明文，偏移依旧精确。

连接方式只有本机 1188：
    GET http://127.0.0.1:1188/r=<单条 Python 表达式>
  命令统一用 __import__('json').dumps(ai_bridge.run(<命令>, timeout=...), ensure_ascii=False)
  包装，让远端在本机人类终端里的常驻 shell 执行；脚本自身不保存任何远端连接信息。

用法：
    C:\\QGB\\miniforge3\\python.exe C:\\test\\github\\rpc\\publish_apk_via_pty127.py

发布参数读自 rpc.json 的 publish_apk 段（键名全小写）：
    C:\\test\\github\\multi_mqtt\\rpc.json
    {"publish_apk": {
        "rpc_base": "https://.../-",
        "gitee": {"api": ..., "repo": ..., "tag": ..., "token": ...},
        "npm":   {"package": ..., "registry": ..., "tag": ..., "name": ..., "token": ...}}}
  rpc_base / npm 会作为 --rpc-base / --npm-* 传给远端 publish_apk_all.py
  （npm token 走 NPM_TOKEN 环境变量，不出现在命令行和日志里），
  rpc_base / npm / gitee 会作为 --pa-base / --npm-* / --gitee-* 传给 check_apk_all.py；
  文件缺失 / JSON 坏 / 类型错只告警并回落远端默认值，不打断主流程。
  --gitee-repo / --gitee-token / 环境变量 GITEE_TOKEN 可覆盖文件里的 Gitee 值。

常用开关：
    --check-only          只复用/执行远端 check，不发布、不下载、不上传 Gitee
    --dry-run             只预检，不执行任何写操作
    --skip-publish-all    跳过第 1 步（需同时给 --apk-url）
    --apk-url URL         指定远端已发布的下载链接（配合 --skip-publish-all）
    --skip-download       跳过第 3 步（本地已有同名 APK）
    --skip-gitee          跳过第 4 步
    --skip-check          跳过第 5 步
    --force-publish       远端 APK 未变化也强制重发 3 个渠道
    --force-gitee         Gitee 同名附件即使大小相同也删掉重传
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---- 唯一的连接入口：本机 1188 HTTP RPC ----
RPC_BASE = "http://127.0.0.1:1188"

DEFAULT_OUT_DIR = r"c:\test\github\rpc"
# Gitee / rpc_base / npm 都不写死在脚本里，统一读这个配置文件的 publish_apk 段。
DEFAULT_RPC_CONFIG = r"c:\test\github\multi_mqtt\rpc.json"

REMOTE_ROOT = "/root/build_xime_home/xime"
REMOTE_SCRIPTS = REMOTE_ROOT + "/scripts/publish_apk"
REMOTE_OUT = REMOTE_ROOT + "/out"
REMOTE_WORK = "/tmp/apk_publish_flow"
APK_GLOB = "com.qgb.xime-*-arm64-v8a.apk"

UA = "xime-publish-flow/1.0"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
GIT_RC0_RE = re.compile(r"\[git\][^\n]*推送结束：rc=0")
# APK 与仓库 HEAD 完全一致时，git 通道会跳过推送、不打印链接：
#   [git] APK 与 SHA256SUMS.txt 都与仓库 HEAD 一致（blob ...），跳过 amend/推送...
GIT_SKIP_RE = re.compile(r"\[git\][^\n]*跳过 amend/推送")
# 不硬编码任何下载域名：只从远端打印的“加速链接/APK 加速”那一行里取 URL。
URL_RE = re.compile(r"https?://\S+")


def extract_accel_url(text: str) -> "str | None":
    """取远端日志里“加速”行打印的下载链接（成功推送或 dry-run 都会打印）。"""
    for line in text.splitlines():
        if "加速" not in line:
            continue
        m = URL_RE.search(line)
        if m:
            return m.group(0).rstrip(".,;)]'\"")
    return None

_PRINT_LOCK = threading.Lock()
_LOG_FH = None


class FlowError(Exception):
    pass


class BusyError(FlowError):
    pass


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%.0f B" % n
        n /= 1024.0
    return "%.1f GiB" % n


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def emit(text: str) -> None:
    """打印并写入本次运行日志（远端输出也走这里）。"""
    with _PRINT_LOCK:
        print(text, flush=True)
        if _LOG_FH is not None:
            try:
                _LOG_FH.write(text + "\n")
                _LOG_FH.flush()
            except Exception:
                pass


def log(msg: str = "") -> None:
    emit("[%s] %s" % (stamp(), msg))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class ProgressMeter:
    """每 3 秒打印已处理/总量、即时/平均速度；停滞也照报。"""

    def __init__(self, label: str, total: int, interval: float = 3.0) -> None:
        self.label = label
        self.total = int(total or 0)
        self.done = 0
        self.phase = "准备"
        self.interval = interval
        self.started = time.monotonic()
        self.moved = self.started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "ProgressMeter":
        self._thread.start()
        return self

    def set(self, done: "int | None" = None, phase: "str | None" = None) -> None:
        with self._lock:
            if done is not None:
                self.done = int(done)
            if phase is not None:
                self.phase = phase
            self.moved = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        last_n, last_t = 0, self.started
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            with self._lock:
                phase, done, total, moved = self.phase, self.done, self.total, self.moved
            elapsed = now - self.started
            idle = now - moved
            if idle >= self.interval * 2:
                emit("[%s]   %s %s：已 %.0fs 无新字节（累计 %.0fs，已处理 %s/%s）"
                     % (stamp(), self.label, phase, idle, elapsed,
                        human_size(done), human_size(total)))
                last_n, last_t = done, now
                continue
            inst = (done - last_n) / (now - last_t) if now > last_t else 0.0
            avg = done / elapsed if elapsed > 0 else 0.0
            pct = ("%.1f%%" % (100.0 * done / total)) if total else "-"
            emit("[%s]   %s %s %s/%s (%s) 即时 %s/s 平均 %s/s 用时 %.0fs"
                 % (stamp(), self.label, phase, human_size(done), human_size(total),
                    pct, human_size(inst), human_size(avg), elapsed))
            last_n, last_t = done, now


# =============================== 1188 RPC ===============================

class Bridge:
    """本机 1188 的 /r= 表达式通道；命令统一交给远端 ai_bridge.run 执行。"""

    def __init__(self, base: str = RPC_BASE):
        self.base = base.rstrip("/")

    def _expr(self, expr: str, timeout: float) -> str:
        url = self.base + "/r=" + urllib.parse.quote(expr, safe="")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise FlowError("1188 RPC 执行失败（HTTP %s）：%s" % (exc.code, detail))
        except Exception as exc:  # noqa: BLE001
            raise FlowError("连接 %s 失败：%s: %s"
                            % (self.base, type(exc).__name__, exc))
        if status != 200:
            raise FlowError("1188 RPC 返回 HTTP %s：%s" % (status, body[:400]))
        return body

    def status(self) -> dict:
        body = self._expr(
            "__import__('json').dumps(ai_bridge.status(), ensure_ascii=False)", 20.0)
        try:
            obj = json.loads(body)
        except ValueError:
            raise FlowError("1188 status 返回不是 JSON：%s" % body[:300])
        if not isinstance(obj, dict):
            raise FlowError("1188 status 返回类型异常：%r" % (obj,))
        return obj

    def run(self, cmd: str, timeout: float = 60.0,
            acquire_timeout: float = 10.0) -> dict:
        expr = ("__import__('json').dumps(ai_bridge.run(%s, timeout=%r, "
                "acquire_timeout=%r), ensure_ascii=False)"
                % (repr(cmd), float(timeout), float(acquire_timeout)))
        body = self._expr(expr, float(timeout) + float(acquire_timeout) + 20.0)
        try:
            obj = json.loads(body)
        except ValueError:
            raise FlowError("1188 run 返回不是 JSON：%s" % body[:300])
        if not isinstance(obj, dict):
            raise FlowError("1188 run 返回类型异常：%r" % (obj,))
        if obj.get("busy"):
            raise BusyError("远端常驻 shell 正忙（另一条命令尚未结束）")
        return obj


def connect_bridge() -> Bridge:
    """确认 1188 在线、PTY 已附着、命令通道可执行。"""
    b = Bridge()
    try:
        st = b.status()
    except FlowError as exc:
        raise FlowError("连不上 %s。请先在本机终端启动 pty_client_mqtt（默认 1188 控制口）。"
                        "原始错误：%s" % (RPC_BASE, exc))

    if not st.get("attached"):
        raise FlowError("1188 已响应，但远端 PTY 未附着（attached=false，end_reason=%r）；"
                        "请在人类终端里重新启动 pty_client_mqtt 窗口"
                        % (st.get("end_reason"),))

    deadline = time.monotonic() + 20.0
    while st.get("busy") and time.monotonic() < deadline:
        time.sleep(2.0)
        st = b.status()
    if st.get("busy"):
        raise FlowError("远端常驻 shell 持续忙（busy=true），等不到执行权")

    log("1188 已连接：sid=%s brokers=%s/%s pane=%s channel=%s"
        % (st.get("sid"), st.get("brokers_online"), st.get("brokers_total"),
           st.get("ai_pane"), st.get("ai_channel")))

    r = b.run("printf '__RPC_OK__\\n'; id -u; hostname; pwd", timeout=45)
    text = r.get("out") or ""
    if "__RPC_OK__" not in text:
        raise FlowError("1188 命令通道回包异常（rc=%s，timed_out=%s）：%s"
                        % (r.get("rc"), r.get("timed_out"), text[:300]))
    lines = [ln for ln in text.splitlines() if ln.strip()]
    uid = lines[1] if len(lines) > 1 else "?"
    host = lines[2] if len(lines) > 2 else "?"
    cwd = lines[3] if len(lines) > 3 else "?"
    log("远端命令通道正常：host=%s uid=%s cwd=%s" % (host, uid, cwd))
    if uid != "0":
        log("警告：远端当前不是 root（uid=%s），发布脚本可能需要 root 权限" % uid)
    return b


def remote_preflight(b: Bridge) -> None:
    cmd = (
        "test -f %s/publish_apk_all.py && echo publish_all_ok; "
        "test -f %s/check_apk_all.py && echo check_all_ok; "
        "test -d %s && echo out_ok; "
        "command -v setsid >/dev/null && echo setsid_ok; "
        "command -v pgrep >/dev/null && echo pgrep_ok; "
        "python3 --version"
        % (REMOTE_SCRIPTS, REMOTE_SCRIPTS, REMOTE_OUT)
    )
    r = b.run(cmd, timeout=60)
    text = r.get("out") or ""
    for marker in ("publish_all_ok", "check_all_ok", "out_ok", "setsid_ok"):
        if marker not in text:
            raise FlowError("远端预检失败，缺少 %s。输出：%s" % (marker, text[:400]))
    last = text.splitlines()[-1] if text.splitlines() else "python3"
    log("远端预检通过：%s" % last)


def remote_apk_info(b: Bridge) -> dict:
    cmd = (
        "cd %s && f=$(ls -t %s 2>/dev/null | head -1) && "
        "[ -n \"$f\" ] && printf '%%s\\n' \"$f\" && stat -c %%s \"$f\" && "
        "sha256sum \"$f\" | cut -d' ' -f1"
        % (REMOTE_OUT, APK_GLOB)
    )
    r = b.run(cmd, timeout=120)
    if r.get("rc") != 0:
        raise FlowError("读取远端最新 APK 失败（rc=%s）：%s"
                        % (r.get("rc"), (r.get("out") or "")[:300]))
    lines = [ln.strip() for ln in (r.get("out") or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        raise FlowError("远端 APK 信息输出不完整：%r" % (r.get("out") or "")[:300])
    name, size_s, sha = lines[0], lines[1], lines[2].lower()
    try:
        size = int(size_s)
    except ValueError:
        raise FlowError("远端 APK 大小不是整数：%r" % size_s)
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise FlowError("远端 APK sha256 格式不对：%r" % sha)
    info = {"name": name, "path": "%s/%s" % (REMOTE_OUT, name),
            "size": size, "sha256": sha}
    log("远端 APK：%s  %s  sha256=%s" % (name, human_size(size), sha))
    return info


def remote_git_links(b: Bridge, remote_python: str) -> str:
    """git 通道因内容未变化跳过推送时，用远端脚本 --dry-run 打印同一组链接。"""
    cmd = "cd %s && %s publish_apk_git.py --dry-run" % (REMOTE_SCRIPTS, remote_python)
    r = b.run(cmd, timeout=180)
    text = r.get("out") or ""
    if r.get("rc") != 0:
        raise FlowError("远端 publish_apk_git.py --dry-run 失败（rc=%s）：%s"
                        % (r.get("rc"), text[:400]))
    m = extract_accel_url(text)
    if not m:
        raise FlowError("远端 git dry-run 未打印加速链接：%s" % text[:400])
    log("远端 dry-run 取得加速链接：%s" % m)
    return m


def remote_file_size(b: Bridge, path: str) -> int:
    r = b.run("stat -c %s %s 2>/dev/null || echo 0" % (path, path), timeout=30)
    text = (r.get("out") or "").strip()
    try:
        return int(text.splitlines()[0]) if text else 0
    except (ValueError, IndexError):
        return 0


def remote_process_alive(b: Bridge, pattern: str) -> bool:
    """pgrep -f 判断远端任务是否还在跑。

    模式首字符包成 [p]：承载本命令的 shell 命令行里也带这段文字，用方括号后
    它自身不再匹配该正则（p] 与 p 后跟 u 不同），避免“自己把自己当活进程”。
    """
    probe = pattern
    if len(probe) >= 1 and probe[0].isalnum():
        probe = "[" + probe[0] + "]" + probe[1:]
    r = b.run("pgrep -f %s >/dev/null 2>&1 && echo __YES__ || echo __NO__"
              % shlex.quote(probe), timeout=30)
    return "__YES__" in (r.get("out") or "")


def list_recent_jobs(b: Bridge, prefix: str, limit: int = 5) -> list:
    """列出 /tmp/apk_publish_flow/<prefix>_* 的最近任务目录、日志年龄与 rc。"""
    cmd = (
        "n=$(date +%s); for d in $(ls -dt " + REMOTE_WORK + "/" + prefix + "_* "
        "2>/dev/null | head -" + str(limit) + "); do "
        "m=$(stat -c %Y \"$d/run.log\" 2>/dev/null || echo 0); "
        "rc=$(cat \"$d/run.rc\" 2>/dev/null || echo none); "
        "echo \"$d|$m|$rc|$n\"; done"
    )
    r = b.run(cmd, timeout=60)
    jobs = []
    for line in (r.get("out") or "").splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        d, mtime, rc, now = [p.strip() for p in parts]
        try:
            age = max(0.0, float(now) - float(mtime))
        except ValueError:
            age = 1e9
        rc_val = int(rc) if rc.lstrip("-").isdigit() else None
        jobs.append({"dir": d, "age": age, "rc": rc_val})
    return jobs


def job_log_sha(b: Bridge, log_path: str) -> "str | None":
    r = b.run("grep -a 'SHA256' %s 2>/dev/null | tail -n 1" % log_path, timeout=30)
    m = re.search(r"[0-9a-f]{64}", r.get("out") or "")
    return m.group(0) if m else None


def job_log_url(b: Bridge, log_path: str) -> "str | None":
    r = b.run("grep -a '加速' %s 2>/dev/null | tail -n 2" % log_path, timeout=30)
    return extract_accel_url(r.get("out") or "")


def read_remote_json(b: Bridge, path: str) -> "dict | None":
    r = b.run("test -f %s && cat %s" % (path, path), timeout=60)
    if r.get("rc") != 0:
        return None
    text = (r.get("out") or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def check_json_ok(obj: dict, apk_info: dict, cfg: dict = None) -> bool:
    """上次 check 的每条渠道都与当前 APK 一致，且测的就是当前配置的入口，才可复用。"""
    cfg = cfg or {}
    chans = obj.get("channels") or []
    if not chans:
        return False
    for c in chans:
        if c.get("verdict") != "一致":
            return False
        if str(c.get("sha256") or "").lower() != apk_info["sha256"]:
            return False

    by_key = dict((c.get("key"), c) for c in chans)
    base = (cfg.get("rpc_base") or "").rstrip("/").rsplit("/-", 1)[0]
    if base:
        pa = by_key.get("pa") or {}
        if not str(pa.get("url") or "").startswith(base + "/"):
            return False
    npm = cfg.get("npm") or {}
    if npm.get("package") or npm.get("name"):
        nc = by_key.get("npm") or {}
        url = str(nc.get("url") or "")
        if npm.get("package") and ("unpkg.com/%s@" % npm["package"]) not in url:
            return False
        if npm.get("name") and not url.endswith("/" + npm["name"]):
            return False
    gitee = cfg.get("gitee") or {}
    if gitee.get("repo") or gitee.get("tag"):
        gc = by_key.get("gitee") or {}
        url = str(gc.get("url") or "")
        if gitee.get("repo"):
            try:
                if "/%s/" % repo_slug(gitee["repo"]) not in url:
                    return False
            except FlowError:
                return False
        if gitee.get("tag") and ("/%s/" % gitee["tag"]) not in url:
            return False
    return True


def save_check_json(local_path: Path, obj: dict, info: dict) -> None:
    local_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    info["json_local"] = str(local_path)
    chans = obj.get("channels") or []
    info["channels"] = [
        {"key": c.get("key"), "verdict": c.get("verdict"),
         "ok": c.get("ok"), "size": c.get("size"),
         "sha256": c.get("sha256"), "error": c.get("error")}
        for c in chans
    ]
    for c in chans:
        log("校验 %-7s %-6s 大小=%s %s"
            % (c.get("key"), c.get("verdict"),
               c.get("size"), (c.get("error") or "")[:160]))
    info["all_consistent"] = bool(
        all(c.get("verdict") == "一致" for c in chans))


# =============================== 远端后台任务 ===============================

class RemoteJob:
    """setsid 后台执行远端长命令；按字节偏移增量读日志，等 rc 文件。

    日志块由远端先写临时文件、用 wc -c 报告真实字节数再 cat，所以本地看到的是
    可读原文（不再 base64），偏移由远端字节数保证精确，不受 ai_bridge 去 ANSI 影响。
    """

    def __init__(self, bridge: Bridge, name: str, inner: str, timeout: float,
                 token: str, poll: float = 4.0, chunk: int = 30000):
        self.bridge = bridge
        self.name = name
        self.inner = inner
        self.timeout = float(timeout)
        self.poll = float(poll)
        self.chunk = int(chunk)
        self.run_dir = "%s/%s_%s" % (REMOTE_WORK, name, token)
        self.log_path = self.run_dir + "/run.log"
        self.rc_path = self.run_dir + "/run.rc"
        self.offset = 0
        self.rc = None
        self.ghfast_url = None
        self.git_ok = False
        self.git_skipped = False
        self.git_seconds = None
        self._scan_tail = ""

    def start(self) -> None:
        inner = "%s; echo $? > %s" % (self.inner, self.rc_path)
        wrapper = "setsid sh -c %s" % shlex.quote(inner)
        cmd = (
            "mkdir -p %s && rm -f %s %s && "
            "(%s > %s 2>&1 < /dev/null &) && echo __STARTED__"
            % (self.run_dir, self.log_path, self.rc_path,
               wrapper, self.log_path)
        )
        r = self.bridge.run(cmd, timeout=30)
        if "__STARTED__" not in (r.get("out") or ""):
            raise FlowError("远端后台任务启动失败（%s）：%s"
                            % (self.name, (r.get("out") or "")[:300]))
        log("远端任务已启动：%s（日志 %s）" % (self.name, self.log_path))

    def attach(self, run_dir: str, offset: "int | None" = None) -> None:
        """接手一个已经存在的远端任务目录（本地窗口关掉后任务仍在远端跑）。"""
        self.run_dir = run_dir
        self.log_path = run_dir + "/run.log"
        self.rc_path = run_dir + "/run.rc"
        self.offset = int(offset or 0)
        log("接手远端任务：%s（日志 %s，从第 %d 字节继续）"
            % (self.name, self.log_path, self.offset))

    def pump(self) -> "tuple[int | None, str]":
        chunk_file = self.run_dir + "/.chunk"
        cmd = (
            "O=" + str(self.offset + 1) + "; "
            "if [ -f " + self.rc_path + " ]; then echo __RC__$(cat " + self.rc_path + "); fi; "
            "tail -c +$O " + self.log_path + " 2>/dev/null | head -c " + str(self.chunk) +
            " > " + chunk_file + "; "
            "echo __SIZE__$(wc -c <" + chunk_file + "); "
            "cat " + chunk_file
        )
        r = self.bridge.run(cmd, timeout=30)
        text = r.get("out") or ""
        rc = None
        size = None
        idx = 0
        lines = text.split("\n")
        if lines and lines[0].startswith("__RC__"):
            token = lines[0][len("__RC__"):].strip()
            try:
                rc = int(token)
            except ValueError:
                rc = -1
            idx = 1
        for i in range(idx, len(lines)):
            if lines[i].startswith("__SIZE__"):
                token = lines[i][len("__SIZE__"):].strip()
                if token.isdigit():
                    size = int(token)
                    idx = i + 1
                break
        if size is None:
            raise FlowError("远端日志块缺少 __SIZE__ 标记：%s" % text[:200])
        self.offset += size
        return rc, "\n".join(lines[idx:])

    def _emit(self, text: str) -> None:
        if not text:
            return
        clean = ANSI_RE.sub("", text)
        # 跨块扫描缓冲：head -c 可能把标记行切成两半，拼上上一块尾巴再匹配。
        scan = self._scan_tail + clean
        self._scan_tail = scan[-4096:]
        if self.ghfast_url is None:
            url = extract_accel_url(scan)
            if url:
                self.ghfast_url = url
                log("解析到远端加速链接：%s" % url)
        if not self.git_ok and GIT_RC0_RE.search(scan):
            self.git_ok = True
            log("远端 git 通道推送成功（rc=0）")
        elif not self.git_ok and GIT_SKIP_RE.search(scan):
            self.git_ok = True
            self.git_skipped = True
            log("远端 git 通道内容未变化，跳过推送")
        shown = clean.replace("\r\n", "\n").replace("\r", "\n")
        for line in shown.split("\n"):
            line = line.rstrip()
            if not line:
                continue
            if len(line) > 400:
                line = line[:400] + "…"
            emit("  [%s] %s" % (self.name, line))

    def wait_for_git(self, timeout: float) -> str:
        """等 git 通道结束；返回 "url"（已打印加速链接）或 "skip"（内容未变化跳过推送）。"""
        started = time.monotonic()
        deadline = started + float(timeout)
        drained_after_rc = 0
        while True:
            try:
                rc, text = self.pump()
            except BusyError:
                time.sleep(1.0)
                if time.monotonic() > deadline:
                    raise FlowError("等待 git 通道时远端 shell 一直忙，超时")
                continue
            self._emit(text)
            if rc is not None:
                self.rc = rc
                if text:
                    drained_after_rc = 0
                else:
                    drained_after_rc += 1
            if self.ghfast_url is not None:
                self.git_seconds = time.monotonic() - started
                return "url"
            if self.git_skipped:
                self.git_seconds = time.monotonic() - started
                return "skip"
            if rc is not None and drained_after_rc >= 1:
                raise FlowError(
                    "远端任务已结束（rc=%s），但日志里没有 ghfast 链接；日志：%s"
                    % (rc, self.log_path))
            if time.monotonic() > deadline:
                raise FlowError("等待远端 git 通道超过 %.0fs；日志：%s"
                                % (timeout, self.log_path))
            time.sleep(self.poll)

    def wait(self) -> int:
        started = time.monotonic()
        deadline = started + self.timeout
        while True:
            try:
                rc, text = self.pump()
            except BusyError:
                time.sleep(1.0)
                if time.monotonic() > deadline:
                    raise FlowError("等待远端任务 %s 时 shell 一直忙，超时" % self.name)
                continue
            self._emit(text)
            if rc is not None:
                self.rc = rc
                if not text:
                    break
            if time.monotonic() > deadline:
                raise FlowError(
                    "远端任务 %s 超过 %.0fs 仍未结束；它可能仍在远端运行，日志：%s"
                    % (self.name, self.timeout, self.log_path))
            time.sleep(self.poll)
        log("远端任务结束：%s rc=%s（本轮等待 %.0fs，日志 %s）"
            % (self.name, self.rc, time.monotonic() - started, self.log_path))
        return int(self.rc)


# =============================== 接手上次任务 ===============================

def resume_or_start_publish(b: Bridge, apk_info: dict, args, work_token: str,
                            cfg: dict):
    """优先接手最近 24 小时内跑完/仍在跑的 publish_all，否则新启动。

    返回 (job, url, outcome)：
      job=None, outcome="reused"  上次已成功完成，直接复用链接，不再重发；
      job!=None, outcome="attached" 接手仍在远端跑的进程；
      job!=None, outcome="started"  新启动。
    """
    expect_base = ("[all] rpc: " + cfg["rpc_base"]) if cfg.get("rpc_base") else ""
    for j in list_recent_jobs(b, "publish_all", 5):
        if j["age"] > 86400:
            continue
        log_path = j["dir"] + "/run.log"
        url = job_log_url(b, log_path)
        sha = job_log_sha(b, log_path)
        if j["rc"] is not None:
            if expect_base and not remote_log_contains(b, log_path, expect_base):
                continue
            if j["rc"] == 0 and url and (not sha or sha == apk_info["sha256"]):
                log("捡起上次 publish_all：%.0fs 前完成 rc=0，sha 一致，复用链接"
                    % j["age"])
                return None, url, "reused"
            continue
        if remote_process_alive(b, "publish_apk_all.py"):
            if expect_base and not remote_log_contains(b, log_path, expect_base):
                continue
            job = RemoteJob(b, "publish_all", "", args.timeout_publish,
                            token=work_token, poll=5.0)
            if url:
                job.attach(j["dir"], offset=remote_file_size(b, log_path))
                job.ghfast_url = url
                job.git_seconds = round(j["age"], 1)
                log("捡起仍在运行的 publish_all，已有链接，只等它结束")
                return job, url, "attached"
            job.attach(j["dir"], offset=0)
            outcome = job.wait_for_git(args.timeout_git)
            if outcome == "url":
                return job, job.ghfast_url, "attached"
            return job, remote_git_links(b, args.remote_python), "attached"

    inner = ("cd %s && %s%s publish_apk_all.py --no-gitee%s"
             % (REMOTE_SCRIPTS, publish_env_prefix(cfg), args.remote_python,
                publish_all_flags(cfg, args)))
    job = RemoteJob(b, "publish_all", inner,
                    timeout=args.timeout_publish, token=work_token, poll=5.0)
    job.start()
    outcome = job.wait_for_git(args.timeout_git)
    if outcome == "url":
        return job, job.ghfast_url, "started"
    return job, remote_git_links(b, args.remote_python), "started"


def resume_or_run_check(b: Bridge, apk_info: dict, args, work_token: str,
                        check_json_local: Path, cfg: dict) -> dict:
    """优先复用/接手最近 24 小时内的 check，否则新跑一次轻量校验。"""
    for j in list_recent_jobs(b, "check", 5):
        if j["age"] > 86400:
            continue
        log_path = j["dir"] + "/run.log"
        json_path = j["dir"] + "/check.json"
        if j["rc"] is not None:
            obj = read_remote_json(b, json_path)
            if j["rc"] == 0 and obj and check_json_ok(obj, apk_info, cfg):
                log("捡起上次 check：%.0fs 前完成，6 渠道都与当前 APK 一致，"
                    "不再重复下载校验" % j["age"])
                info = {"rc": 0, "json": json_path, "reused": True}
                save_check_json(check_json_local, obj, info)
                return info
            continue
        if remote_process_alive(b, "check_apk_all.py"):
            job = RemoteJob(b, "check", "", args.timeout_check,
                            token=work_token, poll=5.0)
            job.attach(j["dir"], offset=remote_file_size(b, log_path))
            log("捡起仍在运行的 check（日志 %s），继续等待" % log_path)
            rc = job.wait()
            info = {"rc": rc, "log": log_path, "json": json_path, "attached": True}
            obj = read_remote_json(b, json_path)
            if obj:
                save_check_json(check_json_local, obj, info)
            return info

    check_dir = "%s/check_%s" % (REMOTE_WORK, work_token)
    remote_json = check_dir + "/check.json"
    inner = (
        "mkdir -p %s && cd %s && %s check_apk_all.py --ref-sha %s --ref-size %d "
        "--timeout %.0f --json %s%s"
        % (check_dir, REMOTE_SCRIPTS, args.remote_python, apk_info["sha256"],
           apk_info["size"], args.check_channel_timeout, remote_json,
           check_flags(cfg))
    )
    job = RemoteJob(b, "check", inner, timeout=args.timeout_check,
                    token=work_token, poll=5.0)
    job.start()
    rc = job.wait()
    info = {"rc": rc, "log": job.log_path, "json": remote_json}
    obj = read_remote_json(b, remote_json)
    if obj:
        save_check_json(check_json_local, obj, info)
    else:
        log("警告：读取远端 check JSON 失败（%s）" % remote_json)
    return info


# =============================== 本地下载 ===============================

def apk_name_from_url(url: str) -> str:
    """从远端给出的下载链接解析发布文件名（不硬编码任何仓库/文件名）。"""
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise FlowError("无法从远端链接解析 APK 文件名：%r" % url)
    return name


def download_once(url: str, dest: Path, expected_size: int, timeout: float,
                  meter: ProgressMeter) -> int:
    """带 Range 续传的单次下载；网络断流时留在 .part 上重试。"""
    pos = dest.stat().st_size if dest.exists() else 0
    if expected_size and pos > expected_size:
        dest.unlink(missing_ok=True)
        pos = 0
    last_err = None
    for net_try in range(4):
        if net_try:
            time.sleep(2.0 + net_try)
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if pos:
            headers["Range"] = "bytes=%d-" % pos
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if status == 416:
                    if expected_size and dest.exists() and dest.stat().st_size == expected_size:
                        return expected_size
                    dest.unlink(missing_ok=True)
                    pos = 0
                    last_err = "HTTP 416，重置本地文件"
                    continue
                if status == 200 and pos:
                    # 服务端忽略了 Range：必须从头写，否则会重复拼接。
                    pos = 0
                if status not in (200, 206):
                    raise FlowError("下载 HTTP %s：%s" % (status, url))
                meter.set(done=pos, phase="下载")
                with open(dest, "ab" if pos else "wb") as fh:
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        fh.write(chunk)
                        pos += len(chunk)
                        meter.set(done=pos)
            if expected_size and pos < expected_size:
                last_err = "连接提前结束，已下载 %d/%d" % (pos, expected_size)
                continue
            if expected_size and pos > expected_size:
                dest.unlink(missing_ok=True)
                pos = 0
                last_err = "文件比预期大，重置"
                continue
            return pos
        except FlowError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = "%s: %s" % (type(exc).__name__, exc)
            if dest.exists():
                pos = dest.stat().st_size
            continue
    raise FlowError("下载失败（断流重试 4 次后）：%s；URL=%s" % (last_err, url))


def download_apk(url: str, expected: dict, dest: Path, timeout: float,
                 work_token: str) -> dict:
    """用远端打印的加速链接下载；校验大小与 sha256，必要时破缓存/回源重试。"""
    expected_size = int(expected["size"])
    expected_sha = str(expected["sha256"]).lower()

    variants = []
    sep = "&" if "?" in url else "?"
    variants.append(url + sep + "flow=" + urllib.parse.quote(work_token))
    variants.append(url)
    parts = urllib.parse.urlparse(url)
    # 代理链接形如 https://<proxy>/https://github.com/...：去掉代理前缀作回源兜底。
    if parts.netloc and parts.path.startswith("/http"):
        direct = parts.path.lstrip("/")
        if direct and direct not in variants:
            variants.append(direct)

    dest.parent.mkdir(parents=True, exist_ok=True)
    started_all = time.monotonic()
    meter = ProgressMeter("download", expected_size).start()
    last_reason = ""
    attempts = 0
    try:
        for variant in variants:
            for _ in range(2):
                attempts += 1
                if dest.exists():
                    dest.unlink()
                meter.set(done=0, phase="连接 %s" % (urllib.parse.urlparse(variant).netloc or "-"))
                log("下载 %s（第 %d 次尝试）" % (variant, attempts))
                try:
                    download_once(variant, dest, expected_size, timeout, meter)
                    actual_size = dest.stat().st_size
                    actual_sha = sha256_file(dest)
                    if actual_size == expected_size and actual_sha == expected_sha:
                        elapsed = time.monotonic() - started_all
                        avg = actual_size / elapsed if elapsed > 0 else 0.0
                        log("下载完成：%s  %s  %.1fs  平均 %s/s  sha256=%s"
                            % (dest, human_size(actual_size), elapsed,
                               human_size(avg), actual_sha))
                        return {"url": variant, "path": str(dest),
                                "size": actual_size, "sha256": actual_sha,
                                "elapsed": round(elapsed, 1),
                                "attempts": attempts}
                    last_reason = ("大小/哈希不符：got %d/%s，want %d/%s"
                                   % (actual_size, actual_sha[:12],
                                      expected_size, expected_sha[:12]))
                    log("校验不符：%s，等待后换地址/重试（可能是代理缓存旧文件）"
                        % last_reason)
                except FlowError as exc:
                    last_reason = str(exc)
                    log("下载尝试失败：%s" % last_reason)
                time.sleep(10)
        raise FlowError("下载最终失败：%s" % last_reason)
    finally:
        meter.stop()


# =============================== rpc.json 配置 ===============================

PUBLISH_SECTION = {
    "rpc_base": "",
    "gitee": {"api": "", "repo": "", "tag": "", "token": ""},
    "npm": {"package": "", "registry": "", "tag": "", "name": "", "token": ""},
}


def read_publish_apk_config(path: Path) -> dict:
    """读 rpc.json 的 publish_apk 段（键名全小写）。

    结构：
        "publish_apk": {
          "rpc_base": "https://.../-",
          "gitee": {"api": ..., "repo": ..., "tag": ..., "token": ...},
          "npm":   {"package": ..., "registry": ..., "tag": ..., "name": ...}
        }

    文件缺失、JSON 坏、段类型写错、某键不是字符串，都只告警并回落到
    空值/已读到的部分：这些配置只影响对应的渠道，不能打断主流程。
    """
    cfg = {"rpc_base": "", "gitee": dict(PUBLISH_SECTION["gitee"]),
           "npm": dict(PUBLISH_SECTION["npm"])}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log("警告：读取 %s 失败（%s），publish_apk 配置不可用" % (path, exc))
        return cfg
    try:
        document = json.loads(text)
    except ValueError as exc:
        log("警告：%s 不是合法 JSON（%s），publish_apk 配置不可用" % (path, exc))
        return cfg
    if not isinstance(document, dict):
        log("警告：%s 根节点不是 JSON object，publish_apk 配置不可用" % path)
        return cfg
    section = document.get("publish_apk")
    if section is None:
        log("警告：%s 缺少 publish_apk 段" % path)
        return cfg
    if not isinstance(section, dict):
        log("警告：%s 的 publish_apk 段不是 JSON object，已忽略" % path)
        return cfg

    value = section.get("rpc_base")
    if isinstance(value, str) and value.strip():
        cfg["rpc_base"] = value.strip()
    elif value is not None and not isinstance(value, str):
        log("警告：publish_apk.rpc_base 不是字符串，已忽略")

    for group in ("gitee", "npm"):
        sub = section.get(group)
        if sub is None:
            log("警告：publish_apk 缺少 %s 段" % group)
            continue
        if not isinstance(sub, dict):
            log("警告：publish_apk.%s 不是 JSON object，已忽略" % group)
            continue
        for key in cfg[group]:
            item = sub.get(key)
            if item is None:
                continue
            if not isinstance(item, str):
                log("警告：publish_apk.%s.%s 不是字符串，已忽略" % (group, key))
                continue
            item = item.strip()
            if item:
                cfg[group][key] = item
    cfg["gitee"]["api"] = cfg["gitee"]["api"].rstrip("/")
    return cfg


def publish_all_flags(cfg: dict, args) -> str:
    """把 publish_apk 段转成 publish_apk_all.py 的命令行参数。"""
    flags = []
    if cfg["rpc_base"]:
        flags += ["--rpc-base", shlex.quote(cfg["rpc_base"])]
    else:
        # 没配 rpc_base 就只关掉 RPC 通道，npm/git 照常跑（不打断主流程）。
        flags.append("--no-rpc")
    for flag, key in (("--npm-package", "package"),
                      ("--npm-registry", "registry"),
                      ("--npm-tag", "tag"),
                      ("--npm-name", "name")):
        if cfg["npm"].get(key):
            flags += [flag, shlex.quote(cfg["npm"][key])]
    if getattr(args, "force_publish", False):
        flags.append("--force")
    return (" " + " ".join(flags)) if flags else ""


def publish_env_prefix(cfg: dict) -> str:
    """把 npm token 用环境变量传给远端，避免出现在命令行/日志里。"""
    token = (cfg["npm"].get("token") or "").strip()
    return ("NPM_TOKEN=%s " % shlex.quote(token)) if token else ""


def check_flags(cfg: dict) -> str:
    """把 publish_apk 段转成 check_apk_all.py 的命令行参数。"""
    flags = []
    if cfg["rpc_base"]:
        flags += ["--pa-base", shlex.quote(cfg["rpc_base"])]
    else:
        # 没配 pa 入口就跳过 pa 渠道，其余 5 条照常校验。
        flags += ["--skip", "pa"]
    if cfg["npm"].get("package"):
        flags += ["--npm-package", shlex.quote(cfg["npm"]["package"])]
    if cfg["npm"].get("name"):
        flags += ["--npm-name", shlex.quote(cfg["npm"]["name"])]
    if cfg["gitee"].get("repo"):
        flags += ["--gitee-repo", shlex.quote(repo_slug(cfg["gitee"]["repo"]))]
    if cfg["gitee"].get("tag"):
        flags += ["--gitee-tag", shlex.quote(cfg["gitee"]["tag"])]
    return (" " + " ".join(flags)) if flags else ""


def remote_log_contains(b: Bridge, log_path: str, needle: str) -> bool:
    cmd = ("grep -qF %s %s && echo __HIT__ || echo __MISS__"
           % (shlex.quote(needle), shlex.quote(log_path)))
    r = b.run(cmd, timeout=30)
    return "__HIT__" in (r.get("out") or "")


def repo_slug(repo: str) -> str:
    text = repo.strip().rstrip("/")
    text = re.sub(r"^git@[^:]+:", "", text)
    text = re.sub(r"^https?://[^/]+/", "", text)
    text = re.sub(r"\.git$", "", text)
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        raise FlowError("Gitee 仓库格式无法识别：%r" % repo)
    return "%s/%s" % (parts[-2], parts[-1])


class GiteeClient:
    def __init__(self, token: str, slug: str, api_base: str,
                 timeout: float = 120.0):
        self.token = (token or "").strip()
        if not self.token:
            raise FlowError("缺少 Gitee 令牌（rpc.json 的 gitee.token 或 --gitee-token）")
        self.slug = slug
        self.api_base = (api_base or "").strip().rstrip("/")
        if not self.api_base:
            raise FlowError("缺少 Gitee API 地址（rpc.json 的 gitee.api）")
        self.timeout = float(timeout)

    def _api(self, path: str, method: str = "GET", params: "dict | None" = None,
             timeout: "float | None" = None) -> dict:
        q = {"access_token": self.token}
        q.update(params or {})
        # access_token 始终放 query，POST 的其它参数放 body；DELETE 也能正常鉴权。
        url = self.api_base + path + "?access_token=" + urllib.parse.quote(self.token)
        data = None
        if method == "GET":
            if params:
                url += "&" + urllib.parse.urlencode(params)
        else:
            body_params = {k: v for k, v in q.items() if k != "access_token"}
            data = urllib.parse.urlencode(body_params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                if not body.strip():
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            return {"__error__": exc.code, "__body__": body}
        except Exception as exc:  # noqa: BLE001
            return {"__error__": -1, "__body__": "%s: %s" % (type(exc).__name__, exc)}

    def find_release(self, tag: str) -> "dict | None":
        obj = self._api("/repos/%s/releases/tags/%s" % (self.slug, tag))
        if isinstance(obj, dict) and obj.get("__error__") == 404:
            return None
        if isinstance(obj, dict) and obj.get("__error__"):
            raise FlowError("查询 Gitee 发行版失败 HTTP %s：%s"
                            % (obj["__error__"], str(obj.get("__body__"))[:200]))
        return obj

    def ensure_release(self, tag: str) -> dict:
        rel = self.find_release(tag)
        if rel:
            log("Gitee 发行版已存在：tag=%s id=%s" % (tag, rel.get("id")))
            return rel
        log("Gitee 发行版不存在，创建 tag=%s" % tag)
        rel = self._api("/repos/%s/releases" % self.slug, method="POST", params={
            "tag_name": tag,
            "name": "xime APK",
            "body": "xime APK 直链（Gitee 发行版附件，匿名可下载）",
        })
        if isinstance(rel, dict) and rel.get("__error__"):
            raise FlowError("创建 Gitee 发行版失败 HTTP %s：%s"
                            % (rel["__error__"], str(rel.get("__body__"))[:300]))
        log("Gitee 发行版已创建：id=%s" % rel.get("id"))
        return rel

    def list_attachments(self, release_id) -> list:
        obj = self._api("/repos/%s/releases/%s/attach_files"
                        % (self.slug, release_id))
        if isinstance(obj, dict) and obj.get("__error__"):
            log("警告：列 Gitee 附件失败 HTTP %s（继续尝试上传）"
                % obj.get("__error__"))
            return []
        return obj if isinstance(obj, list) else []

    def delete_attachment(self, release_id, attach_id) -> bool:
        obj = self._api("/repos/%s/releases/%s/attach_files/%s"
                        % (self.slug, release_id, attach_id), method="DELETE")
        ok = not (isinstance(obj, dict) and obj.get("__error__"))
        log("%s删除旧附件 id=%s" % ("已" if ok else "未能", attach_id))
        return ok

    def upload_attachment(self, release_id, apk: Path, filename: str,
                          timeout: float) -> dict:
        size = apk.stat().st_size
        boundary = "----ximeflow%d" % int(time.time() * 1000)
        head = ("--%s\r\n"
                "Content-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
                % (boundary, filename)).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        total = len(head) + size + len(tail)
        url = ("%s/repos/%s/releases/%s/attach_files?access_token=%s"
               % (self.api_base, self.slug, release_id, urllib.parse.quote(self.token)))
        u = urllib.parse.urlparse(url)
        if not u.hostname:
            raise FlowError("gitee.api 不是合法 URL：%r" % self.api_base)
        target = (u.path or "/") + (("?" + u.query) if u.query else "")
        log("Gitee 上传：%s（%s），最多等待 %.0fs"
            % (filename, human_size(size), timeout))
        meter = ProgressMeter("gitee", size).start()
        started = time.monotonic()
        conn = http.client.HTTPSConnection(u.hostname, port=u.port or 443,
                                           timeout=timeout)
        try:
            conn.putrequest("POST", target, skip_host=False, skip_accept_encoding=True)
            conn.putheader("User-Agent", UA)
            conn.putheader("Content-Type",
                           "multipart/form-data; boundary=%s" % boundary)
            conn.putheader("Content-Length", str(total))
            conn.putheader("Connection", "close")
            conn.endheaders()
            conn.send(head)
            sent = 0
            with open(apk, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    meter.set(done=sent)
            conn.send(tail)
            meter.set(phase="等待 Gitee 回包")
            resp = conn.getresponse()
            status = resp.status
            body = resp.read()
        except Exception as exc:  # noqa: BLE001
            raise FlowError("Gitee 上传失败：%s: %s" % (type(exc).__name__, exc))
        finally:
            meter.stop()
            try:
                conn.close()
            except Exception:
                pass
        elapsed = time.monotonic() - started
        text = body.decode("utf-8", "replace")
        log("Gitee 上传返回 HTTP %s，用时 %.1fs，平均 %s/s"
            % (status, elapsed, human_size(size / elapsed if elapsed else 0)))
        if status not in (200, 201):
            raise FlowError("Gitee 上传失败 HTTP %s：%s" % (status, text[:300]))
        try:
            obj = json.loads(text)
        except ValueError:
            raise FlowError("Gitee 上传返回非 JSON：%s" % text[:300])
        if not isinstance(obj, dict) or not obj.get("browser_download_url"):
            raise FlowError("Gitee 上传返回缺少直链：%s" % text[:300])
        obj["_upload_seconds"] = elapsed
        return obj


def publish_gitee(token: str, slug: str, tag: str, api_base: str, apk: Path,
                  filename: str, timeout: float, force: bool) -> dict:
    client = GiteeClient(token, slug, api_base, timeout=min(timeout, 180.0))
    rel = client.ensure_release(tag)
    release_id = rel.get("id")
    atts = client.list_attachments(release_id)
    same = [a for a in atts if str(a.get("name")) == filename]
    if same and not force:
        for a in same:
            try:
                if int(a.get("size") or -1) == apk.stat().st_size:
                    url = a.get("browser_download_url")
                    if not url:
                        # 只在 API 没回直链时按 api 的 scheme/host 拼，避免写死站点域名。
                        p = urllib.parse.urlparse(api_base)
                        url = "%s://%s/%s/releases/download/%s/%s" % (
                            p.scheme or "https", p.netloc, slug, tag, filename)
                    log("Gitee 附件已存在且大小一致，跳过上传（--force-gitee 可强制覆盖）")
                    return {"url": url, "skipped": True, "upload_seconds": 0.0}
            except (TypeError, ValueError):
                pass
    for a in same:
        client.delete_attachment(release_id, a.get("id"))
    obj = client.upload_attachment(release_id, apk, filename, timeout)
    url = obj.get("browser_download_url")
    log("Gitee 直链：%s" % url)
    return {"url": url, "skipped": False,
            "upload_seconds": obj.get("_upload_seconds", 0.0)}


# =============================== 主流程 ===============================

def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def run(args) -> int:
    global _LOG_FH
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "publish_flow.log"
    _LOG_FH = open(log_path, "w", encoding="utf-8")
    report_path = out_dir / "publish_flow_report.json"
    check_json_local = out_dir / "check_apk_all_result.json"

    work_token = time.strftime("%Y%m%d-%H%M%S") + "-%d" % (int(time.time() * 1000) % 100000)
    if args.check_only:
        # 轻量模式：不做任何上传/下载，只复用或执行远端 6 渠道校验。
        args.skip_publish_all = True
        args.skip_download = True
        args.skip_gitee = True
        args.apk_url = ""
    report = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "work_token": work_token,
        "rpc": RPC_BASE,
        "steps": {},
        "ok": False,
    }
    bridge = None
    try:
        log("=" * 72)
        log("APK 发布流程：1188 远端发布 3 渠道 → 远端 ghfast 链接本地下载 → 本地 Gitee → 远端校验")
        log("=" * 72)

        bridge = connect_bridge()
        remote_preflight(bridge)
        apk_info = remote_apk_info(bridge)
        report["remote_apk"] = apk_info
        pub_cfg = read_publish_apk_config(Path(args.rpc_config))
        if not pub_cfg["rpc_base"]:
            log("警告：publish_apk.rpc_base 未配置，本次跳过 pythonanywhere RPC 渠道")
        npm_conf = dict(pub_cfg["npm"])
        if npm_conf.get("token"):
            npm_conf["token"] = "****" + npm_conf["token"][-4:]
        report["publish_apk_config"] = {
            "rpc_base": pub_cfg["rpc_base"],
            "npm": npm_conf,
            "gitee": {
                "api": pub_cfg["gitee"]["api"],
                "repo": pub_cfg["gitee"]["repo"],
                "tag": pub_cfg["gitee"]["tag"],
                "token": ("****" + pub_cfg["gitee"]["token"][-4:]
                          if pub_cfg["gitee"]["token"] else ""),
            },
        }

        # 1) 远端发布其他 3 个渠道（npm + GitHub git + pythonanywhere rpc）
        job = None
        pub_url = args.apk_url or None
        outcome = "skipped"
        t0 = time.monotonic()
        if args.skip_publish_all or args.dry_run:
            log("已跳过远端 publish_apk_all.py（%s）"
                % ("--dry-run" if args.dry_run
                   else ("--check-only" if args.check_only else "--skip-publish-all")))
            if not args.dry_run and not args.check_only and not pub_url:
                raise FlowError("--skip-publish-all 需要用 --apk-url 指定远端已发布的下载链接")
        else:
            # 先看有没有刚跑完/仍在远端跑的 publish_all，能接手就不重复上传。
            job, pub_url, outcome = resume_or_start_publish(
                bridge, apk_info, args, work_token, pub_cfg)
            report["steps"]["publish_all"] = {
                "ghfast_url": pub_url,
                "git_outcome": outcome,
                "reused": outcome == "reused",
            }
            if job is not None:
                report["steps"]["publish_all"].update({
                    "log": job.log_path,
                    "git_seconds": round(job.git_seconds or 0.0, 1),
                })
            write_report(report_path, report)
            if outcome == "reused":
                log("上次发布已成功，直接复用其加速链接，未重新上传")
            else:
                log("远端 git 链接已就绪，其余渠道继续在远端跑（%s）" % outcome)

        if args.dry_run:
            log("dry-run：预检完成，未执行下载/Gitee/校验")
            report["ok"] = True
            report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            write_report(report_path, report)
            return 0

        # 2) 本地下载（本地已有同一份就不再重复下）
        need_local = (not args.skip_download) or (not args.skip_gitee)
        apk_name = None
        apk_local = None
        download_info = {"skipped": True}
        if need_local:
            if not pub_url:
                raise FlowError("没有可用的下载链接（远端未打印加速链接且未给 --apk-url）")
            apk_name = apk_name_from_url(pub_url)
            apk_local = out_dir / apk_name
            log("发布文件名：%s" % apk_name)
            if args.skip_download:
                log("已跳过下载（--skip-download）")
                if apk_local.is_file():
                    actual_sha = sha256_file(apk_local)
                    if actual_sha != apk_info["sha256"]:
                        raise FlowError("本地 %s 的 sha256 与远端不一致：%s != %s"
                                        % (apk_local, actual_sha[:12],
                                           apk_info["sha256"][:12]))
                    log("本地 APK 校验一致：%s" % apk_local)
                download_info = {"skipped": True, "path": str(apk_local),
                                 "reason": "flag"}
            elif (apk_local.is_file()
                  and apk_local.stat().st_size == apk_info["size"]
                  and sha256_file(apk_local) == apk_info["sha256"]):
                log("本地已有最新 APK（大小/sha 与远端一致），跳过下载：%s" % apk_local)
                download_info = {"path": str(apk_local), "size": apk_info["size"],
                                 "sha256": apk_info["sha256"], "skipped": True,
                                 "reason": "local-match"}
            else:
                download_info = download_apk(
                    pub_url, apk_info, apk_local, args.download_timeout, work_token)
                download_info["skipped"] = False
        report["steps"]["download"] = download_info
        write_report(report_path, report)

        # 3) 本地上传 Gitee
        if args.skip_gitee:
            gitee_info = {"skipped": True}
            log("已跳过 Gitee 上传（--skip-gitee）")
        else:
            if apk_local is None or apk_name is None:
                raise FlowError("需要本地 APK 才能上传 Gitee")
            cfg = pub_cfg["gitee"]
            if args.gitee_repo:
                cfg["repo"] = args.gitee_repo.strip()
            override = (args.gitee_token or os.environ.get("GITEE_TOKEN") or "").strip()
            if override:
                cfg["token"] = override
            missing = [k for k in ("api", "repo", "tag", "token") if not cfg.get(k)]
            if missing:
                gitee_info = {"skipped": True, "reason": "missing-config",
                              "missing": missing}
                log("警告：%s 的 publish_apk.gitee 缺少 %s，跳过 Gitee 上传（其余步骤继续）"
                    % (args.rpc_config, ", ".join(missing)))
            else:
                try:
                    slug = repo_slug(cfg["repo"])
                except FlowError as exc:
                    gitee_info = {"skipped": True, "reason": "bad-repo"}
                    log("警告：publish_apk.gitee.repo 无法解析（%s），跳过 Gitee 上传" % exc)
                else:
                    log("Gitee 配置：api=%s repo=%s tag=%s 令牌 ****%s"
                        % (cfg["api"], cfg["repo"], cfg["tag"], cfg["token"][-4:]))
                    gitee_info = publish_gitee(
                        cfg["token"], slug, cfg["tag"], cfg["api"],
                        apk_local, apk_name, args.gitee_timeout, args.force_gitee)
                    time.sleep(10)
        report["steps"]["gitee"] = gitee_info
        write_report(report_path, report)

        # 4) 等远端 publish_all 全部渠道结束
        if job is not None:
            seconds_before_final_wait = time.monotonic() - t0
            if job.rc is None:
                publish_rc = job.wait()
            else:
                publish_rc = int(job.rc)
            report["steps"]["publish_all"].update({
                "rc": publish_rc,
                "seconds": round(time.monotonic() - t0, 1),
                "seconds_before_final_wait": round(seconds_before_final_wait, 1),
            })
            write_report(report_path, report)
            if publish_rc != 0:
                raise FlowError(
                    "远端 publish_apk_all.py --no-gitee 失败（rc=%s），见日志 %s"
                    % (publish_rc, job.log_path))
            apk_info_after = remote_apk_info(bridge)
            if apk_info_after["sha256"] != apk_info["sha256"]:
                raise FlowError("发布期间远端 APK 发生变化：%s -> %s"
                                % (apk_info["sha256"][:12],
                                   apk_info_after["sha256"][:12]))

        # 5) 远端 check_apk_all.py（能复用/接手上次结果就不重复下载校验）
        if args.skip_check:
            check_info = {"skipped": True}
            log("已跳过远端 check_apk_all.py（--skip-check）")
            report["steps"]["check"] = check_info
        else:
            check_info = resume_or_run_check(
                bridge, apk_info, args, work_token, check_json_local, pub_cfg)
            report["steps"]["check"] = check_info
            write_report(report_path, report)
            check_rc = check_info.get("rc")
            if check_rc != 0:
                bad = [c["key"] for c in check_info.get("channels", [])
                       if c.get("verdict") != "一致"]
                raise FlowError("check_apk_all.py 返回 %s，异常渠道：%s；详见 %s"
                                % (check_rc, ",".join(bad) or "未知",
                                   check_info.get("log") or check_info.get("json")))

        report["ok"] = True
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_report(report_path, report)
        log("=" * 72)
        log("全部完成：远端 3 渠道发布 → 远端链接本地下载 → 本地 Gitee → 远端 6 渠道校验")
        log("报告：%s" % report_path)
        log("日志：%s" % log_path)
        return 0
    except FlowError as exc:
        log("ERROR: %s" % exc)
        report["error"] = str(exc)
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_report(report_path, report)
        return 2
    except KeyboardInterrupt:
        log("已中断")
        report["error"] = "KeyboardInterrupt"
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_report(report_path, report)
        return 130
    finally:
        if _LOG_FH is not None:
            _LOG_FH.close()
            _LOG_FH = None


def build_args(argv=None):
    ap = argparse.ArgumentParser(
        description="1188 远端发布 3 渠道 → 远端 ghfast 链接本地下载 → 本地 Gitee → 远端 6 渠道校验")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="本地输出目录")
    ap.add_argument("--remote-python", default="python3", help="远端 Python 命令")
    ap.add_argument("--apk-url", default="",
                    help="远端已发布的下载链接（仅 --skip-publish-all 时需要）")
    ap.add_argument("--timeout-publish", type=float, default=1800.0,
                    help="远端发布最长等待秒数（默认 1800）")
    ap.add_argument("--timeout-git", type=float, default=600.0,
                    help="等待远端 git 通道打印加速链接的最长秒数（默认 600）")
    ap.add_argument("--timeout-check", type=float, default=1200.0,
                    help="远端校验最长等待秒数（默认 1200）")
    ap.add_argument("--check-channel-timeout", type=float, default=300.0,
                    help="check_apk_all.py 单渠道读超时（默认 300）")
    ap.add_argument("--download-timeout", type=float, default=180.0,
                    help="单次下载读超时秒数（默认 180）")
    ap.add_argument("--gitee-timeout", type=float, default=1800.0,
                    help="Gitee 上传超时秒数（默认 1800）")
    ap.add_argument("--rpc-config", default=DEFAULT_RPC_CONFIG,
                    help="读取 publish_apk 段的配置文件（默认 %s）" % DEFAULT_RPC_CONFIG)
    ap.add_argument("--gitee-repo", default="",
                    help="覆盖 rpc.json 的 publish_apk.gitee.repo")
    ap.add_argument("--gitee-token", default="",
                    help="覆盖 rpc.json 的 publish_apk.gitee.token（也可用环境变量 GITEE_TOKEN）")
    ap.add_argument("--force-publish", action="store_true",
                    help="远端 APK 未变化也强制重发 3 个渠道")
    ap.add_argument("--force-gitee", action="store_true",
                    help="Gitee 同名附件即使大小相同也删除重传")
    ap.add_argument("--check-only", action="store_true",
                    help="只复用/执行远端 check，不发布、不下载、不上传 Gitee")
    ap.add_argument("--skip-publish-all", action="store_true")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-gitee", action="store_true")
    ap.add_argument("--skip-check", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="只连接+预检+读远端 APK 信息，不做任何写操作")
    return ap


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_args(argv).parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
