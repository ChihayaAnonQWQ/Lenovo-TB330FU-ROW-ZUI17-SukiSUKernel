# ota-reinject.sh 使用说明

> 目标设备：Lenovo **TB330FU**（MT6768 / Helio G88，Android 15 / ZUI 17.0.502，A/B 双槽 + 动态分区，内核 `6.6.118-android15-8`）
> Root 方案：**SukiSU Ultra，LKM 模式，MTK 双分区注入**（`boot`/`init_boot` + `vendor_boot`）
> 作用：**系统 OTA 之后**，自动完成"提取原厂镜像 → 修补 → fastboot 刷写 → MTKClient 关 AVB → 重启验证 Root"的闭环。

脚本本体：[ota-reinject.sh](ota-reinject.sh)

---

## 0. 一分钟看懂

```bash
# 1) 先只做检测（安全，不写设备）
bash scripts/ota-reinject.sh --dry-run

# 2) 修补但不刷写（确认产物正常）
bash scripts/ota-reinject.sh --skip-flash

# 3) 正式注入（全程会逐步确认；--yes 跳过确认）
bash scripts/ota-reinject.sh

# 4) 出问题回滚（用 backup/ 或 dump/ 里的原厂镜像）
bash scripts/ota-reinject.sh --rollback
```

---

## 1. 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Linux 主机 / WSL（Debian/Ubuntu 均可） | 脚本用 bash 4.4+，会检查版本，不满足直接退出（码 3） |
| 工具 | `adb`、`fastboot`（platform-tools）、`python`/`python3`（3.x） | 缺任何一个在前置检查就失败 |
| mtkclient | `mtk.py`（默认 `~/mtkclient/mtk.py`） | 用于 `da vbmeta 3`；**刷写模式下缺失直接失败** |
| SukiSU | `${SUKISU}/bin/ksud` **或** `${SUKISU}/tools/boot_patch.sh` | 两者都没有 → 失败退出（码 3），不会静默继续 |
| 可选 | `xxd`（缺失时自动用 `od` 兜底）、`sha256sum`/`shasum`/`openssl` | |

### 目录约定

默认工作目录 `/work/tb330fu`（可用 `--workdir` 或环境变量 `WORKDIR_DEFAULT` 覆盖）：

```
/work/tb330fu/
├── dump/     原厂镜像 boot_<slot>.img / init_boot_<slot>.img / vendor_boot_<slot>.img、幂等缓存 .cache-<sha256>
├── patched/  修补产物 patched-<partition>.img
├── logs/     ota-reinject-<时间戳>.log（tee 到 stdout）、manifest.txt（sha256 台账）
└── backup/   <时间戳>/ 下的原厂镜像 / 旧修补产物备份
```

脚本启动时会检查这四个目录**可写**、以及**磁盘余量 ≥ 预计需要**（约 1.1 GiB）。

---

## 2. 参数表

| 参数 | 取值 | 含义 |
| --- | --- | --- |
| `--slot <auto\|a\|b>` | 默认 `auto` | `auto` 依次尝试 ①`ro.boot.slot_suffix` ②`fastboot getvar current-slot` ③`by-name` 里 boot_a/boot_b 的存在性。**`ro.boot.slot_suffix` 为空时直接硬失败（退出码 5），不猜**；也可强制 `--slot a` / `--slot b` |
| `--dry-run` | — | 只做：前置检查 + 槽位检测 + 分区映射 + 提取 + 校验（sha256/magic/大小）。**不修补、不刷写、不重启** |
| `--skip-flash` | — | 提取 + 修补，**不刷写、不动 vbmeta**，产物留在 `patched/`（适合先人工检查，或先做 TWRP 手动测试） |
| `--rollback` | — | 用 `backup/<最近时间戳>/`（其次 `dump/`）里的**原厂镜像**回刷 + `da vbmeta 3` + 重启，走完整确认流程。与 `--dry-run` 互斥 |
| `--yes` | — | 非交互。**所有**确认点自动 yes（危险，建议先 `--dry-run` + `--skip-flash` 跑通） |
| `--keep` | — | 保留中间产物（例如 `boot_patch.sh` 的临时工作目录 `.work-*`）。`dump/ patched/ backup/` 无论是否 `--keep` 都不会删 |
| `--workdir <dir>` | 默认 `/work/tb330fu` | 工作目录（dump/patched/logs/backup 的父目录） |
| `--mtk-py <path>` | 默认 `$MTKCLIENT_TOOL/mtk.py`（`~/mtkclient/mtk.py`） | mtkclient 的 `mtk.py` 路径 |
| `--sukisu <dir>` | 默认 `/work/sukisu` | SukiSU Ultra 仓库根目录（里面应有 `bin/ksud` 或 `tools/boot_patch.sh`） |
| `--backend <auto\|ksud\|boot_patch\|custom>` | 默认 `auto` | 修补后端：`auto` 优先 `ksud`；`boot_patch` 用 Magisk 风格共享脚本；`custom` 用环境变量 `PATCH_CMD_TEMPLATE` |
| `--partitions <list>` | 如 `boot,vendor_boot` / `init_boot,vendor_boot` | 强制注入目标。**不指定时自动探测**：`by-name` 里有 `init_boot_<slot>` 就注入 init_boot，否则 boot |
| `--unlock` | — | **默认关闭**。显式传入后，流程中会额外询问是否执行 `python mtk.py da seccfg unlock`（永久危险，不可逆）。与 `--dry-run/--skip-flash` 互斥 |
| `--no-mtk` | — | 调试用：跳过 mtkclient 步骤（即**跳过关 AVB**）。真实刷写后跳过必定静默回滚，**不要用于实战** |
| `-h/--help`、`-V/--version` | — | 帮助 / 版本 |

