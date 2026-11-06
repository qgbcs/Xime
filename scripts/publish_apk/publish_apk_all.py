#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_all.py - 并行执行 npm、GitHub、pythonanywhere RPC 与 Gitee 四条发布通道。

四条通道互不依赖（npm 写 registry，git 写 QGB/suishou，rpc 写
.pythonanywhere.com/ime.apk，gitee 写
https://gitee.com/qgbcs/suishou/releases/download/apk/ime.apk），可以同时跑：
- 四条共用一个 APK，都自带“内容未变化则跳过”的保护；
- 输出按 [npm] / [git] / [rpc] / [gitee] 前缀实时拼接，每条通道都有实时速度/状态行，
  长时间无输出时打 ⏳ 心跳，任何通道都不会出现“卡住不动”的静默；
- 任一失败则本脚本返回非 0（不会冒充成功）。

发布后可用 check_apk_all.py 从所有渠道下载回来实测速度并核对 sha256/大小。

- 上传：把 body 写成 ime.apk，并在服务端用 hashlib 校验 sha256；
- 校验：只读，回 JSON（path/exists/size/mtime/sha256/md5）。
上传前后都调校验入口，所以“写进去的字节”必须与服务端算出的 sha256/大小一致才算成功。

用法:

    python3 publish_apk_all.py                 # 四条并行发布（APK 未变则自动跳过）
    python3 publish_apk_all.py --dry-run       # 四条都演练，不上传
    python3 publish_apk_all.py --force         # APK 没变也强制重发
    python3 publish_apk_all.py --no-rpc        # 只跑 npm + git + gitee
    python3 publish_apk_all.py --no-gitee      # 只跑 npm + git + rpc
    python3 publish_apk_all.py --apk out/xxx.apk --branch main
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# pythonanywhere 的“路径即代码”RPC 入口：base 后面直接拼 URL 编码后的 Python 源码。
# 不在脚本里写死：由 --rpc-base 或环境变量 XIME_RPC_BASE 提供。
RPC_BASE = os.environ.get("XIME_RPC_BASE", "").strip()
RPC_NAME = "ime.apk"

DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
BLOCK = 1 << 20  # 上传/下载分块 1MiB：进度够细，吞吐也不亏

_PRINT_LOCK = threading.Lock()

# 服务端执行的源码：body → ime.apk，并校验 sha256（占位符由客户端注入 name / sha）
RPC_UPLOAD_TEMPLATE = """import os, hashlib, traceback
__b = (request.rfile.read(int(request.headers.get('Content-Length',0) or 0)) if hasattr(request,'rfile') else request.environ['wsgi.input'].read(int(request.environ.get('CONTENT_LENGTH',0) or 0)))
__p = @@NAME@@
__expect_sha = @@SHA@@
try:
    __p = os.path.abspath(os.path.expanduser(__p))
    __d = os.path.dirname(__p)
    if __d: os.makedirs(__d, exist_ok=True)
    __f = open(__p, 'wb'); __f.write(__b); __f.close()
    __sha = hashlib.sha256(__b).hexdigest()
    if __expect_sha and __sha != __expect_sha:
        raise ValueError('SHA_MISMATCH expected=%s actual=%s size=%d path=%s' % (__expect_sha, __sha, len(__b), __p))
    r = __p
except Exception:
    try:
        response.set_status(500)
    except Exception:
        pass
    r = traceback.format_exc()"""

# 服务端执行的源码：只读校验，回 JSON（path/exists/size/mtime/sha256/md5）
RPC_VERIFY_TEMPLATE = """import os, hashlib, json, time
__p = os.path.abspath(os.path.expanduser(@@NAME@@))
info = {'path': __p, 'exists': False}
if os.path.exists(__p):
    st = os.stat(__p)
    h = hashlib.sha256()
    m = hashlib.md5()
    with open(__p,'rb') as f:
        while True:
            chunk = f.read(1<<20)
            if not chunk: break
            h.update(chunk); m.update(chunk)
    info.update({'exists': True, 'size': st.st_size, 'mtime': st.st_mtime, 'mtime_iso': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime)), 'sha256': h.hexdigest(), 'md5': m.hexdigest()})
r = json.dumps(info, ensure_ascii=False)"""


