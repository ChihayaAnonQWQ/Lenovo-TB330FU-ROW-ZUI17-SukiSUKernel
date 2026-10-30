# 设备实测事实（Device Facts）

> 采集时间：2026-10-30 16:08 (Asia/Hong_Kong)
> 采集方式：`adb`（adbd 以 root 运行），设备 `HA2FDDCR`
> 性质：**全部为本机实测，可直接作为构建与打包的权威输入**

---

## 1. 设备身份

| 项 | 值 |
|---|---|
| 型号 / 代号 | `TB330FU` / `TB330FU` |
| 产品名 | `TB330FU_PRC` |
| SoC | `mt6768`（`boot.chipid=MT8786_8+128_18`，CPU arm64-v8a） |
| 屏幕 | 1200×1920，密度 240，`LCM_name=hx83102j_dsi_vdo_boe_drv` |
| 系统 | Android 15 (35)，ZUI 17.0.074 |
| 编译版本 | `S2003138_261010_ROW` |
| fingerprint | `Lenovo/TB330FU_PRC/TB330FU:15/AP3A.240905.015.A2/S2003138_261010_ROW:user/release-keys` |
| security_patch | `2026-10-05` |
| Bootloader | **unlocked**（`verifiedbootstate=orange`） |
| A/B | **`ro.build.ab_update=true`**，当前 `ro.boot.slot_suffix=_a`（**作业只在 A 槽**） |

## 2. 内核（权威串）

```
6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k
```

`uname -a`：
```
Linux localhost 6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k #1 SMP PREEMPT Sat Jan  3 21:50:53 UTC 2026 aarch64 Toybox
```

`/proc/version`：
```
Linux version 6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k (kleaf@build-host)
(Android (11368308, +pgo, +bolt, +lto, +mlgo, based on r510928) clang version 18.0.0
 (https://android.googlesource.com/toolchain/llvm-project 477610d4d0d988e69dbc3fae4fe86bff3f07f2b5), LLD 18.0.0)
#1 SMP PREEMPT Sat Jan  3 21:50:53 UTC 2026
```

**串的构成（已确证）**

```
6.6.118 - android15-8 - gf9c84f40b6ba - ab14657223 - 4k
   │           │              │                │         └ CONFIG_LOCALVERSION 原值
   │           │              │                └ kleaf build id
   │           │              └ 源码 commit 短 sha（Lenovo/MTK fork，非 ACK tag）
   │           └ build.config 分支名（ACK `kernel/common` 无此分支）
   └ Makefile 版本
```

**关键结论**
- 原厂用 **kleaf + PGO + BOLT + LTO + MLGO** 构建（`kleaf@build-host`），不是纯 make。
- 原厂 `CONFIG_LOCALVERSION="-4k"`，**完整后缀由 kleaf 注入**，不是写死在 config 里。
- 原厂 `CONFIG_LOCALVERSION_AUTO=y`（**注意**：复现时必须关掉，否则 `setlocalversion` 会追加 `-g<hash>` 或 `+`）。
- 编译器：**clang r510928 (18.0.0) + LLD 18.0.0**，来源 `prebuilts/clang/host/linux-x86`（非 NDK）。

## 3. 原厂内核配置（关键项，实测）

| 符号 | 值 | 说明 |
|---|---|---|
| `CONFIG_LOCALVERSION` | `"-4k"` | 原值 |
| `CONFIG_LOCALVERSION_AUTO` | **`y`** | 复现时必须改为 `n` |
| `CONFIG_IKCONFIG` / `_PROC` | `y` / `y` | 可直接 `zcat /proc/config.gz` |
| `CONFIG_MODVERSIONS` | **`y`** | 厂商 `.ko` 校验 vermagic + 符号 CRC |
| `CONFIG_RFKILL` | **`m`** | |
| `CONFIG_WLAN` | **`y`** | MTK 全栈 WiFi 驱动 |
| `CONFIG_CFG80211` | 未设 | |
| `CONFIG_ANDROID_KABI_RESERVE` | `y` | kABI 填充槽可用 |
| `CONFIG_FTRACE_SYSCALLS` | **不存在** | ★ 编 KernelSU 前必须补 `=y` |
| `CONFIG_SYSVIPC` | 未设 | 若开启需打 kABI 补丁（`sysvsem`/`sysvshm` 会移位 `task_struct`） |
| `CONFIG_KSU` / `CONFIG_KSU_SUSFS` | **完全不存在** | 原厂为干净 GKI 内核 |

完整 defconfig：`ref/tb330fu-6.6.118_defconfig`（7,752 行，已去 CRLF）

## 4. 分区表（`/dev/block/by-name/` 实测）

**A 槽（作业区）**

| 分区 | 设备节点 | 尺寸 | 当前镜像 |
|---|---|---|---|
| `boot_a` | `/dev/block/mmcblk0p28` | 33,554,432 (32 MiB) | SukiSU 已修补 |
| `vendor_boot_a` | `/dev/block/mmcblk0p29` | 67,108,864 (64 MiB) | SukiSU 已修补 |
| `init_boot_a` | `/dev/block/mmcblk0p30` | 8,388,608 (8 MiB) | 原厂 |
| `dtbo_a` | `/dev/block/mmcblk0p31` | 8,388,608 (8 MiB) | 原厂 |
| `lk_a` | `/dev/block/mmcblk0p27` | 2,097,152 (2 MiB) | 原厂（lkpatcher 状态待验） |
| `vbmeta_a` | `/dev/block/mmcblk0p8` | 8,388,608 (8 MiB) | flags 已被改写 |
| `vbmeta_system_a` | `/dev/block/mmcblk0p9` | 8,388,608 | 原厂 |
| `vbmeta_vendor_a` | `/dev/block/mmcblk0p10` | 8,388,608 | 原厂 |

**B 槽（只备份、不刷写）**：`boot_b`(p46) / `vendor_boot_b`(p47) / `init_boot_b`(p48) / `dtbo_b`(p49) / `lk_b`(p45) / `vbmeta_b`(p11) / `vbmeta_system_b`(p12) / `vbmeta_vendor_b`(p13)

**关键非槽位分区**

| 分区 | 节点 | 尺寸 | 备注 |
|---|---|---|---|
| `seccfg` | p21 | 8 MiB | 解锁状态，`mtk da seccfg unlock` 作用点 |
| `proinfo` | p34 | 3 MiB | IDME 母板信息 |
| `metadata` | p15 | 32 MiB | |
| `preloader_a` | `mmcblk0boot0` | — | ★ **eMMC boot 区，刷坏=变砖，永不刷** |
| `preloader_b` | `mmcblk0boot1` | — | ★ 同上 |
| `preloader_raw_a/b` | `/dev/block/mapper/pl_a` / `pl_b` | — | 逻辑映射 |
| `super` | p51 | — | 动态分区宿主 |
| `expdb` | p6 | — | MTK 异常/日志分区 |
| `protect1/2`, `nvdata`, `nvcfg`, `nvram`, `persist`, `frp`, `otp`, `para`, `misc`, `logo`, `flashinfo`, `lenovocust`, `lenovoraw`, `lenovolock`, `lenovosku_1/2`, `boot_para`, `md_udc`, `sec1`, `tee_a/b`, `gz_a/b`, `scp_a/b`, `sspm_a/b`, `spmfw_a/b`, `md1img_a/b` | — | — | MTK/联想平台分区 |

> ⚠️ **`preloader` 映射到 eMMC boot 区（`mmcblk0boot0/boot1`）**，比普通分区更危险：写坏后 fastboot/BROM 都可能救不回。SP Flash Tool 全量刷机时**必须取消勾选 preloader**。

## 5. boot / vendor_boot 镜像实测结构

### 5.1 `boot_a`（原厂 `boot_a.bin`，32 MiB）

