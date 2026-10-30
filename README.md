# Lenovo TB330FU 定制内核

联想 TB330FU（MT6768 / Helio G88，ZUI 17，Android 15，内核 6.6.118）的自编译内核，
内置 SukiSU Ultra + SUSFS + AVC 伪装 + Droidspaces + KernelPatch/KPM 支持。

> 本项目所有结论均来自**实机验证**，完整实测记录见 [`research/device-facts.md`](research/device-facts.md)。

---

## 特性

| 功能 | 状态 | 说明 |
|---|---|---|
| 内置 SukiSU Ultra | ✅ | `CONFIG_KSU=y`，`/proc/modules` 中无 `kernelsu` |
| SUSFS v2.3.0 | ✅ | SukiSU `builtin` 分支 + 25 文件内核补丁 |
| AVC 日志伪装 | ✅ | 从 `main` 分支移植 `avc_spoof`（`builtin` 原生没有） |
| Droidspaces | ✅ | 官方 `droidspaces check` 全部必需项通过 |
| KernelPatch / KPM | ✅ | kpimg 注入内核，可加载 `.kpm` 模块 |
| WiFi / 蓝牙 | ✅ | 542~544 个厂商模块全部正常加载 |

---

## 为什么需要这些改动

原厂内核是 GKI 架构，**导出符号的 CRC 必须与厂商 `.ko` 的 `__versions` 完全一致**，
否则模块拒绝加载 → `init` 死亡 → 内核 panic。这不是"能不能编译"的问题，
而是"编译出来能不能开机"的问题。

本项目踩过的关键坑（详细分析见 device-facts.md）：

| 坑 | 后果 | 处理 |
|---|---|---|
| 开 `CONFIG_FTRACE_SYSCALLS` | 20 个 trace/BPF 符号 CRC 变化 → `aee_aed.ko` 拒载 → init panic | **必须保持关闭** |
| 开 `CONFIG_MODULE_SIG_PROTECT` | 无法验签 Google 签名的 `system_dlkm` 模块 → `rfkill` 崩链 → WiFi 完全不可用 | **必须保持关闭** |
| `abi_symbollist.raw` 带表头/缩进 | `TRIM_UNUSED_KSYMS` 认不出 → 导出从 4525 掉到 1701 | **必须裸符号名格式** |
| `CONFIG_CFS_BANDWIDTH` / `CONFIG_CGROUP_PIDS` | 改 scheduler/cgroup 结构，无 kABI 补丁可救 | **必须保持关闭** |
| kpimg 带 `ANDROID=1` 编译 | `.setup.map` 超 4KB 硬断言，链接失败 | **不带 ANDROID 编译** |

---

## 构建

### 环境要求

- Linux 或 WSL2（本项目在 Arch Linux WSL 上开发）
- 16 GB+ 内存，约 60 GB 空闲磁盘
- 依赖：`base-devel bc bison flex pahole lz4 cpio zstd git curl python3`

```bash
# Arch
sudo pacman -S --needed base-devel bc bison flex pahole lz4 cpio zstd git curl python3
```

### 工具链

需要两套，脚本会自动检测：

1. **内核工具链**：clang r510928
   ```
   tools/clang-r510928/
   ```
2. **KernelPatch 裸机工具链**（仅 `--kpm` 需要）：
   [arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf](https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/12.2.rel1/binrel/arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf.tar.xz)
   解压到 `tools/`

### 前置数据

`ref/` 目录需要以下文件（仓库已提供）：

| 文件 | 用途 |
|---|---|
| `tb330fu-6.6.118_defconfig` | 设备原厂 defconfig |
| `vendor-crc.json` | 3958 个厂商模块所需符号的 CRC（门禁数据源） |
| `abi_symbollist.known-good` | 已验证可启动构建的导出白名单（5798 个） |
| `reference-exports.txt` | 已验证构建的导出集（覆盖率门禁基准） |

### 一键构建

