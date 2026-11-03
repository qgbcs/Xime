#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一真机 e2e 测试框架（qgb 分支）。

把「过去真实翻过车的问题」固化成可筛选、可重复执行的用例集，每次改动后跑一遍：

  install        安装 APK（--skip-install 可跳过）
  ime            启用并切换到 Xime 输入法
  launch         拉起短信编写页并点输入框
  ime_shown      确认键盘真的弹出（mInputShown=true）
  schema_26      切到简体拼音（26 键基线）
  kb_26          26 键主键盘非空白
  panel_26       按左下角 ?123 进入符号面板
  panel_26_ok    符号面板非空白且与主键盘不同（20261102 中文 ?123 空白回归）
  panel_compact  符号面板每行键数 = 10/10/9/6（「符号显示太大、不够紧凑」回归）
  back_26        再按左下角键返回主键盘并确认恢复
  panel_again    再次按 ?123 回面板（全集符号页入口在面板里）
  open_symbol_page 面板点「符号」打开全集符号页
  symbol_back    符号页返回箭头回面板
  panel_en       菜单「中文•西文」切英文后 ?123 面板非空白且紧凑（英文 ?123 空白回归）
  restore_zh     切回中文（避免影响后续九键用例）
  schema_t9      切到拼音九键（拼音九键无法切换显示回归）
  t9_ok          九键布局出现且与 26 键不同
  t9_symbol      九键点「符号」面板非空白
  logcat         logcat 无 FATAL / AndroidRuntime 崩溃

用法：
  python3 scripts/qgb_device_test.py --apk out/com.qgb.xime-20261103-arm64-v8a.apk
  python3 scripts/qgb_device_test.py --skip-install --serial ENU7N16322378460
  python3 scripts/qgb_device_test.py --skip-install --cases panel_26,panel_compact,t9_ok
  python3 scripts/qgb_device_test.py --list-cases

坐标全部是「屏幕比例」，默认值按 Nexus 6P（1440x2560，API27）校准；
换机型先 --list-cases 看用例，再按截图调参数。

退出码：0 全部通过；1 有失败项；2 环境/依赖错误。
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib

DEFAULT_PACKAGE = "com.qgb.xime"
DEFAULT_IME = "com.qgb.xime/com.kingzcheung.xime.service.XimeInputMethodService"
DEFAULT_ADB = os.environ.get("ADB", shutil.which("adb") or "adb")


class Fail(Exception):
    pass


