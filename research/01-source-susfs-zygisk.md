# 01 · 源码、SUSFS、Zygisk、LKM 注入：可执行技术事实

> 目标设备：Lenovo TB330FU（Tab M11，代号 **barley**），ZUI 17.0.502 (ROW)，内核 `6.6.118-android15-8`，A/B + 动态分区，已用 SukiSU Ultra **LKM 模式** root（boot_a + vendor_boot_a 双分区注入）。
> 调研时间：2026-10-30。**本机无设备访问**，所有"设备侧取证"步骤均为待你实测的步骤，不是已核实结果。
> 标记约定：**【已核实】** = 我实际抓取到原文/接口返回/源码并核对；**【推断】** = 由已核实事实推导、未见直接文档；**【未找到可靠来源】** = 明确查不到。

---

## 0. 本次最重要的一条修正（请先读）

Lead 转述的一手 README 里写着内核基线是 **6.6.30**（`6.6.30-android15-8-g94c1a24cabd5-ab12293704-4k`）。但该 README 所依据的固件是 **ZUI 16 / ROW074**，与本任务的 **ZUI 17.0.502** 不是同一版本。因此：

**6.6.30 那一套 vermagic 串、commit SHA、defconfig 全部不能直接用于 6.6.118。** 方法论（构造方式、提取方式、AVB 处理）可以复用，具体数值必须在本机重新取证。

---

## 1. MTK 6.6 开源内核源码

### 1.1 MTK 是否公开 MT6768 的 6.6 (android15) 内核源码？

| 结论 | 标记 |
|---|---|
| MTK 公开的"内核源码"只有 `kernel/mediatek`，而它**没有 6.6 分支**：`android.googlesource.com/kernel/mediatek` 的全部 heads 只有 `android-mediatek-mooneye-3.18-*`、`-pike-3.10-*`、`-sprout-3.4/3.10-*`、`android-mtk-3.18`、`main`、`master`（共 13 个 head，0 个含 `6.6`）；CodeLinaro 镜像 `clo/la/kernel/mediatek` 39 branches/34 tags，同样最快只到 4.4/wear。 | 已核实 |
| MTK 的 GPL 源码是**对 OEM 定向释放**、或经 `git01.mediatek.com`（Gerrit，需带客户账号）发布的，没有面向公众的"MT6768 android15 源码包"页面。 | 推断（partner Gerrit 需登录，无法验证其内容） |
| MTK 官方对 MTK Android BSP 的公开说明（BayLibre 维护的 MTK 文档）明确其结构为：`common/`（公共内核）+ `board/`（产品设备树与驱动）+ 外部模块，且**"all the drivers written for a customer must be modules"**——这正是 DLKM 的来源。该文档示例为 5.10/5.15，未覆盖 6.6。 | 已核实 |

证据：
- https://android.googlesource.com/kernel/mediatek/+refs （已核实）
- https://git.codelinaro.org/clo/la/kernel/mediatek （已核实，`/api/v4/.../repository/branches` 返回 39 条）
- https://baylibre.pages.baylibre.com/mediatek/rita/device/mediatek/mtk-android-12/docs/kernel/kernel-src-org.html （已核实）

### 1.2 Lenovo 是否释放了 TB330FU 源码？

**结论：截至 2026-10-29，Lenovo 开放源代码中心（github.com/lenovo/gplcc）上 TB330FU 的内核源码仍未发布。**

- Issue #6「Request to release Linux kernel source code for Lenovo Tab M11 (TB330FU)」**仍是 open**（created 2026-04-23，最后活跃 2026-10-28），4 条评论全是催更，无 Lenovo 答复。证据: https://github.com/lenovo/gplcc/issues/6 （已核实，`issues/6` 返回 state=open / comments=4）
- 对照：该仓库的其它 GPL 请求单 **#1（P11/TB-J606F, 2022-04）、#2（Legion Y70, 2023-04）、#3（TB373FU, 2025-11）、#4（TB321FU 设备树）、#5（TB321FU 6.1.112）、#8（Xiaoxin Pad Pro GT）也全部 open**，只有 #7 被 closed。→ **该渠道长期不响应，"走 GPL 申请拿源码"不可作为排期前提**。（已核实，逐单查询 `api.github.com/repos/lenovo/gplcc/issues/{1..8}`）
- `opensource.lenovo.com.cn` / `opensource.lenovo.com` / `oss.lenovo.com.cn` 从本机**均无法连接（curl 000）**，无法确认其是否收录 TB330FU。— 未找到可靠来源
- 证据: https://github.com/lenovo/gplcc （已核实，仓库存在，20 个 open issue）

### 1.3 GitHub 上真正可用的 TB330FU 6.6 内核仓库（一手，已抓取原文）

三个仓库我都读了 README/脚本原文，并存档在 `research/raw/`。

#### (a) `Barley-Project/tb330fu-ubuntu` —— **最有价值的一个**
https://github.com/Barley-Project/tb330fu-ubuntu

- 明确写：**"booted … on top of the Android 15 kernel of the ROW074 firmware (`6.6.30-android15-8`, GKI)"**；SoC 标注 **"MediaTek MT6768 / Helio G85"**；`kernel-config/tb330fu-sukisu.config` 是"SukiSU-Ultra 作为 drivers/kernelsu 编进内核"的**实验**，结果 **build 通过、CRC 无变化、但刷 boot_a 后平板连不上（未查到原因）**。证据: 同一 README 的 Verification status 段（已核实，原文存档 `raw/Barley-Project_tb330fu-ubuntu__README.md`）
- 内核源码获取方式（**一手命令**）：
  ```bash
  # scripts/kernel/fetch-kernel-source.sh
  git -C "$TB_KSRC" fetch --depth=1 https://android.googlesource.com/kernel/common "$TB_KSRC_COMMIT"
  git -C "$TB_KSRC" checkout -q --detach FETCH_HEAD
  ```
  即：**基线就是 AOSP `kernel/common`（android15-6.6）的一个精确 commit**，不是 MTK BSP。（已核实，原文存档 `raw/Barley-Project_tb330fu-ubuntu__scripts_kernel_fetch-kernel-source.sh`）
- 工具链（**一手**）：`clang-r510928`（`clang version 18.0.0`），来源
  `https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/refs/heads/android15-release/clang-r510928.tar.gz`。（已核实）
- 编译方式（**一手**）：**纯 `make`，不用 bazel/Kleaf**：
  ```bash
  make -C "$TB_KSRC" O="$B" olddefconfig
  make -C "$TB_KSRC" O="$B" -j"$(nproc)" Image modules
  make -C "$TB_KSRC" O="$B" INSTALL_MOD_PATH="$B/modules-staged" INSTALL_MOD_STRIP=1 modules_install
  ```
  （已核实，存档 `raw/Barley-Project_tb330fu-ubuntu__scripts_kernel_build-kernel.sh`）
- 它验证过的关键事实（对我们极重要）：**"every exported symbol keeps its CRC, so the vendor modules still load"** 且 `scripts/kernel/crc-compare.py` 做机器校验。（已核实）
- `kernel-config/tb330fu-sukisu.config` 原文（已核实，全文只有 8 行）：
  > "It has to be built in: it calls kernel-internal functions that are not exported to modules, so **a loadable module cannot be linked (modpost: undefined)**."
  → 见 §2.4，这与"SUSFS 能否做 LKM"是同一个技术障碍的两种表现。

#### (b) `lifengqaq/tb330fu-kernelsu-droidspaces` —— **只到 6.6.30，且它是"换基线"路线**
https://github.com/lifengqaq/tb330fu-kernelsu-droidspaces（默认分支 `main`，README 只有 `README.zh.md`）