| 字段 | 值 |
|---|---|
| magic / header_version | `ANDROID!` / **4** |
| header_size / page_size | 1584 / **4096** |
| kernel_size | 14,623,212（**gzip**） |
| **ramdisk_size** | **0** |
| cmdline | 空 |
| AVB footer | **有**，`original_image_size=14,647,296` |
| footer algorithm | **`SHA256_RSA4096`**（真签名） |
| footer flags | 0 |
| digest_match | **True** |

### 5.2 `vendor_boot_a`（原厂，64 MiB）

| 字段 | 值 |
|---|---|
| magic / header_version / header_size | `VNDRBOOT` / **4** / **2128** |
| page_size | 4096 |
| kernel_addr / ramdisk_addr / tags_addr / dtb_addr | `0x40080000` / `0x47c80000` / `0x4bc80000` / `0x4bc80000` |
| vendor_ramdisk | 27,586,716 B，**lz4_legacy** |
| **dtb 段** | 156,675 B，**magic `0xd7b7ab1e` = Android DT-table**（非裸 FDT） |
| ramdisk_table | 324 B，**entry_num=3**，entry_size=108 |
| bootconfig | 0 字节 |
| AVB footer | `original_image_size=27,758,592`，**`algorithm=NONE`** |

**vendor_ramdisk_table（原厂）**

| idx | name | type | size | offset |
|---|---|---|---|---|
| 0 | `""` | platform(1) | 7,081,946 | 0 |
| 1 | `recovery` | recovery(2) | 17,732,269 | 7,081,946 |
| 2 | **`init_boot`** | platform(1) | 2,772,501 | 24,814,215 |

`board_id`：全部为 0（对所有板型生效）

### 5.3 SukiSU 修补后对比

| | 原厂 | SukiSU 修补后 | 变化 |
|---|---|---|---|
| `boot_a` ramdisk_size | 0 | **554,410** | +554,410（ksuinit + kernelsu.ko + ksu_config） |
| `boot_a` AVB descriptor | digest_match=**True** | digest_match=**False** | ★ footer **未重算**，RSA4096 描述符已失效 |
| vendor_boot fragment #0 | 7,081,946 @ 0 | 7,081,946 @ 0 | 不变 |
| vendor_boot fragment #1 | 17,732,269 @ 7,081,946 | 17,732,269 @ 7,081,946 | 不变 |
| vendor_boot fragment #2 | 2,772,501 @ 24,814,215 | **3,326,204** @ 24,814,215 | +553,703 |
| vendor_boot dtb_offset | 27,594,752 | 28,147,712 | 后移（内容不变） |

**结论**：SukiSU **只改 name==`init_boot` 的 fragment**，并按规则重算 table 与后续偏移；**不重算 AVB footer**（依赖 orange 解锁态跳过校验）。

## 6. 校验状态（`/proc/bootconfig` 实测）

```
androidboot.veritymode          = "eio"
androidboot.veritymode.managed  = "yes"
androidboot.verifiedbootstate   = "orange"
```

- `verifiedbootstate=orange` → BL 已解锁，**不校验签名**，未签名镜像可启动。
- `veritymode=eio` + `managed=yes` → dm-verity **处于“托管”状态**：出错返回 EIO 而非强制重启。
  - ✗ 不等于 `disabled`。
  - 这与「第一屏冻结」（LK 层解析失败）是**两个不同层面的故障**，排障要分开。

## 7. 符号表

- `/proc/kallsyms` 共 **315,347 行**（其中 `t/T` 文本符号 191,495 个）。
- **地址被 `kptr_restrict` 清零为全 0**，但符号名与类型完整可用 → 足够做导出符号 CRC 基线比对。

存档：`ref/kallsyms.txt`

---

## 7bis. ★★★ 颠覆性发现：vermagic 并不校验版本串 ★★★

> 采集：2026-10-30，从 `vendor_boot` 与 `/vendor/lib/modules` 里的 344 个厂商 `.ko` 实测 + AOSP 源码比对。

### 7bis.1 厂商模块的 vermagic 与运行内核**根本不一致**

| | 串 |
|---|---|
| 运行内核 `uname -r` | `6.6.118-android15-8-g`**`f9c84f40b6ba`**`-`**`ab14657223`**`-4k` |
| 全部 344 个厂商 `.ko` | `6.6.118-android15-8-g`**`0393b5aebcd1`**`-4k`（**无 `ab` 段、sha 不同**） |

`vendor_boot` 171 个 + `/vendor/lib/modules` 173 个，**vermagic 100% 都是 `g0393b5aebcd1-4k`**。

但设备能正常开机、WiFi/显示/触控全部正常 → 说明内核**不因为版本串不同而拒绝加载**。

### 7bis.2 源码级原因

`kernel/module/version.c:80`：
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

`kernel/module/main.c:2135` 调用：`same_magic(modmagic, vermagic, info->index.vers)`

因为本机 `CONFIG_MODVERSIONS=y`，模块都有 `__versions` 段 → `has_crcs = true` → **只比较第一个空格之后的部分**：

| | 前半段（被跳过） | 后半段（**真正比较**） |
|---|---|---|
| 模块 | `6.6.118-android15-8-g0393b5aebcd1-4k` | ` SMP preempt mod_unload modversions aarch64` |
| 内核 | `6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k` | ` SMP preempt mod_unload modversions aarch64` |
| | ← 无所谓 | ← **完全相同 → 加载通过** |

### 7bis.3 结论（推翻既有文档与社区说法）

| 原说法 | 实测结论 |
|---|---|
| “vermagic 必须逐字符一致，差一个字符所有厂商模块拒加载” | ❌ **错**。`CONFIG_MODVERSIONS=y` 时版本串被跳过，只比 flags |
| “必须用 `CONFIG_LOCALVERSION` 硬编码 `-android15-8-g<sha>-ab<buildid>-4k`” | ⚠️ **非必需**（但仍建议对齐，便于识别与排障） |
| 真正约束是什么 | ✅ **`__versions` 段里的每个符号 CRC** |

**必须匹配的只有 flags 部分**：`SMP` / `preempt` / `mod_unload` / `modversions` / `aarch64`
—— 对应 `CONFIG_SMP=y`、`CONFIG_PREEMPT=y`(或 PREEMPT_DYNAMIC)、`CONFIG_MODULE_UNLOAD=y`、`CONFIG_MODVERSIONS=y`。

> 这条发现让路线自由度大增：不再需要伪造版本串，**唯一硬指标变成 CRC 表**。

---

## 7ter. 厂商模块的 CRC 需求清单（构建的真正验收标准）

从 344 个厂商 `.ko` 的 `__versions` 段提取（`struct { unsigned long crc; char name[56]; }`，64 字节/项）：

| 项 | 数值 |
|---|---|
| 模块文件数 | 344（vendor_boot 171 + /vendor/lib/modules 173） |
| 去重后需要的符号 | **3958** |
| 多模块间 CRC 冲突 | **0**（说明同一套头文件编译，一致） |
| 存档 | `ref/vendor-crc.json`（symbol → crc32） |

---

## 7quater. M1 基线复现结果（2026-10-30 17:42）

**构建环境**：WSL3 / Arch Linux，16 核 / 23 GB，clang-r510928，kernel/common `android15-6.6.118_r00`（`5650aac9…`）

| 验收项 | 结果 |
|---|---|
| `make kernelrelease` | `6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k` ✅ 逐字符一致 |
| `Image` 产物 | 36,006,400 字节 ✅ |
| 内核 banner | `Linux version 6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k (kleaf@build-host) (Android (11368308, +pgo, +bolt, +lto, +mlgo, based on r510928) clang version 18.0.0 …)` ✅ |
| **CRC 比对** | **2287 个内核导出符号全部一致，不一致 0 个** ✅ |
| 内核未导出但模块需要的 1671 个 | 抽样确认均为**其他厂商 .ko 提供**（`adapter_device_register`←`adapter_class.ko`、`Charger_Detect_Init`←`musb_hdrc.ko`、`DAL_Printf`←`mediatek-drm.ko`）✅ |

