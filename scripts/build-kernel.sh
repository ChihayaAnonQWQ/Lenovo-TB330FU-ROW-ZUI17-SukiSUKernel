#!/usr/bin/env bash
# =============================================================================
# build-kernel.sh —— TB330FU 内核一键构建
#
# 把「源码 → config → 编译 → CRC 硬门禁 → 打包 boot 镜像」固化下来。
#
# 关键设计：
#   1. CRC 门禁是**硬失败**：只要厂商模块需要的任何一个符号 CRC 不一致，
#      立即终止且不产出镜像。历史教训：CRC 不一致的内核能编译通过、
#      能刷进去，但会在 init 阶段 panic（详见 docs/device-facts.md §7sexies）。
#   2. 幂等：源码/工具链/out 目录可复用，重复执行只做增量。
#   3. profile 化：baseline / ksu / susfs / droidspaces。
#
# 用法：
#   ./build-kernel.sh --profile ksu
#   ./build-kernel.sh --profile susfs --jobs 16
#   ./build-kernel.sh --profile ksu --package-only     # 只重新打包镜像
#
# 退出码：
#   0  成功（构建 + CRC 通过 + 镜像已产出）
#   2  前置检查失败
#   3  源码/工具链准备失败
#   4  配置失败
#   5  编译失败
#   6  CRC 门禁未通过   ← 最重要
#   7  打包失败
# =============================================================================
set -Eeuo pipefail

# -----------------------------------------------------------------------------
# 默认值（可用环境变量覆盖）
# -----------------------------------------------------------------------------
: "${TB_WORK:=$HOME/tb330fu}"
: "${TB_REF:=$TB_WORK/ref}"
: "${TB_BACKUP:=/mnt/d/TB330FU备份}"
: "${TB_CLANG:=$TB_WORK/tools/clang-r510928}"
: "${TB_AVBTOOL:=$TB_WORK/tools/avb/avbtool.py}"
: "${TB_KSRC:=$TB_WORK/kernel}"
: "${TB_SUKISU:=$TB_WORK/SukiSU}"
: "${TB_SUKISU_BUILTIN:=$TB_WORK/SukiSU-builtin}"
: "${TB_KERNEL_COMMIT:=android15-6.6.118_r00}"
: "${TB_KERNEL_URL:=https://android.googlesource.com/kernel/common}"
: "${TB_CLANG_URL:=https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/refs/heads/android15-release/clang-r510928.tar.gz}"
: "${TB_BUILD_USER:=kleaf}"
: "${TB_BUILD_HOST:=build-host}"
: "${TB_BUILD_TIMESTAMP:=Sat Jan 3 21:50:53 UTC 2026}"
# 目标 vermagic 后缀（仅影响 uname -r 观感，不影响模块加载——见 device-facts §7bis）
: "${TB_LOCALVERSION:=-android15-8-gf9c84f40b6ba-ab14657223-4k}"
: "${TB_BOOT_PART_SIZE:=33554432}"
: "${TB_BOOT_NAME:=boot}"
: "${TB_FINGERPRINT:=Lenovo/TB330FU_PRC/TB330FU:15/AP3A.240905.015.A2/S2003138_261010_ROW:user/release-keys}"
: "${TB_SECURITY_PATCH:=2026-10-05}"
: "${TB_ANDROID_RELEASE:=15}"

PROFILE=""
JOBS="$(nproc 2>/dev/null || echo 4)"
OUT_DIR=""
SKIP_FETCH=0
NO_CRC_GATE=0
PACKAGE_ONLY=0
WITH_KPM=0
BOOT_BASE=""
DRY_RUN=0

# -----------------------------------------------------------------------------
# 日志
# -----------------------------------------------------------------------------
_ts() { date +%H:%M:%S; }
log()  { printf '\033[1;36m[%s] %s\033[0m\n' "$(_ts)" "$*"; }
ok()   { printf '\033[1;32m[%s] ✓ %s\033[0m\n' "$(_ts)" "$*"; }
warn() { printf '\033[1;33m[%s] ! %s\033[0m\n' "$(_ts)" "$*" >&2; }
err()  { printf '\033[1;31m[%s] ✗ %s\033[0m\n' "$(_ts)" "$*" >&2; }
die()  { err "$*"; exit "${2:-1}"; }

trap 'err "第 $LINENO 行失败（命令: $BASH_COMMAND）"' ERR

usage() {
  cat <<'EOF'
build-kernel.sh —— TB330FU 内核一键构建

必选:
  --profile <name>        baseline | ksu | susfs | droidspaces | ksu+droidspaces | susfs+droidspaces

可选:
  --out-dir <dir>         构建目录（默认 $TB_WORK/out/<profile>）
  --jobs <n>              并行度（默认 nproc）
  --boot-base <img>       boot 骨架镜像（默认自动找 $TB_BACKUP/boot_a.bin）
  --skip-fetch            跳过源码/工具链拉取
  --package-only          只做「打包 boot 镜像」这一步（复用已有 Image）
  --kpm                   启用 SukiSU KPM（自动编 kpimg 并用 kptools 注入内核）
  --no-crc-gate           跳过 CRC 门禁【危险，仅供调试】
  --dry-run               只打印将执行的步骤
  -h, --help

环境变量（均有默认值）:
  TB_WORK TB_REF TB_BACKUP TB_CLANG TB_AVBTOOL TB_KSRC TB_SUKISU
  TB_KERNEL_COMMIT TB_KERNEL_URL TB_LOCALVERSION

示例:
  ./build-kernel.sh --profile ksu
  ./build-kernel.sh --profile susfs --jobs 16
  sudo apt install bc bison flex libssl-dev libelf-dev pahole lz4 cpio zstd git curl make
EOF
}

