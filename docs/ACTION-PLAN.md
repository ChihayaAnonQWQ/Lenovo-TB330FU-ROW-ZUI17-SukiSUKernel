# MTK 6.6 Non-GKI 内核定制与移植 · 行动计划（Action Plan）

> **设备**：Lenovo TB330FU（产品名 `TB330FU_PRC`，`mt6768` / `chipid MT8786_8+128_18`）
> **系统**：ZUI 17.0.502（ROW），编译版本 `S2003138_261010_ROW`，Android 15 (35)，A/B + 动态分区
> **内核**：**`6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k`**（`#1 SMP PREEMPT Sat Jan 3 21:50:53 UTC 2026`）
> **现状**：BL 已解锁（`verifiedbootstate=orange`）、已用 SukiSU Ultra（LKM）通过 `boot_a` + `vendor_boot_a` 双分区注入 Root
> **作业范围**：**只在 A 槽作业**（`ro.boot.slot_suffix=_a`）；B 槽只作逃生保险，**不刷写**
> **文档版本**：2026-10-30（v2，已用实机数据校正）
> **权威数据源**：[`research/device-facts.md`](../research/device-facts.md) —— 本文所有"实测"均指该文档中的实机采集值

---

## 0. 先说三个必须纠正的前提（否则第一步就错）