**结论：用 AOSP 6.6.118 重编的内核与联想/MTK 厂商模块 kABI 完全兼容。**

### 构建中必须打的两个补丁

| # | 问题 | 修复 |
|---|---|---|
| 1 | `tools/lib/bpf/libbpf.c:11255` — clang r510928 `-Werror` 报 const 丢弃 | `next_path = (char *)strchr(s, ':')`（等价于 Barley-Project 的 `libbpf-const-correctness.patch`） |
| 2 | `No rule to make target 'abi_symbollist.raw'` | `CONFIG_UNUSED_KSYMS_WHITELIST="abi_symbollist.raw"` 是 kleaf 产物；纯 make 需自行生成到 `$O/` 下 |

`abi_symbollist.raw` 的生成方式（**超集策略**，安全）：
```
[abi_symbol_list]
  <符号>
```
= `android/abi_gki_aarch64`（3）∪ `abi_gki_aarch64_mtk`（3816）∪ `abi_gki_aarch64_lenovo`（1359）
∪ 厂商模块需要的全部 3958 个符号 → **4695 个**（`TRIM_UNUSED_KSYMS` 只裁不在表里的，给超集只会多留、不会少留）

> 后续修正：实际最终用了 5798 个（把 `/vendor/lib/modules` 的 173 个模块也纳入后，需要 3958 个符号；
> 早期版本因合并顺序错误只用了 2776 个，导致 34 个符号被 `TRIM` 裁掉）。

---

## 7quinquies. ★★ 实刷结果与 WiFi 故障根因（2026-10-30 18:05）★★

### 实刷成功

`fastboot flash boot_a boot_a.kernelonly.img` → 重启 → **正常开机**。

指纹确认跑的是自编内核：

| 指纹 | 原厂 | 刷后 |
|---|---|---|
| `CONFIG_MODULE_SCMVERSION` | `=y` | **不存在** |
| `CONFIG_LOCALVERSION_AUTO` | `=y` | **`is not set`** |
| 内核构建号 | `#1` | **`#2`** |
| `__ksymtab_` 条目 | 13374 | **8045** |
| `uname -r` | 同 | 同（`-4k` 后缀，因为配置一致） |

- 显示正常（1200×1920 @ 90Hz）
- **Root 保住**（`kernelsu 483328 - Live`，`su -c id` → `context=u:r:ksu:s0`）
- **WiFi / BT 失效**

### WiFi 失效根因（三层，只有第一层是真因）

```
rfkill.ko  ──失败──▶ cfg80211.ko ──失败──▶ wlan_drv_gen4m_6768.ko
```

`dmesg` 关键行：
```
rfkill: exports protected symbol rfkill_alloc
cfg80211: Unknown symbol rfkill_set_hw_state_reason (err -2)
wlan_drv_gen4m_6768: Unknown symbol cfg80211_scan_done (err -2)
```

**根因**：`rfkill.ko` 来自 **`/system_dlkm/lib/modules/`**（GKI 通用模块分区，本机 77 个模块）。
这些模块本应由 Google 签名，内核验签通过后 `mod->sig_ok=true`，才允许**导出** protected 符号
（`kernel/module/main.c:1368`）。

我们自编内核用的是**自建签名密钥**（`CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"`），
验签必然失败 → `sig_ok=false` → 被当作"未签名厂商模块" → 拒绝导出 `rfkill_alloc`。

`tainted = 4609`（bit0 专有 + **bit9 未签名** + bit12 树外）印证了这一点。

**为什么只挂 70 个模块**：`system_dlkm` 共 77 个，其中 70 个因依赖链断裂未加载（542→472）。

**重要**：`version magic` / CRC 类错误 **0 个** —— 说明 §7bis 的 vermagic 分析与 §7ter 的 CRC 表完全正确，
唯一障碍是签名保护。

### 修复

`CONFIG_MODULE_SIG_PROTECT=n`（保留 `CONFIG_MODULE_SIG=y`，仅去掉"未签名模块不得导出/引用 protected 符号"这一层）。

> 无法用 Google 私钥给模块重签，也无法让自编内核信任 Google 公钥，因此只能放宽这一层。
> 该机已 `orange` 解锁且全部厂商模块本就未签名，放宽此层不改变实际安全边界。

### 修复后结果（v2，2026-10-30 18:20）

刷入 `boot_a.v2.img`（`MODULE_SIG_PROTECT=n`）后**完全正常**：

| 检查项 | 结果 |
|---|---|
| 内核 | `6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k #3 SMP PREEMPT`（自编，`#3`） |
| `sys.boot_completed` | `1` |
| 槽位 | `_a` |
| **加载模块数** | **542**（与原厂完全一致；v1 时只有 472） |
| WiFi 链路模块 | `rfkill` / `cfg80211` / `mac80211` / `wlan_drv_gen4m_6768` 全部加载 |
| **WiFi** | **已连接 `FanchmWrt-5G`，`192.168.1.131/24`** |
| 蓝牙 | `enabled: true` / `state: ON`，`bluetooth`/`hci_uart`/`btqca` 已加载 |
| Root | `su -c id` → `uid=0`，`context=u:r:ksu:s0`，`kernelsu` 模块 Live |
| 显示 | 1200×1920，密度 240，刷新率 90 Hz |
| 触摸/传感器 | `hid_multitouch`、`gt1151`、`accelhub`、`gyrohub`、`alspshub` 等全部加载 |
| 电池/充电 | `level: 98`，`present: true`，`status: 2`（充电中） |
| `tainted` | `12801` = bit0 专有 + bit9 WARN + bit12 树外 + bit13 未签名（全部为预期） |
| dmesg 告警 | 仅 2 条 `WARN_ON_ONCE(ts->timer_expires_base)`（`tick-sched.c:1183/1364`），定时器恰在 idle 边界到期的已知良性告警 |

**结论：自编内核在 TB330FU 上达到与原厂完全一致的功能水平，且 Root 保留。**

### 完整修复清单（从 AOSP 源码到可用内核，共 3 处必要改动）

| # | 改动 | 原因 |
|---|---|---|
| 1 | `tools/lib/bpf/libbpf.c:11255` → `next_path = (char *)strchr(s, ':')` | clang r510928 `-Werror` 报 const 丢弃 |
| 2 | 生成 `$O/abi_symbollist.raw`（5798 符号超集） | `CONFIG_UNUSED_KSYMS_WHITELIST` 是 kleaf 产物，纯 make 缺失 |
| 3 | `# CONFIG_MODULE_SIG_PROTECT is not set` | `/system_dlkm` 的 GKI 模块由 Google 签名，自编内核验签失败 → 被当作未签名模块 → 拒绝导出 `rfkill_alloc` 等 protected 符号 → `rfkill.ko` 加载失败 → `cfg80211` → `wlan` 全链断裂 |

---

## 7sexies. ★★★ 内核内置 SukiSU（告别 LKM）—— 含一次惨痛踩坑 ★★★

### 最终结果（2026-10-30 18:42）

内核 `6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k` **内置 SukiSU Ultra**，不再需要 `ksuinit` 与 LKM 注入。

| 验证项 | 结果 |
|---|---|
| `/proc/modules` 中的 `kernelsu` | **0**（内置，非模块） |
| 内核 `ksu_*` 符号 | **353 个**（`kernelsu_init`、`ksu_syscall_hook_manager_init` 在 kallsyms） |
| KSU 启动日志 | `welcome to KernelSU version 41104, package name com.sukisu.ultra` / `dispatcher installed at slot 42` |
| Root | `uid=0`，`context=u:r:ksu:s0` |
| 加载模块数 | **542**（全量） |
| WiFi / 蓝牙 | 已连接 / ON |
| dmesg 错误 | **0** |
| 部署 | `boot_a` = 内置 KSU 内核（原厂底板，`ramdisk_size=0`）+ `vendor_boot_a` = **原厂**（移除 ksuinit） |

### 集成路线