# -----------------------------------------------------------------------------
# 参数解析
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)      PROFILE="${2:?}"; shift 2 ;;
    --out-dir)      OUT_DIR="${2:?}"; shift 2 ;;
    --jobs)         JOBS="${2:?}"; shift 2 ;;
    --boot-base)    BOOT_BASE="${2:?}"; shift 2 ;;
    --skip-fetch)   SKIP_FETCH=1; shift ;;
    --package-only) PACKAGE_ONLY=1; shift ;;
    --kpm)          WITH_KPM=1; shift ;;
    --no-crc-gate)  NO_CRC_GATE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) die "未知参数: $1（--help 查看用法）" 2 ;;
  esac
done

[ -n "$PROFILE" ] || { usage; die "缺少 --profile" 2; }

case "$PROFILE" in
  baseline|ksu|susfs|droidspaces|ksu+droidspaces|susfs+droidspaces) ;;
  *) die "未知 profile: $PROFILE" 2 ;;
esac

: "${OUT_DIR:=$TB_WORK/out/$PROFILE}"
LOG_DIR="$TB_WORK/logs"
BUILD_LOG="$LOG_DIR/build-$PROFILE.log"
mkdir -p "$LOG_DIR" "$OUT_DIR"

say_step() { log "==== $* ===="; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then echo "  [dry-run] $*"; return 0; fi
  "$@"
}

# -----------------------------------------------------------------------------
# 1. 前置检查
# -----------------------------------------------------------------------------
preflight() {
  say_step "1/8 前置检查"
  local missing=()
  for t in make git curl gzip python3 bc flex bison pahole lz4 cpio zstd depmod; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    err "缺少依赖: ${missing[*]}"
    err "Debian/Ubuntu:  sudo apt install -y build-essential bc bison flex libssl-dev libelf-dev pahole lz4 cpio zstd git curl"
    err "Arch:            sudo pacman -S --needed base-devel bc pahole lz4 cpio zstd git curl"
    exit 2
  fi
  ok "构建依赖齐全"

  [ -f "$TB_REF/vendor-crc.json" ] || die "缺少 $TB_REF/vendor-crc.json（CRC 门禁的数据源）" 2
  ok "CRC 基线表: $TB_REF/vendor-crc.json ($(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$TB_REF/vendor-crc.json") 个符号)"

  [ -f "$TB_REF/tb330fu-6.6.118_defconfig" ] || die "缺少 $TB_REF/tb330fu-6.6.118_defconfig" 2
  ok "设备原始 defconfig 就绪"

  # boot 骨架
  if [ -z "$BOOT_BASE" ]; then
    for c in "$TB_BACKUP/boot_a.bin" "$TB_BACKUP/boot_a.img"; do
      [ -f "$c" ] && BOOT_BASE="$c" && break
    done
  fi
  if [ "$PACKAGE_ONLY" -eq 1 ] || [ ! -z "$BOOT_BASE" ]; then
    [ -n "$BOOT_BASE" ] && [ -f "$BOOT_BASE" ] || die "找不到 boot 骨架镜像（--boot-base 指定）" 2
    ok "boot 骨架: $BOOT_BASE ($(stat -c%s "$BOOT_BASE") 字节)"
  fi

  [ -f "$TB_AVBTOOL" ] || warn "avbtool 不存在: $TB_AVBTOOL（打包阶段会失败）"
  ok "前置检查通过"
}

# -----------------------------------------------------------------------------
# 2. 工具链
# -----------------------------------------------------------------------------
ensure_toolchain() {
  say_step "2/8 工具链"
  if [ -x "$TB_CLANG/bin/clang" ]; then
    ok "已存在: $("$TB_CLANG/bin/clang" --version | head -1)"
    return 0
  fi
  [ "$SKIP_FETCH" -eq 1 ] && die "工具链缺失且指定了 --skip-fetch" 3
  log "下载 clang-r510928（约 865 MB）…"
  mkdir -p "$(dirname "$TB_CLANG")"
  run curl -4 -fL --retry 3 --retry-delay 5 --connect-timeout 20 \
      -o "$TB_CLANG.tar.gz" "$TB_CLANG_URL"
  mkdir -p "$TB_CLANG"
  run tar -xzf "$TB_CLANG.tar.gz" -C "$TB_CLANG"
  [ -x "$TB_CLANG/bin/clang" ] || die "工具链解压后仍找不到 clang" 3
  ok "工具链就绪: $("$TB_CLANG/bin/clang" --version | head -1)"
}