环境变量（也可代替参数）：`WORKDIR_DEFAULT`、`SUKISU`、`MTKCLIENT_TOOL`、`PATCH_BACKEND`、`PATCH_CMD_TEMPLATE`，以及超时项 `FASTBOOT_TIMEOUT`(180) `ADB_BOOT_TIMEOUT`(180) `ADB_ROOT_TIMEOUT`(20) `ADB_QUERY_TIMEOUT`(15) `PULL_TIMEOUT`(600) `PATCH_TIMEOUT`(900) `MTK_TIMEOUT`(600)。

### 修补后端：`patch_image()` 可插拔函数

脚本顶部集中配置，函数内对两种入口都留了调用位置：

```
PATCH_BACKEND = auto | ksud | boot_patch | custom
```

| 后端 | 实际命令 | 说明 |
| --- | --- | --- |
| `ksud` | `ksud boot-patch --lkm --image <in> --out <out>`（vendor_boot 追加 `--vendor-boot`） | SukiSU 官方 CLI 形态。**不同版本参数可能不同**，见 §8 假设 3 |
| `boot_patch` | `bash ${SUKISU}/tools/boot_patch.sh <staged.img>`（在临时目录里跑），产物取 `new-boot.img / new_image / patched.img / *.img` | Magisk 风格共享脚本 |
| `custom` | `PATCH_CMD_TEMPLATE`，占位符 `{in}` `{out}` `{part}` | 例：`PATCH_CMD_TEMPLATE='magiskboot --boot-patch {in} -o {out}'` |

修补后**强制校验**：产物非空、大小变化 ≤ ±5%、`boot/init_boot` 仍是 `ANDROID!`、`vendor_boot` 仍是 `VNDRBOOT`；任何一项不过 → 退出码 8，绝不刷写。

**幂等**：同一 slot 的同一份原厂镜像（sha256 相同）只会修补一次，标记写在 `dump/.cache-<sha256>`（含输入/输出路径与 sha256）。再次运行直接命中缓存；如果产物被改动过则自动重修补。已存在的产物会先移到 `backup/<时间戳>/patched/` 再覆盖。

---

## 3. 流程与每步的失败保护

```
preflight        工具/目录/磁盘/设备在线        → 失败 3 或 4
  ↓
slot-detect      三段式槽位检测（slot_suffix 空 = 硬失败）→ 失败 5
  ↓
partition-map    by-name 里有无 init_boot_<slot> → 决定 boot vs init_boot → 失败 6
  ↓
extract          adb root → exec-out dd 流式提取（只读）
                 校验：大小区间 / ANDROID! / VNDRBOOT / ramdisk_size≠0 / sha256→manifest  → 失败 6 或 7
  ↓  （--dry-run 在此结束）
patch            patch_image() × N，产物二次校验，缓存幂等，旧产物备份 → 失败 8
  ↓  （--skip-flash 在此结束）
flash            刷前 getvar current-slot 必须等于检测结果；每一步超时；preloader 守卫 → 失败 9
  ↓
avb              python mtk.py da vbmeta 3；失败 = 打印手工指令 + 退出 10，**绝不重启**
  ↓
verify           fastboot reboot → 等 adb(180s) → sys.boot_completed=1 → su -c id(含一次 15s 重试) → 失败 11/12 + 回滚指引
```

