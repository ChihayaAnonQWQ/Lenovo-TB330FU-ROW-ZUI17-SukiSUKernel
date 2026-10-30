# 02 — vendor_boot 重打包与 AVB 处理（Lenovo TB330FU / MT6768 / Android 15）

> 目标设备：Lenovo Tab M11 **TB330FU**，MT6768（Helio G88），Android 15 / ZUI 17.0.074，内核 `6.6.118-android15-8`，A/B + 动态分区（super），BL 已解锁、已 Root。
>
> **本文的"已核实"定义**：能指向 AOSP / avbtool / Magisk / SukiSU / mtkclient **源码或官方文档**原文，或指向 TB330FU 专属的设备树/实操记录。
> **"推测"**：由已核实事实推出、但未有该机型原始 dump 直接佐证（例如没有实际读到本机 `vendor_boot.img`）。
>
> 全文所有引用的源码均为本次实际抓取（curl/android.googlesource `?format=TEXT` 解码 / raw.githubusercontent.com），非记忆复述。

---

## 0. 针对 TB330FU 的预判结论（先看这里）

| 项 | 结论 | 依据/状态 |
|---|---|---|
| 平台代号 | `mt6768`，板名 `barley_row_wifi` | TWRP 设备树 `TARGET_BOARD_PLATFORM := mt6768`、`TARGET_BOOTLOADER_BOARD_NAME := barley_row_wifi`（已核实） |
| 是否存在 `dtbo` 分区 | **存在**，独立分区 `dtbo`（是否 A/B 未在 fstab 标注 slotselect，需以本机 GPT 为准） | freeradical recovery.fstab 中有 `/dtbo … /dev/block/by-name/dtbo`（已核实存在该分区名） |
| 是否存在独立 `dtb` 分区 | fstab 中**未见** `dtb` 分区；`dtb` 以 **vendor_boot 头部 `dtb_size/dtb_addr` 段** 形式存在 | fstab 全量列表无 `dtb`（已核实）；`--dtb` 出现在 BoardConfig 的 mkbootimg 参数中（已核实） |
| 是否有 `init_boot` 分区 | **未出现**；generic ramdisk 仍在 `boot`/`vendor_boot` | fstab 无 `init_boot`（已核实） |
| `vendor_boot` 是否 A/B | 有 `vendor_boot`（fstab 无 slotselect 标志），但实际上板上 MTK 通常为 `vendor_boot_a/_b`；XDA 线程与 SukiSU 的 `by-name/vendor_boot{_a/_b}` 命名均印证 | **推测**（需本机 `ls -l /dev/block/by-name/` 确认） |
| 已确认的实测 root 路径 | LK（`lk_a`/`lk_b`）必须打补丁，否则刷完出现 `dm-verity corruption`；刷写时用 `fastboot flash --disable-verity --disable-verification` | threadexio/tb330fu（TB330FU 实操记录，已核实） |
| A15 上 Magisk 官方无法自动定位 ramdisk | 需要 patch `vendor_boot` 里的 `init_boot.cpio`（Magisk issue #8835 同款机型行为：`vendor_ramdisk/{init_boot,ramdisk,recovery}.cpio`） | **推测**（TB330FU 的 XDA 线程与 Magisk #9263 主题一致，但未读到完整日志） |

---

## 1. vendor_boot v4 结构（Android 13+ / Android 15）

### 1.1 头部字段（已核实，AOSP 原文）

AOSP《Vendor boot partitions》给出 v3/v4 头部。**v3 头部 2112 字节，v4 头部 2128 字节**（多出 4 个 `uint32_t`）。

证据: https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions（**已核实**）

v4 头部（`struct vendor_boot_img_hdr_v4`，继承 v3 后追加 4 字段）：

| 偏移 | 字段 | 说明 |
|---|---|---|
| 0 | `magic[8]` | `"VNDRBOOT"` |
| 8 | `header_version` | 4 |
| 12 | `page_size` | 厂商页大小；**boot.img v4 固定 4096，但 vendor_boot v4 的 page_size 由厂商定**（`bootimg.h` 注释原文：*"Note that in version 4 of the boot image header, page size is fixed at 4096 bytes"* 只针对 boot；vendor_boot 头部仍是 `page_size` 字段，`q/r/s` 页数按它计算） |
| 16 | `kernel_addr` | 物理加载地址 |
| 20 | `ramdisk_addr` | vendor ramdisk 加载地址 |
| 24 | `vendor_ramdisk_size` | **所有 fragment 的总字节数（不是页数）** |
| 28 | `cmdline[2048]` | vendor cmdline |
| 2076 | `tags_addr` | |
| 2080 | `name[16]` | 产品名 |
| 2096 | `header_size` | 2128 |
| 2100 | `dtb_size` | |
| 2104 | `dtb_addr` (u64) | |
| **2112** | `vendor_ramdisk_table_size` | v4 新增 |
| **2116** | `vendor_ramdisk_table_entry_num` | v4 新增 |
| **2120** | `vendor_ramdisk_table_entry_size` | v4 新增（固定 `sizeof(vendor_ramdisk_table_entry_v4)`） |
| **2124** | `bootconfig_size` | v4 新增 |

证据（字段顺序 + v4 布局注释 + 4 个新字段）: https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/include/bootimg/bootimg.h（**已核实**，本次抓取 `boot_img_hdr_v4`/`vendor_boot_img_hdr_v4` 定义）

### 1.2 镜像物理布局（已核实）

| 段 | 页数 | 说明 |
|---|---|---|
| vendor boot header | `o = (2128 + page_size - 1) / page_size` | v4 |
| vendor ramdisk section | `p = (vendor_ramdisk_size + page_size - 1) / page_size` | 多个 fragment **首尾相接**，无各自对齐 |
| dtb | `q = (dtb_size + page_size - 1) / page_size` | |
| vendor ramdisk table | `r = (vendor_ramdisk_table_size + page_size - 1) / page_size` | 在 dtb **之后** |
| bootconfig | `s = (vendor_bootconfig_size + page_size - 1) / page_size` | 最末 |

证据: https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions（**已核实**）
证据（写盘顺序在源码中一一对应）: https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/mkbootimg.py — `write_vendor_boot_data()` 依次写 `ramdisks → dtb → entries(table) → bootconfig`（**已核实**）

### 1.3 vendor_ramdisk_table 条目（已核实）

```c
struct vendor_ramdisk_table_entry_v4 {
    uint32_t ramdisk_size;                 /* 该 fragment 字节数 */
    uint32_t ramdisk_offset;               /* 相对 vendor ramdisk section 起始的偏移 */
    uint32_t ramdisk_type;                 /* 0/1/2/3 */
    uint8_t  ramdisk_name[32];             /* asciiz，唯一 */
    uint32_t board_id[16];                 /* 硬件标识，厂商自定义编码 */
};
```
`ramdisk_type`：`NONE=0`、`PLATFORM=1`、`RECOVERY=2`、`DLKM=3`
证据: https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions 与 bootimg.h（**已核实**）

**关于"MTK 的 vendor `.ko` 通常放哪个 type"——重要纠偏：**
- AOSP 的语义：`DLKM` = *"ramdisks contain dynamic loadable kernel modules"*；**`vendor_ramdisk`（即 `--vendor_ramdisk` 传入的那个主体 fragment）在 mkbootimg 里被硬编码为 `PLATFORM`**，其 `ramdisk_name` 被写成空字符串 `b''`。
  ```python
  if args.vendor_ramdisk is not None:
      vendor_ramdisk_table_builder.add_entry(
          args.vendor_ramdisk.name, VENDOR_RAMDISK_TYPE_PLATFORM, b'', None)
  ```
  证据: https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/mkbootimg.py（**已核实**，第 451–454 行区）