# -----------------------------------------------------------------------------
# 3. 源码
# -----------------------------------------------------------------------------
ensure_source() {
  say_step "3/8 内核源码"
  if [ -d "$TB_KSRC/.git" ] && [ -f "$TB_KSRC/Makefile" ]; then
    ok "已存在源码树: $(git -C "$TB_KSRC" rev-parse --short HEAD 2>/dev/null || echo '?')"
  else
    [ "$SKIP_FETCH" -eq 1 ] && die "源码缺失且指定了 --skip-fetch" 3
    log "拉取 kernel/common @ $TB_KERNEL_COMMIT …"
    mkdir -p "$TB_KSRC"
    run git -C "$TB_KSRC" init -q .
    run git -C "$TB_KSRC" fetch --depth=1 "$TB_KERNEL_URL" "$TB_KERNEL_COMMIT"
    run git -C "$TB_KSRC" checkout -q --detach FETCH_HEAD
    ok "源码就绪"
  fi
  # 版本自检
  local v p s
  v=$(sed -n 's/^VERSION = //p' "$TB_KSRC/Makefile")
  p=$(sed -n 's/^PATCHLEVEL = //p' "$TB_KSRC/Makefile")
  s=$(sed -n 's/^SUBLEVEL = //p' "$TB_KSRC/Makefile")
  [ "$v.$p.$s" = "6.6.118" ] || warn "源码版本为 $v.$p.$s（预期 6.6.118）"
  ok "源码版本: $v.$p.$s"

  # 必要补丁：libbpf const-correctness（clang r510928 -Werror）
  if ! grep -q 'next_path = (char \*)strchr' "$TB_KSRC/tools/lib/bpf/libbpf.c" 2>/dev/null; then
    log "应用 libbpf const-correctness 补丁"
    run sed -i "s|^\([[:space:]]*\)next_path = strchr(s, ':');|\1next_path = (char *)strchr(s, ':');|" \
        "$TB_KSRC/tools/lib/bpf/libbpf.c"
  fi
  grep -q 'next_path = (char \*)strchr' "$TB_KSRC/tools/lib/bpf/libbpf.c" \
    && ok "libbpf 补丁已就位" || die "libbpf 补丁应用失败" 3
}

# -----------------------------------------------------------------------------
# 4. profile 专属：源码级集成
# -----------------------------------------------------------------------------
integrate_sukisu() {
  # $1 = 分支：main（kprobes+syscall dispatcher，无源码 hook）或 builtin（带 SUSFS 扩展）
  local branch="${1:-main}"
  local src
  case "$branch" in
    main)    src="$TB_SUKISU";;
    builtin) src="$TB_SUKISU_BUILTIN";;
    *) die "未知 SukiSU 分支: $branch" 3 ;;
  esac
  say_step "4a/8 集成 SukiSU（内置，分支 $branch）"

  if [ ! -d "$src/kernel" ]; then
    [ "$SKIP_FETCH" -eq 1 ] && die "SukiSU($branch) 源码缺失" 3
    log "克隆 SukiSU Ultra $branch 分支…"
    run git clone --depth=1 -b "$branch" https://github.com/SukiSU-Ultra/SukiSU-Ultra "$src"
  fi
  ok "SukiSU($branch): $(git -C "$src" rev-parse --short HEAD 2>/dev/null || echo '?')"

  # Kconfig: config KSU 的 default y -> default n（否则 olddefconfig 会默认打开）
  if grep -qE '^[[:space:]]+default y[[:space:]]*$' "$src/kernel/Kconfig"; then
    python3 - "$src/kernel/Kconfig" <<'PY'
import re, sys
from pathlib import Path
p = Path(sys.argv[1]); s = p.read_text(); out=[]; in_ksu=False
for ln in s.splitlines():
    if re.match(r'^config KSU\s*$', ln.strip()):
        in_ksu = True; out.append(ln); continue
    if in_ksu and ln.strip().startswith('config '):
        in_ksu = False
    if in_ksu and re.match(r'^\s+default y\s*$', ln):
        out.append('\tdefault n'); continue
    out.append(ln)
p.write_text('\n'.join(out) + '\n')
PY
    ok "Kconfig: config KSU default y -> n"
  fi

  # builtin 分支原生缺 avc_spoof（main 分支有 feature/avc_spoof.c）。
  # 为了同时拿到 SUSFS(builtin) + AVC 伪装，这里把 main 的实现移植过来。
  # 补丁同时修了两个 builtin 特有的坑：
  #   1) include "runtime/ksud_boot.h" -> "runtime/ksud.h"（builtin 无前者）
  #   2) policy/feature.c 的 forced_mask/forced_pending 与 extension.c 同名，
  #      unity build 下会冲突（KSU_FEATURE_MAX 变大后类型不一致）-> 改名 feature_forced_*
  if [ "$branch" = "builtin" ]; then
    local avcpatch="$TB_WORK/patches/0001-sukisu-builtin-add-avc-spoof.patch"
    if grep -q 'KSU_FEATURE_AVC_SPOOF' "$src/kernel/include/uapi/feature.h" 2>/dev/null; then
      ok "avc_spoof 移植补丁已应用，跳过"
    elif [ -f "$avcpatch" ]; then
      ( cd "$src" && patch -p1 --no-backup-if-mismatch -i "$avcpatch" ) 2>&1 | tail -5
      grep -q 'KSU_FEATURE_AVC_SPOOF' "$src/kernel/include/uapi/feature.h" \
        && ok "avc_spoof 已移植进来（builtin 分支原生没有）" \
        || die "avc_spoof 补丁应用失败" 3
    else
      warn "缺少 $avcpatch —— avc_spoof 将不可用"
    fi
  fi

  # 软链 + Makefile/Kconfig 挂载（幂等）
  run ln -sfn "$src/kernel" "$TB_KSRC/drivers/kernelsu"
  grep -q 'CONFIG_KSU.*kernelsu/' "$TB_KSRC/drivers/Makefile" 2>/dev/null || \
    run bash -c "printf '\nobj-\$(CONFIG_KSU) += kernelsu/\n' >> '$TB_KSRC/drivers/Makefile'"
  grep -q 'drivers/kernelsu/Kconfig' "$TB_KSRC/drivers/Kconfig" || \
    run sed -i '/^endmenu/i source "drivers/kernelsu/Kconfig"' "$TB_KSRC/drivers/Kconfig"
  ok "drivers/kernelsu -> $branch 挂载完成"
}