用 SukiSU **`main` 分支**（kprobes + syscall table dispatcher，**内核源码零 hook 调用点**），
不需要 `builtin` 分支的 manual-hook 补丁：

```bash
git clone --depth=1 -b main https://github.com/SukiSU-Ultra/SukiSU-Ultra
sed -i 's/^\tdefault y$/\tdefault n/' SukiSU/kernel/Kconfig   # 只改 config KSU（KSU_MANUAL_SU 用空格缩进，不受影响）
ln -sfn $W/SukiSU/kernel $W/kernel/drivers/kernelsu
echo 'obj-$(CONFIG_KSU) += kernelsu/' >> kernel/drivers/Makefile
sed -i '/^endmenu/i source "drivers/kernelsu/Kconfig"' kernel/drivers/Kconfig
# .config 增加：
CONFIG_KSU=y
```

依赖（本机 defconfig 已全部满足）：`KPROBES=y` `KRETPROBES=y` `KALLSYMS=y` `KALLSYMS_ALL=y` `EXT4_FS=y` `TRACEPOINTS=y` `HAVE_SYSCALL_TRACEPOINTS=y`

### ⚠️ 踩坑记录：`CONFIG_FTRACE_SYSCALLS=y` 会改 CRC，导致内核 panic

**第一次尝试**：按调研报告的建议加了 `CONFIG_FTRACE_SYSCALLS=y`。结果**开机一屏后重启**。

`pstore`/`ramoops` 里抓到了完整 panic：

```
aee_aed: disagrees about version of symbol trace_event_buffer_reserve
aee_aed: disagrees about version of symbol trace_event_buffer_commit
aee_aed: disagrees about version of symbol __trace_trigger_soft_disabled
aee_aed: disagrees about version of symbol bpf_trace_run1
... （共 20 个 trace/BPF 符号）
init: Failed to insmod '/lib/modules/aee_aed.ko' with args '': Invalid argument
init: LoadWithAbbrev was unable to load aee_aed
Kernel panic - not syncing: Attempted to kill init! exitcode=0x00007f00
```

**原因**：`CONFIG_FTRACE_SYSCALLS` 改变 `struct trace_event_call` 相关类型布局 →
`genksyms` 重算 CRC → 20 个符号 CRC 变化 → 厂商 `aee_aed.ko` 拒绝加载 →
`init` 加载必需模块失败 → `init` 退出 → **内核 panic**。

实测 CRC 对比：

| 符号 | 无 FTRACE_SYSCALLS（正确） | 有（错误） | 厂商需要 |
|---|---|---|---|
| `trace_event_buffer_reserve` | `0x7adce031` | `0x23bd241` | `0x7adce031` |
| `trace_event_buffer_commit` | `0x2a1f1757` | `0xc61b13e4` | `0x2a1f1757` |
| `bpf_trace_run1` | `0x10919295` | `0x4fd58dae` | `0x10919295` |
| `module_layout`（对照） | `0x4e276f37` | `0x4e276f37`（不变） | |

**结论**：**SukiSU `main` 分支不需要 `CONFIG_FTRACE_SYSCALLS`**。
`__tracepoint_sys_enter` 在不开启该选项的内核里同样存在（实测 `nm vmlinux` 可见 `D __tracepoint_sys_enter`），
`syscall_hook_manager.c` 的 `#ifdef CONFIG_HAVE_SYSCALL_TRACEPOINTS` 分支能正常编译。

**教训**：调研报告写的"必须开 FTRACE_SYSCALLS"来自 KernelSU 旧版/其它分支，**不适用于 SukiSU main**。
**任何 config 变更都必须重跑 CRC 全量校验**——本次就是靠 `compare-crc.py` 抓出 20 个差异。

### ⚠️ 第二次踩坑：校验脚本自身的 bug 造成假阴性

第一次 KSU 构建后我"验证通过"（0 差异），实际是**假阴性**：

```python
# 原脚本硬编码路径
symvers = W/'out/baseline/Module.symvers'
```
我用 `sed -i 's|/out/baseline/Module.symvers|/out/ksu/Module.symvers|'` 想改路径，
但脚本里的实际文本是 `'out/baseline/Module.symvers'`（**前面是单引号，不是斜杠**），
模式 `.../out/baseline/...` 匹配不到 → **sed 什么也没改** → 仍然校验的是 baseline → 报 0 差异。

**修复**：`scripts/compare-crc.py` 改为**接受路径参数**，禁止硬编码：

```bash
python3 scripts/compare-crc.py out/ksu/Module.symvers ref/vendor-crc.json
```

**教训**：验证脚本必须参数化；"验证通过"要先确认它真的读了目标文件。

---

## 7septies. 阶段 1 成果：一键构建脚本（2026-10-30 19:00）

### `scripts/build-kernel.sh`

把「源码 → config → 编译 → **双硬门禁** → 打包 boot 镜像」固化成一条命令。

```bash
./build-kernel.sh --profile ksu                      # 完整构建
./build-kernel.sh --profile ksu --package-only       # 只重新打包
./build-kernel.sh --profile susfs --jobs 16          # 阶段 2
```

**双硬门禁**（不通过就拒绝产出镜像，退出码 6）：

| 门禁 | 检查 | 挡住的历史故障 |
|---|---|---|
| **CRC** | 厂商模块需要的每个符号 CRC 必须一致 | `FTRACE_SYSCALLS=y` 改 CRC → `aee_aed.ko` 拒载 → init panic |
| **覆盖率** | 本次导出集必须是已验证构建（`ref/reference-exports.txt`，4525 个）的超集 | `abi_symbollist.raw` 格式错 → `TRIM` 裁掉 2824 个导出 → 大量模块 Unknown symbol |

### 踩坑：`abi_symbollist.raw` 的格式有两种，不能混用

| 格式 | 消费者 | 是否适用于本方案 |
|---|---|---|
| **裸符号名**，一行一个，无表头无缩进 | `CONFIG_UNUSED_KSYMS_WHITELIST`（TRIM 裁剪） | ✅ **必须用这个** |
| `[abi_symbol_list]` 表头 + 两空格缩进 | `scripts/gen_gki_modules_headers.sh` | ❌ 本方案已关 `MODULE_SIG_PROTECT`，该消费者不存在 |

用错格式的后果：`TRIM` 认不出任何符号 → 导出从 4525 掉到 1701 → 覆盖率门禁拦下（**这次是门禁救的**）。

### 复现性验证

用脚本从零构建（全新 out 目录，203 秒）：

| 指标 | 已验证构建 `out/ksu` | 脚本复现 `out/ksu-repro` | 结论 |
|---|---|---|---|
| `Module.symvers` | — | — | **逐字节完全相同** |
| 导出符号数 | 4525 | 4525 | 一致 |
| `Image` 大小 | 36,006,400 | 36,006,400 | 一致 |
| `Image` 内容 | — | 仅 `.note.gnu.build-id` 处 1129 字节不同 | 链接器构建指纹，**不影响功能** |
| 实机 | 已验 | **已刷入验证：boot_completed=1 / Root / WiFi / 蓝牙 / 542 模块全正常** | ✅ |

### 设备启动的一个正常现象

解锁 BL 后每次开机都会停在**橙色警告屏**（"Your device is unlocked and can't be trusted…"），
需要按一次电源键才继续。这会让"从重启到 adb 上线"看起来要 80 秒 —— 不是内核慢，
是在等人按键。无人在场时不会自动跳过。

### 关键不变式（写进脚本，勿删）

```bash
CONFIG_LOCALVERSION="-android15-8-gf9c84f40b6ba-ab14657223-4k"   # 仅观感
# CONFIG_LOCALVERSION_AUTO is not set
# CONFIG_MODULE_SIG_PROTECT is not set     # 否则 system_dlkm 的 GKI 模块拒载 → rfkill 崩链 → WiFi 挂
# CONFIG_FTRACE_SYSCALLS is not set        # 否则 trace 符号 CRC 变 → aee_aed.ko 拒载 → init panic
CONFIG_KSU=y                               # profile: ksu
```

