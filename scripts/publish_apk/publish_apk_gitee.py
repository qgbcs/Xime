#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_gitee.py - 把 xime 的 APK 上传到 Gitee 发行版附件，输出匿名下载直链。

为什么单独一个文件:
- Gitee 相关逻辑已从 publish_apk_git.py 完全拆出，那个脚本只负责 GitHub；
  本脚本负责 Gitee 发行版附件，publish_apk_all.py 默认把本脚本作为第 4 条通道并发调用
  （传 --release --publish）。
- 单独执行本脚本时默认只演练：不带 --publish 不建发行版、不上传附件。

为什么不用 Gitee raw 直链:
- Gitee raw 对 >10MB 文件匿名访问直接 403，必须登录或带私人 Token：
  https://gitee.com/qgbcs/qgbcs/raw/main/ime.apk   -> 匿名 403（大文件）
- Gitee Pages 把仓库当静态站点托管，不受 raw 的 10MB 匿名 403 限制：
  https://qgbcs.gitee.io/ime.apk

Gitee Pages 的现实限制（务必知道）:
- 总流量风控：高频/大量下载会被临时屏蔽，不是无限 CDN。
- 免费版每次 push 后要在网页端 “服务 -> Gitee Pages -> 更新” 才会重新部署；
  只有 Pages Pro 才自动部署。
- Pages 服务需要先在仓库设置里人工开启一次，本脚本无法自动开启。
- 仓库里是否有 index.html 不影响直接访问 /ime.apk，但根路径会 404。

仓库与链接:
- 默认仓库: git@gitee.com:qgbcs/suishou.git（main 分支）
- 发行版直链: https://gitee.com/qgbcs/suishou/releases/download/apk/ime.apk
- raw 直链 : https://gitee.com/qgbcs/suishou/raw/main/ime.apk（>10MB 匿名 403，不用作分发）

前置条件:
- 本机有 SSH 私钥（默认 ~/.ssh/NIST256p.pem，缺失直接退出）。
- 该 key 对目标仓库有写权限（同一把 key 已能访问 qgbcs/suishou）。
- 仓库已在 Gitee 建好；Pages 需人工在网页端开启。

典型用法:

    # 默认：只演练（校验 key/仓库/APK，打印两条链接与探测结果）
    python3 publish_apk_gitee.py

    # 真正推送（写入 ime.apk + SHA256SUMS.txt，commit+push）
    python3 publish_apk_gitee.py --publish

    # 指定 APK / 仓库 / 分支 / 工作副本
    python3 publish_apk_gitee.py --publish --apk out/xxx.apk \\
        --repo git@gitee.com:qgbcs/suishou.git --branch main --dir /root/suishou_gitee

    # 推送后探测两条链接（raw 大文件应为 403，Pages 开启后应为 200）
    python3 publish_apk_gitee.py --publish --check-url

    # 发行版附件（Pages 下线后推荐）：匿名直链，单文件 ≤100MB
    python3 publish_apk_gitee.py --release            # 演练，只看链接/发行版状态
    python3 publish_apk_gitee.py --release --publish  # 建发行版并上传 ime.apk

实测记录（2026-10-31，repo=qgbcs/qgbcs）:

1) 空仓库首推 43B 测试文件 pages_test.txt
   $ git clone git@gitee.com:qgbcs/qgbcs.git -> ok
   commit a3e88b5，push 用时 5.2s（* [new branch] main -> main）
   匿名 raw : HTTP 200（43B）
             https://gitee.com/qgbcs/qgbcs/raw/main/pages_test.txt
   Pages    : HTTP 404（服务未开启，需人工在网页端开启并点“更新”）

2) 推 62.9MiB 的 ime.apk（同一脚本 --publish）时被 Gitee 限速
   Writing objects: 60% (3/5), 9.55 MiB | 12.00 KiB/s（约 13 KiB/s）
   按这个速度 63MB 需要约 80 分钟，脚本 1800s 超时后会终止；
   本次在第 632s、9.55MiB 处手动 Ctrl-C，远端 ref 未更新，仓库里仍然
   只有上面那次测试提交。
   结论：从这台境外 VPS 直推大文件不可行。要在国内网络/本机推，
   或用 Gitee 网页端上传（网页单文件上限 100MB），再手动更新 Pages。

3) Pages 免费版每次 push 后都必须在网页端“服务 -> Gitee Pages -> 更新”；
   本脚本没有 Pages 部署 API，只能把文件推到仓库并打印 raw/Pages 两条链接。

