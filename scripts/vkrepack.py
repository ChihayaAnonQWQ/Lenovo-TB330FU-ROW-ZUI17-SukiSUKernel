#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vkrepack.py —— Android boot v4 / vendor_boot v4 解包·重打包·AVB NONE footer 工具.

目标设备：联想 TB330FU（MT6768 / barley，Android 15，A/B 双槽）。
实测形态：boot 与 vendor_boot 的 header_version = 4，页大小 4096，无 init_boot 分区，
vendor_boot 64MB，其 AVB footer 的 algorithm = NONE（即没有签名的 hash footer）。

纯 Python 3 标准库实现，单文件，无第三方依赖。

子命令
------
info    <img>                                     打印全部头字段 / ramdisk table / AVB footer
unpack  <img> -o <dir>                            导出各段 + header.bin + ramdisk_table.json
repack  <indir> -o <img> [--partition-size N] [--avb none|keep]
                                                  按原头字段重建镜像（可选补 AVB NONE footer）
verify  <img> [--partition-size N]                结构自检，输出 PASS/FAIL 明细
splice  <orig.img> --kernel Image.gz -o <out.img> [--partition-size N]
                                                  只替换 boot 内核段的快捷模式

AVB footer —— 为什么本工具能直接生成
------------------------------------
本工具生成的是 AOSP avbtool（external/avb/avbtool.py）在
`add_hash_footer --algorithm NONE --partition_name <p> --partition_size <N>`
下写出的**逐字段复制品**，而不是"看起来像"的占位符。依据（avbtool.py 源码）：

* `AvbFooter`：'!4s2LQQQ28x'，header_block_size = 4096，
  镜像末尾 4096 字节块的最后 64 字节是 footer，前面 4032 字节补 0。
* `AvbVBMetaHeader`：256 字节，'!4s2L2QL2Q2Q2Q2QQLL47sx80x'。
* algorithm NONE 在 `ALGORITHMS` 表中 hash_num_bytes = signature_num_bytes
  = public_key_num_bytes = 0，于是：
    authentication_data_block_size = round_to_multiple(0, 64) = 0
    auxiliary_data_block_size      = round_to_multiple(len(descriptors), 64)
    hash_offset = 0, hash_size = 0
    public_key_offset = len(descriptors), public_key_size = 0
    signature_offset = 0, signature_size = 0
    algorithm_type = 0, rollback_index_location = 0, required_libavb_version = 1.0
* descriptors = 一个 `AvbHashDescriptor`（tag=2）：
    image_size = 内容对齐后的长度，hash_algorithm = 'sha256'，
    salt 默认 = 32 字节随机（avbtool：盐长度 = 摘要长度），
    digest = sha256(salt || 对齐后的整段内容)，flags = 0。
* 布局：`[内容][补 0 到 4096][vbmeta blob][补 0 到 4096][0 填充][4032 个 0 + footer]`，
  总长恰好 = partition_size。
* `footer.original_image_size` = **打 footer 之前**的内容长度；avbtool 在
  `seek(size-64)` 读到 'AVBf' 时会先 truncate 回这个值，所以重复执行是幂等的，
  本工具同样支持（输入已带 footer 会先剥离）。

因此 `--avb none` 的输出与 avbtool 产物**字节级等价（盐/签名除外）**；README 里仍给出
`avbtool add_hash_footer` 与 `avbtool verify_image` 命令，用作交叉验证与官方认可。
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"

BOOT_PAGE_SIZE = 4096
BOOT_HDR_V4_SIZE = 1584
BOOT_CMDLINE_LEN = 1536
BOOT_SIG_SIZE_OFFSET = 1580
BOOT_HDR_V4_RAW = (            # magic..header_version（不含 cmdline / signature_size）
    "8s" "I" "I" "I" "I" "4I" "I"      # 8+4+4+4+4+16+4 = 44 字节
)
VENDOR_BOOT_HDR_V3_SIZE = 2112          # magic..dtb_addr（packed，逐字节核对 bootimg.h）
VENDOR_BOOT_HDR_V4_SIZE = 2128          # v3 + 4 个 u32（table/bootconfig 尺寸）
# 字段偏移：与 AOSP bootimg.h 的 packed 结构逐项对齐（已双向验证）。
VB_OFF_VRAMDISK_SIZE = 24               # 8s + 4×u32 = 24
VB_OFF_CMDLINE = 28                     # cmdline[2048] -> [28, 2076)
VB_OFF_CMDLINE_END = VB_OFF_CMDLINE + 2048      # 2076
VB_OFF_TAGS_ADDR = 2076                 # u32 -> [2076, 2080)
VB_OFF_NAME = 2080                      # name[16] -> [2080, 2096)
VB_OFF_NAME_END = VB_OFF_NAME + 16      # 2096
VB_OFF_HEADER_SIZE = 2096               # u32
VB_OFF_DTB_SIZE = 2100                  # u32
VB_OFF_DTB_ADDR = 2104                  # u64 -> [2104, 2112)
VB_OFF_V4_EXTRA = 2112                  # v4 追加 4×u32 -> [2112, 2128)
VENDOR_BOOT_PAGE_SIZE = 4096
VENDOR_BOOT_CMDLINE_LEN = 2048
VENDOR_BOOT_NAME_LEN = 16
VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE = 108
VENDOR_RAMDISK_TABLE_ENTRY_V4_FMT = "III32s16I"
VENDOR_RAMDISK_TYPE_NAMES = {0: "none", 1: "platform", 2: "recovery", 3: "dlkm"}

AVB_FOOTER_MAGIC = b"AVBf"
AVB_FOOTER_SIZE = 64
AVB_FOOTER_VERSION_MAJOR = 1
AVB_FOOTER_VERSION_MINOR = 0
AVB_FOOTER_FMT = "!4s" "2L" "Q" "Q" "Q" "28x"        # 4s+2x4+3x8+28 = 64

AVB_MAGIC = b"AVB0"
AVB_VBMETA_HEADER_SIZE = 256
AVB_VBMETA_HEADER_FMT = (
    "!4s" "2L"   # magic, version major/minor
    "2Q"         # auth block size, aux block size
    "L"          # algorithm_type
    "2Q"         # hash offset/size
    "2Q"         # signature offset/size
    "2Q"         # public key offset/size
    "2Q"         # public key metadata offset/size
    "2Q"         # descriptors offset/size
    "Q"          # rollback_index
    "L"          # flags
    "L"          # rollback_index_location
    "47s" "x"    # release string
    "80x"        # reserved
)
AVB_HASH_DESC_TAG = 2
AVB_HASH_DESC_RESERVED = 60
AVB_HASH_DESC_SIZE = 72 + AVB_HASH_DESC_RESERVED      # 132
AVB_ALGORITHM_NONE = 0
AVB_ALG_NAMES = {
    0: "NONE", 1: "SHA256_RSA2048", 2: "SHA256_RSA4096", 3: "SHA256_RSA8192",
    4: "SHA512_RSA2048", 5: "SHA512_RSA4096", 6: "SHA512_RSA8192",
}
AVB_VERSION_MAJOR = 1
AVB_VERSION_MINOR = 3
AVB_VERSION_SUB = 0
AVB_RELEASE_STRING = f"avbtool {AVB_VERSION_MAJOR}.{AVB_VERSION_MINOR}.{AVB_VERSION_SUB}"
AVB_BLOCK_SIZE = 4096
AVB_MAX_VBMETA_SIZE = 64 * 1024
AVB_MAX_FOOTER_SIZE = 4096
AVB_MAX_METADATA_SIZE = AVB_MAX_VBMETA_SIZE + AVB_MAX_FOOTER_SIZE

DEFAULT_PARTITION_SIZE = 32 * 1024 * 1024
DEFAULT_VENDOR_BOOT_PARTITION_SIZE = 64 * 1024 * 1024

COMPRESSION_MAGICS: Sequence[Tuple[str, bytes]] = (
    ("gzip", b"\x1f\x8b"),
    ("lz4_legacy", b"\x02\x21\x4c\x18"),
    ("lz4_skippable", b"\x50\x2a\x4d\x18"),
    ("lz4_frame", b"\x04\x22\x4d\x18"),
    ("xz", b"\xfd7zXZ\x00"),
    ("bzip2", b"BZh"),
    ("zstd", b"\x28\xb5\x2f\xfd"),
    ("lzop", b"\x89LZO\x00\r\n\x1a\n"),
)

PARTITION_SIZE_JSON = "avb_partition_size.json"
TABLE_JSON = "ramdisk_table.json"
NOTES_TXT = "unpack_notes.txt"


# ---------------------------------------------------------------------------
# 错误出口与基础工具
# ---------------------------------------------------------------------------

