#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_apk_all.py - 从所有发布渠道下载同一份 ime.apk，实测速度并核对哈希/大小。

渠道（可用 --only / --skip 过滤）：
  pa       pythonanywhere 直链（publish_apk_all.py 的 rpc 上传点）
  ghfast   GitHub 经 ghfast.top 加速
  github   GitHub 源站 raw 跳转链（github.com/.../raw/main/ime.apk）
  raw      raw.githubusercontent.com 直连
  gitee    Gitee 发行版附件（gitee.com/qgbcs/suishou/releases/download/apk/ime.apk）
  npm      unpkg（npm 包 qgb@latest 包内的 ime.apk）

每个渠道并行下载（默认不落盘，边下边算 sha256/md5），每 3s 打印实时状态：
  [pa] 下载 12.3MiB/62.9MiB (19.5%) 即时 0.5MiB/s 平均 0.4MiB/s 用时 25s TTFB 12.4s
久无新字节时改打 ⏳ 行（说明仍在连接/传输，不是卡死；pythonanywhere 免费站点
实测 TTFB≈129s，因为服务端先缓冲整份 63MB 再回包）。

最后输出一张表：HTTP / 大小 / 用时 / TTFB / 平均速度 / sha256 / md5 / 是否与基准一致。
基准优先级：--ref-sha > 本地 out/ 最新 APK > 成功渠道的多数共识哈希。

用法：

    python3 check_apk_all.py                      # 6 条渠道全测；默认核对 4 个发布来源
    python3 check_apk_all.py --only pa,npm        # 只测指定渠道
    python3 check_apk_all.py --require ""         # 关闭四来源强制核对
    python3 check_apk_all.py --require pa,ghfast,npm,gitee   # 换一套必需来源
    python3 check_apk_all.py --keep --dir /tmp/apk_check
    python3 check_apk_all.py --ref-sha <sha256> --json /tmp/check.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 配置

# 不在脚本里写死：由 --pa-base 或环境变量 XIME_PA_BASE 提供。
PA_BASE = os.environ.get("XIME_PA_BASE", "").strip()
GH_REPO = "QGB/suishou"
GITEE_REPO = "qgbcs/suishou"
GITEE_RELEASE_TAG = "apk"
BRANCH = "main"
NPM_PACKAGE = "qgb"
NAME = "ime.apk"

DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
BLOCK = 1 << 20
UA = "xime-check-apk/1.0"

_PRINT_LOCK = threading.Lock()


def log(prefix: str, msg: str) -> None:
    # 多线程并行下载时，print() 会先 write(text) 再 write("\n")，两条线程插进来就会
    # 出现“…271MiB/s[raw] 完成…”这种粘连。加锁后整行原子输出。
    with _PRINT_LOCK:
        print("[%s] %s" % (prefix, msg), flush=True)


def human_size(n: float) -> str:
    n = float(n)
    if n <= 0:
        return "0B"
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


def channels() -> "list[tuple[str, str, str]]":
    gh = "https://github.com/%s/raw/%s/%s" % (GH_REPO, BRANCH, NAME)
    return [
        ("pa", "pythonanywhere 直链",
         (PA_BASE.rsplit("/-", 1)[0] + "/" + NAME) if PA_BASE else ""),
        ("ghfast", "GitHub 经 ghfast.top", "https://ghfast.top/" + gh),
        ("github", "GitHub 源站 raw 跳转", gh),
        ("raw", "raw.githubusercontent", "https://raw.githubusercontent.com/%s/%s/%s"
         % (GH_REPO, BRANCH, NAME)),
        ("gitee", "Gitee 发行版附件", "https://gitee.com/%s/releases/download/%s/%s"
         % (GITEE_REPO, GITEE_RELEASE_TAG, NAME)),
        ("npm", "npm unpkg @latest", "https://unpkg.com/%s@latest/%s" % (NPM_PACKAGE, NAME)),
    ]


# ---------------------------------------------------------------- 实时状态

