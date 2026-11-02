#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_git.py - 把 xime 的 APK 以固定名 ime.apk 覆盖式推送到 Git 仓库。

与 publish_apk_npm.py 的区别:
- 目标是 GitHub 仓库（默认 git@ssh.github.com:QGB/suishou），不是 npm。
- 只负责 GitHub；Gitee（含 Gitee Pages）已完全拆到独立文件
  publish_apk_gitee.py，且默认不启用，本脚本不再接受 --gitee。
- 包内文件名固定 ime.apk，且只保留一份历史：
  首次发布 -> 普通 commit + push；
  后续发布 -> `git commit --amend` + `git push --force-with-lease`，
  这样 63MB 的 blob 不会每构建一次就在历史里再存一份。
- 同时发布 SHA256SUMS.txt（sha256sum -c 兼容格式），下载大文件前可先取它校验。
- 结束后打印下载直链：GitHub 源站直链 + ghfast.top 加速链。

前置条件:
- 本机必须有 SSH 私钥（默认 ~/.ssh/NIST256p.pem，缺失直接报错退出）。
- 已安装 git，且该 key 对目标仓库有写权限。

典型用法:

    # 演练：只校验 key / 仓库 / APK，打印将要推送的链接，不动仓库
    python3 publish_apk_git.py --dry-run

    # 默认：取 out/ 下最新 APK，推到 QGB/suishou 的 main，覆盖式更新
    python3 publish_apk_git.py

    # 指定 APK / 分支 / 工作副本 / key
    python3 publish_apk_git.py --apk out/com.qgb.xime-xxxx.apk --branch main \
        --dir /root/suishou --key ~/.ssh/NIST256p.pem

    # 仓库已经有多次提交时，重写为“单提交”历史（仍用 force-with-lease）
    python3 publish_apk_git.py --reset-history

    # 结束后顺带 HEAD 一下 ghfast 链接
    python3 publish_apk_git.py --check-url
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "git@ssh.github.com:QGB/suishou"
DEFAULT_BRANCH = "main"
DEFAULT_WORKDIR = Path("/root/suishou")
DEFAULT_KEY = Path("~/.ssh/NIST256p.pem")
DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
DEFAULT_PUBLISH_NAME = "ime.apk"
DEFAULT_MESSAGE = "ime.apk 覆盖更新（xime arm64-v8a）"
SUMS_NAME = "SHA256SUMS.txt"
# GitHub 单文件硬上限 100MiB；超过直接拦下
MAX_FILE_MIB = 100.0

_PRINT_LOCK = threading.Lock()


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 2) -> "None":
    print("ERROR: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def human_size(n: float) -> str:
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
        die("找不到 SSH 私钥：%s（没有 %s 就退出，请先放好密钥或用 --key 指定）"
            % (p, p.name), code=4)
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
    """流式 git（push 用）：输出边产生边打印，沉默时打心跳，超时自动终止。

    与 git() 的区别是不再 capture_output —— 后者会把 git 的进度条憋到结束才一次性
    显示，60MB 推送期间看起来就是“卡死”。这里 --progress 的 `… | x MiB/s` 实时可见。
    """
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
                log("  推送中：已 %.0fs 无新输出（pid=%s 仍在跑，累计 %.0fs；"
                    "git 每传 1MiB 才刷新一次进度）" % (idle, proc.pid, elapsed))

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
                   env: "dict[str, str]", dry_run: bool) -> Path:
    if (target / ".git").is_dir():
        cur = git(["remote", "get-url", "origin"], target, env).stdout.strip()
        if cur.rstrip("/") != repo.rstrip("/"):
            die("工作目录 %s 的 origin 是 %s，与 --repo %s 不一致；"
                "换个 --dir 或先手动清理" % (target, cur, repo))
        if dry_run:
            return target
        git(["fetch", "origin", "--prune"], target, env)
        has_branch = git(["rev-parse", "--verify", "origin/%s" % branch], target, env,
                         allow_fail=True)
        if has_branch.returncode == 0:
            git(["checkout", branch], target, env, allow_fail=True)
        else:
            git(["checkout", "-B", branch], target, env, allow_fail=True)
        return target

    if dry_run:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and any(target.iterdir()):
        die("目录 %s 已存在且不是 git 仓库，请清空或换 --dir" % target)
    log("$ git clone %s %s" % (repo, target))
    git(["clone", repo, str(target)], target.parent, env)
    has_branch = git(["rev-parse", "--verify", "origin/%s" % branch], target, env,
                     allow_fail=True)
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