4) 第二次小文件测试 pages_test2.txt（98B，--publish --check-url）
   commit 9c680ae（amend 覆盖 a3e88b5，仓库始终只有一次提交），
   push 用时 5.4s（forced update a3e88b5...9c680ae main -> main）
   匿名 raw : HTTP 200（98B）https://gitee.com/qgbcs/qgbcs/raw/main/pages_test2.txt
   Pages    : HTTP 404  https://qgbcs.gitee.io/pages_test2.txt
   再次确认：Pages 服务未开启时一律 404，与文件大小/推送方式无关；
   必须先在网页端开启 Pages 并部署，Pages 链接才可能 200。

5) 2026-10-31 复核：Gitee Pages 已彻底下线（服务菜单里根本没有 Pages 入口）。
   官方回复："由于不可抗原因，Pages 服务已经下线，暂无具体计划再次上线"，
   2024 年关闭，只对企业版保留。所以 qgbcs.gitee.io 永远 404，之前的
   "开启 Pages" 步骤已不存在。改用【发行版附件】：
   - 单附件 ≤100MB、每仓库附件 ≤1G；匿名下载无需登录（实测公开仓库附件 200）
   - 直链：https://gitee.com/qgbcs/suishou/releases/download/apk/ime.apk
   - 实测：建 tag=apk 发行版（id=1260795），上传 98B 测试附件后匿名 GET 200（0.55s）。
   - 实测（2026-10-31，本机 Windows，curl -> Gitee API）：ime.apk 65,922,493B，
     上传 24.2s（2.7MB/s），匿名下载 46.2s（1.4MB/s），SHA256 与源文件一致。
     同一台境外 VPS 直传同一文件超 5 分钟仍未完成，确认大文件必须国内网络上传。
   - 注意：境外 VPS 直传 Gitee 被限速（git push 约 12KB/s，API 上传同样慢），
     大文件建议在国内网络/本机上传；本机实测 clone 1.2s、push 1.4s。
   - 新增 --release 模式（默认仍只演练）：--publish 时才建发行版并上传附件。
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_REPO = "git@gitee.com:qgbcs/suishou.git"
DEFAULT_BRANCH = "main"
DEFAULT_WORKDIR = Path("/root/suishou_gitee")
DEFAULT_KEY = Path("~/.ssh/NIST256p.pem")
DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
DEFAULT_PUBLISH_NAME = "ime.apk"
DEFAULT_MESSAGE = "ime.apk 覆盖更新（xime arm64-v8a，Gitee Pages）"
SUMS_NAME = "SHA256SUMS.txt"
# Gitee 免费仓库单文件上限 100MB；超过直接拦下
MAX_FILE_MIB = 100.0
UA = "xime-publish-gitee/1.0"

# ---- 发行版（Release）附件：Pages 下线后唯一可用的匿名直链方案 ----
# 单附件 ≤100MB、每仓库附件 ≤1G，匿名下载不需要登录，走国内 CDN。
GITEE_API = "https://gitee.com/api/v5"
RELEASE_TAG = "apk"
RELEASE_NAME = "xime APK"
RELEASE_BODY = "ime.apk 直链（Gitee 发行版附件，匿名可下载）"
# 私人令牌不进仓库：默认读环境变量 GITEE_TOKEN，缺失时用 --token 显式传入。
# 历史上这里曾硬编码 qgbcs 的令牌，已清理；旧令牌请尽快在 Gitee 注销。
DEFAULT_TOKEN = os.environ.get("GITEE_TOKEN", "").strip()

_PRINT_LOCK = threading.Lock()


def log(msg: str = "") -> None:
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


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sums_text(name: str, digest: str) -> str:
    """sha256sum -c 兼容格式：摘要 + 两个空格 + 文件名 + 换行。"""
    return "%s  %s\n" % (digest, name)


# ---------------------------------------------------------------- key / git

def check_key(key: Path) -> Path:
    p = key.expanduser()
    if not p.is_file():
        die("找不到 SSH 私钥：%s（请先用 --key 指定）" % p, code=4)
    if p.stat().st_size == 0:
        die("SSH 私钥为空：%s" % p, code=4)
    if not os.access(p, os.R_OK):
        die("SSH 私钥不可读：%s" % p, code=4)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def ssh_env(key: Path) -> "dict[str, str]":
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = (
        "ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" % key)
    return env