要点：

* **提取**：优先 `adb root`；`adb root` 不可用时会检测 `su -c id`，**但不会静默降级**——因为 `adb exec-out` 在非 root shell 下读不了块设备，脚本会明确失败并提示改用原厂包镜像。`dump/` 里若有手工放入的原厂镜像，同样可用于 `--skip-flash` / `--rollback`。
* **`init_boot` 与 `boot` 的取舍**：Android 13+ / GKI 真机上 `boot` 往往 `ramdisk_size=0`。脚本会读 boot 头偏移 16 的 `ramdisk_size`；若为 0 且目标是 boot → **直接失败**（退出码 7），而不是刷一个无效镜像；此时应存在 `init_boot_<slot>`，用 `--partitions init_boot,vendor_boot` 重跑。
* **vendor_boot magic**：用 `xxd -p -l 8`（或 `od --skip-bytes=0 --read-bytes=8 --endian=big -c`）读出 `VNDRBOOT`，不匹配即失败。
* **刷写前的 preloader 守卫**：`guard_no_preloader()` 对分区名与镜像文件名做小写匹配，命中 `preloader` 立即打印红框并退出（码 9）。**脚本内不存在任何 preloader 刷写分支**：`grep -n preloader` 只出现在注释/守卫/提示里。
* **AVB 失败不重启**：这是"静默回滚"的主要来源，因此 `disable_avb()` 失败 → 打印手工 BROM 步骤 → 退出码 10，绝不继续。
* **回滚**：`--rollback` 先在 `backup/<最近时间戳>/`、再在 `dump/` 里找**通过 magic 校验**的原厂镜像，预览 sha256 → 确认 → 进 fastboot → 刷写 → `da vbmeta 3` → 重启。找不到原厂镜像会直接失败（码 13）。

---

## 4. 退出码表

| 码 | 含义 | 典型原因 / 处置 |
| --- | --- | --- |
| 0 | 成功 | dry-run/skip-flash 完成，或注入并验证 Root 通过 |
| 2 | 参数错误 | 未知参数、`--slot` 取值非法、`--dry-run` 与 `--rollback` 同用 |
| 3 | 环境缺失 | 缺 adb/fastboot/python、bash<4.4、目录不可写、磁盘不足、**mtk.py 找不到（刷写模式）**、**SukiSU 两个入口都没有**、`seccfg unlock` 失败 |
| 4 | 设备未就绪 | 无设备 / adb unauthorized |
| 5 | 槽位检测失败 | `ro.boot.slot_suffix` 为空（硬失败）、getvar 与 by-name 都无法判定 |
| 6 | 提取失败 | adb root 与 su 都不可用；dd 返回非零；空文件；`--partitions` 指定了不存在的分区 |
| 7 | 校验失败 | 大小超区间、magic 不对、`ramdisk_size=0`（GKI boot） |
| 8 | 修补失败 | 后端未找到/执行失败、产物为空、大小变化 >±5%、magic 被破坏 |
| 9 | 刷写失败 | 设备不在 fastboot、`current-slot` 与目标不一致、flash 超时/失败、preloader 守卫触发、用户拒绝 |
| 10 | AVB 关闭失败 | `mtk.py da vbmeta 3` 非零或输出无成功标志 → 按屏幕提示手工 BROM 处理 |
| 11 | 开机验证失败 | 180s 内 adb 未上线 或 `sys.boot_completed≠1` |
| 12 | Root 验证失败 | `su -c id` 无 uid=0（注入目标错 / SukiSU 未授权 / AVB 回滚） |
| 13 | 回滚失败 | 找不到原厂镜像、回滚刷写失败 |
| 130 | 被 Ctrl-C/TERM 中断 | 已执行步骤会打印在日志里 |

失败时 `trap ERR` 会打印：**当前阶段 + 失败行号 + 命令 + 返回码 + 已执行步骤清单 + 回滚指引**，并写入 `logs/ota-reinject-<ts>.log`。

---

## 5. 日志

* 文件：`logs/ota-reinject-<YYYYmmdd-HHMMSS>.log`，同时 `tee` 到 stdout（子进程输出也会进日志）。
* 台账：`logs/manifest.txt` 追加记录每次提取/修补/刷写的 partition、字节数、sha256、输入 sha256、尺寸变化、kernel、build；可直接 diff "OTA 前后到底刷了什么"。
* 非零退出额外追加一行 `[EXIT] 非零退出 rc=… stage=…`。

---

## 6. 示例调用

