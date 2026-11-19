#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unpublish_apk.py - 独立撤销 ime.apk 在四个渠道的发布（npm / GitHub / Gitee / pythonanywhere）。

与 publish_apk_*.py 完全解耦：只读同一套凭据（SSH key / npm token / GITEE_TOKEN /
XIME_RPC_BASE），不 import 发布脚本，删完自己复查。

默认只体检（只读，不删任何东西）；加 --purge 才真正删除。

用法:

    # 1) 体检：看四个渠道现在各自有什么
    python3 unpublish_apk.py

    # 2) 真正撤销四个渠道
    python3 unpublish_apk.py --purge

    # 3) 只撤其中几个渠道
    python3 unpublish_apk.py --purge --channels npm,gitee

    # 4) GitHub 连分支一起删（仓库变成空仓库，网页上看不到任何文件/提交）
    python3 unpublish_apk.py --purge --channels github --github-delete-branch

    # 5) npm 只删指定版本 / 整个包
    python3 unpublish_apk.py --purge --channels npm --npm-versions 1.0.7,1.0.8
    python3 unpublish_apk.py --purge --channels npm --npm-all

    # 6) 只撤 pa 上的 ime.apk 和 SHA256SUMS.txt
    python3 unpublish_apk.py --purge --channels rpc --names ime.apk,SHA256SUMS.txt

关于“历史能不能也查不到”（脚本结束时也会打印）:

- GitHub: 重写成空提交并 force push 后，旧 commit 在网页/API 上变成 unreachable，
  正常途径找不到；但 GitHub 服务端仍会保留一段时间才 GC，别人已经 clone/fork 的副本、
  ghfast/Fastly/raw.githubusercontent 的边缘缓存都删不掉。要更彻底只能删整个仓库。
- npm: 发布 72 小时内可以 npm unpublish；超过 72 小时基本只能找 npm 官方支持。
  unpkg/jsDelivr 对已缓存版本的 tarball 可能还能下几小时到几天（jsDelivr 固定版本 URL
  甚至可能长期命中缓存）。
- Gitee: 删附件/发行版后直链 404，但服务端访问日志、审核/操作记录仍在。
- pythonanywhere: 文件真正从磁盘删除（os.remove），但账号里的访问日志仍在。

结论：本脚本能撤销“现在还能下载到的发布”，无法保证“历史上绝对查不到”。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 默认值
# 与 publish_apk_*.py 保持同一套默认值，保证“撤的就是刚发的那个东西”。

GH_DEFAULT_REPO = "git@ssh.github.com:QGB/suishou"
GH_DEFAULT_BRANCH = "main"
GH_DEFAULT_WORKDIR = Path("/root/suishou")
DEFAULT_KEY = Path("~/.ssh/NIST256p.pem")
DEFAULT_NAMES = "ime.apk"
SUMS_NAME = "SHA256SUMS.txt"

NPM_DEFAULT_PACKAGE = "qgb"
NPM_DEFAULT_REGISTRY = "https://registry.npmjs.org"