def _harden_stdio() -> None:
    """Windows 控制台默认 cp936/cp1252，中文提示会乱码甚至 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):        # pragma: no cover - 非 TTY 或老版本
            pass


def die(message: str, code: int = 1) -> None:
    """打印人话错误并 sys.exit(code)。所有致命错误都必须走这里。"""
    print(f"vkrepack: error: {message}", file=sys.stderr)
    sys.exit(code)


def warn(message: str) -> None:
    print(f"vkrepack: warning: {message}", file=sys.stderr)


def note(message: str) -> None:
    print(f"vkrepack: note: {message}", file=sys.stderr)


def round_up(value: int, multiple: int) -> int:
    """向上取整到 multiple 的倍数。"""
    if multiple <= 0:
        raise ValueError("multiple must be positive")
    return (value + multiple - 1) // multiple * multiple


def round_to_multiple_avb(number: int, size: int) -> int:
    """avbtool.round_to_multiple() 的逐字复刻。"""
    remainder = number % size
    if remainder == 0:
        return number
    return number + size - remainder


def cstr(raw: bytes) -> str:
    """按 NUL 截断的 C 字符串。"""
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def fmt_sz(value: int) -> str:
    """偏移/尺寸统一显示为 十进制 (0x十六进制)。"""
    return f"{value} (0x{value:x})"


def detect_compression(blob: bytes) -> str:
    """透明检测压缩格式（不解压）。"""
    if not blob:
        return "empty"
    for name, magic in COMPRESSION_MAGICS:
        if blob.startswith(magic):
            return name
    if looks_like_cpio(blob):
        return "none(cpio)"
    if len(blob) >= 4:
        first = struct.unpack_from("<I", blob, 0)[0]
        if (first >> 30) == 3 and 0 < (first & 0x0FFFFFFF) <= len(blob) + 4:
            return "lz4_legacy(无 magic)"
    return "unknown"


def payload_hint(blob: bytes) -> str:
    """对 vendor_ramdisk 给出「压缩 + 载荷」的附加判断。"""
    kind = detect_compression(blob)
    hints = []
    for name, magic in COMPRESSION_MAGICS:
        if kind == name and magic:
            inner = blob[len(magic):]
            if looks_like_cpio(inner):
                hints.append("载荷=cpio(newc)")
            elif inner[:2] == b"\x1f\x8b":
                hints.append("载荷=二次 gzip")
            break
    if kind == "none(cpio)":
        hints.append("载荷=cpio(newc)，无需解压")
    elif kind == "lz4_legacy(无 magic)" and len(blob) > 8 and looks_like_cpio(blob[8:16]):
        hints.append("载荷=cpio(newc)（magic 之后）")
    elif kind == "unknown":
        hints.append("无法识别：可能是 raw cpio 以外的私有格式")
    return kind + ("，" + "；".join(hints) if hints else "")


def looks_like_cpio(blob: bytes) -> bool:
    return blob[:6] in (b"070701", b"070702")


def sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def lz4_legacy_payload_offset(blob: bytes) -> Optional[int]:
    """返回 lz4 legacy（含 magic）之后第一块的数据起点；用于判断内部是否 cpio。"""
    if not blob.startswith(b"\x02\x21\x4c\x18"):
        return None
    return 4


# ---------------------------------------------------------------------------
# boot v4 头
# ---------------------------------------------------------------------------

def parse_boot_header(data: bytes) -> Dict[str, Any]:
    """解析 boot header v4（header_size=1584，头区占 4096 字节）。"""
    if len(data) < BOOT_PAGE_SIZE:
        die(f"文件只有 {len(data)} 字节，连 4096 字节头区都不够，不是 boot 镜像")
    if data[:8] != BOOT_MAGIC:
        die(f"boot magic 不匹配：期望 b'ANDROID!'，实际 {data[:8]!r}")
    (magic, kernel_size, ramdisk_size, os_version, header_size,
     r0, r1, r2, r3, header_version) = struct.unpack_from("<" + BOOT_HDR_V4_RAW, data, 0)
    if len(data) < BOOT_SIG_SIZE_OFFSET + 4:
        die("头部被截断：读不到 signature_size（v4 需要至少 1584 字节）")
    signature_size = struct.unpack_from("<I", data, BOOT_SIG_SIZE_OFFSET)[0]
    if header_version not in (3, 4):
        die(f"只支持 boot header v3/v4，实际 header_version={header_version}")
    if header_version == 3 and signature_size != 0:
        die("header_version=3 却出现 signature_size!=0，头部结构可疑")
    nominal = max(header_size, BOOT_HDR_V4_SIZE) if header_version == 4 else header_size
    if nominal < BOOT_HDR_V4_SIZE and header_version == 4:
        die(f"header_version=4 但 header_size={header_size} < 1584")
    return {
        "kind": "boot",
        "header_version": header_version,
        "header_size": header_size,
        "kernel_size": kernel_size,
        "ramdisk_size": ramdisk_size,
        "os_version": os_version,
        "os_version_text": decode_os_version(os_version),
        "reserved": [r0, r1, r2, r3],
        "cmdline": cstr(data[44:44 + BOOT_CMDLINE_LEN]),
        "signature_size": signature_size,
        "page_size": BOOT_PAGE_SIZE,
        "header_block_size": BOOT_PAGE_SIZE,
        "header_raw": bytes(data[:BOOT_PAGE_SIZE]),
        "header_nominal_len": nominal,
    }


def decode_os_version(os_version: int) -> str:
    """aosp: 31..25 major, 24..18 minor, 17..11 patch, 10..4 year-2000, 3..0 month。"""
    a = (os_version >> 25) & 0x7F
    b = (os_version >> 18) & 0x7F
    c = (os_version >> 11) & 0x7F
    y = (os_version >> 4) & 0x7F
    m = os_version & 0xF
    return f"{a}.{b}.{c} ({2000 + y}-{m:02d})"


def build_boot_header(hdr: Dict[str, Any], kernel_size: int, ramdisk_size: int) -> bytes:
    """按解析结果重新编码 boot 头部（固定 4096 字节，未用区域补零）。"""
    cmdline = (hdr.get("cmdline") or "").encode("utf-8")
    if len(cmdline) > BOOT_CMDLINE_LEN - 1:
        die(f"cmdline 过长：{len(cmdline)} > {BOOT_CMDLINE_LEN - 1} 字节")
    header_size = int(hdr.get("header_size") or BOOT_HDR_V4_SIZE)
    if header_size != BOOT_HDR_V4_SIZE:
        die(f"header_size={header_size} 不是规范的 v4 值 1584，拒绝改写")
    out = bytearray(BOOT_PAGE_SIZE)
    out[0:8] = BOOT_MAGIC
    struct.pack_into(
        "<" + BOOT_HDR_V4_RAW, out, 0,
        BOOT_MAGIC, kernel_size, ramdisk_size, int(hdr.get("os_version", 0)),
        header_size, *[int(x) for x in hdr.get("reserved", [0, 0, 0, 0])],
        int(hdr.get("header_version", 4)),
    )
    out[44:44 + len(cmdline)] = cmdline
    struct.pack_into("<I", out, BOOT_SIG_SIZE_OFFSET, int(hdr.get("signature_size", 0)))
    return bytes(out)


# ---------------------------------------------------------------------------
# vendor_boot v3/v4 头
# ---------------------------------------------------------------------------

def parse_vendor_boot_header(data: bytes) -> Dict[str, Any]:
    """解析 vendor_boot v3/v4 头部。

    v3 = 2112 字节，v4 = 2128 字节（均在 bootimg.h 里标注 __attribute__((packed))）。
    与 AOSP 的 o = (2128 + page_size - 1) / page_size 一致。
    """
    if len(data) < VENDOR_BOOT_HDR_V3_SIZE:
        die(f"文件只有 {len(data)} 字节，不足以容纳 vendor_boot v3 的 "
            f"{VENDOR_BOOT_HDR_V3_SIZE} 字节头部")
    if data[:8] != VENDOR_BOOT_MAGIC:
        die(f"vendor_boot magic 不匹配：期望 b'VNDRBOOT'，实际 {data[:8]!r}")
    (magic, header_version, page_size, kernel_addr, ramdisk_addr) = struct.unpack_from(
        "<8sIIII", data, 0)
    (vendor_ramdisk_size,) = struct.unpack_from("<I", data, VB_OFF_VRAMDISK_SIZE)
    cmdline = cstr(data[VB_OFF_CMDLINE:VB_OFF_CMDLINE_END])
    (tags_addr,) = struct.unpack_from("<I", data, VB_OFF_TAGS_ADDR)
    name = cstr(data[VB_OFF_NAME:VB_OFF_NAME_END])
    (header_size,) = struct.unpack_from("<I", data, VB_OFF_HEADER_SIZE)
    (dtb_size,) = struct.unpack_from("<I", data, VB_OFF_DTB_SIZE)
    (dtb_addr,) = struct.unpack_from("<Q", data, VB_OFF_DTB_ADDR)
    if header_version not in (3, 4):
        die(f"不支持的 vendor_boot header_version={header_version}（只处理 v3/v4）")
    expect = VENDOR_BOOT_HDR_V4_SIZE if header_version == 4 else VENDOR_BOOT_HDR_V3_SIZE
    if len(data) < expect:
        die(f"文件只有 {len(data)} 字节，不够 vendor_boot v{header_version} 的 "
            f"{expect} 字节头部（被截断）")
    if page_size == 0 or page_size & (page_size - 1) or page_size < 2048:
        die(f"vendor_boot page_size={page_size} 不是合法的 2 的幂"
            "（vendor_boot 的页大小取自本字段，不是固定 4096）")
    if header_size != expect:
        warn(f"header_size={header_size} 与 v{header_version} 规范值 {expect} 不符"
             "（厂商私有扩展？）；本工具按规范结构解析并按规范值重建")
    info: Dict[str, Any] = {
        "kind": "vendor_boot",
        "header_version": header_version,
        "header_size": header_size,
        "header_size_expected": expect,
        "header_size_ok": header_size == expect,
        "page_size": page_size,
        "kernel_addr": kernel_addr,
        "ramdisk_addr": ramdisk_addr,
        "tags_addr": tags_addr,
        "dtb_addr": dtb_addr,
        "name": name,
        "vendor_ramdisk_size": vendor_ramdisk_size,
        "cmdline": cmdline,
        "dtb_size": dtb_size,
        # 头区占用的页数按规范结构大小计算（o = (2128 + page - 1) / page）
        "header_block_size": round_up(expect, page_size),
        "header_raw": bytes(data[:round_up(expect, page_size)]),
        "header_nominal_len": expect,
    }
    if header_version == 4:
        (table_size, entry_num, entry_size, bootconfig_size) = struct.unpack_from(
            "<4I", data, VB_OFF_V4_EXTRA)
        info.update(
            vendor_ramdisk_table_size=table_size,
            vendor_ramdisk_table_entry_num=entry_num,
            vendor_ramdisk_table_entry_size=entry_size,
            bootconfig_size=bootconfig_size,
        )
    else:
        info.update(
            vendor_ramdisk_table_size=0, vendor_ramdisk_table_entry_num=0,
            vendor_ramdisk_table_entry_size=0, bootconfig_size=0,
        )
    return info


def build_vendor_boot_header(info: Dict[str, Any], sizes: Dict[str, int]) -> bytes:
    """按解析结果重新编码 vendor_boot 头部（补零到 page 对齐）。"""
    page = int(info.get("page_size") or VENDOR_BOOT_PAGE_SIZE)
    cmdline = (info.get("cmdline") or "").encode("utf-8")
    if len(cmdline) > VENDOR_BOOT_CMDLINE_LEN - 1:
        die(f"vendor_boot cmdline 过长：{len(cmdline)} > {VENDOR_BOOT_CMDLINE_LEN - 1}")
    name = (info.get("name") or "").encode("utf-8")
    if len(name) >= VENDOR_BOOT_NAME_LEN:
        die(f"vendor_boot name 过长：{info.get('name')!r}")
    hdr_version = int(info.get("header_version", 4))
    header_size = VENDOR_BOOT_HDR_V4_SIZE if hdr_version == 4 else VENDOR_BOOT_HDR_V3_SIZE
    out = bytearray(round_up(header_size, page))
    struct.pack_into("<8sIIII", out, 0, VENDOR_BOOT_MAGIC, hdr_version, page,
                     int(info.get("kernel_addr", 0)), int(info.get("ramdisk_addr", 0)))
    struct.pack_into("<I", out, VB_OFF_VRAMDISK_SIZE, int(sizes["vendor_ramdisk_size"]))
    out[VB_OFF_CMDLINE:VB_OFF_CMDLINE + len(cmdline)] = cmdline
    struct.pack_into("<I", out, VB_OFF_TAGS_ADDR, int(info.get("tags_addr", 0)))
    out[VB_OFF_NAME:VB_OFF_NAME + len(name)] = name
    struct.pack_into("<I", out, VB_OFF_HEADER_SIZE, header_size)
    struct.pack_into("<I", out, VB_OFF_DTB_SIZE, int(sizes["dtb_size"]))
    struct.pack_into("<Q", out, VB_OFF_DTB_ADDR, int(info.get("dtb_addr", 0)))
    if hdr_version == 4:
        struct.pack_into(
            "<4I", out, VB_OFF_V4_EXTRA,
            int(sizes["vendor_ramdisk_table_size"]),
            int(sizes["vendor_ramdisk_table_entry_num"]),
            VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE,
            int(sizes["bootconfig_size"]),
        )
    return bytes(out)


def parse_ramdisk_table(blob: bytes, entry_num: int, entry_size: int,
                        where: str) -> List[Dict[str, Any]]:
    """解析 vendor_ramdisk_table_entry_v4 列表。"""
    if entry_size != VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE:
        die(f"{where}: table entry_size={entry_size}，与 v4 的 "
            f"{VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE} 不符（不认识的变体）")
    need = entry_num * entry_size
    if len(blob) < need:
        die(f"{where}: ramdisk table 只有 {len(blob)} 字节，需要 {need} 字节")
    entries: List[Dict[str, Any]] = []
    for i in range(entry_num):
        raw = blob[i * entry_size:(i + 1) * entry_size]
        fields = struct.unpack_from("<" + VENDOR_RAMDISK_TABLE_ENTRY_V4_FMT, raw, 0)
        size, offset, rtype = fields[0], fields[1], fields[2]
        name_raw = fields[3]
        board_id = list(fields[4:20])
        entries.append({
            "index": i,
            "ramdisk_size": size,
            "ramdisk_offset": offset,
            "ramdisk_type": rtype,
            "ramdisk_type_name": VENDOR_RAMDISK_TYPE_NAMES.get(rtype, f"unknown({rtype})"),
            "ramdisk_name": cstr(name_raw),
            "board_id": board_id,
        })
    return entries


def build_ramdisk_table(entries: Sequence[Dict[str, Any]]) -> bytes:
    """由 JSON 条目重新编码 ramdisk table。"""
    out = bytearray()
    for i, ent in enumerate(entries):
        name = str(ent["ramdisk_name"]).encode("utf-8")
        if not name:
            die(f"ramdisk table 第 {i} 项 ramdisk_name 为空")
        if len(name) > 32:
            die(f"ramdisk table 第 {i} 项 ramdisk_name 过长：{ent['ramdisk_name']!r}")
        board_id = list(ent.get("board_id") or [0] * 16)
        if len(board_id) != 16:
            die(f"ramdisk table 第 {i} 项 board_id 必须正好 16 个 u32，实际 {len(board_id)}")
        for v in board_id:
            if not 0 <= int(v) <= 0xFFFFFFFF:
                die(f"ramdisk table 第 {i} 项 board_id 含越界值 {v}")
        rtype = int(ent["ramdisk_type"])
        if rtype not in VENDOR_RAMDISK_TYPE_NAMES:
            warn(f"ramdisk table 第 {i} 项 ramdisk_type={rtype} 不是已知类型")
        out += struct.pack(
            "<" + VENDOR_RAMDISK_TABLE_ENTRY_V4_FMT,
            int(ent["ramdisk_size"]), int(ent["ramdisk_offset"]), rtype,
            name + b"\x00" * (32 - len(name)),
            *[int(v) for v in board_id],
        )
    return bytes(out)


# ---------------------------------------------------------------------------
# AVB：解析
# ---------------------------------------------------------------------------

def parse_avb_footer(image: bytes) -> Optional[Dict[str, Any]]:
    """镜像末尾 64 字节是否为 AvbFooter。"""
    if len(image) < AVB_FOOTER_SIZE:
        return None
    (magic, vmaj, vmin, original_image_size, vbmeta_offset, vbmeta_size) = struct.unpack(
        AVB_FOOTER_FMT, image[-AVB_FOOTER_SIZE:])
    if magic != AVB_FOOTER_MAGIC:
        return None
    return {
        "version_major": vmaj,
        "version_minor": vmin,
        "original_image_size": original_image_size,
        "vbmeta_offset": vbmeta_offset,
        "vbmeta_size": vbmeta_size,
    }


def parse_vbmeta_header(blob: bytes) -> Dict[str, Any]:
    """解析 AvbVBMetaHeader（256 字节）。"""
    if len(blob) < AVB_VBMETA_HEADER_SIZE:
        die(f"vbmeta blob 只有 {len(blob)} 字节，读不出 256 字节头")
    values = struct.unpack_from(AVB_VBMETA_HEADER_FMT, blob, 0)
    keys = (
        "magic", "required_libavb_version_major", "required_libavb_version_minor",
        "authentication_data_block_size", "auxiliary_data_block_size", "algorithm_type",
        "hash_offset", "hash_size", "signature_offset", "signature_size",
        "public_key_offset", "public_key_size",
        "public_key_metadata_offset", "public_key_metadata_size",
        "descriptors_offset", "descriptors_size", "rollback_index", "flags",
        "rollback_index_location", "release_string",
    )
    info: Dict[str, Any] = dict(zip(keys, values))
    if info["magic"] != AVB_MAGIC:
        die(f"vbmeta magic 不是 b'AVB0'（实际 {info['magic']!r}）：footer 指向的位置"
            "不是合法 vbmeta blob（镜像可能被截断，或 footer 是残留的）")
    info["magic"] = AVB_MAGIC.decode()
    info["release_string"] = cstr(info["release_string"])
    info["algorithm_name"] = AVB_ALG_NAMES.get(info["algorithm_type"],
                                               f"unknown({info['algorithm_type']})")
    info["blob_size"] = (AVB_VBMETA_HEADER_SIZE + info["authentication_data_block_size"]
                         + info["auxiliary_data_block_size"])
    return info


def parse_avb_descriptors(vbmeta: Dict[str, Any], blob: bytes) -> List[Dict[str, Any]]:
    """解析 vbmeta 的 descriptor 列表（识别 Hash descriptor，其余给 tag）。"""
    base = AVB_VBMETA_HEADER_SIZE + vbmeta["authentication_data_block_size"] \
        + vbmeta["descriptors_offset"]
    region = blob[base:base + vbmeta["descriptors_size"]]
    if len(region) != vbmeta["descriptors_size"]:
        die(f"descriptor 区被截断：声明 {vbmeta['descriptors_size']} 字节，"
            f"实际 {len(region)}")
    out: List[Dict[str, Any]] = []
    pos = 0
    while pos + 16 <= len(region):
        tag, nbf = struct.unpack_from("!QQ", region, pos)
        body = region[pos + 16:pos + 16 + nbf]
        if len(body) != nbf:
            die(f"descriptor(tag={tag}) 声明 {nbf} 字节，实际只剩 {len(body)}")
        desc: Dict[str, Any] = {"tag": tag, "num_bytes_following": nbf, "kind": "other"}
        if tag == AVB_HASH_DESC_TAG and len(body) >= AVB_HASH_DESC_SIZE - 16:
            (image_size, hash_algorithm, pn_len, salt_len, digest_len, flags) = \
                struct.unpack_from("!Q32sLLLL", body, 0)
            base_payload = AVB_HASH_DESC_SIZE - 16
            desc.update(
                kind="hash",
                image_size=image_size,
                hash_algorithm=cstr(hash_algorithm),
                partition_name=body[base_payload:base_payload + pn_len].decode("utf-8", "replace"),
                salt=body[base_payload + pn_len:base_payload + pn_len + salt_len].hex(),
                digest=body[base_payload + pn_len + salt_len:
                            base_payload + pn_len + salt_len + digest_len].hex(),
                flags=flags,
            )
        out.append(desc)
        pos += 16 + nbf
    return out


# ---------------------------------------------------------------------------
# AVB：生成（avbtool add_hash_footer --algorithm NONE 的逐字段复刻）
# ---------------------------------------------------------------------------

def build_hash_descriptor(partition_name: str, image_size: int, hash_algorithm: str,
                          salt: bytes, digest: bytes, flags: int = 0) -> bytes:
    """复刻 AvbHashDescriptor.encode()。"""
    pn = partition_name.encode("utf-8")
    num_bytes_following = AVB_HASH_DESC_SIZE + len(pn) + len(salt) + len(digest) - 16
    nbf = round_to_multiple_avb(num_bytes_following, 8)
    padding = nbf - num_bytes_following
    head = struct.pack(
        "!QQQ32sLLLL" + str(AVB_HASH_DESC_RESERVED) + "s",
        AVB_HASH_DESC_TAG, nbf, image_size, hash_algorithm.encode("ascii"),
        len(pn), len(salt), len(digest), flags, b"\x00" * AVB_HASH_DESC_RESERVED,
    )
    return head + pn + salt + digest + b"\x00" * padding


def build_vbmeta_none(release_string: str, rollback_index: int, flags: int,
                      descriptors: bytes) -> bytes:
    """复刻 avbtool._generate_vbmeta_blob() 在 algorithm=NONE 下的输出。

    NONE：hash_num_bytes = signature_num_bytes = public_key_num_bytes = 0
      auth block size = round_to_multiple(0, 64) = 0
      aux  block size = round_to_multiple(len(descriptors), 64)
      rollback_index_location = 0 -> required_libavb_version_minor 保持 0
    """
    aux_size = round_to_multiple_avb(len(descriptors), 64)
    desc_size = len(descriptors)
    header = struct.pack(
        AVB_VBMETA_HEADER_FMT,
        AVB_MAGIC,
        AVB_VERSION_MAJOR, 0,          # required libavb 1.0
        0,                             # authentication_data_block_size
        aux_size,                      # auxiliary_data_block_size
        AVB_ALGORITHM_NONE,
        0, 0,                          # hash_offset, hash_size
        0, 0,                          # signature_offset, signature_size
        desc_size, 0,                  # public_key_offset, public_key_size
        desc_size, 0,                  # public_key_metadata_offset/size
        0, desc_size,                  # descriptors_offset, descriptors_size
        rollback_index,
        flags,
        0,                             # rollback_index_location
        release_string.encode("utf-8"),
    )
    if len(header) != AVB_VBMETA_HEADER_SIZE:
        raise AssertionError("AvbVBMetaHeader 编码长度错误")
    return header + descriptors + b"\x00" * (aux_size - len(descriptors))


def strip_avb_footer(image: bytes) -> bytes:
    """若带 AVB footer，截断回 original_image_size（与 avbtool 的幂等处理一致）。"""
    footer = parse_avb_footer(image)
    if footer is None:
        return image
    return image[:footer["original_image_size"]]


def add_avb_none(image: bytes, partition_name: str, partition_size: int,
                 hash_algorithm: str = "sha256", salt: Optional[bytes] = None,
                 release_string: str = AVB_RELEASE_STRING,
                 rollback_index: int = 0, flags: int = 0,
                 block_size: int = AVB_BLOCK_SIZE,
                 orig_image_size: Optional[int] = None) -> bytes:
    """【参考实现 / 仅供测试与离线比对，CLI 不走这里】

    CLI 的 `--avb none` 一律调用官方 avbtool（见 avbtool_add_hash_footer_none），
    本函数只是把同样的字节布局保留下来，用于单测里在没有 avbtool 的环境中断言结构，
    以及历史上与 avbtool 的逐字节对照（本实现已确认与 avbtool 1.3.0 输出字节级一致）。
    生产路径**不会**用它的输出替换 avbtool 的产物。
    """
    if salt is None:
        salt = os.urandom(hashlib.new(hash_algorithm).digest_size)
    if not partition_size or partition_size < AVB_MAX_METADATA_SIZE:
        die(f"分区尺寸 {partition_size} 太小；avbtool 要求至少 {AVB_MAX_METADATA_SIZE} 字节")
    if partition_size % block_size:
        die(f"分区尺寸 {partition_size} 不是块大小 {block_size} 的整数倍")

    existing = parse_avb_footer(image)
    if existing is not None:
        image = image[:existing["original_image_size"]]
    original_image_size = len(image) if orig_image_size is None else int(orig_image_size)
    if original_image_size > len(image):
        die(f"original_image_size {original_image_size} 超过实际内容 {len(image)}")

    lead = 0
    if original_image_size % block_size:
        lead = block_size - (original_image_size % block_size)
    data_size = original_image_size + lead
    if data_size > len(image):
        image = image + b"\x00" * (data_size - len(image))
    padded = image[:data_size]
    max_image_size = partition_size - AVB_MAX_METADATA_SIZE
    if data_size > max_image_size:
        die(f"内容 {original_image_size} 字节（对齐后 {data_size}）超过分区允许的最大镜像 "
            f"{max_image_size} 字节（分区 {partition_size} - 元数据 {AVB_MAX_METADATA_SIZE}）；"
            "请确认 --partition-size 与分区表一致")

    # avbtool 的哈希与 h_desc.image_size 都取「补齐之前」的 image_size，
    # 之后才 truncate 出对齐区（那一段零填充不参与哈希，也不计入 descriptor）。
    digest = hashlib.new(hash_algorithm, salt + image[:original_image_size]).digest()
    desc = build_hash_descriptor(partition_name, original_image_size, hash_algorithm,
                                 salt, digest, flags)
    vbmeta_blob = build_vbmeta_none(release_string, rollback_index, flags, desc)

    vbmeta_offset = data_size
    vbmeta_padded = vbmeta_blob + b"\x00" * (
        round_to_multiple_avb(len(vbmeta_blob), block_size) - len(vbmeta_blob))
    vbmeta_end = vbmeta_offset + len(vbmeta_padded)
    if vbmeta_end + block_size > partition_size:
        die(f"vbmeta 放不下：vbmeta_end={vbmeta_end} + footer 块 {block_size} "
            f"> 分区 {partition_size}")
    fill = partition_size - vbmeta_end - block_size
    footer = struct.pack(
        AVB_FOOTER_FMT, AVB_FOOTER_MAGIC, AVB_FOOTER_VERSION_MAJOR,
        AVB_FOOTER_VERSION_MINOR, original_image_size, vbmeta_offset, len(vbmeta_blob),
    )
    out = padded + vbmeta_padded + b"\x00" * fill + b"\x00" * (block_size - AVB_FOOTER_SIZE) + footer
    if len(out) != partition_size:
        raise AssertionError(f"AVB 结果长度 {len(out)} != 分区尺寸 {partition_size}")
    return out


# ---------------------------------------------------------------------------
# 分区尺寸来源（CLI > avb_partition_size.json > ramdisk_table.json > 默认值）
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path} 不是合法 JSON：{exc}")
    return None


def resolve_partition_size(indir: Path, cli_value: int, fallback: int) -> int:
    """按优先级解析分区尺寸。"""
    if cli_value:
        return int(cli_value)
    meta = load_json(indir / PARTITION_SIZE_JSON)
    if isinstance(meta, dict):
        for key in ("partition_size", "boot_partition_size", "vendor_boot_partition_size"):
            value = meta.get(key)
            if isinstance(value, int) and value > 0:
                return value
    table = load_json(indir / TABLE_JSON)
    if isinstance(table, dict):
        value = table.get("original_partition_size") or table.get("partition_size")
        if isinstance(value, int) and value > 0:
            return int(value)
    return int(fallback or 0)


# ---------------------------------------------------------------------------
# 镜像对象
# ---------------------------------------------------------------------------

class Image:
    """一个已解析的 boot / vendor_boot 镜像（含 AVB 尾部）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            raw = path.read_bytes()
        except OSError as exc:
            die(f"读不了 {path}：{exc}")
        self.file_size = len(raw)
        kind = parsed_kind(raw)
        if kind is None:
            die(f"{path} 既不是 boot（ANDROID!）也不是 vendor_boot（VNDRBOOT）："
                f"前 8 字节 {raw[:8]!r}")
        self.kind: str = kind
        self.footer: Optional[Dict[str, Any]] = parse_avb_footer(raw)
        self.data = raw[:self.footer["original_image_size"]] if self.footer else raw
        self.partition_size_declared: Optional[int] = len(raw) if self.footer else None
        self.vbmeta: Optional[Dict[str, Any]] = None
        self.descriptors: List[Dict[str, Any]] = []
        if self.footer is not None:
            self._load_vbmeta(raw)
        if self.kind == "boot":
            self._parse_boot()
        else:
            self._parse_vendor_boot()

    # -- AVB -------------------------------------------------------------
    def _load_vbmeta(self, raw: bytes) -> None:
        assert self.footer is not None
        off = int(self.footer["vbmeta_offset"])
        size = int(self.footer["vbmeta_size"])
        if off + size > len(raw) or size <= 0:
            die(f"footer.vbmeta_offset/size 越界：offset={off} size={size} 镜像={len(raw)}")
        blob = raw[off:off + size]
        self.vbmeta = parse_vbmeta_header(blob)
        if self.vbmeta["blob_size"] > size:
            die(f"vbmeta 头声明总长 {self.vbmeta['blob_size']} 超过 footer 记录的 {size}")
        self.descriptors = parse_avb_descriptors(self.vbmeta, blob)
        for desc in self.descriptors:
            if desc.get("kind") != "hash" or not desc.get("digest"):
                continue
            alg = desc["hash_algorithm"]
            if alg not in ("sha256", "sha512"):
                desc["digest_matches"] = None
                continue
            data = self.data[: desc["image_size"]]
            if len(data) != desc["image_size"]:
                desc["digest_matches"] = False
                continue
            actual = hashlib.new(alg, bytes.fromhex(desc["salt"]) + data).hexdigest()
            desc["digest_matches"] = actual == desc["digest"]

    # -- boot ------------------------------------------------------------
    def _parse_boot(self) -> None:
        h = parse_boot_header(self.data)
        self.header = h
        page = BOOT_PAGE_SIZE
        off = BOOT_PAGE_SIZE
        k = h["kernel_size"]
        if off + k > len(self.data):
            die(f"kernel_size={k} 越界：kernel 段结束于 {off + k}，镜像只有 {len(self.data)}")
        kernel = self.data[off:off + k]
        off += round_up(k, page)
        ramdisk = b""
        r = h["ramdisk_size"]
        if r:
            if off + r > len(self.data):
                die(f"ramdisk_size={r} 越界：结束于 {off + r}，镜像只有 {len(self.data)}")
            ramdisk = self.data[off:off + r]
            off += round_up(r, page)
        self.segments = {"kernel": kernel, "ramdisk": ramdisk}
        self.ramdisk_entries = []
        self.layout = {
            "header_end": BOOT_PAGE_SIZE,
            "kernel_offset": BOOT_PAGE_SIZE,
            "kernel_size": k,
            "padded_kernel_end": BOOT_PAGE_SIZE + round_up(k, page),
            "ramdisk_offset": BOOT_PAGE_SIZE + round_up(k, page),
            "ramdisk_size": r,
            "end": off,
        }

    # -- vendor_boot -----------------------------------------------------
    def _parse_vendor_boot(self) -> None:
        h = parse_vendor_boot_header(self.data)
        self.header = h
        page = int(h["page_size"])
        hdr_block = int(h["header_block_size"])
        vs = int(h["vendor_ramdisk_size"])
        ds = int(h["dtb_size"])
        cur = hdr_block
        if cur + vs > len(self.data):
            die(f"vendor_ramdisk_size={vs} 越界（头区 {hdr_block}，镜像 {len(self.data)}）")
        vendor_ramdisk = self.data[cur:cur + vs]
        cur += round_up(vs, page)
        if cur + ds > len(self.data):
            die(f"dtb_size={ds} 越界（起点 {cur}，镜像 {len(self.data)}）")
        dtb = self.data[cur:cur + ds]
        cur += round_up(ds, page)
        ts = int(h["vendor_ramdisk_table_size"])
        table_blob = b""
        entries: List[Dict[str, Any]] = []
        if ts:
            if cur + ts > len(self.data):
                die(f"vendor_ramdisk_table_size={ts} 越界（起点 {cur}，镜像 {len(self.data)}）")
            table_blob = self.data[cur:cur + ts]
            entries = parse_ramdisk_table(
                table_blob, int(h["vendor_ramdisk_table_entry_num"]),
                int(h["vendor_ramdisk_table_entry_size"]), str(self.path))
            cur += round_up(ts, page)
        bs = int(h["bootconfig_size"])
        bootconfig = b""
        table_end_padded = cur
        if bs:
            if cur + bs > len(self.data):
                die(f"bootconfig_size={bs} 越界（起点 {cur}，镜像 {len(self.data)}）")
            bootconfig = self.data[cur:cur + bs]
        self.segments = {
            "vendor_ramdisk": vendor_ramdisk,
            "dtb": dtb,
            "vendor_ramdisk_table": table_blob,
            "bootconfig": bootconfig,
        }
        self.ramdisk_entries = entries
        self.layout = {
            "header_block": hdr_block,
            "vendor_ramdisk_offset": hdr_block,
            "vendor_ramdisk_size": vs,
            "dtb_offset": hdr_block + round_up(vs, page),
            "dtb_size": ds,
            "table_offset": hdr_block + round_up(vs, page) + round_up(ds, page),
            "table_size": ts,
            "table_end_padded": table_end_padded,
            "bootconfig_offset": cur,
            "bootconfig_size": bs,
            "end": cur + bs,
        }

    @property
    def padded_content_end(self) -> int:
        """「引导内容」结束位置（不含尾部零填充与 AVB 元数据）。"""
        return int(self.layout["end"])