- **结论**：用 `mkbootimg --vendor_boot` 传统方式重打包时，`/lib/modules/*.ko` 位于**主体 `PLATFORM` fragment 内**；只有当设备把模块拆成独立 fragment（`BOARD_VENDOR_RAMDISK_FRAGMENT.xxx.KERNEL_MODULE_DIRS`）时才会出现 `DLKM` 条目。
- **TB330FU 的实际情形需要 dump 验证**：用 `unpack_bootimg --format=info` 看 `vendor ramdisk table` 的 `ramdisk_type` 列表。若只有 1 个条目且 name 为空 → 就是 PLATFORM 主体盘（**推测**，符合非 GKI-fragment 化的 MTK 设备习惯）。
- `/vendor/lib/modules` 与 DLKM fragment 的关系：**DLKM fragment 解压后也是覆盖到根 `/`**，因此其内容通常是 `lib/modules/<kver>/…` 路径；运行时 dm-verity 保护的 `/vendor/lib/modules` 是**独立只读分区的内容**，并非 ramdisk 内容，二者靠 overlay/同名路径竞争，**不要**指望改 ramdisk 里的 lib/modules 能替换掉 `/vendor` 分区里的 `system_dlkm`/`vendor_dlkm` 模块（**推测**，AOSP 未明说 MTK 具体实现）。
- AOSP 还明确：DLKM fragment 的默认 `--ramdisk_type` 就是 `DLKM`；`--ramdisk_name default` **被禁止**（`VENDOR_RAMDISK_NAME_BLOCKLIST = {b'default'}`）。
  证据: AOSP vendor-boot-partitions（*"For DLKM vendor ramdisk, the default --ramdisk_type would be DLKM"*）+ mkbootimg.py `VENDOR_RAMDISK_NAME_BLOCKLIST`（**已核实**）

### 1.4 mkbootimg 版本要求（已核实）

| AOSP 分支 | 支持 v4？ | 证据 |
|---|---|---|
| `android-11.0.0_r1` | **否**，只有 v3（`VENDOR_BOOT_IMAGE_HEADER_V3_SIZE = 2112`，无 `vendor_ramdisk_table`） | **已核实**（本次抓取该 tag 的 `mkbootimg.py`，`vendor_ramdisk_table` 出现 0 次） |
| `android-12.0.0_r1` | **是**（`vendor_ramdisk_table` 出现 10 次） | **已核实** |
| `android15-release` | **是**，且新增 `--vendor_bootconfig` 的实际写盘 | **已核实** |

> **实操结论**：不要用 Debian/Ubuntu 包里的老 `mkbootimg`、也不要用 Android-10/11 时代的 prebuilt。请直接从 `android15-release` 分支取 `mkbootimg.py` + `unpack_bootimg.py`（纯 Python3，无依赖），或用 Android 15 的 `system/tools/mkbootimg` 源码目录。

### 1.5 可执行命令（推荐"无损往返"法：**已核实** AOSP 官方推荐做法）

AOSP `unpack_bootimg.py` 自带 `--format=mkbootimg`，能直接吐出可回灌的 mkbootimg 参数，这比手写所有字段安全得多（避免 `dtb_addr`/`board_id`/`page_size` 写错导致变砖）：

```bash
# 0) 取工具（android-15 分支）
for f in mkbootimg.py unpack_bootimg.py; do
  curl -s "https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/$f?format=TEXT" | base64 -d > $f
done

# 1) 解包（AOSP 官方文档给出的标准往返流程）
python unpack_bootimg.py --boot_img vendor_boot_a.img --out vb_out
python unpack_bootimg.py --boot_img vendor_boot_a.img --out vb_out --format=mkbootimg \
  | tee mkbootimg_args

#    vb_out/ 内容（v4）：dtb, bootconfig, vendor_ramdisk00, vendor_ramdisk01...
#    以及按 ramdisk_name 命名的软链（如 vendor_ramdisk02 -> ramdisk_<name>）
#    每个 fragment 的 type/name/board_id 都已在 mkbootimg_args 里复现

# 2) 修改 fragment 后重打包（关键：--vendor_boot + --header_version 4）
python mkbootimg.py --header_version 4 --vendor_boot vendor_boot_a.repacked.img  $(cat mkbootimg_args)

# 3) 自检：解包重打包结果，确认头部字段一致
python unpack_bootimg.py --boot_img vendor_boot_a.repacked.img --out vb_check --format=info
cmp <(python unpack_bootimg.py --boot_img vendor_boot_a.img --out a --format=mkbootimg) \
    <(python unpack_bootimg.py --boot_img vendor_boot_a.repacked.img --out b --format=mkbootimg)
```

证据:
- 官方往返示例与参数说明: https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/unpack_bootimg.py （`get_unpack_usage()`，**已核实**：`mkbootimg $(cat mkbootimg_args) --vendor_boot repacked.img`）
- `--header_version` / `--vendor_boot` / `--vendor_ramdisk_fragment` / `--ramdisk_type` / `--ramdisk_name` / `--board_id{0..15}` / `--vendor_bootconfig` 参数定义: https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/mkbootimg.py （**已核实**）

**手写 fragment 形式的等价命令（需要替换主体盘时用这个形状）：**

```bash
python mkbootimg.py \
  --header_version 4 \
  --pagesize 2048 \
  --kernel_offset 0x00008000 --ramdisk_offset 0x01000000 \
  --tags_offset 0x00000100 --dtb_offset 0x01f00000 \
  --vendor_cmdline "bootopt=64S3,32N2,64N2 buildvariant=user" \
  --board barley_row_wifi \
  --dtb vb_out/dtb \
  --vendor_bootconfig vb_out/bootconfig \
  --ramdisk_type 3 --ramdisk_name dlkm_extra \
  --board_id0 0xF00BA5 --board_id1 0xC0FFEE \
  --vendor_ramdisk_fragment my_dlkm.cpio.lz4 \
  --vendor_boot vendor_boot_a.repacked.img
```
> ⚠️ 上表中的 `--kernel_offset/--ramdisk_offset/--tags_offset/--dtb_offset/--pagesize` **必须**从 `unpack_bootimg --format=info` 或 `mkbootimg_args` 里照抄本机真实值，**不要**用示例值。TB330FU 的 boot 参考值（TWRP 设备树，仅供参考）：`BOARD_KERNEL_BASE := 0x40078000`、`BOARD_RAMDISK_OFFSET := 0x07c08000`、`BOARD_KERNEL_TAGS_OFFSET := 0x0bc08000`、`BOARD_KERNEL_PAGESIZE := 2048`。
> 证据: https://github.com/twrpdtgen/android_device_lenovo_TB330FU/blob/master/BoardConfig.mk （**已核实**；注意该文件还写着 `BOARD_BOOTIMG_HEADER_VERSION := 2` / `BOARD_INCLUDE_DTB_IN_BOOTIMG := true`，那是 twrpdtgen 自动从**旧版/误判**的镜像或模板推出来的，与 Android 15 + vendor_boot 的现实不符，**不可信**，务必以本机 dump 为准）

---

## 2. MTK 的 kernel_dtb / dtbo 分区

### 2.1 TB330FU 实际布局的取证方式（可执行）

```bash
# A) 分区名清单（最权威，来自本机 GPT）
adb shell ls -l /dev/block/by-name/
#   关注：boot_a/boot_b, vendor_boot_a/b, init_boot?, dtbo, dtb?, lk/lk_a/lk_b, vbmeta*, super...

# B) 槽位与分区大小
adb shell getprop ro.boot.slot_suffix
fastboot getvar all 2>&1 | grep -Ei "slot|partition-type:dtb|partition-size:dtb|has-slot"

# C) vendor_boot 里到底有没有 dtb 段（关键判据）
adb shell "su -c 'dd if=/dev/block/by-name/vendor_boot\$(getprop ro.boot.slot_suffix) of=/data/local/tmp/vb.img bs=1M'"
adb pull /data/local/tmp/vb.img
python unpack_bootimg.py --boot_img vb.img --out vb_out --format=info
#   看 "dtb size" 是否 > 0；看 "vendor ramdisk table" 的条目数与 type

# D) dtbo 分区内容是不是 dt_table（magic 0xd7b7ab1e）
adb shell "su -c 'dd if=/dev/block/by-name/dtbo of=/data/local/tmp/dtbo.img bs=1M'"
adb pull /data/local/tmp/dtbo.img
xxd -l 16 dtbo.img     # 前 4 字节若为 1e ab b7 d7，则是 dt_table_header
```
证据（`dt_table_header.magic = DT_TABLE_MAGIC 0xd7b7ab1e`，且 "dt_table_header 只用于 dtb/dtbo 分区"）: https://source.android.com/docs/core/architecture/dto/partitions（**已核实**）
证据（`unpack_bootimg` 会打印 `dtb size` / `vendor ramdisk table size` / 每个 entry 的 type/name/board_id）: https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/unpack_bootimg.py（**已核实**，`VendorBootImageInfo.format_pretty_text()`）

