#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_catbox.py - 把 xime 的 APK 上传到 catbox.moe，输出永久下载直链。

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
本脚本当前【默认停止】: catbox 对匿名上传做 IP 级封锁，本环境所有出口都被拒绝。
保留代码只为记录与将来在住宅 IP / 带 userhash 的机器上复用；
默认运行只会打印下面的失败结论并退出 7，确实要再试一次请加 --try-anyway。
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

实测记录（2026-10-31，全部失败，按出现顺序）:

1) 本机直连 catbox（无代理）
   curl -sS -A 'Mozilla/5.0' https://catbox.moe/
   -> curl: (56) schannel: server closed abruptly (missing close_notify)
   curl -sS -I https://files.catbox.moe/
   -> curl: (35) Recv failure: Connection was reset
   说明：国内直连在 TLS 阶段被重置，连页面都拿不到。

2) 本机走本地 xray HTTP 代理 127.0.0.1:1080
   curl -x http://127.0.0.1:1080 -A 'Mozilla/5.0' \
        -F reqtype=fileupload -F fileToUpload=@probe.txt \
        https://catbox.moe/user/api.php
   -> HTTP 412，响应体纯文本: Invalid uploader
   说明：1080 是 HTTP inbound（不是 SOCKS），用 --socks5-hostname 会
   “connection to proxy closed”；改成 -x http:// 才能走到 catbox。

3) 远端 pty127（VPS）跑同样的 multipart curl
   -> HTTP 412 Invalid uploader

4) 远端 pty100（AWS EC2，3.142.185.104）
   -> HTTP 412 Invalid uploader

5) 本脚本真实上传 62.9MiB APK（publish_apk_catbox.py --check-url）
   [hb] 文件已发完，等 catbox 校验并返回直链 62.9MiB/62.9MiB (100.0%)
        即时 21.0MiB/s 平均 21.0MiB/s 用时 3s
   [upload] 失败：HTTP 412 Invalid uploader（用时 3.1s）
   说明：3 秒内 63MB 全部发完，multipart 格式、Content-Length、分块发送都正常；
   412 是 catbox 收到完整请求后按出口 IP 拒绝，不是脚本格式问题。

6) 顺带验证过的其他主机（catbox 之外的选择，仅供记录）
   - pixeldrain  -> {"success":false,"value":"authentication_required"}（匿名 API 已关）
   - 0x0.st      -> "uploads disabled because it’s been almost nothing but AI botnet spam"
   - file.coffee -> 302 跳到 ti.fo 首页，旧上传 API 已不可用
   - kappa.lol（现 segs.lol，100MiB 上限）-> 200，探针直链
     https://kappa.lol/ylixCy （国内可直连；永久性未承诺）
   - pomf2.lain.la -> 200，探针直链 https://pomf2.lain.la/f/hjf1151q.txt
     （国内直连超时，必须走代理）

结论：catbox 官方 API 本身没问题（匿名上传按文档就不需要 userhash），
但机房/VPN 出口会被判成 Invalid uploader；要拿到 files.catbox.moe 直链，
只能在住宅 IP 上跑，或登录 catbox 后用账号 userhash（本脚本尚未实现 userhash）。

与 publish_apk_git.py / publish_apk_npm.py / publish_apk_123pan.py 的区别:
- 目标是 catbox.moe 匿名文件托管（https://catbox.moe/user/api.php），不是 git/npm/网盘。
- 不需要任何鉴权：匿名上传，multipart 里的文件名固定 ime.apk
  （catbox 会把文件名改成随机短名，但保留 .apk 扩展名）。
- catbox 没有“根目录/覆盖”语义：每次上传都是一条新直链，旧文件不会被替换。
  为了少传 63MB，脚本把上次的 sha256/大小/直链记在 ~/.xime_catbox_last.json，
  内容未变且直链仍可访问时直接复用（--force 可强制重传）。
- 单文件上限 200MB（catbox 硬限制），超过直接拦下。
- 上传结束后只打印 catbox 的永久下载直链（files.catbox.moe）。

前置条件:
- python3.8+；能访问 catbox.moe 与 files.catbox.moe。

典型用法:

    # 默认：取 out/ 下最新 APK，匿名上传为 ime.apk，打印直链
    python3 publish_apk_catbox.py

    # 演练：只算 sha256、判断会不会跳过，不上传
    python3 publish_apk_catbox.py --dry-run

    # 内容没变也强制重传
    python3 publish_apk_catbox.py --force

    # 指定 APK / 上传文件名 / 状态文件 / 超时
    python3 publish_apk_catbox.py --apk out/xxx.apk --name ime.apk \\
        --state-file ~/.xime_catbox_last.json --timeout 900

    # 结束后 HEAD 一次直链
    python3 publish_apk_catbox.py --check-url
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