def build_links(repo: str, branch: str, name: str) -> "tuple[str, str, str]":
    # 返回 (主直链, 加速链, 源站链)，只拼 GitHub 链接
    owner, rname = owner_repo(repo)
    raw = "https://github.com/%s/%s/raw/%s/%s" % (owner, rname, branch, name)
    fast = "https://ghfast.top/" + raw
    direct = "https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, rname, branch, name)
    return raw, fast, direct


def check_url(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "xime-publish-git/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            size = resp.headers.get("Content-Length")
            return "HTTP %s, %s" % (resp.status, human_size(int(size)) if size else "unknown")
    except urllib.error.HTTPError as exc:
        return "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - 链接探测失败不影响发布结果
        return "探测失败：%s" % exc


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="把 APK 以固定名 ime.apk 覆盖式推送到 Git 仓库（amend + force-with-lease）。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apk", "--file", dest="apk", default=None,
                    help="要发布的 APK（默认 out/ 下最新的 %s）" % APK_PATTERN)
    ap.add_argument("--name", default=DEFAULT_PUBLISH_NAME,
                    help="仓库内的文件名（默认 %s）" % DEFAULT_PUBLISH_NAME)
    ap.add_argument("--repo", default=None,
                    help="GitHub 仓库地址（默认 %s）" % DEFAULT_REPO)
    ap.add_argument("--branch", default=DEFAULT_BRANCH, help="分支（默认 %s）" % DEFAULT_BRANCH)
    ap.add_argument("--dir", default=None,
                    help="本地工作副本（默认 %s，不存在时 clone）" % DEFAULT_WORKDIR)
    ap.add_argument("--key", default=str(DEFAULT_KEY),
                    help="SSH 私钥（默认 %s，不存在直接退出）" % DEFAULT_KEY)
    ap.add_argument("--message", default=None, help="提交信息；默认带构建信息与 SHA256")
    ap.add_argument("--dry-run", action="store_true", help="只校验 key/仓库/APK 并打印链接，不提交不推送")
    ap.add_argument("--force", action="store_true",
                    help="仓库里 ime.apk 与本地 APK 内容相同时也照常重写提交（默认跳过）")
    ap.add_argument("--reset-history", action="store_true",
                    help="历史超过 1 个提交时重写为单提交（仍用 force-with-lease）")
    ap.add_argument("--check-url", action="store_true", help="推送后 HEAD 一次 ghfast 直链")
    ap.add_argument("--timeout", type=float, default=1200.0, help="git 操作超时秒数")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    # 1) key 必须存在，否则直接退出
    key = check_key(Path(args.key))
    log("SSH key  : %s" % key)

    # 2) APK
    apk = Path(args.apk).expanduser() if args.apk else find_latest_apk(DEFAULT_OUT_DIR)
    if apk is None or not apk.is_file():
        die("找不到 APK：%s（用 --apk 指定）" % (apk or DEFAULT_OUT_DIR))
    size = apk.stat().st_size
    if size == 0:
        die("APK 为空：%s" % apk)
    if size > MAX_FILE_MIB * 1024 * 1024:
        die("APK %s 超过 GitHub 单文件 %.0fMiB 上限" % (human_size(size), MAX_FILE_MIB))
    digest = sha256_file(apk)
    sums = sums_text(args.name, digest)
    log("APK      : %s (%s)" % (apk, human_size(size)))
    log("SHA256   : %s" % digest)

    env = ssh_env(key)
    repo = (args.repo or DEFAULT_REPO).rstrip("/")
    workdir = Path(args.dir or DEFAULT_WORKDIR).expanduser()
    branch = args.branch
    raw, fast, direct = build_links(repo, branch, args.name)
    sums_raw, sums_fast, _ = build_links(repo, branch, SUMS_NAME)
    msg = args.message or "%s（构建 %s，%s，sha256 %s）" % (
        DEFAULT_MESSAGE, apk.name, human_size(size), digest)

    workdir = ensure_workdir(workdir, repo, branch, env, args.dry_run)
    log("工作副本 : %s" % workdir)

    # 比对仓库里已有的同名文件与本地 APK（git blob 相同即内容相同）
    local_blob = ""
    head_blob = ""
    sums_same = False
    if workdir.is_dir():
        local_blob = git(["hash-object", str(apk)], workdir, env,
                         allow_fail=True).stdout.strip()
        head_blob = git(["rev-parse", "HEAD:%s" % args.name], workdir, env,
                        allow_fail=True).stdout.strip()
        show = git(["show", "HEAD:%s" % SUMS_NAME], workdir, env, allow_fail=True)
        sums_same = show.returncode == 0 and (show.stdout or "") == sums
    same = bool(local_blob) and local_blob == head_blob and sums_same

    if args.dry_run:
        rhead = remote_head(workdir, env, branch) if workdir.is_dir() else None
        log("远端分支 : %s" % ((rhead or "(空仓库/无该分支)")))
        log("APK/SUMS : %s" % ("否（重复运行会跳过）" if same and not args.force
                              else "是/无法判断（会提交并推送）"))
        if same and not args.force:
            log("\n[dry-run] APK 与 %s 都和仓库一致，实际运行会直接跳过（加 --force 才会重写）"
                % SUMS_NAME)
        else:
            log("\n[dry-run] 将执行：写入 %s + %s → %s → %s"
                % (args.name, SUMS_NAME,
                   "git commit --amend" if rhead and head_count(workdir, env) else "git commit",
                   "git push --force-with-lease" if rhead else "git push -u origin %s" % branch))
        log("APK 直链  : %s" % raw)
        if fast != raw:
            log("APK 加速  : %s" % fast)
        log("SUMS 直链 : %s" % sums_raw)
        if sums_fast != sums_raw:
            log("SUMS 加速 : %s" % sums_fast)
        return 0

    if same and not args.force:
        log("APK 与 %s 都与仓库 HEAD 一致（blob %s），跳过 amend/推送；要强制重写加 --force"
            % (SUMS_NAME, local_blob))
        log("  仓库     : %s (%s)" % (repo, branch))
        log("  下载直链 : %s" % raw)
        if fast != raw:
            log("  加速链接 : %s" % fast)
            log("  源站直连 : %s" % direct)
        return 0

    # 3) 放入仓库（跨文件系统时退回复制）
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

    # 同一次提交里放校验文件：下载方先取 SHA256SUMS.txt，再下大文件
    (workdir / SUMS_NAME).write_text(sums, encoding="utf-8")

    git(["config", "user.name"], workdir, env, allow_fail=True)
    if git(["config", "user.email"], workdir, env, allow_fail=True).returncode != 0:
        git(["config", "user.email", "qgbcs1@gmail.com"], workdir, env, allow_fail=True)
    if git(["config", "user.name"], workdir, env, allow_fail=True).returncode != 0:
        git(["config", "user.name", "QGB"], workdir, env, allow_fail=True)

    git(["add", args.name, SUMS_NAME], workdir, env)
    count = head_count(workdir, env)
    rhead = remote_head(workdir, env, branch)

    if count == 1:
        git(["commit", "--amend", "-m", msg], workdir, env)
    elif count == 0:
        git(["commit", "-m", msg], workdir, env)
    else:
        if not args.reset_history:
            die("本地历史有 %d 个提交，amend 只能覆盖最后一个，旧的大文件仍留在历史里；"
                "确认要重写为单提交就加 --reset-history" % count, code=3)
        log("历史有 %d 个提交，重写为单提交…" % count)
        git(["checkout", "--orphan", "__single__"], workdir, env)
        git(["commit", "-m", msg], workdir, env)
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
        die("git push 失败（rc=%s）：远端拒绝或网络中断；看上面最后几行输出" % rc, code=1)

    new_remote = remote_head(workdir, env, branch)
    if new_remote != head:
        die("推送后远端 HEAD=%s 与本地 %s 不一致" % (new_remote, head), code=1)

    log("\n推送完成（覆盖式，仅保留 1 个提交）")
    log("  仓库     : %s (%s)" % (repo, branch))
    log("  下载直链 : %s" % raw)
    if fast != raw:
        log("  加速链接 : %s" % fast)
        log("  源站直连 : %s" % direct)
    log("  SHA256   : %s" % digest)
    log("  SUMS     : %s" % sums_raw)
    if sums_fast != sums_raw:
        log("  SUMS 加速 : %s" % sums_fast)
    if args.check_url:
        log("  链接探测 : %s" % check_url(fast))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