**TB330FU 已知的分区名（来自已核实的 recovery.fstab，全量节选）**：
`system/vendor/product（logical, slotselect）`、`md_udc`、`userdata`、`protect1/protect2`、`nvdata`、`nvcfg`、`lenovocust`、`persist`、`frp`、`nvram`、`proinfo`、`lenovoraw`、**`lk`/`lk2`**、`misc`、`para`、**`vendor_boot`**、**`boot`（slotselect）**、`vbmeta_vendor`、`vbmeta_system`、`logo`、`expdb`、`seccfg`、`tee1/tee2`、`scp1/scp2`、`sspm_1/sspm_2`、`md1img`、`md1dsp`、`gz1/gz2`、`spmfw`、`boot_para`、**`dtbo`**、`otp`、**`vbmeta`**。
证据: https://github.com/twrpdtgen/android_device_lenovo_TB330FU/blob/master/recovery.fstab （**已核实**；twrpdtgen 自动生成，分区名可信度高，但 slotselect 标注可能不准）

> 注意：该 fstab 中 `dtbo`、`vendor_boot`、`vbmeta` **都没有** `slotselect` 标志，但同表的 `boot`、`vbmeta_system`、`vbmeta_vendor` 有。MTK + A/B 的通常做法是*所有*这些都有 `_a/_b`。**以第 A 步实测为准**（此前 `dmesg`/XDA 线程与 SukiSU 的路径拼接 `vendor_boot{_a|_b}` 都暗示是 A/B，因此**推测** fstab 标注缺省）。

### 2.2 `dtb` 与 `dtbo` 的区别（已核实）

| | `dtb` | `dtbo` |
|---|---|---|
| 内容 | **基础设备树**（完整、可独立启动的 FDT） | **overlay**（`/plugin/` 片段，只描述差异），apply 到基础 FDT 上 |
| v3/v4 设备上的位置 | 放在 `vendor_boot` 的 `dtb` 段（`dtb_size`/`dtb_addr`）；旧设备也可在 `boot` v2 里 | 独立分区 `dtbo`（dt_table 格式），或 `recovery_dtbo` |
| 格式 | 裸 FDT（magic `0xd00dfeed`） | `dt_table_header`(magic `0xd7b7ab1e`) + N×`dt_table_entry` + N 个 dtbo |
| 选择机制 | 单条（vendor_boot 场景），或 dt_table 内的 `id/rev/custom[]` | `dt_table_entry.id/rev/custom[0..3]` 由 bootloader 匹配 |
| 工具 | `dtc` / `mkdtimg` / `mkbootimg --dtb` | `mkdtimg create/cfg_create`、`dtc -@` |
| AVB | 作为 `vendor_boot` 分区的一部分被整分区签名 | **是独立分区，通常有 `dtbo` 的 hashtree descriptor（`--include_descriptors_from_image dtbo.img`）** |

证据（dt_table 结构与 id/rev/custom 的匹配语义、"dt_table_header 只用于 dtb/dtbo 分区"、`mkdtimg create --id/--rev/--custom0..3`）: https://source.android.com/docs/core/architecture/dto/partitions（**已核实**）
证据（GKI 把 DTB 移入 vendor_boot）: https://source.android.com/docs/core/architecture/bootloader/dtb-images 与 https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions（**已核实**："Because the DTB image contains vendor-specific data, it's now part of the vendor boot partition"）

**AVB 是否覆盖 dtbo**：
- 机制上**可以也必须**（`dtbo.img` 通常带 hashtree/hash footer，其 descriptor 被顶层 `vbmeta` 通过 `--include_descriptors_from_image` 收录）。
- TB330FU 是否这么做**需实测**：
  ```bash
  python avbtool.py info_image --image dtbo.img | head -40   # 看是否有 Footer/AvbHashtreeDescriptor
  python avbtool.py info_image --image vbmeta_a.img | grep -i -A3 dtbo
  ```
  证据（`--include_descriptors_from_image` 的语义 + 同名分区只保留最后一个 descriptor）: https://android.googlesource.com/platform/external/avb/+/refs/heads/main/avbtool.py 与 README（**已核实**）

### 2.3 MTK lk/preloader 依据什么选 DTB 条目

**两条独立路径，必须分清：**

**(a) 现代 LK 从 `boot`/`vendor_boot` 取 DTB —— 已核实（源码级）**
MTK LK 的 `boot_linux()` 在 64-bit kernel 分支里，从 `kernel` 镜像**末尾向前**扫描 FDT magic，找到 dtb 后拷到 `fdt`：
```c
for (dtb_size = 0; dtb_size < zimage_size; dtb_size++, addr--) {
    magic = (unsigned char *)addr;
    if (*(magic+3)==0xED && *(magic+2)==0xFE && *(magic+1)==0x0D && *(magic+0)==0xD0) {
        dtb_addr = addr; ... memcpy_u8(fdt, (void*)dtb_addr, dtb_size); break;
    }
}
...
#ifdef MTK_DTBO_FEATURE
    rtn = dtb_overlay(fdt, dtb_size);   // load_overlay_dtbo("odmdtbo", ...) → apply_overlay()
#endif
```
证据: https://github.com/dguidipc/gemini-lk/blob/master/lk/app/mt_boot/mt_boot.c — `boot_linux()` 与 `dtb_overlay()`、`load_overlay_dtbo("odmdtbo", &overlay_len)`（**已核实**源码，但这是 gemini-lk（非 TB330FU）的公开 LK 分支，MTK LK 各平台代码高度同源，**逻辑可参考、具体分区名需以本机 lk 为准**）

> **注意**：这段老 LK 代码是"boot.img 里 kernel+append dtb"的路径；A15 + vendor_boot 的镜像由 LK 的 vendor_boot 解析器负责读 `dtb_size/dtb_addr`（`#ifdef USE_ITS_BOOTIMG` / `g_smem_off` 分支），也就是**头部字段驱动**，不再依赖扫描。

**(b) preloader/LK 的多 DTB 选择 —— `platform_id / soc_rev / version_id`，以及 dt_table 的 `id/rev/custom[]`**
同一份 LK 源码里多 DTB 分支是：
```c
struct dtb_header *hdr = g_smem_off;  // multi-dtb found
struct dtb_entry *dtb_ptr = (struct dtb_entry *)(g_smem_off + sizeof(struct dtb_header));
for (i=0; i<hdr->num_of_dtbs; i++) {
    if (dtb_ptr->platform_id == platform_id && dtb_ptr->soc_rev == soc_rev
        && dtb_ptr->version_id == version_id) { memcpy(fdt, ...); break; }
    dtb_ptr++;
}
```
证据: 同上 `mt_boot.c`（**已核实**该结构体/字段名；**推测**其值源自 IDME（`proinfo`/`otp`）分区，因为源码注释写着 `// These should be got from IDME???`）
- 对**独立 `dtbo` 分区**，选择依据是 AOSP 标准的 `dt_table_entry{ id, rev, custom[4] }`，这些值通常由 `mkdtimg create --id=/:board_id --rev=/:board_rev --custom0=...` 从 DTS 里抽取，或直接由 scatter/firmware 指定。
  证据: https://source.android.com/docs/core/architecture/dto/partitions（**已核实**）
