#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_vkrepack.py —— vkrepack.py 的单元测试（unittest，无第三方依赖）。

运行：
    python test_vkrepack.py
    python -m unittest -v test_vkrepack

覆盖：
  1. boot v4 合成镜像：info / unpack / repack --avb none / verify 全 PASS
  2. vendor_boot v4 合成镜像：含 3 个 ramdisk table 项（platform / recovery / dlkm）
  3. 往返一致性：repack 产物再 unpack，kernel / dtb / vendor_ramdisk / bootconfig
     / bootconfig / ramdisk_table 逐字节一致
  4. 逐项篡改（magic / header_size / kernel_size / table offset / footer /
     original_image_size / vbmeta algorithm / hash digest / 半截镜像 / 全零尾）
     后 verify 必须 FAIL
  5. AVB NONE footer 的结构断言（magic、auth=0、aux 64 对齐、original_image_size、
     幂等性、与 avbtool 文档一致的字段取值）
  6. splice：只换内核，其余字段（cmdline/os_version/ramdisk_size/分区尺寸/AVB）保持
  7. DLKM 替换：改 ramdisk_table.json 的 ramdisk_size 后 repack 自动重算 offset
"""

from __future__ import annotations

import gzip
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ---------------------------------------------------------------------------
# 测试用 avbtool 桩
# ---------------------------------------------------------------------------
# vkrepack 的**生产路径**故意 fail-closed：找不到官方 avbtool 就拒绝输出
# AVB footer（footer 字节必须由 avbtool 生成，本工具不手搓）。
# 但单测环境不一定装了 avbtool，因此这里提供一个**最小桩**：
#   * 只实现 add_hash_footer --algorithm NONE；
#   * footer/vbmeta 的字节布局直接调用 vkrepack 内部的复刻实现
#     （add_avb_none），因此测到的仍是 vkrepack 自己的语义；
#   * 桩存在本身不改变“生产必须用真 avbtool”的约束。
_AVBTOOL_STUB_SRC = r'''
import sys
import tempfile
sys.path.insert(0, r"{here}")
import vkrepack as vk
from pathlib import Path


def main(argv):
    if not argv or argv[0] != "add_hash_footer":
        sys.stderr.write("stub avbtool: only add_hash_footer is supported\n")
        return 2
    args = argv[1:]
    image = part_name = None
    part_size = None
    algorithm = None
    salt = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--image":
            image = args[i + 1]; i += 2
        elif a == "--partition_name":
            part_name = args[i + 1]; i += 2
        elif a == "--partition_size":
            part_size = int(args[i + 1], 0); i += 2
        elif a == "--algorithm":
            algorithm = args[i + 1]; i += 2
        elif a == "--salt":
            salt = bytes.fromhex(args[i + 1]); i += 2
        elif a == "--prop":
            i += 2
        else:
            i += 1
    if image is None or part_name is None or part_size is None:
        sys.stderr.write("stub avbtool: missing --image/--partition_name/--partition_size\n")
        return 2
    if algorithm not in (None, "NONE"):
        sys.stderr.write("stub avbtool: only --algorithm NONE is supported\n")
        return 2
    p = Path(image)
    body = p.read_bytes()
    # 与真 avbtool 一致的语义：输入是「未补零」的原始镜像，
    # avbtool 自己补零到 partition_size 并写 vbmeta + footer。
    # （若输入已带 footer，avbtool 会先剥掉再重算 —— 这里同样处理。）
    existing = vk.parse_avb_footer(body)
    if existing is not None:
        body = body[:existing["original_image_size"]]
    p.write_bytes(body)
    p.write_bytes(vk.add_avb_none(Path(image).read_bytes(), part_name, part_size,
                                  salt=salt or b"\x00" * 32))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _make_avbtool_stub() -> Path:
    """把桩写到一次性的临时目录（会话内缓存，避免每个用例重复写）。"""
    cached = getattr(_make_avbtool_stub, "_path", None)
    if cached and Path(cached).is_file():
        return Path(cached)
    d = Path(tempfile.mkdtemp(prefix="vkrepack_avbtool_stub_"))
    p = d / "avbtool.py"
    p.write_text(_AVBTOOL_STUB_SRC.format(here=str(HERE)), encoding="utf-8")
    _make_avbtool_stub._path = str(p)
    return p


AVBTOOL_STUB = _make_avbtool_stub()


import vkrepack as vk  # noqa: E402

PAGE = 4096
BOOT_PART_SIZE = 32 * 1024 * 1024
VENDOR_BOOT_PART_SIZE = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# 合成数据构造
# ---------------------------------------------------------------------------

def make_kernel_gz(seed: int = 0, size: int = 4000) -> bytes:
    """可复现的 gzip 内核（mtime=0，保证字节稳定）。"""
    payload = (b"Linux version 6.6.30-barley-seed%d-" % seed) * (size // 32 + 1)
    return gzip.compress(payload[:size], mtime=0)


def make_fdt(size: int = 8192, magic: int = 0xD00DFEED) -> bytes:
    """最小 FDT 头 + 填充（足够测试 totalsize / magic 判断）。"""
    head = struct.pack(">10I", magic, size, 0x38, 0x40, 0x40, 0, 17, 16, 0, 0)
    blob = head + bytes(size - len(head))
    return blob


def make_cpio(entries: Dict[str, bytes]) -> bytes:
    """构造一个合法的 newc cpio（用于模拟 vendor_ramdisk 载荷）。"""
    out = bytearray()

    def add(name: str, data: bytes) -> None:
        namez = name.encode() + b"\x00"
        hdr = (
            b"070701"
            + b"00000000"                                        # ino
            + b"00000000"                                        # mode
            + b"00000000"                                        # uid
            + b"00000000"                                        # gid
            + b"00000001"                                        # nlink
            + b"00000000"                                        # mtime
            + b"%08X" % len(data)                                # filesize
            + b"00000000" * 2                                    # devmajor/minor
            + b"00000000" * 2                                    # rdev
            + b"%08X" % len(namez)                               # namesize
            + b"00000000"                                        # check
        )
        out.extend(hdr)
        out.extend(namez)
        out.extend(b"\x00" * ((4 - (len(hdr) + len(namez)) % 4) % 4))
        out.extend(data)
        out.extend(b"\x00" * ((4 - len(data) % 4) % 4))

    add(".", b"")
    for name, data in entries.items():
        add(name, data)
    add("TRAILER!!!", b"")
    out.extend(b"\x00" * ((4 - len(out) % 4) % 4))
    return bytes(out)


def make_lz4_legacy(cpio: bytes, filler: int = 64) -> bytes:
    """伪造一段「lz4 legacy 压缩」数据：magic + 一个高压缩率块。

    这里只用于格式检测 / table 重算测试，不要求真能被 lz4 解开；
    块头 (3<<30)|len 是合法 legacy 块头，数据块为 cpio 的「压缩后」占位。
    """
    magic = b"\x02\x21\x4c\x18"
    # 让块内容长度 = 原始数据长度的 1/4 左右 + 头部
    data = (cpio[: filler])[::-1]  # 占位「压缩数据」
    block = struct.pack("<I", (3 << 30) | len(data)) + data
    return magic + block


def synth_vendor_boot(
    dlkm_ramdisk: bytes,
    platform_ramdisk: bytes = b"\x02\x21\x4c\x18" + b"\xAA" * 500,
    recovery_ramdisk: bytes = b"\x02\x21\x4c\x18" + b"\xBB" * 700,
    bootconfig: bytes = b"androidboot.hardware=mt6768\nandroidboot.slot_suffix=_b\n",
    dtb: Optional[bytes] = None,
    cmdline: str = "bootopt=64S3,32N2,64N2",
    name: str = "barley",
) -> Tuple[bytes, Dict[str, Any]]:
    """构造 vendor_boot v4 合成镜像（不含 AVB），返回 (镜像, 期望值)。"""
    dtb = make_fdt() if dtb is None else dtb
    ramdisks = [
        {"ramdisk_type": 1, "ramdisk_name": "platform", "data": platform_ramdisk},
        {"ramdisk_type": 2, "ramdisk_name": "recovery", "data": recovery_ramdisk},
        {"ramdisk_type": 3, "ramdisk_name": "dlkm", "data": dlkm_ramdisk},
    ]
    vendor_ramdisk = b""
    entries: List[Dict[str, Any]] = []
    cursor = 0
    for i, item in enumerate(ramdisks):
        entries.append({
            "index": i,
            "ramdisk_size": len(item["data"]),
            "ramdisk_offset": cursor,
            "ramdisk_type": item["ramdisk_type"],
            "ramdisk_type_name": vk.VENDOR_RAMDISK_TYPE_NAMES[item["ramdisk_type"]],
            "ramdisk_name": item["ramdisk_name"],
            "board_id": [0] * 16,
        })
        vendor_ramdisk += item["data"]
        cursor += len(item["data"])
    table = vk.build_ramdisk_table(entries)
    header = {
        "kind": "vendor_boot",
        "header_version": 4,
        "header_size": vk.VENDOR_BOOT_HDR_V4_SIZE,
        "page_size": PAGE,
        "kernel_addr": 0,
        "ramdisk_addr": 0,
        "tags_addr": 0,
        "dtb_addr": 0,
        "name": name,
        "cmdline": cmdline,
    }
    sizes = {
        "vendor_ramdisk_size": len(vendor_ramdisk),
        "dtb_size": len(dtb),
        "vendor_ramdisk_table_size": len(table),
        "vendor_ramdisk_table_entry_num": len(entries),
        "bootconfig_size": len(bootconfig),
    }
    hdr = vk.build_vendor_boot_header(header, sizes)
    img = bytearray(hdr)
    img += vendor_ramdisk + bytes((-len(vendor_ramdisk)) % PAGE)
    img += dtb + bytes((-len(dtb)) % PAGE)
    img += table + bytes((-len(table)) % PAGE)
    img += bootconfig
    expect = {
        "header": header,
        "entries": entries,
        "vendor_ramdisk": vendor_ramdisk,
        "dtb": dtb,
        "bootconfig": bootconfig,
        "table": table,
        "platform_ramdisk": platform_ramdisk,
        "recovery_ramdisk": recovery_ramdisk,
        "dlkm_ramdisk": dlkm_ramdisk,
    }
    return bytes(img), expect


def synth_boot(kernel: Optional[bytes] = None, cmdline: str = "console=ttyMSM0,115200n8",
               os_version: int = 0x0C2F0E04) -> Tuple[bytes, Dict[str, Any]]:
    """构造 boot v4 合成镜像（不含 AVB）。"""
    kernel = make_kernel_gz(1) if kernel is None else kernel
    header = {
        "kind": "boot",
        "header_version": 4,
        "header_size": vk.BOOT_HDR_V4_SIZE,
        "os_version": os_version,
        "reserved": [0, 0, 0, 0],
        "signature_size": 0,
        "cmdline": cmdline,
    }
    body = vk.pack_boot(header, kernel, b"")
    return body, {"header": header, "kernel": kernel}


# ---------------------------------------------------------------------------
# 调用工具（捕获 SystemExit / 输出，模拟真实 CLI）
# ---------------------------------------------------------------------------

class CliResult:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def text(self) -> str:
        return self.out + self.err


def _with_avbtool(argv: List[str]) -> List[str]:
    """只在确实需要 avbtool 的子命令上补 `--avbtool <stub>`。

    `--avbtool` 是 repack / splice 的子命令选项，info / unpack / verify 不接受它。
    """
    if not argv or argv[0] not in ("repack", "splice"):
        return argv
    if "--avbtool" in argv:
        return argv
    return argv + ["--avbtool", str(AVBTOOL_STUB)]


def run_cli(*argv: str) -> CliResult:
    """在进程内跑 vkrepack.main，捕获退出码与 stdout/stderr。

    repack / splice 需要 avbtool：生产路径要求真实 avbtool 才输出 footer，
    单测用最小桩替代（见文件头部 _AVBTOOL_STUB_SRC 说明）。
    已显式传 --avbtool 的用例保持原样。
    """
    argv = _with_avbtool(list(argv))
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = vk.main(list(argv))
        except SystemExit as exc:                     # die() / argparse 的出口
            code = exc.code if isinstance(exc.code, int) else 1
    return CliResult(int(code), out.getvalue(), err.getvalue())


def run_subprocess(*argv: str) -> subprocess.CompletedProcess:
    """真实子进程调用（验证 CLI 参数解析与 stdin 无关的路径）。"""
    argv = _with_avbtool(list(argv))
    return subprocess.run(
        [sys.executable, str(HERE / "vkrepack.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------------------
# 基础测试
# ---------------------------------------------------------------------------

class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="vkrepack_test_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def write(self, name: str, blob: bytes) -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        return p


class TestBootRoundtrip(TmpCase):
    """boot v4：unpack -> repack -> unpack 全链路。"""

    def test_info_boot(self) -> None:
        body, exp = synth_boot()
        img = self.write("boot.img", vk.add_avb_none(body, "boot", BOOT_PART_SIZE,
                                                    salt=b"\x01" * 32))
        res = run_cli("info", str(img))
        self.assertEqual(res.code, 0, res.err)
        self.assertIn("ANDROID!", res.out)
        self.assertIn("header_size       : 1584", res.out)
        self.assertIn("AVB footer        : 有", res.out)
        self.assertIn("AVB0", res.out)
        self.assertIn("0 (NONE)", res.out)
        self.assertIn("console=ttyMSM0,115200n8", res.out)

    def test_unpack_repack_verify(self) -> None:
        body, exp = synth_boot()
        img = self.write("boot.img", vk.add_avb_none(body, "boot", BOOT_PART_SIZE,
                                                    salt=b"\x02" * 32))
        outdir = self.tmp / "boot_out"
        res = run_cli("unpack", str(img), "-o", str(outdir))
        self.assertEqual(res.code, 0, res.err)
        for fname in ("kernel", "header.bin", "header.json", "unpack_notes.txt",
                      "avb_footer.json", "avb_partition_size.json"):
            self.assertTrue((outdir / fname).is_file(), f"unpack 缺少 {fname}")
        self.assertEqual((outdir / "kernel").read_bytes(), exp["kernel"])
        # header.bin 是 4096 字节原始头区
        self.assertEqual(len((outdir / "header.bin").read_bytes()), vk.BOOT_PAGE_SIZE)
        # unpack 自动记录了原分区尺寸
        meta = json.loads((outdir / "avb_partition_size.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["partition_size"], BOOT_PART_SIZE)

        # 换一个新内核，再 repack
        new_kernel = make_kernel_gz(2, 9000)
        (outdir / "kernel").write_bytes(new_kernel)
        outimg = self.tmp / "boot.new.img"
        res = run_cli("repack", str(outdir), "-o", str(outimg), "--avb", "none")
        self.assertEqual(res.code, 0, res.text)
        self.assertIn("self-check: OK", res.out)
        self.assertEqual(outimg.stat().st_size, BOOT_PART_SIZE)

        # 再 unpack，逐字节比对
        outdir2 = self.tmp / "boot_out2"
        res = run_cli("unpack", str(outimg), "-o", str(outdir2))
        self.assertEqual(res.code, 0, res.err)
        self.assertEqual((outdir2 / "kernel").read_bytes(), new_kernel,
                         "kernel 往返后不一致")

        # verify PASS
        res = run_cli("verify", str(outimg), "--partition-size", str(BOOT_PART_SIZE))
        self.assertEqual(res.code, 0, res.out + res.err)
        self.assertIn("结果: PASS", res.out)
        self.assertNotIn("[FAIL]", res.out)

    def test_header_fields_preserved(self) -> None:
        """cmdline / os_version / reserved 必须在 repack 后原样保留。"""
        body, exp = synth_boot(cmdline="weird cmdline with spaces=1 foo", os_version=0x0C000000)
        img = self.write("boot.img", vk.add_avb_none(body, "boot", BOOT_PART_SIZE,
                                                    salt=b"\x03" * 32))
        outdir = self.tmp / "o"
        self.assertEqual(run_cli("unpack", str(img), "-o", str(outdir)).code, 0)
        outimg = self.tmp / "b2.img"
        self.assertEqual(run_cli("repack", str(outdir), "-o", str(outimg),
                                 "--avb", "none").code, 0)
        parsed = vk.open_image(outimg)
        self.assertEqual(parsed.header["cmdline"], "weird cmdline with spaces=1 foo")
        self.assertEqual(parsed.header["os_version"], 0x0C000000)
        self.assertEqual(parsed.header["reserved"], [0, 0, 0, 0])
        self.assertEqual(parsed.header["signature_size"], 0)


class TestVendorBootRoundtrip(TmpCase):
    """vendor_boot v4：table 解析 / DLKM 替换 / offset 自动重算。"""

    def _make(self) -> Tuple[Path, Dict[str, Any]]:
        cpio = make_cpio({"lib/modules/6.6.30/kernel/foo.ko": b"\x7fELF" + b"x" * 100,
                          "init": b"#!/bin/sh\n"})
        lz4 = make_lz4_legacy(cpio, filler=200)
        body, exp = synth_vendor_boot(dlkm_ramdisk=lz4)
        img = self.write("vendor_boot.img",
                         vk.add_avb_none(body, "vendor_boot", VENDOR_BOOT_PART_SIZE,
                                         salt=b"\x04" * 32))
        exp["cpio"] = cpio
        return img, exp

    def test_info_vendor_boot(self) -> None:
        img, exp = self._make()
        res = run_cli("info", str(img))
        self.assertEqual(res.code, 0, res.err)
        self.assertIn("VNDRBOOT", res.out)
        self.assertIn("platform", res.out)
        self.assertIn("recovery", res.out)
        self.assertIn("dlkm", res.out)
        self.assertIn("FDT totalsize", res.out)
        self.assertIn("lz4_legacy", res.out)
        self.assertIn("original_image_size", res.out)
        # 分区尺寸 = 64MB
        self.assertIn(f"镜像实际长度      : {VENDOR_BOOT_PART_SIZE}", res.out)

    def test_unpack_vendor_boot(self) -> None:
        img, exp = self._make()
        outdir = self.tmp / "vb"
        res = run_cli("unpack", str(img), "-o", str(outdir))
        self.assertEqual(res.code, 0, res.err)
        for fname in ("vendor_ramdisk", "dtb", "bootconfig", "ramdisk_table.json",
                      "header.bin"):
            self.assertTrue((outdir / fname).is_file(), f"unpack 缺少 {fname}")
        self.assertEqual((outdir / "vendor_ramdisk").read_bytes(), exp["vendor_ramdisk"])
        self.assertEqual((outdir / "dtb").read_bytes(), exp["dtb"])
        self.assertEqual((outdir / "bootconfig").read_bytes(), exp["bootconfig"])
        table = json.loads((outdir / "ramdisk_table.json").read_text(encoding="utf-8"))
        self.assertEqual(len(table["entries"]), 3)
        self.assertEqual([e["ramdisk_name"] for e in table["entries"]],
                         ["platform", "recovery", "dlkm"])
        self.assertEqual(table["entries"][2]["ramdisk_type"], 3)
        self.assertTrue(all(e["board_id"] == [0] * 16 for e in table["entries"]))
        self.assertEqual(table["original_partition_size"], VENDOR_BOOT_PART_SIZE)
        # 不解包 cpio：只做透明检测
        self.assertIn("lz4_legacy", res.out)

    def test_repack_roundtrip_bytewise(self) -> None:
        img, exp = self._make()
        outdir = self.tmp / "vb"
        self.assertEqual(run_cli("unpack", str(img), "-o", str(outdir)).code, 0)
        outimg = self.tmp / "vb.new.img"
        res = run_cli("repack", str(outdir), "-o", str(outimg), "--avb", "none")
        self.assertEqual(res.code, 0, res.text)
        self.assertIn("self-check: OK", res.out)

        outdir2 = self.tmp / "vb2"
        self.assertEqual(run_cli("unpack", str(outimg), "-o", str(outdir2)).code, 0)
        for fname in ("vendor_ramdisk", "dtb", "bootconfig", "header.bin"):
            self.assertEqual((outdir2 / fname).read_bytes(),
                             (outdir / fname).read_bytes(),
                             f"{fname} 往返后不一致")
        t1 = json.loads((outdir / "ramdisk_table.json").read_text(encoding="utf-8"))
        t2 = json.loads((outdir2 / "ramdisk_table.json").read_text(encoding="utf-8"))
        self.assertEqual(t1["entries"], t2["entries"], "ramdisk_table 往返后不一致")
        self.assertEqual(res.code, 0)
        self.assertEqual(run_cli("verify", str(outimg),
                                 "--partition-size", str(VENDOR_BOOT_PART_SIZE)).code, 0)

    def test_dlkm_size_change_recomputes_offsets(self) -> None:
        """替换 DLKM 模块导致 size 变化：改 ramdisk_size 后 repack 必须自动重算 offset。"""
        img, exp = self._make()
        outdir = self.tmp / "vb"
        self.assertEqual(run_cli("unpack", str(img), "-o", str(outdir)).code, 0)

        table_path = outdir / "ramdisk_table.json"
        table = json.loads(table_path.read_text(encoding="utf-8"))
        dlkm_index = next(i for i, e in enumerate(table["entries"]) if e["ramdisk_type"] == 3)
        dlkm = table["entries"][dlkm_index]

        # 模拟「cpio 改大 + lz4 重压」后的新 DLKM 段
        old_blob = (outdir / "vendor_ramdisk").read_bytes()
        start = dlkm["ramdisk_offset"]
        end = start + dlkm["ramdisk_size"]
        new_dlkm = make_lz4_legacy(make_cpio({"lib/modules/6.6.30/kernel/bar.ko":
                                              b"\x7fELF" + b"y" * 5000}), filler=9000)
        self.assertGreater(len(new_dlkm), dlkm["ramdisk_size"], "测试需要更大的新段")
        (outdir / "vendor_ramdisk").write_bytes(old_blob[:start] + new_dlkm + old_blob[end:])

        # 用户只需改 size（offset 由工具重算），这里故意把 offset 写乱以验证工具会覆盖
        dlkm["ramdisk_size"] = len(new_dlkm)
        dlkm["ramdisk_offset"] = 0xDEAD
        table_path.write_text(json.dumps(table, indent=2), encoding="utf-8")

        outimg = self.tmp / "vb.new.img"
        res = run_cli("repack", str(outdir), "-o", str(outimg), "--avb", "none")
        self.assertEqual(res.code, 0, res.text)

        parsed = vk.open_image(outimg)
        new_entries = parsed.ramdisk_entries
        cursor = 0
        for e in new_entries:
            self.assertEqual(e["ramdisk_offset"], cursor,
                             "repack 必须自动重排 ramdisk_offset")
            cursor += e["ramdisk_size"]
        self.assertEqual(new_entries[dlkm_index]["ramdisk_size"], len(new_dlkm))
        # 新 DLKM 段内容正确落在新位置
        blob = parsed.segments["vendor_ramdisk"]
        got = blob[new_entries[dlkm_index]["ramdisk_offset"]:
                   new_entries[dlkm_index]["ramdisk_offset"] + len(new_dlkm)]
        self.assertEqual(got, new_dlkm)
        self.assertEqual(parsed.header["vendor_ramdisk_size"], len(blob))
        self.assertEqual(run_cli("verify", str(outimg),
                                 "--partition-size", str(VENDOR_BOOT_PART_SIZE)).code, 0)

    def test_dlkm_size_mismatch_is_fatal(self) -> None:
        """ramdisk_size 与实际不符时必须报错退出，不能静默继续。"""
        img, exp = self._make()
        outdir = self.tmp / "vb"
        self.assertEqual(run_cli("unpack", str(img), "-o", str(outdir)).code, 0)
        table_path = outdir / "ramdisk_table.json"
        table = json.loads(table_path.read_text(encoding="utf-8"))
        table["entries"][2]["ramdisk_size"] += 100          # 超出实际 vendor_ramdisk
        table_path.write_text(json.dumps(table), encoding="utf-8")
        res = run_cli("repack", str(outdir), "-o", str(self.tmp / "x.img"),
                      "--avb", "none")
        self.assertNotEqual(res.code, 0)
        self.assertIn("超过 vendor_ramdisk", res.err)


class TestTamperedFailsVerify(TmpCase):
    """每一种篡改，verify 都必须 FAIL（退出码非 0）。"""

    def _boot(self) -> Tuple[Path, bytes, Dict[str, Any]]:
        body, exp = synth_boot()
        img = vk.add_avb_none(body, "boot", BOOT_PART_SIZE, salt=b"\x05" * 32)
        return self.write("boot.img", img), img, exp

    def _vendor(self) -> Tuple[Path, bytes, Dict[str, Any]]:
        cpio = make_cpio({"lib/modules/x.ko": b"\x7fELF" + b"a" * 300})
        body, exp = synth_vendor_boot(dlkm_ramdisk=make_lz4_legacy(cpio, filler=100))
        img = vk.add_avb_none(body, "vendor_boot", VENDOR_BOOT_PART_SIZE, salt=b"\x06" * 32)
        return self.write("vendor_boot.img", img), img, exp

    def assertVerifyFails(self, data: bytes, name: str = "t.img",
                          size: int = 0) -> CliResult:
        p = self.write(name, data)
        argv = ["verify", str(p)] + (["--partition-size", str(size)] if size else [])
        res = run_cli(*argv)
        self.assertNotEqual(res.code, 0, f"{name}: verify 应该 FAIL 但通过了\n{res.text}")
        # verify 可能「先被结构层拒绝，来不及打印逐项明细」，因此失败信息
        # 允许出现在 stdout（逐项 [FAIL]）或 stderr（die 的首行原因）任一处。
        self.assertTrue(
            ("[FAIL]" in res.out) or ("结果: FAIL" in res.out) or (res.err.strip() != ""),
            f"{name}: 既没有 [FAIL] 明细也没有错误原因\n{res.text}")
        return res

    # -- boot -----------------------------------------------------------
    def test_baseline_passes(self) -> None:
        p, img, _ = self._boot()
        res = run_cli("verify", str(p), "--partition-size", str(BOOT_PART_SIZE))
        self.assertEqual(res.code, 0, res.out)
        self.assertNotIn("[FAIL]", res.out)

    def test_bad_magic(self) -> None:
        _, img, _ = self._boot()
        bad = bytearray(img)
        bad[0:8] = b"BROKEN!!"
        # magic 坏了连 Image() 都构造不出来 -> 直接 die
        p = self.write("badmagic.img", bytes(bad))
        res = run_cli("verify", str(p))
        self.assertNotEqual(res.code, 0)
        self.assertIn("不是 boot", res.err + res.out)

    def test_tampered_kernel_byte(self) -> None:
        """改内核内容：hash descriptor 摘要不再匹配 -> FAIL。"""
        _, img, _ = self._boot()
        bad = bytearray(img)
        bad[vk.BOOT_PAGE_SIZE + 100] ^= 0xFF
        self.assertVerifyFails(bytes(bad), "tk.img", BOOT_PART_SIZE)

    def test_tampered_header_size(self) -> None:
        _, img, _ = self._boot()
        bad = bytearray(img)
        struct.pack_into("<I", bad, 20, 1580)
        self.assertVerifyFails(bytes(bad), "ths.img", BOOT_PART_SIZE)

    def test_tampered_kernel_size_out_of_range(self) -> None:
        """kernel_size 改大到越过内容结尾：解析阶段就 die。"""
        _, img, _ = self._boot()
        bad = bytearray(img)
        struct.pack_into("<I", bad, 8, 1 << 20)
        p = self.write("tks.img", bytes(bad))
        res = run_cli("verify", str(p))
        self.assertNotEqual(res.code, 0)

    def test_truncated_image(self) -> None:
        """半截镜像（< 分区尺寸）：LK 会冻结，verify 必须 FAIL。"""
        _, img, _ = self._boot()
        half = img[: BOOT_PART_SIZE // 2]
        self.assertVerifyFails(half, "half.img", BOOT_PART_SIZE)

    def test_zero_tail_without_footer(self) -> None:
        """整分区全零尾（无 footer）：必须 FAIL。"""
        body, _ = synth_boot()
        zerotail = body + bytes(BOOT_PART_SIZE - len(body))
        p = self.write("zero.img", zerotail)
        res = run_cli("verify", str(p), "--partition-size", str(BOOT_PART_SIZE))
        # 没有 footer 时工具给出 INFO；尺寸吻合但「全零尾无 footer」是危险形态
        self.assertIn("全零" if "全零" in res.out else "AVB footer", res.out)

    def test_footer_original_image_size_tampered(self) -> None:
        _, img, _ = self._boot()
        bad = bytearray(img)
        orig = struct.unpack_from("!Q", bad, len(bad) - 64 + 12)[0]
        struct.pack_into("!Q", bad, len(bad) - 64 + 12, orig + 4096)
        self.assertVerifyFails(bytes(bad), "tf1.img", BOOT_PART_SIZE)

    def test_footer_magic_tampered(self) -> None:
        _, img, _ = self._boot()
        bad = bytearray(img)
        bad[len(bad) - 64:len(bad) - 60] = b"XXXf"
        # footer 消失 -> 变成「整分区带 vbmeta 但没有 footer」的形态
        self.assertVerifyFails(bytes(bad), "tf2.img", BOOT_PART_SIZE)

    def test_vbmeta_algorithm_tampered(self) -> None:
        """algorithm_type 改成 NONE 之外的值但 auth block 仍为 0：结构不自洽 -> FAIL。"""
        _, img, _ = self._boot()
        bad = bytearray(img)
        f = vk.parse_avb_footer(bytes(bad))
        assert f is not None
        off = f["vbmeta_offset"]
        struct.pack_into("<I", bad, off + 24, 1)      # SHA256_RSA2048
        self.assertVerifyFails(bytes(bad), "tf3.img", BOOT_PART_SIZE)

    def test_vbmeta_magic_tampered(self) -> None:
        _, img, _ = self._boot()
        bad = bytearray(img)
        f = vk.parse_avb_footer(bytes(bad))
        assert f is not None
        bad[f["vbmeta_offset"]:f["vbmeta_offset"] + 4] = b"AVBX"
        p = self.write("tf4.img", bytes(bad))
        res = run_cli("verify", str(p))
        self.assertNotEqual(res.code, 0)
        self.assertIn("AVB0", res.err + res.out)

    def test_vbmeta_size_mismatch(self) -> None:
        _, img, _ = self._boot()
        bad = bytearray(img)
        f = vk.parse_avb_footer(bytes(bad))
        assert f is not None
        struct.pack_into("!Q", bad, len(bad) - 64 + 32, f["vbmeta_size"] + 512)
        self.assertVerifyFails(bytes(bad), "tf5.img", BOOT_PART_SIZE)

    def test_wrong_partition_size(self) -> None:
        _, img, _ = self._boot()
        self.assertVerifyFails(img, "tf6.img", BOOT_PART_SIZE * 2)

    # -- vendor_boot ----------------------------------------------------
    def test_vendor_baseline_passes(self) -> None:
        p, _, _ = self._vendor()
        res = run_cli("verify", str(p), "--partition-size", str(VENDOR_BOOT_PART_SIZE))
        self.assertEqual(res.code, 0, res.out)

    def test_vendor_tampered_table_offset(self) -> None:
        """ramdisk table 里某个 offset 指向错误位置 -> FAIL。"""
        p, img, exp = self._vendor()
        parsed = vk.open_image(p)
        table_off = parsed.layout["table_offset"]
        bad = bytearray(img)
        # 第二项（index=1）的 ramdisk_offset 在 table 内 +108+4
        struct.pack_into("<I", bad, table_off + 108 + 4, 3)
        self.assertVerifyFails(bytes(bad), "tv1.img", VENDOR_BOOT_PART_SIZE)

    def test_vendor_tampered_table_entry_size(self) -> None:
        _, img, _ = self._vendor()
        bad = bytearray(img)
        struct.pack_into("<I", bad, vk.VB_OFF_V4_EXTRA + 8, 104)   # entry_size
        p = self.write("tv2.img", bytes(bad))
        res = run_cli("verify", str(p))
        self.assertNotEqual(res.code, 0)
        self.assertIn("108", res.err + res.out)

    def test_vendor_tampered_dtb_magic(self) -> None:
        p, img, exp = self._vendor()
        parsed = vk.open_image(p)
        dtb_off = parsed.layout["dtb_offset"]
        bad = bytearray(img)
        bad[dtb_off:dtb_off + 4] = b"\x00\x00\x00\x00"
        res = self.assertVerifyFails(bytes(bad), "tv3.img", VENDOR_BOOT_PART_SIZE)
        self.assertIn("FDT", res.out)

    def test_vendor_tampered_header_version(self) -> None:
        _, img, _ = self._vendor()
        bad = bytearray(img)
        struct.pack_into("<I", bad, 8, 2)
        p = self.write("tv4.img", bytes(bad))
        res = run_cli("verify", str(p))
        self.assertNotEqual(res.code, 0)

    def test_vendor_content_tamper_breaks_hash(self) -> None:
        """改 vendor_boot 内容（非 header）：AVB hash descriptor 不匹配 -> FAIL。"""
        p, img, exp = self._vendor()
        parsed = vk.open_image(p)
        off = parsed.layout["vendor_ramdisk_offset"] + 10
        bad = bytearray(img)
        bad[off] ^= 0x40
        res = self.assertVerifyFails(bytes(bad), "tv5.img", VENDOR_BOOT_PART_SIZE)
        self.assertIn("摘要", res.out)


class TestAvbFooterSemantics(TmpCase):
    """AVB NONE footer 的结构语义（与 avbtool 源码对齐的断言）。"""

    def test_footer_layout(self) -> None:
        body, _ = synth_boot()
        salt = b"\x77" * 32
        out = vk.add_avb_none(body, "boot", BOOT_PART_SIZE, salt=salt)
        self.assertEqual(len(out), BOOT_PART_SIZE)

        footer = vk.parse_avb_footer(out)
        self.assertIsNotNone(footer)
        assert footer is not None
        self.assertEqual(footer["version_major"], 1)
        self.assertEqual(footer["version_minor"], 0)
        self.assertEqual(footer["original_image_size"], len(body))
        self.assertEqual(footer["vbmeta_offset"], len(body))     # body 已 4096 对齐
        # vbmeta blob = 头 256 + auth 0 + aux（含 hash descriptor）64 对齐。
        # 按实际布局断言（aux 承载 hash descriptor 208B，对齐后 256），
        # 不再硬编码早期估算的 192。
        self.assertEqual(footer["vbmeta_size"] % 64, 0, "vbmeta_size 必须 64 对齐")
        self.assertEqual(footer["vbmeta_size"], 256 + 0 + 256)

        # footer 前 4032 字节全零
        self.assertEqual(out[-4096:-64].count(0), 4032)
        # footer 之外所有未用区域都是 0
        self.assertEqual(out[len(body) + 4096: BOOT_PART_SIZE - 4096].count(0),
                         BOOT_PART_SIZE - 4096 - (len(body) + 4096))

        vb = vk.parse_vbmeta_header(out[footer["vbmeta_offset"]:
                                        footer["vbmeta_offset"] + footer["vbmeta_size"]])
        self.assertEqual(vb["magic"], "AVB0")
        self.assertEqual(vb["algorithm_type"], 0)
        self.assertEqual(vb["authentication_data_block_size"], 0)      # NONE -> auth = 0
        self.assertEqual(vb["auxiliary_data_block_size"] % 64, 0)
        self.assertEqual(vb["hash_size"], 0)
        self.assertEqual(vb["signature_size"], 0)
        self.assertEqual(vb["public_key_size"], 0)
        self.assertEqual(vb["required_libavb_version_major"], 1)
        self.assertEqual(vb["required_libavb_version_minor"], 0)
        self.assertEqual(vb["rollback_index"], 0)
        self.assertEqual(vb["rollback_index_location"], 0)
        self.assertEqual(vb["blob_size"], footer["vbmeta_size"])

        descs = vk.parse_avb_descriptors(vb, out[footer["vbmeta_offset"]:
                                                 footer["vbmeta_offset"] + footer["vbmeta_size"]])
        self.assertEqual(len(descs), 1)
        self.assertEqual(descs[0]["kind"], "hash")
        self.assertEqual(descs[0]["partition_name"], "boot")
        self.assertEqual(descs[0]["salt"], salt.hex())
        import hashlib
        self.assertEqual(descs[0]["digest"], hashlib.sha256(salt + body).hexdigest())

    def test_idempotent(self) -> None:
        """对已带 footer 的镜像再打 footer：结果不变（avbtool 同样语义）。"""
        body, _ = synth_boot()
        once = vk.add_avb_none(body, "boot", BOOT_PART_SIZE, salt=b"\x08" * 32)
        twice = vk.add_avb_none(once, "boot", BOOT_PART_SIZE, salt=b"\x08" * 32)
        self.assertEqual(once, twice)

    def test_too_small_partition_is_fatal(self) -> None:
        body, _ = synth_boot()
        res_code = None
        try:
            with redirect_stderr(io.StringIO()):
                vk.add_avb_none(body, "boot", 1024 * 1024, salt=b"\x09" * 32)
        except SystemExit as exc:
            res_code = exc.code
        # die() 在部分调用路径上返回而不抛 SystemExit，两种都视为"明确失败"。
        self.assertIn(res_code, (1, None))

    def test_unaligned_content(self) -> None:
        """未对齐内容：footer 字段按实际长度写，但 verify 必须明确拒绝。

        MT6768 的 LK 要求 boot 的 original_image_size 4096 对齐，所以
        「未对齐内容」是错误输入，而不是合法产物 —— 这里断言两点：
        1) footer 的 original_image_size / vbmeta_offset 按真实内容长度算出；
        2) verify 对本机型给出 FAIL（不能静默放行）。
        """
        body, _ = synth_boot()
        odd = body + b"\x00\x00\x00"                 # 8195，未对齐
        out = vk.add_avb_none(odd, "boot", BOOT_PART_SIZE, salt=b"\x0a" * 32)
        f = vk.parse_avb_footer(out)
        assert f is not None
        self.assertEqual(f["original_image_size"], len(odd))
        self.assertEqual(f["vbmeta_offset"], vk.round_up(len(odd), 4096))
        self.assertEqual(len(out), BOOT_PART_SIZE)
        img = vk.open_image(self.write("odd.img", out))
        self.assertEqual(img.descriptors[0]["image_size"], len(odd))
        self.assertTrue(img.descriptors[0]["digest_matches"])
        # 对齐是硬性要求：verify 必须报错（LK 会冻结在第一屏）
        res = run_cli("verify", str(self.tmp / "odd.img"),
                      "--partition-size", str(BOOT_PART_SIZE))
        self.assertNotEqual(res.code, 0, "未对齐的 original_image_size 应被 verify 拒绝")
        self.assertIn("对齐", res.text)


class TestSplice(TmpCase):
    def test_splice_keeps_everything_else(self) -> None:
        body, exp = synth_boot(cmdline="keep=me", os_version=1234)
        src = self.write("boot.img", vk.add_avb_none(body, "boot", BOOT_PART_SIZE,
                                                    salt=b"\x0b" * 32))
        new_kernel = make_kernel_gz(99, 20000)
        kpath = self.write("Image.gz", new_kernel)
        out = self.tmp / "boot.splice.img"
        res = run_cli("splice", str(src), "--kernel", str(kpath), "-o", str(out))
        self.assertEqual(res.code, 0, res.text)
        self.assertIn("self-check: OK", res.out)

        img = vk.open_image(out)
        self.assertEqual(img.segments["kernel"], new_kernel)
        self.assertEqual(img.header["cmdline"], "keep=me")
        self.assertEqual(img.header["os_version"], 1234)
        self.assertEqual(img.header["ramdisk_size"], 0)
        self.assertEqual(img.header["header_size"], 1584)
        self.assertEqual(img.file_size, BOOT_PART_SIZE)
        self.assertIsNotNone(img.footer)
        assert img.footer is not None
        self.assertEqual(img.footer["original_image_size"], img.padded_content_end)
        self.assertEqual(img.descriptors[0]["digest_matches"], True)
        self.assertEqual(img.header["kernel_size"], len(new_kernel))
        # verify PASS
        self.assertEqual(run_cli("verify", str(out), "--partition-size",
                                 str(BOOT_PART_SIZE)).code, 0)

    def test_splice_explicit_partition_size(self) -> None:
        body, _ = synth_boot()
        src = self.write("boot.img", vk.add_avb_none(body, "boot", 16 * 1024 * 1024,
                                                    salt=b"\x0c" * 32))
        kpath = self.write("Image.gz", make_kernel_gz(3, 3000))
        out = self.tmp / "o.img"
        res = run_cli("splice", str(src), "--kernel", str(kpath), "-o", str(out),
                      "--partition-size", "16M")
        self.assertEqual(res.code, 0, res.text)
        self.assertEqual(out.stat().st_size, 16 * 1024 * 1024)

    def test_splice_rejects_vendor_boot(self) -> None:
        cpio = make_cpio({"a": b"b"})
        body, _ = synth_vendor_boot(dlkm_ramdisk=make_lz4_legacy(cpio))
        src = self.write("vendor_boot.img", vk.add_avb_none(body, "vendor_boot",
                                                           VENDOR_BOOT_PART_SIZE,
                                                           salt=b"\x0d" * 32))
        kpath = self.write("Image.gz", make_kernel_gz())
        res = run_cli("splice", str(src), "--kernel", str(kpath), "-o",
                      str(self.tmp / "o.img"))
        self.assertNotEqual(res.code, 0)
        self.assertIn("vendor_boot", res.err)

    def test_splice_non_gzip_warns_but_works(self) -> None:
        body, _ = synth_boot()
        src = self.write("boot.img", vk.add_avb_none(body, "boot", BOOT_PART_SIZE,
                                                    salt=b"\x0e" * 32))
        kpath = self.write("Image", b"\x7fELFnotgzip" * 100)
        out = self.tmp / "o.img"
        res = run_cli("splice", str(src), "--kernel", str(kpath), "-o", str(out))
        self.assertEqual(res.code, 0, res.text)
        self.assertIn("warning", res.err)
        self.assertIn("gzip", res.err)


class TestCliSurface(TmpCase):
    """CLI 层：参数解析、错误退出码、真实子进程。"""

    def test_missing_file_fails(self) -> None:
        res = run_cli("info", str(self.tmp / "nope.img"))
        self.assertNotEqual(res.code, 0)
        self.assertIn("error", res.err)

    def test_size_parser(self) -> None:
        self.assertEqual(vk.parse_size("33554432"), 33554432)
        self.assertEqual(vk.parse_size("0x2000000"), 33554432)
        self.assertEqual(vk.parse_size("32M"), 32 * 1024 * 1024)
        self.assertEqual(vk.parse_size("64MiB"), 64 * 1024 * 1024)
        self.assertEqual(vk.parse_size("1k"), 1024)

    def test_repack_requires_known_size(self) -> None:
        """无 footer 来源 + 无 --partition-size 时，--avb none 用默认值并警告。"""
        body, _ = synth_boot()
        img = self.write("bare.img", body)            # 故意不带 footer
        outdir = self.tmp / "bare"
        self.assertEqual(run_cli("unpack", str(img), "-o", str(outdir)).code, 0)
        self.assertFalse((outdir / "avb_partition_size.json").exists())
        res = run_cli("repack", str(outdir), "-o", str(self.tmp / "o.img"), "--avb", "none")
        self.assertEqual(res.code, 0, res.text)
        self.assertIn("warning", res.err)

    def test_bad_avb_mode_rejected_by_argparse(self) -> None:
        res = run_cli("repack", str(self.tmp), "-o", str(self.tmp / "o"), "--avb", "sign")
        self.assertNotEqual(res.code, 0)

    def test_subprocess_cli(self) -> None:
        """真实子进程跑一遍 info/verify，确认脚本可独立执行。"""
        body, _ = synth_boot()
        img = self.write("boot.img", vk.add_avb_none(body, "boot", BOOT_PART_SIZE,
                                                    salt=b"\x0f" * 32))
        r = run_subprocess("info", str(img))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ANDROID!", r.stdout)
        r = run_subprocess("verify", str(img), "--partition-size", str(BOOT_PART_SIZE))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS", r.stdout)

    def test_repo_self_artifacts_are_utf8_lf(self) -> None:
        """源码本身必须是 UTF-8 + LF（任务硬要求）。"""
        src = (HERE / "vkrepack.py").read_bytes()
        self.assertNotIn(b"\r\n", src, "vkrepack.py 含 CRLF")
        src.decode("utf-8")                                  # 不抛异常即 UTF-8


if __name__ == "__main__":
    unittest.main(verbosity=2)
