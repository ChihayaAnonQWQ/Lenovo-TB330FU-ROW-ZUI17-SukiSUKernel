#!/usr/bin/env python3
"""compare-crc.py —— 校验内核 Module.symvers 与厂商模块 CRC 需求是否一致。

用法:
    python3 compare-crc.py <Module.symvers> [vendor-crc.json]

退出码: 0=全部一致, 1=存在不一致或缺失
"""
import json, sys
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("用法: compare-crc.py <Module.symvers> [vendor-crc.json]", file=sys.stderr)
        return 2
    symvers = Path(sys.argv[1]).expanduser().resolve()
    crcjson = Path(sys.argv[2]).expanduser().resolve() if len(sys.argv) > 2 else None
    if crcjson is None:
        # 依次尝试：从 symvers 路径上溯找 ref/，以及当前工作目录下的 ref/
        for cand in (symvers.parent.parent.parent / 'ref' / 'vendor-crc.json',
                     Path.cwd() / 'ref' / 'vendor-crc.json'):
            if cand.is_file():
                crcjson = cand
                break
    if crcjson is None or not crcjson.is_file():
        print("找不到 vendor-crc.json", file=sys.stderr)
        return 2
    if not symvers.is_file():
        print(f"找不到 {symvers}", file=sys.stderr)
        return 2

    print(f"symvers : {symvers}")
    print(f"crc表   : {crcjson}")
    need = json.loads(crcjson.read_text())
    ours = {}
    exported_by = {}
    for line in symvers.read_text(errors='replace').splitlines():
        p = line.split('\t')
        if len(p) >= 3 and p[0].startswith('0x'):
            try:
                ours[p[1]] = int(p[0], 16)
            except ValueError:
                continue
            exported_by[p[1]] = p[2]

    have = set(need) & set(ours)
    missing = set(need) - set(ours)
    match = [s for s in have if ours[s] == need[s]]
    mism = [(s, ours[s], need[s]) for s in have if ours[s] != need[s]]

    print(f"\n模块需要符号 : {len(need)}")
    print(f"内核已导出   : {len(have)}")
    print(f"内核未导出   : {len(missing)}  (可能由其他 .ko 提供)")
    print(f"\n✓ CRC 一致   : {len(match)}")
    print(f"✗ CRC 不一致 : {len(mism)}")
    if mism:
        print("\n=== 不一致清单 ===")
        for s, a, b in sorted(mism):
            print(f"  {s:<48} ours={a:#010x} need={b:#010x} ({exported_by.get(s,'?')})")
        return 1
    print("\n结果: PASS")
    return 0

if __name__ == '__main__':
    sys.exit(main())