class Adb:
    def __init__(self, path, serial=None):
        self.base = [path] + (["-s", serial] if serial else [])

    def sh(self, *args, timeout=60, check=True):
        p = subprocess.run(self.base + list(args), capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise Fail("adb %s 失败 rc=%s: %s" % (" ".join(args), p.returncode, p.stderr.strip()))
        return p.stdout

    def shell(self, cmd, timeout=60, check=True):
        return self.sh("shell", cmd, timeout=timeout, check=check)

    def screencap(self, path):
        # exec-out 比 shell screencap + pull 少一次落盘
        p = subprocess.run(self.base + ["exec-out", "screencap", "-p"], capture_output=True, timeout=60)
        if p.returncode != 0 or not p.stdout:
            raise Fail("screencap 失败: %s" % p.stderr.decode("utf-8", "ignore"))
        with open(path, "wb") as f:
            f.write(p.stdout)

    def screen_size(self):
        out = self.shell("wm size")
        m = re.findall(r"(\d+)x(\d+)", out)
        if not m:
            raise Fail("wm size 解析失败: %s" % out)
        return int(m[-1][0]), int(m[-1][1])


# ── 最小 PNG 解码（只处理 Android screencap 的 8bit RGB/RGBA、非隔行）──

def _paeth(a, b, c):
    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


_PNG_CACHE = {}


def read_png_rgba(path):
    key = (os.path.abspath(path), os.path.getmtime(path))
    if key in _PNG_CACHE:
        return _PNG_CACHE[key]
    data = open(path, "rb").read()
    pos = 8
    width = height = depth = ctype = None
    idat = b""
    while pos < len(data):
        length, ctag = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctag == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or interlace != 0 or ctype not in (2, 6):
                raise Fail("不支持的 PNG: depth=%s ctype=%s interlace=%s" % (depth, ctype, interlace))
        elif ctag == b"IDAT":
            idat += chunk
        elif ctag == b"IEND":
            break
    if width is None:
        raise Fail("PNG 缺少 IHDR")
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        ft = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        if ft == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ft == 4:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                upleft = prev[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + _paeth(left, prev[x], upleft)) & 0xFF
        rows.append(line)
        prev = line
    out = (width, height, bpp, rows)
    _PNG_CACHE[key] = out
    return out


def region_stats(png_path, y0f, y1f, x0f=0.0, x1f=1.0):
    """返回 (相邻像素平均差, 平均亮度)：空白键盘两者都接近 0。"""
    w, h, bpp, rows = read_png_rgba(png_path)
    y0, y1 = int(h * y0f), int(h * y1f)
    x0, x1 = int(w * x0f), int(w * x1f)
    total = 0
    count = 0
    lum = 0
    for y in range(y0, y1, 4):
        row = rows[y]
        prev = None
        for x in range(x0, x1, 4):
            o = x * bpp
            v = (row[o] + row[o + 1] + row[o + 2]) // 3
            lum += v
            if prev is not None:
                total += abs(v - prev)
                count += 1
            prev = v
    return (total / max(count, 1), lum / max((y1 - y0) // 4 * ((x1 - x0) // 4), 1))


def region_diff(a, b, y0f, y1f, x0f=0.0, x1f=1.0):
    wa, ha, bppa, ra = read_png_rgba(a)
    wb, hb, bppb, rb = read_png_rgba(b)
    if (wa, ha) != (wb, hb):
        return 255.0
    y0, y1 = int(ha * y0f), int(ha * y1f)
    x0, x1 = int(wa * x0f), int(wa * x1f)
    total = 0
    count = 0
    for y in range(y0, y1, 4):
        rowa, rowb = ra[y], rb[y]
        for x in range(x0, x1, 4):
            o = x * bppa
            total += abs(rowa[o] - rowb[o]) + abs(rowa[o + 1] - rowb[o + 1]) + abs(rowa[o + 2] - rowb[o + 2])
            count += 1
    return total / max(count * 3, 1)


def edge_ratio(png_path, y0f, y1f, x0f=0.0, x1f=1.0, threshold=20):
    """区域内横向相邻差 > threshold 的像素占比。

    空白页（整片纯色 + 零星文字）约 1%，键盘（键框/键帽/文字）约 3%-6%；
    比"平均差"更能区分浅色主题下的空白键盘。
    """
    w, h, bpp, rows = read_png_rgba(png_path)
    y0, y1 = int(h * y0f), int(h * y1f)
    x0, x1 = int(w * x0f), int(w * x1f)
    edges = 0
    count = 0
    for y in range(y0, y1, 4):
        row = rows[y]
        prev = None
        for x in range(x0, x1, 4):
            o = x * bpp
            v = (row[o] + row[o + 1] + row[o + 2]) // 3
            if prev is not None:
                if abs(v - prev) > threshold:
                    edges += 1
                count += 1
            prev = v
    return edges / max(count, 1)


def count_key_runs(png_path, yf, threshold=12, max_gap=6, min_run=20):
    """在某个 y 比例处横向扫描，统计"键面"色块数量。

    背景色取最左侧像素；与背景通道差 > threshold 的连续像素算键面。
    键与键之间只有 2dp 内衬，抗锯齿可能让 1-2 个像素回落到背景色，
    因此把间隔 <= max_gap 的相邻色块合并，再过滤掉过短的碎块。
    """
    w, h, bpp, rows = read_png_rgba(png_path)
    y = int(h * yf)
    row = rows[y]
    o = 0
    bg = (row[0], row[1], row[2])
    runs = []
    start = 0
    inrun = False
    for x in range(w):
        o = x * bpp
        d = max(abs(row[o] - bg[0]), abs(row[o + 1] - bg[1]), abs(row[o + 2] - bg[2]))
        if d > threshold:
            if not inrun:
                inrun = True
                start = x
        else:
            if inrun:
                runs.append((start, x - start))
                inrun = False
    if inrun:
        runs.append((start, w - start))
    merged = []
    for s, ln in runs:
        if merged and s - (merged[-1][0] + merged[-1][1]) <= max_gap:
            last_s, last_ln = merged[-1]
            merged[-1] = (last_s, s + ln - last_s)
        else:
            merged.append((s, ln))
    return [r for r in merged if r[1] >= min_run]


def count_columns_robust(png_path, y0f, y1f, samples=9, threshold=12, max_gap=6, min_run=20):
    """在 y0f-y1f 区间内多条扫描线取样，取出现次数最多的键数。

    网格行与行之间有明显留白，单条扫描线可能正好落在行间隙；
    取众数可以稳定得到列数（同时排除只有 1 段的间隙扫描线）。
    """
    from collections import Counter
    counts = Counter()
    for i in range(samples):
        yf = y0f + (y1f - y0f) * i / max(samples - 1, 1)
        n = len(count_key_runs(png_path, yf, threshold, max_gap, min_run))
        if n >= 2:
            counts[n] += 1
    if not counts:
        return 0
    return counts.most_common(1)[0][0]


def detect_key_rows(png_path, y0f=0.655, y1f=0.95, step=0.004, min_cells=5, min_samples=6):
    """自动找出按键行的中心 y 比例（从下往上取最后 4 行）。

    逐条扫描线统计键数，连续 >= min_cells 的扫描线聚成一行；
    这样中/英面板、候选栏高度不同、键盘整体上下位移都不影响断言。
    """
    bands = []
    cur = []
    y = y0f
    while y <= y1f:
        n = len(count_key_runs(png_path, y))
        if n >= min_cells:
            cur.append(y)
        else:
            if len(cur) >= min_samples:
                bands.append(cur)
            cur = []
        y += step
    if len(cur) >= min_samples:
        bands.append(cur)
    centers = [sum(b) / len(b) for b in bands]
    return centers[-4:] if len(centers) >= 4 else centers


def check_row_counts(png_path, ys, expected, label):
    if ys is None:
        ys = detect_key_rows(png_path)
        if len(ys) < 4:
            raise Fail("%s 只找到 %d 行按键（应至少 4 行）" % (label, len(ys)))
        print("    自动定位按键行 y=%s" % ["%.3f" % v for v in ys], flush=True)
    actual = [len(count_key_runs(png_path, y)) for y in ys]
    if actual != list(expected):
        raise Fail("%s 每行键数 %s != 期望 %s（面板没有恢复紧凑布局？）" % (label, actual, list(expected)))
    print("    %s 每行键数=%s" % (label, actual), flush=True)


# ── 设备操作小工具 ──

def wait_ime_shown(adb, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        d = adb.shell("dumpsys input_method", check=False)
        if "mInputShown=true" in d:
            return True
        time.sleep(0.5)
    return False


def check_logcat(adb):
    out = adb.sh("logcat", "-d", "-v", "brief", check=False)
    bad = [l for l in out.splitlines() if re.search(r"FATAL EXCEPTION|E/AndroidRuntime", l)]
    if bad:
        raise Fail("logcat 命中崩溃：\n" + "\n".join(bad[-8:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk")
    ap.add_argument("--skip-install", action="store_true")
    ap.add_argument("--serial")
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    ap.add_argument("--ime", default=DEFAULT_IME)
    ap.add_argument("--out", default="out/device_test")
    ap.add_argument("--cases", help="只跑这些用例（逗号分隔），默认全部")
    ap.add_argument("--skip-cases", help="跳过这些用例（逗号分隔）")
    ap.add_argument("--list-cases", action="store_true", help="打印用例列表后退出")
    ap.add_argument("--expect-version", help="安装了 APK 时校验 versionName 包含该子串")
    ap.add_argument("--inputx", type=float, default=0.61, help="短信输入框点击 x 占比")
    ap.add_argument("--inputy", type=float, default=0.89, help="短信输入框点击 y 占比")
    ap.add_argument("--panelx", type=float, default=0.083, help="?123 键中心 x 占比")
    ap.add_argument("--panely", type=float, default=0.879, help="?123 / 返回 键中心 y 占比")
    ap.add_argument("--toolbarx", type=float, default=0.056, help="键盘工具栏菜单图标 x 占比")
    ap.add_argument("--toolbary", type=float, default=0.63, help="键盘工具栏菜单图标 y 占比")
    ap.add_argument("--schemacardx", type=float, default=0.616, help="菜单「输入方案」卡片 x 占比")
    ap.add_argument("--schemacardy", type=float, default=0.698, help="菜单「输入方案」卡片 y 占比")
    ap.add_argument("--pinyin26x", type=float, default=0.616, help="方案列表「简体拼音」卡片 x 占比")
    ap.add_argument("--pinyin26y", type=float, default=0.713, help="方案列表「简体拼音」卡片 y 占比")
    ap.add_argument("--t9x", type=float, default=0.376, help="方案列表「拼音九键」卡片 x 占比")
    ap.add_argument("--t9y", type=float, default=0.713, help="方案列表「拼音九键」卡片 y 占比")
    ap.add_argument("--symbolkeyx", type=float, default=0.069, help="面板「符号」键 x 占比")
    ap.add_argument("--symbolkeyy", type=float, default=0.810, help="面板「符号」键 y 占比")
    ap.add_argument("--symbolbackx", type=float, default=0.049, help="全集符号页返回箭头 x 占比")
    ap.add_argument("--symbolbacky", type=float, default=0.635, help="全集符号页返回箭头 y 占比")
    ap.add_argument("--symbolcols", type=int, default=8, help="全集符号页期望列数")
    ap.add_argument("--rowcounts", default="10,10,9,6", help="面板每行期望键数（逗号分隔）")
    ap.add_argument("--enkeyx", type=float, default=0.46, help="菜单「中文•西文」西文半区 x 占比")
    ap.add_argument("--zhkeyx", type=float, default=0.326, help="菜单「中文•西文」中文半区 x 占比")
    ap.add_argument("--langcardy", type=float, default=0.838, help="菜单「中文•西文」卡片 y 占比")
    args = ap.parse_args()

    cases = []
    seen = set()

    def add(cid, title, fn):
        if cid not in seen:
            seen.add(cid)
            cases.append((cid, title, fn))

    selected = set(x.strip() for x in args.cases.split(",") if x.strip()) if args.cases else None
    skipped = set(x.strip() for x in args.skip_cases.split(",") if x.strip()) if args.skip_cases else set()

    def enabled(cid):
        if cid in skipped:
            return False
        return selected is None or cid in selected

    results = []

    def run(cid, title, fn):
        if not enabled(cid):
            print("-- skip %s %s" % (cid, title), flush=True)
            return
        print("==> [%s] %s" % (cid, title), flush=True)
        try:
            fn()
            results.append((cid, title, True, ""))
            print("    PASS", flush=True)
        except Fail as e:
            results.append((cid, title, False, str(e)))
            print("    FAIL: %s" % e, flush=True)

    # ── 环境与前置 ──
    adb = Adb(args.adb, args.serial)
    ctx = {}

    def do_install():
        if not args.apk or not os.path.exists(args.apk):
            raise Fail("APK 不存在: %s" % args.apk)
        adb.sh("install", "-r", args.apk, timeout=600)

    add("install", "安装 APK", do_install)

    def check_version():
        if not args.expect_version:
            return
        d = adb.shell("dumpsys package %s" % args.package, check=False)
        m = re.search(r"versionName=(\S+)", d)
        if not m:
            raise Fail("dumpsys 未找到 versionName")
        if args.expect_version not in m.group(1):
            raise Fail("versionName=%s 不含 %s" % (m.group(1), args.expect_version))
        print("    versionName=%s" % m.group(1), flush=True)

    add("version", "校验已安装版本号（可选）", check_version)

    def ime_setup():
        adb.shell("ime enable %s" % args.ime, check=False)
        adb.shell("ime set %s" % args.ime)

    add("ime", "启用并切换输入法", ime_setup)

    add("logcat_clear", "清空 logcat 基线", lambda: adb.sh("logcat", "-c", check=False))

    def screen_size():
        if "w" not in ctx:
            ctx["w"], ctx["h"] = adb.screen_size()
        return ctx["w"], ctx["h"]

    def tap(fx, fy, wait=1.2):
        w, h = screen_size()
        adb.shell("input tap %d %d" % (int(w * fx), int(h * fy)))
        time.sleep(wait)

    def launch():
        w, h = screen_size()
        adb.shell("am force-stop com.google.android.apps.messaging", check=False)
        adb.shell("am start -a android.intent.action.SENDTO -d smsto:10086", check=False)
        time.sleep(2.5)
        adb.shell("input tap %d %d" % (int(w * args.inputx), int(h * args.inputy)))
        time.sleep(1.2)
        # 首次点可能落在附件面板/工具栏上：没弹出键盘就按 BACK 收起再点一次
        if not wait_ime_shown(adb, 2):
            adb.shell("input keyevent 4")
            time.sleep(0.6)
            adb.shell("input tap %d %d" % (int(w * args.inputx), int(h * args.inputy)))
            time.sleep(1.2)

    add("launch", "拉起输入框", launch)

    def ime_visible():
        if not wait_ime_shown(adb):
            raise Fail("键盘没有弹出（mInputShown=false）")

    add("ime_shown", "键盘弹出", ime_visible)

    def open_schema_menu():
        tap(args.toolbarx, args.toolbary, 0.9)
        tap(args.schemacardx, args.schemacardy, 0.9)

    def switch_schema(x, y):
        tap(x, y, 1.4)

    add("schema_26", "切到简体拼音（26 键基线）",
        lambda: (open_schema_menu(), switch_schema(args.pinyin26x, args.pinyin26y)))

    def shot_full():
        p = os.path.join(args.out, "1_full.png")
        adb.screencap(p)
        ctx["full_png"] = p

    add("kb_26", "截图 26 键主键盘", shot_full)

    def full_not_blank():
        p = ctx["full_png"]
        var, lum = region_stats(p, 0.60, 0.95)
        er = edge_ratio(p, 0.63, 0.90)
        if er < 0.02:
            raise Fail("主键盘区域近乎空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        print("    主键盘边缘占比=%.3f 相邻差=%.1f 亮度=%.1f" % (er, var, lum), flush=True)

    add("kb_26_ok", "26 键主键盘非空白", full_not_blank)

    add("panel_26", "按 ?123 进入符号面板", lambda: tap(args.panelx, args.panely))

    def shot_panel(name):
        p = os.path.join(args.out, name)
        adb.screencap(p)
        ctx["panel_png"] = p

    add("panel_26_shot", "截图符号面板", lambda: shot_panel("2_panel.png"))

    def panel_not_blank():
        p = ctx.get("panel_png")
        if not p:
            p = os.path.join(args.out, "2_panel.png")
        var, lum = region_stats(p, 0.60, 0.95)
        er = edge_ratio(p, 0.63, 0.90)
        if er < 0.02:
            raise Fail("符号面板空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        full = ctx.get("full_png")
        if full and os.path.exists(full):
            diff = region_diff(full, p, 0.60, 0.95)
            if diff < 8:
                raise Fail("符号面板与主键盘几乎相同（diff=%.1f），可能没切过去" % diff)
            print("    符号面板边缘占比=%.3f 相邻差=%.1f diff=%.1f" % (er, var, diff), flush=True)
        else:
            print("    符号面板边缘占比=%.3f 相邻差=%.1f" % (er, var), flush=True)

    add("panel_26_ok", "符号面板非空白且已切换", panel_not_blank)

    def panel_compact():
        p = ctx.get("panel_png")
        if not p:
            p = os.path.join(args.out, "2_panel.png")
        expected = [int(x) for x in args.rowcounts.split(",")]
        check_row_counts(p, None, expected, "符号面板")

    add("panel_compact", "符号面板每行键数（紧凑布局回归）", panel_compact)

    add("back_26", "按左下角键返回主键盘", lambda: tap(args.panelx, args.panely))

    def shot_back():
        p = os.path.join(args.out, "3_back.png")
        adb.screencap(p)
        ctx["back_png"] = p

    add("back_26_shot", "截图返回后的主键盘", shot_back)

    def back_ok():
        p = ctx["back_png"]
        var, _ = region_stats(p, 0.60, 0.95)
        er = edge_ratio(p, 0.63, 0.90)
        if er < 0.02:
            raise Fail("返回主键盘后空白（边缘占比=%.3f 相邻差=%.1f）" % (er, var))
        print("    返回后边缘占比=%.3f 相邻差=%.1f" % (er, var), flush=True)

    add("back_26_ok", "返回主键盘恢复显示", back_ok)

    # ── 全集符号页（默认英文分类、8 列网格）──
    add("panel_again", "回主键盘后再次进 ?123 面板",
        lambda: tap(args.panelx, args.panely))

    add("open_symbol_page", "面板点「符号」打开全集符号页",
        lambda: tap(args.symbolkeyx, args.symbolkeyy))

    def shot_symbol_page():
        p = os.path.join(args.out, "4_symbol_page.png")
        adb.screencap(p)
        ctx["symbol_png"] = p

    add("symbol_page_shot", "截图全集符号页", shot_symbol_page)

    def symbol_page_ok():
        p = ctx["symbol_png"]
        var, lum = region_stats(p, 0.62, 0.92)
        er = edge_ratio(p, 0.62, 0.92)
        if er < 0.02:
            raise Fail("符号页空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        cols = count_columns_robust(p, 0.66, 0.80)
        if cols != args.symbolcols:
            raise Fail("符号页列数=%d != 期望 %d（布局被改动？）" % (cols, args.symbolcols))
        print("    符号页边缘占比=%.3f 列数=%d" % (er, cols), flush=True)

    add("symbol_page_ok", "符号页非空白且列数正确", symbol_page_ok)

    add("symbol_back", "符号页返回箭头回面板",
        lambda: tap(args.symbolbackx, args.symbolbacky))

    # ── 英文模式 ?123 面板（20261102 英文 ?123 空白回归）──
    def row_signature(p):
        return [len(count_key_runs(p, y)) for y in detect_key_rows(p)]

    def close_compact_panel_if_any():
        """symbol_back 之后仍停在 ?123 面板里；面板没有工具栏，必须先回主键盘。

        用截图里的每行键数判断当前是否面板（10/10/9/6），避免 --cases 单跑时
        状态不对就乱点。
        """
        p = os.path.join(args.out, "8_state_before_en.png")
        adb.screencap(p)
        if row_signature(p) == [int(x) for x in args.rowcounts.split(",")]:
            tap(args.panelx, args.panely, 1.2)

    def switch_en():
        close_compact_panel_if_any()
        tap(args.toolbarx, args.toolbary, 0.9)
        tap(args.enkeyx, args.langcardy, 1.5)
        # 先断言语言真的切过去了：英文主键盘 vs 中文主键盘必须不同
        p = os.path.join(args.out, "5_en_main.png")
        adb.screencap(p)
        ctx["en_main_png"] = p
        zh = ctx.get("full_png")
        if zh and os.path.exists(zh):
            d = region_diff(zh, p, 0.60, 0.95)
            # 英文键盘与中文只差键上的拼音注音，diff 只有 4-5；未切换时约 1.7
            if d < 3:
                raise Fail("切英文后主键盘与中文几乎相同（diff=%.1f），语言没切过去" % d)
            print("    英文主键盘 diff(中)=%.1f" % d, flush=True)
        tap(args.panelx, args.panely, 1.5)

    add("panel_en", "切英文后按 ?123", switch_en)

    def shot_panel_en():
        p = os.path.join(args.out, "5_panel_en.png")
        adb.screencap(p)
        ctx["panel_en_png"] = p

    add("panel_en_shot", "截图英文 ?123 面板", shot_panel_en)

    def panel_en_ok():
        p = ctx["panel_en_png"]
        var, lum = region_stats(p, 0.60, 0.95)
        er = edge_ratio(p, 0.63, 0.90)
        if er < 0.02:
            raise Fail("英文 ?123 面板空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        en_main = ctx.get("en_main_png")
        diff = 0.0
        if en_main and os.path.exists(en_main):
            diff = region_diff(en_main, p, 0.60, 0.95)
            if diff < 8:
                raise Fail("英文 ?123 与英文主键盘几乎相同（diff=%.1f），可能没切到面板" % diff)
        expected = [int(x) for x in args.rowcounts.split(",")]
        check_row_counts(p, None, expected, "英文符号面板")
        print("    英文 ?123 边缘占比=%.3f diff(英主键)=%.1f" % (er, diff), flush=True)

    add("panel_en_ok", "英文 ?123 面板非空白且紧凑", panel_en_ok)

    def restore_zh():
        tap(args.panelx, args.panely)  # 面板左下角「返回」回主键盘
        tap(args.toolbarx, args.toolbary, 0.9)
        tap(args.zhkeyx, args.langcardy, 1.0)

    add("restore_zh", "切回中文", restore_zh)

    # ── 拼音九键（无法切换显示回归）──
    add("schema_t9", "切到拼音九键",
        lambda: (open_schema_menu(), switch_schema(args.t9x, args.t9y)))

    def shot_t9():
        p = os.path.join(args.out, "6_t9.png")
        adb.screencap(p)
        ctx["t9_png"] = p

    add("t9_shot", "截图九键键盘", shot_t9)

    def t9_ok():
        p = ctx["t9_png"]
        var, lum = region_stats(p, 0.60, 0.95)
        er = edge_ratio(p, 0.63, 0.90)
        if er < 0.02:
            raise Fail("九键键盘空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        full = ctx.get("full_png")
        if full:
            diff = region_diff(full, p, 0.60, 0.95)
            if diff < 8:
                raise Fail("九键与 26 键几乎相同（diff=%.1f），可能没切换" % diff)
            print("    九键边缘占比=%.3f diff=%.1f" % (er, diff), flush=True)
        else:
            print("    九键边缘占比=%.3f" % er, flush=True)

    add("t9_ok", "九键布局已切换且非空白", t9_ok)

    add("t9_symbol", "九键点「符号」", lambda: tap(args.symbolkeyx, args.panely))

    def t9_symbol_ok():
        p = os.path.join(args.out, "7_t9_symbol.png")
        adb.screencap(p)
        var, lum = region_stats(p, 0.60, 0.95)
        er = edge_ratio(p, 0.63, 0.90)
        if er < 0.02:
            raise Fail("九键符号面板空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        print("    九键符号面板边缘占比=%.3f" % er, flush=True)

    add("t9_symbol_ok", "九键符号面板非空白", t9_symbol_ok)

    add("logcat", "logcat 无崩溃", lambda: check_logcat(adb))

    if args.list_cases:
        for cid, title, _ in cases:
            print("%-18s %s" % (cid, title))
        return 0

    if not (os.path.exists(args.adb) or shutil.which(args.adb)):
        print("[FAIL] 找不到 adb: %s" % args.adb)
        return 2
    os.makedirs(args.out, exist_ok=True)

    for cid, title, fn in cases:
        if cid == "install" and args.skip_install:
            print("-- skip install （--skip-install）", flush=True)
            continue
        run(cid, title, fn)

    print("\n==== 结果 ====")
    bad = 0
    for cid, title, ok, err in results:
        print(("PASS" if ok else "FAIL") + "  [%s] %s%s" % (cid, title, ("  | " + err) if err else ""))
        bad += 0 if ok else 1
    print("截图目录: %s" % os.path.abspath(args.out))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