integrate_susfs() {
  say_step "4b/8 集成 SUSFS（内核侧）"
  local S="$TB_WORK/susfs4ksu"
  local PATCH="$S/kernel_patches/50_add_susfs_in_gki-android15-6.6.patch"

  if [ ! -f "$PATCH" ]; then
    [ "$SKIP_FETCH" -eq 1 ] && die "SUSFS 补丁缺失" 3
    log "克隆 susfs4ksu…"
    run git clone --depth=1 -b gki-android15-6.6-dev https://github.com/Zarathos30/susfs4ksu "$S"
  fi
  ok "SUSFS 补丁: $(basename "$PATCH")"

  # 幂等：已经打过就跳过
  if grep -q 'susfs' "$TB_KSRC/fs/Makefile" 2>/dev/null; then
    ok "SUSFS 内核补丁已应用（fs/Makefile 已含 susfs.o），跳过"
    return 0
  fi

  # 新文件
  run cp "$S/kernel_patches/fs/susfs.c"                     "$TB_KSRC/fs/"
  run cp "$S/kernel_patches/include/linux/susfs.h"          "$TB_KSRC/include/linux/"
  run cp "$S/kernel_patches/include/linux/susfs_def.h"      "$TB_KSRC/include/linux/"

  # 补丁：git apply 会整包拒绝，GNU patch 的 fuzz 能干净落位（实测 0 .rej）
  log "应用 50_add_susfs 补丁（fuzz=3）"
  ( cd "$TB_KSRC" && patch -p1 --fuzz=3 --no-backup-if-mismatch -i "$PATCH" ) 2>&1 | tail -6
  local rej
  rej=$(find "$TB_KSRC" -name '*.rej' 2>/dev/null | wc -l)
  if [ "$rej" -ne 0 ]; then
    find "$TB_KSRC" -name '*.rej' >&2
    die "SUSFS 补丁产生 $rej 个 .rej 文件" 3
  fi
  ok "SUSFS 补丁应用完成（0 个 .rej）"
  ok "  fs/namei.c: $(grep -c susfs "$TB_KSRC/fs/namei.c") 处 / fs/namespace.c: $(grep -c susfs "$TB_KSRC/fs/namespace.c") 处 / kernel/sys.c: $(grep -c susfs "$TB_KSRC/kernel/sys.c") 处"
}

integrate_droidspaces() {
  say_step "4c/8 集成 Droidspaces（SYSVIPC kABI 补丁 + GKI 安全配置）"
  local DS="$TB_WORK/Droidspaces"
  local PDIR="$DS/Documentation/resources/kernel-patches/GKI/below-kernel-6.12"
  local PATCH="$PDIR/001.GKI-below-6.12-fix_sysvipc_kabi_6_7_8.patch"

  if [ ! -f "$PATCH" ]; then
    [ "$SKIP_FETCH" -eq 1 ] && die "Droidspaces 补丁缺失" 3
    log "克隆 Droidspaces-OSS…"
    run git clone --depth=1 https://github.com/ravindu644/Droidspaces-OSS "$DS"
  fi
  ok "kABI 补丁: $(basename "$PATCH")"

  # 幂等
  if grep -q 'ANDROID_KABI_USE(6, struct sysv_sem' "$TB_KSRC/include/linux/sched.h" 2>/dev/null; then
    ok "SYSVIPC kABI 补丁已应用，跳过"
  else
    # 该补丁把 task_struct 的 sysvsem/sysvshm 搬进 ANDROID_KABI_RESERVE(6~8)。
    # _ANDROID_KABI_REPLACE 在 __GENKSYMS__ 下展开为原始 reserve，故 genksyms
    # 看到的 task_struct 与官方一致 -> 导出符号 CRC 不变。
    # （实测：开 SYSVIPC/IPC_NS/POSIX_MQUEUE/USER_NS 后 CRC 仍 2321/2321 一致）
    log "应用 SYSVIPC kABI 补丁（6_7_8 槽位）"
    ( cd "$TB_KSRC" && patch -p1 --fuzz=3 --no-backup-if-mismatch -i "$PATCH" ) 2>&1 | tail -4
    local rej
    rej=$(find "$TB_KSRC" -name '*.rej' 2>/dev/null | wc -l)
    [ "$rej" -eq 0 ] || { find "$TB_KSRC" -name '*.rej' >&2; die "kABI 补丁产生 $rej 个 .rej" 3; }
    ok "kABI 补丁应用完成（0 个 .rej）"
  fi

  # profile fragment（由 configure 合并）
  cat > "$OUT_DIR/profile.fragment" <<'EOF'

# --- profile: droidspaces (GKI 安全集) ---
CONFIG_SYSVIPC=y
CONFIG_POSIX_MQUEUE=y
CONFIG_IPC_NS=y
CONFIG_PID_NS=y
CONFIG_DEVTMPFS=y
CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=y
CONFIG_USER_NS=y
CONFIG_IP6_NF_NAT=y
CONFIG_IP6_NF_TARGET_MASQUERADE=y
CONFIG_MACVLAN=y
CONFIG_NETFILTER_XT_TARGET_REJECT=y
CONFIG_NETFILTER_XT_TARGET_LOG=y
CONFIG_NETFILTER_XT_MATCH_RECENT=y
CONFIG_IP_SET=y
CONFIG_IP_SET_HASH_IP=y
CONFIG_IP_SET_HASH_NET=y
CONFIG_NETFILTER_XT_SET=y
CONFIG_TMPFS_POSIX_ACL=y
CONFIG_TMPFS_XATTR=y
EOF
  ok "profile.fragment 已写入（19 项）"
  warn "刻意不启用 CONFIG_CFS_BANDWIDTH / CONFIG_CGROUP_PIDS —— 改 scheduler/cgroup 结构，无 kABI 补丁可救"
}