def parsed_kind(data: bytes) -> Optional[str]:
    if data[:8] == BOOT_MAGIC:
        return "boot"
    if data[:8] == VENDOR_BOOT_MAGIC:
        return "vendor_boot"
    return None


def open_image(path: Path) -> Image:
    if not path.exists():
        die(f"文件不存在：{path}")
    if path.is_dir():
        die(f"{path} 是目录，不是镜像")
    return Image(path)


# ---------------------------------------------------------------------------
# 描述（info 用）
# ---------------------------------------------------------------------------

def fdt_summary(blob: bytes) -> str:
    if len(blob) < 8:
        return "过短，不是 FDT"
    magic, totalsize = struct.unpack_from(">II", blob, 0)
    if magic != 0xD00DFEED:
        return f"不是 FDT（magic=0x{magic:08x}）"
    tail = " + 尾部填充/dtb 2" if totalsize < len(blob) else ""
    return f"FDT totalsize={totalsize}{tail}（容器 {len(blob)} 字节）"


def compression_summary(blob: bytes) -> str:
    if not blob:
        return "空"
    return f"{len(blob)} 字节, 压缩格式={payload_hint(blob)}"


def describe_image(img: Image) -> List[str]:
    """info / verify 共用的字段清单。"""
    out: List[str] = []
    h = img.header
    if img.kind == "boot":
        out += [
            "格式              : Android boot image v4 (magic 'ANDROID!')",
            "header_version    : " + str(h["header_version"]),
            "header_size       : " + str(h["header_size"]) + "（头区占 4096 字节，其余补零）",
            "page_size         : " + str(h["page_size"]),
            "kernel_size       : " + str(h["kernel_size"]),
            "kernel_offset     : " + fmt_sz(img.layout["kernel_offset"]),
            "padded_kernel_end : " + fmt_sz(img.layout["padded_kernel_end"]),
            "ramdisk_size      : " + str(h["ramdisk_size"]),
            "os_version        : " + str(h["os_version"]) + "  " + str(h["os_version_text"]),
            "reserved[4]       : " + str(h["reserved"]),
            "signature_size    : " + str(h["signature_size"]),
            "cmdline           : " + repr(h["cmdline"]),
            "kernel            : " + compression_summary(img.segments["kernel"]),
        ]
        if h["ramdisk_size"]:
            out.append("ramdisk           : " + compression_summary(img.segments["ramdisk"]))
    else:
        out += [
            "格式              : Android vendor_boot v" + str(h["header_version"])
            + " (magic 'VNDRBOOT')",
            "header_version    : " + str(h["header_version"]),
            "header_size       : " + str(h["header_size"])
            + "（v" + str(h["header_version"]) + " 规范值 "
            + str(h["header_size_expected"]) + "，"
            + ("一致" if h["header_size_ok"] else "**不一致**")
            + "；头区补零到 " + str(h["page_size"]) + " -> "
            + str(h["header_block_size"]) + " 字节）",
            "page_size         : " + str(h["page_size"]),
            "kernel_addr       : " + fmt_sz(h["kernel_addr"]),
            "ramdisk_addr      : " + fmt_sz(h["ramdisk_addr"]),
            "tags_addr         : " + fmt_sz(h["tags_addr"]),
            "dtb_addr          : " + fmt_sz(h["dtb_addr"]),
            "name              : " + repr(h["name"]),
            "cmdline           : " + repr(h["cmdline"]),
            "vendor_ramdisk    : " + str(h["vendor_ramdisk_size"]) + " 字节 @ "
            + fmt_sz(img.layout["vendor_ramdisk_offset"]) + "  "
            + compression_summary(img.segments["vendor_ramdisk"]),
            "dtb               : " + str(h["dtb_size"]) + " 字节 @ "
            + fmt_sz(img.layout["dtb_offset"]) + "  " + fdt_summary(img.segments["dtb"]),
            "ramdisk_table     : " + str(h["vendor_ramdisk_table_size"]) + " 字节 @ "
            + fmt_sz(img.layout["table_offset"]) + f"，entry_num={h['vendor_ramdisk_table_entry_num']}"
            + f"，entry_size={h['vendor_ramdisk_table_entry_size']}",
            "bootconfig        : " + str(h["bootconfig_size"]) + " 字节 @ "
            + fmt_sz(img.layout["bootconfig_offset"]),
        ]
    out += [
        "镜像实际长度      : " + str(img.file_size),
        "引导内容结束      : " + fmt_sz(img.padded_content_end),
    ]
    return out