| # | 流行说法 | 实际情况 | 证据 |
|---|---|---|---|
| 1 | "MTK 公开了 MT6768 的 6.6 源码 / 找 MTK BSP" | **不存在**。`kernel/mediatek` 全部 13 个 head 中没有 6.6；Lenovo GPL 请求单 `lenovo/gplcc#6` 至今 open、零官方回复。**可用的基线只有 AOSP `kernel/common`（android15-6.6）** | [kernel/mediatek refs](https://android.googlesource.com/kernel/mediatek/+refs)、[gplcc#6](https://github.com/lenovo/gplcc/issues/6) |
| 2 | "SUSFS 可以打成模块（LKM）加载" | **做不到**。SUSFS 的 6.6 补丁要改 **26 个内核源文件**（`fs/namei.c` 一个文件 17 个 hunk），且 SukiSU 只有 **`builtin` 分支**带 `CONFIG_KSU_SUSFS`，`main` 分支（就是当前 LKM 用的）里 SUSFS 命中 **0** | [SUSFS 6.6 补丁](https://github.com/Zarathos30/susfs4ksu/blob/gki-android15-6.6-dev/kernel_patches/50_add_susfs_in_gki-android15-6.6.patch)、[SukiSU how-to-integrate](https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/docs/guide/how-to-integrate.md) |
| 3 | "存在内核级 Zygisk" | **不存在**。ZygiskNext / ReZygisk / NeoZygisk 全部是用户态（NeoZygisk 还是 ptrace 注入 zygote），不引入任何内核补丁 | [ZygiskNext](https://github.com/Dr-TSNG/ZygiskNext)、[ReZygisk](https://github.com/PerformanC/ReZygisk)、[NeoZygisk](https://github.com/JingMatrix/NeoZygisk) |

另外两条底层事实会贯穿全文：

- **`python mtk.py da vbmeta 3` 的真实作用是**：把当前槽 `vbmeta` 分区 **偏移 `0x78`（AVB header 的 `flags`）写成大端 3**（bit0=关 dm-verity，bit1=关验证），而且**只写当前槽**。不是"LK 缓存了状态"——LK 里没有这种东西。
  证据：[mtk_da_handler.py](https://raw.githubusercontent.com/bkerler/mtkclient/main/mtkclient/Library/DA/mtk_da_handler.py)（`patch_vbmeta` / `da_vbmeta`）
- **LK 必须在 `boot` 分区找到结构合法的 AVB footer 才能启动**（哪怕已 orange 解锁）。实测三态：半截镜像 → 冻结；整分区全零尾 → 冻结；**整分区尺寸 + `avbtool add_hash_footer --algorithm NONE` → 可启动**
  证据：[lifengqaq README §A1](https://github.com/lifengqaq/tb330fu-kernelsu-droidspaces/blob/main/README.zh.md)

---

## 1. 前置依赖

### 1.1 软件环境（建议 Linux x86_64 或 WSL2 Ubuntu 24.04，不要在 Windows 原生 bash 里干）

| 类别 | 依赖 | 说明 |
|---|---|---|
| 内核构建 | `clang-r510928`（clang 18.0.0） | **必须**用 `android15-release` 的 AOSP prebuilt，不要用 NDK 的 clang、也不要系统 clang |
| | `make python3 pahole flex bison bc openssl depmod lz4 cpio zstd` | BTF 需要 pahole |
| | 磁盘 ≥ 120 GB（源码 ~7 GB + 一次构建 out ~20 GB） | |
| 镜像工具 | AOSP `mkbootimg.py` / `unpack_bootimg.py`（**取 `android15-release` 分支**） | android-11 的分支**只支持 v3**，会静默丢掉 ramdisk table |
| | `avbtool.py` | 打 NONE footer、`info_image`/`verify_image` |
| 设备通信 | `adb` / `fastboot`（platform-tools） | |
| | **mtkclient**（`python mtk.py`） | BROM/DA；`da seccfg unlock`、`da vbmeta 3`、分区读写 |
| | **lkpatcher**（R0rt1z2） | `pip3 install git+https://github.com/R0rt1z2/lkpatcher` |
| 本仓库工具 | `scripts/vkrepack.py`、`scripts/ota-reinject.sh` | 见第 3、4 节 |
| Root | SukiSU Ultra 的 `ksud`（含 `assets/android15-6.6_kernelsu.ko`） | 与内核 KMI 对应 |

> 工具链获取（一手命令，来自 Barley-Project 的 `fetch-toolchain.sh`）：
> ```bash
> curl -L -o clang-r510928.tar.gz \
>   "https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/refs/heads/android15-release/clang-r510928.tar.gz"
> mkdir -p tools/clang-r510928 && tar -xf clang-r510928.tar.gz -C tools/clang-r510928
> ```

### 1.2 设备侧取证（**已完成** · 2026-10-30 16:08 实机采集）

> 全部结果已归档到 [`research/device-facts.md`](../research/device-facts.md)。摘要：

| 项 | 实测值 |
|---|---|
| 内核串 | `6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k` |
| 编译器 | clang **r510928 (18.0.0)** + LLD 18.0.0，`kleaf@build-host`，**+pgo/+bolt/+lto/+mlgo** |
| `CONFIG_LOCALVERSION` | **`"-4k"`**（完整后缀由 kleaf 注入，非写死在 config） |
| `CONFIG_LOCALVERSION_AUTO` | **`y`** ← 复现时必须改为 `n` |
| `CONFIG_MODVERSIONS` | **`y`** |
| `CONFIG_RFKILL` / `CONFIG_WLAN` | **`m`** / **`y`** |
| `CONFIG_FTRACE_SYSCALLS` | **不存在** ← 编 KSU 前必须补 |
| `CONFIG_KSU` / `SUSFS` | **完全不存在**（原厂干净 GKI） |
| 槽位 | `ro.boot.slot_suffix=_a`，`ro.build.ab_update=true` |
| boot_a | **v4 / page 4096 / kernel 14,623,212 (gzip) / ramdisk_size=0** / 32 MiB / AVB **SHA256_RSA4096** |
| vendor_boot_a | **v4 / header 2128 / page 4096** / dtb **156,675 B (DT-table `d7b7ab1e`)** / 3 个 fragment / AVB **NONE** / 64 MiB |
| 注入点 | SukiSU 只改 fragment **#2 `init_boot`**（2,772,501 → 3,326,204） |
| verity 状态 | `veritymode=eio` + `managed=yes` + `verifiedbootstate=orange` |
| kallsyms | 315,347 行（地址被清零，符号名/类型可用） |

**重新采集的命令**（换固件/OTA 后重跑）：
```bash
adb shell ls -l /dev/block/by-name/
adb shell getprop ro.boot.slot_suffix
adb shell uname -a
adb shell cat /proc/version
adb shell "zcat /proc/config.gz" > ref/tb330fu_defconfig
adb shell "cat /proc/kallsyms" > ref/kallsyms.txt
for p in boot_a vendor_boot_a init_boot_a dtbo_a lk_a vbmeta_a; do
  adb shell "blockdev --getsize64 /dev/block/by-name/$p"
done
```

### 1.3 备份（**部分已完成**）

**已完成**（`D:\TB330FU备份\`）：
- `boot_a.bin`（原厂，32 MiB，`digest_match=True`）← **干净的救砖镜像**
- `vendor_boot_a.bin`（原厂，64 MiB）
- `init_boot_a.bin`、`vbmeta_a.bin`
- `Sukisu BOOT\boot_a.img`、`Sukisu BOOT\vendor_boot_a.img`（当前生效镜像）

**仍缺（建议补，尤其 B 槽逃生镜像）**：
```bash
mkdir -p backup/b_slot
for p in boot_b vendor_boot_b init_boot_b dtbo_b lk_b vbmeta_b lk_a dtbo_a proinfo seccfg; do
  adb shell "dd if=/dev/block/by-name/$p of=/data/local/tmp/$p.img bs=4M"
  adb pull /data/local/tmp/$p.img backup/b_slot/
  adb shell "rm -f /data/local/tmp/$p.img"
done
sha256sum backup/b_slot/*.img > backup/b_slot/SHA256SUMS
```
> ⚠️ **B 槽只备份、不刷写**。它是 A 槽刷坏后唯一的 `fastboot --set-active=b` 逃生通道。

再用 **mtkclient 做 BROM 全量备份**（唯一能救"系统完全无法启动"的通道）：
```bash
python mtk.py rl backup/brom_full --partitions boot_a,vendor_boot_a,init_boot_a,dtbo_a,vbmeta_a,lk_a,seccfg,proinfo
```
> ⚠️ **绝不把 `preloader` 纳入任何刷写操作**。本机 `preloader_a/_b` 映射到 eMMC boot 区 `mmcblk0boot0/boot1`，写坏后 fastboot 与普通 BROM 读取都可能失效。

### 1.4 安全闸门（逐条打勾再动手）

- [ ] BROM 通道已验证可用（能进 BROM、mtkclient 能读到分区）
- [ ] **原厂** `boot_a` / `vendor_boot_a` 已落盘且 `sha256` 已记录（`D:\TB330FU备份\` 已有）
- [ ] **B 槽**逃生镜像已备份（`boot_b` / `vendor_boot_b` / `lk_b` / `vbmeta_b`）
- [ ] 已确认**绝不刷/勾 `preloader`**（映射 eMMC boot 区，最危险）
- [ ] 已知当前为 A 槽（`_a`），且知道 B 槽是逃生通道（`fastboot --set-active=b`）
- [ ] 手边有 `lkpatcher`，且**已确认 `lk_a` 是否已打补丁**（未打 → dm-verity corruption）
- [ ] Windows 构建机确认无 `bash`/`make` 环境 → **内核必须在 Linux/WSL2 上编**（见 1.1）

---

## 2. Step-by-step：源码获取 → 内核定制 → 打包

### Step 1 · 拉取 6.6.118 匹配源码（基线 = AOSP `kernel/common`）

> **关键认知**：`android15-6.6.118_r00` 是**存在的** tag → commit `5650aac9411fe9b3462b10d9f038054e94f86cbe`（Makefile SUBLEVEL=118）。
> 而原厂串里的 `-android15-8` **不是** ACK 分支（`kernel/common` 没有该分支），它是联想/MTK 在自己 `build.config` 里起的名字 → **只能靠 `CONFIG_LOCALVERSION` 硬编码复现**。

```bash
export WORK=$HOME/tb330fu && mkdir -p $WORK && cd $WORK
mkdir -p kernel && cd kernel
git init -q .
# 方式 A（推荐，体积小）：按 tag 拉精确 commit
git fetch --depth=1 https://android.googlesource.com/kernel/common android15-6.6.118_r00
git checkout -q --detach FETCH_HEAD
# 校验
grep -E '^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)' Makefile    # 必须 6 / 6 / 118 / (空)
git rev-parse HEAD                                                # 5650aac9411fe9b3462b10d9f038054e94f86cbe
```

**★ 实测结果（2026-10-30）**：本机 `uname -r` = `...-g`**`f9c84f40b6ba`**`-ab14657223-4k`。
`f9c84f40b6ba` **不是** `android15-6.6.118_r00`（那个是 `5650aac9…`），说明 Lenovo/MTK 在自己的 fork 上又打了一批补丁。所以：

- **不能指望"拉 tag 得到同源代码"**——`lenovo/gplcc#6` 至今未发布。
- **判据改为：不追求 commit 一致，只要求 `UTS_RELEASE` 逐字一致 + 导出符号 CRC 不变。**

### Step 1.2 · 提取原厂 defconfig（**唯一正确的 config 来源**）

> ★ **本步已完成**：`ref/tb330fu-6.6.118_defconfig`（7,752 行，已去 CRLF），直接来自本机 `/proc/config.gz`。

**不要用 `gki_defconfig`，也不要抄别人仓库的 defconfig**——本机 WiFi 是全栈模块驱动（实测 `CONFIG_WLAN=y`、`CONFIG_RFKILL=m`），AOSP 默认 config 会导致 WiFi 全挂。

```bash
# 重新提取（OTA / 换固件后）
adb exec-out "zcat /proc/config.gz" > ref/tb330fu_defconfig
tr -d '\r' < ref/tb330fu_defconfig | sed '/^$/d' > ref/tb330fu_defconfig.clean
grep -E '^CONFIG_(LOCALVERSION|LOCALVERSION_AUTO|MODVERSIONS|WLAN|RFKILL|FTRACE_SYSCALLS|IKCONFIG)' \
  ref/tb330fu_defconfig.clean
```

### Step 1.3 · 复现官方 vermagic（**★★★ 2026-10-30 实测已推翻原假设，务必先读 ★★★**）

> **原文档此处是错的。** 现在有了实机证据，结论完全反过来了。

**实测事实**：全部 344 个厂商 `.ko` 的 vermagic 是 `6.6.118-android15-8-g`**`0393b5aebcd1`**`-4k`，
而运行内核是 `...-g`**`f9c84f40b6ba`**`-`**`ab14657223`**`-4k` —— **两者完全不同，但模块能正常加载**。

**源码依据**（`kernel/module/version.c:80`）：
```c
int same_magic(const char *amagic, const char *bmagic, bool has_crcs)
{
	if (has_crcs) {
		amagic += strcspn(amagic, " ");   // ← 跳过第一个空格前的全部内容
		bmagic += strcspn(bmagic, " ");
	}
	return strcmp(amagic, bmagic) == 0;
}
```
本机 `CONFIG_MODVERSIONS=y` → 模块有 `__versions` → `has_crcs=true` → **版本串整体被跳过**，
只比较 ` SMP preempt mod_unload modversions aarch64`。

**因此**：

| 项 | 是否必须匹配 |
|---|---|
| 版本号 `6.6.118` | ❌ 不需要（被跳过） |
| `-android15-8-g<sha>-ab<buildid>-4k` | ❌ **不需要** |
| ` SMP`（CONFIG_SMP） | ✅ **必须** |
| ` preempt`（CONFIG_PREEMPT / PREEMPT_DYNAMIC） | ✅ **必须** |
| ` mod_unload`（CONFIG_MODULE_UNLOAD） | ✅ **必须** |
| ` modversions`（CONFIG_MODVERSIONS） | ✅ **必须** |
| ` aarch64` | ✅ 必须 |
| **`__versions` 里每个符号的 CRC** | ✅✅ **真正的硬约束** |

**结论**：不必伪造版本串。想让 `uname -r` 好看可以照下面做，但**它不是能否开机的关键**：

```bash
# 可选：仅为了 uname -r 观感一致（非必需）
sed -i 's|^CONFIG_LOCALVERSION=.*|CONFIG_LOCALVERSION="-android15-8-gf9c84f40b6ba-ab14657223-4k"|' configs/xxx_defconfig
sed -i 's|^CONFIG_LOCALVERSION_AUTO=y|# CONFIG_LOCALVERSION_AUTO is not set|' configs/xxx_defconfig
export LOCALVERSION=""
```

**真正的验收标准改成 CRC 比对**（见 Step 1.5）。

### Step 1.4 · 首次构建（**先做"零功能改动"的基线复现**，这是最重要的一次构建）

```bash
# 工具链必须是 r510928（实测：Android (11368308, +pgo, +bolt, +lto, +mlgo) clang 18.0.0）
export PATH=$WORK/tools/clang-r510928/bin:$PATH
export CC=clang LD=ld.lld AR=llvm-ar NM=llvm-nm OBJCOPY=llvm-objcopy OBJDUMP=llvm-objdump STRIP=llvm-strip

B=$WORK/out/baseline
mkdir -p $B && cp configs/tb330fu-6.6.118_defconfig $B/.config

make -C $WORK/kernel O=$B ARCH=arm64 LLVM=1 LLVM_IAS=1 olddefconfig
# ★ 验收 1：必须逐字符等于 6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k
make -s -C $WORK/kernel O=$B ARCH=arm64 kernelrelease

make -C $WORK/kernel O=$B ARCH=arm64 LLVM=1 LLVM_IAS=1 -j"$(nproc)" Image modules
# ★ 验收 2
file $B/arch/arm64/boot/Image
```

> ⚠️ **纯 make 与 kleaf 的差异**：原厂用 **PGO + BOLT + LTO + MLGO** 构建，纯 make 出来的**代码布局/函数顺序不同、二进制不可能一致**。但这**不影响 `.ko` 加载**——模块只看 `vermagic` + 导出符号 CRC，不看代码布局。因此不需要追求字节级复现。

### Step 1.5 · kABI / CRC 不变性验收（**决定能否保厂商模块**）

```bash
# 设备侧已抓：ref/kallsyms.txt（315,347 行；地址被 kptr_restrict 清零，符号名/类型可用）
adb exec-out "cat /proc/kallsyms" > ref/kallsyms.txt

# 构建侧导出表
awk -F'\t' '{print $2"\t"$1}' $B/Module.symvers | sort > $B/exports.txt
# 比对：新构建的每个导出符号 CRC 是否与原厂一致
python3 tools/crc-compare.py $B/Module.symvers ref/Module.symvers.ref
```

> 判据：**每一个导出符号的 CRC 必须不变**。变了 → 厂商 .ko 加载时 `disagrees about version of symbol` → WiFi/触屏/显示全挂，甚至开不了机。
> 参考实现：[Barley-Project `scripts/kernel/crc-compare.py`](https://github.com/Barley-Project/tb330fu-ubuntu)（该仓库已用此方法证明"纯 make 复现 + config 逐字对齐"可行）

### Step 1.6 · （可选）建 `vendor_boot` 里的 DTB 与模块的对应关系

本机 **无独立 `dtb` 分区**，DTB 在 `vendor_boot` 头的 `dtb_size`/`dtb_addr` 段；**有独立 `dtbo` 分区**（overlay，`dt_table` magic `0xd7b7ab1e`）。
编译产物 `arch/arm64/boot/dts/mediatek/` 下的 MTK 板级 DTS **在 GKI 源码里不存在**（那是 vendor 的），所以：

- **不要**自己编 DTB → 会丢节点/引用了不存在的 label → LK `ufdt` 精确匹配失败 → 开机中止。
- **正确做法**：DTB 从原厂 `vendor_boot` 原样保留，只替换内核段与 ramdisk 段。
- 如果你**确实**要改设备树（例如加 `regulator-always-on`、改中断极性），只在原厂 DTB 上用 `dtc`/`fdtoverlay` 做**最小 diff**，并保留 `dt_table` 结构与 `board_id`（`vendor_boot` 的 `dtb` 段若是 Android DT table，必须以 `d7b7ab1e` 开头、FDT 在偏移 64）。

---

### Step 2 · SUSFS / Zygisk 集成（**必须重编内核，不能 LKM**）

#### 2.1 路线选择（只有 A 是稳妥的）

| 路线 | 做法 | 结论 |
|---|---|---|
| **A（推荐）** | `kernel/common` 6.6.118 + **SukiSU `builtin` 分支**（自带 SUSFS 选项）+ `50_add_susfs_in_gki-android15-6.6.patch` | **唯一可生产**。代价：要解决 26 文件补丁冲突 |
| B | 第三方 `susfs4ksu-for-lkm`（运行期 `patch_memory`/`lsm_hook` 改内核内存） | 研究可行、生产不可靠；无 6.6 MTK 支持 |
| C | 不加 SUSFS，用 KSU 自身 hide + ZygiskNext | 最省事，隐藏能力弱（无 SUS_PATH / SUS_MOUNT / SUS_KSTAT） |

> **为什么 LKM 不行**：SUSFS 要在 `fs/namei.c`/`fs/namespace.c`/`fs/open.c` 内部插调用点，并 `obj-$(CONFIG_KSU_SUSFS) += susfs.o` 编进 `fs/`。这是"改内核树"，不是"加模块"。同一个障碍也是 Barley-Project 把 KSU 编进模块时 `modpost: undefined` 的原因。

> **★ 本机实测前提**：原厂 defconfig **完全没有** `CONFIG_KSU` / `CONFIG_KSU_SUSFS`（grep 全空），是干净 GKI。所以 SUSFS 一定要求**重编内核**，无法在当前 LKM Root 上"加装"。

#### 2.2 具体步骤

```bash
# ① 取 SUSFS 6.6 补丁与文件（上游是 gitlab simonpunk/susfs4ksu:gki-android15-6.6；
#    国内/限流可用 GitHub 镜像 Zarathos30/susfs4ksu:gki-android15-6.6-dev）
S=$WORK/susfs && mkdir -p $S
cp susfs/kernel_patches/50_add_susfs_in_gki-android15-6.6.patch            $WORK/kernel/
cp susfs/kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch             $WORK/kernel-dir-of-ksu/
cp susfs/kernel_patches/fs/susfs.c                                          $WORK/kernel/fs/
cp susfs/kernel_patches/include/linux/susfs.h  susfs/kernel_patches/include/linux/susfs_def.h \
                                                                            $WORK/kernel/include/linux/

# ② 先干跑，看冲突在哪
cd $WORK/kernel
git apply --check --verbose 50_add_susfs_in_gki-android15-6.6.patch ||   \
  patch -p1 --dry-run --fuzz=3 -i 50_add_susfs_in_gki-android15-6.6.patch

# ③ 落补丁（git apply 整包拒绝时，GNU patch 的 fuzz 往往能干净落位）
git apply 50_add_susfs_in_gki-android15-6.6.patch 2>/dev/null || \
  patch -p1 --fuzz=3 --no-backup-if-mismatch -i 50_add_susfs_in_gki-android15-6.6.patch
# ★ 验收：不允许有 .rej
find . -name '*.rej' -print
```

**必踩的冲突点（按风险排序，逐个处理）**

| 文件 | hunk 量 | 风险点 | 处理建议 |
|---|---|---|---|
| `fs/namei.c` | **17 个** | `lookup_fast` / `__lookup_slow` / `walk_component` / `lookup_open` | 最容易漂；**先 `--dry-run` 看 fuzz 是否 >2**，超了就读 `.rej` 手工落位 |
| `fs/namespace.c` | 6 处（含 119 行新 helper） | `clone_mnt` / `vfs_create_mount` / `mnt_alloc_id` / `mnt_free_id` | 插在 `mnt_get_count` 之后 |
| `kernel/sys.c` | 6 处 | `override_release`（uname 伪造）、`setresuid` | 6.6 各 sublevel 有微调 |
| `security/selinux/{avc.c,hooks.c,selinuxfs.c}` | 3 个文件 | 日志伪造 | 若厂商改了 SELinux hook 表，hunk 会整体失败 |
| `fs/proc/{base.c,task_mmu.c,fd.c}` + `fs/proc_namespace.c` + `mm/memory.c` | 多个 | maps/mounts 隐藏、SUS_MAP | |
| `fs/Makefile` | 1 行 | `obj-$(CONFIG_KSU_SUSFS) += susfs.o` | 追加在 `obj-y` 块后 |

**Kconfig 调整（实测名称，6.6 分支）**

```kconfig
# fs/Kconfig 不用改：CONFIG_KSU_SUSFS 定义在 KernelSU 的 kernel/Kconfig 里（menu "KernelSU - SUSFS"）
config KSU_SUSFS                    # bool, depends on KSU && THREAD_INFO_IN_TASK, default y
config KSU_SUSFS_SUS_PATH           # default y
config KSU_SUSFS_SUS_MOUNT          # default y
config KSU_SUSFS_SUS_KSTAT          # default y
config KSU_SUSFS_SPOOF_UNAME        # default y
config KSU_SUSFS_ENABLE_LOG         # default y
config KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS
config KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG
config KSU_SUSFS_OPEN_REDIRECT
config KSU_SUSFS_SUS_MAP
```

> ⚠️ **`KSU_SUSFS_HAS_MAGIC_MOUNT` 在 6.6 分支已不存在**（grep 命中 0），别去开它。
> ⚠️ **全是 `default y`**：如果你的流程先 `olddefconfig` 再叠 fragment，会把 KSU/SUSFS 默认打开。**必须先把 `config KSU` 的 `default y` 改成 `default n`**，只在目标 fragment 里显式 `=y`。

**SukiSU 内核侧接入（`builtin` 分支）**

```bash
# 用 builtin 分支：main 分支里没有 SUSFS 代码（实测 Kconfig 命中 0 / 22）
git clone -b builtin --depth=1 https://github.com/SukiSU-Ultra/SukiSU-Ultra $WORK/SukiSU
cd $WORK/kernel
ln -s $WORK/SukiSU/kernel drivers/kernelsu
echo 'obj-$(CONFIG_KSU) += kernelsu/' >> drivers/Makefile
echo 'source "drivers/kernelsu/Kconfig"' >> drivers/Kconfig
# 把 kernel/Kconfig 里 config KSU 的 default y 改为 default n
```

**必需的 config 增量（`builtin` + tracepoint 路线都需要的底座）**

```kconfig
CONFIG_KSU=y
CONFIG_KSU_SUSFS=y
CONFIG_KPROBES=y
CONFIG_KRETPROBES=y
CONFIG_KALLSYMS=y
CONFIG_KALLSYMS_ALL=y
CONFIG_EXT4_FS=y
CONFIG_FTRACE_SYSCALLS=y        # ★ 官方出厂 config 里没有，缺了链接失败
CONFIG_TRACEPOINTS=y
```

**Zygisk 的正确集成点：不在内核，在用户态**

```bash
# 内核编好、root 起来之后，用 KSU 管理器装模块（三选一）
#  ZygiskNext（功能最全，v4-0.9.2 起闭源）  /  ReZygisk（C 重写，AGPL）  /  NeoZygisk（ptrace）
# 要求：KernelSU ≥ 10940，ksud ≥ 11575（SukiSU 派生需自行核对）
adb push ZygiskNext.zip /sdcard/Download/     # 然后在 KSU 管理器「模块」里安装并重启
```
> 结论：**"内核级 Zygisk"是不存在的东西**。内核侧能做的只有 SUSFS（文件/挂载/kstat 不可见），Zygisk 的 .so 注入必须靠用户态。不要把 SUSFS 当 Zygisk 的替代，二者互补。

#### 2.3 第二次构建（带 SUSFS）与验收

```bash
B=$WORK/out/susfs
cp $WORK/out/baseline/.config $B/.config     # 从基线 config 出发，避免"从头 olddefconfig"漂移
# 叠 fragment
cat > $B/susfs.fragment <<'EOF'
CONFIG_KSU=y
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_PATH=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_FTRACE_SYSCALLS=y
EOF
(cd $B && $WORK/kernel/scripts/kconfig/merge_config.sh -m -O $B .config susfs.fragment)
make -C $WORK/kernel O=$B ARCH=arm64 LLVM=1 LLVM_IAS=1 olddefconfig
grep -E '^CONFIG_(KSU|KSU_SUSFS|FTRACE_SYSCALLS)' $B/.config     # 全部 =y 才算生效

# ★ 验收 1：kernelrelease 仍与 uname -r 一致
make -s -C $WORK/kernel O=$B ARCH=arm64 kernelrelease
# ★ 验收 2：校验 CRC 不变
python3 tools/crc-compare.py $B/Module.symvers ref/Module.symvers.ref
# ★ 验收 3：编
make -C $WORK/kernel O=$B ARCH=arm64 LLVM=1 LLVM_IAS=1 -j"$(nproc)" Image modules
```

---

### Step 3 · DTB 与 Vendor 模块匹配 + 安全重打包（不破坏 AVB）

#### 3.1 定位与原则

| 段 | 位置（**本机实测**） | 我们是否动它 |
|---|---|---|
| 内核 | `boot_a` 的 `kernel` 段：offset 4096，**14,623,212 B，gzip** | **替换** |
| ramdisk | `boot_a` 的 `ramdisk` 段：**`ramdisk_size=0`**（原厂无 ramdisk） | 原厂保留；SukiSU 会新建 554,410 B 的 cpio |
| DTB | **`vendor_boot_a` 头的 `dtb` 段：offset 27,594,752，156,675 B，magic `0xd7b7ab1e`（DT-table）** | **保留原厂** |
| dtbo | 独立分区 `dtbo_a`（8 MiB，`mmcblk0p31`） | **不动**（动它等于动 LK 的 overlay 匹配） |
| vendor 模块 `.ko` | `vendor_boot_a` 的 fragment **#0**（`name=""`, `type=platform`, 7,081,946 B）里的 `/lib/modules/` | **保留原厂**（除非明确要换某个 .ko） |
| recovery ramdisk | fragment **#1**（`name=recovery`, 17,732,269 B） | **不动** |
| KSU 注入 | fragment **#2**（`name=init_boot`, 原厂 2,772,501 B → SukiSU 后 3,326,204 B） | SukiSU 独占 |

> **两个反直觉点（已被本机实测印证）**：
> 1. **不要把 `.ko` 自己加到 DLKM 段**——mkbootimg 对 `--vendor_ramdisk` 主体盘**硬编码为 `PLATFORM(1)` 且 name 为空**；本机 3 个 fragment 里**没有任何 DLKM(3)**。手加 DLKM 条目必须同步重算 table 的 size/offset/num，写错即砖。
> 2. **SukiSU 的 `ksud` 不碰 `/lib/modules`、不改 `init.rc`、不加 DLKM**：它只在 fragment #2 里放 `/init`（=`ksuinit`）+ `/kernelsu.ko` + `/ksu_config`，原 `init` 改名 `init.real`。**实测证据**：fragment #0/#1 的 size 与 offset **一字节未变**，只有 #2 变大 553,703 B，dtb 段内容不变、位置后移。

#### 3.2 重打包命令（两种，A 最稳）

**做法 A（推荐）：`vkrepack.py splice` —— 只换内核段，其余沿用原镜像**

```bash
cd $WORK
# 0) 把新内核压成 gzip（★ 实测：原厂 boot_a 的 kernel 段就是 gzip，14,623,212 B）
gzip -9 -c out/susfs/arch/arm64/boot/Image > new-kernel.gz
# 1) 用原厂 boot_a 做骨架，替换 kernel 段；补零到 32 MiB 并加 NONE footer
python3 scripts/vkrepack.py splice /path/to/boot_a.bin \
    --kernel new-kernel.gz \
    --partition-size 33554432 \
    -o patched/boot_a.custom.img \
    --avb none \
    --avbtool "$WORK/tools/avb/avbtool.py" \
    --prop-fingerprint "Lenovo/TB330FU_PRC/TB330FU:15/AP3A.240905.015.A2/S2003138_261010_ROW:user/release-keys" \
    --prop-security-patch "2026-10-05" \
    --prop-avb-version 15
# 2) 自检：结构 + footer
python3 scripts/vkrepack.py verify patched/boot_a.custom.img --partition-size 33554432
python3 tools/avb/avbtool.py info_image --image patched/boot_a.custom.img | head -30
#    期望：Algorithm: NONE，Image size == 33554432，Original image size == 补零前长度
```

**做法 B（AOSP 官方往返，改动 vendor_boot 时必用）**

```bash
# 只用 android15-release 的 python 工具（android-11 的只支持 v3，会丢 table）
python3 mkbootimg/unpack_bootimg.py --boot_img vendor_boot_a.img --out vb_a --format=mkbootimg | tee vb_a.args
# 修改 vb_a/vendor_ramdisk02（SukiSU 注入点 init_boot）后：
python3 mkbootimg/mkbootimg.py --header_version 4 --vendor_boot patched/vendor_boot_a.img $(cat vb_a.args)
# 自检：dtb 段与 fragment 列表必须一致
python3 mkbootimg/unpack_bootimg.py --boot_img patched/vendor_boot_a.img --out chk --format=info
cmp vb_a/dtb chk/dtb && echo "dtb 未变 OK"
cmp vb_a/vendor_ramdisk00 chk/vendor_ramdisk00 && echo "主体盘未变 OK"
```

> ⚠️ **不要手写 `mkbootimg` 的全量参数**。本机真实值为 `page_size=4096`、`kernel_addr=0x40080000`、`ramdisk_addr=0x47c80000`、`tags_addr=dtb_addr=0x4bc80000`、`cmdline='bootopt=64S3,32N2,64N2'`、`name=''`。
> 而 TWRP 设备树里的 `BOARD_BOOTIMG_HEADER_VERSION := 2`、`BOARD_KERNEL_BASE := 0x40078000`、`BOARD_RAMDISK_OFFSET := 0x07c08000` 等**全是错的**（那是 4.19 时代反推值），**不可信**。

#### 3.3 AVB：既不要"破坏签名"，也不要"不留 footer"

TB330FU 的现实是：**LK 解析 footer 是硬要求，但 orange 解锁态不校验签名**。所以正确做法不是"保留原签名"（做不到——内核一换，原始 hash 就不匹配），而是：

| 方案 | 命令 | 适用 |
|---|---|---|
| **NONE footer（推荐）** | `avbtool add_hash_footer --image <img> --partition_size <N> --partition_name boot --algorithm NONE --salt <固定32B> --prop ...` | 结构合法、无签名可验 → LK 放行 |
| 自签 RSA4096 | `make_vbmeta_image --key avb_private.pem --algorithm SHA256_RSA4096 --flags 3 --include_descriptors_from_image ...` | 需要恢复"看起来正常"的 verified boot；更复杂 |
| 完全不留 footer | 不推荐 | 半截镜像 / 全零尾 **都会冻结在第一屏** |

一手可抄的 `add_hash_footer` 参数（**本机真值已填入**）：
```bash
python3 avbtool.py add_hash_footer \
  --image boot.img --partition_size 33554432 --partition_name boot \
  --algorithm NONE \
  --salt 014d0f17ca8359f23ed8b03e35e679b983b87f8f4d4636a9d5e41c6e19a09c0c \
  --prop "com.android.build.boot.os_version:15" \
  --prop "com.android.build.boot.fingerprint:Lenovo/TB330FU_PRC/TB330FU:15/AP3A.240905.015.A2/S2003138_261010_ROW:user/release-keys" \
  --prop "com.android.build.boot.security_patch:2026-10-05"
python3 avbtool.py info_image --image boot.img
```
> ⚠️ 上面指纹与 patch 已换成本机实测值（`S2003138_261010_ROW` / `2026-10-05`）。**不要照抄任何教程里的 `S2001962_250417_ROW` / `2024-09-05`。**
>
> ⚠️ **实测差异**：原厂 `boot_a` 的 footer 是 **`SHA256_RSA4096`（真签名）**，而原厂 `vendor_boot_a` 的 footer 是 **`NONE`**。所以：
> - 改 `vendor_boot` → 补 NONE footer 是**复刻原厂**；
> - 改 `boot` → 从 RSA4096 降级为 NONE，是**实测可行的放宽**（orange 解锁态跳过校验）；
> - 另注意：SukiSU 改完 `boot` 后**没有重算 footer**，`digest_match=False`（你机器现在就是这个状态，能开机纯靠 orange）。

#### 3.4 刷写与关闭校验（顺序不能反）

```bash
# 0) 先备份 vbmeta（本机已有 D:\TB330FU备份\vbmeta_a.bin）
# 1) 刷 boot（只刷 A 槽）
fastboot flash boot_a patched/boot_a.custom.img
# 2) 如果你也换了 vendor_boot：
fastboot flash vendor_boot_a patched/vendor_boot_a.img
# 3) 关校验：两条路都走一遍，互为保险
fastboot --disable-verity --disable-verification flash vbmeta_a vbmeta_a.img   # 原厂备份
#    再进 BROM 打一次（写 vbmeta_a 偏移 0x78 = BE(3)）
python mtk.py da vbmeta 3
# 4) 重启并验收
fastboot reboot
adb wait-for-device
adb shell getprop sys.boot_completed            # 期望 1
adb shell uname -r                              # 期望 6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k
adb shell su -c id                              # 期望 uid=0
adb shell "cat /proc/bootconfig | grep -iE 'veritymode|verifiedboot'"
#   期望 verifiedbootstate=orange；veritymode 可能仍是 eio + managed=yes（本机实测即为该状态，
#   属于"托管"模式，出错返回 EIO 而非重启；不要误以为必须变成 disabled）
```

---

## 3. OTA 无缝重注入脚本（`scripts/ota-reinject.sh`）

> 系统 OTA 会**覆盖当前槽的 `boot` 与 `vendor_boot`**，并把 `vbmeta` 重置回原厂（flags 归零）→ Root 丢失 + 校验恢复。脚本要做的是"OTA 之后一条命令重新长回 Root"。

### 3.1 脚本流程

```
前置检查(工具/设备/磁盘/mtk.py)
   ↓
槽位检测(getprop → fastboot getvar → by-name 推断；不一致则中止)
   ↓
镜像提取(dd from by-name → boot_<slot>.img / vendor_boot_<slot>.img)
   ↓ 校验：尺寸合理 + VNDRBOOT magic + sha256 落 manifest（并发检测 init_boot 决定注入目标）
修补(SukiSU: boot + vendor_boot 双分区；ksud 必须显式 --partition vendor_boot --kmi android15-6.6)
   ↓ 产物：整分区尺寸 + AVB NONE footer（短镜像会被 LK 拒绝）
刷写(fastboot flash boot_<slot> / vendor_boot_<slot>；刷前再次确认 slot 一致)
   ↓
关校验(fastboot --disable-verity --disable-verification flash vbmeta_<slot> <原厂备份>
        + python mtk.py da vbmeta 3 )        ← 这一步失败则拒绝重启
   ↓
重启 → 等 boot_completed → 验 su -c id → 失败自动打印回滚指令
```

### 3.2 用法

```bash
./ota-reinject.sh --slot auto --yes            # 标准流程
./ota-reinject.sh --dry-run                    # 只检测+提取+报告，不修补不刷写（OTA 后先跑这个）
./ota-reinject.sh --skip-flash                 # 只提取+修补，产出镜像人工校验
./ota-reinject.sh --rollback                   # 用 backup/ 里的原厂镜像回刷 + da vbmeta 3
```

### 3.3 关键实现约束（写死在脚本里）

1. **绝不刷 preloader**（任何分支都没有该操作，SP Flash Tool 全量刷机时同理不勾）。
2. **不自动 `da seccfg unlock`**（默认关闭，需显式 `--unlock`）。
3. **`mtk.py da vbmeta 3` 只写当前槽** → 若 `--slot b` 而设备在 a，脚本先 `fastboot --set-active=b`，并在日志里注明"另一槽 vbmeta 未处理"。
4. **刷写前校验镜像尺寸 == 分区尺寸**，不等就中止（防"第一屏冻结"）。
5. **幂等**：同 slot、同 sha256 的原厂镜像已修补过则跳过修补（`.cache-<sha256>` 标记），避免重复注入造成 ramdisk 里两份 `init`。
6. **可插拔修补后端**：`patch_image()` 对 `ksud` 与 `boot_patch.sh` 两种入口都留位；检测不到就**报错退出**，不静默继续。
7. `set -Eeuo pipefail` + `trap ERR` 打印"当前阶段 + 已完成步骤"，杜绝半途静默。

---

## 4. 最可能导致 Kernel Panic / 变砖的风险点与回滚预案

### 4.1 风险清单（按"杀机"概率排序）

| # | 风险 | 表现 | 根因 | 预防 | 回滚 |
|---|---|---|---|---|---|
| **R1** | **vermagic 不一致** | 开机黑屏/反复重启；dmesg `disagrees about version of symbol` 或 `version magic ... should be ...` | `UTS_RELEASE` 少一个字符 → **所有厂商 .ko 拒绝加载**（WiFi/触屏/显示全挂） | 目标串必须是 **`6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k`**；`make kernelrelease` 逐字符比对；`LOCALVERSION=""`；`CONFIG_LOCALVERSION_AUTO=n`（**原厂是 y，必须改**） | 刷回原厂 boot |
| **R1b** | **`CONFIG_LOCALVERSION` 只写 `-4k`** | 同上（得到 `6.6.118-4k`） | 误以为纯 make 也会像 kleaf 一样拼出 `-android15-8-g...-ab...` | 纯 make 路线**必须**把完整后缀写进 `CONFIG_LOCALVERSION` | 同上 |
| **R2** | **导出符号 CRC 变化**（kABI 破坏） | 能开机但设备缓慢崩溃、`Unknown symbol`、GPU/WiFi 起不来 | defconfig 不是原厂的（实测 `CONFIG_RFKILL=m` / `CONFIG_WLAN=y`）、打开了会改变结构体布局的选项 | 用 `ref/tb330fu-6.6.118_defconfig`；**任何新选项先查是否改 `task_struct`/kABI 结构体** | 同上 |
| **R2b** | **纯 make vs 原厂 kleaf(PGO/LTO/MLGO) 差异** | *预期内的差异，不是故障* | 原厂用 `+pgo,+bolt,+lto,+mlgo` 构建，代码布局不同 | **不影响 .ko 加载**（只看 vermagic + CRC）；不要为"二进制不一致"纠结 | — |
| **R3** | **SUSFS 补丁 hunk 错位**（`fs/namei.c` 17 处） | 编译通过但一进 App 就 panic（空指针/`kernel paging request`），或开机 30 秒内死机 | fuzz 落位落错位置；`susfs_fake_qstr_name` 相关的 dentry 生命周期被改坏 | **严禁 `.rej`**；fuzz>2 时人工重写 hunk；`git apply --check` 通过才继续；单独编译只带 SUSFS 不带 KSU 跑一次静态检查 | 刷回；或退回路线 C（不加 SUSFS） |
| **R4** | **boot 镜像尺寸/footer 不对** | **第一屏冻结**（不重启、无 adb、无日志） | 镜像短于分区（残留旧 footer 指向被覆盖的 vbmeta）；或整分区全零尾 | 补零到分区尺寸 + `add_hash_footer --algorithm NONE`；刷前 `verify` 校验尺寸 | 用原厂 boot 重刷（fastboot 可救） |
| **R5** | **vendor_boot 重打包写坏 table/dtb** | 第一屏冻结或 `dtb` 解析失败 | 手写 mkbootimg 参数；自己加 DLKM 条目；改了 dtb 段 | 只用 `unpack_bootimg --format=mkbootimg` 往返；dtb 段 `cmp` 必须一致 | 刷回原厂 vendor_boot |
| **R6** | **SukiSU 补错分区** | 刷完仍无 Root（且白白改了 boot） | `ksud` 的 `choose_boot_partition()` **没有 vendor_boot 分支**；无 init_boot → 回退 `boot` | **必须** `--partition vendor_boot --kmi android15-6.6` | 无需回滚（只是白做） |
| **R7** | **`da vbmeta 3` 没执行/写错槽** | 能开机但 `dm-verity corruption` → 反复重启 | 只写当前槽；fastboot 的 `--disable-*` 只对当次 buffer 生效 | 刷完先 `mtk.py da vbmeta 3` 再重启；切槽后重跑 | 进 BROM 重跑 `da vbmeta 3` |
| **R8** | **KSU/SUSFS 默认 `default y` 被 olddefconfig 打开** | 基线构建莫名带上 KSU，导致基线本身跑不起来（Barley-Project 实测：KSU builtin 刷 boot_a 后**失联、原因未查明**） | `config KSU` 是 `default y` | 先把 `default y` 改 `default n`，只让 fragment 显式打开 | 退回纯 LKM 模式（SukiSU 官方 kernelsu.ko），**不要**在第一次就换内核 |
| **R9** | **反回滚（anti-rollback）** | LK 拒绝启动到 recovery/BROM 之外的路径 | TWRP 设备树里 `PLATFORM_SECURITY_PATCH := 2099-12-31`（防 anti-rollback 的 hack）说明该机可能启用 | `--rollback_index 0`；**不要降级刷老固件** | BROM 线刷同版本或更高版本全量包（**不勾 preloader**） |
| **R10** | **DTB overlay 精确匹配失败** | LK 中止（无显示或直接断电） | CN `dtbo` 引用基础树里没有的 label；改了 DTB 节点名 | **不动 dtbo、不改 DTB 节点名**；用 `ufdt_overlay_test_app` 先验证 | 刷回原厂 dtbo / vendor_boot |
| **R11** | **MTK 中断极性 `INT_POL` 未初始化** | `i2c-mt65xx` 加载即**中断风暴**，CPU0 被打满、循环重启 | ROW 6.6 设备树假定 `INT_POL`（`0x0c53a650`）已被固件预置，但某些链路没人写 | 本机是 ROW 链路，**原厂内核能启动说明已预置**；只有换 DTB/换基线时才需要处理（Barley-Project 的 `tb330fu_intpol.ko` 方案） | 刷回原厂内核 |

### 4.2 分层回滚预案

**L1 — 系统能进 fastboot（最常见：第一屏冻结但 fastboot 可用）**
```bash
fastboot devices                                     # 确认在线
fastboot flash boot_a      "D:\TB330FU备份\boot_a.bin"              # 原厂，digest_match=True
fastboot flash vendor_boot_a "D:\TB330FU备份\vendor_boot_a.bin"     # 原厂
fastboot --disable-verity --disable-verification flash vbmeta_a "D:\TB330FU备份\vbmeta_a.bin"
fastboot reboot
```
**若仍冻结 → 切 B 槽（本机 B 槽为完整 A/B 镜像，是首选逃生通道）**
```bash
fastboot --set-active=b
fastboot reboot
# 起来之后：确认 boot_b / vendor_boot_b 是原厂；B 槽的 vbmeta_b 也要打 3
# 之后可再从 B 槽切回 A（修复 A 槽后 fastboot --set-active=a）
```

**L2 — 系统能进 BROM（fastboot 挂死 / 分区被写坏）**
```bash
python mtk.py w boot_a        "D:\TB330FU备份\boot_a.bin"
python mtk.py w vendor_boot_a "D:\TB330FU备份\vendor_boot_a.bin"
python mtk.py w vbmeta_a      "D:\TB330FU备份\vbmeta_a.bin"
python mtk.py da vbmeta 3
python mtk.py reset
```

**L3 — 全分区损坏 / 无法进任何模式（终极）**
```
SP Flash Tool + TB330FU ROW 全量固件（Download Only / Firmware Upgrade）
  ★ 必须手动取消勾选 preloader（铁律 2）
    本机 preloader_a/_b 映射到 eMMC boot 区 mmcblk0boot0/boot1，写坏后 fastboot/BROM 都可能救不回
  刷完 re-flash lkpatcher 处理过的 lk_a/lk_b（若之前刷过原版 lk）
  → 再走一次 mtk.py da seccfg unlock + da vbmeta 3
```

**L4 — 内核能起但 SUSFS/KSU 不稳定（软回滚，不刷分区）**
```bash
# 保留自定义内核，只退回"官方 LKM 模式" root：
# 用 D:\TB330FU备份\Sukisu BOOT\ 里的两个镜像回刷（那是当前已验证可启动的 LKM 状态）
fastboot flash boot_a         "D:\TB330FU备份\Sukisu BOOT\boot_a.img"
fastboot flash vendor_boot_a  "D:\TB330FU备份\Sukisu BOOT\vendor_boot_a.img"
python mtk.py da vbmeta 3
```

### 4.3 排障信道

- **本机 `ramoops` 已启用**（实测 cmdline：`ramoops.mem_address=0x4d010000 ramoops.mem_size=0xe0000 ramoops.pmsg_size=0x80000 ramoops.console_size=0x40000`）→ panic 日志**可以落 pstore**，重启后从 `/sys/fs/pstore/` 取。
- **MTK `expdb` 分区**（`mmcblk0p6`）也会收到 LK 层面的日志。
- 最实用策略：**M1/M2 阶段只刷 `boot_a`，B 槽保持原厂**；出问题直接 `fastboot --set-active=b`。
- 日志取证命令（设备能起时）：
  ```bash
  adb shell "ls -l /sys/fs/pstore/"
  adb shell "cat /sys/fs/pstore/console-ramoops-0" | tail -200
  adb shell "dmesg" | tail -200
  ```

---

## 5. 执行顺序建议（里程碑，不要一步到位）

| 里程碑 | 内容 | 通过判据 | 失败回退 |
|---|---|---|---|
| **M0** | 取证 + 备份（§1.2/§1.3） | ✅ **已完成**：`device-facts.md` 已填满；A 槽原厂镜像已备份 | 补 B 槽逃生镜像 |
| **M1** | 基线内核复现（Step 1.4/1.5），**不带任何功能改动** | `make kernelrelease` == `6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k` + CRC 不变 | 不刷机，纯构建验证 |
| **M2** | 只替换内核段（`vkrepack splice` + NONE footer），**只刷 boot_a** | 开机、`su -c id` 正常、WiFi/触屏正常（LKM 仍在 vendor_boot） | 刷回 `D:\TB330FU备份\boot_a.bin` |
| **M3** | 内核内置 KSU（`builtin`，**先不开 SUSFS**） | 开机且 Root 由内核提供 | 退回 M2（LKM 模式） |
| **M4** | 叠 SUSFS 补丁 + 编 | 开机 + `ksu_susfs show version` 有输出 | 退回 M3 |
| **M5** | 装 ZygiskNext/ReZygisk + LSPosed | 模块加载、目标 App 能被 hook | 禁用模块 |
| **M6** | 跑 `ota-reinject.sh --dry-run` → 真实 OTA → `--yes` | OTA 后一条命令恢复 Root | `--rollback` |

> **为什么坚持这个顺序**：`Barley-Project` 的实测记录里，**"把 CONFIG_KSU 编进内核 → 构建通过、CRC 无变化 → 刷 boot_a 后平板失联，原因未查明"**。所以 M1/M2 必须先证明"纯替换内核段能开机"，再谈 KSU 内置，最后才谈 SUSFS。否则一次失败就同时引入 3 个未知变量。

---

## 6. 交付物索引

| 文件 | 说明 |
|---|---|
| [`research/device-facts.md`](../research/device-facts.md) | **★ 本机实测事实总表**：分区表、内核串、config、镜像结构、AVB、verity 状态（所有"实测"的数据源） |
| [`research/01-source-susfs-zygisk.md`](../research/01-source-susfs-zygisk.md) | 源码获取、SUSFS 26 文件落点、Zygisk 现状、LKM 注入机制（593 行，逐条证据） |
| [`research/02-vendorboot-dtb-avb.md`](../research/02-vendorboot-dtb-avb.md) | vendor_boot v4 结构、DTB/dtbo、AVB/`da vbmeta 3` 源码级解释、SukiSU 行为（655 行，29 条引用） |
| [`scripts/vkrepack.py`](../scripts/vkrepack.py) | boot/vendor_boot 解包 · 重打包 · 替换内核段 · AVB NONE footer · 结构自检 |
| [`scripts/test_vkrepack.py`](../scripts/test_vkrepack.py) | 上述工具的单元测试（合成镜像 roundtrip） |
| [`scripts/ota-reinject.sh`](../scripts/ota-reinject.sh) | OTA 后自动重注入 + 关校验 + 验证 + 回滚 |
| `ref/tb330fu-6.6.118_defconfig` | 本机原厂 defconfig（7,752 行，已去 CRLF）—— **构建的唯一 config 基准** |
| `ref/kallsyms.txt` | 本机 `/proc/kallsyms`（315,347 行）—— CRC 基线比对用 |
| `research/raw/` | 所有一手原文存档（README/脚本/补丁/Kconfig/CI yml/源码片段） |

---

## 7. 尚未确定的事项（volatile，OTA 后需重新取证）

以下已经从"未知"变为"已知"，但**每次 OTA 都可能变**，脚本必须重新采集而非假设：

1. `boot_a` / `vendor_boot_a` 的 `kernel_size`、`dtb_size`、fragment 尺寸 —— **OTA 后必变**，重跑 `vkrepack.py info`。
2. 内核串里的 `g<sha>` 与 `ab<buildid>` —— **每次固件更新都会变**（当前 `gf9c84f40b6ba` / `ab14657223`）。
3. `dtbo_a` 是否被顶层 `vbmeta_a` 收录 —— `avbtool info_image --image vbmeta_a.bin | grep -i dtbo`。
4. `lk_a` 是否已被 lkpatcher 补过（决定"不刷 LK"是否成立）——`python3 -m lkpatcher lk_a.bin --analyze-policies --dry-run`。
5. 是否启用 anti-rollback —— 只能从"能否刷入低版本"实测；保守策略：`--rollback_index 0` 且不降级固件。
6. `boot_a.bin` 为何仍带合法 `SHA256_RSA4096` footer（它是 OTA 后原厂镜像？还是 SukiSU 保留了 footer？）—— 用 `Sukisu BOOT\boot_a.img` 对比已确认结构差异，但**来源待你确认**。