class Reporter:
    """每 interval 秒打一行“已下载/总量、即时/平均速度、用时、TTFB”；停滞也照报状态。"""

    def __init__(self, prefix: str, interval: float = 3.0) -> None:
        self.prefix = prefix
        self.interval = interval
        self.phase = "建立连接"
        self.sent = 0
        self.total = 0
        self.ttfb: "float | None" = None
        self.started = time.monotonic()
        self.moved = self.started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "Reporter":
        self._thread.start()
        return self

    def set(self, phase: "str | None" = None, sent: "int | None" = None,
            total: "int | None" = None, ttfb: "float | None" = None) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if sent is not None:
                self.sent = int(sent)
            if total is not None:
                self.total = int(total)
            if ttfb is not None:
                self.ttfb = float(ttfb)
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
                phase, sent, total = self.phase, self.sent, self.total
                moved, ttfb = self.moved, self.ttfb
            elapsed = now - self.started
            idle = now - moved
            head = "TTFB %.1fs" % ttfb if ttfb is not None else ""
            tail = ("，已下载 %s/%s" % (human_size(sent), human_size(total))) if total else ""
            if idle >= self.interval:
                log(self.prefix, "⏳ %s：已 %.0fs 无新字节（连接仍在，不是卡死；累计 %.0fs %s%s）"
                    % (phase, idle, elapsed, head, tail))
                last_n, last_t = sent, now
                continue
            if not total:
                log(self.prefix, "%s：累计 %.0fs（等待响应头/首字节）" % (phase, elapsed))
                last_n, last_t = sent, now
                continue
            inst = (sent - last_n) / (now - last_t) if now > last_t else 0.0
            avg = sent / elapsed if elapsed > 0 else 0.0
            log(self.prefix, "%s：%s/%s (%.1f%%) 即时 %s 平均 %s 用时 %.0fs %s"
                % (phase, human_size(sent), human_size(total),
                   100.0 * sent / total, speed(inst), speed(avg), elapsed, head))
            last_n, last_t = sent, now


# ---------------------------------------------------------------- 单渠道下载

def fetch_channel(key: str, label: str, url: str, timeout: float,
                  save_dir: "Path | None") -> dict:
    rep = Reporter(key).start()
    started = time.monotonic()
    info: dict = {"key": key, "label": label, "url": url, "ok": False,
                  "status": 0, "size": 0, "sha256": "", "md5": "",
                  "ttfb": None, "elapsed": 0.0, "speed": 0.0, "error": "",
                  "saved": ""}
    resp = None
    sink = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                  "Accept-Encoding": "identity"})
        rep.set(phase="等待响应头")
        resp = urllib.request.urlopen(req, timeout=timeout)
        ttfb = time.monotonic() - started
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        info["status"] = resp.status
        info["ttfb"] = ttfb
        info["final_url"] = resp.geturl()
        rep.set(phase="下载", total=total, ttfb=ttfb)
        log(key, "HTTP %s，TTFB %.2fs，Content-Length %s"
            % (resp.status, ttfb, human_size(total) if total else "未知"))
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            target = save_dir / ("%s_%s" % (key, NAME))
            sink = open(target, "wb")
            info["saved"] = str(target)
        h, m = hashlib.sha256(), hashlib.md5()
        sent = 0
        while True:
            chunk = resp.read(BLOCK)
            if not chunk:
                break
            sent += len(chunk)
            h.update(chunk)
            m.update(chunk)
            if sink is not None:
                sink.write(chunk)
            rep.set(sent=sent)
        elapsed = time.monotonic() - started
        info.update({"size": sent, "sha256": h.hexdigest(), "md5": m.hexdigest(),
                     "elapsed": elapsed,
                     "speed": sent / elapsed if elapsed > 0 else 0.0,
                     "transfer_speed": sent / (elapsed - ttfb) if elapsed > ttfb else 0.0})
        if total and sent != total:
            info["error"] = "长度不符：Content-Length=%d，实收 %d" % (total, sent)
        else:
            info["ok"] = True
        log(key, "完成：%s，用时 %.1fs（TTFB %.1fs + 传输 %.1fs），平均 %s，传输期 %s"
            % (human_size(sent), elapsed, ttfb or 0.0, max(0.0, elapsed - (ttfb or 0.0)),
               speed(info["speed"]), speed(info["transfer_speed"])))
    except urllib.error.HTTPError as exc:
        info["status"] = exc.code
        try:
            body = (exc.read() or b"")[:300].decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            body = ""
        info["error"] = "HTTP %d %s" % (exc.code, body)
        log(key, "失败：HTTP %d %s" % (exc.code, body.replace("\n", " ")[:200]))
    except Exception as exc:  # noqa: BLE001 - 网络/超时一律记为失败，不影响其它渠道
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
        log(key, "失败：%s（已用 %.1fs）" % (info["error"], time.monotonic() - started))
    finally:
        rep.stop()
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if sink is not None:
            try:
                sink.close()
            except Exception:  # noqa: BLE001
                pass
        info.setdefault("elapsed", time.monotonic() - started)
    return info