一手事实（已核实，存档 `raw/lifengqaq_tb330fu-kernelsu-droidspaces__README.zh.md`）：
- 基线：AOSP `kernel/common` commit **94c1a24cabd5**（Makefile = 6.6.30），`git fetch --depth 1 origin 94c1a24cabd5`；**不用 tag**（"Google GKI 仓库中并不存在 android15-6.6.30 这个 tag……最低 sublevel 为 46"）。
- config：**从当前槽位 boot.img 的 IKCONFIG 段提取的官方出厂 config**，只改 `CONFIG_LOCALVERSION` 与 `CONFIG_LOCALVERSION_AUTO`。
- 明确 **不用 SUSFS**（第 12 节），理由三条：Droidspaces 官方不支持 / 与 GKI kABI 冲突面大 / 与 KernelSU 主线无官方集成。
- 版本串三要素（第 5 节，可复用方法论，数值需换）：
  1. `CONFIG_LOCALVERSION` 直接写完整后缀；
  2. `CONFIG_LOCALVERSION_AUTO=n`；
  3. 构建时 `LOCALVERSION=""`（否则 `scripts/setlocalversion` 在无 tag 树上输出 `+`）。
- AVB 三态实测（**对本任务第 3 目标最关键**）：半截镜像 → LK 冻结；32 MiB 全零尾 → 冻结；**32 MiB + `avbtool add_hash_footer --algorithm NONE` → 可启动**。
- 无 `init_boot` 分区；`vendor_boot` 64 MiB，recovery ramdisk 在其内；boot.img = **boot header v4，仅 gzip 内核，ramdisk_size=0，page 4096**。
- 刷机链：`lkpatcher`（lk_a/lk_b 都刷）→ `fastboot flashing unlock` → `fastboot flash --disable-verity --disable-verification vbmeta <原版备份>`。
- 产物：`Image.gz` + `tools/mkboot_v4.py`（纯 Python 构造 boot v4）。

#### (c) `feliksyasnopolski/nevedoma-zverushka`
https://github.com/feliksyasnopolski/nevedoma-zverushka（注意：**默认分支是 `master`，不是 `main`**，`/main/README.md` 会 404）

- "native Linux device-enablement stack for the Lenovo Tab M11 / TB330FU"，**"boots … on the tablet's stock-derived Android 15 Linux 6.6 kernel and MediaTek BSP"**，SoC 写作 **"MediaTek MT6768-family BSP"**。（已核实，存档 `raw/NZ_README.md`）
- `docs/BOOT.md`：自建 vendor ramdisk 替换 init；工具 `scripts/rebuild_vendor_boot.py`（vendor_boot v4 解析 + 片段替换）、`validate_vendor_boot.py`、`build_vendor_boot.sh`；**"replaces exactly the platform `init_boot` fragment and preserves the other vendor ramdisk fragments and container metadata"**；"supported container is Android vendor_boot v4 with one platform fragment named `init_boot`"。（已核实，存档 `raw/NZ_BOOT.md`）
- ⚠️ 它**没有**说明如何获取原厂 6.6.118 内核源码（它依赖"external inputs"），所以**不能作为原厂源码来源**。

#### (d) `threadexio/tb330fu` —— 解锁链源头（README 在 `master`）
https://github.com/threadexio/tb330fu
- 一手流程（已核实，存档 `raw/threadexio_tb330fu__README.md`）：
  1. `mtk_gui` / BROM 全分区备份（除 userdata/cache）；
  2. **`lkpatcher lk_a.bin -o lk_a.patched.bin`（lk_a、lk_b 都要）**，不补丁会 "Dm-verity corruption error"；
  3. 用 Magisk 打 **boot_a/boot_b 和 vendor_boot_a/vendor_boot_b 四个镜像**；
  4. `fastboot flashing unlock`；
  5. `fastboot flash --disable-verity --disable-verification vbmeta_a|boot_a|vendor_boot_a …`（a/b 各刷一遍）。
- `lkpatcher`（R0rt1z2）：Python ≥3.8，`pip3 install git+https://github.com/R0rt1z2/lkpatcher`，`python3 -m lkpatcher lk.img -o patched.img`；补丁以 JSON 的 needle/patch 字节对形式存在，category 含 `fastboot`、`dm_verity`（示例：`"dm_verity": {"30b583b002ab0022": "00207047"}` → 即把函数头改成 `mov x0,#0; ret`）。**注意其 README 明确前提："unlock seccfg using mtkclient"**，以及"若解锁后不能启动，设备可能使用 SBC，不受支持"。证据: https://github.com/R0rt1z2/lkpatcher （已核实，存档 `raw/R0rt1z2_lkpatcher__README.md`）

> **对 lkpatcher 适用性的判断**：它是"字节模式匹配 + 补丁库"的通用工具，不是 MT6768 专用；是否适用取决于你的 `lk_a.bin` 里能否命中 pattern。**【推断】** 用 `python3 -m lkpatcher lk_a.bin --analyze-policies --dry-run` 先看命中情况，再决定。你现在的机器**已经解锁且已 root**，说明这条链已被前人走通，**升级到 ZUI 17 后不必重做**（除非你重刷了 lk）。

### 1.4 GKI 2.0：`kernel/common` android15-6.6 与 MTK `kernel/mediatek` 的关系

- **GKI 2.0 的模型**：`kernel/common`（ACK）出 GKI 内核二进制，MTK/Lenovo 的板级驱动以 **DLKM（vendor modules）** 形式放在 `vendor_dlkm`/`vendor_boot`，通过 KMI（`android/abi_gki_aarch64*`）与 GKI 内核绑定。这正是为什么本机厂商 .ko 会校验 `UTS_RELEASE` + 符号 CRC。
- 所以：**MTK 的 `kernel/mediatek` 不是本设备的源码基线**（它连 6.6 分支都没有）；你的原厂 6.6.118 内核**就是** `kernel/common`（android15-6.6）某个 commit + 一份 LENOVO/MTK 私有 defconfig + 一组 vendor modules。【已核实的部分：`kernel/mediatek` 无 6.6；基线为 `kernel/common` 的部分：Barley-Project 明确说明并以 `git fetch android.googlesource.com/kernel/common <commit>` 复现成功】
- **MTK 产品源码的目录约定**（BayLibre 文档，已核实）：`common/`（公共内核）+ `board/`（产品设备树与驱动）+ 外部模块 + `prebuilts`、`build`、`build.config.mtk`。**"all the drivers written for a customer must be modules"**。

### 1.5 android15-6.6 的分支/tag 现状（**已核实，直接查 refs 得到**）

查询：`https://android.googlesource.com/kernel/common/+refs?format=TEXT`（51 MB 明文）

- **`android15-6.6.118_r00` 存在**：`refs/tags/android15-6.6.118_r00` → commit `5650aac9411fe9b3462b10d9f038054e94f86cbe`（peeled），其 Makefile 实测为 `VERSION=6 / PATCHLEVEL=6 / SUBLEVEL=118 / EXTRAVERSION=`。
- **`android15-6.6.30` 确实不存在**（最低 r00 tag 是 `6.6.46_r00`，另有 50/56/57/58/66/76/77…），与 lifengqaq README 的说法一致。→ **6.6.30 的原厂基线只能按 commit SHA 拉。**
- 日期分支：`android15-6.6-2025-09 / 2025-10 / 2026-01 / 2026-04 / 2026-07 / 2026-10`；另有 `android15-6.6`、`-lts`、`-sp`、`-partner_predev`、`-desktop*`、`-pkvm_experimental`。
- **`kernel/common` 里没有叫 `android15-8` 的分支**（grep refs 命中 0）。→ 原厂串里的 `-android15-8` 是 **MTK/联想 build 配置（`build.config`）里的分支名**，不是 ACK 分支，**必须靠 `CONFIG_LOCALVERSION` 硬编码复现**。【已核实"无该分支" + 推断"是 build.config 名"】
- 查询接口：https://android.googlesource.com/kernel/common/+refs 与 `+/refs/tags/android15-6.6.118_r00/Makefile?format=TEXT`（已核实）

### 1.6 编译工具链 / bazel vs build.sh（android15-6.6）