---

## 7octies. 阶段 2 成果：SUSFS v2.3.0 内核内置（2026-10-30 19:12）

### 结果

内核内置 SukiSU + SUSFS v2.3.0，**实机验证通过**：

```
susfs:[0][1][susfs_init] susfs is initialized! version: v2.3.0
KernelSU: extension: registered handler for susfs (id=4, caps=0x10, min_api=0)

ksud susfs implementation → builtin          ← 内核内置，非 LKM
ksud susfs version        → v2.3.0
ksud susfs features       → 9 项全部启用
```

| 检查项 | 结果 |
|---|---|
| `boot_completed` | 1 |
| `su -c id` | `uid=0`, `context=u:r:ksu:s0` |
| `/proc/modules` 中 kernelsu | **0**（内置） |
| WiFi / 蓝牙 | 已连接 / enabled |
| 模块数 | **542**（全量） |
| dmesg 错误 | **0** |
| CRC 门禁 | 2321 一致 / 0 不一致 |
| 覆盖率门禁 | 导出 4525 = 参考 4525，无丢失 |

### 关键发现：`builtin` 分支自带 SUSFS 扩展

**不需要**外部 `10_enable_susfs_for_ksu.patch`（那个补丁触碰 31 个 KSU 文件，对当前版本已冲突）。

SukiSU `builtin` 分支自带：
- `kernel/extension/susfs_extension.c` / `.h`
- `kernel/Kconfig` 里 19 处 SUSFS 选项（`main` 分支是 **0**）

所以只需两步：
1. 换到 `builtin` 分支（`drivers/kernelsu` 软链指向它）
2. 打内核侧 `50_add_susfs_in_gki-android15-6.6.patch`

### 关键发现：SUSFS 内核补丁 `--fuzz=3` 可干净落地

- `git apply --check`：**有 2 处冲突**（`fs/exec.c:67`、`fs/proc/task_mmu.c:1040`）
- `patch -p1 --fuzz=3`：**25 个文件全部成功，0 个 `.rej`**

实测插入点数量：
| 文件 | 插入点 |
|---|---|
| `fs/namei.c` | 21 处 |
| `fs/namespace.c` | 17 处 |
| `kernel/sys.c` | 7 处 |
| `fs/Makefile` | `obj-$(CONFIG_KSU_SUSFS) += susfs.o` |

> 之前调研报告把「`fs/namei.c` 17 个 hunk」列为最高风险，实际 25 文件在 fuzz=3 下全部通过。
> **风险被显著高估**——但必须验证 `.rej` 数为 0。

### 启用项（全部实测生效）

```
CONFIG_KSU=y
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_PATH=y / _SUS_MOUNT / _SUS_KSTAT / _SUS_MAP
CONFIG_KSU_SUSFS_SPOOF_UNAME=y / _SPOOF_CMDLINE_OR_BOOTCONFIG
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
```

---

## 7nonies. 阶段 3 成果：Droidspaces 支持（2026-10-30 19:47）

### 结果：官方自检全部通过

`/data/local/Droidspaces/bin/droidspaces check`（应用 v7.1.0，`com.droidspaces.app`）：

```
[MUST HAVE]      全部 ✓  Root / Linux version / PID / Mount / UTS / IPC namespace
                          pivot_root / proc / sys / Seccomp
[RECOMMENDED]    全部 ✓  epoll / signalfd / PTY / devpts / loop / ext4
                          Cgroup v2 / Cgroup namespace / devtmpfs
[OPTIONAL]       9/11 ✓  FUSE / TUN/TAP / OverlayFS / Network ns / Bridge / Veth
                          Macvlan / IPv6 NAT / Memory+Swap limit / CPU accounting
                 ✗ CPU limit      (需要 CONFIG_CFS_BANDWIDTH)
                 ✗ Process limit  (需要 CONFIG_CGROUP_PIDS)

Summary: [✓] All required features found!
```

缺的两项是**刻意关闭**的（见下），只影响 `--cpus` / `--pids-limit` 两个参数，容器本身完全可用。

### 核心：SYSVIPC kABI 补丁

`CONFIG_SYSVIPC` / `CONFIG_IPC_NS` / `CONFIG_POSIX_MQUEUE` 会给 `task_struct` 加字段 → 后续所有字段偏移移动 → 导出符号 CRC 变 → 厂商模块拒载 → **开机 bootloop**。

补丁 `001.GKI-below-6.12-fix_sysvipc_kabi_6_7_8.patch` 把这两个字段搬进 `ANDROID_KABI_RESERVE(6~8)`：

```c
#ifdef CONFIG_SYSVIPC
	ANDROID_KABI_USE(6, struct sysv_sem sysvsem);
	_ANDROID_KABI_REPLACE(ANDROID_KABI_RESERVE(7); ANDROID_KABI_RESERVE(8), struct sysv_shm sysvshm);
#else
	ANDROID_KABI_RESERVE(6);
	ANDROID_KABI_RESERVE(7);
	ANDROID_KABI_RESERVE(8);
#endif
```

**为什么这样 CRC 不变**：`_ANDROID_KABI_REPLACE` 在 `__GENKSYMS__` 下展开回原始 reserve，
所以 genksyms 看到的 `task_struct` 与官方完全一致；而真实编译时字段落在预留 padding 里，不移动偏移。

**实测验证**：开启 `SYSVIPC`/`IPC_NS`/`POSIX_MQUEUE`/`USER_NS`/`DEVTMPFS` 后，
**CRC 仍 2321 一致 / 0 不一致**，导出数 4525 → **4531**（多 6 个是 IPC 新代码带来的，无丢失）。

### 槽位选择（重要）

| 补丁变体 | 结果 |
|---|---|
| `..._6_7_8.patch` | ✅ **应用成功**（Hunk2 fuzz=2），本机采用 |
| `..._3_4_5.patch` | ✅ 也能应用 |
| `..._1_2_3.patch` | ❌ Hunk2 FAILED —— 本机 `ANDROID_KABI_RESERVE(1)` 已被 `task_dma_buf_info *dmabuf_info` 占用 |

### 绝对禁止开启的两项

```
# CONFIG_CFS_BANDWIDTH is not set
# CONFIG_CGROUP_PIDS is not set
```

它们改变 scheduler / cgroup 结构体大小，**没有任何 padding 技巧能覆盖**（官方文档称在 5.15 GKI 上会改变约 4000 个导出符号的校验和）。
只有能"从同一源码树重编全部内核模块并同时刷 vendor_boot / vendor_dlkm / system_dlkm"时才可以考虑 —— 本设备有大量无源码的 MTK 预编译模块，**不可能**。

### 启用项（实测生效）

```
CONFIG_SYSVIPC / POSIX_MQUEUE / IPC_NS / PID_NS / DEVTMPFS
CONFIG_USER_NS / MACVLAN / TMPFS_POSIX_ACL / TMPFS_XATTR
CONFIG_NETFILTER_XT_MATCH_ADDRTYPE / _TARGET_REJECT / _TARGET_LOG / _MATCH_RECENT
CONFIG_IP_SET / IP_SET_HASH_IP / IP_SET_HASH_NET / NETFILTER_XT_SET
CONFIG_IP6_NF_NAT / IP6_NF_TARGET_MASQUERADE
```

### 命名空间功能实测（不依赖应用）

```
PID NS : child pid:[4026534062]  ≠  parent pid:[4026531836]   ✓
IPC NS : child ipc:[4026534056]  ≠  parent ipc:[4026531839]   ✓
USER NS / MOUNT NS                                             ✓
SysV IPC: /proc/sys/kernel/sem = 32000 1024000000 500 32000    ✓
POSIX mqueue: /proc/sys/fs/mqueue/msg_max = 10                 ✓
```

### 又一个踩坑：白名单从 ABI 文件推导不确定