# ---------------------------------------------------------------- 报表

def cjk_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cjk_width(text))


def print_table(rows: "list[dict]") -> None:
    cols = [("通道", 8), ("HTTP", 6), ("大小", 9), ("用时", 7), ("TTFB", 7),
            ("平均速度", 11), ("sha256", 20), ("md5", 12), ("结论", 0)]
    header = "  ".join(pad(name, w) for name, w in cols).rstrip()
    print("\n" + header, flush=True)
    print("-" * cjk_width(header), flush=True)
    for r in rows:
        cells = [
            pad(r["key"], 8),
            pad(str(r["status"] or "-"), 6),
            pad(human_size(r["size"]) if r["size"] else "-", 9),
            pad("%.1fs" % r["elapsed"], 7),
            pad("%.1fs" % r["ttfb"] if r["ttfb"] is not None else "-", 7),
            pad(speed(r["speed"]) if r["size"] else "-", 11),
            pad((r["sha256"][:18] + "…") if r["sha256"] else "-", 20),
            pad((r["md5"] or "-")[:12], 12),
            r["verdict"],
        ]
        print("  ".join(cells).rstrip(), flush=True)


def decide_ref(args, rows: "list[dict]") -> "tuple[str, int, str]":
    """返回 (基准 sha256, 基准大小, 基准来源)；--ref-sha > 本地 APK > 多数共识。"""
    if args.ref_sha:
        return args.ref_sha.strip().lower(), int(args.ref_size or 0), "--ref-sha"
    if not args.no_local_ref:
        apk = Path(args.ref_apk).expanduser() if args.ref_apk else find_latest_apk(DEFAULT_OUT_DIR)
        if apk and apk.is_file():
            return sha256_file(apk), apk.stat().st_size, "本地 %s" % apk
    good = [r for r in rows if r["ok"] and r["sha256"]]
    if good:
        counts: "dict[str, list[int]]" = {}
        for r in good:
            counts.setdefault(r["sha256"], []).append(r["size"])
        best = max(counts.items(), key=lambda kv: len(kv[1]))
        size = max(set(best[1]), key=best[1].count)
        return best[0], size, "多数共识（%d/%d 条成功渠道）" % (len(best[1]), len(good))
    return "", 0, "无基准"