- **`dtb_idx`**：这是 MTK 下载/烧写工具链（SP Flash Tool / `mtk` scatter / `dtboimg` 打包）里的**打包索引**，不是 LK 运行时概念。在 A15 时代它更像"dtb 在 dt_table 里的第 N 条"。**未找到 LK 源码中出现名为 `dtb_idx` 的变量**（本次 grep 该 LK 源码无命中）→ **未找到可靠来源**（建议直接对本机 `lk_a.bin` 用 `strings`/Ghidra 搜 `dtb_idx` 或 `DTB_IDX` 验证）。
- LK 镜像本体是**打包容器**：modern LK 含 `lk`, `bl2_ext`, `aee`, `lk_main_dtb`, `lk_dtbo` 五个子分区，各自带 header + （多数）两段 cert。可用 `lkpatcher --list-partitions` 列出。
  证据: https://blog.r0rt1z2.com/posts/reverse-engineering-mediatek-lk/（**已核实**，原文 `python3 -m lkpatcher lk.bin --list-partitions` 示例输出）
  ```bash
  python3 -m lkpatcher lk_a.bin --list-partitions
  ```
  > `lk_dtbo`（**推测**）就是 LK 自己的 overlay（例如 `odmdtbo`），与内核 `dtbo` 分区不是一回事。

---

## 3. AVB 与验证流程

### 3.1 解锁 BL 后 `fastboot flash` 一个未签名/签名不同的分区会怎样

**官方语义（已核实）**：
> "When a device powers on, the bootloader first checks if a device is LOCKED or UNLOCKED. **If a device is UNLOCKED, the bootloader shows the user a warning and then proceeds to boot even if the loaded OS isn't signed by the root of trust.** If the device is LOCKED, the bootloader goes through the steps in Verifying Boot to verify…"

证据: https://source.android.com/docs/security/features/verifiedboot/device-state（**已核实**）
另有："use `ro.boot.verifiedbootstate` to set `ro.boot.flash.locked` to 0; this unlocks the bootloader if the verified boot state is **orange**" → 解锁态即 **ORANGE**，AVB 校验被跳过。
证据: https://source.android.com/docs/core/architecture/bootloader/locking_unlocking（**已核实**）

**因此**：
- 解锁态下刷入**未签名 / 签名不同**的 `boot`/`vendor_boot`，**不会被 AVB 拒绝**，能正常启动（否则所有自定义内核/GSI 都无法刷）。
- 但**dm-verity 仍可能生效**：解锁 ≠ 自动关 verity。若 `vbmeta` 未置 `HASHTREE_DISABLED` 位，内核侧 `androidboot.veritymode=enforcing`，一旦被校验的**只读分区**（system/vendor/product 等，非 boot）数据不符 → `dm-verity corruption` → restart/EIO（表现成 bootloop 或反复重启）。这解释了"改 boot 没问题，一旦动到 system 侧就炸"的现象。
- 只有置了 `AVB_VBMETA_IMAGE_FLAGS_HASHTREE_DISABLED`（=bit0）才会：
  > "if the `AVB_VBMETA_IMAGE_FLAGS_HASHTREE_DISABLED` flag is set in the top-level vbmeta, then `androidboot.veritymode` is set to **disabled** and `androidboot.veritymode.managed` and `androidboot.vbmeta.invalidate_on_error` are unset."
  证据: https://android.googlesource.com/platform/external/avb/+/refs/heads/main/README.md（**已核实**，`Handling dm-verity Errors` 节）

### 3.2 为什么经验上仍需 `mtk.py da vbmeta 3`（已核实到源码级）

**"3" 的含义 = `DISABLE_VERITY(1) | DISABLE_VERIFICATION(2)`**，即同时关 verity 和 verification。
mtkclient 源码：
```python
def patch_vbmeta(self, vbmeta: bytes, vbmode: int):
    DISABLE_VERITY = 1
    DISABLE_VERIFICATION = 2
    ...
    vbmeta[0x78:0x78 + 4] = int.to_bytes(vbmode, 4, 'big')   # 大端写入 flags
    return vbmeta

def da_vbmeta(self, vbmode: int = 3, display: bool = True):
    ...
    slot = self.get_current_slot()
    partition = "vbmeta" + slot            # ← 只改当前槽的 vbmeta(_a 或 _b)
    ... readflash → patch_vbmeta → writeflash
```
证据: https://raw.githubusercontent.com/bkerler/mtkclient/main/mtkclient/Library/DA/mtk_da_handler.py （**已核实**，函数 `patch_vbmeta` / `da_vbmeta`）

**这个 `0x78` 偏移与 AVB 结构完全对得上**：`AvbVBMetaImageHeader` 中 `flags` 是 `/* 120: */ AvbVBMetaImageFlags`（120 = 0x78）。fastboot 用的 `123 + vbmeta_offset` 是**大端 4 字节中最低字节的偏移**（120+3），本质相同。
证据: https://android.googlesource.com/platform/external/avb/+/refs/heads/main/libavb/avb_vbmeta_image.h （`/* 120: Flags from the AvbVBMetaImageFlags enumeration…*/`，**已核实**）
证据: https://raw.githubusercontent.com/aosp-mirror/platform_system_core/master/fastboot/fastboot.cpp — `uint64_t flags_offset = 123 + vbmeta_offset; if (g_disable_verity) data[flags_offset] |= 0x01; if (g_disable_verification) data[flags_offset] |= 0x02;`（**已核实**）

**为什么 MTK 上"已经 `fastboot --disable-verity` 了还要 mtk.py da vbmeta 3"？** 逐条给可核实的原因：

1. **`fastboot --disable-verity` 只对"你这次 `flash` 的那一条命令"生效，且只改这一个分区的 vbmeta buffer**。看 fastboot 源码：flags 是在 `flash_buf()` 里对**正在刷的 buffer** 就地打 patch：
   ```cpp
   if (g_disable_verity || g_disable_verification) {
     if (apply_vbmeta) rewrite_vbmeta_buffer(buf, false /* vbmeta_in_boot */);
     else if (!has_vbmeta_partition() && (partition=="boot"||...)) rewrite_vbmeta_buffer(buf, true);
   }
   ```
   即：`fastboot --disable-verity flash vbmeta_a vbmeta.img` 只会把 **vbmeta_a** 的 flags 打上；不带 `--disable-verity` 的 `fastboot flash vendor_boot_a xxx.img` **不会**动 vbmeta。若中间任何人/任何脚本（或厂商的 `flash_all.bat`、ota 包里的 vbmeta）重刷了 `vbmeta`，flags 就被还原。（**已核实**，fastboot.cpp 上述片段）
2. **`has_vbmeta_partition()` 会改变行为**：MTK 有独立 `vbmeta` 分区 → `apply_vbmeta` 为真 → 只有当你刷的分区名是 `vbmeta`/`vbmeta_a`/`vbmeta_b` 时才打 patch。刷 boot/vendor_boot 时**完全不碰 vbmeta**。（**已核实**，同上）
3. **`--disable-verity` 不重算签名**：它只是把 flags 字节 OR 上 1/2，**签名 block 保持原厂商签名**。这在解锁态可行（avb 校验被跳过），但意味着这批 flags 依赖"BL 保持 unlocked"。若 `seccfg` 状态被重置/重锁，flags 虽在对但签名已不匹配 → 校验失败。（**推测**，基于 device-state 文档的"UNLOCKED 才跳过校验"推出）
4. **`da_vbmeta` 走的是 BROM/Preloader + DA 直写 flash**，绕开 fastboot 协议，也绕开 fastboot 对"分区表槽位/后缀"的猜测；它在**下载模式**下按 GPT 直读直写，对 MTK 的 `vbmeta`/`vbmeta_a`/`vbmeta_b` 布局更稳。（**已核实** 源码用的是 `daloader.readflash/writeflash` + `get_partition_data(parttype="user")`）
5. **只改当前 slot**：`partition = "vbmeta" + slot`。若你切槽/OTA 后跑到另一槽，另一槽的 `vbmeta_b` 仍是 enforcing → 需要再跑一次，或显式切槽后跑。（**已核实**，源码 `get_current_slot()`）
6. **LK/preloader 里没有"持久 vbmeta 缓存"**——本次未在任何 LK 源码中找到"缓存的 vbmeta 状态"。因此"重启后 lk 保留了 vbmeta 状态"这个说法**未找到可靠来源**（更准确的解释是上面 1–5 的"fastboot 只改你当下刷的那一个 buffer / 只改当前 slot / 不动非 vbmeta 分区"）。（**未找到可靠来源**，作为对流行说法的纠偏）