# -----------------------------------------------------------------------------
# 5. abi_symbollist.raw（CONFIG_UNUSED_KSYMS_WHITELIST 指向它）
# -----------------------------------------------------------------------------
gen_abi_symbollist() {
  say_step "5/8 生成 abi_symbollist.raw"
  local out="$OUT_DIR/abi_symbollist.raw"
  # ★ 格式极其重要：CONFIG_UNUSED_KSYMS_WHITELIST 要的是**裸符号名，一行一个**，
  #   不能带 [abi_symbol_list] 表头、不能有缩进。
  #   带表头/缩进 -> TRIM_UNUSED_KSYMS 认不出符号 -> 大量导出被裁掉
  #   -> 厂商 .ko 因 Unknown symbol 拒载 -> init panic。
  #   （[abi_symbol_list] 那套只给 gen_gki_modules_headers.sh 用；
  #     本方案关闭了 MODULE_SIG_PROTECT，该消费方已不存在。）
  # ★ 来源优先级：ref/abi_symbollist.known-good（已验证可启动构建的白名单）
  #   > android/abi_gki_aarch64*
  #   历史教训：从 ABI 文件推导曾在相同输入下产出 4106/4102 两种结果，
  #   导致 usb_gadget_frame_number 等符号被裁。钉死基准才确定性。
  local base="$TB_REF/abi_symbollist.known-good"
  local gen="$TB_WORK/scripts/gen-abi-symbollist.py"
  [ -f "$gen" ] || die "缺少 $gen" 4
  # 用独立脚本文件而非 heredoc：python 3.14 在 heredoc+set -e 环境下
  # 曾偶发"打印完结果后于退出时段错误"（SIGSEGV / rc=139）
  python3 "$gen" --kernel "$TB_KSRC" --crc "$TB_REF/vendor-crc.json" \
      --base "$base" --out "$out" || die "生成 abi_symbollist.raw 失败" 4
  [ -s "$out" ] || die "abi_symbollist.raw 生成失败" 4
  if grep -qE '^\[|^[[:space:]]' "$out"; then
    die "abi_symbollist.raw 格式错误（含表头或缩进）—— 会导致 TRIM 裁掉全部导出" 4
  fi
  ok "$(wc -l < "$out") 个符号（裸格式，格式自检通过）-> $out"
}

# -----------------------------------------------------------------------------
# 6. 配置
# -----------------------------------------------------------------------------
configure() {
  say_step "6/8 配置"
  local cfg="$OUT_DIR/.config"
  cp "$TB_REF/tb330fu-6.6.118_defconfig" "$cfg"

  # 基线强制项（历史教训，见 device-facts.md）
  #  1) UT_S_RELEASE 观感（不影响模块加载，但便于识别）
  sed -i "s|^CONFIG_LOCALVERSION=.*|CONFIG_LOCALVERSION=\"$TB_LOCALVERSION\"|" "$cfg"
  sed -i 's|^CONFIG_LOCALVERSION_AUTO=y|# CONFIG_LOCALVERSION_AUTO is not set|' "$cfg"
  #  2) 必须关闭：否则自编内核无法验签 Google 签名的 system_dlkm 模块 → rfkill 崩链 → WiFi 挂
  sed -i 's|^CONFIG_MODULE_SIG_PROTECT=y|# CONFIG_MODULE_SIG_PROTECT is not set|' "$cfg"
  #  3) 必须保持关闭：开启会改变 trace 符号 CRC → aee_aed.ko 拒载 → init panic
  sed -i '/^CONFIG_FTRACE_SYSCALLS=/d; /^# CONFIG_FTRACE_SYSCALLS/d' "$cfg"

  # profile 追加：三个维度正交组合，避免 "susfs+droidspaces" 这类组合漏项
  #   维度1 KSU      profile 名含 ksu 或 susfs
  #   维度2 SUSFS    profile 名含 susfs
  #   维度3 fragment  由 integrate_droidspaces 写入（droidspaces 相关）
  if [[ "$PROFILE" == *ksu* ]] || [[ "$PROFILE" == *susfs* ]]; then
    { echo ""; echo "# --- profile: KernelSU (builtin) ---"; echo "CONFIG_KSU=y"; } >> "$cfg"
  fi
  # KPM 是可组合能力：--kpm 时对任意带 KSU 的 profile 生效
  if [ "$WITH_KPM" -eq 1 ]; then
    if [[ "$PROFILE" == *ksu* ]] || [[ "$PROFILE" == *susfs* ]]; then
      { echo "# --- option: KPM ---"; echo "CONFIG_KPM=y"; } >> "$cfg"
    else
      die "--kpm 需要 profile 含 ksu 或 susfs（当前: $PROFILE）" 2
    fi
  fi
  if [[ "$PROFILE" == *susfs* ]]; then
    {
      echo "# --- profile: SUSFS v2.3.0 ---"
      for sym in KSU_SUSFS KSU_SUSFS_SUS_PATH KSU_SUSFS_SUS_MOUNT KSU_SUSFS_SUS_KSTAT \
                 KSU_SUSFS_SPOOF_UNAME KSU_SUSFS_ENABLE_LOG \
                 KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS \
                 KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG \
                 KSU_SUSFS_OPEN_REDIRECT KSU_SUSFS_SUS_MAP; do
        echo "CONFIG_${sym}=y"
      done
    } >> "$cfg"
  fi
  if [ -f "$OUT_DIR/profile.fragment" ]; then
    cat "$OUT_DIR/profile.fragment" >> "$cfg"
  fi

  # 去重：同一符号只保留最后一行（避免 defconfig 里的旧值和新追加值并存）
  awk -F= '!/^CONFIG_/ {print; next} {k=$1; last[k]=NR; line[NR]=$0} END {for (i=1;i<=NR;i++) if (line[i] != "" && (!(line[i] ~ /^CONFIG_/) || last[substr(line[i],1,index(line[i],"=")-1)]==i)) print line[i]}' \
      "$cfg" > "$cfg.dedup" && mv "$cfg.dedup" "$cfg"

  export PATH="$TB_CLANG/bin:$PATH"
  export LOCALVERSION=""
  ( cd "$TB_KSRC" && make O="$OUT_DIR" ARCH=arm64 LLVM=1 LLVM_IAS=1 olddefconfig ) \
      >> "$BUILD_LOG" 2>&1 || die "olddefconfig 失败（见 $BUILD_LOG）" 4

  # 自检：关键项
  local bad=0
  for chk in "CONFIG_MODULE_SIG_PROTECT" "CONFIG_FTRACE_SYSCALLS"; do
    if grep -q "^$chk=" "$OUT_DIR/.config"; then
      err "关键项 $chk 被启用（必须关闭）"; bad=1
    fi
  done
  if [[ "$PROFILE" == *ksu* ]] || [[ "$PROFILE" == *susfs* ]]; then
    grep -q '^CONFIG_KSU=y' "$OUT_DIR/.config" || { err "CONFIG_KSU 未生效"; bad=1; }
  fi
  if [ "$WITH_KPM" -eq 1 ]; then
    grep -q '^CONFIG_KPM=y' "$OUT_DIR/.config" || { err "CONFIG_KPM 未生效"; bad=1; }
  fi
  if [[ "$PROFILE" == *susfs* ]]; then
    grep -q '^CONFIG_KSU_SUSFS=y' "$OUT_DIR/.config" || { err "CONFIG_KSU_SUSFS 未生效"; bad=1; }
    [ -f "$TB_KSRC/fs/susfs.c" ] || { err "fs/susfs.c 缺失（SUSFS 补丁未应用）"; bad=1; }
  fi
  [ "$bad" -eq 0 ] || die "配置自检失败" 4

  # abi_symbollist.raw 已由 gen_abi_symbollist 写入 $OUT_DIR（即 make 的 $O），无需再拷贝
  ok "配置就绪: $OUT_DIR/.config"
  grep -E '^CONFIG_(KSU|LOCALVERSION|MODULE_SIG_PROTECT)' "$OUT_DIR/.config" | sed 's/^/    /' || true
}

