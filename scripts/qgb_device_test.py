#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一真机测试脚本（qgb 分支）。

覆盖每次改动后最容易翻车的几条链路：
  1. adb 安装 APK（可用 --skip-install 跳过）
  2. 启用/切换 Xime 输入法
  3. 拉起短信编写页，点输入框，确认键盘真的弹出（mInputShown=true）
  4. 切到简体拼音（26 键基线），截图并判断键盘区域“不是空白页”（边缘像素占比）
  5. 按左下角 ?123，确认符号/数字面板有内容且与主键盘不同
  6. 再按左下角键返回主键盘，确认恢复
  7. 切到拼音九键，确认九键布局出现，再按「符号」确认面板非空白
  8. 检查 logcat 有无 FATAL / AndroidRuntime 崩溃

用法：
  python3 scripts/qgb_device_test.py --apk out/com.qgb.xime-20261103-arm64-v8a.apk
  python3 scripts/qgb_device_test.py --skip-install --serial ENU7N16322378460

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


def read_png_rgba(path):
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
    return width, height, bpp, rows


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
    比“平均差”更能区分浅色主题下的空白键盘。
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


# ── 测试步骤 ──

def check_logcat(adb):
    out = adb.sh("logcat", "-d", "-v", "brief", check=False)
    bad = [l for l in out.splitlines() if re.search(r"FATAL EXCEPTION|E/AndroidRuntime", l)]
    if bad:
        raise Fail("logcat 命中崩溃：\n" + "\n".join(bad[-8:]))