**"MTK 上为什么常要绕道 mtkclient"**：TB330FU 的实操记录明确说 **LK 必须打补丁**，否则即使关了 verity 仍会在启动时 `Dm-verity corruption`；而 LK 是 `lk_a`/`lk_b` 这种 MTK 私有打包容器（含 `lk_main_dtb`/`lk_dtbo` 子分区 + 证书），**不能用 fastboot 刷**，只能 BROM 模式 + mtkclient 整块写。
证据: https://github.com/threadexio/tb330fu （*"We need to patch something called the LK partitions… not patching them results in the device unable to boot with a 'Dm-verity corruption error'… Enter BROM mode again with mtkclient… Write partition(s)"*，**已核实**，TB330FU 专属记录）
证据: https://blog.r0rt1z2.com/posts/reverse-engineering-mediatek-lk/（LK 容器结构、`lkpatcher`，**已核实**）

### 3.3 avbtool 自签流程（已核实参数语义）

avbtool 官方签名参数（`make_vbmeta_image`）：
```
--algorithm ALGORITHM --key /path/to/key
--rollback_index NUMBER  --rollback_index_location NUMBER
--include_descriptors_from_image /path/to/image.bin
--setup_rootfs_from_kernel /path/to/image.bin   # 别名 --generate_dm_verity_cmdline_from_hashtree
--chain_partition part_name:rollback_index_location:/path/to/key.bin
--flags NUMBER | --set_hashtree_disabled_flag | --set_verification_disabled_flag
--append_to_release_string STR
```
证据: https://android.googlesource.com/platform/external/avb/+/refs/heads/main/README.md（`Using avbtool` 节 usage 原文）+ https://android.googlesource.com/platform/external/avb/+/refs/heads/main/avbtool.py（`_add_common_args` / `_fixup_common_args`，**已核实**）

- **`--flag 2` / `--flag 3` 的写法注意**：`avbtool` 的实际参数名是 **`--flags`**（复数）。`--flags 2` = 只关 verification；**`--flags 3` = 同时关 verity+verification**（这正是 TWRP 设备树里的 `BOARD_AVB_MAKE_VBMETA_IMAGE_ARGS += --flags 3`）。也有等价开关 `--set_hashtree_disabled_flag`（bit0）与 `--set_verification_disabled_flag`（bit1）。
  证据: avbtool.py `AVB_VBMETA_IMAGE_FLAGS_HASHTREE_DISABLED = 1` / `AVB_VBMETA_IMAGE_FLAGS_VERIFICATION_DISABLED = 2` 与 `_fixup_common_args()`（**已核实**）；TB330FU 的 `--flags 3`: https://github.com/twrpdtgen/android_device_lenovo_TB330FU/blob/master/BoardConfig.mk（**已核实**）
  > 若你在某教程里看到单数 `--flag`，那是 `build/make` 的 `BOARD_AVB_MAKE_VBMETA_IMAGE_ARGS` 之外的非标准写法，**直接用 avbtool 会报 unrecognized arguments**（**推测/提醒**）。

- **`--setup_rootfs_from_kernel`**：不是"复制 rootfs"，而是**从 kernel 镜像的 vbmeta footer 里取 dm-verity cmdline descriptor，注入到新 vbmeta 的 cmdline descriptor**（源码：`calculate_kernel_cmdline(image, hashtree_disabled)` 只读取 `AvbKernelCmdlineDescriptor` 并按 `FLAGS_USE_ONLY_IF_HASHTREE_*` 过滤）。所以它用于**保留 system 分区的 `dm=...` cmdline**，避免自己手写 `androidboot` 参数。
  证据: avbtool.py `calculate_kernel_cmdline()`（**已核实**）

**TB330FU 的可执行自签命令（A/B 双槽）**：
```bash
# 1) 生成密钥（RSA4096，PKCS8）
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out avb_private.pem
openssl pkey -in avb_private.pem -pubout -out avb_public.pem

# 2) 给 boot / vendor_boot / dtbo 加 footer 或 hash descriptor（按分区大小）
python avbtool.py add_hash_footer --image boot_a.img --partition_name boot \
    --partition_size $(stat -c%s /dev/block/mmcblk0pX) --key avb_private.pem \
    --algorithm SHA256_RSA4096 --rollback_index 0
python avbtool.py add_hash_footer --image vendor_boot_a.img --partition_name vendor_boot \
    --partition_size <VENDOR_BOOT_SIZE> --key avb_private.pem --algorithm SHA256_RSA4096
python avbtool.py add_hash_footer --image dtbo_a.img --partition_name dtbo \
    --partition_size <DTBO_SIZE> --key avb_private.pem --algorithm SHA256_RSA4096

# 3) 生成顶层 vbmeta（A/B 由 rollback_index_location 区分，用 _a / _b 各一份）
for s in a b; do
python avbtool.py make_vbmeta_image \
    --output vbmeta_$s.img \
    --key avb_private.pem --algorithm SHA256_RSA4096 \
    --flags 3 \
    --rollback_index 0 \
    --setup_rootfs_from_kernel system_$s.img \
    --include_descriptors_from_image boot_$s.img \
    --include_descriptors_from_image vendor_boot_$s.img \
    --include_descriptors_from_image dtbo_$s.img \
    --include_descriptors_from_image vbmeta_system_$s.img \
    --include_descriptors_from_image vbmeta_vendor_$s.img \
    --append_to_release_string "tb330fu-user"
done

# 4) 校验
python avbtool.py verify_image --image vbmeta_a.img --key avb_public.pem
```

**A/B 双槽 `--rollback_index`（已核实机制）**：
- `rollback_index[n]` 是**整数字段**，设备在安全存储（RPMB/TEE/NVRAM）里存 `stored_rollback_index[n]`；只要满足 `rollback_index[n] >= stored_rollback_index[n]` 就算通过。
  证据: avb README `Rollback Protection`（*"rollback_index[n] >= stored_rollback_index[n] for all n"*，**已核实**）
- `--rollback_index_location N` 指定这条 index 存在**哪个槽位**；顶层 vbmeta 的 `rollback_index_location` 在 header 里，被 chained 分区的放在 `AvbChainPartitionDescriptor` 里。
  证据: avb README / `avb_vbmeta_image.h`（**已核实**）
- **对自签/移植的实用建议**：把 `--rollback_index` 设为 `0`（最低），否则若原固件用过非 0 值而你的镜像更小，**LK 会拒绝**。同时 **TB330FU 的 TWRP 设备树为了防 anti-rollback 直接写 `PLATFORM_SECURITY_PATCH := 2099-12-31`**，说明该机确实启用了 anti-rollback —— 刷入低于已存 rollback 的镜像会 brick。
  证据: https://github.com/twrpdtgen/android_device_lenovo_TB330FU/blob/master/BoardConfig.mk（**已核实**：`# Hack: prevent anti rollback` + `PLATFORM_SECURITY_PATCH := 2099-12-31`；"该机启用 anti-rollback"为**推测**）

### 3.4 MTK 上的 dm-verity 关闭路径（两种，按危险度排序）

**路径 A（首选，fastboot，AOSP 官方语义）**
```bash
# 关键：--disable-* 必须和“这一次 flash vbmeta”同一条命令；且要分别刷两槽
fastboot --disable-verity --disable-verification flash vbmeta_a vbmeta_a.img
fastboot --disable-verity --disable-verification flash vbmeta_b vbmeta_b.img
# 若要刷 boot/vendor_boot（不带 --disable-* 也不会动 vbmeta，见 3.2 第 1–2 条）
fastboot flash boot_a boot_a.patched.img
fastboot flash vendor_boot_a vendor_boot_a.patched.img
```
TB330FU 实操中就是这么做的（连 boot/vendor_boot 也带上了 `--disable-*`，无害但非必需）：
证据: https://github.com/threadexio/tb330fu （**已核实**，原命令逐字引用：
`fastboot flash --disable-verity --disable-verification vbmeta_a path/to/vbmeta_a.bin` … `fastboot flash --disable-verity --disable-verification vendor_boot_a vendor_boot_a.patched.bin` …）

**路径 B（MTK 专用，mtkclient / BROM）**
```bash
python mtk.py da vbmeta 3        # 关 verity + verification，写当前槽 vbmeta
python mtk.py da vbmeta 0        # 反向：恢复
# 注意：da_vbmeta 只改当前 slot；切槽后需重跑
```
证据: https://raw.githubusercontent.com/bkerler/mtkclient/main/mtkclient/Library/DA/mtk_da_handler.py（**已核实**）