# -----------------------------------------------------------------------------
# 7. 编译
# -----------------------------------------------------------------------------
build() {
  say_step "7/8 编译（-j$JOBS）"
  export PATH="$TB_CLANG/bin:$PATH"
  export LOCALVERSION=""
  export KBUILD_BUILD_USER="$TB_BUILD_USER"
  export KBUILD_BUILD_HOST="$TB_BUILD_HOST"
  export KBUILD_BUILD_TIMESTAMP="$TB_BUILD_TIMESTAMP"

  local t0; t0=$(date +%s)
  ( cd "$TB_KSRC" && make O="$OUT_DIR" ARCH=arm64 LLVM=1 LLVM_IAS=1 -j"$JOBS" Image modules ) \
      >> "$BUILD_LOG" 2>&1 || {
        err "编译失败，末尾日志："
        tail -30 "$BUILD_LOG" >&2
        die "编译失败（完整日志 $BUILD_LOG）" 5
      }
  local t1; t1=$(date +%s)
  [ -f "$OUT_DIR/arch/arm64/boot/Image" ] || die "编译声称成功但 Image 不存在" 5
  ok "编译完成，用时 $((t1-t0)) 秒，Image $(stat -c%s "$OUT_DIR/arch/arm64/boot/Image") 字节"

  # kernelrelease 自检
  local rel
  rel=$( cd "$TB_KSRC" && make -s O="$OUT_DIR" ARCH=arm64 kernelrelease )
  ok "kernelrelease: $rel"
}