`susfs+droidspaces` 首次用脚本构建时，覆盖率门禁拦下 **1 个符号丢失**：`usb_gadget_frame_number`。

原因：从 `android/abi_gki_aarch64*` 推导白名单，**相同输入两次运行分别产出 4106 和 4102 个符号**。
`usb_gadget_frame_number` 和 `sock_common_setsockopt` 明明在 `abi_gki_aarch64_mtk` 里且格式正常，却偶发漏掉。

**修复**：白名单改为**钉死基准** `ref/abi_symbollist.known-good`（取自已验证可启动构建的 5798 个符号），
与 `vendor-crc.json` 求并集。**确定性优先于"从源码推导"** —— 一个能开机一个不能，没有折中余地。

修复后脚本完整复现：

| 指标 | 手工构建 | 脚本构建 | 结论 |
|---|---|---|---|
| `Module.symvers` | — | — | **逐字节完全相同** |
| Image 大小 | 36,215,296 | 36,215,296 | 一致 |
| Image 内容 | — | 1132 字节不同（`.note.gnu.build-id`） | 链接器指纹，不影响功能 |
| CRC 门禁 | 2321/0 | 2321/0 | ✓ |
| 覆盖率门禁 | 4531 导出 | 4531 导出 | ✓ |

### 部署方式

- `boot_a` = 内核（SUSFS + Droidspaces，原厂底板 `ramdisk_size=0`）
- `vendor_boot_a` = **原厂**
- Droidspaces 应用 v7.1.0（`com.droidspaces.app`，官方 release，SHA256 校验通过）
- 应用自动安装 `/data/adb/modules/droidspaces/`（daemon + init 脚本）与 `/data/local/Droidspaces/bin/`（后端 + busybox）

---

## 7decies. Droidspaces 实跑容器验证（2026-10-30 20:10）

### 结果：Alpine 容器完整跑通

```
$ droidspaces show
│ NAME   │ PID  │
│ alpine │ 8139 │

容器内:
  OS      : 3.24.3 (Alpine)
  kernel  : 6.6.118-android15-8-gf9c84f40b6ba-ab14657223-4k
  python  : Python 3.14.10   arch aarch64   hostname alpine   pid 777
  net     : curl https://dl-cdn.alpinelinux.org/alpine/ → HTTP 200
  apk     : apk update → OK: 28570 distinct packages available
            apk add python3 → OK: 56.1 MiB in 43 packages
```

命名空间隔离实测：容器内 `pid:[4026534084]` ≠ 宿主 `pid:[4026531836]`。

### 踩坑：容器 HTTPS 全挂，明文 HTTP 正常

**症状**：容器内 `wget http://...` 正常，`apk update` / `wget https://...` 报
`TLS: unspecified error` 或直接超时。

排除过程：
| 检查 | 结果 |
|---|---|
| MTU（wlan0 / ds-br0 / eth0） | 全 1500，一致 → 不是 MTU 不匹配 |
| `/dev/urandom`、`/dev/random` | 都在 |
| ca-certificates、容器时间 | 都正确（与宿主同一秒） |
| 宿主 `ping 1.1.1.1` | **也失败**（上游拦 ICMP，与容器无关，别被误导） |
| 容器内 DNS 解析 | 成功（说明 NAT 通路是好的） |

**根因**：Droidspaces 只在 `nat/POSTROUTING` 加了 TCPMSS clamp，
但容器→外网的 SYN 包走的是 **`FORWARD` 链**，没被 clamp。
TLS 握手时服务器证书是大包，被丢弃 → 握手卡死；明文 HTTP 响应小，侥幸能过。

**修复**：
```sh
iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN \
  -j TCPMSS --clamp-mss-to-pmtu
```

加上后 **HTTPS 立即恢复**，`apk update` 拿到 28570 个包。

**持久化**：`/data/adb/service.d/99-droidspaces-mss.sh`（KSU 开机脚本），
启动后 20 秒检查并补该规则。内核侧 `CONFIG_NETFILTER_XT_TARGET_TCPMSS=y` 已具备。

### 另一个现象：容器网卡不会自动配 IP

Alpine **minirootfs 没有 openrc**，`/sbin/init` 是 busybox init，不会跑 DHCP 客户端。
所以容器起来后 `eth0` 是 UP 但没有 IP。

手工配置（注意网关必须是 **`172.28.0.1`**，即 `ds-br0` 的实际地址，
Droidspaces 日志里提示的 `172.28.195.1` 并不存在，ARP 会失败）：
```sh
ip addr add 172.28.195.3/16 dev eth0     # 用 /16，与 ds-br0 的 172.28.0.1/16 同网段
ip route add default via 172.28.0.1
```

用完整发行版（含 openrc 的 Alpine ISO / Ubuntu / Debian rootfs）则不需要手工配网。

### 未验证/限制

- `--cpus` / `--pids-limit` 不可用（`CFS_BANDWIDTH` / `CGROUP_PIDS` 破 kABI，无补丁）
- 未测试 `--net=host` / `macvlan` / `gateway` 模式
- 未测试 overlayfs 卷模式（`-V` volatile）与稀疏镜像 rootfs

---

## 7undecies. KPM 支持：已编译进内核，但功能上被 KernelPatch 卡住（2026-10-30 20:30）

### 已完成部分

`CONFIG_KPM=y` 加入内核并成功编译、刷入、启动：

```
CONFIG_KPM=y
-- KPM is enabled                              (编译日志)
vmlinux: ksu_extension_kpm_io / ksu_extension_kpm_get / ksu_extension_kpm_apply_forced
         ksu_extension_kpm_ops / sukisu_kpm_bridge / do_kpm
实测: boot_completed=1 / Root / SUSFS builtin / WiFi / 544 模块  全部正常
双门禁: CRC 2321 一致 0 不一致；导出 4531 无丢失
```

内核侧 KPM 桥接代码确实在了（`#ifdef CONFIG_KPM` 分支已编译——dmesg 未出现
`"extension/kpm: IO disabled: rebuild SukiSU with CONFIG_KPM=y"`）。

### 但 KPM 仍然不可用

```
$ ksud extension list
[NOT_SUPPORTED] kpm (ID=0)
dmesg: KernelSU: extension: registered handler for kpm (id=0, caps=0x0, min_api=0)
                                   注意 caps=0x0（其他扩展都是 caps=0x1）
```

**根因**（`kernel/extension/kpm.c`）：

```c
int ksu_extension_kpm_get(u32 id, u64 *value)
{
#ifdef CONFIG_KPM
    *value = sukisu_kpm_bridge(SUKISU_KPM_PROBE, 0, 0, 0, 0) == SUKISU_KPM_ABI_VERSION;
#else
    *value = 0;
#endif
}
```

而 `sukisu_kpm_bridge()`（`extension/kpm/kpm.c`）的做法是：

```c
fn = (handler_t)find_kernel_symbol_exact("__arm64_sys_truncate");
regs.regs[0] = SUKISU_KPM_SUPERCALL_MAGIC;
if (fn(&regs) != SUKISU_KPM_ABI_VERSION) return -EOPNOTSUPP;
/* Without KP this is a real truncate handler: the magic is a non-userspace
 * address and yields an error, not a function pointer. */
```

它把 `__arm64_sys_truncate` 当成 **KernelPatch 的 supercall 分发器**去调用。
没有 KernelPatch 时，那就是真正的 truncate 系统调用，返回错误而不是 ABI 版本 2。

源码注释直接写明了：`Both built-in and LKM modes use the same KernelPatch kernel-only bridge.`
`bridge.h` 也写 `mirrored in KernelPatch patch/sukisu/supercall.h`。

**结论：SukiSU 的 KPM 只是 KernelPatch 的客户端。`CONFIG_KPM=y` 编译了客户端，
但没有 KernelPatch 就永远探测失败。**

已验证 `__arm64_sys_truncate` 在 vmlinux 中确实存在（`ffffffc0803c933c`），
所以不是"找不到符号"，是"找到了但探测不通过"。