**为什么在 MTK 上"要走 mtkclient"（已核实的三点）**：
1. TB330FU 的 `dm-verity corruption` 根因在 **LK 层**，而 LK 只能 BROM 整块写（见 3.2 末尾）。`fastboot` 根本刷不了 `lk`。
2. LK 镜像含证书（`cert1`/`cert2`），用 `lkpatcher` 打补丁时要处理 cert；整块写回需 DA。
   证据: https://blog.r0rt1z2.com/posts/reverse-engineering-mediatek-lk/（**已核实**）
3. `mtk.py da vbmeta` 直接按 GPT 写 `vbmeta<slot>`，不依赖 fastboot 的 `partition-type:vbmeta_a` 之类的 getvar 应答（MTK lk 的 getvar 实现经常残缺）。
   证据: 源码 `get_partition_data(parttype="user")`（**已核实**）

**实测建议（开机后 10 秒内自查）**
```bash
adb shell cat /proc/cmdline | tr ' ' '\n' | grep -E "veritymode|verifiedbootstate|flash.locked|verity"
adb shell cat /proc/bootconfig | grep -E "verity|verifiedboot"
adb shell getprop | grep -Ei "verity|vbmeta|verifiedboot"
```
期望（成功关掉后）：`androidboot.veritymode=disabled`、`androidboot.vbmeta.digest` 不存在或 `verifiedbootstate=orange`，且**不再出现** `dm-verity corruption` 横幅。

---

## 4. SukiSU LKM 修补 vendor_boot 的具体做法

### 4.1 仓库与版本（已核实）

- 主仓库：**`SukiSU-Ultra/SukiSU-Ultra`**（`https://github.com/SukiSU-Ultra/SukiSU-Ultra`）——Kernel-based Android Root Solution & KPM。
- LKM 修补器是 Rust 写的 **`ksud`**：`userspace/ksud/src/boot_patch.rs`（约 1491 行）。
  证据: https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/userspace/ksud/src/boot_patch.rs（**已核实**，本次抓取了完整源码）
- 官方文档：https://sukisu.org/guide/installation 与仓库内 `docs/guide/installation.md`（**已核实**）；中文 README：`docs/zh/README.md`。
- **KMI**：预置 `android12-5.10 / android13-5.10 / android13-5.15 / android14-5.15 / android14-6.1 / **android15-6.6** / android16-6.12 / android17-6.18`（aarch64 + x86_64）。
  证据: https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/docs/guide/installation.md（**已核实**）→ **TB330FU 的 `6.6.118-android15-8` 正好落在 `android15-6.6` 这一档**（**已核实** KMI 列表 + 设备内核版本；具体 KMI 字符串请用 `ksud boot-info kmi` 或 `uname -r` 对齐确认）

### 4.2 `ksud boot-patch` 对 vendor_boot 到底做了什么（逐步骤，源码级已核实）

**(1) 选择目标分区** —— `choose_boot_partition()`：
```rust
// 若指定了 --partition，且值为 "boot" | "init_boot" | "vendor_boot" 则直接用它，否则回退 "boot"
if let Some(part) = partition { return match part.as_str() { "boot"|"init_boot"|"vendor_boot" => part.clone(), _ => "boot".into() }; }
let skip_init_boot = kmi.starts_with("android12-");
let init_boot_exist = Path::new(&format!("/dev/block/by-name/init_boot{slot_suffix}")).exists();
if !is_replace_kernel && init_boot_exist && !skip_init_boot { return "init_boot".into(); }
"boot".into()
```
> ⚠️ **重要**：**自动选择逻辑里没有 `vendor_boot` 分支**。TB330FU 用 `android15-6.6` KMI，`skip_init_boot=false`；若本机**没有** `init_boot` 分区（fstab 里确实没有），会回退到 `"boot"`。但 A15 的通用 ramdisk 在 `vendor_boot` 里 → **必须显式指定**。
> 证据: https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/userspace/ksud/src/boot_patch.rs — `choose_boot_partition()`（**已核实**）；"TB330FU 必须手动指定 vendor_boot"为**推测**

**(2) 解析镜像并找对 ramdisk** —— `extract_ramdisk()`：
```rust
if ramdisk_image.is_vendor_ramdisk() {
    let (pos, target) = ramdisk_image.iter_vendor_ramdisk().enumerate()
        .find(|e| e.1.get_name_raw() == b"init_boot")           // 优先 name=="init_boot"
        .or_else(|| ramdisk_image.iter_vendor_ramdisk().enumerate()
                       .find(|e| e.1.get_name_raw() == b""))     // 否则空名 = PLATFORM 主体盘
        .ok_or_else(|| anyhow!("No suitable vendor ramdisk entry found"))?;
    ...
}
```
证据: `boot_patch.rs` `extract_ramdisk()`（**已核实**，且注释写明 "matching the original magiskboot lookup order (init_boot.cpio before ramdisk.cpio)"）
> 这解释了 MTK 设备的两种形态：① fragment 名为 `init_boot`（部分 OEM）；② 空名主体盘（mkbootimg 的默认 `PLATFORM` 条目）。**TB330FU 属于哪一类要 dump 后看**（**推测**：大概率是②，因为只有 1 个 fragment）

**(3) 版本门槛** —— `enforce_bootimage_version()`：
```rust
if let BootImageVersion::Android(ver) = boot.get_header().get_version() && ver < 3 {
    bail!("bootimage version {ver} is not supported!")
}
```
→ **只支持 v3/v4**，v4 是 Android 15 GKI 的正常形态（**已核实**）

**(4) 注入内容**（这不是 `boot_patch.sh`，是等价的 Rust 实现）：
```rust
cpio.add("init",         CpioEntry::regular(0o755, ksu_init))?;      // 用 ksuinit 覆盖 /init
cpio.add("kernelsu.ko",  CpioEntry::regular(0o755, kernelsu_ko))?;   // 把 LKM 放到 ramdisk 根
if !is_kernelsu_patched && cpio.exists("init") { cpio.mv("init", "init.real")?; }  // 备份原 init
```
> **关键差异（务必注意）**：SukiSU **不**把 `kernelsu.ko` 放进 `/lib/modules` 或 DLKM 段，而是放在 **ramdisk 根目录 `/kernelsu.ko`**，由注入的 `init`(=`ksuinit`) 在 first-stage 里自己 `insmod`。因此：
> - **不需要**改 `vendor_boot` 的 `vendor_ramdisk_table`、**不需要**新增 DLKM fragment、**不需要**动 `init.rc`；
> - **不会**出现"ramdisk 里塞 .ko 到 /lib/modules 与 /vendor/lib/modules 冲突"的问题。
> 证据: `boot_patch.rs` 第 1002–1010 行（**已核实**）
> 补充：`--no-custom-rc`、`--no-preinit`、`--kmi`、`--module`、`--init`、`--cmdline`、`--spoof-release`、`--spoof-version`、`--enable-adbd`、`--out`、`--out-name`、`--flash`、`--backup`、`--ota`、`--partition`、`--no-install` 等参数见 `BootPatchArgs`（**已核实**）
> KMI 相关的 ko 命名：`assets/{kmi}_kernelsu.ko`（Android 上）／`{arch}/{kmi}_kernelsu.ko`（主机上）（**已核实**）

**(5) 写回并重建**：
```rust
patcher.replace_vendor_ramdisk(idx, Box::new(Cursor::new(new_cpio)), false);
```
→ 用 `android_bootimg` crate 的 `BootImagePatchOption` **就地替换第 idx 个 vendor ramdisk fragment 的字节流**，然后整个镜像重新 dump。**头部字段（page_size / dtb / table 的 name、type、board_id）原样保留**，所以 `dtb_addr` 不会错。
证据: `boot_patch.rs` 第 1100–1106 行 + 第 940 行 `BootImagePatchOption::new(&boot_image)`（**已核实**）

