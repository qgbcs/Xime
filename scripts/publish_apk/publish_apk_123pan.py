#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_123pan.py - 把 xime 的 APK 上传到 123 云盘根目录（固定名 ime.apk）。

与 publish_apk_git.py / publish_apk_npm.py 的区别:
- 目标是 123 云盘（网页版接口 api.123278.com），不是 git/npm。
- 包内文件名固定 ime.apk，放在根目录（parentFileId=0）。
- 同名旧文件先删后传，保证根目录只有一份 ime.apk。
- 内容未变化（根目录已有同名且 md5 相同）时自动跳过上传，只取直链。
- 上传走官方网页版流程：upload_request -> s3 分片签名 -> PUT -> upload_complete。
- 结束时打印两种链接：123 云盘中转页链接 + 解出来的 CDN 下载直链。

鉴权（网页版 token，不是开放平台 clientId）:
- 默认从 ~/.123pan_auth.json 读取 {"token": "...", "loginUuid": "..."}；
- 也可用环境变量 PAN123_TOKEN / PAN123_LOGIN_UUID，或 --token / --login-uuid。
- token 过期(401)时从浏览器 https://yun.123pan.cn 的 localStorage.authorToken 重新取。

前置条件:
- python3.8+；能访问 api.123278.com 与 *.123pan.com。

典型用法:

    # 默认：取 out/ 下最新 APK，上传为根目录 ime.apk，打印直链
    python3 publish_apk_123pan.py

    # 演练：只算 md5/列目录/校验 token，不上传
    python3 publish_apk_123pan.py --dry-run

    # 内容没变也强制重传（重传前仍会先删旧文件）
    python3 publish_apk_123pan.py --force

    # 指定 APK / 目标文件名 / 鉴权文件
    python3 publish_apk_123pan.py --apk out/xxx.apk --name ime.apk \\
        --auth-file ~/.123pan_auth.json

{"token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3OTM0NDQ5NTgsImlkIjo0MDA3MTU4MzM0LCJ2IjowLCJtYWlsIjoiIiwidXNlcm5hbWUiOiIxNzM3MzIwMDA4MyIsIm5pY2tuYW1lIjoiMTczNzMyMDAwODMiLCJzdXBwZXIiOmZhbHNlLCJpc1RvdXJpc3QiOmZhbHNlLCJpc09hdXRoMkxvZ2luIjpmYWxzZSwiYXBwTmFtZSI6IiIsImxvZ2luX3R5cGUiOjEsInBsYXRmb3JtIjoyLCJkZXZpY2VJZCI6IkVkZ2UoOilwRzdubnJyeWlIVENGS05UT3dBMW8oOilXaW5kb3dzIDEwIiwiY2xpZW50SVAiOiIxMTkuMzkuNTMuMTg3IiwiZGV2aWNlTmFtZSI6IiIsImRldmljZU1vZGVsIjoiIiwiZGV2aWNlVHlwZSI6IiIsInJlbWVtYmVyIjoxLCJleHAiOjE4MDEyMjA5NTh9.mLGJpw52vhic58uFs6xnOHwXQ8QqWAKXWBM5URX8OVs","loginUuid":"788b23774aaadc05f0c9cd1fd3b5db7a7aabd1d3bb026f2f932ba58be389b5e1"}

"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API_BASE = "https://api.123278.com/b/api/"
YUNPAN_HOME = "https://yun.123pan.cn/"
DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
DEFAULT_PUBLISH_NAME = "ime.apk"
DEFAULT_AUTH_FILE = Path("~/.123pan_auth.json")
DEFAULT_SLICE = 16 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")

_PRINT_LOCK = None


def log(msg: str) -> None:
    print(msg, flush=True)


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%dB" % n if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGiB" % n


def speed(bps: float) -> str:
    return human_size(max(0.0, bps)) + "/s"


def md5_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


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