# -----------------------------------------------------------------------------
# 7b. KernelPatch (kpimg) 注入 —— 让 SukiSU 的 KPM 真正可用
# -----------------------------------------------------------------------------
# 背景：SukiSU 的 KPM 只是 KernelPatch 的客户端。ksu_extension_kpm_get() 会调用
# __arm64_sys_truncate 并期待 KernelPatch 的 supercall 返回 ABI v2；没有 KP 就永远
# 探测失败。CONFIG_KPM=y 只编译客户端，不提供 supercall 通道。
#
# 正确做法（不需要导出任何 cred/fs 符号）：
#   1. 用 SukiSU_KernelPatch_patch 编出 kpimg（裸机 payload，靠 kallsyms
#      运行时自己解析符号 —— 这正是 KernelPatch 的设计目标）
#   2. 用 kptools 把 kpimg 注入内核 Image
#   3. 正常打包刷入 -> KP 极早期运行 -> sukisu_kpm_supercall_install() 成功
#
# 踩坑记录：
#   - kpimg 必须**不带 ANDROID=1** 编。SukiSU fork 的 map.c 在 -DANDROID 下会让
#     .setup.map 超过链接脚本的 4KB 硬断言，编译失败。
#   - 必须用 ARM 官方裸机工具链（aarch64-none-elf-），clang 包装会同样超限。
#   - CI 里的 `make hdr kpimg` 是上游遗留，本 fork 没有 hdr 目标。
inject_kpimg() {
  say_step "7b/8 注入 KernelPatch kpimg（KPM 支持）"
  local KP="$TB_WORK/KP-SukiSU"
  local TC="$TB_WORK/tools/arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf"
  local IMG="$OUT_DIR/arch/arm64/boot/Image"

  [ -f "$IMG" ] || die "找不到 $IMG" 5

  if [ ! -d "$KP/kernel" ]; then
    [ "$SKIP_FETCH" -eq 1 ] && die "KP-SukiSU 源码缺失（git clone SukiSU-Ultra/SukiSU_KernelPatch_patch）" 3
    log "克隆 SukiSU_KernelPatch_patch…"
    run git clone --depth=1 https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch "$KP"
  fi

  if [ ! -x "$TC/bin/aarch64-none-elf-gcc" ]; then
    die "缺少 ARM 裸机工具链 $TC
  下载 https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/12.2.rel1/binrel/arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf.tar.xz
  解压到 $TB_WORK/tools/ 即可。" 3
  fi

  # kpimg（绝对不能加 ANDROID=1）
  if [ ! -f "$KP/kernel/kpimg" ]; then
    log "编译 kpimg（ARM 裸机工具链，不带 ANDROID）"
    ( cd "$KP/kernel" && { make clean >/dev/null 2>&1 || true; } && \
      PATH="$TC/bin:$PATH" TARGET_COMPILE=aarch64-none-elf- make -j"$JOBS" ) \
      >> "$BUILD_LOG" 2>&1 || die "kpimg 编译失败（见 $BUILD_LOG）" 3
  fi
  [ -f "$KP/kernel/kpimg" ] || die "kpimg 未生成" 3
  ok "kpimg: $(stat -c%s "$KP/kernel/kpimg") 字节"

  # kptools（Arch 无静态 zlib，去掉 -static）
  if [ ! -x "$KP/tools/kptools" ]; then
    log "编译 kptools"
    ( cd "$KP/tools" && { make clean >/dev/null 2>&1 || true; } && make LDFLAGS="-lz" ) \
      >> "$BUILD_LOG" 2>&1 || die "kptools 编译失败（见 $BUILD_LOG）" 3
  fi
  [ -x "$KP/tools/kptools" ] || die "kptools 未生成" 3
  ok "kptools 就绪"

  local SKEYFILE="$TB_REF/kp-superkey.txt"
  if [ ! -s "$SKEYFILE" ]; then
    head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24 > "$SKEYFILE"
    ok "生成 superkey -> $SKEYFILE"
  fi
  local SKEY; SKEY=$(cat "$SKEYFILE")

  log "注入 kpimg 到 Image"
  "$KP/tools/kptools" -p -i "$IMG" -k "$KP/kernel/kpimg" -s "$SKEY" -o "$OUT_DIR/Image.kp" \
      > "$OUT_DIR/kptools.log" 2>&1 || die "kptools 注入失败（见 $OUT_DIR/kptools.log）" 5
  if ! "$KP/tools/kptools" -l -i "$OUT_DIR/Image.kp" 2>/dev/null | grep -q 'patched=true'; then
    die "注入后校验失败" 5
  fi
  ok "注入成功: Kernel $(stat -c%s "$IMG") -> Image.kp $(stat -c%s "$OUT_DIR/Image.kp")"
  cp "$OUT_DIR/Image.kp" "$OUT_DIR/Image.kp.bak" 2>/dev/null || true
}

# -----------------------------------------------------------------------------
# 8. CRC 硬门禁
# -----------------------------------------------------------------------------
crc_gate() {
  say_step "8a/8 CRC + 覆盖率 硬门禁"
  if [ "$NO_CRC_GATE" -eq 1 ]; then
    warn "已用 --no-crc-gate 跳过（危险：CRC 不一致/导出被裁会在 init 阶段 panic）"
    return 0
  fi
  local symvers="$OUT_DIR/Module.symvers"
  [ -f "$symvers" ] || die "找不到 $symvers，无法校验" 6

  local script="$TB_WORK/scripts/compare-crc.py"
  [ -f "$script" ] || die "找不到 $script" 6

  # ---- 门禁 1：CRC ----
  if ! python3 "$script" "$symvers" "$TB_REF/vendor-crc.json" | tee "$OUT_DIR/crc-gate.txt"; then
    err "════════════════════════════════════════════════════════"
    err " CRC 门禁未通过 —— 拒绝产出镜像"
    err " 有不一致符号，刷入后会在 init 阶段 panic。"
    err " 常见原因：config 中开启了会改变结构体布局的选项"
    err " （如 FTRACE_SYSCALLS / 未走 kABI 补丁的 SYSVIPC / CFI 等）"
    err "════════════════════════════════════════════════════════"
    exit 6
  fi
  ok "CRC 全量一致"

  # ---- 门禁 2：覆盖率（防"导出被 TRIM 裁掉"却 CRC 仍 PASS 的假阳性）----
  # 判据：本次导出集必须是已验证可用构建（ref/reference-exports.txt）的超集。
  local ref="$TB_REF/reference-exports.txt"
  if [ -f "$ref" ]; then
    awk -F'\t' '$1 ~ /^0x/ {print $2}' "$symvers" | sort -u > "$OUT_DIR/exports.txt"
    local lost
    comm -23 "$ref" "$OUT_DIR/exports.txt" > "$OUT_DIR/lost-exports.txt" || true
    lost=$(wc -l < "$OUT_DIR/lost-exports.txt")
    local have refn
    have=$(wc -l < "$OUT_DIR/exports.txt")
    refn=$(wc -l < "$ref")
    if [ "$lost" -ne 0 ]; then
      err "════════════════════════════════════════════════════════"
      err " 覆盖率门禁未通过：有 $lost 个「已验证可用构建里存在」的导出符号被裁掉了"
      err " 导出数 $have < 参考 $refn"
      err " 最可能原因：abi_symbollist.raw 格式错误（含表头/缩进）"
      err "             -> TRIM_UNUSED_KSYMS 认不出 -> 大量导出被裁"
      err " 被裁掉的符号（前 20，完整见 $OUT_DIR/lost-exports.txt）："
      head -n 20 "$OUT_DIR/lost-exports.txt" 2>/dev/null | sed 's/^/     /' >&2 || true
      err "════════════════════════════════════════════════════════"
      exit 6
    fi
    ok "覆盖率检查通过：导出 $have 个（参考 $refn，无丢失）"
  else
    warn "缺少 $ref，跳过覆盖率检查（建议先跑一次 --profile ksu 生成）"
  fi
}