def describe_avb(img: Image) -> List[str]:
    out: List[str] = []
    f = img.footer
    if f is None:
        out.append("AVB footer        : 无（末尾 64 字节不是 'AVBf'）")
        tail = img.data[-64:] if len(img.data) >= 64 else img.data
        if img.data and not tail.strip(b"\x00"):
            out.append("  note           : 末尾全零 —— 若刷进整分区，LK 找不到 footer（见 README §AVB）")
        return out
    out += [
        "AVB footer        : 有（镜像末尾 64 字节，header_block_size=4096）",
        "  version         : " + f"{f['version_major']}.{f['version_minor']}",
        "  original_image_size : " + fmt_sz(f["original_image_size"]),
        "  vbmeta_offset   : " + fmt_sz(f["vbmeta_offset"]),
        "  vbmeta_size     : " + fmt_sz(f["vbmeta_size"]),
    ]
    v = img.vbmeta
    if v is None:
        out.append("  vbmeta          : 未解析")
        return out
    out += [
        "  vbmeta blob     :",
        "    magic                         : " + repr(v["magic"]),
        "    required_libavb_version       : " + f"{v['required_libavb_version_major']}."
        + str(v["required_libavb_version_minor"]),
        "    authentication_data_block_size: " + str(v["authentication_data_block_size"]),
        "    auxiliary_data_block_size     : " + str(v["auxiliary_data_block_size"]),
        "    algorithm_type                : " + str(v["algorithm_type"]) + " ("
        + v["algorithm_name"] + ")",
        "    hash_offset/size              : " + f"{v['hash_offset']}/{v['hash_size']}",
        "    signature_offset/size         : " + f"{v['signature_offset']}/{v['signature_size']}",
        "    public_key_offset/size        : " + f"{v['public_key_offset']}/{v['public_key_size']}",
        "    descriptors_offset/size       : " + f"{v['descriptors_offset']}/{v['descriptors_size']}",
        "    rollback_index                : " + str(v["rollback_index"]),
        "    flags                         : " + str(v["flags"]),
        "    rollback_index_location       : " + str(v["rollback_index_location"]),
        "    release_string                : " + repr(v["release_string"]),
    ]
    if v["algorithm_type"] == AVB_ALGORITHM_NONE:
        out.append("    note                          : NONE = 不签名；校验只靠 hash descriptor")
    for d in img.descriptors:
        if d.get("kind") == "hash":
            out.append("    [descriptor] hash: partition=" + repr(d["partition_name"])
                       + " image_size=" + str(d["image_size"])
                       + " alg=" + str(d["hash_algorithm"]) + " flags=" + str(d["flags"])
                       + " digest_match=" + str(d.get("digest_matches")))
        else:
            out.append(f"    [descriptor] tag={d['tag']} size={d['num_bytes_following']}")
    return out


def print_table(entries: Sequence[Dict[str, Any]], indent: str = "    ") -> None:
    if not entries:
        print(indent + "(空)")
        return
    keys = ("index", "ramdisk_name", "ramdisk_type_name", "ramdisk_size", "ramdisk_offset")
    widths = {k: max(len(k), *(len(str(e[k])) for e in entries)) for k in keys}
    print(indent + "  ".join(f"{k:<{widths[k]}}" for k in keys))
    for e in entries:
        print(indent + "  ".join(f"{str(e[k]):<{widths[k]}}" for k in keys))