def load_auth(auth_file: Path, token: str, uuid: str) -> "tuple[str, str]":
    token = token or os.environ.get("PAN123_TOKEN", "")
    uuid = uuid or os.environ.get("PAN123_LOGIN_UUID", "")
    if token and uuid:
        return token, uuid
    path = Path(os.path.expanduser(str(auth_file)))
    if not path.is_file():
        sys.exit("[auth] 缺少鉴权文件 %s（或设置 PAN123_TOKEN / PAN123_LOGIN_UUID）" % path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        sys.exit("[auth] 鉴权文件解析失败: %s" % exc)
    token = token or str(data.get("token", ""))
    uuid = uuid or str(data.get("loginUuid", "") or data.get("login_uuid", ""))
    if not token or not uuid:
        sys.exit("[auth] 鉴权文件缺少 token / loginUuid")
    return token, uuid


def headers(token: str, uuid: str, with_json: bool = True) -> "dict[str, str]":
    h = {
        "Authorization": "Bearer " + token,
        "LoginUuid": uuid,
        "App-Version": "3",
        "platform": "web",
        "Accept": "application/json, text/plain, */*",
        "Referer": YUNPAN_HOME,
        "User-Agent": UA,
    }
    if with_json:
        h["Content-Type"] = "application/json;charset=UTF-8"
    return h


def api(path: str, payload, token: str, uuid: str, timeout: float,
        method: str = "POST") -> dict:
    url = API_BASE + path
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers(token, uuid))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("%s -> HTTP %s %s" % (path, exc.code, detail)) from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("%s -> %s: %s" % (path, type(exc).__name__, exc)) from None
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        raise RuntimeError("%s -> 非 JSON 响应: %r" % (path, raw[:200])) from None
    if obj.get("code") != 0:
        raise RuntimeError("%s -> code=%s %s" %
                           (path, obj.get("code"), obj.get("message")))
    return obj.get("data") or {}


def list_root(token: str, uuid: str, timeout: float) -> "list[dict]":
    q = ("driveId=0&limit=100&next=0&orderBy=update_time&orderDirection=desc"
         "&parentFileId=0&trashed=false&SearchData=&Page=1"
         "&OnlyLookAbnormalFile=0&event=homeListFile&operateType=1"
         "&inDirectSpace=false&fileCategory=0&isSearchOrder=false&fileTypes%5B%5D=0")
    req = urllib.request.Request(API_BASE + "file/list/new?" + q,
                                 headers=headers(token, uuid, with_json=False))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("file/list/new -> %s: %s" % (type(exc).__name__, exc)) from None
    if obj.get("code") != 0:
        raise RuntimeError("file/list/new -> code=%s %s" %
                           (obj.get("code"), obj.get("message")))
    return list((obj.get("data") or {}).get("InfoList") or [])


def delete_files(items: "list[dict]", token: str, uuid: str,
                 timeout: float) -> None:
    body = {"driveId": 0, "fileIdList": [
        {"fileId": it["FileId"], "fileName": it["FileName"], "type": it.get("Type", 0)}
        for it in items]}
    try:
        api("file/delete", body, token, uuid, timeout)
    except RuntimeError as exc:
        # 7301 = 已删除，空间释放中；按成功处理
        if "code=7301" in str(exc):
            return
        raise


def presign(base_body: dict, token: str, uuid: str, timeout: float,
            start: int, end: int,
            endpoint: str = "file/s3_repare_upload_parts_batch") -> "dict[int, str]":
    payload = dict(base_body)
    payload["partNumberStart"] = start
    # 服务端要求区间长度 >= 2，传 start=end 会直接返回 code=-1 非法请求
    payload["partNumberEnd"] = max(end, start + 1)
    data = api(endpoint, payload, token, uuid, timeout)
    urls = data.get("presignedUrls") or {}
    out = {}
    for k, v in urls.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


def put_part(url: str, blob: bytes, timeout: float) -> str:
    req = urllib.request.Request(url, data=blob, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(resp.headers.get("ETag", "")).strip('"')
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("PUT part -> HTTP %s %s" % (exc.code, detail)) from None


def upload_parts(path: Path, parts: int, slice_size: int, urls: "dict[int, str]",
                 timeout: float, workers: int) -> None:
    total = path.stat().st_size
    done = [0]
    started = time.monotonic()

    def one(n: int) -> None:
        off = (n - 1) * slice_size
        with open(path, "rb") as f:
            f.seek(off)
            blob = f.read(slice_size)
        url = urls.get(n)
        if not url:
            raise RuntimeError("缺少第 %d 片签名 URL" % n)
        put_part(url, blob, timeout)
        done[0] += len(blob)
        elapsed = max(1e-9, time.monotonic() - started)
        log("[upload] %d/%d 片  %s/%s  %.1f%%  即时 %s" %
            (n, parts, human_size(min(done[0], total)), human_size(total),
             100.0 * min(done[0], total) / max(1, total), speed(done[0] / elapsed)))

    if workers <= 1:
        for n in range(1, parts + 1):
            one(n)
        return
    with ThreadPoolExecutor(max_workers=min(workers, parts)) as pool:
        futs = {pool.submit(one, n): n for n in range(1, parts + 1)}
        try:
            for fut in as_completed(futs):
                fut.result()
        except Exception:
            for f in futs:
                f.cancel()
            raise


def resolve_direct_link(wrapper: str, timeout: float) -> str:
    """把 download_info 返回的中转链接解成最终 CDN 直链。

    中转形如 https://web-pro2.cjjd18.com/download-v2/?params=<base64(inner)>&is_s3=0
    inner 返回 {"code":0,"data":{"redirect_url": "https://.../c-m8010?..."}}。
    """
    if "params=" not in wrapper:
        return wrapper
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(wrapper).query).get("params")
    if not q:
        return wrapper
    try:
        inner = base64.b64decode(q[0] + "=" * (-len(q[0]) % 4)).decode("utf-8")
    except Exception:  # noqa: BLE001
        return wrapper
    req = urllib.request.Request(inner, headers={"User-Agent": UA,
                                                 "Referer": YUNPAN_HOME})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("解析中转链接失败: %s: %s" % (type(exc).__name__, exc)) from None
    if obj.get("code") != 0:
        raise RuntimeError("解析中转链接失败: code=%s %s" %
                           (obj.get("code"), obj.get("message")))
    return str((obj.get("data") or {}).get("redirect_url") or "") or wrapper