**(6) 直刷模式（`--flash`）**：SukiSU 在设备上直接写块设备，并且：
- 先 `getprop ro.build.ab_update` / `ro.boot.slot_suffix` 确认槽位；
- **写前 TOCTOU 校验**：`boot partition changed since it was read; refusing to flash`；写后**逐 64KB 回读比对**；
- `BLKROSET` ioctl 解只读，写完后不重算 AVB。
证据: `boot_patch.rs` `flash_partition_verified()` 第 234–330 行（**已核实**）
> **重要**：`--flash` **不处理 AVB**（源码无 vbmeta/avb 相关逻辑）。所以在 *先关 verity* 的顺序上，要先做 3.4 的路径 A 或 B，再 `ksud boot-patch --flash`。

### 4.3 可执行流程（TB330FU）

```bash
# ── 0) 前置：确认分区名（这一步决定后面所有命令对不对）
adb shell ls -l /dev/block/by-name/ | grep -E "vendor_boot|init_boot|dtbo|lk|vbmeta"

# ── 1) 备份（务必！）——有 root 时直接 dd，别只依赖 mtkclient
adb shell "su -c 'dd if=/dev/block/by-name/vendor_boot_a of=/sdcard/vendor_boot_a.img bs=4M'"
adb shell "su -c 'dd if=/dev/block/by-name/vendor_boot_b of=/sdcard/vendor_boot_b.img bs=4M'"
adb pull /sdcard/vendor_boot_a.img ; adb pull /sdcard/vendor_boot_b.img
sha256sum vendor_boot_*.img | tee stock.sha256

# ── 2) 关 verity（先做，见 §3.4；或用 mtk.py da vbmeta 3）
fastboot --disable-verity --disable-verification flash vbmeta_a vbmeta_a.img
fastboot --disable-verity --disable-verification flash vbmeta_b vbmeta_b.img

# ── 3) 用 SukiSU 的 ksud 打补丁（显式 --partition vendor_boot！）
#     手机上（Manager 已装、ksud 已就位）
adb shell "su -c '/data/adb/ksud boot-patch -b /sdcard/vendor_boot_a.img --partition vendor_boot --flash'"
#     或 PC 上（离线修补再 fastboot 刷）
ksud boot-patch -b vendor_boot_a.img --kmi android15-6.6 \
     --partition vendor_boot --out . --out-name vendor_boot_a.patched.img
#     lkm 需匹配：默认取内置 assets/android15-6.6_kernelsu.ko，可 --module 指定

# ── 4) 自检：确认 patched 镜像只改了 ramdisk，头部/dt 段不变
python unpack_bootimg.py --boot_img vendor_boot_a.patched.img --out chk --format=info
cmp chk/dtb vb_out/dtb && echo "dtb 未变 OK"
ls chk/   # 期望看到 vendor_ramdisk00 内含 init / init.real / kernelsu.ko

# ── 5) 刷入（A 槽先试）
fastboot flash vendor_boot_a vendor_boot_a.patched.img
fastboot reboot
# ── 6) 成功后再做 B 槽；失败则 fastboot flash 回 stock（或 BROM + mtk w）
```

**回滚**：
```bash
fastboot flash vendor_boot_a vendor_boot_a.img   # 原始备份
# 若开不了机 → BROM: python mtk.py w vendor_boot_a stock/vendor_boot_a.img
#                        python mtk.py w lk_a stock/lk_a.bin（若 LK 也动过）
```

### 4.4 与 Magisk 的关系（对 TB330FU 的现实提醒，已核实）

- Magisk 官方 `scripts/boot_patch.sh` 里**已经有** vendor_boot 支持：
  ```sh
  ./magiskboot unpack "$BOOTIMAGE"
  case $? in  3 ) ui_print "- Vendor boot image detected"; VENDORBOOT=true ;; esac
  for path in ramdisk.cpio vendor_ramdisk/init_boot.cpio vendor_ramdisk/ramdisk.cpio; do
      [ -e $path ] && RAMDISK=$path && break
  done
  ...
  if $VENDORBOOT; then BOOTIMAGE="/dev/block/by-name/vendor_boot$SLOT" ...
  ```
  证据: https://github.com/topjohnwu/Magisk/blob/master/scripts/boot_patch.sh 与 https://github.com/topjohnwu/Magisk/blob/master/scripts/util_functions.sh（**已核实**，`find_boot_image()` 中的 `VENDORBOOT` 分支）
- **但**对"ramdisk 位于 `vendor_boot:/vendor_ramdisk/init_boot.cpio`"这类 OEM 变体，Magisk 曾长期无法自动识别（issue #8835，同为 **MTK Helio G85/G88 平板**：Redmi Pad SE 8.7 = MT8876），官方答复是"手工对 `init_boot.cpio` 跑 `magiskboot cpio … patch`"，合入建议后仍无自动检测。
  证据: https://github.com/topjohnwu/Magisk/issues/8835（**已核实**，含 `vendor_ramdisk/` 下 `init_boot.cpio`、`ramdisk.cpio`、`recovery.cpio` 三个 cpio 的实测输出与维护者回复）
- TB330FU 上有两处独立记录指向同一现象：A15 后通用 ramdisk 移到了 vendor_boot，需要同时处理 `boot` 与 `vendor_boot`；且官方 Magisk app 打不出来。
  证据: https://xdaforums.com/t/cant-sucessfully-root-lenovo-tab-m11-tb330fu.4754581/（*"With A15, generic ramdisk is moved to /vendor_boot… so you should use official root method"*；另一帖 *"get both A partition for 'boot' and 'vendor_boot' and patch th[em]"*，**已核实**摘录）、https://github.com/topjohnwu/Magisk/issues/9263（TB-330FU / Android 15 / Magisk v30.2 报告 root 不生效，**已核实** issue 存在；评论正文因 GitHub API 限流未能完整读取 → 细节为**推测**）
- 社区对这类机型给的替代品是 `NeutroGe/Magisk_enhanced`（支持直接 patch `vendor_boot.img`）。
  证据: 上述 XDA 线程 + Magisk #8835 评论（**已核实** 该仓库被两处提及；其代码质量未审）

> **结论建议**：TB330FU 走 **SukiSU LKM（`ksud boot-patch --partition vendor_boot`）** 路由，比"补 Magisk 的 cpio 手工流程"少踩坑；两者都必须**先完成 §3.4 的 verity 关闭**（或至少确保 `vbmeta` flags=3 且 BL unlocked）。

---

## 5. 一页速查（给执行阶段用）

```
分区（TB330FU，已核实来自 recovery.fstab，slot 后缀需实测）
  boot(_a/_b)  vendor_boot(/?_a/_b)  dtbo  vbmeta(_a/_b)  vbmeta_system(_a/_b)  vbmeta_vendor(_a/_b)
  lk/lk2（MTK 打包容器）  seccfg  misc  para  expdb  persist  super(system+vendor+product)
  无 init_boot，无独立 dtb 分区（推测）

vendor_boot v4
  头 2128B；段序 = header | vendor_ramdisk(fragments 相接) | dtb | vendor_ramdisk_table | bootconfig
  工具必须用 AOSP android-12.0.0_r1 及以后；推荐 android15-release
  往返：unpack_bootimg --format=mkbootimg → 改文件 → mkbootimg --header_version 4 --vendor_boot out.img $(args)

DTB 位置
  vendor_boot 头 dtb_size/dtb_addr（GKI v3+）   独立 dtbo 分区（dt_table magic 0xd7b7ab1e）
  LK 选 dtb：multi-dtb 用 platform_id/soc_rev/version_id；dtbo 用 dt_table_entry.id/rev/custom[]
  dtb_idx = 打包/烧写侧索引，非 LK 运行时变量（未找到可靠来源）

AVB
  UNLOCKED → BL 跳过签名校验（orange），未签名镜像可启动
  verity 仍 enforcing，除非 vbmeta flags bit0(HASHTREE_DISABLED) 置位
  flags: bit0=disable-verity, bit1=disable-verification; avbtool 参数是 --flags（复数），3 = 全关
  快关：fastboot --disable-verity --disable-verification flash vbmeta_a|_b vbmeta.img
  兜底：python mtk.py da vbmeta 3   （= 在 vbmeta 偏移 0x78 写大端 3，仅当前 slot）
  必须额外做：lkpatcher 打 lk_a/lk_b，否则 dm-verity corruption（TB330FU 实测）

SukiSU
  ksud boot-patch -b vendor_boot_a.img --partition vendor_boot [--kmi android15-6.6] [--flash]
  做的事：解析 → 选 fragment(name=="init_boot" 优先，否则 name=="" 的 PLATFORM 主体盘)
        → cpio 里 add /init(ksuinit) + /kernelsu.ko，原 init → init.real
        → replace_vendor_ramdisk(idx) → 整镜像重 dump（头字段/dtb 不动）
  不改 vendor_ramdisk_table、不加 DLKM fragment、不改 init.rc
  不支持 header_version < 3；需 KMI 匹配；--flash 不做 AVB
```