UPLOAD_URL = "https://catbox.moe/user/api.php"
CATBOX_HOME = "https://catbox.moe/"
DIRECT_PREFIXES = ("https://files.catbox.moe/", "http://files.catbox.moe/")
DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
DEFAULT_PUBLISH_NAME = "ime.apk"
DEFAULT_STATE_FILE = Path("~/.xime_catbox_last.json")
# catbox 匿名上传单文件上限 200MB
MAX_FILE_MB = 200.0
BLOCK = 1 << 20  # 1MiB 分块：进度够细，吞吐也不亏
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")

_PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def die(msg: str, code: int = 2) -> "None":
    print("ERROR: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%dB" % n if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGiB" % n


def speed(bps: float) -> str:
    return human_size(max(0.0, bps)) + "/s"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def find_latest_apk(out_dir: Path) -> "Path | None":
    if not out_dir.is_dir():
        return None
    cands = [p for p in out_dir.glob(APK_PATTERN) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: (p.stat().st_mtime, p.name))


class Heartbeat:
    """后台心跳：长上传永远有“实时速度/当前阶段”输出，绝不看起来卡死。"""

    def __init__(self, phase: str, total: int = 0, interval: float = 3.0) -> None:
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
                phase, sent, total, moved = (self.phase, self.sent,
                                             self.total, self.moved)
            idle = now - moved
            elapsed = now - self.started
            if idle >= self.interval:
                log("[hb] %s：已 %.0fs 无进展（连接仍在 / catbox 还在收，累计 %.0fs%s）"
                    % (phase, idle, elapsed,
                       "，已发 %s/%s" % (human_size(sent), human_size(total))
                       if total else ""))
                last_n, last_t = sent, now
                continue
            if not total:
                log("[hb] %s：累计 %.0fs（仍在等待响应）" % (phase, elapsed))
                last_n, last_t = sent, now
                continue
            inst = (sent - last_n) / (now - last_t) if now > last_t else 0.0
            avg = sent / elapsed if elapsed > 0 else 0.0
            log("[hb] %s %s/%s (%.1f%%) 即时 %s 平均 %s 用时 %.0fs"
                % (phase, human_size(sent), human_size(total),
                   100.0 * sent / total, speed(inst), speed(avg), elapsed))
            last_n, last_t = sent, now


# ---------------------------------------------------------------- 状态 / 探测

def load_state(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 - 状态文件只是缓存，坏了当没有
        return {}


def save_state(path: Path, data: dict) -> None:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - 写缓存失败不影响本次直链
        log("[warn] 状态文件写入失败（不影响本次直链）：%s" % exc)


def url_alive(url: str, timeout: float) -> bool:
    """探测直链是否还能取到（HEAD，被拒时退回 1 字节 GET）。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            return False
    except Exception:  # noqa: BLE001 - 网络问题/超时都按不可用处理
        return False
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 上传

def upload_file(apk: Path, name: str, size: int,
                timeout: float) -> "tuple[int, str]":
    """multipart 流式 POST（http.client 直连，1MiB 分块），边发边报速度。

    urllib 的 urlopen(data=整份 bytes) 不可见进度，63MiB 上传期间会长时间静默，
    所以这里自己管 putrequest/send，把每块字节数喂给 Heartbeat。

    catbox 接口字段：reqtype=fileupload + fileToUpload=<文件>；匿名无需 userhash。
    成功时响应体就是一条纯文本直链（不是 JSON）。
    """
    u = urllib.parse.urlsplit(UPLOAD_URL)
    boundary = "----ximecatbox" + uuid.uuid4().hex
    part_name = urllib.parse.quote(name, safe="")
    head = (
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"reqtype\"\r\n\r\n"
        "fileupload\r\n"
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"fileToUpload\"; "
        "filename=\"%s\"\r\n"
        "Content-Type: application/vnd.android.package-archive\r\n\r\n"
        % (boundary, boundary, part_name)
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    hb = Heartbeat("上传到 catbox", total=size).start()
    conn = http.client.HTTPSConnection(u.hostname or "catbox.moe",
                                       port=u.port or 443, timeout=timeout)
    try:
        conn.putrequest("POST", u.path or "/", skip_host=True,
                        skip_accept_encoding=True)
        conn.putheader("Host", u.netloc)
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        conn.putheader("Content-Type",
                       "multipart/form-data; boundary=%s" % boundary)
        conn.putheader("User-Agent", UA)
        conn.putheader("Accept", "*/*")
        conn.putheader("Origin", CATBOX_HOME.rstrip("/"))
        conn.putheader("Referer", CATBOX_HOME)
        conn.putheader("Connection", "close")
        conn.endheaders()
        conn.send(head)
        sent = 0
        with open(apk, "rb") as fh:
            while True:
                chunk = fh.read(BLOCK)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                hb.set(sent=sent)
        conn.send(tail)
        hb.set(phase="文件已发完，等 catbox 校验并返回直链")
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一当失败上报
        return 0, "%s: %s" % (type(exc).__name__, exc)
    finally:
        hb.stop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="把 APK 匿名上传到 catbox.moe（文件名 ime.apk），输出永久直链。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apk", "--file", dest="apk", default=None,
                    help="要发布的 APK（默认 out/ 下最新的 %s）" % APK_PATTERN)
    ap.add_argument("--name", default=DEFAULT_PUBLISH_NAME,
                    help="multipart 里的上传文件名（默认 %s；catbox 会改成随机短名）"
                         % DEFAULT_PUBLISH_NAME)
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE),
                    help="上次上传记录（默认 %s）" % DEFAULT_STATE_FILE)
    ap.add_argument("--dry-run", action="store_true",
                    help="只算 sha256 / 判断是否跳过，不上传")
    ap.add_argument("--force", action="store_true",
                    help="内容未变也强制重新上传")
    ap.add_argument("--check-url", action="store_true",
                    help="结束后 HEAD 一次直链")
    ap.add_argument("--try-anyway", action="store_true",
                    help="已知会被 412 拒绝仍发起上传（默认直接停止并打印原因）")
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="网络操作超时秒数（默认 900）")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    # catbox 已实测封锁（HTTP 412 Invalid uploader，详见文件头记录）：
    # 默认不再白传 63MB，只打印结论；确需复现请显式加 --try-anyway。
    if not args.dry_run and not args.try_anyway:
        log("[blocked] catbox 匿名上传在当前出口被拒：HTTP 412 Invalid uploader")
        log("[blocked] 本地直连 TLS 被重置；VPS/AWS 出口均为 412，详见文件头实测记录")
        log("[blocked] 默认不发起上传；要再试一次加 --try-anyway（住宅 IP 或 userhash 才可能成功）")
        return 7

    apk = Path(args.apk).expanduser() if args.apk else find_latest_apk(DEFAULT_OUT_DIR)
    if apk is None or not apk.is_file():
        die("找不到 APK：%s（用 --apk 指定）" % (apk or DEFAULT_OUT_DIR))
    size = apk.stat().st_size
    if size == 0:
        die("APK 为空：%s" % apk)
    if size > MAX_FILE_MB * 1024 * 1024:
        die("APK %s 超过 catbox 单文件 %.0fMB 上限" % (human_size(size), MAX_FILE_MB),
            code=3)
    digest = sha256_file(apk)
    log("[apk] %s  %s" % (apk, human_size(size)))
    log("[hash] sha256=%s" % digest)

    state_path = Path(os.path.expanduser(args.state_file))
    state = load_state(state_path)
    last_url = str(state.get("url") or "")
    same = (last_url and str(state.get("sha256") or "") == digest
            and int(state.get("size") or 0) == size)

    if same and not args.force:
        alive = url_alive(last_url, min(args.timeout, 120.0))
        log("[state] 上次直链可用=%s：%s" % ("是" if alive else "否", last_url))
        if alive:
            if args.dry_run:
                log("[dry-run] 内容未变化且直链可访问，实际运行会直接复用上述直链")
                return 0
            log("[skip] 内容未变化（sha256/大小一致）且直链仍可访问，跳过上传")
            log("[link] 直链: %s" % last_url)
            return 0
        log("[state] 直链已失效或探测失败，将重新上传")
    elif last_url:
        log("[state] 上次记录：%s（%s）"
            % (last_url, "--force 强制重传" if same else "内容已变化"))
    else:
        log("[state] 无历史记录")

    if args.dry_run:
        log("[dry-run] 将上传 %s → %s（上传文件名 %s），未发起上传"
            % (human_size(size), UPLOAD_URL, args.name))
        return 0

    log("[upload] POST %s（%s，multipart 流式）" % (UPLOAD_URL, human_size(size)))
    started = time.monotonic()
    status, text = upload_file(apk, args.name, size, args.timeout)
    elapsed = time.monotonic() - started
    body = text.strip()

    link = ""
    for token in body.split():
        if token.startswith(DIRECT_PREFIXES):
            link = token
            break

    if status == 0:
        log("[upload] 失败：%s（用时 %.1fs）" % (body[:300], elapsed))
        return 4
    if not 200 <= status < 300:
        log("[upload] 失败：HTTP %s %s（用时 %.1fs）"
            % (status, body[:300] or "(空响应)", elapsed))
        return 5
    if not link:
        log("[upload] catbox 未返回直链：%s（用时 %.1fs）"
            % (body[:300] or "(空响应)", elapsed))
        return 6

    log("[upload] 完成 %.1fs 平均 %s"
        % (elapsed, speed(size / max(1e-9, elapsed))))
    save_state(state_path, {
        "name": args.name, "sha256": digest, "size": size, "url": link,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"), "local": str(apk),
    })

    log("[link] 直链: %s" % link)
    if args.check_url:
        log("[check] 链接探测: %s"
            % ("可访问" if url_alive(link, min(args.timeout, 120.0)) else "探测失败"))
    log("[done] %s 已发布，%s（catbox 随机短名，扩展名 .apk）"
        % (args.name, human_size(size)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