```bash
# A. 首次上手：只检测（不写设备）
bash scripts/ota-reinject.sh --dry-run --sukisu /work/sukisu --mtk-py /work/mtkclient/mtk.py

# B. 非标准路径 + 指定槽位 + 只修补
bash scripts/ota-reinject.sh --skip-flash --slot b \
     --workdir /work/tb330fu --sukisu /work/sukisu --mtk-py /work/mtkclient/mtk.py

# C. 正式注入（逐步确认，不加 --yes）
bash scripts/ota-reinject.sh

# D. 无人值守（CI/docker 里预演）
bash scripts/ota-reinject.sh --yes --skip-flash

# E. boot 无 ramdisk 的特殊情况：强制注入 init_boot
bash scripts/ota-reinject.sh --partitions init_boot,vendor_boot

# F. 回滚到原厂并重开 AVB
bash scripts/ota-reinject.sh --rollback

# G. 自定义修补后端（示例：自带命令）
PATCH_CMD_TEMPLATE='python3 /work/suki-patch.py --image {in} --out {out} --part {part}' \
  bash scripts/ota-reinject.sh --backend custom --skip-flash
```

---

## 7. 常见错误排查

| 现象 | 判断 | 处理 |
| --- | --- | --- |
| `退出码 3：找不到 mtkclient 的 mtk.py` | 路径不对 | `--mtk-py /abs/path/mtk.py` 或 `export MTKCLIENT_TOOL=/abs/path`；先 `python mtk.py --help` 验证能跑 |
| `退出码 3：修补后端不可用` | 缺 ksud / boot_patch.sh | 确认 `--sukisu` 指向仓库根（不是 `bin/`），仓库里有 `bin/ksud` 或 `tools/boot_patch.sh` |
| `退出码 5：ro.boot.slot_suffix 为空` | 设备/内核没暴露槽位 | 手动 `--slot a` 或 `--slot b`；可用 `adb shell getprop ro.boot.slot` 旁证后再指定 |
| `退出码 6：既无法 adb root，也无法 su -c` | user 版本 + 无 root | 从原厂全量包里解出 `boot.img / init_boot.img / vendor_boot.img` 放进 `dump/`，然后 `--skip-flash`（或此前提取过的直接用 `--rollback`） |
| `退出码 7：ramdisk_size=0` | GKI boot 没有 ramdisk | `--partitions init_boot,vendor_boot` 重跑；若设备确实没有 init_boot，说明平台不是 GKI-ramdisk 布局，需要人工核对分区表 |
| `退出码 7：头部不是 ANDROID!` | 分区名/槽位错 | 打印 `adb shell ls -l /dev/block/by-name/` 核对，必要时 `--slot` 明确指定 |
| `退出码 8：大小变化 >±5%` | 修补工具输出异常（比如把整个镜像重新打包） | 用 `--backend boot_patch` 或 `custom` 换后端；产物留在 `*.tmp` 供检查 |
| `退出码 9：槽位不一致` | 用户指定/检测为 a，fastboot 说 b | 说明设备被切了槽（OTA 刚切），重新检测（不带 `--slot`）或手动对齐 |
| `退出码 9：设备不在 fastboot` | 还停在系统里 | `adb reboot bootloader` 后重跑（脚本从提取前就可以在 fastboot 下运行，但提取需要 adb） |
| `退出码 10：AVB 关闭失败` | BROM 未进入 / mtkclient 报 seccfg | 按屏幕手工指令：断电 → 按住音量+/- 插 USB → `cd <mtkclient目录> && python mtk.py da vbmeta 3`；报 seccfg/hw crypto 才考虑 `da seccfg unlock`（永久破坏安全，慎用） |
| `退出码 11：adb 未上线` | bootloop / 卡 logo | 长按电源 10s 强关，进 fastboot 跑 `--rollback` |
| `退出码 12：Root 未生效` | 注入到了错误分区 / SukiSU 未授权 / AVB 回滚 | 检查 `getprop ro.boot.slot_suffix`、`ls /dev/block/by-name | grep init_boot`；平板上 SukiSU app 里点"允许"；重跑 `--partitions init_boot,vendor_boot` |
| 日志里看到 `tee` 相关 Broken pipe | 正常退出路径上的收尾噪声（bash 5.x 中 `exec > >(tee)` 的已知行为） | 不影响退出码；已提供 `flush_log` 尽量让最后几行落盘 |

---

## 8. 重要假设（请核对后再实战）

