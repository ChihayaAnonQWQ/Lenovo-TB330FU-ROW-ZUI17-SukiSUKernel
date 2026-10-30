# vkrepack.py 使用说明

> boot v4 / vendor_boot v4 的解包·重打包·替换内核·AVB footer 工具
> 针对 **Lenovo TB330FU（MT6768 / Helio G88，Android 15）**
> 纯 Python 3、仅标准库、无第三方依赖

---

## 1. 它解决什么问题

| 场景 | 命令 |
|---|---|
| 只想换内核，其它一切不动 | `splice` |
| 要看清镜像结构（分区/DTB/fragment/AVB） | `info` |
| 要改 vendor_boot 里的某个 fragment | `unpack` → 改 → `repack` |
| 刷前自检（尺寸/对齐/哈希） | `verify` |

**核心约束（本机实测得出）**：
- **镜像必须等于分区尺寸**（boot 32 MiB / vendor_boot 64 MiB）。半截镜像或整分区全零尾都会让 LK **冻结在第一屏**。
- **必须有结构合法的 AVB footer**（哪怕 `algorithm=NONE`）。
- **AVB footer 一律由官方 `avbtool` 生成，本工具不手搓字节**。找不到 avbtool 时**拒绝输出**（fail-closed）。

---

## 2. 本机真实形态（已实测，用作断言基准）

### boot_a（32 MiB）
```
header_version = 4, header_size = 1584, page_size = 4096
kernel_offset  = 4096, kernel_size = 14,623,212 (gzip)
ramdisk_size   = 0                     ← 原厂 boot 没有 ramdisk
AVB footer     = 有, original_image_size = 14,647,296
algorithm      = SHA256_RSA4096        ← 原厂是真签名
```

### vendor_boot_a（64 MiB）
```
header_version = 4, header_size = 2128, page_size = 4096
kernel_addr = 0x40080000, ramdisk_addr = 0x47c80000
tags_addr   = 0x4bc80000, dtb_addr     = 0x4bc80000   ← MTK 非零（AOSP 规范要求 0）
cmdline     = "bootopt=64S3,32N2,64N2"
vendor_ramdisk = 27,586,716 B (lz4_legacy)
dtb            = 156,675 B, magic 0xd7b7ab1e          ← Android DT-table，不是裸 FDT
ramdisk_table  = 3 条 × 108 B
AVB footer     = algorithm NONE, original_image_size = 27,758,592
```

**ramdisk table（原厂）**

| idx | name | type | size | offset |
|---|---|---|---|---|
| 0 | `""` | platform(1) | 7,081,946 | 0 |
| 1 | `recovery` | recovery(2) | 17,732,269 | 7,081,946 |
| 2 | **`init_boot`** | platform(1) | 2,772,501 | 24,814,215 |

> **SukiSU 的注入点是 #2 `init_boot`**（不是主体盘）。修补后 2,772,501 → 3,326,204，
> 而 #0/#1 的 size/offset **一字节未变**，dtb 段内容不变、位置后移。

---

## 3. 安装与自检

```bash
# 需要 python3（≥3.8）与官方 avbtool
pip install avbtool
#   或：git clone https://android.googlesource.com/platform/external/avb
#      然后 --avbtool /path/to/avb/avbtool.py

python vkrepack.py --help
python test_vkrepack.py          # 41 项单测，应全 OK
```

---

## 4. 子命令

### 4.1 `info` —— 看清结构

```bash
python vkrepack.py info boot_a.bin
python vkrepack.py info vendor_boot_a.bin
python vkrepack.py info vendor_boot_a.bin --json > info.json
```

输出包含：头字段、段偏移表、ramdisk table（含 **SukiSU 会选中哪个 fragment**）、
AVB footer 字段、vbmeta 头、所有 descriptor。

### 4.2 `unpack` —— 拆开

```bash
python vkrepack.py unpack vendor_boot_a.bin -o vb_a
```

产出：
```
vb_a/
  header.bin           原始头区（4096 字节）
  header.json          全部头字段
  vendor_ramdisk       全部 fragment 首尾相接的原始字节
  dtb                  DTB 段（DT-table 格式）
  bootconfig           bootconfig 段
  ramdisk_table.json   每条 fragment 的 name/type/size/offset/board_id
  avb_footer.json      footer 字段
  avb_partition_size.json  自动记录的分区尺寸（repack 时自动使用）
  unpack_notes.txt     外部解压命令提示
```

解压 fragment（工具只做**透明检测**，不代做）：
```bash
# vendor_ramdisk 是 lz4_legacy（本机）
lz4 -d -l vb_a/vendor_ramdisk ramdisk.cpio
mkdir -p root && cd root && cpio -idmv --no-absolute-filenames < ../ramdisk.cpio
# 换模块后重打：
find . -print0 | cpio -o -H newc --null -R 0:0 > ../new.cpio
lz4 -l -12 --favor-decSpeed ../new.cpio new_ramdisk.lz4
```

