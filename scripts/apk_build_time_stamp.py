#!/usr/bin/env python3
"""在 APK 内原地写入/读取构建时间戳（assets/build_time.txt）。

设计约束（可重复构建）：
  * 目标条目必须是 STORED（不压缩）、固定 48 字节，否则无法定长替换；
  * 只改写数据区 48 字节 + 本地文件头 CRC + 中央目录 CRC 共 3 处，
    文件长度、条目偏移、对齐都保持不变；
  * 必须在 zipalign 之后、签名之前执行（签名覆盖整包）。

用法：
  apk_build_time_stamp.py set <apk> [--value "2026-11-24 12:00:00 +0000"]
  apk_build_time_stamp.py get <apk>
"""

import argparse
import datetime
import struct
import sys
import zlib
from pathlib import Path

ENTRY_NAME = b"assets/build_time.txt"
ENTRY_SIZE = 48


def _locate_eocd(data: bytes) -> int:
    pos = data.rfind(b"PK\x05\x06")
    if pos < 0:
        raise ValueError("未找到 EOCD，不是合法 ZIP/APK")
    return pos


def _find_entry(data: bytes):
    eocd = _locate_eocd(data)
    cd_off = struct.unpack_from("<I", data, eocd + 16)[0]
    p = cd_off
    while data[p:p + 4] == b"PK\x01\x02":
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, p + 28)
        name = bytes(data[p + 46:p + 46 + name_len])
        if name == ENTRY_NAME:
            compress_type = struct.unpack_from("<H", data, p + 10)[0]
            comp_size, uncomp_size = struct.unpack_from("<II", data, p + 20)
            local_off = struct.unpack_from("<I", data, p + 42)[0]
            return {
                "cd_off": p,
                "local_off": local_off,
                "compress_type": compress_type,
                "comp_size": comp_size,
                "uncomp_size": uncomp_size,
            }
        p += 46 + name_len + extra_len + comment_len
    raise ValueError(f"APK 内未找到 {ENTRY_NAME.decode()}（占位文件必须随源码打包）")


def _data_offset(data: bytes, entry) -> int:
    local_off = entry["local_off"]
    if data[local_off:local_off + 4] != b"PK\x03\x04":
        raise ValueError("本地文件头签名不对")
    flags = struct.unpack_from("<H", data, local_off + 6)[0]
    if flags & 0x08:
        raise ValueError("条目带 data descriptor，无法定长就地改写")
    name_len, extra_len = struct.unpack_from("<HH", data, local_off + 26)
    return local_off + 30 + name_len + extra_len


def _format_value(raw: str) -> bytes:
    value = raw.strip()
    if not value:
        raise ValueError("时间戳不能为空")
    encoded = value.encode("utf-8")
    if len(encoded) > ENTRY_SIZE:
        raise ValueError(f"时间戳过长（{len(encoded)} > {ENTRY_SIZE} 字节）")
    return encoded + b" " * (ENTRY_SIZE - len(encoded))


def cmd_get(apk: Path) -> int:
    data = apk.read_bytes()
    entry = _find_entry(data)
    offset = _data_offset(data, entry)
    value = data[offset:offset + entry["uncomp_size"]].decode("utf-8", "replace")
    print(f"apk={apk}")
    print(f"entry_off={offset} size={entry['uncomp_size']} compress_type={entry['compress_type']}")
    print(f"value={value.strip()!r}")
    return 0


def cmd_set(apk: Path, value: str | None) -> int:
    data = bytearray(apk.read_bytes())
    entry = _find_entry(data)
    if entry["compress_type"] != 0:
        raise ValueError("assets/build_time.txt 必须是 STORED 条目（repack 阶段强制设置）")
    if entry["uncomp_size"] != ENTRY_SIZE or entry["comp_size"] != ENTRY_SIZE:
        raise ValueError(
            f"占位文件长度必须是 {ENTRY_SIZE} 字节，实际 uncomp={entry['uncomp_size']} comp={entry['comp_size']}"
        )

    if value is None:
        value = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    payload = _format_value(value)

    offset = _data_offset(data, entry)
    data[offset:offset + ENTRY_SIZE] = payload
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    struct.pack_into("<I", data, entry["local_off"] + 14, crc)
    struct.pack_into("<I", data, entry["cd_off"] + 16, crc)
    apk.write_bytes(data)

    print(f"[+] 已写入构建时间戳: {payload.decode('utf-8').strip()}")
    print(f"    entry_off={offset} crc=0x{crc:08x}（本地头与中央目录已同步）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="就地读写 APK 内构建时间戳")
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser("set", help="写入时间戳")
    set_parser.add_argument("apk", type=Path)
    set_parser.add_argument("--value", help="固定时间戳（可重复性测试用）；缺省取当前本地时间")
    get_parser = sub.add_parser("get", help="读取时间戳")
    get_parser.add_argument("apk", type=Path)
    args = parser.parse_args()

    if not args.apk.is_file():
        raise SystemExit(f"APK 不存在: {args.apk}")
    if args.command == "get":
        return cmd_get(args.apk)
    return cmd_set(args.apk, args.value)


if __name__ == "__main__":
    sys.exit(main())