| 项 | 事实 | 标记 |
|---|---|---|
| `build/build.sh` | AOSP 文档原文：**"build.sh is not supported on Android 14 and above"**；仅适用于 ≤ Android 12 或无 Kleaf 的分支。 | 已核实 |
| 官方推荐 | "Build with Bazel (Kleaf)"：`tools/bazel run //common:kernel_aarch64_dist -- --destdir=$DIST_DIR`；需 `repo init -u https://android.googlesource.com/kernel/manifest -b BRANCH && repo sync`。 | 已核实 |
| 是否强制 | AOSP 文档没说"禁止 make"，Rust/第三方均不走 Kleaf；**实机证据：Barley-Project 用纯 `make O=... Image modules` 在 android15-6.6 上成功产出可启动 Image 并通过 CRC 比对**。 | 已核实（`raw/..._build-kernel.sh`） |
| 工具链 | `clang-r510928`（clang 18.0.0），来自 `android15-release` 的 prebuilts；**不要用 NDK 的 clang**——AOSP 内核构建用的是 `prebuilts/clang/host/linux-x86`，NDK 是给 userspace 的。 | 已核实（路径）+ 推断（"别用 NDK"） |
| 依赖 | `make python3 pahole flex bison bc openssl depmod`，以及 BTF 需要 pahole。 | 已核实 |

证据：https://source.android.com/docs/setup/build/building-kernels （已核实，存档 `raw/android_build.html`）
https://github.com/Barley-Project/tb330fu-ubuntu/blob/main/scripts/kernel/fetch-toolchain.sh （已核实）

> **结论**：android15-6.6 上 **Kleaf/bazel 是官方路径但非强制**；对"少量改动 + 精确复现 vermagic"的目标，**纯 make 反而是更可控的路径**（Kleaf 会注入 `-ab<build_id>` 之类的东西，见 §1.7）。

### 1.7 在 **6.6.118** 上复现官方 vermagic（方法论，数值待你取证）

原厂串结构（由 lifengqaq README 给出的 6.6.30 样本推得，**结构可复用，数值不可复用**）：

```
6.6.118 - android15-8 - g<12位sha> - ab<build_id> - -4k
                                              └ CONFIG_LOCALVERSION 原值（是否仍为 -4k 待取证）
                               └ kleaf build id（ab 前缀）
                  └ git describe -g<sha>
       └ build.config 分支名
Makefile VERSION.PATCHLEVEL.SUBLEVEL
```

**设备侧取证步骤（在平板上做，无需拆包）**：
```sh
uname -r                      # 期望输出 6.6.118-android15-8-gXXXXXXXXXXXX-abXXXXXXXX-4k
cat /proc/version             # 同上 + 编译器信息
getprop ro.boot.slot_suffix   # 当前槽位
getprop ro.build.display.id   # ZUI 17.0.502 校验
# 有 root 时最可靠：
zcat /proc/config.gz | grep -E 'LOCALVERSION|MODVERSIONS'   # 若开了 IKCONFIG_PROC
```
**PC 侧取证（拿官方 defconfig 的唯一正确来源）**：
```sh
python3 scripts/extract-ikconfig boot_a.img > barley_6.6.118_defconfig
# 或 AOSP：python3 common/scripts/extract-ikconfig
grep -E 'CONFIG_LOCALVERSION(|_AUTO)|CONFIG_MODVERSIONS|CONFIG_IKCONFIG' barley_6.6.118_defconfig
# 再从 boot.img 里取内核 banner 复核：
python3 mkboot_v4.py info boot_a.img     # 或 mkbootimg/unpack_bootimg 提取 kernel 段
gzip -dc kernel.gz | strings | grep -m1 'Linux version 6\.6\.'
```
⚠️ **注意矛盾点**：lifengqaq 说官方 `CONFIG_LOCALVERSION_AUTO=y`，而 Barley-Project 说"release string is CONFIG_LOCALVERSION (**no scm suffix**)"并断言官方 config 里 `CONFIG_LOCALVERSION_AUTO=y`。两者都用"把完整后缀写进 `CONFIG_LOCALVERSION` + 关掉 AUTO"来完成复现。**你必须以自己提取的 `/proc/config.gz` 为准**，不要照抄任一方。（已核实两份原文均如此写，但它们描述的是不同固件）

必需的构建开关：
```sh
CONFIG_LOCALVERSION="-android15-8-g<sha>-ab<buildid>-4k"   # ← 逐字符，用你取证到的串
# CONFIG_LOCALVERSION_AUTO is not set
# 构建环境
export LOCALVERSION=""            # 关键：否则 setlocalversion 会追加 '+'
export KBUILD_BUILD_TIMESTAMP='<原厂 /proc/version 里的时间>'   # 若要 UTS_VERSION 也一致
```
**验收**：`make -s O=build kernelrelease` 必须逐字符等于 `uname -r` 的输出，否则不要刷。

### 1.8 boot 镜像形态与 AVB（**直接影响重打包**）

| 事实 | 标记 |
|---|---|
| Barley-Project 的 `build-boot.sh` 注释：原厂 `boot-debug.img` 是 **boot header v4 + gzip 内核（gzip -9）+ 通用 ramdisk（3022350 字节）+ 空 cmdline**（cmdline 在 vendor_boot），补零到 **32 MiB**。**"the stock image continues with an AVB hash footer (vbmeta)"**。 | 已核实（存档 `raw/BP__build-boot.sh`） |
| lifengqaq：**LK 必须在 boot 分区找到合法 AVB footer 才能启动**；32 MiB + `avbtool add_hash_footer --algorithm NONE` 可启动。 | 已核实（第三方一手作者实测，非我复现） |
| 两者冲突：Barley-Project **不重建** AVB footer（依赖"设备跑在绕过校验的状态下"）；lifengqaq **必须**重建 NONE footer。 | 已核实（原文如此） |

**`avbtool add_hash_footer` 的确切参数——一手原文（可直接抄）**

lifengqaq 仓库的 CI 里有**完整、已在实机验证过格式**的一步（`.github/workflows/build.yml`，原文已核实，存档 `raw/lifengqaq__tb330fu-kernelsu-droidspaces__.github_workflows_build.yml`）：

```yaml
- name: Add AVB hash footer (community-proven format, algorithm NONE)
  # TB330FU 的 LK 会解析 boot 分区的 AVB footer；半截镜像刷入会残留旧 footer
  # 指向已被覆盖的 vbmeta 导致冻结在第一屏。此步骤产出 32MB 整尺寸镜像，
  # 带 NONE 算法哈希描述符 footer（与已验证可启动的社区镜像同构）。
  run: |
    cp boot.img boot-nosig.img
    python3 tools/avbtool.py add_hash_footer \
      --image boot.img \
      --partition_size 33554432 \
      --partition_name boot \
      --algorithm NONE \
      --salt 014d0f17ca8359f23ed8b03e35e679b983b87f8f4d4636a9d5e41c6e19a09c0c \
      --prop "com.android.build.boot.os_version:15" \
      --prop "com.android.build.boot.fingerprint:Lenovo/TB330FU/TB330FU:15/AP3A.240905.015.A2/S2001962_250417_ROW:user/release-keys" \
      --prop "com.android.build.boot.security_patch:2024-09-05"
    python3 tools/avbtool.py info_image --image boot.img
```

要点（**已核实自上述原文**）：
1. `--partition_size 33554432`（32 MiB）——**镜像先补零到该尺寸**（`mkboot_v4.py --partition-size 33554432`），avbtool 只负责追加 footer 区。
2. `--algorithm NONE` = 无签名、无 hash 校验，但仍产出**结构合法**的 footer + 内嵌 vbmeta —— LK 能解析、无签名可验 → 放行。这就是 lifengqaq 报告"可启动"的形态。
3. `--salt` 用的是**一个固定的 32 字节 hex**（不是随机），目的是让产物可复现。若你想让 `/proc/bootconfig` 之类字段与原厂更接近，可以照抄；想区分自编镜像则换一个。
4. 三个 `--prop` 是 **boot 的 AVB property**（`os_version` / `fingerprint` / `security_patch`）。注意 `--prop` 里的指纹写的是 **`S2001962_250417_ROW`**（作者的固件），**你应改成自己 `getprop ro.build.fingerprint` 的输出**，否则 LK/上层读到的 AVB 指纹与 build 指纹不一致，可能被判定异常。
5. 校验：`avbtool info_image --image boot.img`，看 footer 里 `Algorithm: NONE`、`Image size` 与 `Original image size`。
6. **矛盾仍在**：Barley-Project 的 `build-boot.sh` 明确**不生成** footer（"the stock image continues with an AVB hash footer (vbmeta): not reproduced, the device runs with verification bypassed"）。→ **先在你机器上 `xxd -s -64 boot_a.img | grep AVBf` 判定原厂形态，再决定**。两种做法不互斥：最保险是"替换内核段 + 用 add_hash_footer 重建 NONE footer"，这样无论 LK 是否要求 footer 都能过。