### 尝试用 KernelPatch 的 LKM 形态补齐 —— 卡在 GKI 符号导出

KernelPatch 有 `lkm/` 目录（标准 Kbuild 模块，自带 `kpm/`）：

```
config KP_LKM
	tristate "KernelPatch LKM support"
	depends on MODULES && ARM64
	  Provides the supercall channel on syscall 45 (truncate)
```

用它对着我们的内核树编译，**所有目标文件都编过了**（`LD [M] kernelpatch.o`），
但 modpost 失败，因为 10 个符号内核有实现却**不导出**：

| 符号 | 内核中有 | 已导出 |
|---|---|---|
| `kallsyms_lookup_name` | ✓ | ✗ |
| `__set_fixmap` | ✓ | ✗ |
| `copy_to_kernel_nofault` | ✓ | ✗ |
| `dcache_clean_inval_poc` | ✓ | ✗ |
| `caches_clean_inval_pou` | ✓ | ✗ |
| `filp_open` | ✓ | ✗ |
| `strndup_user` | ✓ | ✗ |
| `prepare_kernel_cred` | ✓ | ✗ |
| `commit_creds` | ✓ | ✗ |
| `prepare_creds` | ✓ | ✗ |

GKI 故意不导出这些（`commit_creds` / `prepare_kernel_cred` 是提权原语）。

### 三条可选路径（尚未执行，待决策）

1. **导出这 10 个符号 + 加载 kpatch.ko**
   - 需改约 7 个内核文件加 `EXPORT_SYMBOL`
   - ⚠️ 严重削弱安全边界：任何模块都能直接拿 root
   - 且 kpatch.ko 的 Kconfig 写明"authenticates by the trusted APatch manager uid"，
     与 KSU 生态未必兼容，仍可能失败

2. **把 KernelPatch 的 `kernel/` 形态（kpimg）链进内核**
   - 它是 freestanding payload（自带链接脚本、`-nostdinc`、交叉 gcc 编译），
     **不是设计成链接进内核的**，改造量大

3. **放弃 KPM**，承认它是 APatch/KernelPatch 生态的功能
   - 对普通 Root / SUSFS / 容器用途没有影响

> ⚠️ **本节结论已被下一节推翻。** 上面的三条路径都是在「用 bmax121 上游 KernelPatch」的
> 错误前提下推导的。正确做法见 **§7terdecies**。

---

## 7terdecies. ★★★ KPM 彻底打通：kpimg 注入（2026-10-30 21:17）★★★

### 最终结果：KPM 可用，且真实加载了模块

```
$ ksud extension list
[AVAILABLE] kpm (ID=0)                           ← 原为 NOT_SUPPORTED
$ ksud extension check kpm
supported

$ ksud kpm load /data/local/tmp/hello.kpm
$ ksud kpm list
kpm-hello-demo

dmesg:
[    0.000000] KP KernelPatch Version: e00
[    0.017725] KP sukisu_kpm_supercall_install done: 0
[  114.655247] [+] KP D load_module_path_event: /data/local/tmp/hello.kpm
[  114.655315] [+] KP D     name: kpm-hello-demo
[  114.655398] kpm hello init, event: load-file, args: 
[  114.655404] [+] KP I load_module_ex: [kpm-hello-demo] succeed with []
```

`ksud kpm unload kpm-hello-demo` 也能干净卸载。KPM loader 版本 3584 (=0xE00)。

### 之前三条路全错在哪

| 之前的判断 | 实际 |
|---|---|
| 方案1：导出 10 个符号 + 载入 kpatch.ko | **完全走错**。KernelPatch 根本不需要导出符号——它靠 kallsyms 运行时自己解析 |
| 用 bmax121 上游 KernelPatch | 上游**没有** SukiSU 的 supercall ABI（无 `patch/sukisu/`） |
| 方案2：把 kernel/ 形态链进内核 | 它是裸机 payload，设计上就不是链进内核的，而是注入 |

### 正确路线

**用 `SukiSU-Ultra/SukiSU_KernelPatch_patch`**（bmax121 的裁剪 fork，专供 SukiSU KPM）：

- `kernel/patch/sukisu/supercall.h` —— ABI 与 SukiSU `bridge.h` **逐位相同**
  （`SUKISU_KPM_SUPERCALL_MAGIC 0x53554b504d000002`、`ABI_VERSION 2`）
- `kernel/patch/sukisu/sukisu.c` —— `SUKISU_KPM_PROBE` 直接 `return SUKISU_KPM_ABI_VERSION`
- `kernel/patch/patch.c:97` —— `sukisu_kpm_supercall_install()`
- 注释明确：`The magic is a PUBLIC protocol tag, not a secret or authentication token.`

流程：
```
1. 编 kpimg（ARM 裸机工具链 aarch64-none-elf-）
2. kptools -p -i Image -k kpimg -s <superkey> -o Image.kp
3. 照常打包刷入
→ KP 在内核极早期运行 → 安装 syscall 45(truncate) 的 supercall 分发器
→ SukiSU 的 kpm bridge PROBE 拿到 ABI v2 → KPM 可用
```

### 三个构建坑（按出现顺序）

**坑一：`.setup.map` 超 4KB 导致链接失败**

```
ld: Size too large of .setup.map!     (ASSERT(SIZEOF(.setup.map) < 0x1000))
```

实测 `.setup.map = 0x12a0`（4768 字节），其中 `map.o(.text)` 就占 0x11e8。

- 用 clang 包装工具链编译 → 失败
- 用 GCC 13.3.rel1 → 失败
- 用 GCC 12.2.rel1（CI 用的版本）→ **仍然失败**

**真因：`ANDROID=1` 会定义 `-DANDROID`，使 `map.c` 生成更大代码。**
不带 `ANDROID` 时 `map.o(.text) = 0xe58`（3672 字节），顺利通过。

我们不需要 KernelPatch 的 `su` 命令（SukiSU 自己提供 root），只需要 KPM supercall 通道，
所以不带 `ANDROID=1` 完全够用。**编出来的 kpimg 里确实有
`sukisu_kpm_supercall_install done: %d` 字符串。**

**坑二：`make hdr kpimg` 的 `hdr` 目标不存在**

那个 CI workflow 是上游遗留的，这个 fork 的 Makefile 只有 `all` 和 `clean`。

**坑三：kptools 需要静态 zlib，Arch 不提供 `libz.a`**

```
/usr/bin/ld: cannot find -lz (have you installed the static version of the z library?)
```

kptools 只在本机跑，`make LDFLAGS="-lz"` 去掉 `-static` 即可。

### 第四个坑：注入对了，但镜像里没开 CONFIG_KPM

第一次注入成功后仍然 `NOT_SUPPORTED`，dmesg 报：

```
KernelSU: extension/kpm: IO disabled: rebuild SukiSU with CONFIG_KPM=y
```

这行来自 `extension/kpm.c` 的 `#else` 分支 —— 说明 `CONFIG_KPM` 没定义。
查 `.config` 是 `# CONFIG_KPM is not set`：我当时注入的是为测 avc_spoof 构建的
`out/susfs-ds-avc`，**那个构建没加 `--kpm`**。

教训：**注入的镜像必须来自同一个带 `CONFIG_KPM` 的构建**。

### 与 avc_spoof / SUSFS 共存 —— 无冲突

最终镜像同时包含：SUSFS v2.3.0 + avc_spoof + Droidspaces + CONFIG_KPM + kpimg。

| 检查 | 结果 |
|---|---|
| boot_completed | 1 |
| Root | `uid=0`, `context=u:r:ksu:s0` |
| kernelsu 模块数 | 0（内置） |
| SUSFS | builtin |
| avc_spoof | enabled（`slow_avc_audit spoofing enabled`） |
| WiFi / 模块数 | 已连接 / 544 |
| dmesg 严重错误 | 0 |
| CRC 门禁 | 2321 一致 / 0 不一致 |
| 覆盖率门禁 | 导出 4531，无丢失 |
| Image | 36,280,832 → 注入 kpimg 后 36,576,880（+296 KB） |