def log(prefix: str, msg: str) -> None:
    # 三条通道并行 + 心跳线程共用 stdout：print() 的 text/换行是两次 write，
    # 不加锁会出现两行粘在一起。锁住整行保证可读。
    with _PRINT_LOCK:
        print("[%s] %s" % (prefix, msg), flush=True)


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%dB" % n if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGiB" % n


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def speed(bps: float) -> str:
    """字节/秒 → 人类可读速度（给所有通道的“实时速度”列用）。"""
    return human_size(max(0.0, bps)) + "/s"


class Heartbeat:
    """后台心跳：让长操作永远有“实时速度 + 当前阶段”输出，绝不看起来卡死。

    调用方通过 set() 更新 phase / sent / total；线程每 interval 秒打一行：
      有进展 → `[prefix] 阶段 已发 X/Y (Z%) 即时 Vx 平均 Vy 用时 Ts`
      停滞   → `[prefix] ⏳ 阶段：已 Ns 无进展（连接仍在，服务端/闸口慢，不是卡死）`
    """

    def __init__(self, prefix: str, phase: str, total: int = 0,
                 interval: float = 3.0) -> None:
        self.prefix = prefix
        self.phase = phase
        self.total = int(total)
        self.sent = 0
        self.interval = interval
        self.started = time.monotonic()
        self.moved = self.started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def set(self, phase: "str | None" = None, sent: "int | None" = None,
            total: "int | None" = None) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if sent is not None:
                self.sent = int(sent)
            if total is not None:
                self.total = int(total)
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
                phase, sent, total, moved = self.phase, self.sent, self.total, self.moved
            idle = now - moved
            elapsed = now - self.started
            if idle >= self.interval:
                log(self.prefix, "⏳ %s：已 %.0fs 无进展（连接仍在 / 服务端还在处理，累计 %.0fs%s）"
                    % (phase, idle, elapsed,
                       "，已发 %s/%s" % (human_size(sent), human_size(total)) if total else ""))
                last_n, last_t = sent, now
                continue
            if not total:
                log(self.prefix, "%s：累计 %.0fs（仍在等待响应）" % (phase, elapsed))
                last_n, last_t = sent, now
                continue
            inst = (sent - last_n) / (now - last_t) if now > last_t else 0.0
            avg = sent / elapsed if elapsed > 0 else 0.0
            log(self.prefix, "%s %s/%s (%.1f%%) 即时 %s 平均 %s 用时 %.0fs"
                % (phase, human_size(sent), human_size(total),
                   100.0 * sent / total, speed(inst), speed(avg), elapsed))
            last_n, last_t = sent, now


def find_latest_apk(out_dir: Path) -> "Path | None":
    if not out_dir.is_dir():
        return None
    cands = [p for p in out_dir.glob(APK_PATTERN) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: (p.stat().st_mtime, p.name))


# ---------------------------------------------------------------- RPC 通道

def rpc_url(base: str, code: str) -> str:
    """base 后面直接拼 URL 编码后的 Python 源码（空格/换行/% 全部转义）。"""
    return base + urllib.parse.quote(code, safe="")


def rpc_link(base: str, name: str) -> str:
    """由 RPC base 推导站点根下的下载直链（base 形如 https://host/-）。"""
    root = base.rsplit("/-", 1)[0]
    return root + "/" + urllib.parse.quote(name)


def rpc_post(url: str, data: bytes, timeout: float) -> "tuple[int, bytes, float]":
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "User-Agent": "xime-publish-rpc/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except Exception:  # noqa: BLE001
            body = b""
        return exc.code, body, time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一当失败上报
        return 0, ("%s: %s" % (type(exc).__name__, exc)).encode(), time.monotonic() - started