1. **`init_boot` 的存在性**：本机 Android 15 + GKI 6.6，按 AOSP 规则应存在 `init_boot` 分区，ramdisk（即 LKM 注入点）在 `init_boot` 而不是 `boot`。脚本不假设，而是先看 `by-name` 里有没有 `init_boot_<slot>`、以及 boot 头的 `ramdisk_size`。**如果 TB330FU 的 ZUI 固件其实没拆 init_boot（`boot` 仍带 ramdisk），脚本会自动落到 boot 分支**。
2. **`vendor_boot` 需要单独注入**：MTK + 动态分区机型上 LKM 通常同时在 `vendor_boot` 的 ramdisk 里放内核模块；本脚本默认 boot 侧 + vendor_boot 双分区（可用 `--partitions` 只做其一）。
3. **SukiSU 修补入口的命令行形态**：`ksud boot-patch --lkm --image <in> --out <out> [--vendor-boot]` 是按 SukiSU Ultra 常见 CLI 写的；**若你的 ksud 版本参数不同**，用 `--backend boot_patch`（跑仓库里的 `tools/boot_patch.sh`）或 `--backend custom` + `PATCH_CMD_TEMPLATE` 覆盖，不需要改脚本。
4. **AVB 关闭方式**：`mtk.py da vbmeta 3` 是 MTK 平台"关闭 vbmeta 校验"的 BROM 命令（`3` 为 vbmeta 分区/校验开关标志，以你本地 mtkclient 版本为准）。脚本以"输出含 success/done/written 且返回码 0"判定成功。
5. **每次 OTA 后**：脚本假设"新系统已就位但 root 丢失"，即 `dump/` 里上次的镜像是旧版本——所以每次运行都会**重新提取**并把旧提取移到 `backup/<ts>/pre-existing/`，并用 sha256 判断是否需要重新修补。
6. **BROM 步骤需要人工介入**：脚本只能提示"请断电按住音量+/- 插 USB"，无法自动让设备进入 BROM。

---

## 9. 本脚本不做的事

**明确不做，代码里也不存在对应分支：**

1. **刷 preloader** —— 绝不刷 preloader。脚本内有 `guard_no_preloader()` 守卫：任何分区名/镜像名含 `preloader` 会立即拒绝并退出（码 9）。
   同时提醒：**严禁在 SP Flash Tool 中勾选 preloader**（会破坏 BROM 关键区域，设备可能无法通过 fastboot 恢复）。
2. **刷全量固件** —— 不调用 `fastboot flashall`、不刷 `super`/`system`/`dtbo`/`vbmeta` image，不跑 `flash-all.sh`。
3. **`da seccfg unlock`** —— 默认**不执行**。只有显式传入 `--unlock` 时才会在流程中额外询问是否执行（该操作会永久清除设备安全状态，不可逆）。不加 `--unlock` 时，脚本对 seccfg 完全只读。
4. **处理 userdata** —— 不格式化、不清除、不备份 userdata，也不刷 `metadata`/`userdata`/`cache`。
5. （附带）不修改 `fstab`、不 patch `vbmeta` 镜像本身、不动分区表（GPT）、不写 `persist`/`nvdata`/`nvram`。

---

## 10. 开发期校验记录

* `bash -n ota-reinject.sh` 通过。
* **`shellcheck` 0.10.0**，`--shell=bash --severity=info`（含 info/style 级别）：**0 条**（`--format=json1` 输出 `{"comments":[]}`）。
  * 开发过程中修掉的问题：SC2034 未用变量、SC2140 引号拼接、SC2155 声明与赋值分离、SC2010 `ls|grep`、SC2015 `a && b || c`、SC2086 未加引号、SC1009/SC1073 heredoc 内嵌 `$(...)` 语法错误。
  * 不适用于本脚本的告警（已人工逐条对照）：SC2016（单引号内 `$` 不展开）—— 用于 `su -c '…'` 的设备端脚本，属预期；SC3041/SC3028/SC3030（POSIX sh 下的 `-E`/`BASH_SOURCE`/数组）—— 本脚本显式要求 bash 4.4+，文件首行有版本硬检查。
* 用 adb/fastboot/mtk.py/ksud 桩做了 11 组冒烟：dry-run、skip-flash、幂等二次运行、无 init_boot 落到 boot、slot_suffix 为空（码 5）、全流程（含 flash → vbmeta → reboot → boot_completed → su）、AVB 失败（码 10，确认未重启）、槽位不一致（码 9，确认未写盘）、`--rollback` 成功、Root 验证失败（码 12）、参数错误（码 2）。
* 文件编码：**UTF-8 无 BOM、LF 换行**（已检查无 CRLF、无 TAB）。
