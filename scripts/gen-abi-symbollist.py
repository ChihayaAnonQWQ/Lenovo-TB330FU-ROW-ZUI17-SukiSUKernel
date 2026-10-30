#!/usr/bin/env python3
"""gen-abi-symbollist.py —— 生成内核 TRIM_UNUSED_KSYMS 所需的符号白名单。

★ 格式极其重要：CONFIG_UNUSED_KSYMS_WHITELIST 要的是**裸符号名，一行一个**，
  不能带 [abi_symbol_list] 表头、不能有缩进。带表头/缩进会让 TRIM 认不出符号，
  大量导出被裁掉 -> 厂商 .ko 因 Unknown symbol 拒载 -> init panic。

★ 来源优先级：
  1. --base 指定的基准白名单（取自已验证可启动构建），确定性最好
  2. 否则从 android/abi_gki_aarch64* 推导

  历史教训：从 ABI 文件推导曾在相同输入下产出 4106/4102 两种结果，
  导致 usb_gadget_frame_number 等符号被裁。所以默认用基准白名单。

用法:
    gen-abi-symbollist.py --kernel <源码树> --crc <vendor-crc.json> \\
        --base <基准白名单> --out <输出文件>
"""
import argparse
import json
import sys
from pathlib import Path

ABI_FILES = ("abi_gki_aarch64", "abi_gki_aarch64_mtk", "abi_gki_aarch64_lenovo")


def read_list(path: Path) -> set:
    """读白名单类文件；跳过表头和注释，自动 strip。"""
    out = set()
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith(("[", "#")):
            out.add(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", required=True, help="内核源码树根目录")
    ap.add_argument("--crc", required=True, help="vendor-crc.json")
    ap.add_argument("--base", default=None, help="基准白名单（可选，优先级最高）")
    ap.add_argument("--out", required=True, help="输出文件")
    args = ap.parse_args()

    ksrc = Path(args.kernel)
    out = Path(args.out)

    need = set(json.loads(Path(args.crc).read_text()))

    src = None
    base = set()
    if args.base:
        bp = Path(args.base)
        if bp.is_file():
            base = read_list(bp)
            src = f"基准白名单({bp.name})"

    if src is None:
        for name in ABI_FILES:
            p = ksrc / "android" / name
            if p.is_file():
                base |= read_list(p)
        src = "ABI 文件"

    syms = sorted(base | need)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="\n") as f:
        for s in syms:
            f.write(s + "\n")

    print(f"  来源 {src}: {len(base)} ∪ 模块需要 {len(need)} = {len(syms)} 个符号（裸格式）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