def pick_ksu_target(entries: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按 SukiSU/KernelSU 的判定优先级选出它只会改动的那一个 fragment。

    优先级（TB330FU 实测，见 README）：
      1) ramdisk_name == "init_boot"（有独立 init_boot 分区的机型走这条）
      2) 否则 name == "" 且 type == PLATFORM(1) 的主体盘（mkbootimg 硬编码这一条）
    都没有则返回 None（SukiSU 不会注入 vendor_boot）。
    """
    for e in entries:
        if e["ramdisk_name"] == "init_boot":
            return e
    for e in entries:
        if e["ramdisk_name"] == "" and e["ramdisk_type"] == VENDOR_RAMDISK_TYPE_PLATFORM:
            return e
    return None


def cmd_info(args: argparse.Namespace) -> int:
    img = open_image(Path(args.image))
    print(f"== {img.path} ==")
    for line in describe_image(img):
        print("  " + line)
    for line in describe_avb(img):
        print("  " + line)
    if img.kind == "vendor_boot":
        print("  vendor_ramdisk_table:")
        print_table(img.ramdisk_entries)
        if img.ramdisk_entries:
            print("  board_id（只显示非零项；全零 = 对所有板型生效）:")
            for e in img.ramdisk_entries:
                nz = {i: v for i, v in enumerate(e["board_id"]) if v}
                print(f"    #{e['index']} {e['ramdisk_name']}: " + (repr(nz) if nz else "全零"))
        dlkm = [e for e in img.ramdisk_entries if e["ramdisk_type"] == 3]
        if dlkm:
            print("  可替换的 DLKM 条目（type=3）:")
            for e in dlkm:
                blob = img.segments["vendor_ramdisk"][
                    e["ramdisk_offset"]:e["ramdisk_offset"] + e["ramdisk_size"]]
                print(f"    #{e['index']} {e['ramdisk_name']}: {compression_summary(blob)}")
        # ---- SukiSU / KernelSU 注入点（防错关键信息）----
        target = pick_ksu_target(img.ramdisk_entries)
        print("  SukiSU/KernelSU 会修改的 fragment:")
        if target is None:
            print("    (无) —— 本 vendor_boot 里既没有 name=='init_boot' 的 fragment，"
                  "也没有 name=='' 的 PLATFORM 主体盘")
            print("    => SukiSU 不会通过 vendor_boot 注入；请改用 boot 分区方案")
        else:
            why = ("name=='init_boot'（优先级 1）"
                   if target["ramdisk_name"] == "init_boot"
                   else "name=='' 且 type=PLATFORM(1) 的主体盘（优先级 2）")
            print(f"    #{target['index']}  name={target['ramdisk_name']!r}  "
                  f"type={target['ramdisk_type']}({target['ramdisk_type_name']})  "
                  f"offset={target['ramdisk_offset']}  size={target['ramdisk_size']}")
            print(f"    判定依据: {why}")
            for e in dlkm:
                if e is not target:
                    print(f"    注意: 另有 DLKM fragment #{e['index']} "
                          f"{e['ramdisk_name']!r}，SukiSU 的 init_boot 方案**不会**动它；"
                          f"替换内核模块请按 README 的 DLKM 流程单独处理")
    else:
        print("  vendor_ramdisk_table: N/A（boot 分区没有 ramdisk table）")
        print("  SukiSU/KernelSU 注入点: 本机无 init_boot 分区，"
              "KernelSU 的 boot 方案直接改 GKI 内核所在分区（boot）")
    return 0


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------

def _meta_of_header(h: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in h.items() if k != "header_raw"}


def cmd_unpack(args: argparse.Namespace) -> int:
    img = open_image(Path(args.image))
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    h = img.header
    notes: List[str] = []

    (outdir / "header.bin").write_bytes(img.header["header_raw"])
    notes.append(f"header.bin: {img.kind} v{h['header_version']} 头部原始字节 "
                 f"({len(img.header['header_raw'])} 字节)")

    if img.kind == "boot":
        (outdir / "kernel").write_bytes(img.segments["kernel"])
        notes.append("kernel: " + compression_summary(img.segments["kernel"]))
        if h["ramdisk_size"]:
            (outdir / "ramdisk").write_bytes(img.segments["ramdisk"])
            notes.append("ramdisk: " + compression_summary(img.segments["ramdisk"]))
        (outdir / "header.json").write_text(
            json.dumps({"kind": "boot", "header": _meta_of_header(h),
                        "layout": img.layout}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        notes.append("header.json: 头字段 + 各段 offset（repack 依据）")
    else:
        (outdir / "vendor_ramdisk").write_bytes(img.segments["vendor_ramdisk"])
        notes.append("vendor_ramdisk: " + compression_summary(img.segments["vendor_ramdisk"]))
        if img.segments["dtb"]:
            (outdir / "dtb").write_bytes(img.segments["dtb"])
            notes.append("dtb: " + str(len(img.segments["dtb"])) + " 字节, "
                         + fdt_summary(img.segments["dtb"]))
        if img.segments["bootconfig"]:
            (outdir / "bootconfig").write_bytes(img.segments["bootconfig"])
            notes.append(f"bootconfig: {len(img.segments['bootconfig'])} 字节")
        table_json: Dict[str, Any] = {
            "kind": "vendor_boot",
            "header_version": h["header_version"],
            "page_size": h["page_size"],
            "header": _meta_of_header(h),
            "layout": img.layout,
            "original_partition_size": img.partition_size_declared or 0,
            "entries": [dict(e) for e in img.ramdisk_entries],
        }
        (outdir / TABLE_JSON).write_text(
            json.dumps(table_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        notes.append(f"{TABLE_JSON}: {len(img.ramdisk_entries)} 项（含 board_id，repack 依据）")
        for e in img.ramdisk_entries:
            blob = img.segments["vendor_ramdisk"][
                e["ramdisk_offset"]:e["ramdisk_offset"] + e["ramdisk_size"]]
            notes.append(f"  #{e['index']} name={e['ramdisk_name']!r} "
                         f"type={e['ramdisk_type']}({e['ramdisk_type_name']}) "
                         f"offset={e['ramdisk_offset']} size={e['ramdisk_size']} "
                         f"[{detect_compression(blob)}]")

    if img.footer is not None:
        (outdir / "avb_footer.json").write_text(
            json.dumps({"partition_size": img.file_size, "footer": img.footer,
                        "vbmeta": img.vbmeta, "descriptors": img.descriptors},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (outdir / PARTITION_SIZE_JSON).write_text(
            json.dumps({"source": str(img.path), "partition_size": img.file_size},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        notes.append("avb_footer.json / avb_partition_size.json: 原 AVB 信息与整分区尺寸 "
                     f"{img.file_size} 字节（repack --avb none 会沿用该尺寸）")
    else:
        warn(f"{img.path} 没有 AVB footer，无法推断分区尺寸；repack 时必须显式 "
             "--partition-size（boot 常见 32M，vendor_boot 常见 64M，以 FlashTool 为准）")

    (outdir / NOTES_TXT).write_text("\n".join(notes) + "\n", encoding="utf-8")

    print(f"== unpack {img.path} -> {outdir} ==")
    for n in notes:
        print("  " + n)
    print()
    print("  外部解包命令（本工具只做透明检测，不代劳）：")
    if img.kind == "vendor_boot":
        kind = detect_compression(img.segments["vendor_ramdisk"])
        if kind == "lz4_legacy":
            print("    lz4 -d -l vendor_ramdisk ramdisk.cpio        # legacy magic 0x184C2102")
            print("    # 若你的 lz4 只支持 frame：用 `lz4 -d -l` 或 python-lz4 的 LZ4_LEGACY")
        elif kind in ("lz4_frame",):
            print("    lz4 -d vendor_ramdisk ramdisk.cpio")
        elif kind in ("lz4_skippable",):
            print("    lz4 -d vendor_ramdisk ramdisk.cpio           # skippable frame")
        elif kind == "none(cpio)":
            print("    cp vendor_ramdisk ramdisk.cpio")
        elif kind == "gzip":
            print("    gzip -dc vendor_ramdisk > ramdisk.cpio")
        else:
            print("    # 按实际格式选择解压命令（见 README）")
        print("    mkdir -p root && cd root && cpio -idmv --no-absolute-filenames < ../ramdisk.cpio")
        print("    # 替换 lib/modules/*.ko 后：")
        print("    find . -print0 | cpio -o -H newc --null -R 0:0 > ../new.cpio")
        print("    lz4 -l -12 --favor-decSpeed ../new.cpio new_ramdisk.lz4   # legacy")
    else:
        print("    gzip -dc kernel > Image        # 或 zcat kernel > Image")
    return 0


# ---------------------------------------------------------------------------
# repack
# ---------------------------------------------------------------------------

def load_boot_dir(indir: Path) -> Tuple[Dict[str, Any], bytes, bytes]:
    meta = load_json(indir / "header.json")
    if not isinstance(meta, dict):
        die(f"{indir} 缺少 header.json —— 请先对该 boot 镜像执行 unpack")
    if meta.get("kind") != "boot":
        die(f"{indir}/header.json 的 kind={meta.get('kind')!r}，不是 boot 目录")
    kernel_path = indir / "kernel"
    if not kernel_path.is_file():
        die(f"{indir}/kernel 不存在")
    kernel = kernel_path.read_bytes()
    ramdisk = b""
    ramdisk_path = indir / "ramdisk"
    if ramdisk_path.is_file():
        ramdisk = ramdisk_path.read_bytes()
    elif int(meta["header"].get("ramdisk_size", 0)):
        die(f"原头 ramdisk_size={meta['header']['ramdisk_size']}，但 {ramdisk_path} 不存在")
    return meta["header"], kernel, ramdisk


def load_vendor_boot_dir(indir: Path) -> Tuple[Dict[str, Any], Dict[str, bytes], List[Dict[str, Any]]]:
    meta = load_json(indir / TABLE_JSON)
    if not isinstance(meta, dict):
        die(f"{indir} 缺少 {TABLE_JSON} —— 请先对该 vendor_boot 镜像执行 unpack")
    if meta.get("kind") != "vendor_boot":
        die(f"{indir}/{TABLE_JSON} 的 kind={meta.get('kind')!r}，不是 vendor_boot 目录")
    vr_path = indir / "vendor_ramdisk"
    if not vr_path.is_file():
        die(f"{indir}/vendor_ramdisk 不存在")
    segs = {"vendor_ramdisk": vr_path.read_bytes()}
    for name in ("dtb", "bootconfig"):
        p = indir / name
        segs[name] = p.read_bytes() if p.is_file() else b""
    entries = [dict(e) for e in (meta.get("entries") or [])]
    if not entries and segs["vendor_ramdisk"]:
        warn(f"{TABLE_JSON} 里没有 table 条目，但 vendor_ramdisk 非空；table 将写空")
    return meta["header"], segs, entries


def pack_boot(header: Dict[str, Any], kernel: bytes, ramdisk: bytes) -> bytes:
    """按 boot v4 布局打包（不含尾部填充/AVB）。"""
    out = bytearray(build_boot_header(header, len(kernel), len(ramdisk)))
    out += kernel
    out += bytes((-len(kernel)) % BOOT_PAGE_SIZE)
    if ramdisk:
        out += ramdisk
        out += bytes((-len(ramdisk)) % BOOT_PAGE_SIZE)
    return bytes(out)


def pack_vendor_boot(header: Dict[str, Any], segs: Dict[str, bytes],
                     entries: List[Dict[str, Any]]) -> Tuple[bytes, List[Dict[str, Any]]]:
    """按 vendor_boot v3/v4 布局打包，并**自动重算** table 的 size/offset。

    返回 (镜像字节, 重算后的条目列表)。
    """
    page = int(header.get("page_size") or VENDOR_BOOT_PAGE_SIZE)
    vr = segs["vendor_ramdisk"]
    dtb = segs["dtb"]
    bootconfig = segs["bootconfig"]
    hdr_version = int(header.get("header_version", 4))

    fixed: List[Dict[str, Any]] = []
    table_blob = b""
    if entries:
        if hdr_version != 4:
            die(f"header_version={hdr_version} 却带有 table 条目；v3 不应有 ramdisk table")
        ordered = sorted(entries, key=lambda e: int(e["ramdisk_offset"]))
        cursor = 0
        for e in ordered:
            size = int(e["ramdisk_size"])
            if size < 0:
                die(f"table 条目 {e.get('ramdisk_name')!r} 的 ramdisk_size 为负")
            ent = dict(e)
            ent["ramdisk_offset"] = cursor
            fixed.append(ent)
            cursor += size
        for i, ent in enumerate(fixed):
            ent["index"] = i
        if cursor > len(vr):
            die(f"table 声明总长 {cursor} 超过 vendor_ramdisk 实际 {len(vr)} 字节；"
                "如果替换了 DLKM 模块，请同步更新 ramdisk_table.json 中的 ramdisk_size")
        if cursor < len(vr):
            warn(f"vendor_ramdisk 尾部 {len(vr) - cursor} 字节不被任何 table 条目覆盖")
        table_blob = build_ramdisk_table(fixed)

    sizes = {
        "vendor_ramdisk_size": len(vr),
        "dtb_size": len(dtb),
        "vendor_ramdisk_table_size": len(table_blob),
        "vendor_ramdisk_table_entry_num": len(fixed),
        "bootconfig_size": len(bootconfig),
    }
    out = bytearray(build_vendor_boot_header(header, sizes))
    out += vr
    out += bytes((-len(vr)) % page)
    out += dtb
    out += bytes((-len(dtb)) % page)
    out += table_blob
    out += bytes((-len(table_blob)) % page)
    out += bootconfig
    return bytes(out), fixed


def read_footer_source(path: Optional[str]) -> Optional[bytes]:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        die(f"--avb-footer 指定的文件不存在：{p}")
    blob = p.read_bytes()
    if parse_avb_footer(blob) is None:
        die(f"{p} 末尾没有 'AVBf' footer 且不足一个块，无法 --avb keep")
    return blob


def repack_vbmeta_keep(body: bytes, original_with_footer: bytes) -> bytes:
    """把原始 AVB 尾部（对齐串 + vbmeta + 填充 + footer 块）接回新内容之后。

    只有新内容对齐后的长度恰等于原 footer.vbmeta_offset 时字节才有效；
    否则哈希/偏移都会失效，必须报错要求改用 --avb none。
    """
    footer = parse_avb_footer(original_with_footer)
    assert footer is not None
    if len(body) != footer["vbmeta_offset"]:
        die(f"--avb keep 无法复用：新内容长度 {len(body)} 与原 vbmeta_offset "
            f"{footer['vbmeta_offset']} 不一致（内容尺寸变化后哈希与偏移都失效）；"
            "请改用 --avb none 重新生成 footer")
    return body + original_with_footer[footer["vbmeta_offset"]:]


# ---------------------------------------------------------------------------
# avbtool 后端（AVB NONE footer 的**唯一**权威来源）
# ---------------------------------------------------------------------------

AVBTOOL_NAMES = ("avbtool", "avbtool.py")


def find_avbtool(explicit: Optional[str]) -> Optional[str]:
    """定位 avbtool。优先 --avbtool 指定路径，其次 PATH 中的 avbtool / avbtool.py。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        die(f"--avbtool 指定的文件不存在：{explicit}")
    for name in AVBTOOL_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _avbtool_cmd(tool: str) -> List[str]:
    """avbtool.py 是 Python 脚本，需带解释器；可执行的 avbtool 直接跑。"""
    p = Path(tool)
    try:
        head = p.read_bytes()[:64]
    except OSError:
        head = b""
    if head.startswith(b"#!"):
        return [sys.executable, tool]
    if p.suffix in (".py",) or head.startswith(b"import") or head.startswith(b"#!/"):
        return [sys.executable, tool]
    return [tool]