**更省事的替代**：若原厂 `boot_a.img` 自带 footer，可"就地覆盖 kernel 段 + 保留尾部 footer"；但 kernel 大小一变 footer 的 `original_image_size` 就错，所以**还是重跑 `add_hash_footer` 最稳**。

证据：https://github.com/lifengqaq/tb330fu-kernelsu-droidspaces/blob/main/README.zh.md （已核实）；https://github.com/Barley-Project/tb330fu-ubuntu/blob/main/scripts/kernel/build-boot.sh （已核实）

---

## 2. SUSFS

### 2.1 上游与 6.6 分支

| 项 | 事实 | 标记 |
|---|---|---|
| 官方上游 | GitLab `simonpunk/susfs4ksu`（**官方源**）。本机对 gitlab.com 返回 **429 rate limit**，无法直读其页面。 | 已核实"429" |
| 6.6 分支 | **`gki-android15-6.6`**（`gitlab.com/simonpunk/susfs4ksu/-/tree/gki-android15-6.6`），另有 `gki-android15-6.6-dev` / `-mod` 等活动分支。 | 已核实（搜索结果 + 多镜像仓库引用同一分支名） |
| 可用镜像 | `github.com/Zarathos30/susfs4ksu`（`gki-android15-6.6-dev`，我实际从这里抓的源码）；`omajili-manbu/susfs4ksu`（`gki-android15-6.6-mod`）；`ShirkNeko/susfs4ksu`；`wxx9248`、`Star-Seven`、`dev-sm8350` 等。 | 已核实（`Zarathos30` 分支的 `kernel_patches/` 目录列表与文件已抓） |
| 版本（我抓到的镜像） | `include/linux/susfs.h` 中 `#define SUSFS_VERSION "v2.3.0"` | 已核实 |
| 补丁文件名 | `kernel_patches/50_add_susfs_in_gki-android15-6.6.patch`（注意：**不是** `50_add_susfs_in_kernel-6.6.patch`，那个名字在 6.6 分支上 404） | 已核实 |
| 目录结构 | `kernel_patches/{50_…patch, KernelSU/10_enable_susfs_for_ksu.patch, fs/（susfs.c）, include/linux/（susfs.h, susfs_def.h）}` + `ksu_susfs/`（userspace 工具 C 源） + `ksu_module_susfs/`（示例 KSU 模块） + `build_ksu_susfs_tool.sh` | 已核实（`fs/` 下**没有** Kconfig/Makefile，两者由 50 号补丁改） |

证据：
- https://gitlab.com/simonpunk/susfs4ksu/-/tree/gki-android15-6.6 （429，未能直读）
- https://github.com/Zarathos30/susfs4ksu/tree/gki-android15-6.6-dev/kernel_patches （已核实，原文存档 `raw/susfs50_gki-android15-6.6.patch` 84 KB）

### 2.2 补丁落点：**我逐文件核对了 50 号补丁触碰的 26 个文件**

`50_add_susfs_in_gki-android15-6.6.patch` 的 `diff --git` 清单（**已核实，完整**）：

```
block/ioprio.c            drivers/input/input.c     fs/Makefile
fs/exec.c                 fs/namei.c                fs/namespace.c
fs/notify/fdinfo.c        fs/open.c                 fs/proc/base.c
fs/proc/bootconfig.c      fs/proc/fd.c              fs/proc/task_mmu.c
fs/proc_namespace.c       fs/read_write.c           fs/readdir.c
fs/stat.c                 fs/statfs.c               fs/super.c
kernel/kallsyms.c         kernel/reboot.c           kernel/sys.c
mm/memory.c               security/selinux/avc.c    security/selinux/hooks.c
security/selinux/selinuxfs.c
```
外加两个**新文件**（由 `kernel_patches/{fs/susfs.c, include/linux/susfs.h, include/linux/susfs_def.h}` 拷入内核树）。

**接入方式（原文用法，已核实）**：
```bash
cp kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch  $KERNEL_REPO/KernelSU/
cp kernel_patches/50_add_susfs_in_gki-android15-6.6.patch $KERNEL_REPO/common/
cp kernel_patches/fs/*              $KERNEL_REPO/common/fs/           # 放入 susfs.c
cp kernel_patches/include/linux/*   $KERNEL_REPO/common/include/linux/ # 放入 susfs.h / susfs_def.h
(cd $KERNEL_REPO/KernelSU && patch -p1 < 10_enable_susfs_for_ksu.patch)
(cd $KERNEL_REPO/common   && patch -p1 < 50_add_susfs_in_gki-android15-6.6.patch)
```
**`fs/Kconfig` / `fs/Makefile` 的插入**（已核实）：
- `fs/Makefile`：在 `obj-y := open.o read_write.o … remap_range.o` 块之后**新增一行**
  `obj-$(CONFIG_KSU_SUSFS) += susfs.o`
- **`fs/Kconfig` 不需要改**：`CONFIG_KSU_SUSFS` 定义在 **KernelSU 的 `kernel/Kconfig`**（`menu "KernelSU - SUSFS"`），该文件在开启 `CONFIG_KSU` 时已被 `drivers/Kconfig` source 进来。
- `include/linux/susfs_def.h` 里有 `#define SUSFS_MAGIC 0xFAFAFAFA`、`CMD_SUSFS_SHOW_VERSION 0x555e1` 等 ioctl 命令号。

**冲突热点（依你表里列的 `kernel/sys.c` / `fs/namespace.c` / `fs/open.c`，我给出补丁里的实际 hunk 位置）**：

| 文件 | hunk 位置（补丁 @@ 头） | 做什么 | 冲突风险 |
|---|---|---|---|
| `fs/namespace.c` | `@@ -32,12 +32,22`（include）；`@@ -147,+4`（`mnt_alloc_id`）；`@@ -155,+25`（`mnt_free_id`，**插入 119 行 helper**：`susfs_alloc_unshare_ksu_vfsmnt` / `susfs_alloc_non_unshare_ksu_vfsmnt`）；`@@ -697,+20`（`__lookup_mnt`）；`@@ -1094,+21`（`vfs_create_mount`）；`@@ -1176,+42`（`clone_mnt`） | 假 mnt_id / 隐藏 sus mount | **高**：`clone_mnt`/`vfs_create_mount` 在 6.6 各 sublevel 间有改动；厂商若有 mount 相关改动会更糟 |
| `fs/open.c` | `@@ -34,6 +34,9`；`@@ -455,+6` / `@@ -462,+9` / `@@ -481,+23`（`do_faccessat`） | 路径隐藏 + `OPEN_REDIRECT` | **中**：`do_faccessat` 签名在 6.6 稳定，但 6.6.118 需实测 |
| `fs/namei.c` | **17 个 hunk**，遍布 `lookup_dcache`、`lookup_one_qstr_excl`、`lookup_fast`、`__lookup_slow`、`walk_component`、`link_path_walk`、`lookup_last`、`lookup_open` | `susfs_is_inode_sus_path()` + `susfs_fake_qstr_name` 假 dentry | **极高**：`fs/namei.c` 是每轮 LTS 都最容易变的文件之一 |
| `kernel/sys.c` | `@@ -217,+4`（`set_one_prio`）、`@@ -263,+4`（`setpriority`）、`@@ -331,+4`（`getpriority`）、`@@ -661,+3`（`setuid`）、`@@ -675,+4`（`__sys_setresuid`）、`@@ -1305,+21`（`override_release` → `susfs_spoof_uname`） | uname 伪造 + uid 相关 | **中**：`override_release` 在 6.6 各 sublevel 有微调 |
| `kernel/kallsyms.c` | 隐藏 ksu/susfs 符号 | | 中 |
| `fs/stat.c` / `fs/read_write.c` / `fs/exec.c` | 各若干 | | 中 |