# ---------------------------------------------------------------- 主流程

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="并行下载各渠道 ime.apk，实测速度并核对 sha256/md5/大小。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="只测这些渠道，逗号分隔（pa,ghfast,gitee,npm,raw,github）")
    ap.add_argument("--skip", default="", help="跳过这些渠道，逗号分隔")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="单渠道读超时秒数（默认 240；pythonanywhere TTFB 可能 ~130s）")
    ap.add_argument("--ref-apk", default=None, help="基准 APK 文件（默认 out/ 下最新）")
    ap.add_argument("--ref-sha", default=None, help="基准 sha256（优先于本地文件）")
    ap.add_argument("--ref-size", type=int, default=0, help="配合 --ref-sha 的基准字节数")
    ap.add_argument("--no-local-ref", action="store_true",
                    help="不用本地 APK 当基准，改按成功渠道的多数共识比对")
    ap.add_argument("--keep", action="store_true", help="保留下载文件（默认边下边算完即弃）")
    ap.add_argument("--dir", default="/tmp/apk_check", help="--keep 时的保存目录（默认 /tmp/apk_check）")
    ap.add_argument("--json", default=None, help="把结果表另存为 JSON")
    ap.add_argument("--require", default="pa,github,npm,gitee",
                    help="必须全部与基准一致的来源，逗号分隔（默认 pa,github,npm,gitee；传空串关闭）")
    ap.add_argument("--pa-base", default=None,
                    help="pythonanywhere RPC base（也可用环境变量 XIME_PA_BASE）")
    ap.add_argument("--npm-package", default=None, help="npm 包名（默认 %s）" % NPM_PACKAGE)
    ap.add_argument("--npm-name", default=None, help="包内文件名（默认 %s）" % NAME)
    ap.add_argument("--gitee-repo", default=None, help="Gitee 仓库（默认 %s）" % GITEE_REPO)
    ap.add_argument("--gitee-tag", default=None, help="Gitee 发行版 tag（默认 %s）" % GITEE_RELEASE_TAG)
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    global PA_BASE, NPM_PACKAGE, NAME, GITEE_REPO, GITEE_RELEASE_TAG
    if args.pa_base:
        PA_BASE = args.pa_base
    if args.npm_package:
        NPM_PACKAGE = args.npm_package
    if args.npm_name:
        NAME = args.npm_name
    if args.gitee_repo:
        GITEE_REPO = args.gitee_repo
    if args.gitee_tag:
        GITEE_RELEASE_TAG = args.gitee_tag
    all_ch = channels()
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    skip = [s.strip() for s in args.skip.split(",") if s.strip()]
    unknown = [k for k in only if k not in dict((c[0], c) for c in all_ch)]
    if unknown:
        print("[check] 未知渠道：%s（可选 %s）"
              % (",".join(unknown), ",".join(c[0] for c in all_ch)), flush=True)
        return 2
    picked = [c for c in all_ch if (not only or c[0] in only) and c[0] not in skip]
    if any(c[0] == "pa" for c in picked) and not PA_BASE:
        print("[check] 缺少 --pa-base（或环境变量 XIME_PA_BASE），无法测 pa 渠道",
              flush=True)
        return 2
    if not picked:
        print("[check] 没有可测渠道", flush=True)
        return 2

    save_dir = Path(args.dir).expanduser() if args.keep else None
    print("[check] 渠道: %s" % ", ".join(c[0] for c in picked), flush=True)
    print("[check] 并行下载（每 3s 打点，停滞会打 ⏳ 而不是干等）…", flush=True)
    started = time.monotonic()

    results: "list[dict]" = []
    threads = []
    for key, label, url in picked:
        t = threading.Thread(target=lambda k=key, lb=label, u=url:
                             results.append(fetch_channel(k, lb, u, args.timeout, save_dir)),
                             daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started

    ref_sha, ref_size, ref_src = decide_ref(args, results)
    for r in results:
        if not r["ok"]:
            r["verdict"] = "失败"
        elif not ref_sha:
            r["verdict"] = "无基准"
        elif r["sha256"] == ref_sha and (not ref_size or r["size"] == ref_size):
            r["verdict"] = "一致"
        elif r["size"] != ref_size and ref_size:
            r["verdict"] = "大小不符"
        else:
            r["verdict"] = "哈希不符"
    results.sort(key=lambda r: (not r["ok"], -r["size"], r["key"]))

    print_table(results)
    ok = [r for r in results if r["ok"]]
    good = [r for r in ok if ref_sha and r["verdict"] == "一致"]
    bad = [r for r in results if not r["ok"] or (ref_sha and r["verdict"] != "一致")]
    print("\n[check] 基准: %s%s" % (ref_src, ("  sha256 " + ref_sha) if ref_sha else ""),
          flush=True)
    print("[check] 共 %d 条：成功 %d，与基准一致 %d，异常 %d，总耗时 %.1fs，共下载 %s"
          % (len(results), len(ok), len(good), len(bad), elapsed,
             human_size(sum(r["size"] for r in results))), flush=True)
    if bad:
        print("[check] 异常渠道：", flush=True)
        for r in bad:
            print("  - %-7s %s%s" % (r["key"], r["verdict"],
                                     ("：" + r["error"]) if r["error"] else ""), flush=True)

    if args.json:
        p = Path(args.json).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ref": {"sha256": ref_sha, "size": ref_size, "source": ref_src},
                                 "channels": results, "elapsed": elapsed},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print("[check] JSON 已写入 %s" % p, flush=True)

    if save_dir is not None:
        print("[check] 下载文件保留在 %s" % save_dir, flush=True)

    require = [s.strip() for s in args.require.split(",") if s.strip()]
    req_ok = True
    if require:
        by_key = {r["key"]: r for r in results}
        missing = [k for k in require if k not in by_key]
        bad_req = [k for k in require if k in by_key and by_key[k]["verdict"] != "一致"]
        print("[check] 必需来源：%s" % "、".join(require), flush=True)
        if missing:
            print("[check] 必需来源未测到：%s（被 --only/--skip 排除？）"
                  % "、".join(missing), flush=True)
        if bad_req:
            print("[check] 必需来源未通过：%s" % "、".join(bad_req), flush=True)
        req_ok = not missing and not bad_req
        print("[check] 四来源核对：%s" % ("PASS" if req_ok else "FAIL"), flush=True)

    if require:
        return 0 if req_ok else 1
    if not ref_sha:
        return 0 if ok else 1
    return 0 if all(r["verdict"] == "一致" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