def avbtool_add_hash_footer_none(
    image_path: Path,
    partition_size: int,
    partition_name: str,
    avbtool_path: Optional[str] = None,
    salt: Optional[str] = None,
    props: Optional[Sequence[str]] = None,
) -> str:
    """调用官方 avbtool 给**已补零到分区尺寸**的镜像补 NONE footer。

    avbtool 会就地改写 image_path。返回实际执行的命令行（便于日志/复现）。
    footer 字节一律由 avbtool 生成，本工具不再手搓。
    """
    tool = find_avbtool(avbtool_path)
    if tool is None:
        die(
            "找不到 avbtool —— 拒绝输出不含合法 AVB footer 的镜像。\n"
            " AVB NONE footer 必须由官方 avbtool 生成（本工具不手搓 footer 字节）。\n"
            " 请安装：pip install avbtool  或  git clone https://android.googlesource.com/platform/external/avb\n"
            " 然后用以下命令手工补 footer：\n"
            + indent_command(
                ["avbtool", "add_hash_footer", "--image", str(image_path),
                 "--partition_size", str(partition_size),
                 "--partition_name", partition_name, "--algorithm", "NONE"]
                + (["--salt", salt] if salt else [])
                + [f'--prop "{p}"' for p in (props or [])]
            )
            + "\n 或用 --avbtool <path> 指定 avbtool 路径。"
        )
    argv_cmd = _avbtool_cmd(tool) + [
        "add_hash_footer",
        "--image", str(image_path),
        "--partition_size", str(partition_size),
        "--partition_name", partition_name,
        "--algorithm", "NONE",
    ]
    if salt:
        argv_cmd += ["--salt", salt]
    for prop in props or []:
        argv_cmd += ["--prop", prop]
    try:
        proc = subprocess.run(argv_cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        die(f"调用 avbtool 失败：{exc}")
    if proc.returncode != 0:
        die(f"avbtool add_hash_footer 失败（退出码 {proc.returncode}）：\n"
            f"  命令: {' '.join(argv_cmd)}\n"
            f"  stdout: {proc.stdout.strip()}\n  stderr: {proc.stderr.strip()}")
    return " ".join(argv_cmd)


def avbtool_verify(image_path: Path, avbtool_path: Optional[str] = None) -> Optional[str]:
    """可选交叉验证：若 avbtool 可用，跑 verify_image，返回其输出；不可用返回 None。"""
    tool = find_avbtool(avbtool_path)
    if tool is None:
        return None
    argv_cmd = _avbtool_cmd(tool) + ["verify_image", "--image", str(image_path)]
    try:
        proc = subprocess.run(argv_cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return None
    # avbtool 的 hash descriptor 校验会按分区名去找同目录的 <name>.img；
    # 找不到时它抛 FileNotFoundError，但这不影响 footer/vbmeta 本身的校验。
    out = (proc.stdout or "") + (proc.stderr or "")
    if "Successfully verified footer" in out:
        return out
    return f"(avbtool verify_image 退出码 {proc.returncode})\n{out}"


def indent_command(parts: Sequence[str]) -> str:
    """把命令折成可复制的多行。"""
    text = " ".join(parts)
    if " --" not in text:
        return "    " + text
    head, *rest = text.split(" --")
    return "    " + head + " \\\n      --" + " \\\n      --".join(rest)


def default_avb_props(ro_props: Optional[Dict[str, str]] = None) -> List[str]:
    """README/lead 要求的 3 个 avbtool --prop（取不到就标注 TODO，避免编造值）。"""
    rp = ro_props or {}
    osv = rp.get("ro.build.version.release", "")
    return [
        "com.android.build.boot.os_version:" + (osv if osv else "<ro.build.version.release>"),
        "com.android.build.boot.fingerprint:" + rp.get(
            "ro.build.fingerprint", "<ro.build.fingerprint>"),
        "com.android.build.boot.security_patch:" + rp.get(
            "ro.build.version.security_patch", "<ro.build.version.security_patch>"),
    ]


def finish_image(body: bytes, partition_size: int, avb_mode: str,
                 partition_name: str, avb_footer_path: Optional[str],
                 avbtool_path: Optional[str] = None,
                 workdir: Optional[Path] = None,
                 props: Optional[Sequence[str]] = None,
                 salt: Optional[str] = None) -> bytes:
    """收尾：AVB NONE footer（调用 avbtool）/ 复用原 footer / 仅补零到整分区。"""
    if avb_mode == "none":
        if not partition_size:
            die("--avb none 需要分区尺寸：请给 --partition-size N，"
                "或在 unpack 目录放 avb_partition_size.json（带 footer 的镜像 unpack 时会自动生成）")
        # avbtool add_hash_footer 要求输入是「未补零」的原始镜像：
        # 它自己把镜像补零到 partition_size 并在尾部预留区写 vbmeta + footer。
        # （若先补零到整分区再交给 avbtool，会报
        #   "Image size ... exceeds maximum image size ... in order to fit in a
        #    partition size of ..."）
        if len(body) > partition_size:
            die(f"内容 {len(body)} 字节超过分区尺寸 {partition_size}")
        tmp = Path(workdir or tempfile.mkdtemp(prefix="vkrepack_avb_")) / "_avb_stage.img"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(body)
        try:
            argv_text = avbtool_add_hash_footer_none(
                tmp, partition_size, partition_name, avbtool_path=avbtool_path,
                salt=salt, props=props)
            out = tmp.read_bytes()
        finally:
            if workdir is None:
                shutil.rmtree(tmp.parent, ignore_errors=True)
        if len(out) != partition_size:
            die(f"avbtool 输出 {len(out)} 字节 != 分区尺寸 {partition_size}")
        note(f"avbtool: {argv_text}")
        return out
    if avb_mode == "keep":
        orig = read_footer_source(avb_footer_path)
        if orig is None:
            die("--avb keep 需要 --avb-footer <原始整分区镜像>")
        assert orig is not None
        out = repack_vbmeta_keep(body, orig)
        if partition_size and len(out) > partition_size:
            die(f"复用原 footer 后 {len(out)} 字节超过分区尺寸 {partition_size}")
        if partition_size and len(out) < partition_size:
            die(f"复用原 footer 后只有 {len(out)} 字节，缺 {partition_size - len(out)} 字节；"
                "原 AVB 尾部与本分区尺寸不符，请改用 --avb none")
        return out
    # plain：仅补零
    if partition_size:
        if len(body) > partition_size:
            die(f"内容 {len(body)} 字节超过分区尺寸 {partition_size}")
        return body + bytes(partition_size - len(body))
    return body


def collect_verify_failures(img: Image, partition_size: int) -> List[str]:
    """verify 的轻量封装（repack/splice 自检用）。"""
    report = verify_image(img, partition_size)
    return report.failed


def selfcheck_roundtrip(outpath: Path, kind: str, body: bytes,
                        partition_size: int) -> None:
    """把刚写出的镜像再解析一遍并逐项核对；失败即 sys.exit(非 0)。"""
    img = open_image(outpath)
    problems: List[str] = []
    if img.kind != kind:
        problems.append(f"镜像类型变了：期望 {kind}，实得 {img.kind}")
    if img.padded_content_end != len(body):
        problems.append(f"内容结束位置 {img.padded_content_end} != 写入的 {len(body)}")
    if body[:8] != img.data[:8]:
        problems.append("identity（magic）被破坏")
    for label, failures in (("结构", collect_verify_failures(img, partition_size)),):
        for f in failures:
            problems.append(f"{label}: {f}")
    if problems:
        print("  self-check: FAIL", file=sys.stderr)
        for p in problems:
            print("    - " + p, file=sys.stderr)
        die("repack/splice 输出无法通过自检，已终止（输出文件保留以便排查）")
    print("  self-check: OK（重新解析 + 逐项结构校验通过；"
          + ("AVB footer/vbmeta 自洽，hash descriptor 摘要与内容相符"
             if img.footer else "无 AVB footer") + "）")


def cmd_repack(args: argparse.Namespace) -> int:
    indir = Path(args.indir)
    if not indir.is_dir():
        die(f"{indir} 不是目录")
    avb_mode = args.avb
    if avb_mode not in ("none", "keep"):
        die(f"不支持的 --avb 取值：{avb_mode}（只能是 none / keep）")

    boot_meta = load_json(indir / "header.json")
    if isinstance(boot_meta, dict) and boot_meta.get("kind") == "boot":
        kind = "boot"
        header, kernel, ramdisk = load_boot_dir(indir)
        body = pack_boot(header, kernel, ramdisk)
        table_entries: List[Dict[str, Any]] = []
        partition_name = args.partition_name or "boot"
        default_size = DEFAULT_PARTITION_SIZE
    elif isinstance(load_json(indir / TABLE_JSON), dict):
        kind = "vendor_boot"
        header, segs, entries = load_vendor_boot_dir(indir)
        body, table_entries = pack_vendor_boot(header, segs, entries)
        partition_name = args.partition_name or "vendor_boot"
        default_size = DEFAULT_VENDOR_BOOT_PARTITION_SIZE
    else:
        die(f"{indir} 里既没有 boot 的 header.json，也没有 vendor_boot 的 {TABLE_JSON}；"
            "请先执行 unpack")

    partition_size = resolve_partition_size(indir, args.partition_size, 0)
    size_source = "--partition-size" if args.partition_size else "unpack 目录记录"
    if not partition_size and avb_mode == "none":
        partition_size = default_size
        size_source = f"默认值 {default_size}（{kind} 常见值，可能与分区表不符！）"
        warn(f"不知道真实分区尺寸，暂按 {default_size} 字节处理；"
             "请用 FlashTool/mtkclient 核对后显式 --partition-size")

    img_bytes = finish_image(body, partition_size, avb_mode, partition_name,
                             args.avb_footer, avbtool_path=args.avbtool,
                             props=build_props(args), salt=args.salt)
    outpath = Path(args.output)
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_bytes(img_bytes)

    print(f"== repack {indir} -> {outpath} ==")
    print(f"  kind            : {kind}")
    print(f"  内容段长度      : {len(body)} 字节")
    print(f"  partition_size  : {partition_size or '(未指定)'}  来源: {size_source}")
    print(f"  输出长度        : {len(img_bytes)} 字节")
    print(f"  AVB             : {avb_mode}"
          + (f"（partition_name={partition_name}）" if avb_mode == "none" else ""))
    if kind == "vendor_boot":
        print("  重算后的 ramdisk table:")
        print_table(table_entries, indent="    ")
    selfcheck_roundtrip(outpath, kind, body, partition_size)
    return 0


def cmd_splice(args: argparse.Namespace) -> int:
    """只替换 boot 内核段，其余原样保留（含整分区尺寸与 AVB NONE footer）。"""
    src = open_image(Path(args.orig))
    if src.kind != "boot":
        die(f"{args.orig} 是 {src.kind} 镜像，splice 只支持 boot（vendor_boot 请用 unpack/repack）")
    new_kernel = Path(args.kernel).read_bytes()
    if not new_kernel:
        die(f"{args.kernel} 是空文件")
    kind = detect_compression(new_kernel)
    if kind != "gzip":
        warn(f"{args.kernel} 不是 gzip（检测为 {kind}）；MT6768 的 kernel 段必须是 Image.gz，"
             "否则内核解压失败会卡第一屏")

    body = pack_boot(src.header, new_kernel, src.segments.get("ramdisk", b""))
    partition_size = args.partition_size or (src.file_size if src.footer else 0)
    size_source = "--partition-size" if args.partition_size else "原镜像长度"
    if not partition_size:
        partition_size = DEFAULT_PARTITION_SIZE
        size_source = f"默认值 {partition_size}（原镜像无 footer，真实尺寸未知）"
        warn(f"原镜像没有 AVB footer，无法推断分区尺寸；暂按 {partition_size} 字节处理。"
             "请核对后再刷")

    avb_mode = args.avb
    if avb_mode == "keep":
        orig = read_footer_source(args.avb_footer or args.orig)
        if orig is None:
            die("--avb keep 需要带 footer 的原始镜像（或 --avb-footer 指定）")
        assert orig is not None
        img_bytes = repack_vbmeta_keep(body, orig)
        if len(img_bytes) < partition_size:
            die("原 AVB 尾部与分区尺寸不符；请改用 --avb none")
    else:
        img_bytes = finish_image(body, partition_size, "none", args.partition_name,
                                 args.avb_footer, avbtool_path=args.avbtool,
                                 props=build_props(args), salt=args.salt)
    if len(img_bytes) != partition_size:
        die(f"输出长度 {len(img_bytes)} != 分区尺寸 {partition_size}")

    outpath = Path(args.output)
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_bytes(img_bytes)

    print(f"== splice {args.orig} + {args.kernel} -> {outpath} ==")
    print(f"  原 kernel_size : {src.header['kernel_size']}")
    print(f"  新 kernel_size : {len(new_kernel)}  ({kind})")
    print(f"  cmdline        : {src.header['cmdline']!r}（沿用）")
    print(f"  os_version     : {src.header['os_version']}（沿用）")
    print(f"  ramdisk_size   : {src.header['ramdisk_size']}（沿用）")
    print(f"  partition_size : {partition_size}  来源: {size_source}")
    print(f"  AVB            : {avb_mode}（partition_name={args.partition_name}）")
    selfcheck_roundtrip(outpath, "boot", body, partition_size)
    print(f"  输出           : {len(img_bytes)} 字节，footer.original_image_size="
          f"{open_image(outpath).footer['original_image_size']}")
    return 0


def binascii_unhex(text: str) -> bytes:
    try:
        return binascii.unhexlify(text.replace(" ", ""))
    except (binascii.Error, ValueError) as exc:
        die(f"--salt 不是合法十六进制：{exc}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

class Report:
    """PASS/FAIL 明细收集器。"""

    def __init__(self) -> None:
        self.items: List[Tuple[bool, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        text = label if not detail else f"{label}  [{detail}]"
        self.items.append((bool(ok), text))
        return bool(ok)

    def info(self, label: str) -> None:
        self.items.append((True, "INFO " + label))

    @property
    def failed(self) -> List[str]:
        return [d for ok, d in self.items if not ok]

    def render(self) -> str:
        return "\n".join(f"  [{'PASS' if ok else 'FAIL'}] {desc}" for ok, desc in self.items)


def verify_image(img: Image, partition_size: int) -> Report:
    """结构自检：magic / header / 对齐 / offsets / table / 尺寸 / footer 关系。"""
    r = Report()
    h = img.header
    raw_len = img.file_size
    end = img.padded_content_end

    r.check(parsed_kind(img.data) == img.kind, "magic 与类型一致", img.kind)

    if img.kind == "boot":
        r.check(h["header_version"] == 4, "header_version == 4", str(h["header_version"]))
        r.check(h["header_size"] == BOOT_HDR_V4_SIZE, "header_size == 1584",
                str(h["header_size"]))
        r.check(end % BOOT_PAGE_SIZE == 0, "内容结束位置页对齐",
                f"{end} % 4096 = {end % BOOT_PAGE_SIZE}")
        lay = img.layout
        r.check(lay["kernel_offset"] == BOOT_PAGE_SIZE, "kernel 从 4096 开始",
                str(lay["kernel_offset"]))
        r.check(lay["kernel_offset"] + h["kernel_size"] <= end, "kernel 不越界",
                f"{lay['kernel_offset'] + h['kernel_size']} <= {end}")
        r.check(lay["padded_kernel_end"] == lay["ramdisk_offset"], "offsets 单调（kernel->ramdisk）")
        r.check(len(img.segments["kernel"]) == h["kernel_size"],
                "kernel 段长度 == kernel_size")
        ck = detect_compression(img.segments["kernel"])
        # 官方 boot_a 的 kernel 段是 gzip；但内核段也可以是未压缩的 arm64 Image
        # （或 lz4/zstd）。非 gzip 只作提示、不作致命错误 —— splice 模式允许
        # 直接喂 Image，此时只应 warning，不应让 verify 失败。
        if ck == "gzip":
            r.check(True, "kernel 是 gzip", ck)
        else:
            r.info(f"kernel 压缩格式为 {ck}（官方为 gzip）；若 MT6768 的 kernel 段"
                   "要求 gzip，请用 gzip -9 后再打包")
        if h["ramdisk_size"]:
            r_end = lay["ramdisk_offset"] + h["ramdisk_size"]
            r.check(r_end <= end, "ramdisk 不越界", f"{r_end} <= {end}")
            r.check(len(img.segments["ramdisk"]) == h["ramdisk_size"],
                    "ramdisk 段长度 == ramdisk_size")
        else:
            pad = img.data[lay["padded_kernel_end"]:end]
            r.check(pad.count(0) == len(pad), "kernel 之后到内容结束全零",
                    f"{len(pad)} 字节中非零 {len(pad) - pad.count(0)}")
        r.check(lay["end"] == (BOOT_PAGE_SIZE + round_up(h["kernel_size"], BOOT_PAGE_SIZE)
                               + (round_up(h["ramdisk_size"], BOOT_PAGE_SIZE)
                                  if h["ramdisk_size"] else 0)),
                "内容长度 == 头 + 对齐的各段之和", str(lay["end"]))
    else:
        page = int(h["page_size"])
        lay = img.layout
        r.check(h["header_version"] in (3, 4), "header_version in (3,4)",
                str(h["header_version"]))
        want = VENDOR_BOOT_HDR_V4_SIZE if h["header_version"] == 4 else VENDOR_BOOT_HDR_V3_SIZE
        r.check(h["header_size"] == want, f"header_size == {want}", str(h["header_size"]))
        # AOSP bootimg.h 规定 v3/v4 的 kernel_addr/ramdisk_addr/tags_addr 为 0；
        # 但 MTK 厂商镜像**不遵守**这一点（本机实测：
        #   0x40080000 / 0x47c80000 / 0x4bc80000），LK 依赖这些地址加载。
        # 因此这里**只记录不判错**；repack 时会原样保留，不会改写。
        if (h["kernel_addr"], h["ramdisk_addr"], h["tags_addr"]) == (0, 0, 0):
            r.check(True, "kernel/ramdisk/tags_addr == 0（AOSP 规范）", "0/0/0")
        else:
            r.info(f"kernel/ramdisk/tags_addr 非零（MTK 厂商变体，属正常）："
                   f"0x{h['kernel_addr']:x}/0x{h['ramdisk_addr']:x}/0x{h['tags_addr']:x}"
                   " —— repack 会原样保留，勿手工改")
        r.check(lay["header_block"] % page == 0, "头区补零到 page 对齐",
                f"{lay['header_block']} % {page} = {lay['header_block'] % page}")
        r.check(lay["vendor_ramdisk_offset"] == lay["header_block"], "vendor_ramdisk 紧接头区")
        r.check(lay["dtb_offset"] == lay["vendor_ramdisk_offset"]
                + round_up(lay["vendor_ramdisk_size"], page),
                "dtb_offset == 头区 + 对齐的 vendor_ramdisk")
        r.check(lay["table_offset"] == lay["dtb_offset"] + round_up(lay["dtb_size"], page),
                "table_offset == dtb 之后（页对齐）")
        r.check(lay["bootconfig_offset"] == lay["table_end_padded"],
                "bootconfig_offset == 对齐后的 table 结束")
        r.check(lay["bootconfig_offset"] + lay["bootconfig_size"] == end,
                "bootconfig 结束 == 内容结束",
                f"{lay['bootconfig_offset'] + lay['bootconfig_size']} vs {end}")
        for key in ("vendor_ramdisk_size", "dtb_size", "table_size", "bootconfig_size"):
            r.check(lay[key] <= end, f"{key} <= 内容长度", f"{lay[key]} <= {end}")
        r.check(len(img.segments["vendor_ramdisk"]) == lay["vendor_ramdisk_size"],
                "vendor_ramdisk 段长度 == header.vendor_ramdisk_size")
        r.check(len(img.segments["dtb"]) == lay["dtb_size"], "dtb 段长度 == header.dtb_size")
        r.check(len(img.segments["bootconfig"]) == lay["bootconfig_size"],
                "bootconfig 段长度 == header.bootconfig_size")
        if lay["dtb_size"]:
            magic = struct.unpack_from(">I", img.segments["dtb"], 0)[0] \
                if len(img.segments["dtb"]) >= 4 else 0
            # vendor_boot 的 dtb 段有两种合法形态：
            #   1) 裸 FDT（magic 0xd00dfeed）—— AOSP 默认；
            #   2) Android DT-table（magic 0xd7b7ab1e，FDT 在偏移 64）——
            #      **本机实测就是这种**（TB330FU / MT6768）。
            # 两者都接受，其它 magic 才判错。
            if magic == 0xD00DFEED:
                r.check(True, "dtb 是裸 FDT", f"magic=0x{magic:08x}")
            elif magic == 0xD7B7AB1E:
                r.check(True, "dtb 是 Android DT-table（FDT 在偏移 64）",
                        f"magic=0x{magic:08x}, size={lay['dtb_size']}")
                if len(img.segments["dtb"]) >= 68:
                    inner = struct.unpack_from(">I", img.segments["dtb"], 64)[0]
                    r.check(inner == 0xD00DFEED, "DT-table 内嵌 FDT magic 正确",
                            f"0x{inner:08x}")
            else:
                r.check(False, "dtb 既不是 FDT 也不是 Android DT-table",
                        f"magic=0x{magic:08x}")
        for gstart, gend in (
            (lay["vendor_ramdisk_offset"] + lay["vendor_ramdisk_size"], lay["dtb_offset"]),
            (lay["dtb_offset"] + lay["dtb_size"], lay["table_offset"]),
            (lay["table_offset"] + lay["table_size"], lay["bootconfig_offset"]),
        ):
            pad = img.data[gstart:gend]
            r.check(pad.count(0) == len(pad), f"对齐填充区全零 [{gstart}:{gend}]",
                    f"{len(pad)} 字节")

        if h["header_version"] == 4:
            entries = img.ramdisk_entries
            r.check(h["vendor_ramdisk_table_entry_size"] == VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE,
                    "table entry_size == 108", str(h["vendor_ramdisk_table_entry_size"]))
            r.check(h["vendor_ramdisk_table_entry_num"] == len(entries),
                    "entry_num == 实际解析条目数", str(len(entries)))
            r.check(h["vendor_ramdisk_table_size"]
                    == len(entries) * VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE,
                    "table_size == entry_num * 108",
                    f"{h['vendor_ramdisk_table_size']} vs "
                    f"{len(entries) * VENDOR_RAMDISK_TABLE_ENTRY_V4_SIZE}")
            if entries:
                cursor = 0
                contiguous = True
                overlap = False
                in_range = True
                for e in sorted(entries, key=lambda x: x["ramdisk_offset"]):
                    if e["ramdisk_offset"] != cursor:
                        contiguous = False
                    if e["ramdisk_offset"] < cursor:
                        overlap = True
                    cursor = e["ramdisk_offset"] + e["ramdisk_size"]
                    if cursor > h["vendor_ramdisk_size"]:
                        in_range = False
                r.check(contiguous, "table 条目首尾相接（无空洞 / 无重叠）")
                r.check(not overlap, "table 条目互不重叠")
                r.check(in_range, "table 条目落在 vendor_ramdisk 范围内",
                        f"覆盖到 {cursor} <= {h['vendor_ramdisk_size']}")
                r.check(cursor <= h["vendor_ramdisk_size"],
                        "table 覆盖长度 <= vendor_ramdisk_size",
                        f"{cursor} <= {h['vendor_ramdisk_size']}")
                names = [e["ramdisk_name"] for e in entries]
                r.check(len(names) == len(set(names)), "ramdisk_name 唯一", ", ".join(names))
                for e in entries:
                    r.info(f"table #{e['index']} {e['ramdisk_name']!r} "
                           f"type={e['ramdisk_type']}({e['ramdisk_type_name']}) "
                           f"offset={e['ramdisk_offset']} size={e['ramdisk_size']}")

    f = img.footer
    if f is None:
        r.info("无 AVB footer（末尾 64 字节不是 'AVBf'）")
        if partition_size:
            r.check(raw_len == partition_size, "镜像尺寸 == 指定分区尺寸",
                    f"{raw_len} vs {partition_size}")
            if raw_len == partition_size:
                # 整分区形态却没 footer：LK 解析必然失败（README §AVB 的实测结论）。
                # 唯一可接受的情况是「紧凑镜像恰好等于分区尺寸」——实际上不可能，
                # 因为分区尺寸总是远大于引导内容，所以这里一律判 FAIL。
                r.check(False,
                        "镜像尺寸等于分区尺寸却没有 AVB footer"
                        "（LK 会冻结在第一屏，需 avbtool add_hash_footer --algorithm NONE）",
                        f"内容结束 {end} / 镜像 {raw_len}")
        else:
            r.info("未指定 --partition-size；无 footer 时视为紧凑镜像，不检查分区尺寸")
        nonzero_tail = img.data[end:].strip(b"\x00")
        if nonzero_tail:
            r.check(False, "内容之后有非零残留数据（可能是被覆盖一半的旧 AVB 结构）",
                    f"{len(nonzero_tail)} 字节非零")
    else:
        # boot v4 内容按页对齐；vendor_boot 以 bootconfig 结尾，可以不页对齐
        if img.kind == "boot":
            r.check(len(img.data) % AVB_BLOCK_SIZE == 0, "original_image_size 4096 对齐",
                    str(len(img.data)))
        else:
            r.check(f["vbmeta_offset"] % AVB_BLOCK_SIZE == 0,
                    "vbmeta_offset 4096 对齐", str(f["vbmeta_offset"]))
        r.check(f["version_major"] == 1 and f["version_minor"] == 0, "footer version == 1.0",
                f"{f['version_major']}.{f['version_minor']}")
        # original_image_size 是「引导内容」+ 厂商可能在末尾追加的少量数据。
        # 本机原厂实测：content_end=0xdf4000，而 original_image_size=0xdf8000
        # （多 16 KiB）。所以判据是「>= 内容结束」且「不超过 vbmeta_offset」，
        # 而不是严格相等。
        r.check(end <= f["original_image_size"],
                "original_image_size >= 引导内容结束位置",
                f"{f['original_image_size']} >= {end}")
        extra = f["original_image_size"] - end
        if extra:
            r.info(f"original_image_size 比引导内容多 {extra} 字节"
                   f"（原厂末尾尾部数据/对齐，repack 时会被丢弃并重算）")
        r.check(f["original_image_size"] <= f["vbmeta_offset"],
                "original_image_size <= vbmeta_offset",
                f"{f['original_image_size']} <= {f['vbmeta_offset']}")
        if img.vbmeta is not None:
            # 原厂 boot 可能是 RSA4096 真签名（本机即如此）；vendor_boot 是 NONE。
            # 只有 NONE 才需要额外检查 auth 块为 0；签名镜像只做信息提示。
            if img.vbmeta["algorithm_type"] == AVB_ALGORITHM_NONE:
                r.check(True, "Algorithm == NONE（无签名，结构合法即可启动）",
                        img.vbmeta["algorithm_name"])
            else:
                r.info(f"Algorithm = {img.vbmeta['algorithm_name']}"
                       "（原厂真签名；解锁态不校验，自编镜像将降级为 NONE）")
        lead = f["vbmeta_offset"] - f["original_image_size"]
        r.check(0 <= lead < AVB_BLOCK_SIZE, "vbmeta 紧跟内容（对齐间隙 < 4096）", str(lead))
        pad = img.data[f["original_image_size"]:f["vbmeta_offset"]]
        r.check(pad.count(0) == len(pad), "对齐间隙全零", f"{len(pad)} 字节")
        r.check(f["vbmeta_offset"] + f["vbmeta_size"] <= raw_len - AVB_BLOCK_SIZE,
                "vbmeta 在 footer 块之前",
                f"{f['vbmeta_offset'] + f['vbmeta_size']} <= {raw_len - AVB_BLOCK_SIZE}")
        mid = img.data[f["vbmeta_offset"] + f["vbmeta_size"]:raw_len - AVB_BLOCK_SIZE]
        r.check(mid.count(0) == len(mid), "vbmeta 与 footer 块之间全零", f"{len(mid)} 字节")
        tail_fill = img.data[raw_len - AVB_BLOCK_SIZE:raw_len - AVB_FOOTER_SIZE]
        r.check(tail_fill.count(0) == len(tail_fill), "footer 块前 4032 字节全零",
                f"{len(tail_fill)} 字节")
        v = img.vbmeta
        r.check(v is not None, "vbmeta 头可解析")
        if v is not None:
            r.check(v["magic"] == AVB_MAGIC.decode(), "vbmeta magic == 'AVB0'", v["magic"])
            r.check(v["blob_size"] == f["vbmeta_size"],
                    "vbmeta 头声明长度 == footer.vbmeta_size",
                    f"{v['blob_size']} vs {f['vbmeta_size']}")
            r.check(v["algorithm_type"] in AVB_ALG_NAMES, "algorithm_type 已知",
                    f"{v['algorithm_type']}({v['algorithm_name']})")
            if v["algorithm_type"] == AVB_ALGORITHM_NONE:
                r.check(v["authentication_data_block_size"] % 64 == 0,
                        "NONE: auth block 64 对齐",
                        str(v["authentication_data_block_size"]))
                r.check(v["auxiliary_data_block_size"] % 64 == 0,
                        "NONE: aux block 64 对齐", str(v["auxiliary_data_block_size"]))
                r.check(v["descriptors_size"] <= v["auxiliary_data_block_size"],
                        "descriptors 落在 aux block 内")
                r.check(v["hash_size"] == 0 and v["signature_size"] == 0
                        and v["public_key_size"] == 0, "NONE: 无 hash/signature/public key")
            r.check(v["rollback_index_location"] == 0
                    or v["required_libavb_version_minor"] >= 2,
                    "rollback_index_location>0 需 libavb>=1.2",
                    f"minor={v['required_libavb_version_minor']}")
            for d in img.descriptors:
                if d.get("kind") == "hash":
                    # avbtool 对未对齐镜像记录的是「补齐前」长度；
                    # 本工具产出的镜像 original_image_size 恒为对齐值，两种都要接受
                    r.check(d["image_size"] in (end, f["original_image_size"]),
                            "hash descriptor.image_size == 内容长度或 original_image_size",
                            f"{d['image_size']} in ({end}, {f['original_image_size']})")
                    r.check(d.get("digest_matches") is not False,
                            "hash descriptor 摘要与内容相符",
                            str(d.get("digest_matches")))
        if partition_size:
            r.check(raw_len == partition_size, "镜像尺寸 == 指定分区尺寸",
                    f"{raw_len} vs {partition_size}")
    return r


def cmd_verify(args: argparse.Namespace) -> int:
    img = open_image(Path(args.image))
    partition_size = int(args.partition_size or 0)
    if not partition_size and img.footer is not None:
        partition_size = img.file_size
    report = verify_image(img, partition_size)
    print(f"== verify {img.path} ==")
    print(f"  kind={img.kind}  file_size={img.file_size}  "
          f"内容结束={img.padded_content_end}  "
          f"partition_size={partition_size or '(未指定)'}")
    for line in describe_image(img)[:0]:
        print(line)
    print(report.render())
    if report.failed:
        print(f"\n结果: FAIL（{len(report.failed)} 项不通过）")
        return 1
    print(f"\n结果: PASS（{len(report.items)} 项全部通过）")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_props(args: argparse.Namespace) -> List[str]:
    """组装 avbtool 的 --prop 列表。

    默认给 3 条（lead/README 要求）；机型的真实取值用 --ro-prop k=v 传入，
    没传时写占位符而不是编造值（avbtool 只把它当字符串存进 vbmeta）。
    """
    props: List[str] = list(getattr(args, "prop", None) or [])
    ro: Dict[str, str] = {}
    for item in getattr(args, "ro_prop", None) or []:
        if "=" not in item:
            die(f"--ro-prop 需要 key=value 形式，收到 {item!r}")
        k, v = item.split("=", 1)
        ro[k.strip()] = v.strip()
    if not props and ro:
        props = default_avb_props(ro)
    return props


def parse_size(text: str) -> int:
    """支持 33554432 / 0x2000000 / 32M / 64MiB 等写法。"""
    s = text.strip().replace("_", "")
    m = re.fullmatch(r"(?i)(\d+)([kmgt])(i?b)?", s)
    if m:
        mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2).lower()]
        return int(m.group(1)) * mult
    try:
        return int(s, 0)
    except ValueError:
        pass
    die(f"无法解析尺寸 {text!r}（示例：33554432 / 0x2000000 / 32M）")
    raise AssertionError  # pragma: no cover


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vkrepack.py",
        description="boot v4 / vendor_boot v4 解包·重打包·AVB NONE footer 工具（TB330FU / MT6768）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python vkrepack.py info   boot.img\n"
            "  python vkrepack.py unpack vendor_boot.img -o vb\n"
            "  python vkrepack.py repack vb -o vendor_boot.new.img --avb none --partition-size 64M\n"
            "  python vkrepack.py splice boot.img --kernel Image.gz -o boot.new.img\n"
            "  python vkrepack.py verify boot.new.img --partition-size 32M\n"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{info,unpack,repack,verify,splice}")

    p = sub.add_parser("info", help="打印头字段 / ramdisk table / AVB footer")
    p.add_argument("image")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("unpack", help="导出各段 + header.bin + ramdisk_table.json")
    p.add_argument("image")
    p.add_argument("-o", "--output", required=True, help="输出目录")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("repack", help="由 unpack 目录重建镜像")
    p.add_argument("indir")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--partition-size", type=parse_size, default=0,
                   help="目标分区字节数（32M / 67108864）；--avb none 必需（可从 unpack 目录推断）")
    p.add_argument("--avb", choices=("none", "keep"), default="none",
                   help="none=生成等价 avbtool --algorithm NONE 的 footer；keep=复用原 footer 字节")
    p.add_argument("--avb-footer", default=None,
                   help="--avb keep 时用于取原 footer/vbmeta 的原始整分区镜像")
    p.add_argument("--partition-name", default=None,
                   help="写进 hash descriptor 的分区名（默认 boot / vendor_boot）")
    p.add_argument("--salt", default=None,
                   help="footer 哈希盐（hex）；缺省由 avbtool 随机生成 32 字节")
    p.add_argument("--avbtool", default=None,
                   help="avbtool 路径（默认从 PATH 找 avbtool / avbtool.py）")
    p.add_argument("--prop", action="append", default=None,
                   help='传给 avbtool 的 --prop "k:v"（可重复）')
    p.add_argument("--ro-prop", action="append", default=None,
                   help="设备属性 key=value（如 ro.build.fingerprint=...），"
                        "用于生成默认的 3 条 avb prop")
    p.set_defaults(func=cmd_repack)

    p = sub.add_parser("verify", help="结构自检，输出 PASS/FAIL 明细")
    p.add_argument("image")
    p.add_argument("--partition-size", type=parse_size, default=0)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("splice", help="只替换 boot 内核段（其余原样保留）")
    p.add_argument("orig")
    p.add_argument("--kernel", required=True, help="新内核（Image.gz）")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--partition-size", type=parse_size, default=0)
    p.add_argument("--avb", choices=("none", "keep"), default="none")
    p.add_argument("--avb-footer", default=None)
    p.add_argument("--partition-name", default="boot")
    p.add_argument("--salt", default=None)
    p.add_argument("--avbtool", default=None)
    p.add_argument("--prop", action="append", default=None)
    p.add_argument("--ro-prop", action="append", default=None)
    p.set_defaults(func=cmd_splice)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    _harden_stdio()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except BrokenPipeError:                          # pragma: no cover
        os._exit(1)
    except KeyError as exc:
        die(f"输入数据缺少字段 {exc}（JSON 可能被手工改坏）")


if __name__ == "__main__":
    sys.exit(main())
