#!/usr/bin/env python3
"""List and extract APK launcher icons, or fetch an app icon from F-Droid."""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


MAX_DOWNLOAD_SIZE = 8 * 1024 * 1024
USER_AGENT = "xime-apk-icon/1.0"
RASTER_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}


@dataclass
class IconVariant:
    path: str
    densities: set[int] = field(default_factory=set)
    width: int = 0
    height: int = 0
    size: int = 0


@dataclass
class SearchResult:
    package: str
    name: str
    summary: str
    icon_url: str


def find_aapt() -> str | None:
    """Find aapt on PATH or in an installed Android SDK."""
    on_path = shutil.which("aapt")
    if on_path:
        return on_path

    sdk_roots = {
        Path(value).expanduser()
        for value in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"))
        if value
    }
    project_root = Path(__file__).resolve().parents[1]
    sdk_roots.add(project_root.parent / ".buildozer/android/platform/android-sdk")

    candidates = []
    for root in sdk_roots:
        build_tools = root / "build-tools"
        if build_tools.is_dir():
            candidates.extend(build_tools.glob("*/aapt"))

    def version_key(path: Path) -> tuple[int, ...]:
        match = re.search(r"(\d+(?:\.\d+)+)", str(path.parent.name))
        return tuple(map(int, match.group(1).split("."))) if match else ()

    usable = sorted((path for path in candidates if path.is_file()), key=version_key)
    return str(usable[-1]) if usable else None


def parse_aapt_icons(output: str) -> dict[str, set[int]]:
    icons: dict[str, set[int]] = {}
    for line in output.splitlines():
        match = re.match(r"application-icon-(\d+):'([^']+)'$", line.strip())
        if match:
            density, path = int(match.group(1)), match.group(2)
            icons.setdefault(path, set()).add(density)

    application = re.search(r"^application:.*?\bicon='([^']+)'", output, re.MULTILINE)
    if application:
        icons.setdefault(application.group(1), set())
    return icons


def image_dimensions(data: bytes, suffix: str) -> tuple[int, int]:
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    if suffix == ".webp" and len(data) >= 25 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk_type = data[12:16]
        if chunk_type == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = data[21:25]
            width = 1 + bits[0] + ((bits[1] & 0x3F) << 8)
            height = 1 + (bits[1] >> 6) + (bits[2] << 2) + ((bits[3] & 0x0F) << 10)
            return width, height
        if chunk_type == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk_type == b"VP8 " and len(data) >= 30:
            marker = data.find(b"\x9d\x01\x2a", 20, 30)
            if marker >= 0 and len(data) >= marker + 7:
                width = int.from_bytes(data[marker + 3:marker + 5], "little") & 0x3FFF
                height = int.from_bytes(data[marker + 5:marker + 7], "little") & 0x3FFF
                return width, height

    if suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            segment_length = int.from_bytes(data[offset:offset + 2], "big")
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            } and segment_length >= 7:
                return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(
                    data[offset + 3:offset + 5], "big"
                )
            offset += segment_length
    return 0, 0


def apk_label(badging: str) -> str | None:
    match = re.search(r"^application: label='([^']*)'", badging, re.MULTILINE)
    return match.group(1) if match else None


def variant_sort_key(variant: IconVariant) -> tuple[int, int, int, int]:
    density = max(variant.densities, default=0)
    return variant.width * variant.height, min(variant.width, variant.height), density, variant.size


def extract_apk_icon(apk_path: Path, output: Path | None) -> int:
    if not apk_path.is_file():
        raise FileNotFoundError(f"APK 文件不存在：{apk_path}")

    aapt = find_aapt()
    if not aapt:
        raise RuntimeError(
            "找不到 Android SDK 的 aapt。请将 aapt 加入 PATH，或设置 ANDROID_HOME/ANDROID_SDK_ROOT。"
        )

    result = subprocess.run(
        [aapt, "dump", "badging", str(apk_path)],
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )
    badging = result.stdout
    icon_paths = parse_aapt_icons(badging)
    if not icon_paths:
        raise RuntimeError("aapt 没有在 APK Manifest 中找到应用图标。")

    variants: list[IconVariant] = []
    non_raster: list[str] = []
    with zipfile.ZipFile(apk_path) as apk:
        for path, densities in icon_paths.items():
            suffix = Path(path).suffix.lower()
            if suffix not in RASTER_EXTENSIONS:
                non_raster.append(path)
                continue
            try:
                info = apk.getinfo(path)
            except KeyError:
                non_raster.append(path + " (资源未作为独立文件打包)")
                continue
            if info.file_size > MAX_DOWNLOAD_SIZE:
                non_raster.append(path + " (超过安全读取上限)")
                continue
            data = apk.read(info)
            width, height = image_dimensions(data, suffix)
            variants.append(IconVariant(path, densities, width, height, len(data)))

    if not variants:
        details = "\n".join(f"  - {path}" for path in non_raster)
        raise RuntimeError("APK 图标是矢量/自适应 XML，或没有可直接提取的栅格图标。\n" + details)

    variants.sort(key=variant_sort_key, reverse=True)
    label = apk_label(badging)
    if label:
        print(f"应用：{label}")
    print(f"APK 图标资源（{len(variants)} 个栅格版本）：")
    for index, icon in enumerate(variants, start=1):
        density = ", ".join(f"{value}dpi" for value in sorted(icon.densities)) or "默认密度"
        dimensions = f"{icon.width}x{icon.height}" if icon.width and icon.height else "尺寸未知"
        print(f"  {index}. {Path(icon.path).name} | {icon.path} | {density} | {dimensions}")
    if non_raster:
        print("未提取的非栅格资源：" + "; ".join(non_raster))

    best = variants[0]
    destination = output or apk_path.with_name(f"{apk_path.stem}_icon{Path(best.path).suffix.lower()}")
    with zipfile.ZipFile(apk_path) as apk:
        destination.write_bytes(apk.read(best.path))
    print(f"已选择最大/最清晰图标：{best.path} ({best.width}x{best.height})")
    print(f"已保存：{destination}")
    return 0


class FdroidSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResult] = []
        self.current: dict[str, str] | None = None
        self.text_field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "package-header" in classes:
            href = attributes.get("href") or ""
            match = re.search(r"/packages/([^/?#]+)", urllib.parse.urlparse(href).path)
            if match:
                self.current = {
                    "package": urllib.parse.unquote(match.group(1)),
                    "name": "",
                    "summary": "",
                    "icon_url": "",
                }
        elif self.current is not None:
            if tag == "img" and "package-icon" in classes:
                self.current["icon_url"] = attributes.get("src") or ""
            elif tag == "h4" and "package-name" in classes:
                self.text_field = "name"
            elif tag == "span" and "package-summary" in classes:
                self.text_field = "summary"

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.text_field:
            self.current[self.text_field] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h4", "span"}:
            self.text_field = None
        elif tag == "a" and self.current is not None:
            result = SearchResult(
                package=self.current["package"],
                name=" ".join(self.current["name"].split()),
                summary=" ".join(self.current["summary"].split()),
                icon_url=self.current["icon_url"],
            )
            if result.icon_url:
                self.results.append(result)
            self.current = None


def fetch_url(url: str, maximum: int) -> tuple[bytes, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise RuntimeError(f"拒绝非 HTTPS 下载地址：{final_url}")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > maximum:
            raise RuntimeError(f"下载内容超过大小限制（{maximum} 字节）。")
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise RuntimeError(f"下载内容超过大小限制（{maximum} 字节）。")
        return data, response.headers.get_content_type(), final_url


def fetch_fdroid_icon(query: str, exact_package: bool, output: Path | None, result_index: int) -> int:
    search_url = "https://search.f-droid.org/?" + urllib.parse.urlencode({"q": query})
    page, _, _ = fetch_url(search_url, MAX_DOWNLOAD_SIZE)
    parser = FdroidSearchParser()
    parser.feed(page.decode("utf-8", "replace"))
    results = parser.results
    if exact_package:
        results = [result for result in results if result.package.casefold() == query.casefold()]
        if not results:
            raise LookupError(f"F-Droid 官方仓库中找不到包名：{query}")
    if not results:
        raise LookupError(f"F-Droid 官方仓库没有找到匹配“{query}”的应用。")

    print(f"F-Droid 搜索结果（{len(results)} 个）：")
    for index, result in enumerate(results, start=1):
        summary = f" — {result.summary}" if result.summary else ""
        print(f"  {index}. {result.name} ({result.package}){summary}")

    if not 1 <= result_index <= len(results):
        raise ValueError(f"--result 超出范围，请选择 1 到 {len(results)}。")
    selected = results[result_index - 1]
    icon_url = urllib.parse.urljoin("https://search.f-droid.org/", selected.icon_url)
    if urllib.parse.urlparse(icon_url).scheme != "https":
        raise RuntimeError(f"F-Droid 返回了非 HTTPS 图标地址：{icon_url}")
    image, content_type, final_url = fetch_url(icon_url, MAX_DOWNLOAD_SIZE)
    if not content_type.startswith("image/"):
        raise RuntimeError(f"F-Droid 图标地址返回了非图像内容：{content_type}")

    suffix = Path(urllib.parse.urlparse(final_url).path).suffix.lower()
    if suffix not in RASTER_EXTENSIONS:
        suffix = mimetypes.guess_extension(content_type) or ".img"
    safe_package = re.sub(r"[^A-Za-z0-9._-]", "_", selected.package)
    destination = output or Path.cwd() / f"{safe_package}_icon{suffix}"
    destination.write_bytes(image)
    print(f"已获取：{selected.name} ({selected.package})")
    print(f"已保存：{destination}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="列出并提取 APK 中最清晰的应用图标，或从 F-Droid 搜索应用图标。",
        epilog=(
            "示例：\n"
            "  %(prog)s app.apk\n"
            "  %(prog)s --package org.fdroid.fdroid\n"
            "  %(prog)s --keyword keyboard --result 2\n\n"
            "在线查询仅使用 F-Droid 官方搜索服务，不覆盖 Google Play 或厂商商店。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("apk", nargs="?", type=Path, help="本地 APK 文件")
    query = parser.add_mutually_exclusive_group()
    query.add_argument("--package", help="按精确包名从 F-Droid 搜索图标")
    query.add_argument("--keyword", help="按关键词从 F-Droid 搜索图标")
    parser.add_argument("-o", "--output", type=Path, help="输出图标路径")
    parser.add_argument(
        "--result",
        type=int,
        default=1,
        help="关键词查询时选择的结果序号（从 1 开始，默认 1）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    local_apk = args.apk is not None
    online_query = args.package is not None or args.keyword is not None
    if local_apk == online_query:
        parser.error("请提供一个本地 APK 路径，或指定 --package/--keyword 之一。")
    if args.result < 1:
        parser.error("--result 必须大于或等于 1。")
    if args.package and args.result != 1:
        parser.error("--result 仅适用于 --keyword 查询。")

    try:
        if local_apk:
            return extract_apk_icon(args.apk, args.output)
        query = args.package if args.package is not None else args.keyword
        return fetch_fdroid_icon(query, args.package is not None, args.output, args.result)
    except (
        FileNotFoundError,
        LookupError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        subprocess.CalledProcessError,
        urllib.error.URLError,
        OSError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