**"版本适配"的现实做法（原文警告 + 实操）**：
- 上游 README 原文：**"The susfs kernel patches may differ for different kernel version or even on the same kernel version; you may need to create your own patches for your kernel."**（已核实）
- 实操：`patch -p1 --fuzz=3 --dry-run` 先看；失败 hunk 读 `.rej` 手工落位。lifengqaq 的仓库给了同款经验：**`git apply` 整包拒绝时，`patch -p1 --fuzz=2` 可干净落位**，并建议"若 `.rej` 则 workflow fail"。（已核实，存档 `raw/lifengqaq_..._patches_README.zh.md`）

### 2.3 Kconfig 项名称与依赖（**已核实，逐项抄自补丁**）

```kconfig
menu "KernelSU - SUSFS"
config KSU_SUSFS                     # bool, depends on KSU && THREAD_INFO_IN_TASK, default y
config KSU_SUSFS_SUS_PATH            # bool, depends on KSU_SUSFS, default y
config KSU_SUSFS_SUS_MOUNT           # bool, depends on KSU_SUSFS, default y
config KSU_SUSFS_SUS_KSTAT           # bool, depends on KSU_SUSFS, default y
config KSU_SUSFS_SPOOF_UNAME         # bool, depends on KSU_SUSFS, default y
config KSU_SUSFS_ENABLE_LOG          # bool, depends on KSU_SUSFS, default y
config KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS      # bool, default y
config KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG # bool, default y
config KSU_SUSFS_OPEN_REDIRECT       # bool, default y
config KSU_SUSFS_SUS_MAP             # bool, default y
endmenu
```
**依赖关系**：全部 `depends on KSU`（间接经 `KSU_SUSFS`）。`KSU` 本身 `tristate, depends on KPROBES && EXT4_FS, default y`。（已核实，两个 Kconfig 都读过）

**关于 `KSU_SUSFS_HAS_MAGIC_MOUNT`：在 `gki-android15-6.6-dev`（SUSFS v2.3.0）的补丁与 Kconfig 中均 grep 命中 0 次。** → 该符号在当前 6.6 分支**已不存在**（推测是旧版本/其它分支的历史项）。**【未找到可靠来源】** 无 6.6 版本文档记载它。

**注意坑（已核实原文）**：SUSFS 的 `KSU_SUSFS` 以及上面每一项都是 `default y`。若你的构建流程先 `olddefconfig` 再叠 fragment，**会出现"没开 SUSFS 也被默认打开"**——这正是 Barley-Project 在 `tb330fu-sukisu.config` 里叮嘱的做法：**先把 `config KSU` 的 `default y` 改成 `default n`**。同理，集成 SUSFS 后应把 `KSU_SUSFS` 显式设为 `n` 的基线，只在目标 fragment 里开。

SukiSU 额外项（`builtin` 分支 Kconfig，已核实）：`KSU_DEBUG`、`KSU_MANUAL_SU`(default y)、`KPM`(depends KSU && 64BIT, select KALLSYMS(_ALL))、`KSU_DISABLE_MANAGER`、`KSU_DISABLE_POLICY`、`KSU_X86_PATCH_SYSCALL_DISPATCHER`(x86_64 only)。

### 2.4 SukiSU Ultra 内核侧集成 & **"LKM 模式能否带 SUSFS"**

#### 事实 A：SukiSU 有两个分支，**只有 `builtin` 分支带 SUSFS**
`docs/guide/how-to-integrate.md` 原文表格（已核实，存档 `raw/SukiSU__how-to-integrate.md`）：

| 分支 | hook 方式 | 用途 |
|---|---|---|
| `main` | Kprobes/kretprobes + `sys_enter` tracepoint，运行时注册，**内核源码零 hook 调用点** | GKI，编进内核**或**做 module（LKM） |
| `builtin` | 无 kprobes；内核源码调用 SukiSU 的 hook 入口（manual hooks） | 手工打补丁的内核 |

原文紧接一句：**"This branch [`builtin`] also carries the SUSFS options (`CONFIG_KSU_SUSFS` and its sub-options) and `CONFIG_KPM`."**
我也实测：`main` 分支 `kernel/Kconfig` 里 `SUSFS` 命中 **0**；`builtin` 分支命中 **22**（10 个 config + 依赖行）。**【已核实】**

> → **结论 1**：你当前 LKM 模式用的 `kernelsu.ko` 来自 **`main` 分支**，**它根本没有 SUSFS 代码**。想在 `main` 上加 SUSFS，必须自己把 `10_enable_susfs_for_ksu.patch` 打到 `main` 上（上游该补丁是给 KernelSU 主线写的，与 SukiSU 的 `main` 分支文件布局未必对齐）。

#### 事实 B：SUSFS 的内核侧**必须改 26 个内核源文件**，所以**不能只做".ko"**
SUSFS 的隐藏逻辑是在 `fs/namei.c` / `fs/namespace.c` / `fs/open.c` / `fs/stat.c` / `kernel/sys.c` / `security/selinux/*` **内部**插入调用点，并要求 `fs/susfs.c` 编进 `fs/`（`obj-$(CONFIG_KSU_SUSFS) += susfs.o`）。这不是"加一个模块"，是**改内核树**。
→ **只要 SUSFS，就必须自己编内核（或找到别人编好的同 KMI 内核）。LKM 模式 + 官方 kernelsu.ko 无法"带上"SUSFS。**

#### 事实 C：存在"把 SUSFS 做成 LKM"的第三方移植，但**明确有代价**
`inforcqb/susfs4ksu-for-lkm`（README 已核实，存档 `raw/...`）：
- 目标：**"让 susfs 可以在不用重编译内核的情况下运行"**，用 `susfs_guard_lkm-android13-5.15.ko` 之类按 KMI 分发的 `.ko`，`ksud insmod` 加载，通过 `/proc/susfs_*` 下发规则。
- 作者自述其技术手段来源：`patch_memory` / `lsm_hook` / `symbol_resolver`——**即运行时改内核内存/挂 LSM**。
- 只覆盖它自己发布过的 KMI（示例是 5.15）。
- **对我们（6.6.118 MTK，无 KMI 官方列表）**：需自编 `susfs_guard_lkm.ko`；且它 hook 的是 `fs/*` 内部静态函数，**没导出符号就只能靠 kallsyms 挖 + 直接改指令**，在 `CONFIG_MODULE_SIG_PROTECT` + kCFI 的 GKI 内核上风险很高。
  → **【推断】这是"研究性可行、生产不建议"的路**。除非你能拿到同 KMI 的现成 `.ko`。

#### 事实 D（关键）：**LKM 的 `kernelsu.ko` 本身也不能随便编 SUSFS**
Barley-Project 的 `tb330fu-sukisu.config` 原话（已核实）：
> "It has to be built in: **it calls kernel-internal functions that are not exported to modules, so a loadable module cannot be linked (modpost: undefined)**."

而官方 LKM 的 `kernelsu.ko` 之所以能加载，是因为它**只依赖导出符号 + kprobes + syscall 表 patch**。任何需要"未导出内核内部函数"的改动（SUSFS 大面积是），都会在 `modpost` 阶段炸。

#### 结论（SUSFS 可行性判定）

| 路线 | 可行性 | 代价 |
|---|---|---|
| **A. 换内核**：自己编 `6.6.118` + `builtin`/`main`+SUSFS 补丁，刷 boot | **唯一稳妥** | 需先解决 §1.7 vermagic 与 §1.8 AVB；SUSFS 26 文件逐一调；`fs/namei.c` 17 hunk 风险最高 |
| **B. LKM 式 SUSFS**（`susfs4ksu-for-lkm`） | 研究可行、工程不可靠 | 需自编同 KMI `.ko`；kCFI/受保护导出/符号解析障碍；无官方支持 |
| **C. 不加 SUSFS**，用 KSU 自身 hide + ZygiskNext + (`lifengqaq` 路线) | **最省事** | 隐藏能力弱于 SUSFS（无 SUS_PATH / SUS_MOUNT / SUS_KSTAT） |