# -----------------------------------------------------------------------------
# 9. 打包 boot 镜像
# -----------------------------------------------------------------------------
package_boot() {
  say_step "8b/8 打包 boot 镜像"
  local img="$OUT_DIR/arch/arm64/boot/Image"
  # 若已注入 kpimg，优先用注入后的镜像
  if [ -f "$OUT_DIR/Image.kp" ]; then
    img="$OUT_DIR/Image.kp"
    ok "使用 KernelPatch 注入后的镜像: Image.kp"
  fi
  [ -f "$img" ] || die "找不到 $img" 7
  [ -n "$BOOT_BASE" ] && [ -f "$BOOT_BASE" ] || die "缺少 boot 骨架镜像" 7
  [ -f "$TB_AVBTOOL" ] || die "缺少 avbtool: $TB_AVBTOOL" 7

  local gz="$OUT_DIR/Image.gz"
  gzip -9 -c "$img" > "$gz"
  ok "Image.gz $(stat -c%s "$gz") 字节"

  local vkr="$TB_WORK/tools/vkrepack.py"
  [ -f "$vkr" ] || die "缺少 vkrepack.py: $vkr" 7

  local outimg="$OUT_DIR/boot_${PROFILE}.img"
  python3 "$vkr" splice "$BOOT_BASE" \
      --kernel "$gz" --partition-size "$TB_BOOT_PART_SIZE" \
      -o "$outimg" --avb none --avbtool "$TB_AVBTOOL" \
      --ro-prop "ro.build.fingerprint=$TB_FINGERPRINT" \
      --ro-prop "ro.build.version.security_patch=$TB_SECURITY_PATCH" \
      --ro-prop "ro.build.version.release=$TB_ANDROID_RELEASE" 2>&1 | tail -8
  [ -f "$outimg" ] || die "打包失败" 7

  python3 "$vkr" verify "$outimg" --partition-size "$TB_BOOT_PART_SIZE" | tail -2
  ok "镜像: $outimg ($(stat -c%s "$outimg") 字节)"
  sha256sum "$outimg" | sed 's/^/    /'
  echo "$outimg"
}

# -----------------------------------------------------------------------------
# 10. 清单
# -----------------------------------------------------------------------------
write_manifest() {
  local mf="$OUT_DIR/MANIFEST.txt"
  local crc_state="UNKNOWN"
  if [ -f "$OUT_DIR/crc-gate.txt" ] && grep -q '结果: PASS' "$OUT_DIR/crc-gate.txt"; then
    crc_state="PASS"
  fi
  {
    echo "# build-kernel.sh manifest"
    echo "profile         : $PROFILE"
    echo "time            : $(date -Iseconds)"
    echo "kernel commit   : $(git -C "$TB_KSRC" rev-parse HEAD 2>/dev/null || echo '?')"
    echo "kernel release  : $(cd "$TB_KSRC" && make -s O="$OUT_DIR" ARCH=arm64 kernelrelease 2>/dev/null || echo '?')"
    echo "clang           : $("$TB_CLANG/bin/clang" --version 2>/dev/null | head -1)"
    echo "out dir         : $OUT_DIR"
    echo "boot base       : ${BOOT_BASE:-<none>}"
    echo "image           : $OUT_DIR/boot_${PROFILE}.img"
    echo "image sha256    : $(sha256sum "$OUT_DIR/boot_${PROFILE}.img" 2>/dev/null | cut -d' ' -f1 || echo '?')"
    echo "crc gate        : $crc_state"
    echo ""
    echo "## 关键 config"
    grep -E '^CONFIG_(KSU|LOCALVERSION|MODULE_SIG_PROTECT|FTRACE_SYSCALLS|SUSFS)' "$OUT_DIR/.config" 2>/dev/null || true
  } > "$mf"
  ok "清单: $mf"
}

# =============================================================================
# 主流程
# =============================================================================
main() {
  log "profile=$PROFILE  out=$OUT_DIR  jobs=$JOBS"
  [ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN 模式：不会真正执行"

  preflight

  if [ "$PACKAGE_ONLY" -eq 0 ]; then
    ensure_toolchain
    ensure_source
    case "$PROFILE" in
      ksu)              integrate_sukisu main ;;
      susfs)            integrate_sukisu builtin; integrate_susfs ;;
      droidspaces)        integrate_sukisu main; integrate_droidspaces ;;
      ksu+droidspaces)    integrate_sukisu main; integrate_droidspaces ;;
      susfs+droidspaces)  integrate_sukisu builtin; integrate_susfs; integrate_droidspaces ;;
    esac
    gen_abi_symbollist
    configure
    build
    # --kpm 时注入 kpimg：只编 CONFIG_KPM 是不够的，SukiSU 的 KPM 需要
    # KernelPatch 提供 supercall 通道（详见 inject_kpimg 的注释）
    if [ "$WITH_KPM" -eq 1 ]; then
      inject_kpimg
    fi
  else
    log "--package-only：跳过源码/工具链/编译"
  fi

  crc_gate
  package_boot
  write_manifest

  ok "全部完成"
}

main "$@"