def rpc_upload(base: str, code: str, apk: Path, size: int,
               timeout: float) -> "tuple[int, bytes, float]":
    """流式 POST（http.client 直连，1MiB 分块），边发边报速度；停滞也只报状态不装死。

    urllib 的 urlopen(data=整份 bytes) 不可见进度，63MiB 上传期间会长时间静默，
    所以这里自己管 putrequest/send，把每块字节数喂给 Heartbeat。
    """
    u = urllib.parse.urlparse(base)
    target = (u.path or "/") + urllib.parse.quote(code, safe="")
    hb = Heartbeat("rpc", "上传", total=size).start()
    started = time.monotonic()
    conn = http.client.HTTPSConnection(u.hostname or "", port=u.port or 443,
                                      timeout=timeout)
    try:
        conn.putrequest("POST", target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", u.netloc)
        conn.putheader("Content-Length", str(size))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("User-Agent", "xime-publish-rpc/1.0")
        conn.putheader("Connection", "close")
        conn.endheaders()
        sent = 0
        with open(apk, "rb") as fh:
            while True:
                chunk = fh.read(BLOCK)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                hb.set(sent=sent)
        hb.set(phase="字已发完，等服务端落盘并算 sha256")
        resp = conn.getresponse()
        return resp.status, resp.read(), time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一当失败上报
        return 0, ("%s: %s" % (type(exc).__name__, exc)).encode(), time.monotonic() - started
    finally:
        hb.stop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def rpc_verify(base: str, name: str, timeout: float, prefix: str = "rpc") -> "dict | None":
    code = RPC_VERIFY_TEMPLATE.replace("@@NAME@@", repr(name))
    hb = Heartbeat(prefix, "校验：等服务端读文件算 sha256", interval=5.0).start()
    try:
        status, body, elapsed = rpc_post(rpc_url(base, code), b"", timeout)
    finally:
        hb.stop()
    text = body.decode("utf-8", "replace")
    log(prefix, "校验返回: HTTP %s，用时 %.1fs" % (status, elapsed))
    if status != 200:
        log(prefix, "远端校验失败：HTTP %s %s" % (status, text.strip()[:400]))
        return None
    try:
        return json.loads(text)
    except ValueError:
        log(prefix, "远端校验返回非 JSON：%s" % text.strip()[:400])
        return None


def rpc_channel(apk: Path, name: str, base: str, timeout: float,
                force: bool, dry_run: bool) -> int:
    digest = sha256_file(apk)
    size = apk.stat().st_size
    link = rpc_link(base, name)
    log("rpc", "APK    : %s (%s)" % (apk, human_size(size)))
    log("rpc", "SHA256 : %s" % digest)
    log("rpc", "入口   : %s" % base)
    log("rpc", "直链   : %s" % link)

    remote = rpc_verify(base, name, timeout)
    if remote is None:
        return 1
    if remote.get("exists"):
        log("rpc", "远端已有: %s，%s，sha256 %s（mtime %s）"
            % (remote.get("path"), human_size(remote.get("size") or 0),
               remote.get("sha256"), remote.get("mtime_iso")))
    else:
        log("rpc", "远端尚无: %s" % remote.get("path"))

    same = (bool(remote.get("exists"))
            and remote.get("sha256") == digest
            and remote.get("size") == size)
    if same and not force:
        log("rpc", "内容与远端一致，跳过上传（要强制重发加 --force）")
        log("rpc", "下载直链: %s" % link)
        return 0

    if dry_run:
        log("rpc", "[dry-run] 将 POST %s 到 %s" % (human_size(size), base))
        log("rpc", "[dry-run] 上传后会再用同一入口回查 sha256/大小")
        return 0

    code = (RPC_UPLOAD_TEMPLATE
            .replace("@@NAME@@", repr(name))
            .replace("@@SHA@@", repr(digest)))
    log("rpc", "上传中… %s（1MiB 分块流式发送，超时 %.0fs）" % (human_size(size), timeout))
    status, body, elapsed = rpc_upload(base, code, apk, size, timeout)
    text = body.decode("utf-8", "replace").strip()
    if status != 200:
        log("rpc", "上传失败：HTTP %s %s" % (status, text[:400]))
        return 1
    avg_bps = size / elapsed if elapsed > 0 else 0.0
    log("rpc", "上传完成: HTTP %s，用时 %.1fs，平均 %s"
        % (status, elapsed, speed(avg_bps)))
    log("rpc", "服务端写入: %s" % text)

    remote2 = rpc_verify(base, name, timeout)
    if remote2 is None:
        return 1
    if (not remote2.get("exists") or remote2.get("sha256") != digest
            or remote2.get("size") != size):
        log("rpc", "校验不一致：exists=%s size=%s sha256=%s"
            % (remote2.get("exists"), remote2.get("size"), remote2.get("sha256")))
        return 1
    log("rpc", "校验通过: exists=True，%s，sha256 与本地一致" % human_size(size))
    log("rpc", "下载直链: %s" % link)
    return 0


# ---------------------------------------------------------------- 主流程

def pump(proc: subprocess.Popen, prefix: str, started: float) -> None:
    """子进程输出实时转 [prefix] 行；静默超过 5s 打心跳，任何通道都不许看起来卡住。"""
    assert proc.stdout is not None
    last = [time.monotonic()]
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(5.0):
            idle = time.monotonic() - last[0]
            if idle >= 5.0:
                log(prefix, "⏳ 静默 %.0fs（进程仍在运行 pid=%s，该通道累计 %.0fs）"
                    % (idle, proc.pid, time.monotonic() - started))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            last[0] = time.monotonic()
            log(prefix, line.rstrip("\n"))
    finally:
        stop.set()
        watcher.join(timeout=2.0)
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="并行发布 APK 到 npm、GitHub、pythonanywhere RPC 与 Gitee 发行版（固定名 ime.apk）。")
    ap.add_argument("--apk", default=None, help="要发布的 APK（默认 out/ 下最新）")
    ap.add_argument("--force", action="store_true", help="APK 未变化也强制重发")
    ap.add_argument("--dry-run", action="store_true", help="四条都只演练，不上传")
    ap.add_argument("--verify-timeout", type=float, default=420.0,
                    help="npm 发布后回查 registry 的最长等待秒数（默认 420）")
    ap.add_argument("--npm-package", default=None, help="npm 包名（默认 qgb）")
    ap.add_argument("--npm-registry", default=None, help="npm registry（默认 registry.npmjs.org）")
    ap.add_argument("--npm-tag", default=None, help="npm dist-tag（默认 latest）")
    ap.add_argument("--npm-name", default=None, help="包内 APK 文件名（默认 ime.apk）")
    ap.add_argument("--repo", default=None, help="Git 仓库（默认 git@ssh.github.com:QGB/suishou）")
    ap.add_argument("--branch", default=None, help="Git 分支（默认 main）")
    ap.add_argument("--dir", default=None, help="Git 本地工作副本")
    ap.add_argument("--key", default=None, help="SSH 私钥（默认 ~/.ssh/NIST256p.pem）")
    ap.add_argument("--git-name", default=None,
                    help="Git 发布文件名（默认 ime.apk）")
    ap.add_argument("--rpc-base", default=None,
                    help="RPC 入口 base（也可用环境变量 XIME_RPC_BASE）")
    ap.add_argument("--rpc-name", default=RPC_NAME,
                    help="服务端文件名/下载直链文件名（默认 %s）" % RPC_NAME)
    ap.add_argument("--rpc-timeout", type=float, default=900.0,
                    help="RPC 上传与校验超时秒数（默认 900）")
    ap.add_argument("--no-rpc", action="store_true", help="跳过 pythonanywhere RPC 通道")
    ap.add_argument("--no-gitee", action="store_true", help="跳过 Gitee 发行版附件通道")
    ap.add_argument("--gitee-repo", default=None,
                    help="Gitee 仓库（默认 git@gitee.com:qgbcs/suishou.git）")
    ap.add_argument("--gitee-tag", default=None, help="Gitee 发行版 tag（默认 apk）")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    args.rpc_base = (args.rpc_base or RPC_BASE or "").strip()
    if not args.no_rpc and not args.rpc_base:
        print("[all] 缺少 --rpc-base（或环境变量 XIME_RPC_BASE），无法使用 rpc 通道",
              flush=True)
        return 2

    apk = Path(args.apk).expanduser() if args.apk else find_latest_apk(DEFAULT_OUT_DIR)
    if apk is None or not apk.is_file():
        print("[all] 找不到 APK：%s（用 --apk 指定）" % (apk or DEFAULT_OUT_DIR), flush=True)
        return 2

    npm_cmd = [sys.executable, str(SCRIPTS / "publish_apk_npm.py"),
               "--verify-timeout", str(args.verify_timeout)]
    for flag, value in (("--package", args.npm_package),
                        ("--registry", args.npm_registry),
                        ("--tag", args.npm_tag),
                        ("--name", args.npm_name)):
        if value:
            npm_cmd += [flag, value]
    git_cmd = [sys.executable, str(SCRIPTS / "publish_apk_git.py")]
    for cmd in (npm_cmd, git_cmd):
        if args.apk:
            cmd += ["--apk", args.apk]
        if args.force:
            cmd.append("--force")
        if args.dry_run:
            cmd.append("--dry-run")
    for flag, value in (("--repo", args.repo), ("--branch", args.branch),
                        ("--dir", args.dir), ("--key", args.key),
                        ("--name", args.git_name)):
        if value:
            git_cmd += [flag, value]

    # Gitee 通道：publish_apk_gitee.py 自身默认只演练，这里默认补 --publish 真正上传；
    # 传 --dry-run 时不加 --publish，让它只打印发行版状态/链接。
    gitee_cmd = [sys.executable, str(SCRIPTS / "publish_apk_gitee.py"), "--release"]
    if args.apk:
        gitee_cmd += ["--apk", args.apk]
    if not args.dry_run:
        gitee_cmd.append("--publish")
    if args.force:
        gitee_cmd.append("--force")
    if args.gitee_repo:
        gitee_cmd += ["--repo", args.gitee_repo]
    if args.gitee_tag:
        gitee_cmd += ["--tag", args.gitee_tag]

    print("[all] 并行启动发布通道", flush=True)
    print("[all] apk: %s" % apk, flush=True)
    print("[all] npm: " + " ".join(npm_cmd[1:]), flush=True)
    print("[all] git: " + " ".join(git_cmd[1:]), flush=True)
    if args.no_gitee:
        print("[all] gitee: 已关闭（--no-gitee）", flush=True)
    else:
        print("[all] gitee: " + " ".join(gitee_cmd[1:]), flush=True)
    if args.no_rpc:
        print("[all] rpc: 已关闭（--no-rpc）", flush=True)
    else:
        print("[all] rpc: %s" % args.rpc_base, flush=True)

    started = time.monotonic()
    procs = {
        "npm": subprocess.Popen(npm_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1),
        "git": subprocess.Popen(git_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1),
    }
    if not args.no_gitee:
        procs["gitee"] = subprocess.Popen(
            gitee_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
    threads = [threading.Thread(target=pump, args=(proc, name, started), daemon=True)
               for name, proc in procs.items()]
    for t in threads:
        t.start()

    results: "dict[str, int]" = {}
    lock = threading.Lock()
    rpc_thread = None
    if not args.no_rpc:
        def run_rpc() -> None:
            rc = rpc_channel(apk, args.rpc_name, args.rpc_base, args.rpc_timeout,
                             args.force, args.dry_run)
            with lock:
                results["rpc"] = rc
        rpc_thread = threading.Thread(target=run_rpc, daemon=True)
        rpc_thread.start()

    try:
        for name, proc in procs.items():
            proc.wait()
    except KeyboardInterrupt:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            proc.wait()
        for t in threads:
            t.join(timeout=5)
        print("[all] 已中断", flush=True)
        return 130
    for t in threads:
        t.join(timeout=5)
    if rpc_thread is not None:
        rpc_thread.join()

    elapsed = time.monotonic() - started
    rcs = {name: proc.returncode for name, proc in procs.items()}
    with lock:
        rpc_rc = results.get("rpc")
    for name, proc in procs.items():
        print("[all] %s 通道结束：rc=%s，累计 %.1fs" % (name, proc.returncode, elapsed),
              flush=True)
    parts = ["%s rc=%s" % (name, rc) for name, rc in rcs.items()]
    all_rcs = list(rcs.values())
    if not args.no_rpc:
        parts.append("rpc rc=%s" % rpc_rc)
        all_rcs.append(rpc_rc)
    print("\n[all] 完成，用时 %.1fs，%s" % (elapsed, "，".join(parts)), flush=True)
    return 0 if all(rc == 0 for rc in all_rcs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