**如果走 A，必踩的冲突点清单（按风险排序）**：
1. `fs/namei.c` 17 个 hunk（`lookup_fast` / `__lookup_slow` / `lookup_open` 上下文最易漂）；
2. `fs/namespace.c` 的 `clone_mnt` / `vfs_create_mount`（119 行新 helper 插在 `mnt_get_count` 后）；
3. `kernel/sys.c` 的 `override_release`（uname 伪造）；
4. `security/selinux/{avc.c,hooks.c,selinuxfs.c}`（log 伪造）；
5. `fs/proc/{base.c,task_mmu.c,fd.c}` + `fs/proc_namespace.c`（maps/mounts 隐藏）；
6. `mm/memory.c`（SUS_MAP）；
7. **`KSU` 版本耦合**：SUSFS 的 `10_enable_susfs_for_ksu.patch` 会改 KernelSU 的 `kernel/Kconfig`、`core/init.c`、`feature/sucompat.{c,h}`、`hook/setuid_hook.c`、`policy/*`、`preinit/*`、`runtime/ksud_integration.c`、`supercall/*`、`selinux/rules.c` 等 **30+ 个 KernelSU 内部文件**（我数了补丁里 `+++ b/kernel/...` 共 30 条）。**SukiSU 与上游 KernelSU 的文件布局必须匹配，否则这个补丁要整体重写。**
   → 建议：用 **SukiSU `builtin` 分支**（官方已带 SUSFS），不要拿上游补丁硬打 SukiSU `main`。

---

## 3. 内核级 Zygisk

### 3.1 三者的现实定位

| 项目 | 实现方式 | 是否需要内核补丁 | 标记 |
|---|---|---|---|
| **ZygiskNext**（Dr-TSNG） | "Standalone implementation of Zygisk" —— **用户态模块**（KSU/Magisk module zip），无内核组件。要求 KernelSU **≥ 10940**、ksud **≥ 11575**。**从 v4-0.9.2 起闭源**（"all rights reserved"，禁改、禁分发、禁摘取）。 | **否**（只要 root 方案够新） | 已核实 |
| **ReZygisk**（PerformanC） | ZygiskNext 的 fork，**重写为 C**，用 `PLTI`（PLT hook）+ `CSOLoader`（自研 linker）避免用系统 linker。KSU/APatch/Magisk module。AGPL-3.0。 | **否** | 已核实 |
| **NeoZygisk**（JingMatrix） | **`ptrace` 注入 Zygote**；API 兼容 Magisk 内置 Zygisk；自己实现 DenyList（直接 unmount 或 `setns` 切干净 mount ns）。APatch/KSU module。 | **否** | 已核实 |

证据：
- https://github.com/Dr-TSNG/ZygiskNext （已核实，存档 `raw/Dr-TSNG_ZygiskNext__README.md`）
- https://github.com/PerformanC/ReZygisk （已核实，存档 `raw/PerformanC_ReZygisk__README.md`）
- https://github.com/JingMatrix/NeoZygisk （已核实，默认分支 **master**，存档 `raw/NeoZygisk_README.md`；描述 "Zygote injection with ptrace"）

### 3.2 "SUSFS 之外是否存在真正的内核级 Zygisk"？

**结论：没有。截至本次调研，我未找到任何"内核态 Zygisk 注入器"的证据。**

- 三个实现全都在**用户态**：ZygiskNext/ReZygisk 是"root 方案启动一个 daemon → 在 zygote fork 时注入 .so"；NeoZygisk 更明确，是"用 `ptrace` 附加 zygote"。三者都**不修改内核源码、不加载 .ko**。
- 机制上不可替代：Zygisk 的本质是**在 `forkAndSpecialize` 之前/之后把 native 库 load 进子进程**，这让应用进程能跑 native hook（LSPosed 等）。这可以在用户态做（ptrace/PLT），也可以靠内核（tracepoint 拦截 `execve`），但**内核态做 native .so 加载没有成熟实现**——因为内核无法替你调用 `dlopen`。
- **所谓"内核辅助"的真实边界**：ZygiskNext 依赖的"内核能力"其实来自 **KernelSU 本身**（`ksu` 的 `su` / `umount` / `setresuid` hook / 各进程的 `TIF_PROC_UMOUNTED` 标记 / `/data/adb/modules` 的 overlay）。**ZygiskNext 不引入任何内核补丁。**
- SUSFS 与 Zygisk 的关系是**互补而非替代**：SUSFS 负责让"文件/挂载/kstat"看不见；Zygisk 负责"把模块 .so 注入 app 进程"。**两者解决的问题不同**。真正和 ZygiskNext 功能重叠的是 **NeoZygisk 的 DenyList**（用户态做 mount ns 清理）与 **SUSFS 的 `SUS_MOUNT`**（内核态做假 mnt_id）——后者更彻底。

**【未找到可靠来源】**：没有任何项目自称"kernel-level Zygisk"或"Zygisk in kernel"。

---

## 4. LKM 模式修补细节：为什么 boot **和** vendor_boot 都要动

### 4.1 LKM 注入的真实机制（读 `ksud` 源码得到，非猜测）

来源：`tiann/KernelSU` 的 `userspace/ksud/src/boot_patch.rs`（已核实，存档 `raw/tiann_ksud_boot_patch.rs`；SukiSU 版 `raw/SukiSU__boot_patch.rs` 逻辑一致）

1. **选分区**（`choose_boot_partition`）：
   ```rust
   let skip_init_boot = kmi.starts_with("android12-");        // 「android12-6.6」→ 跳过 init_boot
   let init_boot_exist = Path::new(&format!("/dev/block/by-name/init_boot{slot_suffix}")).exists();
   if !is_replace_kernel && init_boot_exist && !skip_init_boot { return "init_boot" }
   "boot"
   ```
   允许显式 `--partition boot|init_boot|vendor_boot`（`--partition` 覆盖）。**你的 KMI 是 `android12-6.6`（6.6 内核都归 android12 KMI），所以默认走 `boot`；本机又确实没有 `init_boot` 分区 → 只能 `boot`。**
2. **取 ramdisk**：若目标分区有 ramdisk 则用它；**没有则新建空 cpio**（日志 `- No ramdisk, create by default`）。→ 你机器的 `boot.img` 是 v4 + `ramdisk_size=0`，这里会**新建**一个 cpio，把 inject 内容写进去。
3. **vendor_boot 特例**（`extract_ramdisk`）：若镜像 is_vendor_ramdisk，则**优先取名为 `init_boot` 的 fragment，否则取名字为空的 fragment**，只改这一段，其它 fragment 原样保留：
   ```rust
   .find(|e| e.1.get_name_raw() == b"init_boot")
   .or_else(|| ... find(|e| e.1.get_name_raw() == b"") )
   ```
4. **注入内容**（`boot_patch` 主体）：
   ```rust
   if !is_kernelsu_patched && cpio.exists("init") { cpio.mv("init", "init.real")?; }
   cpio.add("init",         CpioEntry::regular(0o755, ksu_init))?;   // 资产 ksuinit
   cpio.add("kernelsu.ko",  CpioEntry::regular(0o755, kernelsu_ko))?; // 资产 {kmi}_kernelsu.ko
   … cpio.add("ksu_config", 0o644, …)                                 // 参数（allow_shell / spoof_release …）
   ```
5. **`ksuinit`** 就是那个"假 init"：它在 **first stage / init 阶段**把 `/kernelsu.ko`（连同 `ksu_config` 里的 `spoof_release`/`spoof_version`）`insmod` 进去，然后**链式 `exec` 原来的 `init.real`**。
6. 存在 `boot-patch-v2` 概念（`ksu_config` 注释："stored in ramdisk `/ksu_config` (**boot-patch**) or the **injected capsule** (**boot-patch-v2**)"）——新版把参数装进"capsule"。**【推断】** 这就是为什么你在两个分区都看到了改动。