---

## 6. 未能核实 / 需要实机取证的清单

| 项 | 为什么没定 | 取证方法 |
|---|---|---|
| TB330FU 的 `vendor_boot` 是否 A/B（`_a/_b`） | fstab 没标 slotselect；不同来源不一致 | `adb shell ls /dev/block/by-name/` |
| TB330FU 的 `dtbo` 是否 A/B | 同上 | 同上 |
| `vendor_boot` 的 `page_size` | 未 dump | `unpack_bootimg --format=info` 的 `page size` |
| `vendor_ramdisk_table` 条目数 / 名称 / type | 未 dump | 同上 |
| `dtbo` 是否被 AVB 签名、是否在顶层 vbmeta descriptor 里 | 未 dump | `avbtool info_image --image dtbo.img` / `vbmeta_a.img` |
| `dtb_idx` 在 MTK A15 LK 中的确切用法 | 公开 LK 源码未出现该符号 | `strings lk_a.bin \| grep -i dtb`；Ghidra 交叉引用 |
| MTK 的 vendor `.ko` 究竟在 PLATFORM 主体盘还是独立 DLKM 段 | 取决于 OEM 配置 | `unpack_bootimg --format=info` 看 type；或解 fragment 看 `lib/modules` |
| TB330FU 是否启用 anti-rollback（`--rollback_index` 下限） | 只从 TWRP 设备树的 hack 反推 | `fastboot getvar rollback-index` 可能不实现；稳妥做法：刷入时 `--rollback_index 0` 且**保留原 vbmeta 备份** |
| "重启后 lk 里保留 vbmeta 状态"这一流行说法 | 未在任何 LK 源码中找到对应机制 | 已被 §3.2 的 4 条更精确解释替代 |

---

## 7. 引用清单（全部本次实际抓取）

| # | URL | 用途 | 状态 |
|---|---|---|---|
| 1 | https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions | v3/v4 头部、布局、ramdisk_type 语义、build 参数 | 已核实 |
| 2 | https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/include/bootimg/bootimg.h | 结构体定义、段序注释、page 计算 | 已核实 |
| 3 | https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/mkbootimg.py | `--vendor_boot`/`--ramdisk_type`/`--vendor_ramdisk_fragment`、PLATFORM 硬编码、name 黑名单 | 已核实 |
| 4 | https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/android15-release/unpack_bootimg.py | `--format=mkbootimg` 往返流程、fragment 输出命名 | 已核实 |
| 5 | https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/tags/android-11.0.0_r1/mkbootimg.py | v3-only（无 table） | 已核实 |
| 6 | https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/tags/android-12.0.0_r1/mkbootimg.py | 首次支持 v4 table | 已核实 |
| 7 | https://source.android.com/docs/core/architecture/dto/partitions | dt_table_header/entry、id/rev/custom、mkdtimg | 已核实 |
| 8 | https://source.android.com/docs/core/architecture/bootloader/dtb-images | DTB 移入 vendor_boot | 已核实 |
| 9 | https://source.android.com/docs/core/architecture/partitions/generic-boot | A13 起 generic ramdisk 从 boot 移出（→ init_boot / vendor_boot） | 已核实 |
| 10 | https://source.android.com/docs/security/features/verifiedboot/device-state | UNLOCKED 跳过校验 | 已核实 |
| 11 | https://source.android.com/docs/core/architecture/bootloader/locking_unlocking | verifiedbootstate orange → flash.locked=0 | 已核实 |
| 12 | https://android.googlesource.com/platform/external/avb/+/refs/heads/main/avbtool.py | `--flags`、`--setup_rootfs_from_kernel`、`--include_descriptors_from_image`、`calculate_kernel_cmdline` | 已核实 |
| 13 | https://android.googlesource.com/platform/external/avb/+/refs/heads/main/README.md | usage、rollback_index 语义、HASHTREE_DISABLED → veritymode=disabled | 已核实 |
| 14 | https://android.googlesource.com/platform/external/avb/+/refs/heads/main/libavb/avb_vbmeta_image.h | flags 在偏移 120(0x78) | 已核实 |
| 15 | https://raw.githubusercontent.com/aosp-mirror/platform_system_core/master/fastboot/fastboot.cpp | `--disable-verity` 实现、只改当前刷写 buffer、`has_vbmeta_partition()` | 已核实 |
| 16 | https://raw.githubusercontent.com/bkerler/mtkclient/main/mtkclient/Library/DA/mtk_da_handler.py | `da vbmeta` / `patch_vbmeta` / `vbmeta[0x78:0x7c]=vbmode` / 仅当前 slot | 已核实 |
| 17 | https://github.com/bkerler/mtkclient/issues/101 | 用户实测 `dm-verity corruption` + 两条命令的搭配 | 已核实（issue 正文；维护者未作答） |
| 18 | https://blog.r0rt1z2.com/posts/reverse-engineering-mediatek-lk/ | LK 容器子分区、lkpatcher、cert | 已核实 |
| 19 | https://github.com/dguidipc/gemini-lk/blob/master/lk/app/mt_boot/mt_boot.c | LK 扫 FDT magic、multi-dtb 匹配 platform_id/soc_rev/version_id、dtb_overlay/odmdtbo | 已核实（非 TB330FU 分支） |
| 20 | https://github.com/twrpdtgen/android_device_lenovo_TB330FU/blob/master/recovery.fstab | TB330FU 真实分区名清单（含 dtbo、无 init_boot） | 已核实 |
| 21 | https://github.com/twrpdtgen/android_device_lenovo_TB330FU/blob/master/BoardConfig.mk | `TARGET_BOARD_PLATFORM := mt6768`、`barley_row_wifi`、`--flags 3`、anti-rollback hack | 已核实（部分字段已过时，已标注） |
| 22 | https://github.com/threadexio/tb330fu | **TB330FU 专属实操**：必须 patch lk_a/lk_b、fastboot --disable-* 命令、patch boot+vendor_boot | 已核实 |
| 23 | https://xdaforums.com/t/cant-sucessfully-root-lenovo-tab-m11-tb330fu.4754581/ | A15 后 ramdisk 移到 vendor_boot；需 patch boot + vendor_boot | 已核实（搜素摘要级，页面反爬未取全文） |
| 24 | https://github.com/topjohnwu/Magisk/issues/9263 | TB-330FU / A15 / Magisk v30.2 root 不生效 | 已核实（issue 存在）；细节推测 |
| 25 | https://github.com/topjohnwu/Magisk/issues/8835 | MTK 平板 `vendor_ramdisk/{init_boot,ramdisk,recovery}.cpio` 实测、维护者建议手工 patch、无自动检测 | 已核实 |
| 26 | https://github.com/topjohnwu/Magisk/blob/master/scripts/boot_patch.sh | `VENDORBOOT` 检测、ramdisk 路径优先级 | 已核实 |
| 27 | https://github.com/topjohnwu/Magisk/blob/master/scripts/util_functions.sh | `find_boot_image()` 的 vendor_boot 分支 | 已核实 |
| 28 | https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/userspace/ksud/src/boot_patch.rs | `choose_boot_partition`、`extract_ramdisk`、注入 `/init`+`/kernelsu.ko`、`replace_vendor_ramdisk`、v<3 拒绝、`flash_partition_verified` | 已核实 |
| 29 | https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/docs/guide/installation.md + https://sukisu.org/guide/installation | KMI 列表（含 android15-6.6）、LKM 三种装法、vendor_boot 推荐 | 已核实 |