def download_info(file_info: dict, size: int, token: str, uuid: str,
                  timeout: float) -> str:
    body = {"driveId": 0, "etag": file_info.get("Etag", ""),
            "fileId": file_info.get("FileId"),
            "s3keyFlag": file_info.get("S3KeyFlag", "") or "",
            "type": file_info.get("Type", 0),
            "fileName": file_info.get("FileName", ""),
            "size": size}
    data = api("file/download_info", body, token, uuid, timeout)
    link = str(data.get("DownloadUrl") or "")
    if not link:
        raise RuntimeError("download_info 未返回 DownloadUrl")
    return link


def main() -> int:
    ap = argparse.ArgumentParser(description="上传 APK 到 123 云盘并输出直链")
    ap.add_argument("--apk", "--file", dest="apk", default=None,
                    help="APK 路径；默认取 out/ 下最新的 %s" % APK_PATTERN)
    ap.add_argument("--name", default=DEFAULT_PUBLISH_NAME,
                    help="云盘内文件名（默认 %s）" % DEFAULT_PUBLISH_NAME)
    ap.add_argument("--auth-file", default=str(DEFAULT_AUTH_FILE),
                    help="鉴权 JSON（默认 %s）" % DEFAULT_AUTH_FILE)
    ap.add_argument("--token", default="", help="覆盖 authorToken")
    ap.add_argument("--login-uuid", default="", help="覆盖 LoginUuid")
    ap.add_argument("--dry-run", action="store_true",
                    help="只校验 token / APK / 目录，不上传")
    ap.add_argument("--force", action="store_true",
                    help="内容未变也强制重传")
    ap.add_argument("--keep-old", action="store_true",
                    help="同名旧文件不删除（默认先删后传）")
    ap.add_argument("--workers", type=int, default=3, help="并发分片数（默认 3）")
    ap.add_argument("--timeout", type=float, default=1800.0,
                    help="单次网络操作超时秒数（默认 1800）")
    args = ap.parse_args()

    token, uuid = load_auth(Path(args.auth_file), args.token, args.login_uuid)
    log("[auth] token=%s... uuid=%s..." % (token[:16], uuid[:12]))

    apk = Path(args.apk) if args.apk else find_latest_apk(DEFAULT_OUT_DIR)
    if not apk or not apk.is_file():
        log("[apk] 未找到 APK：%s" % (args.apk or (str(DEFAULT_OUT_DIR) + "/" + APK_PATTERN)))
        return 2
    size = apk.stat().st_size
    log("[apk] %s  %s" % (apk, human_size(size)))
    md5 = md5_file(apk)
    sha256 = sha256_file(apk)
    log("[hash] md5=%s" % md5)
    log("[hash] sha256=%s" % sha256)

    try:
        root = list_root(token, uuid, args.timeout)
    except RuntimeError as exc:
        log("[api] 目录读取失败：%s" % exc)
        return 3
    existing = [it for it in root if it.get("FileName") == args.name]
    if existing:
        log("[api] 根目录已有 %s：fileId=%s size=%s etag=%s" %
            (args.name, existing[0].get("FileId"), existing[0].get("Size"),
             existing[0].get("Etag")))
    else:
        log("[api] 根目录无同名文件，将新建")

    same = bool(existing) and str(existing[0].get("Etag") or "") == md5 \
        and int(existing[0].get("Size") or 0) == size
    if same and not args.force:
        log("[skip] 内容未变化（md5/大小一致），跳过上传")
        info = dict(existing[0])
        info.setdefault("FileName", args.name)
        link = download_info(info, size, token, uuid, args.timeout)
        log("[link] 中转: %s" % link)
        log("[link] 直链: %s" % resolve_direct_link(link, args.timeout))
        return 0

    if args.dry_run:
        log("[dry-run] 计算完成，未发起上传")
        return 0

    if existing and not args.keep_old:
        log("[api] 删除旧文件 %s (fileId=%s)" %
            (args.name, existing[0].get("FileId")))
        try:
            delete_files(existing, token, uuid, args.timeout)
        except RuntimeError as exc:
            log("[api] 删除失败：%s" % exc)
            return 4

    log("[api] 申请上传...")
    try:
        req = api("file/upload_request", {
            "driveId": 0, "etag": md5, "fileName": args.name,
            "parentFileId": 0, "size": size, "type": 0, "RequestSource": None,
        }, token, uuid, args.timeout)
    except RuntimeError as exc:
        log("[api] 申请上传失败：%s" % exc)
        return 5
    if req.get("Reuse"):
        log("[api] 服务端命中秒传（Reuse=true），跳过字节上传")
        parts = 0
    else:
        parts = 1

    key = str(req.get("Key") or "")
    bucket = str(req.get("Bucket") or "")
    upload_id = str(req.get("UploadId") or "")
    node = str(req.get("StorageNode") or "")
    file_id = req.get("FileId")
    slice_size = int(req.get("SliceSize") or DEFAULT_SLICE)
    log("[api] bucket=%s key=%s uploadId=%s node=%s slice=%s fileId=%s" %
        (bucket, key, upload_id, node, human_size(slice_size), file_id))

    if parts:
        parts = max(1, (size + slice_size - 1) // slice_size)
        base_body = {"bucket": bucket, "key": key, "uploadId": upload_id,
                     "StorageNode": node}
        log("[api] 分片数=%d，申请签名..." % parts)
        if parts == 1:
            # 单分片走 PutObject 授权，multipart 接口不接受单份区间
            urls = presign(base_body, token, uuid, args.timeout, 1, 2,
                           endpoint="file/s3_upload_object/auth")
        else:
            urls = presign(base_body, token, uuid, args.timeout, 1, parts)
        missing = [n for n in range(1, parts + 1) if n not in urls]
        if missing:
            log("[api] 补签缺失分片：%s" % missing[:10])
            for n in missing:
                urls.update(presign(base_body, token, uuid, args.timeout, n, n))
            missing = [n for n in range(1, parts + 1) if n not in urls]
            if missing:
                log("[api] 仍有分片无签名：%s" % missing[:10])
                return 6
        log("[upload] 开始上传 %s，%d 片，并发 %d" %
            (human_size(size), parts, max(1, args.workers)))
        t0 = time.monotonic()
        try:
            upload_parts(apk, parts, slice_size, urls, args.timeout, args.workers)
        except RuntimeError as exc:
            log("[upload] 失败：%s" % exc)
            return 7
        log("[upload] 分片完成 %.2fs 平均 %s" %
            (time.monotonic() - t0, speed(size / max(1e-9, time.monotonic() - t0))))

    info = {}
    if parts:
        log("[api] 提交完成...")
        try:
            done = api("file/upload_complete/v2", {
                "fileId": file_id, "bucket": bucket, "fileSize": size,
                "key": key, "isMultipart": bool(parts > 1),
                "uploadId": upload_id, "StorageNode": node,
            }, token, uuid, args.timeout)
        except RuntimeError as exc:
            log("[api] 提交失败：%s" % exc)
            return 8
        info = dict(done.get("file_info") or {})
    if not info:
        log("[api] 完成响应缺少 file_info，改查目录")
        root = list_root(token, uuid, args.timeout)
        hits = [it for it in root if it.get("FileName") == args.name]
        if not hits:
            log("[api] 目录里找不到 %s" % args.name)
            return 9
        info = dict(hits[0])
    log("[api] 完成 fileId=%s size=%s etag=%s s3keyFlag=%s" %
        (info.get("FileId"), info.get("Size"), info.get("Etag"), info.get("S3KeyFlag")))
    if str(info.get("Etag") or "") and str(info.get("Etag")) != md5:
        log("[warn] 服务端 etag 与本地 md5 不一致：%s != %s" % (info.get("Etag"), md5))

    log("[api] 获取下载链接...")
    try:
        link = download_info(info, size, token, uuid, args.timeout)
        direct = resolve_direct_link(link, args.timeout)
    except RuntimeError as exc:
        log("[api] 获取直链失败：%s" % exc)
        return 10
    log("[link] 中转: %s" % link)
    log("[link] 直链: %s" % direct)
    log("[done] %s 已发布，%s" % (args.name, human_size(size)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