def git(cmd: "list[str]", cwd: Path, env: "dict[str, str]", timeout: float = 900.0,
        allow_fail: bool = False) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git"] + list(cmd), cwd=str(cwd), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die("git %s 超时（%.0fs）" % (" ".join(cmd), timeout), code=1)
    if proc.returncode != 0 and not allow_fail:
        die("git %s 失败（rc=%s）\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(cmd), proc.returncode,
               (proc.stdout or "").strip(), (proc.stderr or "").strip()), code=1)
    return proc


def git_stream(cmd: "list[str]", cwd: Path, env: "dict[str, str]",
               timeout: float) -> int:
    """流式 git（push 用）：输出边产生边打印，沉默时打心跳，超时自动终止。"""
    proc = subprocess.Popen(["git"] + list(cmd), cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    started = time.monotonic()
    last = [started]
    stop = threading.Event()
    killed = [False]

    def watch() -> None:
        while not stop.wait(5.0):
            now = time.monotonic()
            elapsed, idle = now - started, now - last[0]
            if elapsed >= timeout:
                killed[0] = True
                log("  ⏱ 已运行 %.0fs 超过 %.0fs 上限，终止 git %s"
                    % (elapsed, timeout, " ".join(cmd[:2])))
                proc.terminate()
                return
            if idle >= 5.0:
                log("  推送中：已 %.0fs 无新输出（pid=%s 仍在跑，累计 %.0fs）"
                    % (idle, proc.pid, elapsed))

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            last[0] = time.monotonic()
            with _PRINT_LOCK:
                print(line.rstrip("\n"), flush=True)
    finally:
        stop.set()
        t.join(timeout=2.0)
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
    rc = proc.wait()
    log("  推送结束：rc=%s，用时 %.1fs%s"
        % (rc, time.monotonic() - started, "（被超时终止）" if killed[0] else ""))
    return rc


def owner_repo(url: str) -> "tuple[str, str]":
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = [p for p in re.split(r"[:/]", u) if p]
    if len(parts) < 2:
        die("无法从仓库地址解析 owner/repo：%s" % url)
    return parts[-2], parts[-1]


def ensure_workdir(target: Path, repo: str, branch: str,
                   env: "dict[str, str]") -> Path:
    if (target / ".git").is_dir():
        cur = git(["remote", "get-url", "origin"], target, env).stdout.strip()
        if cur.rstrip("/") != repo.rstrip("/"):
            die("工作目录 %s 的 origin 是 %s，与 --repo %s 不一致；"
                "换个 --dir 或先手动清理" % (target, cur, repo))
        git(["fetch", "origin", "--prune"], target, env, allow_fail=True)
        has_branch = git(["rev-parse", "--verify", "origin/%s" % branch], target,
                         env, allow_fail=True)
        if has_branch.returncode == 0:
            git(["checkout", branch], target, env, allow_fail=True)
            git(["reset", "--hard", "origin/%s" % branch], target, env, allow_fail=True)
        else:
            git(["checkout", "-B", branch], target, env, allow_fail=True)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and any(target.iterdir()):
        die("目录 %s 已存在且不是 git 仓库，请清空或换 --dir" % target)
    log("$ git clone %s %s" % (repo, target))
    git(["clone", repo, str(target)], target.parent, env)
    has_branch = git(["rev-parse", "--verify", "origin/%s" % branch], target,
                     env, allow_fail=True)
    if has_branch.returncode == 0:
        git(["checkout", branch], target, env, allow_fail=True)
    else:
        git(["checkout", "-B", branch], target, env, allow_fail=True)
    return target


# ---------------------------------------------------------------- helpers

def find_latest_apk(out_dir: Path) -> "Path | None":
    if not out_dir.is_dir():
        return None
    cands = [p for p in out_dir.glob(APK_PATTERN) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: (p.stat().st_mtime, p.name))


def head_count(workdir: Path, env: "dict[str, str]") -> int:
    proc = git(["rev-list", "--count", "HEAD"], workdir, env, allow_fail=True)
    if proc.returncode != 0:
        return 0
    try:
        return int((proc.stdout or "0").strip() or "0")
    except ValueError:
        return 0


def remote_head(workdir: Path, env: "dict[str, str]", branch: str) -> "str | None":
    proc = git(["ls-remote", "origin", "refs/heads/%s" % branch], workdir, env,
               allow_fail=True)
    out = (proc.stdout or "").strip()
    return out.split("\t")[0] if out else None


def remote_head_url(repo: str, branch: str, env: "dict[str, str]") -> "str | None":
    proc = git(["ls-remote", repo, "refs/heads/%s" % branch], Path("."), env,
               allow_fail=True)
    out = (proc.stdout or "").strip()
    return out.split("\t")[0] if out else None


def build_links(repo: str, branch: str, name: str,
                pages_host: str) -> "tuple[str, str]":
    """返回 (Pages 直链, raw 直链)；Pages 不受 raw 的 10MB 匿名 403 限制。"""
    owner, rname = owner_repo(repo)
    pages = pages_host.rstrip("/") + "/" + name
    raw = "https://gitee.com/%s/%s/raw/%s/%s" % (owner, rname, branch, name)
    return pages, raw


def probe_url(url: str, timeout: float = 30.0) -> "tuple[int, str]":
    """HEAD 探测链接状态；HEAD 不被支持时退回 1 字节 GET。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            size = resp.headers.get("Content-Length")
            return resp.status, (human_size(int(size)) if size else "unknown")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405, 501):
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                      "Range": "bytes=0-0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, "range-ok"
            except urllib.error.HTTPError as exc2:
                return exc2.code, ""
            except Exception as exc2:  # noqa: BLE001
                return 0, "%s: %s" % (type(exc2).__name__, exc2)
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001 - 探测失败不影响发布结果
        return 0, "%s: %s" % (type(exc).__name__, exc)


def describe(status: int, detail: str, kind: str) -> str:
    if status == 0:
        return "探测失败：%s" % detail
    hint = ""
    if status == 403 and kind == "raw":
        hint = "（>10MB 匿名 403，符合预期，raw 只作对照）"
    elif status == 403:
        hint = "（被拒绝，Pages 可能未开启或流量风控）"
    elif status == 404:
        if kind == "release":
            hint = "（发行版或附件不存在，尚未上传）"
        else:
            hint = "（Pages 未开启/未部署，或文件尚未推送）"
    elif 200 <= status < 300:
        hint = "（可下载，%s）" % (detail or "大小未知")
    return "HTTP %s%s" % (status, hint)


# --------------------------------------------- 发行版附件（--release，匿名直链）


def repo_slug(repo: str) -> str:
    """git@gitee.com:qgbcs/qgbcs.git -> qgbcs/qgbcs（OpenAPI 路径需要）。"""
    owner, name = owner_repo(repo)
    return "%s/%s" % (owner, name)


def api_json(url: str, token: str, method: str = "GET",
             params: "dict | None" = None, timeout: float = 30.0):
    """调 Gitee OpenAPI（私人令牌走 access_token 参数），返回解析后的 JSON。

    HTTP 错误不抛异常，返回 {"__error__": 状态码, "__body__": 原始响应}，
    由调用方区分 404（发行版不存在）与真失败；网络层错误直接 die。
    """
    q = dict(params or {})
    q["access_token"] = token
    data = None
    if method == "GET":
        url = url + "?" + urllib.parse.urlencode(q)
    else:
        data = urllib.parse.urlencode(q).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"__error__": exc.code,
                "__body__": exc.read().decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        die("Gitee API 请求失败：%s: %s" % (type(exc).__name__, exc))
    try:
        return json.loads(body)
    except ValueError:
        if not body.strip():
            # Gitee 的 DELETE 成功时回 204/空 200（无 body），空响应按“成功无数据”处理，
            # 否则删完旧附件就直接 die，上传永远执行不到。
            return {}
        die("Gitee API 返回非 JSON：%s" % body[:200])


def find_release(slug: str, tag: str, token: str, timeout: float):
    """按 tag 查发行版；不存在返回 None。"""
    obj = api_json("%s/repos/%s/releases/tags/%s" % (GITEE_API, slug, tag),
                   token, timeout=timeout)
    if isinstance(obj, dict) and obj.get("__error__") == 404:
        return None
    if isinstance(obj, dict) and obj.get("__error__"):
        die("查询发行版失败 HTTP %s：%s"
            % (obj["__error__"], (obj.get("__body__") or "")[:200]))
    return obj


def ensure_release(slug: str, tag: str, token: str, branch: str,
                   timeout: float) -> dict:
    """拿到 tag 对应的发行版；没有就用目标分支 HEAD 新建一个。"""
    rel = find_release(slug, tag, token, timeout)
    if rel:
        log("发行版   : 已存在 id=%s（tag=%s）" % (rel.get("id"), tag))
        return rel
    log("发行版   : 不存在，创建 tag=%s" % tag)
    rel = api_json("%s/repos/%s/releases" % (GITEE_API, slug), token,
                   method="POST",
                   params={"tag_name": tag, "name": RELEASE_NAME,
                           "body": RELEASE_BODY, "target_commitish": branch},
                   timeout=timeout)
    if isinstance(rel, dict) and rel.get("__error__"):
        die("创建发行版失败 HTTP %s：%s"
            % (rel["__error__"], (rel.get("__body__") or "")[:200]))
    log("发行版   : 已创建 id=%s" % rel.get("id"))
    return rel


def list_attachments(slug: str, release_id, token: str, timeout: float) -> list:
    """列出发行版的附件；失败返回空列表（只用于去重判断）。"""
    obj = api_json("%s/repos/%s/releases/%s/attach_files"
                   % (GITEE_API, slug, release_id), token, timeout=timeout)
    return obj if isinstance(obj, list) else []


def delete_attachment(slug: str, release_id, attach_id, token: str,
                      timeout: float) -> bool:
    """删除一个附件（--force 覆盖同名文件时先删旧的）。"""
    obj = api_json("%s/repos/%s/releases/%s/attach_files/%s"
                   % (GITEE_API, slug, release_id, attach_id),
                   token, method="DELETE", timeout=timeout)
    return not (isinstance(obj, dict) and obj.get("__error__"))


class _UploadMeter:
    """上传实时速度：每 3s 打一行 已发/总量、即时/平均速度、用时；停滞打 ⏳。

    之前用 curl -F，curl 只给最终速度，VPS 被限速时整段上传完全静默（看起来像卡死）。
    改成自己分块发送后，每秒都有字节级进度，慢也能看到“还在动”。
    """

    def __init__(self, total: int, interval: float = 3.0) -> None:
        self.total = int(total)
        self.sent = 0
        self.phase = "上传附件"
        self.interval = interval
        self.started = time.monotonic()
        self.moved = self.started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "_UploadMeter":
        self._thread.start()
        return self

    def set(self, sent: "int | None" = None, phase: "str | None" = None) -> None:
        with self._lock:
            if sent is not None:
                self.sent = int(sent)
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
                phase, sent, total, moved = self.phase, self.sent, self.total, self.moved
            elapsed = now - self.started
            idle = now - moved
            if idle >= self.interval:
                log("  ⏳ %s：已 %.0fs 无新字节（连接仍在，Gitee 限速/排队；累计 %.0fs，已发 %s/%s）"
                    % (phase, idle, elapsed, human_size(sent), human_size(total)))
                last_n, last_t = sent, now
                continue
            inst = (sent - last_n) / (now - last_t) if now > last_t else 0.0
            avg = sent / elapsed if elapsed > 0 else 0.0
            log("  %s %s/%s (%.1f%%) 即时 %s/s 平均 %s/s 用时 %.0fs"
                % (phase, human_size(sent), human_size(total),
                   (100.0 * sent / total) if total else 0.0,
                   human_size(inst), human_size(avg), elapsed))
            last_n, last_t = sent, now


def upload_attachment(slug: str, release_id, apk: Path, filename: str,
                      token: str, timeout: float) -> dict:
    """流式 multipart 上传附件，返回含 browser_download_url 的字典。

    自己用 http.client 分块发送（1MiB 一块）并喂给 _UploadMeter，
    所以慢速网络下也能每 3s 看到已发/总量与实时速度，而不是整段静默。
    """
    size = apk.stat().st_size
    boundary = "----xime%d" % int(time.time() * 1000)
    head = ("--%s\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, filename)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    total = len(head) + size + len(tail)
    url = ("%s/repos/%s/releases/%s/attach_files?access_token=%s"
           % (GITEE_API, slug, release_id, urllib.parse.quote(token)))
    u = urllib.parse.urlparse(url)
    target = (u.path or "/") + (("?" + u.query) if u.query else "")

    log("上传中   : %s（%s，最多等 %ds；每 3s 报一次实时速度）"
        % (filename, human_size(size), int(timeout)))
    meter = _UploadMeter(size).start()
    started = time.monotonic()
    conn = http.client.HTTPSConnection(u.hostname or "gitee.com",
                                       port=u.port or 443, timeout=timeout)
    try:
        conn.putrequest("POST", target, skip_host=False, skip_accept_encoding=True)
        conn.putheader("User-Agent", UA)
        conn.putheader("Content-Type", "multipart/form-data; boundary=%s" % boundary)
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
                meter.set(sent=sent)
        conn.send(tail)
        meter.set(phase="字已发完，等 Gitee 回包")
        resp = conn.getresponse()
        status = resp.status
        body = resp.read()
    except Exception as exc:  # noqa: BLE001 - 超时/断连当作失败上报
        die("上传失败：%s: %s" % (type(exc).__name__, exc), code=1)
    finally:
        meter.stop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    elapsed = time.monotonic() - started
    text = body.decode("utf-8", "replace")
    avg = size / elapsed if elapsed > 0 else 0.0
    log("上传返回 : HTTP %s，用时 %.1fs，平均 %s/s，共 %s"
        % (status, elapsed, human_size(avg), human_size(size)))
    if status not in (200, 201):
        die("上传失败 HTTP %s：%s" % (status, text[:300]), code=1)
    try:
        obj = json.loads(text)
    except ValueError:
        die("上传返回非 JSON：%s" % text[:300])
    if not isinstance(obj, dict) or not obj.get("browser_download_url"):
        die("上传失败：%s" % text[:300])
    return obj


def publish_release(args, apk: Path, size: int, digest: str) -> int:
    """Gitee 发行版附件发布：Pages 下线后的匿名直链方案。

    默认只演练（不建发行版、不上传）；--publish 才真正执行。
    同名附件默认跳过，--force 先删旧附件再上传。
    """
    token = (args.token or "").strip()
    if not token:
        die("缺少 Gitee 私人令牌：用 --token 指定", code=3)
    repo = args.repo.rstrip("/")
    slug = repo_slug(repo)
    tag = args.tag
    link = "https://gitee.com/%s/releases/download/%s/%s" % (slug, tag, args.name)
    log("仓库     : %s" % repo)
    log("发行版链 : %s" % link)
    log("说明     : 匿名可下、单文件 ≤100MB、国内 CDN；Gitee Pages 已于 2024 下线")

    if not args.publish:
        rel = find_release(slug, tag, token, min(args.timeout, 60.0))
        if rel:
            atts = list_attachments(slug, rel.get("id"), token,
                                    min(args.timeout, 60.0))
            log("发行版   : 已存在 id=%s，附件 %d 个" % (rel.get("id"), len(atts)))
        else:
            log("发行版   : 不存在（--publish 时会创建 tag=%s）" % tag)
        log("[dry-run] 默认不启用：不创建发行版、不上传附件（加 --publish 才执行）")
        if args.check_url:
            st, detail = probe_url(link, min(args.timeout, 60.0))
            log("[probe] release: %s" % describe(st, detail, "release"))
        return 0

    rel = ensure_release(slug, tag, token, args.branch, args.timeout)
    rid = rel.get("id")
    for a in list_attachments(slug, rid, token, args.timeout):
        if a.get("name") != args.name:
            continue
        old = int(a.get("size") or 0)
        if not args.force and old == size:
            log("同名附件已存在且大小一致（%s，%s），跳过；要强制重发加 --force"
                % (a.get("name"), human_size(old)))
            log("下载直链 : %s" % (a.get("browser_download_url") or link))
            if args.check_url:
                st, detail = probe_url(link, min(args.timeout, 60.0))
                log("[probe] release: %s" % describe(st, detail, "release"))
            return 0
        if not args.force:
            # 大小不一致说明远端是旧 APK：直接覆盖，保证发行版永远是最新版。
            # （Gitee 附件接口不回 sha256，只能按大小判断；同大小不同内容属极少数，
            #   需要绝对确定时加 --force 无条件重传。）
            log("同名附件大小不一致（远端 %s ≠ 本地 %s），自动覆盖"
                % (human_size(old), human_size(size)))
        log("删除旧附件 %s（id=%s）" % (a.get("name"), a.get("id")))
        if not delete_attachment(slug, rid, a.get("id"), token, args.timeout):
            die("删除旧附件失败，先手动处理再重试", code=1)

    att = upload_attachment(slug, rid, apk, args.name, token, args.timeout)
    url = att.get("browser_download_url") or link
    log("")
    log("发布完成：%s" % repo)
    log("  匿名直链 : %s" % url)
    log("  SHA256   : %s" % digest)
    if args.check_url:
        st, detail = probe_url(url, min(args.timeout, 60.0))
        log("  [probe] release: %s" % describe(st, detail, "release"))
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="把 APK 上传到 Gitee 发行版附件（--release --publish）或推送到仓库。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apk", "--file", dest="apk", default=None,
                    help="要发布的 APK（默认 out/ 下最新的 %s）" % APK_PATTERN)
    ap.add_argument("--name", default=DEFAULT_PUBLISH_NAME,
                    help="仓库内的文件名（默认 %s）" % DEFAULT_PUBLISH_NAME)
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="Gitee 仓库地址（默认 %s）" % DEFAULT_REPO)
    ap.add_argument("--branch", default=DEFAULT_BRANCH,
                    help="分支（默认 %s）" % DEFAULT_BRANCH)
    ap.add_argument("--dir", default=str(DEFAULT_WORKDIR),
                    help="本地工作副本（默认 %s，不存在时 clone）" % DEFAULT_WORKDIR)
    ap.add_argument("--key", default=str(DEFAULT_KEY),
                    help="SSH 私钥（默认 %s）" % DEFAULT_KEY)
    ap.add_argument("--pages-host", default="",
                    help="Pages 站点根（默认按 owner 拼 https://<owner>.gitee.io）")
    ap.add_argument("--message", default=DEFAULT_MESSAGE, help="提交信息")
    ap.add_argument("--release", action="store_true",
                    help="走发行版附件（匿名直链，单文件≤100MB；Pages 已下线，推荐）")
    ap.add_argument("--tag", default=RELEASE_TAG,
                    help="发行版 tag（默认 %s）" % RELEASE_TAG)
    ap.add_argument("--token", default=DEFAULT_TOKEN,
                    help="Gitee 私人令牌（默认读环境变量 GITEE_TOKEN）")
    ap.add_argument("--publish", action="store_true",
                    help="真正写入/commit/push（默认只演练，不启用发布）")
    ap.add_argument("--force", action="store_true",
                    help="仓库里 ime.apk 与本地相同时也照常重写提交（默认跳过）")
    ap.add_argument("--reset-history", action="store_true",
                    help="历史超过 1 个提交时重写为单提交（仍用 force-with-lease）")
    ap.add_argument("--check-url", action="store_true",
                    help="结束后探测 Pages/raw 两条链接")
    ap.add_argument("--timeout", type=float, default=7200.0,
                    help="网络/上传超时秒数（默认 7200；慢速网络传 63MB 可能超过 10 分钟）")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    key = None
    if not args.release:
        # 发行版附件走 HTTPS + 令牌，不需要 SSH key；只有 git 推送模式才校验
        key = check_key(Path(args.key))
        log("SSH key  : %s" % key)

    apk = Path(args.apk).expanduser() if args.apk else find_latest_apk(DEFAULT_OUT_DIR)
    if apk is None or not apk.is_file():
        die("找不到 APK：%s（用 --apk 指定）" % (apk or DEFAULT_OUT_DIR))
    size = apk.stat().st_size
    if size == 0:
        die("APK 为空：%s" % apk)
    if size > MAX_FILE_MIB * 1024 * 1024:
        die("APK %s 超过 Gitee 单文件 %.0fMiB 上限" % (human_size(size), MAX_FILE_MIB),
            code=3)
    digest = sha256_file(apk)
    sums = sums_text(args.name, digest)
    log("APK      : %s (%s)" % (apk, human_size(size)))
    log("SHA256   : %s" % digest)

    if args.release:
        # 发行版附件模式：直接返回，不进入下面的 git 写入/推送
        return publish_release(args, apk, size, digest)

    env = ssh_env(key)
    repo = args.repo.rstrip("/")
    branch = args.branch
    pages_host = args.pages_host or ("https://%s.gitee.io" % owner_repo(repo)[0])
    pages, raw = build_links(repo, branch, args.name, pages_host)
    sums_pages, sums_raw = build_links(repo, branch, SUMS_NAME, pages_host)
    log("仓库     : %s (%s)" % (repo, branch))
    log("Pages 链 : %s" % pages)
    log("raw 链   : %s（>10MB 匿名 403）" % raw)

    if not args.publish:
        rhead = remote_head_url(repo, branch, env)
        log("远端分支 : %s" % (rhead or "(空仓库/无该分支)"))
        log("[dry-run] 默认不启用：不写文件、不 commit、不 push（加 --publish 才执行）")
        if args.check_url:
            st, detail = probe_url(raw, min(args.timeout, 60.0))
            log("[probe] raw   : %s" % describe(st, detail, "raw"))
            st, detail = probe_url(pages, min(args.timeout, 60.0))
            log("[probe] pages : %s" % describe(st, detail, "pages"))
        return 0

    workdir = ensure_workdir(Path(args.dir).expanduser(), repo, branch, env)
    log("工作副本 : %s" % workdir)

    local_blob = git(["hash-object", str(apk)], workdir, env,
                     allow_fail=True).stdout.strip()
    head_blob = git(["rev-parse", "HEAD:%s" % args.name], workdir, env,
                    allow_fail=True).stdout.strip()
    show = git(["show", "HEAD:%s" % SUMS_NAME], workdir, env, allow_fail=True)
    sums_same = show.returncode == 0 and (show.stdout or "") == sums
    same = bool(local_blob) and local_blob == head_blob and sums_same

    if same and not args.force:
        log("APK 与 %s 都与仓库 HEAD 一致（blob %s），跳过提交/推送；要强制重写加 --force"
            % (SUMS_NAME, local_blob))
        if args.check_url:
            st, detail = probe_url(raw, min(args.timeout, 60.0))
            log("[probe] raw   : %s" % describe(st, detail, "raw"))
            st, detail = probe_url(pages, min(args.timeout, 60.0))
            log("[probe] pages : %s" % describe(st, detail, "pages"))
        return 0

    dest = workdir / args.name
    if dest.exists() and dest.resolve() == apk.resolve():
        pass
    else:
        dest.unlink(missing_ok=True)
        try:
            os.link(apk, dest)
        except OSError:
            shutil.copy2(apk, dest)
    if sha256_file(dest) != digest:
        die("复制后 SHA256 不一致：%s" % dest, code=1)
    (workdir / SUMS_NAME).write_text(sums, encoding="utf-8")

    if git(["config", "user.email"], workdir, env, allow_fail=True).returncode != 0:
        git(["config", "user.email", "qgbcs1@gmail.com"], workdir, env, allow_fail=True)
    if git(["config", "user.name"], workdir, env, allow_fail=True).returncode != 0:
        git(["config", "user.name", "QGB"], workdir, env, allow_fail=True)

    git(["add", args.name, SUMS_NAME], workdir, env)
    count = head_count(workdir, env)
    rhead = remote_head(workdir, env, branch)

    if count == 1:
        git(["commit", "--amend", "-m", args.message], workdir, env)
    elif count == 0:
        git(["commit", "-m", args.message], workdir, env)
    else:
        if not args.reset_history:
            die("本地历史有 %d 个提交，amend 只能覆盖最后一个；确认要重写为单提交就加 --reset-history"
                % count, code=3)
        log("历史有 %d 个提交，重写为单提交…" % count)
        git(["checkout", "--orphan", "__single__"], workdir, env)
        git(["commit", "-m", args.message], workdir, env)
        git(["branch", "-M", branch], workdir, env)

    head = git(["rev-parse", "HEAD"], workdir, env).stdout.strip()
    log("commit   : %s" % head)
    log("推送中   : git push --progress（%s → %s，%s）"
        % (branch, repo, human_size(size)))
    push_args = ["push", "--progress"]
    if rhead:
        push_args += ["--force-with-lease", "origin", branch]
    else:
        push_args += ["-u", "origin", branch]
    rc = git_stream(push_args, workdir, env, timeout=args.timeout)
    if rc != 0:
        die("git push 失败（rc=%s）：认证失败/仓库拒绝/网络中断，看上面输出" % rc, code=1)

    new_remote = remote_head(workdir, env, branch)
    if new_remote != head:
        die("推送后远端 HEAD=%s 与本地 %s 不一致" % (new_remote, head), code=1)

    log("")
    log("推送完成：%s (%s)" % (repo, branch))
    log("  Pages 直链 : %s" % pages)
    log("  raw 直链   : %s（>10MB 匿名 403）" % raw)
    log("  SHA256     : %s" % digest)
    log("  SUMS       : %s" % sums_pages)
    log("  SUMS raw   : %s" % sums_raw)
    log("  提醒：免费版 Gitee Pages 需在网页端“服务 -> Gitee Pages -> 更新”后才会部署新文件")
    if args.check_url:
        st, detail = probe_url(raw, min(args.timeout, 60.0))
        log("  [probe] raw   : %s" % describe(st, detail, "raw"))
        st, detail = probe_url(pages, min(args.timeout, 60.0))
        log("  [probe] pages : %s" % describe(st, detail, "pages"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