**所以"boot + vendor_boot 双注入"的技术解释**：
- `boot` 分区的 ramdisk（v4、可为空）→ 装 `init`(ksuinit) + `kernelsu.ko`；
- 但**安卓真正的 first-stage ramdisk 在 `vendor_boot`**（你的机器没有 `init_boot`，`vendor_boot` 的 `init_boot` fragment 承担 early init 的 cpio），SukiSU/ksud 为了让 `ksuinit` **在 init 真正起来的最早时刻**被调到，需要在 `vendor_boot` 的 `init_boot` fragment 里也放一份入口；
- 两处内容用 `ksu_config` 里的 **`spoof_release` / `spoof_version`** 保证 `kernelsu.ko` 的 vermagic 校验通过（你的原厂内核开 `CONFIG_MODVERSIONS=y`，`kernelsu.ko` 必须带 `6.6.118-android15-8-…-4k` 的 `vermagic` 才能 insmod）。

**【重要，待你验证】** 上面第 6 条"哪个分区承担 first stage ramdisk"在本机是二选一：
- lifengqaq：**本机没有 init_boot 分区，vendor_boot 64 MB，recovery ramdisk 在里面**；`boot.img` 是 v4、**仅有 gzip 内核、无 ramdisk**（已核实，见其 README 第 9 节）。
- Barley-Project：原厂 `boot-debug.img` 是 "v4 with a gzip-compressed kernel + **the generic Android ramdisk (3022350 bytes)**"（已核实）。
→ 两者矛盾。**在设备上一条命令即可定论**：
```sh
adb shell su -c 'ls -l /dev/block/by-name/ | grep -E "boot|init_boot"'
adb shell getprop ro.boot.hardware          # 期望 barley
adb shell su -c 'ls -l /dev/block/by-name/vendor_boot*'
# 以及看 SukiSU 实际改了哪个：
adb shell su -c 'ls -l /data/adb/ksu/ 2>/dev/null; ls /data/adb/ksu/backup 2>/dev/null'
```

### 4.2 `init.recovery` / `modules` 在 vendor_boot 里的位置（一手证据）

`Barley-Project` 的 `vendor-boot/build-vendor-boot.sh` 用 `mkbootimg.py` **显式**构造 vendor_boot v4，fragment 布局如下（**已核实，原文**）：

```
--ramdisk_type 1 --ramdisk_name ''         --vendor_ramdisk_fragment $RD0   # platform fragment (vendor_ramdisk00)
--ramdisk_type 2 --ramdisk_name recovery   --vendor_ramdisk_fragment $RD1   # recovery fragment (vendor_ramdisk01)
--ramdisk_type 1 --ramdisk_name init_boot  --vendor_ramdisk_fragment $fragment02.lz4   # 自己加的第三段
--header_version 4 --pagesize 0x1000 --base 0 --kernel_offset 0x40080000 \
--ramdisk_offset 0x47c80000 --tags_offset 0x4bc80000 --dtb_offset 0x4bc80000 \
--vendor_cmdline '<cmdline>' --dtb <dtb_table.bin> --vendor_bootconfig <bootconfig>
truncate -s 67108864 vendor_boot.raw     # 64 MiB
```
- **`ramdisk_type`**：`1` = platform，`2` = recovery，`3` = dlkm。
- **你机器上厂商 DLKM 的位置**：README 原文 "`vendor modules in our fragment are copies of the ones in the platform fragment`" → 即 **`vendor_ramdisk00`（type=1, name=''）里的 `/lib/modules/*.ko`**。（已核实）
- `first_stage_ramdisk`：**这个目录名在 vendor_boot 的 fragment 里不是固定路径**——它是 boot ramdisk 里"first stage 判定的 flag 文件"（`/first_stage_ramdisk`）以及 `init.recovery.<hw>.rc` 的约定。vendor_boot 的 fragment 直接 cpio 解出来就是 `/` 根（`/lib/modules`、`/etc/init`、`/init.recovery.*.rc` 都在顶层）。→ 不要去找 `first_stage_ramdisk/` 这个目录，**不存在**。
- `modules.load` / `modules.load.recovery`（`init` 读的模块清单）就在 fragment 的 `/lib/modules/` 下。KernelSU 的 `--block-module` 帮助文本也印证："blocking one that **first stage init loads (`modules.load` / `modules.load.recovery`)** … can bootloop the device"。（已核实）

### 4.3 你要动手时的可执行配方（推荐先做只读复现）

```bash
# 0) 备份三件套
adb pull … ; mtkclient: mtk r boot_a boot_a.img / mtk r vendor_boot_a vendor_boot_a.img / vbmeta_a
sha256sum boot_a.img vendor_boot_a.img

# 1) 只读解包（AOSP mkbootimg 工具，`third_party/mkbootimg/` 可直接用）
python3 mkbootimg/unpack_bootimg.py --boot_img boot_a.img --out out/boot_a        --format=mkbootimg
python3 mkbootimg/unpack_bootimg.py --boot_img vendor_boot_a.img --out out/vb_a   --format=mkbootimg
# 输出会列出 header version / page size / vendor ramdisk total size / fragments
# vendor_boot 会产出 vendor_ramdisk00 / 01 / 02 …

# 2) 看 fragment 类型与内容
for f in out/vb_a/vendor_ramdisk*; do echo "== $f"; lz4 -dc "$f" 2>/dev/null | cpio -itv 2>/dev/null | head -20; done

# 3) 空运行重打包验证（不刷）
python3 mkbootimg/mkbootimg.py --header_version 4 \
  --kernel out/boot_a/kernel --ramdisk out/boot_a/ramdisk \
  -o repack_boot.img
cmp <(head -c $(stat -c%s repack_boot.img) boot_a.img) repack_boot.img && echo "byte-identical 前缀一致"
```
（上述命令的参数来自 Barley-Project `build-boot.sh` 的实测用法：`--header_version 4` + `--kernel` + `--ramdisk`；如果原厂 boot 确实 `ramdisk_size=0`，`unpack_bootimg` 不会产出 ramdisk 文件，此时 `--ramdisk` 传空/省略。）

---

## 5. 换基线 vs 补丁原厂：两条路线的取舍（Lead 特别要求）

| 维度 | **路线 A：换基线**（用 `kernel/common` 某 commit + 出厂 defconfig 重编） | **路线 B：补丁原厂**（在 6.6.118 原厂树上改） |
|---|---|---|
| 前提 | 原厂源码**必须到手**——而 **Lenovo GPLCC issue #6 至今 open、无答复**（§1.2） | 同样需要原厂源码；**没有源码就两条路都走不通** |
| 实际可行的近似做法 | 用 `kernel/common` 的 **`android15-6.6.118_r00`**（commit `5650aac9…`）或日期分支 2026-xx，**配合从 boot.img IKCONFIG 提取的出厂 defconfig** 重编 | 若真能拿到 Lenovo 包，则直接 `git apply` SUSFS |
| vermagic 复现 | 可控（§1.7 三要素 + 手写完整 `CONFIG_LOCALVERSION`） | 天然一致 |
| 厂商模块兼容 | **风险点**：内核内部结构/符号集变了，`Module.symvers` 里厂商 .ko 需要的符号若没导出或 CRC 变了 → 加载失败。Barley-Project 用 `crc-compare.py` 机器校验 15505 vs 7820 个导出（gda vs g94）→ **GKI 下导出集本来就不一致** | 一致 |
| 已有成功先例 | `Barley-Project`：自编内核 + 自建 vendor_boot 上机跑通（**但 SukiSU 变体刷入后连不上，原因未查**） | 无公开先例（因为源码没释放） |
| 工作量 | 中（1 天级） | 中（如果源码到手）/ 不可能（若拿不到） |
| 建议 | ✅ **推荐**：主路线，且**先不加 SUSFS**，先复现一台能开机、CRC 无损的内核 | 作为 A 的"对照实验" |

> 关于 `MT8786` 与 `MT6768/Helio G88` 的关系：两个一手仓库对 SoC 的标注互相矛盾——`lifengqaq` 写 **MT8786**（`tb8786p1_64`），`Barley-Project` 写 **MT6768 / Helio G85**，`nevedoma-zverushka` 写 **"MT6768-family BSP"**。公共数据库（phonesdata / GSMArena / Xiaomi FAQ）把 MT8786 描述为 2×A75@2.0 + 6×A55@1.8 + Mali-G52 MP2 的**平板用 SKU**，与 Helio G88（MT6769H）/G85（MT6769Z）是同一族的改名/分档。（GSMArena/ phonesdata 已核实存在 MT8786 条目；"同一 die"属**【推断】**，**未找到** MTK 官方文档证实）
> **实践结论：SoC 名字不影响内核工作**——因为它是 GKI，DT 和驱动都在 vendor 侧，你只需要 `6.6.118` + 正确的 defconfig。真正影响你的是 **device tree 里的 `qcom,board-id`/`mtk,board-id` 以及 `ro.boot.hardware=barley`**，那些都在原厂镜像里，不用改。