def wait_ime_shown(adb, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        d = adb.shell("dumpsys input_method", check=False)
        if "mInputShown=true" in d:
            return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk")
    ap.add_argument("--skip-install", action="store_true")
    ap.add_argument("--serial")
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    ap.add_argument("--ime", default=DEFAULT_IME)
    ap.add_argument("--out", default="out/device_test")
    ap.add_argument("--panely", type=float, default=0.879, help="?123/符号 键中心 y 占比")
    ap.add_argument("--panelx", type=float, default=0.083, help="?123 键中心 x 占比")
    ap.add_argument("--inputy", type=float, default=0.89, help="短信输入框点击 y 占比")
    ap.add_argument("--toolbarx", type=float, default=0.056, help="键盘工具栏菜单图标 x 占比")
    ap.add_argument("--toolbary", type=float, default=0.63, help="键盘工具栏菜单图标 y 占比")
    ap.add_argument("--schemacardx", type=float, default=0.616, help="菜单「输入方案」卡片 x 占比")
    ap.add_argument("--schemacardy", type=float, default=0.698, help="菜单「输入方案」卡片 y 占比")
    ap.add_argument("--pinyin26x", type=float, default=0.616, help="方案列表「简体拼音」卡片 x 占比")
    ap.add_argument("--pinyin26y", type=float, default=0.713, help="方案列表「简体拼音」卡片 y 占比")
    ap.add_argument("--t9x", type=float, default=0.376, help="方案列表「拼音九键」卡片 x 占比")
    ap.add_argument("--t9y", type=float, default=0.713, help="方案列表「拼音九键」卡片 y 占比")
    ap.add_argument("--symbolkeyx", type=float, default=0.065, help="九键「符号」键 x 占比")
    args = ap.parse_args()

    if not (os.path.exists(args.adb) or shutil.which(args.adb)):
        print("[FAIL] 找不到 adb: %s" % args.adb)
        return 2

    adb = Adb(args.adb, args.serial)
    os.makedirs(args.out, exist_ok=True)
    results = []

    def step(name, fn):
        print("==> %s" % name, flush=True)
        try:
            fn()
            results.append((name, True, ""))
            print("    PASS", flush=True)
        except Fail as e:
            results.append((name, False, str(e)))
            print("    FAIL: %s" % e, flush=True)

    if not args.skip_install:
        if not args.apk or not os.path.exists(args.apk):
            print("[FAIL] APK 不存在: %s" % args.apk)
            return 2
        step("安装 APK", lambda: adb.sh("install", "-r", args.apk, timeout=600))

    def ime_setup():
        adb.shell("ime enable %s" % args.ime, check=False)
        adb.shell("ime set %s" % args.ime)

    step("启用并切换输入法", ime_setup)
    adb.sh("logcat", "-c", check=False)

    w, h = adb.screen_size()

    def launch():
        adb.shell("am force-stop com.google.android.apps.messaging", check=False)
        adb.shell("am start -a android.intent.action.SENDTO -d smsto:10086", check=False)
        time.sleep(2.5)
        adb.shell("input tap %d %d" % (int(w * 0.61), int(h * args.inputy)))
        time.sleep(1.5)

    step("拉起输入框", launch)

    def ime_visible():
        if not wait_ime_shown(adb):
            raise Fail("键盘没有弹出（mInputShown=false）")

    step("键盘弹出", ime_visible)

    # 统一从 26 键简体拼音开始，保证后续 ?123 / 九键的坐标与基线稳定
    def open_schema_menu():
        adb.shell("input tap %d %d" % (int(w * args.toolbarx), int(h * args.toolbary)))
        time.sleep(0.9)
        adb.shell("input tap %d %d" % (int(w * args.schemacardx), int(h * args.schemacardy)))
        time.sleep(0.9)

    def switch_schema(x, y):
        adb.shell("input tap %d %d" % (int(w * x), int(h * y)))
        time.sleep(1.4)

    step("切到简体拼音（26 键基线）",
         lambda: (open_schema_menu(), switch_schema(args.pinyin26x, args.pinyin26y)))

    full_png = os.path.join(args.out, "1_full.png")
    step("截图主键盘", lambda: adb.screencap(full_png))

    def full_not_blank():
        var, lum = region_stats(full_png, 0.60, 0.95)
        er = edge_ratio(full_png, 0.63, 0.90)
        if er < 0.02:
            raise Fail("主键盘区域近乎空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        print("    主键盘边缘占比=%.3f 相邻差=%.1f 亮度=%.1f" % (er, var, lum), flush=True)

    step("主键盘非空白", full_not_blank)

    def tap_left_bottom():
        adb.shell("input tap %d %d" % (int(w * args.panelx), int(h * args.panely)))
        time.sleep(1.2)

    step("按 ?123 进入符号面板", tap_left_bottom)

    panel_png = os.path.join(args.out, "2_panel.png")
    step("截图符号面板", lambda: adb.screencap(panel_png))

    def panel_not_blank():
        var, lum = region_stats(panel_png, 0.60, 0.95)
        er = edge_ratio(panel_png, 0.63, 0.90)
        if er < 0.02:
            raise Fail("符号面板空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        diff = region_diff(full_png, panel_png, 0.60, 0.95)
        if diff < 8:
            raise Fail("符号面板与主键盘几乎相同（diff=%.1f），可能没切过去" % diff)
        print("    符号面板边缘占比=%.3f 相邻差=%.1f diff=%.1f" % (er, var, diff), flush=True)

    step("符号面板有内容且已切换", panel_not_blank)

    step("按返回键回主键盘", tap_left_bottom)
    back_png = os.path.join(args.out, "3_back.png")
    step("截图返回后的主键盘", lambda: adb.screencap(back_png))

    def back_ok():
        var, _ = region_stats(back_png, 0.60, 0.95)
        er = edge_ratio(back_png, 0.63, 0.90)
        if er < 0.02:
            raise Fail("返回主键盘后空白（边缘占比=%.3f 相邻差=%.1f）" % (er, var))
        print("    返回后边缘占比=%.3f 相邻差=%.1f" % (er, var), flush=True)

    step("返回主键盘恢复显示", back_ok)

    # ── T9 九键：切到九键，点「符号」确认面板非空白 ──
    t9_png = os.path.join(args.out, "5_t9.png")
    step("切到拼音九键", lambda: (open_schema_menu(), switch_schema(args.t9x, args.t9y)))
    step("截图九键键盘", lambda: adb.screencap(t9_png))

    def t9_ok():
        var, lum = region_stats(t9_png, 0.60, 0.95)
        er = edge_ratio(t9_png, 0.63, 0.90)
        if er < 0.02:
            raise Fail("九键键盘空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        diff = region_diff(full_png, t9_png, 0.60, 0.95)
        if diff < 8:
            raise Fail("九键与 26 键几乎相同（diff=%.1f），可能没切换" % diff)
        print("    九键边缘占比=%.3f 相邻差=%.1f diff=%.1f" % (er, var, diff), flush=True)

    step("九键布局已切换且非空白", t9_ok)

    t9_sym_png = os.path.join(args.out, "6_t9_symbol.png")

    def tap_t9_symbol():
        adb.shell("input tap %d %d" % (int(w * args.symbolkeyx), int(h * args.panely)))
        time.sleep(1.2)

    step("九键点「符号」", tap_t9_symbol)
    step("截图九键符号面板", lambda: adb.screencap(t9_sym_png))

    def t9_symbol_ok():
        var, lum = region_stats(t9_sym_png, 0.60, 0.95)
        er = edge_ratio(t9_sym_png, 0.63, 0.90)
        if er < 0.02:
            raise Fail("九键符号面板空白（边缘占比=%.3f 相邻差=%.1f 亮度=%.1f）" % (er, var, lum))
        print("    九键符号面板边缘占比=%.3f 相邻差=%.1f 亮度=%.1f" % (er, var, lum), flush=True)

    step("九键符号面板有内容", t9_symbol_ok)
    step("logcat 无崩溃", lambda: check_logcat(adb))

    print("\n==== 结果 ====")
    bad = 0
    for name, ok, err in results:
        print(("PASS" if ok else "FAIL") + "  " + name + (("  | " + err) if err else ""))
        bad += 0 if ok else 1
    print("截图目录: %s" % os.path.abspath(args.out))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