### 已固化进构建脚本

`build-kernel.sh --kpm` 现在会自动：
1. 检查/编译 kpimg（裸机工具链，不带 ANDROID）
2. 检查/编译 kptools（`LDFLAGS=-lz`）
3. 生成并复用 `ref/kp-superkey.txt`
4. 用 kptools 注入 `Image` → `Image.kp`
5. `package_boot` 自动优先使用 `Image.kp`

工具链要求（脚本会检测并给出下载地址）：
`tools/arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf/`

---

## 7duodecies. AVC 日志伪装：从 builtin 分支缺失到成功移植（2026-10-30 20:40）

### 现象

管理器里「AVC 日志伪装」显示**内核不支持此特性**。

```
$ ksud feature list
[NOT_SUPPORTED] avc_spoof (ID=10003)
    AVC Spoof - fix SELinux context leaks in AVC denial logs
```

### 排查：两个同名特性

设备上其实有**两个** AVC 相关功能：

| 功能 | 命令 | 状态 |
|---|---|---|
| SUSFS 的 AVC 日志伪装 | `ksud susfs enable-avc-log-spoofing 1` | ✅ **本来就能用**（`ret: 0`，dmesg 有 `enabling susfs_avc_log_spoofing`） |
| KernelSU 的 `avc_spoof`（ID=10003） | 管理器开关 | ❌ NOT_SUPPORTED |

管理器那个指的是 KernelSU 特性，不是 SUSFS 的。

### 根因：builtin 分支没有 avc_spoof

```
SukiSU main   (a9d66f8f): feature/avc_spoof.c  ✓ 存在
SukiSU builtin(b16e899a): feature/ 下没有 avc_spoof  ✗
```

为了 SUSFS 我们只能停在 `builtin` 分支，而它比 `main` 少了一批特性
（同样缺的还有 `uts_spoof`、`module_blacklist`）。

验证：`out/ksu`（main 构建）的 vmlinux 有 37 处 `avc_spoof`；`out/susfs-ds-kpm`（builtin）是 **0 处**。

### 移植方案（已成功）

`main` 的 `avc_spoof.c` 只有 216 行，依赖恰好 builtin 都有：

| 依赖 | builtin 状态 |
|---|---|
| `ksu_feature_handler` 结构体 / `ksu_register_feature_handler()` | **与 main 完全一致** ✓ |
| `security_secctx_to_secid()` | `<linux/security.h>` ✓ |
| `register_kprobe()` | `CONFIG_KPROBES=y` ✓ |
| `pr_info/pr_err` | `include/klog.h` ✓ |
| `PT_REGS_PARM2` | `include/arch.h` ✓ |
| `ksu_boot_completed` | `runtime/ksud.h` ✓ |

改动 6 个文件（已固化为 `patches/0001-sukisu-builtin-add-avc-spoof.patch`，10.5 KB）：
复制 `feature/avc_spoof.{c,h}`、加 `KSU_FEATURE_AVC_SPOOF = 10003` 枚举、
加 `feature_names[]` 表项、`ksu_main.c` 加 include 与 `ksu_avc_spoof_init()`、
`runtime/ksud.c` 的 `on_boot_completed()` 里加 `ksu_avc_spoof_late_init()`。

### 移植中遇到的两个 builtin 特有坑

**坑一：`runtime/ksud_boot.h` 不存在**
main 用它，builtin 只有 `runtime/ksud.h`。实际代码里 `boot_completed` 是文件内静态变量，
那个 include 是多余的 → 换成 `runtime/ksud.h`。

**坑二：unity build 下的全局符号冲突（这个很隐蔽）**

builtin 是 **unity build**：`ksu_main.c` 直接 `#include` 了 40 个 `.c` 文件，只编译 `ksu_main.o`。

于是 `policy/feature.c` 和 `extension/extension.c` 里**同名的 `forced_mask` / `forced_pending`
落到了同一个编译单元**。原本两者都是 `unsigned long[1]`，作为 C 的**暂定定义**被静默合并
（这本身就是潜伏 bug）；我把 `KSU_FEATURE_MAX` 从 17 改成 10004 后变成 `unsigned long[157]`，直接冲突：

```
error: redefinition of 'forced_mask' with a different type:
       'unsigned long[157]' vs 'unsigned long[1]'
```

**修复**：把 `policy/feature.c` 里的改名为 `feature_forced_mask` / `feature_forced_pending`。

**代价**：`KSU_FEATURE_MAX = 10004` 让 `feature_handlers[]` / `feature_names[]` /
`forced_values[]` 各占约 80 KB BSS（合计约 240 KB）。上游 main 分支也是这么做的，
对 36 MB 的内核可接受，但不是好设计。

### 结果

```
$ ksud feature list
[ENABLED (1)] avc_spoof (ID=10003)          ← 从 NOT_SUPPORTED 变为 ENABLED

dmesg:
  KernelSU: feature: registered handler for avc_spoof (id=10003)
  KernelSU: avc_spoof: su SID: 2090
  KernelSU: avc_spoof: ksu SID: 85
  KernelSU: avc_spoof: priv_app SID: 87
  KernelSU: avc_spoof: register_slow_avc_audit kprobe: 0
  KernelSU: avc_spoof: slow_avc_audit spoofing enabled
```

| 检查 | 结果 |
|---|---|
| boot_completed | 1 |
| Root | `uid=0`，`context=u:r:ksu:s0` |
| kernelsu 模块数 | 0（内置） |
| SUSFS | builtin |
| WiFi / 模块数 | 已连接 / 544 |
| dmesg 严重错误 | 0 |
| CRC 门禁 | 2321 一致 / 0 不一致 |
| 覆盖率门禁 | 导出 4531，无丢失 |
| Image | 36,280,832 字节（比无 avc_spoof 版多约 65 KB） |

### 原理

`avc_spoof` 在 `slow_avc_audit` 上挂 kprobe，把 AVC 拒绝日志里渲染用的 tsid
从 `su`/`ksu` 的 SID 换成 `priv_app` 的 SID —— **只改日志渲染，不改 AVC 判定和 SELinux 策略**。
这样 `dmesg` / `logcat` 里看不到 `u:r:su:s0` 或 `u:r:ksu:s0` 泄漏。

补丁已验证可干净应用到原始 builtin 检出（`git apply --check` 通过），
构建脚本会在 `integrate_sukisu builtin` 阶段自动检测并应用（幂等）。

## 8. 与既有文档冲突、已修正的点

| 原说法 | 实测 | 处理 |
|---|---|---|
| "A/B Dual-Partition"（最初基线） | **`ab_update=true`，确实 A/B**，当前 A 槽 | ✅ 基线正确；作业范围限定 A 槽 |
| twrpdtgen fstab：**无 `init_boot`** | **`init_boot_a/b` 均存在，8 MiB** | ❌ 该仓库不可信，已降级 |
| twrpdtgen：`BOARD_BOOTIMG_HEADER_VERSION := 2` | **实际 boot v4 / vendor_boot v4** | ❌ 同上 |
| Barley-Project：boot 带 3 MB 通用 ramdisk | **`ramdisk_size=0`** | ❌ 那是 debug 镜像 |
| lifengqaq：boot.img 无 ramdisk | **确认 `ramdisk_size=0`** | ✅ 正确 |
| 原厂 `CONFIG_LOCALVERSION_AUTO` 说法不一 | **实测 `=y`** | ✅ lifengqaq 正确 |
| "内核用纯 make 编" | **kleaf + PGO/BOLT/LTO/MLGO** | ⚠️ 纯 make 仍可行，但二进制不会一致（不影响 .ko） |
| "verity 被 disable" | **`eio` + `managed=yes`** | ⚠️ 不是 disabled |