```bash
# 完整功能（推荐）
./scripts/build-kernel.sh --profile susfs+droidspaces --kpm

# 只内置 Root，不带 SUSFS
./scripts/build-kernel.sh --profile ksu

# 只重新打包（复用已有编译产物）
./scripts/build-kernel.sh --profile susfs+droidspaces --kpm --package-only
```

构建流程：

```
源码 → config → 编译 → [kpimg 注入] → CRC+覆盖率双硬门禁 → 打包 boot 镜像
```

### 双硬门禁

**任何一道不通过都会拒绝产出镜像（退出码 6）。** 这是本项目最重要的设计：

| 门禁 | 检查内容 | 挡住的问题 |
|---|---|---|
| **CRC** | 厂商模块需要的每个符号 CRC 必须与 `vendor-crc.json` 一致 | 开了改结构体的 config → 模块拒载 → panic |
| **覆盖率** | 导出集必须 ⊇ `reference-exports.txt`（4525 个） | 白名单格式错 → TRIM 裁掉导出 → 大量模块 Unknown symbol |

> 光靠 CRC 门禁不够。曾经 `abi_symbollist.raw` 格式写错导致 2824 个导出被裁，
> 但 CRC 检查仍报 PASS —— 因为**它只检查"已导出符号的 CRC"，不检查"该导出的有没有被裁掉"**。
> 覆盖率门禁就是补这个洞。

---

## 刷入

```bash
adb reboot bootloader
fastboot flash boot_a boot_a.kpm.img
fastboot reboot
```

**部署结构**：`boot_a` = 自编译内核（原厂底板，`ramdisk_size=0`），`vendor_boot_a` = **原厂不动**。

> 解锁 BL 后每次开机会停在橙色警告屏，需要按一次电源键继续。这不影响任何功能。

---

## 回滚

所有历史镜像见 [Releases](https://github.com/ChihayaAnonQWQ/Lenovo-TB330FU-ROW-ZUI17-SukiSUKernel/releases)。

| 镜像 | 说明 |
|---|---|
| `boot_a.kpm.img` | 全功能（SUSFS + avc_spoof + Droidspaces + KPM） |
| `boot_a.avcspoof.img` | 无 kpimg（怀疑 KernelPatch 引起问题时用这个） |
| `boot_a.susfs-ds.img` | 仅 SUSFS + Droidspaces |
| `boot_a.ksu2.img` | 仅内置 SukiSU |
| `boot_a.v2.img` | 最早验证可用的版本 |

最保险的恢复方式：`fastboot flash boot_a boot_a.kpm.img`，
或用 MTKClient 刷回原厂 `boot_a.bin` + `vendor_boot_a.bin`。

---

## 已知限制

- `--cpus` / `--pids-limit` 容器参数不可用（需要 `CFS_BANDWIDTH` / `CGROUP_PIDS`，会破 kABI 且无补丁可救）
- KernelPatch **并未**导出 `commit_creds` / `prepare_kernel_cred` 等提权原语 —— 它靠 kallsyms 运行时解析，安全性优于常见的"导出一堆危险符号"方案
- kpimg 会 patch syscall 45 (`truncate`) 的分发器。如果遇到奇怪的 syscall 行为，第一个要怀疑的是它

---

## 目录结构

```
docs/         行动计划
research/     调研报告与完整实测记录
scripts/      构建、打包、校验脚本
patches/      SukiSU 移植补丁
ref/          构建所需的设备数据
```

---

## 致谢

本项目站在这些项目之上：

- [SukiSU Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) — 内核内置 Root
- [SUSFS](https://gitlab.com/simonpunk/susfs4ksu) — 文件系统层隐藏
- [KernelPatch](https://github.com/bmax121/KernelPatch) / [SukiSU_KernelPatch_patch](https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch) — KPM 支持
- [Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) — 容器运行时
- [avc_spoof 原始实现](https://github.com/KOWX712/KernelSU) — AVC 日志伪装

## 许可

内核侧改动与移植代码均为 **GPL-2.0**（见 [LICENSE](LICENSE)）。