### 4.3 `repack` —— 重新打包

```bash
python vkrepack.py repack vb_a -o vendor_boot_a.new.img --avb none
```

- `--avb none`：补零到分区尺寸 → 调 `avbtool add_hash_footer --algorithm NONE`
- `--avb keep`：复用原 footer 字节（**仅当内容长度等于原 `vbmeta_offset` 时可用**）
- `--partition-size`：缺省时读 `avb_partition_size.json`
- **会自动重算** ramdisk table 的 offset（改 fragment 尺寸后无需手工改）

**改动 fragment 尺寸后**，必须同步更新 `ramdisk_table.json` 里的 `ramdisk_size`，
工具会据此重算 offset；若声明总长超过实际 `vendor_ramdisk` 字节数则报错退出。

### 4.4 `splice` —— 最常用：只换内核

```bash
gzip -9 -c out/arch/arm64/boot/Image > Image.gz
python vkrepack.py splice boot_a.bin --kernel Image.gz \
    -o boot_a.custom.img \
    --partition-size 33554432 \
    --avb none \
    --ro-prop "ro.build.fingerprint=Lenovo/TB330FU_PRC/TB330FU:15/AP3A.240905.015.A2/S2003138_261010_ROW:user/release-keys" \
    --ro-prop "ro.build.version.security_patch=2026-10-05"
```

- 只替换 `kernel` 段，**cmdline / os_version / ramdisk / 分区尺寸 / 头字段全部沿用原镜像**
- 输出自动补零到分区尺寸并重建 footer
- 非 gzip 内核只给 **warning**（不阻止），但请确认真机是否需要 gzip
- **拒绝**对 vendor_boot 使用 splice（会明确报错）

### 4.5 `verify` —— 刷前自检

```bash
python vkrepack.py verify boot_a.custom.img --partition-size 33554432
python vkrepack.py verify vendor_boot_a.new.img --partition-size 67108864
```

检查项（boot 30 项 / vendor_boot 55 项）：magic、header_size、页对齐、
段偏移单调性、table 首尾相接/不重叠/在范围内、对齐填充全零、
AVB footer 字段、vbmeta 算法与块对齐、hash descriptor 摘要匹配、镜像尺寸。

**必须看到 `结果: PASS` 才能刷。**

---

## 5. 完整流程（改内核）

```bash
# 0) 备份（已有：D:\TB330FU备份\boot_a.bin 等）
# 1) 解包看结构
python vkrepack.py info boot_a.bin
# 2) 换内核
gzip -9 -c Image > Image.gz
python vkrepack.py splice boot_a.bin --kernel Image.gz -o boot_a.custom.img \
    --partition-size 33554432 --avb none --ro-prop "ro.build.fingerprint=<你的>" \
    --ro-prop "ro.build.version.security_patch=<你的>"
# 3) 自检
python vkrepack.py verify boot_a.custom.img --partition-size 33554432
# 4) 刷
fastboot flash boot_a boot_a.custom.img
# 5) 关校验
fastboot --disable-verity --disable-verification flash vbmeta_a vbmeta_a.bin
python mtk.py da vbmeta 3
# 6) 重启验收
fastboot reboot && adb wait-for-device
adb shell getprop sys.boot_completed      # 期望 1
adb shell su -c id                        # 期望 uid=0
```

---

## 6. 已知的“FAIL 是正常的”情形

| 现象 | 原因 | 是否要处理 |
|---|---|---|
| `Sukisu BOOT\boot_a.img` 报 `hash descriptor 摘要与内容相符 [False]` | **SukiSU 改完没重算 footer**（当前系统就靠 orange 解锁态跳过校验） | 是——重编内核后用 `splice` 重建 footer |
| 同上，vendor_boot 版 | SukiSU 改 fragment #2 后 `original_image_size` 还是旧的 27,758,592 | 同上 |
| `current-slot` 为空 | 非 A/B 或 bootctl 缺失 | 本机是 `_a`，正常 |

> ⚠️ **本机原厂 boot 是 `SHA256_RSA4096` 真签名**，不是 NONE。
> `verify` 对签名镜像只做 info 提示；自编镜像会降级为 `NONE`（orange 解锁态可启动，已由社区实测）。

---

## 7. 设计原则

1. **AVB footer 只信 avbtool**：找不到就拒绝输出，绝不手搓字节。
2. **不做“猜测性修复”**：结构不对就 FAIL，不自动补全。
3. **repack 全程保留原头字段**：`kernel_addr`/`dtb_addr`/`page_size`/`board_id` 一个不改。
4. **MTK 变体单独处理**：`kernel_addr != 0`、`dtb` 是 DT-table 而非裸 FDT —— 都是正常的，不判错。
5. **写文件用 LF**：避免 Windows CRLF 污染脚本。