---

## 6. 待办 / 未解决问题（明确列出，不填充）

1. **【必须你实测】** 本机 `uname -r` / `/proc/version` 的确切字符串（决定 §1.7 的 `CONFIG_LOCALVERSION`）。
2. **【必须你实测】** `boot_a.img` 是否真有 ramdisk、是否有 AVB footer（`xxd -s -64`）。
3. **【必须你实测】** `vendor_boot_a.img` 的 fragment 列表与类型（`unpack_bootimg --format=mkbootimg`）。
4. **【未找到可靠来源】** MTK 面向公众的 6.6 android15 GPL 源码入口（`git01.mediatek.com` 需客户账号）。
5. **【未找到可靠来源】** Lenovo 开放源代码中心是否收录 TB330FU（站点本机不可达）。
6. **【未找到可靠来源】** `KSU_SUSFS_HAS_MAGIC_MOUNT` 在 6.6 分支的现状（当前 v2.3.0 无此符号）。
7. **【未找到可靠来源】** 任何"内核级 Zygisk"实现（不存在）。
8. **【需实测】** `avbtool add_hash_footer --algorithm NONE` 在本机 LK 上的效果（lifengqaq 报告可启动并给出完整参数；Barley-Project 报告不需要 footer；以你机器为准）。

---

## 附录：所有引用链接

- https://android.googlesource.com/kernel/common/+refs （已核实）
- https://android.googlesource.com/kernel/common/+/refs/tags/android15-6.6.118_r00/Makefile?format=TEXT （已核实：SUBLEVEL=118）
- https://android.googlesource.com/kernel/mediatek/+refs （已核实：无 6.6 分支）
- https://git.codelinaro.org/clo/la/kernel/mediatek （已核实：branches 39 / tags 34）
- https://baylibre.pages.baylibre.com/mediatek/rita/device/mediatek/mtk-android-12/docs/kernel/kernel-src-org.html （已核实）
- https://source.android.com/docs/setup/build/building-kernels （已核实：build.sh 不支持 Android 14+）
- https://github.com/lenovo/gplcc/issues/6 （已核实：6.6.118 无关——该 issue 是 TB330FU 源码未发布，open）
- https://github.com/lifengqaq/tb330fu-kernelsu-droidspaces （已核实，README 仅 `README.zh.md`）
- https://github.com/Barley-Project/tb330fu-ubuntu （已核实）
- https://github.com/feliksyasnopolski/nevedoma-zverushka （已核实，默认分支 `master`）
- https://github.com/threadexio/tb330fu （已核实，默认分支 `master`）
- https://github.com/R0rt1z2/lkpatcher （已核实）
- https://gitlab.com/simonpunk/susfs4ksu/-/tree/gki-android15-6.6 （429，未直读）
- https://github.com/Zarathos30/susfs4ksu/tree/gki-android15-6.6-dev （已核实，镜像）
- https://github.com/inforcqb/susfs4ksu-for-lkm （已核实）
- https://github.com/SukiSU-Ultra/SukiSU-Ultra/blob/main/docs/guide/how-to-integrate.md （已核实）
- https://github.com/tiann/KernelSU/blob/main/userspace/ksud/src/boot_patch.rs （已核实）
- https://github.com/Dr-TSNG/ZygiskNext （已核实）
- https://github.com/PerformanC/ReZygisk （已核实）
- https://github.com/JingMatrix/NeoZygisk （已核实，默认分支 `master`）

**原文存档目录**：`D:\DSH-Workspace\MTK-Kernel-Port\research\raw\`（README、脚本、补丁、Kconfig、JSON 接口返回）

---

### 附录 A：`lifengqaq` 仓库的完整 CI 参数（一手，可直接照抄骨架）

以下全部摘自 `https://github.com/lifengqaq/tb330fu-kernelsu-droidspaces`（`.github/workflows/build.yml`、`config/*`，**均已存档并逐行读过**）：

**编译命令（原文）**
```yaml
env:
  LOCALVERSION: ''          # 阻止 setlocalversion 额外附加 '+'
run: make -C kernel O="$KERNEL_OUT" ARCH=arm64 LLVM=1 LLVM_IAS=1 LOCALVERSION= -j"$(nproc)" Image.gz
```

**defconfig 里的关键条目（原文）**
```
CONFIG_LOCALVERSION="-android15-8-g94c1a24cabd5-ab12293704-4k"
# CONFIG_LOCALVERSION_AUTO is not set
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
CONFIG_MODVERSIONS=y
CONFIG_KALLSYMS_ALL=y
CONFIG_RFKILL=m
```
（注意 `CONFIG_IKCONFIG_PROC=y` → **你机器上极可能有 `/proc/config.gz`，取证会非常容易**）

**KernelSU 依赖 fragment（原文，全部内容）**
```
CONFIG_KPROBES=y
CONFIG_HAVE_KPROBES=y
CONFIG_KRETPROBES=y
CONFIG_KPROBE_EVENTS=y
CONFIG_FTRACE=y
CONFIG_TRACEPOINTS=y
CONFIG_FTRACE_SYSCALLS=y     # ← v3.3.x syscall tracepoint dispatcher 硬依赖，官方 config 里是 not set
CONFIG_KALLSYMS=y
CONFIG_KALLSYMS_ALL=y
CONFIG_EXT4_FS=y
```

**产物处理**：`Image.gz` → `mkboot_v4.py`（纯 Python，无依赖）→ 32 MiB → `avbtool add_hash_footer` → AnyKernel3 zip。

**`mkboot_v4.py` 的关键常量（原文，我读过全文）**：`MAGIC=b'ANDROID!'`、`HDR_VERSION=4`、`HDR_SIZE=1584`、`HEADER_PAGES=4096`、`PAGE=4096`；字段偏移 `kernel_size@8 / ramdisk_size@12 / os_version@16 / header_size@20 / reserved[4]@24 / header_version@40 / cmdline@44(len 1536) / signature_size@1580`。它能 `info` 出 `kernel_is_gzip` 与尾部是否还有非零数据（AVB vbmeta/footer）——**这是你判断 boot 形态最省事的工具**。

**（对 6.6.118 的替换清单）**
| 位置 | 6.6.30 原值 | 6.6.118 要改成 |
|---|---|---|
| `CONFIG_LOCALVERSION` | `-android15-8-g94c1a24cabd5-ab12293704-4k` | 你在 §1.7 取证到的串 |
| `CONFIG_LOCALVERSION_AUTO` | 未设 | 未设（保持） |
| 源码 commit | `94c1a24cabd5` | `android15-6.6.118_r00` = `5650aac9411fe9b3462b10d9f038054e94f86cbe`（已核实），或你取证到的 `-g<sha>` 对应 commit |
| defconfig | `barley_defconfig`（6.6.30 的 IKCONFIG 提取） | 用你 `boot_a.img` 提取的 IKCONFIG |
| AVB `--prop` 指纹 | `S2001962_250417_ROW` | `getprop ro.build.fingerprint` |
| `avbtool --salt` | 固定 hex | 可沿用 |

**⚠️ 一个必须知道的风险（Barley-Project 的实测教训）**：作者把 `CONFIG_KSU=y` 直接编进内核后，"builds … but the tablet was unreachable after booting it from boot_a; … **the cause was not found**. Do not flash this without a way to capture an early boot log."（已核实）
→ 说明：**KSU 编进内核（y）在原厂 config 基础上的启动成功率并不确定**。你现在 LKM 模式能跑，说明 `kernelsu.ko`（module 形态）路径已被验证；**换内核后如果 KSU 无法用，至少可以退回 LKM 模式**（`ksu_config` 的 spoof_release 会跟着新 vermagic 走，需要重新打 boot）。**所以内核定制与 root 方案解耦，是降低风险的关键设计。**
