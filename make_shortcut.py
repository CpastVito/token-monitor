#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
创建 .lnk 快捷方式（纯文件格式，不使用 COM）
===========================================

Windows 的 Shell Link 是公开的二进制格式。这里按规范拼一个最小可用的
快捷方式：目标程序 + 命令行参数 + 工作目录 + 图标。

用法：
    python make_shortcut.py <快捷方式路径.lnk> <目标exe> [参数] [工作目录] [图标]
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

HEADER_SIZE = 0x4C
CLSID = bytes([
    0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46,
])

# LinkFlags
HAS_LINK_INFO = 0x00000002
HAS_NAME = 0x00000004
HAS_WORKING_DIR = 0x00000010
HAS_ARGUMENTS = 0x00000020
HAS_ICON_LOCATION = 0x00000040
IS_UNICODE = 0x00000080

FILE_ATTRIBUTE_ARCHIVE = 0x00000020
SW_SHOWNORMAL = 1


def _string_data(text: str) -> bytes:
    """Unicode 字符串数据块：2 字节字符数 + UTF-16LE（无结尾 0）。"""
    raw = text.encode("utf-16-le")
    return struct.pack("<H", len(raw) // 2) + raw


def build_lnk(target: str, arguments: str = "", workdir: str = "",
              icon: str = "", icon_index: int = 0, name: str = "") -> bytes:
    flags = HAS_LINK_INFO | IS_UNICODE
    if workdir:
        flags |= HAS_WORKING_DIR
    if arguments:
        flags |= HAS_ARGUMENTS
    if icon:
        flags |= HAS_ICON_LOCATION
    if name:
        flags |= HAS_NAME

    # ---------------- ShellLinkHeader（76 字节）
    header = struct.pack(
        "<I", HEADER_SIZE
    ) + CLSID + struct.pack(
        "<I", flags
    ) + struct.pack(
        "<I", FILE_ATTRIBUTE_ARCHIVE
    ) + struct.pack("<Q", 0)          # CreationTime
    header += struct.pack("<Q", 0)     # AccessTime
    header += struct.pack("<Q", 0)     # WriteTime
    header += struct.pack("<I", 0)     # FileSize
    header += struct.pack("<i", icon_index)
    header += struct.pack("<I", SW_SHOWNORMAL)
    header += struct.pack("<H", 0)     # HotKey
    header += struct.pack("<H", 0)     # Reserved1
    header += struct.pack("<I", 0)     # Reserved2
    header += struct.pack("<I", 0)     # Reserved3
    assert len(header) == HEADER_SIZE, len(header)

    # ---------------- LinkInfo（含 LocalBasePath）
    base_path = str(Path(target).resolve()).encode("mbcs", "replace") + b"\x00"
    suffix = b"\x00"                                # CommonPathSuffix 为空
    volume_id = struct.pack("<I", 17) + struct.pack("<I", 3) + \
        struct.pack("<I", 0) + struct.pack("<I", 0x10) + b"\x00"
    assert len(volume_id) == 17

    link_info_size = 0x1C + len(volume_id) + len(base_path) + len(suffix)
    volume_off = 0x1C
    base_off = volume_off + len(volume_id)
    suffix_off = base_off + len(base_path)

    link_info = struct.pack("<I", link_info_size)
    link_info += struct.pack("<I", 0x1C)            # LinkInfoHeaderSize
    link_info += struct.pack("<I", 0x01)            # VolumeIDAndLocalBasePath
    link_info += struct.pack("<I", volume_off)
    link_info += struct.pack("<I", base_off)
    link_info += struct.pack("<I", 0)               # CommonNetworkRelativeLink
    link_info += struct.pack("<I", suffix_off)
    link_info += volume_id + base_path + suffix
    assert len(link_info) == link_info_size, (len(link_info), link_info_size)

    # ---------------- StringData（顺序必须与标志位顺序一致）
    strings = b""
    if name:
        strings += _string_data(name)
    if workdir:
        strings += _string_data(str(Path(workdir).resolve()))
    if arguments:
        strings += _string_data(arguments)
    if icon:
        strings += _string_data(str(Path(icon).resolve()))

    # ---------------- ExtraData 终止块
    return header + link_info + strings + struct.pack("<I", 0)


def make(dest: Path, target: str, arguments: str = "", workdir: str = "",
         icon: str = "", name: str = "") -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(build_lnk(target, arguments, workdir, icon, 0, name))
    return dest


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 2:
        print("用法: make_shortcut.py <dest.lnk> <target> [args] [workdir] [icon]")
        raise SystemExit(2)
    dest, target = a[0], a[1]
    args = a[2] if len(a) > 2 else ""
    wd = a[3] if len(a) > 3 else ""
    ic = a[4] if len(a) > 4 else ""
    p = make(Path(dest), target, args, wd, ic)
    print(f"created {p} ({p.stat().st_size} bytes)")