GITEE_API = "https://gitee.com/api/v5"
GITEE_DEFAULT_REPO = "git@gitee.com:qgbcs/suishou.git"
GITEE_DEFAULT_TAG = "apk"
GITEE_TOKEN = os.environ.get("GITEE_TOKEN", "").strip()
RPC_BASE = os.environ.get("XIME_RPC_BASE", "").strip()
UA = "xime-unpublish-apk/1.0"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 2) -> "None":
    print("ERROR: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def split_csv(value: str) -> "list[str]":
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# ================================================================ npm


def npmrc_path() -> Path:
    env = os.environ.get("NPM_CONFIG_USERCONFIG") or ""
    return Path(env).expanduser() if env.strip() else Path.home() / ".npmrc"


def resolve_npm_token(explicit: "str | None") -> "tuple[str | None, str]":
    if explicit and explicit.strip():
        return explicit.strip(), "--npm-token"
    for key in ("NPM_TOKEN", "NODE_AUTH_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip(), key
    p = npmrc_path()
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*.*_authToken\s*=\s*(\S+)", line)
            if m:
                return m.group(1).strip(), str(p)
    return None, ""


def fetch_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def npm_versions(registry: str, pkg: str, timeout: float) -> "list[str]":
    url = registry.rstrip("/") + "/" + urllib.parse.quote(pkg, safe="@")
    try:
        doc = fetch_json(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return sorted((doc.get("versions") or {}).keys())


def write_temp_npmrc(token: str, registry: str, workdir: Path) -> Path:
    host = urllib.parse.urlparse(registry).netloc or "registry.npmjs.org"
    p = workdir / ".npmrc.unpublish"
    p.write_text("registry=%s/\n//%s/:_authToken=%s\n"
                 % (registry.rstrip("/"), host, token), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def channel_npm(args, purge: bool) -> bool:
    log("\n==== npm：%s @ %s" % (args.npm_package, args.npm_registry))
    registry, pkg = args.npm_registry, args.npm_package
    try:
        versions = npm_versions(registry, pkg, args.timeout)
    except Exception as exc:  # noqa: BLE001
        log("  [FAIL] 查询 registry 失败：%s: %s" % (type(exc).__name__, exc))
        return False
    if not versions:
        log("  已经是空的（registry 里没有 %s）" % pkg)
        return True
    log("  现有版本：%s" % ", ".join(versions))

    targets = split_csv(args.npm_versions) if args.npm_versions else versions
    missing = [v for v in targets if v not in versions]
    if missing:
        log("  提示：这些版本本来就不存在，忽略：%s" % ", ".join(missing))
    targets = [v for v in targets if v in versions]
    if not targets:
        log("  没有需要删除的版本")
        return True

    if not purge:
        log("  [dry-run] 将执行 npm unpublish：%s"
            % ", ".join("%s@%s" % (pkg, v) for v in targets))
        return True

    token, source = resolve_npm_token(args.npm_token)
    if not token:
        log("  [FAIL] 缺少 npm token（--npm-token / NPM_TOKEN / ~/.npmrc）")
        return False
    log("  token 来源：%s" % source)
    npm = shutil.which("npm")
    if not npm:
        log("  [FAIL] 找不到 npm 可执行文件")
        return False

    workdir = Path(tempfile.mkdtemp(prefix=".npm-unpublish-"))
    ok = True
    try:
        npmrc = write_temp_npmrc(token, registry, workdir)
        cmds = []
        if args.npm_all:
            log("  按 --npm-all：直接撤销整个包 %s" % pkg)
            cmds.append(["unpublish", pkg, "--force"])
        else:
            for v in targets:
                cmds.append(["unpublish", "%s@%s" % (pkg, v), "--force"])
        for base in cmds:
            cmd = [npm] + base + ["--registry", registry.rstrip("/"),
                                  "--userconfig", str(npmrc)]
            if args.npm_otp:
                cmd += ["--otp", args.npm_otp]
            log("  $ npm " + " ".join(base))
            proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True,
                                  text=True, timeout=args.timeout,
                                  env=dict(os.environ, npm_config_userconfig=str(npmrc)))
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if out:
                for line in out.splitlines()[-12:]:
                    log("    " + line)
            if proc.returncode != 0:
                ok = False
                low = out.lower()
                if "72" in low and ("unpublish" in low or "policy" in low):
                    log("    [提示] npm 只允许发布后 72 小时内 unpublish，超过需联系 npm 支持")
                elif "e403" in low or "403" in out:
                    log("    [提示] 403：token 没有该包写权限，或该版本超过 72 小时")
    except subprocess.TimeoutExpired:
        log("  [FAIL] npm unpublish 超时（%.0fs）" % args.timeout)
        ok = False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        left = npm_versions(registry, pkg, args.timeout)
    except Exception as exc:  # noqa: BLE001
        log("  [WARN] 复查失败：%s" % exc)
        return ok
    if left:
        log("  复查：仍存在版本 %s" % ", ".join(left))
        return False
    log("  复查：registry 里已没有 %s" % pkg)
    return ok


# ================================================================ GitHub


def check_key(key: Path) -> Path:
    p = key.expanduser()
    if not p.is_file() or p.stat().st_size == 0:
        die("找不到可用 SSH 私钥：%s（或用 --key 指定）" % p, code=4)
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


def owner_repo(url: str) -> "tuple[str, str]":
    """git@ssh.github.com:QGB/suishou -> ("QGB", "suishou")。"""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = [p for p in re.split(r"[:/]", u) if p]
    if len(parts) < 2:
        die("无法从仓库地址解析 owner/repo：%s" % url)
    return parts[-2], parts[-1]


def git(cmd: "list[str]", cwd: Path, env: "dict[str, str]", timeout: float,
        allow_fail: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git"] + list(cmd), cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 and not allow_fail:
        die("git %s 失败（rc=%s）\n%s"
            % (" ".join(cmd), proc.returncode,
               ((proc.stdout or "") + (proc.stderr or "")).strip()), code=1)
    return proc


def ensure_workdir(target: Path, repo: str, branch: str,
                   env: "dict[str, str]", timeout: float) -> Path:
    if (target / ".git").is_dir():
        cur = git(["remote", "get-url", "origin"], target, env, timeout).stdout.strip()
        if cur.rstrip("/") != repo.rstrip("/"):
            die("工作目录 %s 的 origin 是 %s，与 --repo %s 不一致；换个 --dir 或先清理"
                % (target, cur, repo))
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and any(target.iterdir()):
        die("目录 %s 已存在且不是 git 仓库，请清空或换 --dir" % target)
    log("  $ git clone %s %s" % (repo, target))
    git(["clone", repo, str(target)], target.parent, env, timeout)
    return target


def remote_head(workdir: Path, env: "dict[str, str]", branch: str,
                timeout: float) -> "str | None":
    proc = git(["ls-remote", "origin", "refs/heads/%s" % branch], workdir, env,
               timeout, allow_fail=True)
    out = (proc.stdout or "").strip()
    return out.split("\t")[0] if out else None


def head_of_file(workdir: Path, env: "dict[str, str]", name: str,
                 timeout: float) -> "str | None":
    proc = git(["rev-parse", "HEAD:%s" % name], workdir, env, timeout, allow_fail=True)
    return (proc.stdout or "").strip() if proc.returncode == 0 else None


def probe(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            size = resp.headers.get("Content-Length")
            return "HTTP %s%s" % (resp.status, "，%sB" % size if size else "")
    except urllib.error.HTTPError as exc:
        return "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - 探测失败不影响撤销结果
        return "探测失败：%s" % exc


def channel_github(args, purge: bool) -> bool:
    repo = args.repo.rstrip("/")
    branch = args.branch
    log("\n==== GitHub：%s (%s)" % (repo, branch))
    workdir = Path(args.dir).expanduser()
    if not purge:
        # 只体检：直接用 ls-remote 看远端，不 clone、不改工作副本
        env = ssh_env(check_key(Path(args.key)))
        try:
            out = subprocess.run(
                ["git", "ls-remote", repo, "refs/heads/%s" % branch],
                capture_output=True, text=True, timeout=args.timeout, env=env)
            rhead = (out.stdout or "").strip().split("\t")[0] if out.stdout.strip() else None
        except Exception as exc:  # noqa: BLE001
            log("  [FAIL] ls-remote 失败：%s: %s" % (type(exc).__name__, exc))
            return False
        owner, rname = owner_repo(repo)
        log("  远端分支 HEAD：%s" % (rhead or "(无该分支)"))
        for name in split_csv(args.names):
            url = "https://raw.githubusercontent.com/%s/%s/%s/%s" % (
                owner, rname, branch, name)
            log("  %s → %s" % (url, probe(url)))
        log("  [dry-run] --purge 时会重写为单提交并 force push"
            "（或 --github-delete-branch 直接删分支）")
        return True

    key = check_key(Path(args.key))
    env = ssh_env(key)
    workdir = ensure_workdir(workdir, repo, branch, env, args.timeout)
    git(["fetch", "origin", "--prune"], workdir, env, args.timeout, allow_fail=True)

    if args.github_delete_branch:
        log("  删除远端分支 %s（仓库会变成空仓库）" % branch)
        proc = git(["push", "origin", "--delete", branch], workdir, env,
                   args.timeout, allow_fail=True)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if out:
            log("  " + out.replace("\n", "\n  "))
        rhead = remote_head(workdir, env, branch, args.timeout)
        log("  复查：远端 %s = %s" % (branch, rhead or "(已删除)"))
        return rhead is None

    # 默认：重写为单提交（空树），旧 commit 变 unreachable
    log("  重写为单个空提交，抹掉 ime.apk / SHA256SUMS.txt")
    git(["checkout", branch], workdir, env, args.timeout, allow_fail=True)
    git(["branch", "-D", "__unpublish__"], workdir, env, args.timeout, allow_fail=True)
    git(["checkout", "--orphan", "__unpublish__"], workdir, env, args.timeout)
    git(["rm", "-rf", "--ignore-unmatch", "."], workdir, env, args.timeout,
        allow_fail=True)
    msg = "撤销发布：移除 %s" % ", ".join(split_csv(args.names))
    git(["commit", "--allow-empty", "-m", msg], workdir, env, args.timeout)
    git(["branch", "-M", branch], workdir, env, args.timeout)

    head = git(["rev-parse", "HEAD"], workdir, env, args.timeout).stdout.strip()
    push_args = ["push", "--progress"]
    if args.github_force:
        push_args += ["--force", "origin", branch]
    else:
        push_args += ["--force-with-lease", "origin", branch]
    log("  $ git " + " ".join(push_args))
    proc = subprocess.run(["git"] + push_args, cwd=str(workdir), env=env,
                          capture_output=True, text=True, timeout=args.timeout)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if out:
        for line in out.splitlines()[-10:]:
            log("  " + line)
    if proc.returncode != 0:
        log("  [FAIL] push 失败；如确认远端没人动过，可加 --github-force")
        return False

    rhead = remote_head(workdir, env, branch, args.timeout)
    log("  复查：本地 HEAD=%s，远端 HEAD=%s" % (head, rhead))
    ok = rhead == head
    for name in split_csv(args.names):
        blob = head_of_file(workdir, env, name, args.timeout)
        log("  复查：%s %s" % (name, "已不在当前提交" if not blob else "仍在：%s" % blob))
        ok = ok and not blob
    return ok


# ================================================================ Gitee


def gitee_api(url: str, token: str, method: str = "GET",
              params: "dict | None" = None, timeout: float = 30.0):
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
        return {} if not body.strip() else {"__raw__": body}


def gitee_slug(repo: str) -> str:
    u = repo.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = [p for p in re.split(r"[:/]", u) if p]
    return "%s/%s" % (parts[-2], parts[-1])


def channel_gitee(args, purge: bool) -> bool:
    repo, tag = args.gitee_repo.rstrip("/"), args.gitee_tag
    slug = gitee_slug(repo)
    link = "https://gitee.com/%s/releases/download/%s/%s" % (
        slug, tag, split_csv(args.names)[0])
    log("\n==== Gitee：%s（发行版 tag=%s）" % (slug, tag))
    token = (args.gitee_token or GITEE_TOKEN).strip()
    if not token:
        log("  [FAIL] 缺少 Gitee 令牌：--gitee-token 或环境变量 GITEE_TOKEN")
        return False

    rel = gitee_api("%s/repos/%s/releases/tags/%s" % (GITEE_API, slug, tag),
                    token, timeout=args.timeout)
    if isinstance(rel, dict) and rel.get("__error__") == 404:
        log("  发行版不存在（已撤销）")
        return True
    if isinstance(rel, dict) and rel.get("__error__"):
        log("  [FAIL] 查询发行版失败 HTTP %s" % rel["__error__"])
        return False

    rid = rel.get("id")
    atts = gitee_api("%s/repos/%s/releases/%s/attach_files" % (GITEE_API, slug, rid),
                     token, timeout=args.timeout)
    atts = atts if isinstance(atts, list) else []
    log("  发行版 id=%s，附件 %d 个：%s" % (
        rid, len(atts), ", ".join("%s(%sB)" % (a.get("name"), a.get("size")) for a in atts) or "(无)"))

    names = set(split_csv(args.names))
    targets = [a for a in atts
               if args.gitee_all_attachments or a.get("name") in names]
    if not targets and not args.gitee_delete_release:
        log("  没有匹配的附件（%s）；如需连发行版一起删，加 --gitee-delete-release"
            % ", ".join(sorted(names)))
        return True

    if not purge:
        log("  [dry-run] 将删除附件：%s%s"
            % (", ".join(a.get("name") or "?" for a in targets) or "(无)",
               "，并删除发行版" if args.gitee_delete_release else ""))
        return True

    ok = True
    for a in targets:
        log("  删除附件 %s（id=%s）" % (a.get("name"), a.get("id")))
        obj = gitee_api("%s/repos/%s/releases/%s/attach_files/%s"
                        % (GITEE_API, slug, rid, a.get("id")),
                        token, method="DELETE", timeout=args.timeout)
        if isinstance(obj, dict) and obj.get("__error__"):
            log("  [FAIL] 删除附件失败 HTTP %s" % obj["__error__"])
            ok = False

    if args.gitee_delete_release:
        log("  删除发行版 id=%s" % rid)
        obj = gitee_api("%s/repos/%s/releases/%s" % (GITEE_API, slug, rid),
                        token, method="DELETE", timeout=args.timeout)
        if isinstance(obj, dict) and obj.get("__error__") not in (None, 404):
            log("  [FAIL] 删除发行版失败 HTTP %s" % obj["__error__"])
            ok = False

    # 复查
    rel2 = gitee_api("%s/repos/%s/releases/tags/%s" % (GITEE_API, slug, tag),
                     token, timeout=args.timeout)
    if isinstance(rel2, dict) and rel2.get("__error__") == 404:
        log("  复查：发行版已不存在")
        return ok
    atts2 = gitee_api("%s/repos/%s/releases/%s/attach_files"
                      % (GITEE_API, slug, rel2.get("id")),
                      token, timeout=args.timeout)
    atts2 = atts2 if isinstance(atts2, list) else []
    left = [a.get("name") for a in atts2]
    log("  复查：剩余附件 %s" % (", ".join(left) or "(无)"))
    target_left = names & set(left)
    if target_left:
        log("  [FAIL] 仍存在：%s" % ", ".join(sorted(target_left)))
        ok = False
    log("  直链探测：%s → %s" % (link, probe(link)))
    return ok


# ================================================================ pythonanywhere RPC


RPC_DELETE_TEMPLATE = """import os, json, traceback
__p = os.path.abspath(os.path.expanduser(@@NAME@@))
__existed = os.path.exists(__p)
try:
    if __existed:
        os.remove(__p)
    r = json.dumps({'path': __p, 'existed': __existed, 'exists': os.path.exists(__p)})
except Exception:
    r = traceback.format_exc()"""

RPC_STAT_TEMPLATE = """import os, json, hashlib, time
__p = os.path.abspath(os.path.expanduser(@@NAME@@))
info = {'path': __p, 'exists': False}
if os.path.exists(__p):
    st = os.stat(__p)
    info.update({'exists': True, 'size': st.st_size,
                 'mtime_iso': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))})
r = json.dumps(info)"""


def rpc_call(base: str, code: str, timeout: float) -> "tuple[int, str]":
    url = base + urllib.parse.quote(code, safe="")
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Content-Type": "application/octet-stream", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def channel_rpc(args, purge: bool) -> bool:
    base = (args.rpc_base or RPC_BASE).strip()
    log("\n==== pythonanywhere RPC：%s" % (base or "(未配置)"))
    if not base:
        log("  [FAIL] 缺少 --rpc-base 或环境变量 XIME_RPC_BASE")
        return False

    ok = True
    for name in split_csv(args.names):
        tpl = RPC_STAT_TEMPLATE if not purge else RPC_DELETE_TEMPLATE
        status, body = rpc_call(base, tpl.replace("@@NAME@@", repr(name)), args.timeout)
        if status != 200:
            log("  [FAIL] %s：HTTP %s %s" % (name, status, body.strip()[:200]))
            ok = False
            continue
        try:
            info = json.loads(body)
        except ValueError:
            log("  [FAIL] %s：返回非 JSON：%s" % (name, body.strip()[:200]))
            ok = False
            continue
        if purge:
            log("  %s：existed=%s → exists=%s（%s）"
                % (name, info.get("existed"), info.get("exists"), info.get("path")))
            ok = ok and info.get("exists") is False
        else:
            if info.get("exists"):
                log("  %s：存在，%sB，mtime %s（--purge 才会删）"
                    % (name, info.get("size"), info.get("mtime_iso")))
            else:
                log("  %s：不存在（已撤销）" % name)
    return ok


# ================================================================ main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="独立撤销 ime.apk 在 npm / GitHub / Gitee / pythonanywhere 的发布。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channels", default="npm,github,gitee,rpc",
                    help="要处理的渠道，逗号分隔（默认 npm,github,gitee,rpc）")
    ap.add_argument("--purge", action="store_true",
                    help="真正删除（不加只做只读体检）")
    ap.add_argument("--names", default=DEFAULT_NAMES,
                    help="要撤销的文件名，逗号分隔（默认 %s）" % DEFAULT_NAMES)
    ap.add_argument("--timeout", type=float, default=600.0, help="单步超时秒数")

    g = ap.add_argument_group("npm")
    g.add_argument("--npm-package", default=NPM_DEFAULT_PACKAGE, help="包名（默认 qgb）")
    g.add_argument("--npm-registry", default=NPM_DEFAULT_REGISTRY)
    g.add_argument("--npm-token", default=None, help="token；默认读 NPM_TOKEN / ~/.npmrc")
    g.add_argument("--npm-otp", default=None, help="2FA 一次性验证码")
    g.add_argument("--npm-versions", default="",
                   help="只撤这些版本（逗号分隔，默认全部）")
    g.add_argument("--npm-all", action="store_true",
                   help="连包名一起撤销（npm unpublish <pkg> --force）")

    g = ap.add_argument_group("GitHub")
    g.add_argument("--repo", default=GH_DEFAULT_REPO, help="GitHub 仓库（默认 %s）" % GH_DEFAULT_REPO)
    g.add_argument("--branch", default=GH_DEFAULT_BRANCH, help="分支（默认 main）")
    g.add_argument("--dir", default=str(GH_DEFAULT_WORKDIR), help="本地工作副本")
    g.add_argument("--key", default=str(DEFAULT_KEY), help="SSH 私钥")
    g.add_argument("--github-delete-branch", action="store_true",
                   help="直接删除远端分支（仓库变空仓库，最彻底）")
    g.add_argument("--github-force", action="store_true",
                   help="push 用 --force 而不是 --force-with-lease")

    g = ap.add_argument_group("Gitee")
    g.add_argument("--gitee-repo", default=GITEE_DEFAULT_REPO)
    g.add_argument("--gitee-tag", default=GITEE_DEFAULT_TAG, help="发行版 tag（默认 apk）")
    g.add_argument("--gitee-token", default=None, help="Gitee 令牌；默认读 GITEE_TOKEN")
    g.add_argument("--gitee-delete-release", action="store_true",
                   help="删完附件后再删整个发行版")
    g.add_argument("--gitee-all-attachments", action="store_true",
                   help="删除该发行版所有附件（不限 --names）")

    g = ap.add_argument_group("pythonanywhere")
    g.add_argument("--rpc-base", default=None, help="RPC 入口 base；默认读 XIME_RPC_BASE")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    channels = split_csv(args.channels)
    unknown = [c for c in channels if c not in ("npm", "github", "gitee", "rpc")]
    if unknown:
        die("未知渠道：%s（可选 npm,github,gitee,rpc）" % ", ".join(unknown))

    log("撤销模式：%s" % ("PURGE（真删）" if args.purge else "dry-run（只体检，不删）"))
    log("渠道     ：%s" % ", ".join(channels))
    log("文件名   ：%s" % ", ".join(split_csv(args.names)))
    if not args.purge:
        log("提示     ：确认无误后加 --purge 才真正删除")

    results = {}
    if "npm" in channels:
        results["npm"] = channel_npm(args, args.purge)
    if "github" in channels:
        results["github"] = channel_github(args, args.purge)
    if "gitee" in channels:
        results["gitee"] = channel_gitee(args, args.purge)
    if "rpc" in channels:
        results["rpc"] = channel_rpc(args, args.purge)

    log("\n==== 汇总")
    for name, ok in results.items():
        log("  %-6s %s" % (name, "OK" if ok else "FAIL"))
    if args.purge:
        log("""
==== 关于“历史能不能也查不到”
  GitHub : 旧 commit 已 unreachable，网页/API 找不到；服务端 GC 前仍留有对象，
           别人已 clone/fork 的副本与 ghfast/Fastly/raw CDN 缓存无法删除。
  npm    : 72 小时内可 unpublish；超时需联系 npm 支持；unpkg/jsDelivr 缓存可能仍在。
  Gitee  : 附件/发行版链接已 404，服务端访问日志与操作记录仍保留。
  pa     : 文件已从磁盘 os.remove，账号访问日志仍保留。
  结论   : “可下载的发布”已撤销；“绝对无痕”做不到。""")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
