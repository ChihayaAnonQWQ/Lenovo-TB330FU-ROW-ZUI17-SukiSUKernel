#!/usr/bin/env bash
# =============================================================================
#  ota-reinject.sh — 联想 TB330FU (MT6768 / Helio G88) 系统 OTA 后自动重新注入 Root
#                    + 重装自定义内核（SukiSU Ultra / LKM 模式 / MTK 双分区注入）
#
#  目标设备：Lenovo TB330FU
#    SoC     : MT6768 (Helio G88)
#    System  : Android 15 / ZUI 17.0.074，A/B 双槽 + 动态分区
#    Kernel  : 6.6.118-android15-8 (GKI)
#
#  注入链路：
#    1) 提取 boot_<slot> / [init_boot_<slot>] / vendor_boot_<slot>
#    2) patch_image() 逐个修补（LKM 模式下 MTK 需要 boot + vendor_boot 双分区）
#    3) fastboot flash 回写
#    4) MTKClient(BROM) 执行 `da vbmeta 3` 关闭 AVB，否则系统静默回滚
#    5) 重启 → 验证 boot_completed / su -c id；失败打印回滚指令 / --rollback
#
#  安全红线（本脚本永远不会做的事）：
#    * 绝不刷 preloader（代码里没有任何 preloader 分支，并有显式拒绝守卫）
#    * 绝不自动解锁 seccfg（仅当显式传入 --unlock，默认关闭）
#    * 绝不触碰 userdata / 全量固件
#    * AVB(da vbmeta) 失败时绝不继续重启
#
#  依赖：bash >= 4.4、coreutils、adb、fastboot、python(3)、mtkclient(mtk.py)
#        可选：xxd / od、sha256sum / shasum、shellcheck（开发期）
# =============================================================================

set -Eeuo pipefail

# -----------------------------------------------------------------------------
# 0a. Bash 版本硬要求（用到 ${var//pat/rep}、数组、mapfile 等 bash 特性）
# -----------------------------------------------------------------------------
if [ -z "${BASH_VERSION:-}" ]; then
  echo "错误：本脚本需要 bash（当前 shell 不是 bash），请用: bash $0 ..." >&2
  exit 3
fi
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 4 ]; }; then
  echo "错误：需要 bash >= 4.4（当前 ${BASH_VERSION}）" >&2
  exit 3
fi

# -----------------------------------------------------------------------------
# 0. 集中配置（全部路径变量集中在这里，改这里即可迁移）
# -----------------------------------------------------------------------------
# 默认工作目录 /work/tb330fu（Linux 主机 / WSL）；可用环境变量或 --workdir 覆盖。
WORKDIR_DEFAULT="${WORKDIR_DEFAULT:-/work/tb330fu}"

# SukiSU Ultra 仓库根目录（包含 bin/ksud 与 tools/boot_patch.sh 的那个目录）
#   SukiSU 两个可调用入口（二选一，auto 模式优先 ksud）：
#     ${SUKISU}/bin/ksud          → ksud boot-patch --lkm [--vendor-boot]
#     ${SUKISU}/tools/boot_patch.sh → Magisk 风格共享脚本
SUKISU="${SUKISU:-/work/sukisu}"

# mtkclient 的 mtk.py；默认取 $MTKCLIENT_TOOL/mtk.py（MTKCLIENT_TOOL 是 mtkclient 仓库根）
MTKCLIENT_TOOL="${MTKCLIENT_TOOL:-${HOME:-/root}/mtkclient}"
MTK_PY_DEFAULT="${MTKCLIENT_TOOL}/mtk.py"

# 修补后端：auto | ksud | boot_patch | custom
#   auto      : 有 ksud 用 ksud，否则用 boot_patch.sh
#   ksud      : ksud boot-patch --lkm / --vendor-boot
#   boot_patch: Magisk 风格共享脚本 tools/boot_patch.sh
#   custom    : 外部自定义命令，模板见 README（占位符 {in} {out} {part}）
PATCH_BACKEND="${PATCH_BACKEND:-auto}"
PATCH_CMD_TEMPLATE="${PATCH_CMD_TEMPLATE:-}"

# --- SukiSU / LKM 参数（来自源码级调研结论，见 README §8） -----------------
# 本机无 init_boot，内核 6.6 → KMI 前缀 android12-* → ksud choose_boot_partition() 会
# skip_init_boot 并回退到 boot；而 choose_boot_partition() 里【没有 vendor_boot 分支】。
# 所以 vendor_boot 必须显式 --partition vendor_boot，否则"白补一次"。
KERNEL_KMI="${KERNEL_KMI:-android15-6.6}"
KSU_MODULE="${KSU_MODULE:-}"            # 如 ${SUKISU}/kernels/android15-6.6_kernelsu.ko；留空则仅打 ramdisk
KSU_SKIP_INIT_BOOT="${KSU_SKIP_INIT_BOOT:-auto}"   # auto|1|0，传给 ksud --skip-init-boot

# --- LK（little kernel / preloader 级引导）补丁状态 -------------------------
# TB330FU 必须由 lkpatcher 打 lk_a / lk_b，否则即使 verity 关闭也会 "Dm-verity corruption"。
# 环境变量传入 lkpatcher 可执行路径（默认在 PATH 中找），脚本只做只读检测，绝不刷 LK。
LKPATCHER_BIN="${LKPATCHER_BIN:-lkpatcher}"

# --- 镜像尺寸策略 ------------------------------------------------------------
# LK 需要 boot 分区里有合法 AVB footer；半截镜像或全零尾都会冻结第一屏。
#   exact : 修补产物字节数必须 == 分区字节数（vkrepack.py splice 的输出）
#   repad : 产物较短时脚本补零到分区大小（再可选补 NONE footer）
#   allow : 仅警告（不推荐）
IMAGE_SIZE_POLICY="${IMAGE_SIZE_POLICY:-repad}"     # exact|repad|allow
AVBTOOL_BIN="${AVBTOOL_BIN:-avbtool}"               # 可选：avbtool add_hash_footer --algorithm NONE

# 超时（秒）
FASTBOOT_TIMEOUT="${FASTBOOT_TIMEOUT:-180}"
ADB_BOOT_TIMEOUT="${ADB_BOOT_TIMEOUT:-180}"
ADB_ROOT_TIMEOUT="${ADB_ROOT_TIMEOUT:-20}"
ADB_QUERY_TIMEOUT="${ADB_QUERY_TIMEOUT:-15}"
PULL_TIMEOUT="${PULL_TIMEOUT:-600}"
PATCH_TIMEOUT="${PATCH_TIMEOUT:-900}"
MTK_TIMEOUT="${MTK_TIMEOUT:-600}"

# 大小合理性区间（字节）
BOOT_MIN_BYTES=$((16 * 1024 * 1024))     # 16 MiB —— boot / init_boot 下限
BOOT_MAX_BYTES=$((256 * 1024 * 1024))    # 256 MiB
VENDOR_BOOT_MIN_BYTES=$((16 * 1024 * 1024))
VENDOR_BOOT_MAX_BYTES=$((256 * 1024 * 1024))
# 注意：不再用 ±N% 硬拒修补产物 —— LK 要求的是"整分区尺寸 + 合法 AVB footer"，
# 真正的门禁是 enforce_partition_size()（刷写前，字节级 == 分区大小）；
# 修补阶段只拒绝"超过分区容量"与"magic 损坏"两种情况。

# 磁盘余量：预计占用（原厂镜像 * 2 + 修补产物 + 备份），再加 512MiB 保底
DISK_MARGIN_BYTES=$((512 * 1024 * 1024))

VENDOR_BOOT_MAGIC="VNDRBOOT"
ANDROID_MAGIC_HEX="414e44524f494421"   # 'ANDROID!'

# -----------------------------------------------------------------------------
# 1. 全局状态（trap 用）
# -----------------------------------------------------------------------------
SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TS="$(date +%Y%m%d-%H%M%S)"
CURRENT_STAGE="init"
STEP_LOG=()          # 已执行步骤（ERR trap 会打印）
ROOT_STATE_DIR=""    # 运行时工作目录（默认 /work/tb330fu）
DUMP_DIR="" PATCHED_DIR="" LOGS_DIR="" BACKUP_DIR=""
LOG_FILE="/dev/null"

# CLI
OPT_SLOT="auto"
OPT_DRY_RUN=0
OPT_SKIP_FLASH=0
OPT_ROLLBACK=0
OPT_YES=0
OPT_KEEP=0
OPT_UNLOCK=0
OPT_NO_MTK=0
OPT_WORKDIR="$WORKDIR_DEFAULT"
OPT_MTK_PY="$MTK_PY_DEFAULT"
OPT_SUKISU="$SUKISU"
OPT_BACKEND="$PATCH_BACKEND"
OPT_PARTITIONS=""   # 为空=自动检测；否则逗号分隔 boot,init_boot,vendor_boot
OPT_VBMETA_IMG=""   # 原厂 vbmeta<slot>.img（可选；fastboot 刷回 + --disable-* 双保险，见 README §3）
OPT_NO_LK_CHECK=0   # 跳过 LK 补丁状态检测
OPT_FORCE_ACTIVE=0  # 设备当前 slot != 目标 slot 时，自动 fastboot --set-active=<slot>

# 运行时
SLOT=""             # a / b
SLOT_SUFFIX=""      # _a / _b
BOOT_PART=""        # boot_a / init_boot_a
BOOT_KIND=""        # boot | init_boot
VENDOR_PART=""      # vendor_boot_a
SERIAL=""
SERIAL_ARGS=()
MTK_PY=""           # 解析后实际存在的 mtk.py（preflight 阶段赋值）
KSUS=""             # 实际存在的 ksud
BOOT_PATCH=""       # 实际存在的 boot_patch.sh

# 退出码
E_USAGE=2
E_ENV=3
E_DEVICE=4
E_SLOT=5
E_EXTRACT=6
E_VERIFY=7
E_PATCH=8
E_FLASH=9
E_AVB=10
E_BOOT=11
E_ROOT=12
E_ROLLBACK=13
E_INTERRUPT=130

# -----------------------------------------------------------------------------
# 2. 日志 / 输出
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_CYN=$'\033[36m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_CYN=""; C_RST=""
fi

# log <level> <msg...>：写入日志文件 + stdout（tee 语义）
log() {
  local level="$1"; shift
  local line
  line="[$(date '+%F %T')] [$level] [${CURRENT_STAGE}] $*"
  # 日志文件永远写原始文本（无颜色）
  printf '%s\n' "$line" | sed 's/\x1b\[[0-9;]*m//g' >>"$LOG_FILE" 2>/dev/null || true
  case "$level" in
    OK)   printf '%s\n' "${C_GRN}${line}${C_RST}" ;;
    WARN) printf '%s\n' "${C_YEL}${line}${C_RST}" >&2 ;;
    ERR)  printf '%s\n' "${C_RED}${line}${C_RST}" >&2 ;;
    STEP) printf '%s\n' "${C_CYN}${line}${C_RST}" ;;
    *)    printf '%s\n' "$line" ;;
  esac
}

info() { log INFO "$@"; }
ok()   { log OK   "$@"; }
warn() { log WARN "$@"; }
err()  { log ERR  "$@"; }

die() { # die <exit_code> <msg...>
  local code="$1"; shift
  err "$*"
  err "退出码 $code —— 详见 $LOG_FILE"
  exit "$code"
}

# 记录"已完成步骤"，供 ERR trap 打印
step_done() {
  STEP_LOG+=("$1")
  log STEP "$1"
}

# 分段标题
banner() {
  printf '%s\n' "================================================================" >>"$LOG_FILE" 2>/dev/null || true
  log STEP "== $* =="
}

# 确认：默认每步都问；--yes 跳过；非 tty 且没有 --yes 时默认"No"
confirm() { # confirm <prompt>
  if [ "$OPT_YES" -eq 1 ]; then
    info "自动确认（--yes）：$1"
    return 0
  fi
  local ans=""
  if [ -t 0 ]; then
    printf '%s' "${C_YEL}>>> $1 [y/N]: ${C_RST}"
  else
    warn "stdin 不是终端且未指定 --yes，默认拒绝：$1"
    return 1
  fi
  read -r ans || ans=""
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

have() { command -v -- "$1" >/dev/null 2>&1; }

# 解析可用的 Python：优先 python3，其次 python（>=3）。写入 PY_BIN / PY_BASE / PY_VER
PY_BIN=""
PY_BASE=""
PY_VER=""
resolve_python() {
  local cand base ver
  for cand in python3 python; do
    have "$cand" || continue
    base="$(command -v "$cand")"
    ver="$("$cand" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    case "$ver" in
      3.*) PY_BIN="$cand"; PY_BASE="$base"; PY_VER="$ver"; return 0 ;;
    esac
  done
  die "$E_ENV" "找不到 Python 3（python3 / python 均不可用或版本 <3）。mtkclient 需要 Python 3。"
}

# 带超时的命令执行：timeout_run <seconds> <cmd...>
timeout_run() {
  local secs="$1"; shift
  if have timeout; then
    timeout --kill-after=10 "$secs" "$@"
  else
    "$@"
  fi
}

# 带超时的命令执行并捕获输出：out=$(timeout_capture <secs> <cmd...>)；返回码透传
timeout_capture() {
  local secs="$1"; shift
  local rc=0
  local output=""
  output="$(timeout_run "$secs" "$@" 2>&1)" || rc=$?
  printf '%s' "$output"
  return "$rc"
}

file_size() { # 可移植取字节数
  if stat -c %s -- "$1" >/dev/null 2>&1; then stat -c %s -- "$1"; else wc -c <"$1" | tr -d ' '; fi
}

sha256_of() {
  if have sha256sum; then sha256sum -- "$1" | awk '{print $1}'
  elif have shasum; then shasum -a 256 -- "$1" | awk '{print $1}'
  elif have openssl; then openssl dgst -sha256 -- "$1" | awk '{print $NF}'
  else die "$E_ENV" "缺少 sha256 工具（sha256sum/shasum/openssl），无法做镜像校验。"
  fi
}

human_size() { numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"; }

# -----------------------------------------------------------------------------
# 3. trap（ERR / EXIT / INT / TERM）
# -----------------------------------------------------------------------------
on_err() {
  local rc=$? line=${1:-?} cmd=${2:-?}
  local prev_steps="（无）"
  if [ "${#STEP_LOG[@]}" -gt 0 ]; then
    prev_steps=$(printf '\n   - %s' "${STEP_LOG[@]}")
    prev_steps="${prev_steps#$'\n'}"
  fi
  err "脚本异常中止！"
  err "  当前阶段 : ${CURRENT_STAGE}"
  err "  失败位置 : 行 ${line}  命令: ${cmd}"
  err "  返回码   : ${rc}"
  err "  已执行步骤："
  printf '%s\n' "$prev_steps" | sed 's/^/      /' >&2
  err "  日志     : ${LOG_FILE}"
  if [ -n "${BOOT_PART:-}" ] && [ -n "${SLOT:-}" ]; then
    print_rollback_hint >&2 || true
  fi
}

on_signal() {
  local sig="$1"
  err "收到信号 ${sig}，中止。日志：${LOG_FILE}"
  exit "$E_INTERRUPT"
}

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '[%s] [EXIT] 非零退出 rc=%s stage=%s\n' "$(date '+%F %T')" "$rc" "$CURRENT_STAGE" >>"$LOG_FILE" 2>/dev/null || true
  fi
  return 0
}

trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap on_exit EXIT

# -----------------------------------------------------------------------------
# 4. 帮助
# -----------------------------------------------------------------------------
usage() {
  cat <<'EOF'
ota-reinject.sh — TB330FU OTA 后自动重新注入 Root（SukiSU LKM）+ 重装自定义内核

用法:
  ota-reinject.sh [选项]

选项:
  --slot auto|a|b     目标槽位（默认 auto：自动检测 ro.boot.slot_suffix → current-slot → by-name）
  --dry-run           只做前置检查 + 槽位检测 + 提取 + 校验，不修补、不刷写、不重启
  --skip-flash        提取 + 修补，但不停在 fastboot、不刷写、不动 vbmeta（产物留在 patched/）
  --rollback          用 backup/<最近时间戳>/ 里的原厂镜像回刷 + 关闭 AVB + 重启验证
  --yes               非交互模式，所有确认自动 yes（危险，务必先跑 --dry-run）
  --keep              保留中间产物（默认也会保留 dump/patched/backup，此开关额外保留临时文件）
  --workdir <dir>     工作目录（默认 /work/tb330fu 或 $WORKDIR_DEFAULT）
  --mtk-py <path>     mtk.py 路径（默认 $MTKCLIENT_TOOL/mtk.py）
  --sukisu <dir>      SukiSU 仓库根目录（默认 /work/sukisu）
  --backend <mode>    修补后端: auto|ksud|boot_patch|custom（默认 auto）
  --partitions <list> 注入目标，逗号分隔: boot,init_boot,vendor_boot（默认自动检测）
                     ※ 从源码结论看：TB330FU 无 init_boot，且 vendor_boot 必须单独注入，
                       默认就是 boot + vendor_boot（cpio inner name 走 init_boot fragment/PLATFORM）
  --vbmeta-image <f>  原厂 vbmeta<slot>.img；fastboot 刷它并带 --disable-verity --disable-verification，
                       与 mtk.py da vbmeta 3 互为双保险（推荐提供）
  --set-active        设备当前 slot 与目标 slot 不一致时，自动 fastboot --set-active=<slot>
                       （因为 mtk.py da vbmeta 3 只写"当前 slot"的 vbmeta）
  --no-lk-check       跳过 LK(lk_a/lk_b) 补丁状态检测（默认在 --dry-run 与刷写前都会检查）
  --unlock            允许在需要时执行 `python mtk.py da seccfg unlock`（默认关闭，永久危险）
  --no-mtk            跳过 mtkclient 步骤（仅调试用；真实刷写后跳过 AVB 必定回滚）
  -h, --help          显示本帮助
  -V, --version       显示版本

环境变量（集中配置）:
  KERNEL_KMI=android15-6.6   KSU_MODULE=/path/android15-6.6_kernelsu.ko
  IMAGE_SIZE_POLICY=repad|exact|allow    AVBTOOL_BIN=avbtool   LKPATCHER_BIN=lkpatcher

退出码:
  0 成功 | 2 参数错误 | 3 环境缺失 | 4 设备未就绪 | 5 槽位检测失败 | 6 提取失败
  7 校验失败 | 8 修补失败 | 9 刷写失败 | 10 AVB 关闭失败 | 11 开机验证失败 | 12 Root 验证失败
  13 回滚失败 | 130 被中断

安全红线: 本脚本绝不刷 preloader、绝不刷全量固件、默认不解锁 seccfg、不处理 userdata。
EOF
}

# -----------------------------------------------------------------------------
# 5. 参数解析
# -----------------------------------------------------------------------------
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --slot)
        [ $# -ge 2 ] || die "$E_USAGE" "--slot 需要一个值: auto|a|b"
        OPT_SLOT="$2"; shift 2 ;;
      --slot=*) OPT_SLOT="${1#*=}"; shift ;;
      --dry-run) OPT_DRY_RUN=1; shift ;;
      --skip-flash) OPT_SKIP_FLASH=1; shift ;;
      --rollback) OPT_ROLLBACK=1; shift ;;
      --yes|-y) OPT_YES=1; shift ;;
      --keep) OPT_KEEP=1; shift ;;
      --unlock) OPT_UNLOCK=1; shift ;;
      --no-mtk) OPT_NO_MTK=1; shift ;;
      --workdir)
        [ $# -ge 2 ] || die "$E_USAGE" "--workdir 需要目录参数"
        OPT_WORKDIR="$2"; shift 2 ;;
      --workdir=*) OPT_WORKDIR="${1#*=}"; shift ;;
      --mtk-py)
        [ $# -ge 2 ] || die "$E_USAGE" "--mtk-py 需要路径参数"
        OPT_MTK_PY="$2"; shift 2 ;;
      --mtk-py=*) OPT_MTK_PY="${1#*=}"; shift ;;
      --sukisu)
        [ $# -ge 2 ] || die "$E_USAGE" "--sukisu 需要目录参数"
        OPT_SUKISU="$2"; shift 2 ;;
      --sukisu=*) OPT_SUKISU="${1#*=}"; shift ;;
      --backend)
        [ $# -ge 2 ] || die "$E_USAGE" "--backend 需要值: auto|ksud|boot_patch|custom"
        OPT_BACKEND="$2"; shift 2 ;;
      --backend=*) OPT_BACKEND="${1#*=}"; shift ;;
      --partitions)
        [ $# -ge 2 ] || die "$E_USAGE" "--partitions 需要逗号分隔列表"
        OPT_PARTITIONS="$2"; shift 2 ;;
      --partitions=*) OPT_PARTITIONS="${1#*=}"; shift ;;
      --vbmeta-image)
        [ $# -ge 2 ] || die "$E_USAGE" "--vbmeta-image 需要文件路径"
        OPT_VBMETA_IMG="$2"; shift 2 ;;
      --vbmeta-image=*) OPT_VBMETA_IMG="${1#*=}"; shift ;;
      --no-lk-check) OPT_NO_LK_CHECK=1; shift ;;
      --set-active) OPT_FORCE_ACTIVE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      -V|--version) printf '%s 1.0.0\n' "$SCRIPT_NAME"; exit 0 ;;
      *) usage >&2; die "$E_USAGE" "未知参数: $1" ;;
    esac
  done

  case "$OPT_SLOT" in auto|a|b) ;; *) die "$E_USAGE" "--slot 只能是 auto|a|b（收到: $OPT_SLOT）" ;; esac
  case "$OPT_BACKEND" in auto|ksud|boot_patch|custom) ;; *) die "$E_USAGE" "--backend 只能 auto|ksud|boot_patch|custom" ;; esac

  if [ "$OPT_DRY_RUN" -eq 1 ] && [ "$OPT_ROLLBACK" -eq 1 ]; then
    die "$E_USAGE" "--dry-run 与 --rollback 互斥"
  fi
  if { [ "$OPT_DRY_RUN" -eq 1 ] || [ "$OPT_SKIP_FLASH" -eq 1 ]; } && [ "$OPT_UNLOCK" -eq 1 ]; then
    die "$E_USAGE" "--unlock 与 --dry-run/--skip-flash 互斥（不会写入设备）"
  fi
}

# -----------------------------------------------------------------------------
# 6. 目录 / 日志初始化
# -----------------------------------------------------------------------------
init_dirs() {
  ROOT_STATE_DIR="$OPT_WORKDIR"
  DUMP_DIR="$ROOT_STATE_DIR/dump"
  PATCHED_DIR="$ROOT_STATE_DIR/patched"
  LOGS_DIR="$ROOT_STATE_DIR/logs"
  BACKUP_DIR="$ROOT_STATE_DIR/backup"

  mkdir -p -- "$DUMP_DIR" "$PATCHED_DIR" "$LOGS_DIR" "$BACKUP_DIR" 2>/dev/null \
    || die "$E_ENV" "无法创建目录 $ROOT_STATE_DIR/{dump,patched,logs,backup}（权限不足？）"

  LOG_FILE="$LOGS_DIR/ota-reinject-$TS.log"
  : >"$LOG_FILE" 2>/dev/null || die "$E_ENV" "日志文件不可写: $LOG_FILE"

  # tee 到 stdout：后续所有 log() 同时写文件与终端；这里额外把子进程输出也导入日志
  exec > >(tee -a "$LOG_FILE") 2> >(tee -a "$LOG_FILE" >&2)
}

# 结束 tee 子进程前给它一点时间 flush，避免最后几行日志丢失
flush_log() {
  sleep 0.2
  return 0
}

# -----------------------------------------------------------------------------
# 7. 前置检查
# -----------------------------------------------------------------------------
preflight() {
  CURRENT_STAGE="preflight"
  banner "前置检查"

  info "工作目录 : $ROOT_STATE_DIR"
  info "日志文件 : $LOG_FILE"
  local run_mode="FULL"
  if [ "$OPT_DRY_RUN" -eq 1 ]; then
    run_mode="DRY-RUN"
  elif [ "$OPT_SKIP_FLASH" -eq 1 ]; then
    run_mode="SKIP-FLASH"
  elif [ "$OPT_ROLLBACK" -eq 1 ]; then
    run_mode="ROLLBACK"
  fi
  info "模式     : $run_mode"

  # 7.1 必需工具
  local missing=()
  for t in adb fastboot; do
    have "$t" || missing+=("$t")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "$E_ENV" "缺少必需工具: ${missing[*]}（请先安装 android-platform-tools）"
  fi
  resolve_python   # 写入 PY_BIN / PY_BASE / PY_VER
  info "python  : $PY_BIN $PY_VER（$PY_BASE）"
  # xxd 缺失时 magic 校验退化为 od（BusyBox/GNU 均可）
  have xxd || warn "未找到 xxd，magic 校验将使用 od 兜底"
  info "adb     : $(command -v adb)"
  info "fastboot: $(command -v fastboot)"

  # 7.2 输出目录可写
  local d
  for d in "$DUMP_DIR" "$PATCHED_DIR" "$LOGS_DIR" "$BACKUP_DIR"; do
    [ -w "$d" ] || die "$E_ENV" "目录不可写: $d"
    local probe="$d/.write-test.$$"
    if ! : >"$probe" 2>/dev/null; then
      die "$E_ENV" "目录不可写: $d"
    fi
    rm -f -- "$probe"
  done
  ok "输出目录均可写"

  # 7.3 mtk.py
  MTK_PY=""
  if [ -n "$OPT_MTK_PY" ] && [ -f "$OPT_MTK_PY" ]; then
    MTK_PY="$OPT_MTK_PY"
  else
    # 兜底：PATH 上的 mtk.py / $MTKCLIENT_TOOL/mtk.py / 同目录
    for cand in "$OPT_MTK_PY" "$MTK_PY_DEFAULT" "${MTKCLIENT_TOOL:-}/mtk.py"; do
      if [ -n "$cand" ] && [ -f "$cand" ]; then MTK_PY="$cand"; break; fi
    done
  fi
  if [ "$OPT_NO_MTK" -eq 1 ]; then
    warn "--no-mtk：跳过 mtkclient 步骤（真实刷写后系统极可能静默回滚 AVB！）"
  elif [ -z "$MTK_PY" ]; then
    if [ "$OPT_DRY_RUN" -eq 1 ] || [ "$OPT_SKIP_FLASH" -eq 1 ]; then
      warn "未找到 mtk.py（本次模式不刷写，允许继续）：$OPT_MTK_PY"
    else
      die "$E_ENV" "找不到 mtkclient 的 mtk.py: $OPT_MTK_PY（用 --mtk-py 指定，或设置 MTKCLIENT_TOOL）"
    fi
  else
    ok "mtkclient: $MTK_PY"
  fi

  # 7.4 修补后端
  resolve_patch_backend

  # 7.5 磁盘空间
  local avail
  avail=$(df -PB1 -- "$ROOT_STATE_DIR" | awk 'NR==2{print $4}')
  if ! [[ "${avail:-}" =~ ^[0-9]+$ ]]; then
    die "$E_ENV" "无法读取磁盘剩余空间: $ROOT_STATE_DIR"
  fi
  local need=$DISK_MARGIN_BYTES
  local est_boot=$((BOOT_MAX_BYTES / 2)) est_vbnd=67108864   # 粗估 boot 128MiB、vendor_boot 64MiB
  local want=$(( (est_boot + est_vbnd) * 3 + DISK_MARGIN_BYTES ))
  need=$want
  if [ "$avail" -lt "$need" ]; then
    die "$E_ENV" "磁盘空间不足：可用 $(human_size "$avail")，预计需要 $(human_size "$need")（$ROOT_STATE_DIR）"
  fi
  ok "磁盘剩余 $(human_size "$avail")（预计需要 $(human_size "$need")）"

  # 7.6 设备在线
  check_device_online
  step_done "前置检查完成"
}

# 修补后端可插拔解析（同时准备两种入口，缺一即报错退出）
resolve_patch_backend() {
  KSUS=""
  BOOT_PATCH=""
  local ksus_cand="${OPT_SUKISU}/bin/ksud"
  local patch_cand="${OPT_SUKISU}/tools/boot_patch.sh"
  if [ -x "$ksus_cand" ]; then KSUS="$ksus_cand"; fi
  # boot_patch.sh 可能没有 +x，只要可读即可
  if [ -f "$patch_cand" ]; then BOOT_PATCH="$patch_cand"; fi

  case "$OPT_BACKEND" in
    custom)
      [ -n "$PATCH_CMD_TEMPLATE" ] || die "$E_ENV" "--backend custom 需要环境变量 PATCH_CMD_TEMPLATE"
      ok "修补后端: custom（$PATCH_CMD_TEMPLATE）"
      ;;
    ksud)
      [ -n "$KSUS" ] || die "$E_ENV" "修补后端 ksud 不可用：未找到可执行 $ksus_cand（检查 --sukisu）"
      ok "修补后端: ksud（$KSUS）"
      ;;
    boot_patch)
      [ -n "$BOOT_PATCH" ] || die "$E_ENV" "修补后端 boot_patch 不可用：未找到 $patch_cand（检查 --sukisu）"
      ok "修补后端: boot_patch（$BOOT_PATCH）"
      ;;
    auto)
      if [ -n "$KSUS" ] && [ -n "$BOOT_PATCH" ]; then
        ok "修补后端: auto → ksud（同时检测到 $BOOT_PATCH，可 --backend boot_patch 切换）"
      elif [ -n "$KSUS" ]; then
        ok "修补后端: auto → ksud（$KSUS）"
      elif [ -n "$BOOT_PATCH" ]; then
        ok "修补后端: auto → boot_patch（$BOOT_PATCH）"
      else
        die "$E_ENV" "修补后端不可用：既没有 $ksus_cand（ksud），也没有 $patch_cand（boot_patch.sh）。请设置 --sukisu 或 SUKISU 环境变量。"
      fi
      ;;
  esac
}

check_device_online() {
  local out=""
  local rc=0
  out="$(timeout_capture "$FASTBOOT_TIMEOUT" fastboot devices)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    warn "fastboot devices 执行失败(rc=$rc)。若设备尚未进入 fastboot 可先跳过。"
  fi
  # 过滤掉 fastbootd 之类的注释行
  local fdevs
  fdevs=$(printf '%s\n' "$out" | awk 'NF>=2 && $2 ~ /fastboot/ {print $1}' | grep -v '^$' || true)
  local adevs
  adevs=$(timeout_capture "$ADB_QUERY_TIMEOUT" adb devices || true)
  local adev_list
  adev_list=$(printf '%s\n' "$adevs" | awk 'NR>1 && NF>=2 && $2=="device" {print $1}' | grep -v '^$' || true)

  if [ -n "$fdevs" ]; then
    SERIAL=$(printf '%s\n' "$fdevs" | head -n1)
    info "设备状态: fastboot（$SERIAL）"
  elif [ -n "$adev_list" ]; then
    SERIAL=$(printf '%s\n' "$adev_list" | head -n1)
    info "设备状态: adb（$SERIAL）"
  else
    local unauthorized
    unauthorized=$(printf '%s\n' "$adevs" | awk 'NR>1 && $2=="unauthorized" {print $1}' || true)
    if [ -n "$unauthorized" ]; then
      die "$E_DEVICE" "设备 adb 未授权（$unauthorized）：请在平板上点允许 USB 调试"
    fi
    die "$E_DEVICE" "没有检测到设备：请连接 USB，开启 USB 调试（adb）或进入 bootloader/fastboot"
  fi

  if [ -n "$SERIAL" ]; then
    SERIAL_ARGS=(-s "$SERIAL")
  else
    SERIAL_ARGS=()
  fi

  # 机型校验（容错：拿不到就警告）
  local model=""
  if [ "${#adev_list}" -gt 0 ]; then
    model=$(timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" shell getprop ro.product.model 2>/dev/null | tr -d '\r' | head -n1 || true)
    local device
    device=$(timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" shell getprop ro.product.device 2>/dev/null | tr -d '\r' | head -n1 || true)
    info "设备型号: ${model:-?}  代号: ${device:-?}"
    if [ -n "$device" ] && [ "$device" != "TB330FU" ] && [ "$device" != "tb330fu" ]; then
      warn "设备代号不是 TB330FU（$device）。本脚本按 TB330FU 设计，将继续但风险自负。"
    fi
  else
    local fbmodel
    fbmodel=$(timeout_capture "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" getvar product 2>&1 || true)
    info "fastboot product: $(printf '%s' "$fbmodel" | head -n1 | tr -d '\r')"
  fi
}

# -----------------------------------------------------------------------------
# 8. 槽位检测
# -----------------------------------------------------------------------------
adb_q() { # adb_q <shell command...>  返回 stdout，去 CR
  timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" shell "$@" 2>/dev/null | tr -d '\r' || true
}

detect_slot() {
  CURRENT_STAGE="slot-detect"
  banner "槽位检测"

  if [ "$OPT_SLOT" != "auto" ]; then
    SLOT="$OPT_SLOT"
    ok "使用手动指定槽位: $SLOT（--slot $OPT_SLOT）"
    SLOT_SUFFIX="_$SLOT"
    step_done "槽位 = $SLOT（手动）"
    return 0
  fi

  local detected=""
  local method=""

  # 路径 1：ro.boot.slot_suffix
  local on_adb=0
  if timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" get-state 2>/dev/null | grep -q '^device$'; then
    on_adb=1
  fi

  if [ "$on_adb" -eq 1 ]; then
    local s
    s=$(adb_q getprop ro.boot.slot_suffix)
    info "getprop ro.boot.slot_suffix = '${s}'"
    case "$s" in
      _a) detected="a"; method="ro.boot.slot_suffix" ;;
      _b) detected="b"; method="ro.boot.slot_suffix" ;;
      "")
        # 按需求：ro.boot.slot_suffix 为空时必须硬失败，绝不猜测（也不从别的属性"推断"）。
        die "$E_SLOT" "ro.boot.slot_suffix 为空：设备/内核未暴露 A/B 槽位信息（非 A/B 内核或 bootctl 缺失？）。按约定不猜测槽位，请用 --slot a|b 手动指定后重跑。"
        ;;
      *)
        die "$E_SLOT" "ro.boot.slot_suffix 值异常: '${s}'，请用 --slot a|b 手动指定。"
        ;;
    esac
  else
    info "设备当前不在 adb（可能在 fastboot），跳过 ro.boot.slot_suffix"
  fi

  # 路径 2：fastboot getvar current-slot
  if [ -z "$detected" ]; then
    local fv
    fv=$(timeout_capture "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" getvar current-slot 2>&1 || true)
    info "fastboot getvar current-slot →"
    printf '%s\n' "$fv" | sed 's/^/    /'
    local cs
    cs=$(printf '%s\n' "$fv" | sed -n 's/^current-slot:[[:space:]]*\([ab]\).*$/\1/p' | head -n1)
    if [ -n "$cs" ]; then
      detected="$cs"; method="fastboot getvar current-slot"
    elif printf '%s' "$fv" | grep -qi 'unknown variable'; then
      warn "fastboot 不识别 current-slot（可能不是标准 bootloader 或处于 fastbootd）"
    fi
  fi

  # 路径 3：by-name 目录
  if [ -z "$detected" ]; then
    if [ "$on_adb" -eq 1 ]; then
      local names
      names=$(adb_q ls -l /dev/block/by-name/ 2>/dev/null || true)
      local has_a=0 has_b=0
      if printf '%s\n' "$names" | grep -qE '(^|[[:space:]])boot_a( |$)'; then has_a=1; fi
      if printf '%s\n' "$names" | grep -qE '(^|[[:space:]])boot_b( |$)'; then has_b=1; fi
      if [ "$has_a" -eq 1 ] && [ "$has_b" -eq 0 ]; then detected="a"; method="by-name 只有 boot_a"
      elif [ "$has_b" -eq 1 ] && [ "$has_a" -eq 0 ]; then detected="b"; method="by-name 只有 boot_b"
      elif [ "$has_a" -eq 1 ] && [ "$has_b" -eq 1 ]; then
        warn "by-name 同时存在 boot_a 与 boot_b，无法仅靠目录判定当前槽位，改看 slot_suffix"
        local bootsuf
        bootsuf=$(adb_q getprop ro.boot.slot_suffix)
        while [ -z "$bootsuf" ]; do
          if [ "$OPT_YES" -eq 1 ]; then
            die "$E_SLOT" "boot_a/boot_b 同时存在，--yes 模式无法交互询问当前槽位，请用 --slot a|b 明确指定。"
          fi
          printf '%s' "${C_YEL}>>> 两种槽位都存在，请输入当前运行槽位 a/b（或 Ctrl-C 退出）: ${C_RST}"
          local ans=""
          read -r ans || ans=""
          case "$ans" in a|A) bootsuf="_a" ;; b|B) bootsuf="_b" ;; esac
          if [ -n "$bootsuf" ]; then
            detected="${bootsuf#_}"; method="用户手选"
          fi
        done
        [ -n "$detected" ] || die "$E_SLOT" "boot_a/boot_b 同时存在且无法判定当前槽位，请用 --slot a|b 指定。"
      fi
    else
      warn "设备在 fastboot 且 getvar current-slot 不可用，无法通过 by-name 判定槽位"
    fi
  fi

  if [ -z "$detected" ]; then
    die "$E_SLOT" "槽位检测三条路径全部失败。请用 --slot a|b 手动指定后再运行。"
  fi

  SLOT="$detected"
  SLOT_SUFFIX="_$SLOT"
  ok "检测到当前槽位: $SLOT（来源: $method）"
  step_done "槽位 = $SLOT（来源: $method）"
}

# -----------------------------------------------------------------------------
# 9. 分区映射检测（boot vs init_boot、vendor_boot）
# -----------------------------------------------------------------------------
resolve_partitions() {
  CURRENT_STAGE="partition-map"
  banner "分区映射"

  local want_boot=1 want_vb=1
  if [ -n "$OPT_PARTITIONS" ]; then
    want_boot=0; want_vb=0
    local IFS=','
    local p
    for p in $OPT_PARTITIONS; do
      case "$p" in
        boot|init_boot) want_boot=1 ;;
        vendor_boot) want_vb=1 ;;
        "") ;;
        *) unset IFS; die "$E_USAGE" "--partitions 含未知分区: $p（允许 boot,init_boot,vendor_boot）" ;;
      esac
    done
    unset IFS
  fi

  BOOT_PART=""; BOOT_KIND=""; VENDOR_PART=""
  if [ "$want_boot" -eq 1 ]; then
    local names=""
    if timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" get-state 2>/dev/null | grep -q '^device$'; then
      names=$(adb_q ls /dev/block/by-name/)
    fi
    # --partitions 显式要求 init_boot 时，即使探测失败也必须选 init_boot（不静默降级到 boot）
    local forced_init=0 forced_boot=0
    case ",${OPT_PARTITIONS}," in
      *",init_boot,"*) forced_init=1 ;;
      *",boot,"*) forced_boot=1 ;;
    esac

    if [ "$forced_init" -eq 1 ]; then
      BOOT_PART="init_boot${SLOT_SUFFIX}"
      BOOT_KIND="init_boot"
      ok "--partitions 显式指定 init_boot → 注入目标 init_boot${SLOT_SUFFIX}"
      if [ -n "$names" ] && ! printf '%s\n' "$names" | grep -qE "(^|[[:space:]])init_boot${SLOT_SUFFIX}([[:space:]]|$)"; then
        warn "但 by-name 中未列出 init_boot${SLOT_SUFFIX}，后续提取若失败请改用 --partitions boot,vendor_boot"
      fi
    elif [ "$forced_boot" -eq 1 ]; then
      BOOT_PART="boot${SLOT_SUFFIX}"
      BOOT_KIND="boot"
      ok "--partitions 显式指定 boot → 注入目标 boot${SLOT_SUFFIX}"
      if [ -n "$names" ] && printf '%s\n' "$names" | grep -qE "(^|[[:space:]])init_boot${SLOT_SUFFIX}([[:space:]]|$)"; then
        warn "注意：by-name 里其实存在 init_boot${SLOT_SUFFIX}，但 --partitions 已强制 boot。若开机无 root 请改用 init_boot。"
      fi
    elif [ -n "$names" ] && printf '%s\n' "$names" | grep -qE "(^|[[:space:]])init_boot${SLOT_SUFFIX}([[:space:]]|$)"; then
      # 防御分支：源码调研确认 TB330FU 无 init_boot（DTB 在 vendor_boot 的 dtb 段，另有 dtbo 分区），
      # 但保留探测：万一某个 ZUI 版本改了分区布局也能正确落点。
      BOOT_PART="init_boot${SLOT_SUFFIX}"
      BOOT_KIND="init_boot"
      ok "by-name 中存在 init_boot${SLOT_SUFFIX} → 注入目标调整为 init_boot（与调研结论不同，请复核）"
    elif [ -n "$names" ]; then
      BOOT_PART="boot${SLOT_SUFFIX}"
      BOOT_KIND="boot"
      ok "by-name 中无 init_boot${SLOT_SUFFIX} → 注入目标为 boot${SLOT_SUFFIX}（与调研结论一致）"
    else
      # 拿不到 by-name（设备在 fastboot）→ 用 fastboot getvar has-slot:init_boot 判断
      local hv
      hv=$(timeout_capture "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" getvar "has-slot:init_boot" 2>&1 || true)
      if printf '%s' "$hv" | grep -q 'has-slot:init_boot:[[:space:]]*yes'; then
        BOOT_PART="init_boot${SLOT_SUFFIX}"; BOOT_KIND="init_boot"
        ok "fastboot has-slot:init_boot=yes → 注入目标 init_boot${SLOT_SUFFIX}"
      else
        BOOT_PART="boot${SLOT_SUFFIX}"; BOOT_KIND="boot"
        warn "无法读取 by-name 且 has-slot:init_boot 未确认 → 暂定 boot${SLOT_SUFFIX}（若后续提取校验发现是 GKI 空 ramdisk 会提示）"
      fi
    fi
  fi

  if [ "$want_vb" -eq 1 ]; then
    VENDOR_PART="vendor_boot${SLOT_SUFFIX}"
    ok "vendor_boot 注入目标: $VENDOR_PART"
  fi

  if [ -z "$BOOT_PART" ] && [ -z "$VENDOR_PART" ]; then
    die "$E_USAGE" "--partitions 列表为空，没有可注入的分区"
  fi
  step_done "分区映射 boot=${BOOT_PART:-none} vendor_boot=${VENDOR_PART:-none}"
}

# -----------------------------------------------------------------------------
# 10. 镜像提取 + 校验
# -----------------------------------------------------------------------------
ensure_adb_root() {
  CURRENT_STAGE="adb-root"
  local who
  who=$(adb_q id -u)
  if [ "$who" = "0" ]; then
    ok "adb shell 已是 root（可用于直接读 by-name 分区）"
    return 0
  fi
  info "adb shell 不是 root（id -u=${who:-?}），尝试 adb root ..."
  local r
  r=$(timeout_capture "$ADB_ROOT_TIMEOUT" adb "${SERIAL_ARGS[@]}" root 2>&1 || true)
  printf '%s\n' "$r" | sed 's/^/    /'
  sleep 2
  timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" wait-for-device || true

  who=$(adb_q id -u)
  if [ "$who" = "0" ]; then
    ok "adb root 成功"
    return 0
  fi

  # adb root 不可用（user 版本常见）→ 改用 su -c
  warn "adb root 不可用（user 构建常见）。改用 su -c 提取。"
  local su_id=""
  su_id=$(adb_q sh -c 'su -c id' || true)
  if printf '%s' "$su_id" | grep -q 'uid=0'; then
    ok "su -c 可用：$su_id"
    # 注意：su -c 场景下 adb exec-out 里没有 root，由 extract_part 的 su 路径处理；
    # 目前统一走 exec-out（root shell）——su 场景下 exec-out 也会报权限错误并明确失败，
    # 故这里直接提示用户改用原厂镜像，不静默继续。
    die "$E_EXTRACT" "adb root 不可用，su -c 虽有 root 但无法通过 adb exec-out 流式读分区。请在 fastboot/原厂包中取得 ${BOOT_PART:-boot} 与 ${VENDOR_PART:-vendor_boot}，放到 $DUMP_DIR/ 后，用 --skip-flash 或手动流程继续（文档见 README「镜像提取」一节）。"
  fi
  die "$E_EXTRACT" "既无法 adb root，也无法 su -c（su -c id='${su_id:-空}'）。请确认已解锁且 root 仍在，或从原厂全量包手工解出 ${BOOT_PART:-boot} / ${VENDOR_PART:-vendor_boot} 放到 $DUMP_DIR/ 后继续。"
}

# 从设备分区流式提取到本地：extract_part <partition_name> <out_file> <min> <max>
extract_part() {
  local part="$1" out="$2" min="$3" max="$4"
  local byname="/dev/block/by-name/${part}"
  local real=""
  real=$(adb_q readlink -f "$byname")
  [ -n "$real" ] || real="$byname"
  info "分区 $part → $real"

  local tmp="${out}.part"
  rm -f -- "$tmp"

  info "提取中（adb exec-out dd，超时 ${PULL_TIMEOUT}s，只读）..."
  # 注意：如果是块设备分区，dd 会读到分区末尾；若 readlink 失败则退化为 dd if=<byname>
  if ! timeout_run "$PULL_TIMEOUT" adb "${SERIAL_ARGS[@]}" exec-out "dd if=${real} bs=1048576 2>/dev/null" >"$tmp"; then
    rm -f -- "$tmp"
    die "$E_EXTRACT" "提取 $part 失败（adb exec-out dd 返回非零）。分区路径: $real（若权限不足请确认 adb root / su）"
  fi
  [ -s "$tmp" ] || { rm -f -- "$tmp"; die "$E_EXTRACT" "提取 $part 得到空文件（${real}）"; }
  mv -f -- "$tmp" "$out"

  local size
  size=$(file_size "$out")
  info "  大小: $(human_size "$size")（$size 字节）"
  if [ "$size" -lt "$min" ] || [ "$size" -gt "$max" ]; then
    die "$E_VERIFY" "镜像 $out 大小异常：$size 字节，期望 [$min, $max]。拒绝继续（可能是分区名错误/读取截断）。"
  fi
  ok "  $part 大小合理"
}

check_android_magic() { # check_android_magic <file> -> 打印结果；返回 0=含 ANDROID! 1=不含
  local f="$1" magic=""
  if have xxd; then
    magic=$(xxd -p -l 8 -- "$f" | tr -d '\n')
  else
    magic=$(od -A n -t x1 -N 8 -- "$f" | tr -d ' \n')
  fi
  info "  $f 头 8 字节(hex): $magic"
  [ "$magic" = "$ANDROID_MAGIC_HEX" ]
}

check_vendor_boot_magic() { # 返回 0=magic 正确
  local f="$1"
  local m=""
  if have xxd; then
    m=$(xxd -p -l 8 -- "$f" | xxd -r -p)
  else
    m=$(od -A n --skip-bytes=0 --read-bytes=8 --endian=big -c -- "$f" | tr -d ' \n')
  fi
  info "  vendor_boot magic: '${m}'（期望 '${VENDOR_BOOT_MAGIC}'）"
  [ "$m" = "$VENDOR_BOOT_MAGIC" ]
}

# 读 Android boot image 头里的 ramdisk_size（偏移 16，小端 u32）
# 返回 0=有 ramdisk(<ramdisk_size>)  1=无 ramdisk(GKI 纯内核)
boot_has_ramdisk() {
  local f="$1" hex="" rd=0
  if have xxd; then
    hex=$(xxd -p -s 16 -l 4 -- "$f" | tr -d '\n')
  else
    hex=$(od -A n --skip-bytes=16 --read-bytes=4 -t x1 -- "$f" | tr -d ' \n')
  fi
  case "$hex" in
    ????????) ;;
    *) info "  无法读取 ramdisk_size（hex='${hex}'），跳过 ramdisk 检测"; return 0 ;;
  esac
  # 头部字节序：魔数 "ANDROID!" 表明是小端
  local b0 b1 b2 b3
  b0=${hex:0:2}; b1=${hex:2:2}; b2=${hex:4:2}; b3=${hex:6:2}
  rd=$(( 0x$b3$b2$b1$b0 ))
  info "  ramdisk_size = $rd 字节"
  if [ "$rd" -eq 0 ]; then return 1; fi
  return 0
}

record_manifest() { # record_manifest <partition> <path> <sha256>
  local part="$1" path="$2" sum="$3"
  local size
  size=$(file_size "$path")
  {
    printf 'timestamp=%s\n' "$(date '+%F %T')"
    printf 'slot=%s\n' "$SLOT"
    printf 'partition=%s\n' "$part"
    printf 'path=%s\n' "$path"
    printf 'bytes=%s\n' "$size"
    printf 'sha256=%s\n' "$sum"
    printf 'kernel=%s\n' "$(adb_q uname -r 2>/dev/null || true)"
    printf 'build=%s\n' "$(adb_q getprop ro.build.display.id 2>/dev/null || true)"
    printf -- '---\n'
  } >>"$LOGS_DIR/manifest.txt"
}

extract_images() {
  CURRENT_STAGE="extract"
  banner "镜像提取"

  ensure_adb_root

  if [ -n "$BOOT_PART" ]; then
    local out="$DUMP_DIR/${BOOT_PART}.img"
    local bak=""
    if [ -f "$out" ]; then
      bak="$BACKUP_DIR/$TS/pre-existing"
      mkdir -p -- "$bak"
      mv -f -- "$out" "$bak/"
      warn "已存在旧提取 $out → 已移到 $bak/"
    fi
    extract_part "$BOOT_PART" "$out" "$BOOT_MIN_BYTES" "$BOOT_MAX_BYTES"
    if ! check_android_magic "$out"; then
      die "$E_VERIFY" "$out 头部不是 ANDROID! — 分区名/槽位可能不对，拒绝继续。"
    fi
    if [ "$BOOT_KIND" = "boot" ] && ! boot_has_ramdisk "$out"; then
      # GKI boot 无 ramdisk（ramdisk_size=0）时注入无效：必须改用 init_boot
      if [ -n "$names" ] && ! printf '%s\n' "$names" | grep -qE "(^|[[:space:]])init_boot${SLOT_SUFFIX}([[:space:]]|$)"; then
        warn "警告：设备 by-name 中确实没有 init_boot${SLOT_SUFFIX}，而 boot 又是无 ramdisk 的 GKI。"
        warn "请人工确认分区布局（boot/init_boot/vendor_boot）后，用 --partitions 显式指定，或改从原厂全量包取镜像。"
      fi
      die "$E_VERIFY" "boot${SLOT_SUFFIX} 的 ramdisk_size=0（GKI 纯内核，无 ramdisk），root 注入到 boot 无效，已中止。"
    fi
    local sum
    sum=$(sha256_of "$out")
    ok "sha256($out) = $sum"
    record_manifest "$BOOT_PART" "$out" "$sum"
  fi

  if [ -n "$VENDOR_PART" ]; then
    local out="$DUMP_DIR/${VENDOR_PART}.img"
    local bak=""
    if [ -f "$out" ]; then
      bak="$BACKUP_DIR/$TS/pre-existing"
      mkdir -p -- "$bak"
      mv -f -- "$out" "$bak/"
      warn "已存在旧提取 $out → 已移到 $bak/"
    fi
    extract_part "$VENDOR_PART" "$out" "$VENDOR_BOOT_MIN_BYTES" "$VENDOR_BOOT_MAX_BYTES"
    if ! check_vendor_boot_magic "$out"; then
      die "$E_VERIFY" "$out 头部 magic 不是 VNDRBOOT — 分区名/槽位可能不对（TB330FU 的 vendor_boot 约 64MiB），拒绝继续。"
    fi
    local vsize
    vsize=$(file_size "$out")
    if [ "$vsize" -lt $((32 * 1024 * 1024)) ] || [ "$vsize" -gt $((96 * 1024 * 1024)) ]; then
      warn "vendor_boot 大小 $(human_size "$vsize") 与预期的数十 MB 有偏差，请人工确认"
    fi
    local sum
    sum=$(sha256_of "$out")
    ok "sha256($out) = $sum"
    record_manifest "$VENDOR_PART" "$out" "$sum"
  fi

  step_done "镜像提取与校验完成"
}

# -----------------------------------------------------------------------------
# 11. 修补（可插拔 patch_image）
# -----------------------------------------------------------------------------
# patch_image <input> <output> <partition_kind: boot|init_boot|vendor_boot>
#   - 幂等缓存：dump/.cache-<sha256 of input> 存在则复用已修补产物
#   - 已存在的产物先备份到 backup/<ts>/patched/
#   - 修补后校验非空 + 大小变化在 ±PATCH_SIZE_TOLERANCE_PCT%
patch_image() {
  local in="$1" out="$2" kind="$3"

  [ -f "$in" ] || die "$E_PATCH" "patch_image: 输入不存在: $in"
  local in_sum
  in_sum=$(sha256_of "$in")
  local cache_marker="$DUMP_DIR/.cache-${in_sum}"

  # 1) 幂等：同一份原厂镜像已修补过 → 复用
  if [ -f "$cache_marker" ] && [ -s "$out" ]; then
    local cached_src cached_sum cached_out observed
    cached_src=$(sed -n 's/^input=//p' "$cache_marker" | head -n1)
    cached_sum=$(sed -n 's/^output_sha256=//p' "$cache_marker" | head -n1)
    cached_out=$(sed -n 's/^output=//p' "$cache_marker" | head -n1)
    observed=$(sha256_of "$out")
    if [ "$cached_src" = "$in" ] && [ "$cached_sum" = "$observed" ] && [ "$cached_out" = "$out" ]; then
      ok "幂等命中：$out 已是 $in（sha256 ${in_sum:0:12}…）的修补结果，跳过修补"
      printf '%s cached=%s\n' "$(date '+%F %T')" "$out" >>"$LOGS_DIR/manifest.txt"
      return 0
    fi
    warn "缓存标记存在但产物不一致（$out），重新修补"
  fi

  # 2) 备份已有产物
  if [ -e "$out" ]; then
    local bak="$BACKUP_DIR/$TS/patched"
    mkdir -p -- "$bak"
    local bname
    bname="$(basename -- "$out").$(date +%H%M%S)"
    mv -f -- "$out" "$bak/$bname"
    warn "已存在产物 $out → 备份到 $bak/$bname"
  fi

  local tmp_out="${out}.tmp"
  rm -f -- "$tmp_out"

  # 3) 分发到具体后端
  case "$OPT_BACKEND" in
    ksud|auto)
      if [ -n "$KSUS" ]; then
        patch_via_ksud "$in" "$tmp_out" "$kind"
      elif [ -n "$BOOT_PATCH" ]; then
        patch_via_boot_patch "$in" "$tmp_out" "$kind"
      else
        die "$E_ENV" "patch_image: 修补后端不可用（ksud / boot_patch.sh 都找不到）"
      fi
      ;;
    boot_patch)
      [ -n "$BOOT_PATCH" ] || die "$E_ENV" "patch_image: 未找到 $BOOT_PATCH"
      patch_via_boot_patch "$in" "$tmp_out" "$kind"
      ;;
    custom)
      patch_via_custom "$in" "$tmp_out" "$kind"
      ;;
    *) die "$E_PATCH" "patch_image: 未知后端 $OPT_BACKEND" ;;
  esac

  # 4) 产物校验
  [ -e "$tmp_out" ] || die "$E_PATCH" "patch_image: 后端未产出 $tmp_out（$kind）"
  local out_size
  out_size=$(file_size "$tmp_out")
  [ "$out_size" -gt 0 ] || die "$E_PATCH" "patch_image: 修补产物为空: $tmp_out"
  if [ "$kind" = "vendor_boot" ] && ! check_vendor_boot_magic "$tmp_out"; then
    die "$E_PATCH" "patch_image: 修补后的 vendor_boot magic 已损坏（$tmp_out），拒绝刷写。"
  fi
  if [ "$kind" != "vendor_boot" ] && ! check_android_magic "$tmp_out"; then
    die "$E_PATCH" "patch_image: 修补后的 $kind 头部不是 ANDROID!（$tmp_out），拒绝刷写。"
  fi

  # 4b) 尺寸策略：LK 要求 boot 分区内镜像长度 == 分区长度且尾部有合法 AVB footer。
  #     这里只做"补齐到分区大小"，footer 由 avbtool（可选）或 vkrepack.py splice 负责。
  #     注意：尺寸校验不再用 ±5% 硬拒 —— SukiSU 的 repack 可能明显变长/变短，
  #           真正的门禁是 enforce_partition_size()（刷写前，字节级 == 分区大小）。
  local part_size=""
  part_size=$(partition_size_bytes_for "$kind") || part_size=""
  if [ -n "$part_size" ] && [ "$out_size" -gt "$part_size" ]; then
    die "$E_PATCH" "patch_image: 产物 ${out_size}B 超过分区容量 ${part_size}B（$kind）。请减少 KSU_MODULE 或改用 vkrepack.py splice。"
  fi
  if [ -n "$part_size" ] && [ "$out_size" -lt "$part_size" ]; then
    pad_image_to_partition "$tmp_out" "$part_size" "$kind" || true
    out_size=$(file_size "$tmp_out")
  fi

  mv -f -- "$tmp_out" "$out"
  local out_sum
  out_sum=$(sha256_of "$out")

  # 5) 写幂等缓存标记
  {
    printf 'input=%s\n' "$in"
    printf 'input_sha256=%s\n' "$in_sum"
    printf 'output=%s\n' "$out"
    printf 'output_sha256=%s\n' "$out_sum"
    printf 'kind=%s\n' "$kind"
    printf 'backend=%s\n' "$OPT_BACKEND"
    printf 'slot=%s\n' "$SLOT"
    printf 'kmi=%s\n' "$KERNEL_KMI"
    printf 'partition_size=%s\n' "${part_size:-unknown}"
    printf 'timestamp=%s\n' "$(date '+%F %T')"
  } >"$cache_marker"

  {
    printf 'patched=%s\n' "$out"
    printf 'input=%s\n' "$in"
    printf 'input_sha256=%s\n' "$in_sum"
    printf 'sha256=%s\n' "$out_sum"
    printf 'bytes=%s\n' "$out_size"
    printf 'partition_size=%s\n' "${part_size:-unknown}"
    printf 'backend=%s\n' "$OPT_BACKEND"
    printf -- '---\n'
  } >>"$LOGS_DIR/manifest.txt"

  ok "修补完成: $out（$(human_size "$out_size")，sha256 ${out_sum:0:16}…）"
}

# 分区字节数（来自 by-name/sysfs size × 512 或设备块大小）；拿不到返回 1
partition_size_bytes_for() { # partition_size_bytes_for <boot|init_boot|vendor_boot>
  local kind="$1" part sectors bs
  case "$kind" in
    vendor_boot) part="${VENDOR_PART:-}" ;;
    boot|init_boot) part="${BOOT_PART:-}" ;;
    *) return 1 ;;
  esac
  [ -n "$part" ] || return 1
  sectors=$(adb_q cat "/sys/class/block/${part}/size" 2>/dev/null | tr -d '[:space:]')
  if [[ "$sectors" =~ ^[0-9]+$ ]] && [ "$sectors" -gt 0 ]; then
    bs=$(adb_q cat "/sys/class/block/${part}/queue/logical_block_size" 2>/dev/null | tr -d '[:space:]')
    [[ "$bs" =~ ^[0-9]+$ ]] || bs=512
    printf '%s' $((sectors * bs))
    return 0
  fi
  # 兜底：通过 readlink 到 /dev/block/sdX 再读 sysfs
  local real dev
  real=$(adb_q readlink -f "/dev/block/by-name/${part}")
  dev=$(basename -- "${real:-}")
  if [ -n "$dev" ]; then
    sectors=$(adb_q cat "/sys/class/block/${dev}/size" 2>/dev/null | tr -d '[:space:]')
    if [[ "$sectors" =~ ^[0-9]+$ ]] && [ "$sectors" -gt 0 ]; then
      printf '%s' $((sectors * 512)); return 0
    fi
  fi
  return 1
}

# 把短镜像补零到分区大小，并按需打 NONE footer
pad_image_to_partition() { # pad_image_to_partition <file> <size> <kind>
  local f="$1" want="$2" kind="$3" cur
  cur=$(file_size "$f")
  case "$IMAGE_SIZE_POLICY" in
    exact)
      die "$E_PATCH" "IMAGE_SIZE_POLICY=exact：$f 为 ${cur}B，分区要求 ${want}B。请用 vkrepack.py splice 或 --partition-size 重新打包。"
      ;;
    allow)
      warn "IMAGE_SIZE_POLICY=allow：$f 为 ${cur}B ≠ 分区 ${want}B（LK 可能拒绝启动，仅调试用）"
      return 0
      ;;
    repad|*)
      info "补齐：$f ${cur}B → ${want}B（truncate 补零）"
      truncate -s "$want" -- "$f" || { warn "truncate 失败，无法补齐"; return 0; }
      ;;
  esac
  # 可选：为 boot/init_boot 打 NONE footer（LK 需要合法 footer）
  if [ "$kind" != "vendor_boot" ] && have "$AVBTOOL_BIN"; then
    local footer=""
    footer=$("$AVBTOOL_BIN" info_image --image "$f" 2>/dev/null | grep -i 'Footer version' || true)
    if [ -z "$footer" ]; then
      info "avbtool 打 NONE footer: $f（$(basename -- "$AVBTOOL_BIN")）"
      if ! "$AVBTOOL_BIN" add_hash_footer --image "$f" --partition_size "$want" \
            --partition_name "boot" --algorithm NONE >/dev/null 2>"$PATCHED_DIR/.avbtool.err"; then
        warn "avbtool add_hash_footer 失败（详见 $PATCHED_DIR/.avbtool.err）；镜像已补零但无 NONE footer，LK 可能冻结在第一屏"
      fi
      # add_hash_footer 会重排镜像，重新补齐
      truncate -s "$want" -- "$f" 2>/dev/null || true
    fi
  elif [ "$kind" != "vendor_boot" ]; then
    warn "未找到 $AVBTOOL_BIN：镜像已补零，但 AVB footer 由修补后端（vkrepack.py splice / ksud）负责，请确认产物含合法 footer"
  fi
  return 0
}

# 刷写前硬门禁：待刷镜像字节数必须 == 分区字节数
enforce_partition_size() { # enforce_partition_size <partition> <image>
  local part="$1" img="$2" sz psz kind
  sz=$(file_size "$img")
  kind="vendor_boot"
  if [ "$part" != "$VENDOR_PART" ]; then kind="boot"; fi
  if ! psz=$(partition_size_bytes_for "$kind"); then
    warn "无法读取 $part 分区大小，跳过尺寸门禁（建议在 --dry-run 阶段核对）"
    return 0
  fi
  if [ "$sz" -ne "$psz" ]; then
    die "$E_FLASH" "待刷镜像尺寸 != 分区尺寸：$img 为 ${sz}B，$part 为 ${psz}B。LK 要求 boot 分区内是整分区镜像 + 合法 AVB footer，半截镜像会冻结第一屏。请用 IMAGE_SIZE_POLICY=repad（默认补零）或 vkrepack.py splice。"
  fi
  ok "尺寸门禁通过：$img == $part（${psz}B）"
}

# --- 后端 1：ksud（SukiSU Ultra/LKM） ---------------------------------------
# 源码结论（必须遵守）：
#   * ksud choose_boot_partition() 没有 vendor_boot 分支；
#   * 6.6 的 KMI 前缀是 android12-* → skip_init_boot=true，本机无 init_boot 时会回退到 boot；
#   * 因此 vendor_boot / boot 都必须显式 --partition，并显式 --kmi。
#   * ksud --flash 是直接写块设备、不碰 vbmeta → 永远不要用它替代本脚本的 flash 阶段。
# 期望 CLI（调研给出的形态）：
#   ksud boot-patch -b <img> --partition <boot|init_boot|vendor_boot> --kmi android15-6.6
#        --module <kernelsu.ko> --out <dir> --out-name <name>
patch_via_ksud() {
  local in="$1" out="$2" kind="$3"
  local outdir outname
  outdir="$(dirname -- "$out")"
  outname="$(basename -- "$out")"
  local -a args
  args=("$KSUS" boot-patch -b "$in" "--partition" "$kind" "--kmi" "$KERNEL_KMI"
        "--out" "$outdir" "--out-name" "$outname")
  case "$kind" in
    boot|init_boot|vendor_boot) : ;;
    *) die "$E_PATCH" "ksud: 未知分区类型 $kind" ;;
  esac
  # 仅在明确需要时传 --skip-init-boot（auto 时不传，交给 ksud 自己的 KMI 逻辑）
  case "${KSU_SKIP_INIT_BOOT}:$kind" in
    1:*|auto:init_boot) args+=("--skip-init-boot") ;;
  esac
  if [ -n "$KSU_MODULE" ]; then
    [ -f "$KSU_MODULE" ] || die "$E_PATCH" "KSU_MODULE 不存在: $KSU_MODULE"
    args+=("--module" "$KSU_MODULE")
  else
    warn "未设置 KSU_MODULE（如 \$SUKISU/kernels/${KERNEL_KMI}_kernelsu.ko）：LKM 模式没有 .ko 内核模块，刷完不会真正获得 root，只会替换 ramdisk 里的 ksud"
  fi
  info "执行: ksud boot-patch -b <img> --partition ${kind} --kmi ${KERNEL_KMI} …"
  info "       （${args[*]:1}）"
  if ! timeout_run "$PATCH_TIMEOUT" "${args[@]}"; then
    die "$E_PATCH" "ksud 修补失败（kind=${kind}）。常见原因：--partition/--kmi 参数名与本地 ksud 版本不符，或 KMI 不匹配。"
  fi
  # ksud 可能把产物写到 <outdir>/<outname>；若写成别的名字，这里兜底找回
  if [ ! -s "$out" ]; then
    local cand
    for cand in "$outdir/$outname" "$outdir"/*"$outname"* "$outdir"/boot.img "$outdir"/vendor_boot.img; do
      if [ -s "$cand" ] && [ "$cand" != "$in" ]; then
        warn "ksud 产物不在预期路径，改用 $cand"
        mv -f -- "$cand" "$out"
        break
      fi
    done
  fi
  [ -s "$out" ] || die "$E_PATCH" "ksud 未产出修补镜像（期望 $out）。请检查 ksud --help 的输出参数名。"
}

# --- 后端 2：Magisk 风格 tools/boot_patch.sh ---
patch_via_boot_patch() {
  local in="$1" out="$2" kind="$3"
  local workdir_tmp="$PATCHED_DIR/.work-${kind}-$TS"
  local staged
  staged="$workdir_tmp/$(basename -- "$in")"
  mkdir -p -- "$workdir_tmp"
  cp -f -- "$in" "$staged"
  info "执行: boot_patch.sh $staged（在临时工作目录 $workdir_tmp 中）"
  local rc=0
  if ! ( cd -- "$workdir_tmp" && timeout_run "$PATCH_TIMEOUT" bash -- "$BOOT_PATCH" "$staged" ); then
    rc=$?
  fi

  # 通用的产出定位：boot_patch.sh 一般生成 new-boot.img / new_image / patched.img
  local cand found=""
  for cand in "$workdir_tmp"/new-boot.img "$workdir_tmp"/new-vendor-boot.img \
              "$workdir_tmp"/new_image "$workdir_tmp"/patched.img \
              "$workdir_tmp"/*.img; do
    if [ -f "$cand" ] && [ "$cand" != "$staged" ]; then found="$cand"; break; fi
  done
  if [ "$rc" -ne 0 ] || [ -z "$found" ]; then
    die "$E_PATCH" "boot_patch.sh 修补失败（$kind，rc=$rc，产物=${found:-无}），临时目录: $workdir_tmp"
  fi
  mv -f -- "$found" "$out"
  if [ "$OPT_KEEP" -ne 1 ]; then rm -rf -- "$workdir_tmp"; else info "--keep：保留 $workdir_tmp"; fi
}

# --- 后端 3：外部自定义命令模板，占位符 {in} {out} {part} ---
patch_via_custom() {
  local in="$1" out="$2" kind="$3"
  local cmd="$PATCH_CMD_TEMPLATE"
  [ -n "$cmd" ] || die "$E_PATCH" "custom 后端未配置 PATCH_CMD_TEMPLATE"
  cmd="${cmd//\{in\}/$in}"
  cmd="${cmd//\{out\}/$out}"
  cmd="${cmd//\{part\}/$kind}"
  info "执行自定义修补: $cmd"
  if ! timeout_run "$PATCH_TIMEOUT" bash -c "$cmd"; then
    die "$E_PATCH" "自定义修补命令失败（$kind）"
  fi
}

patch_all() {
  CURRENT_STAGE="patch"
  banner "修补（LKM 双分区注入）"

  if [ "$OPT_DRY_RUN" -eq 1 ]; then
    info "--dry-run：跳过修补"
    return 0
  fi

  local targets=()
  local src=""
  if [ -n "$BOOT_PART" ]; then
    targets+=("$BOOT_PART")
  fi
  if [ -n "$VENDOR_PART" ]; then
    targets+=("$VENDOR_PART")
  fi

  for part in "${targets[@]}"; do
    src="$DUMP_DIR/${part}.img"
    local kind dst
    if [ "$part" = "$VENDOR_PART" ]; then
      kind="vendor_boot"
      dst="$PATCHED_DIR/patched-${part}.img"
    else
      kind="$BOOT_KIND"
      dst="$PATCHED_DIR/patched-${part}.img"
    fi
    info "── 修补 $part（kind=$kind）"
    patch_image "$src" "$dst" "$kind"
  done

  step_done "修补完成: ${targets[*]}"
}

# -----------------------------------------------------------------------------
# 12. 刷写（fastboot）
# -----------------------------------------------------------------------------
# preloader 防呆守卫：任何参数/分区名/镜像文件名命中 preloader 都直接拒绝
guard_no_preloader() {
  local what="$1"
  local lc
  lc=$(printf '%s' "$what" | tr '[:upper:]' '[:lower:]')
  case "$lc" in
    *preloader*)
      err "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      err "!! 检测到 preloader 相关操作，脚本设计上禁止任何 preloader 刷写！!"
      err "!! 触发点: $what"
      err "!! 严禁在 SP Flash Tool 中勾选 preloader —— 会破坏 BROM 关键区域，"
      err "!! 可能导致设备变砖且无法通过 fastboot 恢复。"
      err "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      die "$E_FLASH" "拒绝执行 preloader 相关操作: $what"
      ;;
  esac
}

# 刷前设备/槽位一致性检查
# 重要（源码结论）：mtk.py da vbmeta 3 只写 partition = "vbmeta"+get_current_slot()，
# 所以"设备当前 slot"必须等于"我们要刷的 slot"，否则 3 被打到另一个槽、必定回滚。
fastboot_current_slot() {
  local cv
  cv=$(timeout_capture "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" getvar current-slot 2>&1 || true)
  printf '%s\n' "$cv" | sed -n 's/^current-slot:[[:space:]]*\([ab]\).*$/\1/p' | head -n1
}

pre_flash_checks() {
  CURRENT_STAGE="pre-flash"
  local devs
  devs=$(timeout_capture "$FASTBOOT_TIMEOUT" fastboot devices | awk 'NF>=2 && $2 ~ /fastboot/ {print $1}' || true)
  if [ -z "$devs" ]; then
    die "$E_FLASH" "刷写前检查失败：设备不在 fastboot 模式。请 adb reboot bootloader（或 fastboot reboot bootloader）后重试。"
  fi
  info "fastboot 设备: $devs"

  local cs
  cs=$(fastboot_current_slot)
  if [ -z "$cs" ]; then
    if [ -n "$SERIAL" ] && ! printf '%s\n' "$devs" | grep -qx "$SERIAL"; then
      die "$E_FLASH" "设备出现变化（原 $SERIAL 不在 fastboot 列表）"
    fi
    die "$E_FLASH" "刷写前无法读取 fastboot current-slot，无法确认目标槽位，中止。"
  fi

  if [ "$cs" != "$SLOT" ]; then
    err "槽位不一致：检测/参数指定=$SLOT，fastboot current-slot=$cs"
    err "因为 mtk.py da vbmeta 3 只写【当前 slot】的 vbmeta，若继续：
           - 分区会写进 ${SLOT} 槽，但 3 会打到 ${cs} 槽 → 目标槽 AVB 未关 → 静默回滚"
    if [ "$OPT_FORCE_ACTIVE" -eq 1 ]; then
      info "--set-active：执行 fastboot --set-active=${SLOT} ..."
      if ! timeout_run "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" --set-active="$SLOT"; then
        die "$E_FLASH" "fastboot --set-active=${SLOT} 失败"
      fi
      cs=$(fastboot_current_slot)
      [ "$cs" = "$SLOT" ] || die "$E_FLASH" "--set-active 后 current-slot 仍为 '${cs}'，中止"
      ok "已切到 slot ${SLOT}，current-slot=${cs}"
    elif confirm "是否执行 fastboot --set-active=${SLOT} 把当前槽切到 ${SLOT}？（否则中止）"; then
      timeout_run "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" --set-active="$SLOT" \
        || die "$E_FLASH" "fastboot --set-active=${SLOT} 失败"
      cs=$(fastboot_current_slot)
      [ "$cs" = "$SLOT" ] || die "$E_FLASH" "--set-active 后 current-slot 仍为 '${cs}'，中止"
      ok "已切到 slot ${SLOT}"
    else
      die "$E_FLASH" "槽位不一致且未切换，已中止（未写入任何分区）。可加 --set-active 或 --slot ${cs} 重跑。"
    fi
  fi
  ok "刷前 current-slot=$cs 与目标槽位一致（mtk da vbmeta 3 会作用于该槽）"
}

flash_one() { # flash_one <partition> <image> [fastboot 额外参数...]
  local part="$1" img="$2"; shift 2
  local -a extra=("$@")
  guard_no_preloader "$part"
  guard_no_preloader "$img"
  [ -f "$img" ] || die "$E_FLASH" "待刷镜像不存在: $img"
  # 尺寸硬门禁：LK 要求整分区镜像（含 AVB footer），半截镜像会冻结第一屏
  enforce_partition_size "$part" "$img"
  local sum
  sum=$(sha256_of "$img")
  info "→ fastboot flash ${extra[*]:+${extra[*]} }$part  ←  $img（$(file_size "$img")B, sha256 ${sum:0:16}…）"
  local rc=0
  if ! timeout_run "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" "${extra[@]}" flash "$part" "$img"; then
    rc=$?
    die "$E_FLASH" "fastboot flash $part 失败（rc=$rc / 超时 ${FASTBOOT_TIMEOUT}s）。分区可能已被部分写入，请执行 --rollback。"
  fi
  ok "fastboot flash $part 成功"
  printf '%s partition=%s image=%s sha256=%s bytes=%s\n' \
    "$(date '+%F %T')" "$part" "$img" "$sum" "$(file_size "$img")" >>"$LOGS_DIR/manifest.txt"
}

# 找出可用的 vbmeta 原厂镜像：--vbmeta-image > dump/vbmeta<slot>.img > backup/<ts>/vbmeta<slot>.img
find_vbmeta_image() {
  local cand
  for cand in "$OPT_VBMETA_IMG" "$DUMP_DIR/vbmeta${SLOT_SUFFIX}.img" "$DUMP_DIR/vbmeta.img"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then printf '%s' "$cand"; return 0; fi
  done
  local ts
  ts=$(find_latest_backup 2>/dev/null || true)
  if [ -n "${ts:-}" ]; then
    for cand in "$ts/vbmeta${SLOT_SUFFIX}.img" "$ts/pre-existing/vbmeta${SLOT_SUFFIX}.img"; do
      if [ -f "$cand" ]; then printf '%s' "$cand"; return 0; fi
    done
  fi
  return 1
}

flash_all() {
  CURRENT_STAGE="flash"
  banner "刷写"

  if [ "$OPT_DRY_RUN" -eq 1 ]; then
    info "--dry-run：跳过刷写"
    return 0
  fi

  local -a plan=()
  if [ -n "$BOOT_PART" ]; then
    plan+=("$BOOT_PART:$PATCHED_DIR/patched-${BOOT_PART}.img")
  fi
  if [ -n "$VENDOR_PART" ]; then
    plan+=("$VENDOR_PART:$PATCHED_DIR/patched-${VENDOR_PART}.img")
  fi
  local vbm=""
  vbm=$(find_vbmeta_image || true)

  info "刷写计划："
  local item
  for item in "${plan[@]}"; do
    printf '    %-28s ← %s\n' "${item%%:*}" "${item#*:}"
  done
  if [ -n "$vbm" ]; then
    printf '    %-28s ← %s（fastboot --disable-verity --disable-verification）\n' "vbmeta${SLOT_SUFFIX}" "$vbm"
  else
    warn "未提供原厂 vbmeta 镜像（--vbmeta-image）：只走 mtk.py da vbmeta 3 关闭 AVB"
  fi
  # 显式声明本脚本不会做的事
  info "显式禁止：不刷 preloader / 不刷全量固件 / 不碰 userdata（脚本无此分支）"
  info "不使用 ksud --flash：它直接写块设备且【完全不碰 vbmeta】，会绕过 AVB 关闭流程"

  if ! confirm "确认将上述分区写入设备（slot ${SLOT}）？"; then
    die "$E_FLASH" "用户拒绝刷写，已中止（未写入任何分区）"
  fi

  lk_check
  pre_flash_checks

  # 先刷 boot/vendor_boot（带不带 --disable-* 都无所谓：有独立 vbmeta 分区时只对 vbmeta 生效）
  for item in "${plan[@]}"; do
    flash_one "${item%%:*}" "${item#*:}"
  done

  # 双保险 ①：刷原厂 vbmeta 并带 --disable-verity --disable-verification
  if [ -n "$vbm" ]; then
    if flash_one "vbmeta${SLOT_SUFFIX}" "$vbm" --disable-verity --disable-verification; then
      ok "AVB 路线 A 成功：fastboot flash vbmeta${SLOT_SUFFIX} --disable-verity --disable-verification"
      printf '%s avb_route=A(fastboot vbmeta --disable-*) result=ok image=%s\n' "$(date '+%F %T')" "$vbm" >>"$LOGS_DIR/manifest.txt"
    else
      warn "AVB 路线 A 失败；依赖路线 B（mtk.py da vbmeta 3）"
    fi
  fi
  step_done "fastboot 刷写完成（${#plan[@]} 个分区$([ -n "$vbm" ] && echo ' + vbmeta')）"
}

# -----------------------------------------------------------------------------
# 13. AVB 关闭（MTKClient BROM: mtk.py da vbmeta 3）
# -----------------------------------------------------------------------------
print_manual_avb_hint() {
  cat <<EOF
──────────────────────────────────────────────────────────────
手工执行 AVB 关闭（必须，否则系统会静默回滚到旧镜像）：
  1. 关机（不要插 USB 时关机），确认 USB 线已断开
  2. 接着按住 音量+ 与 音量- 不放，插入 USB 线 → 进入 BROM
  3. 执行：
       cd "$(dirname -- "${MTK_PY:-$OPT_MTK_PY}")"
       python3 mtk.py da vbmeta 3        # 或: python mtk.py da vbmeta 3
  4. 看到 'Successfully sent' / vbmeta 写入完成后再断电重启
     （源码级：它做的是 vbmeta[0x78:0x7c] = BE(3)，即 HASHTREE_DISABLED|VERIFICATION_DISABLED，
       并且【只写当前 slot】的 vbmeta —— 设备必须正跑在 ${SLOT:-<目标>} 槽上）
  5. 若 mtkclient 报 seccfg/hw crypto 相关错误，说明未解锁：
       python3 mtk.py da seccfg unlock     # 永久危险操作，自行评估
  6. 备选路线 A：fastboot flash vbmeta${SLOT_SUFFIX:-${SLOT:+_$SLOT}} --disable-verity --disable-verification
     （只在该条命令刷 vbmeta 时生效；刷 boot/vendor_boot 时带不带都一样）
──────────────────────────────────────────────────────────────
EOF
}

disable_avb() {
  CURRENT_STAGE="avb"
  banner "关闭 AVB 校验（da vbmeta 3）"

  if [ "$OPT_DRY_RUN" -eq 1 ]; then
    info "--dry-run：跳过 AVB 步骤"
    return 0
  fi
  if [ "$OPT_NO_MTK" -eq 1 ]; then
    warn "--no-mtk：跳过 AVB 关闭。若你确实刷了镜像，系统很可能静默回滚！"
    return 0
  fi
  if [ -z "$MTK_PY" ]; then
    print_manual_avb_hint >&2
    die "$E_AVB" "mtk.py 不可用，无法关闭 AVB（已中止，未重启）"
  fi

  info "请把设备断电，按住 音量+/- 后插入 USB 进入 BROM 模式..."
  confirm "设备已进入 BROM（或在 15s 内进入）？" || die "$E_AVB" "用户取消，AVB 未关闭（已中止，未重启）"

  local rc=0
  local out=""
  set +e
  out=$(cd -- "$(dirname -- "$MTK_PY")" && timeout_run "$MTK_TIMEOUT" "$PY_BIN" "$(basename -- "$MTK_PY")" da vbmeta 3 2>&1)
  rc=$?
  set -e
  printf '%s\n' "$out" | sed 's/^/    /'

  # 进入 BROM 后设备不再响应 fastboot，无法 getvar；用断开前记录的目标槽位做口径 + 退出后回查
  if [ "$rc" -ne 0 ] || ! printf '%s' "$out" | grep -Eqi 'vbmeta.*(done|written|success)|Successfully'; then
    err "AVB 关闭失败（rc=$rc）。已中止，不会重启设备。"
    print_manual_avb_hint >&2
    printf '%s avb_route=B(mtk da vbmeta 3) result=FAILED rc=%s\n' "$(date '+%F %T')" "$rc" >>"$LOGS_DIR/manifest.txt"
    die "$E_AVB" "mtk.py da vbmeta 3 执行失败"
  fi
  # 源码级说明：da vbmeta 3 的等价操作是 vbmeta[0x78:0x7c] = BE(3)
  #   （bit0 = HASHTREE_DISABLED，bit1 = VERIFICATION_DISABLED），且只写"当前 slot"的 vbmeta。
  ok "AVB 校验已关闭（da vbmeta 3 → vbmeta flags=3，作用于 slot ${SLOT}）"
  printf '%s avb_route=B(mtk da vbmeta 3) result=ok slot=%s\n' "$(date '+%F %T')" "$SLOT" >>"$LOGS_DIR/manifest.txt"
  step_done "AVB 已关闭（da vbmeta 3，slot ${SLOT}）"
}

# -----------------------------------------------------------------------------
# 13.0 LK（lk_a / lk_b）补丁状态检测 —— OTA 后必须复查
# -----------------------------------------------------------------------------
# 背景：TB330FU 的引导链是 preloader → LK(lk_a/lk_b) → boot。
# 若 LK 未由 lkpatcher 打补丁，即使 vbmeta flags=3，LK 仍会报 "Dm-verity corruption"
# 并卡住（这正是 OTA 后最常见的"刷完 boot 却起不来"）。
# 本脚本【只做只读检测，绝不刷 LK】。
LK_STATUS="unknown"   # patched | unpatched | unknown
lk_check() {
  CURRENT_STAGE="lk-check"
  banner "LK(lk_a/lk_b) 补丁状态检测"
  LK_STATUS="unknown"
  if [ "$OPT_NO_LK_CHECK" -eq 1 ]; then
    warn "--no-lk-check：跳过（自行确认 lk_a/lk_b 仍带 lkpatcher 补丁）"
    return 0
  fi

  # 方式 1：lkpatcher 自带的只读分析
  if have "$LKPATCHER_BIN" || [ -x "$LKPATCHER_BIN" ]; then
    local bout="" rc=0
    set +e
    bout=$("$LKPATCHER_BIN" --analyze-policies --dry-run 2>&1)
    rc=$?
    set -e
    printf '%s\n' "$bout" | sed 's/^/    /' | head -n 40
    if printf '%s' "$bout" | grep -Eqi 'already patched|patch (applied|present|found)'; then
      LK_STATUS="patched"
    elif printf '%s' "$bout" | grep -Eqi 'not patched|unpatched|need(s)? patch|no patch'; then
      LK_STATUS="unpatched"
    else
      LK_STATUS="unknown"
    fi
    info "lkpatcher 分析结果判定: $LK_STATUS（rc=${rc}）"
  else
    warn "未找到 lkpatcher（LKPATCHER_BIN=$LKPATCHER_BIN）：无法自动判定 LK 状态"
  fi

  # 方式 2：从设备导出 lk_a 与备份对比（只读；lk 分区一般没有 by-name，需要从原厂包或备份）
  local lk_backup=""
  local ts
  ts=$(find_latest_backup 2>/dev/null || true)
  if [ -n "${ts:-}" ]; then
    for lk_backup in "$ts/lk_a.img" "$ts/pre-existing/lk_a.img" "$DUMP_DIR/lk_a.img"; do
      [ -f "$lk_backup" ] && break
      lk_backup=""
    done
  fi
  if [ -n "$lk_backup" ]; then
    info "本地 LK 副本: $lk_backup（$(file_size "$lk_backup")B, sha256 $(sha256_of "$lk_backup" | cut -c1-16)…）"
  fi

  case "$LK_STATUS" in
    patched)
      ok "LK 已打补丁"
      ;;
    unpatched)
      err "LK 未打补丁！即使 verity 关闭也会出现 'Dm-verity corruption' 并卡住。"
      err "请在刷写前用 lkpatcher 对 lk_a 与 lk_b 都打补丁（本脚本不会自动刷 LK）。"
      ;;
    *)
      warn "LK 补丁状态【未知】：OTA（尤其是全量包）可能覆盖 lk_a/lk_b，务必人工确认 lkpatcher 已打过两个槽。"
      if [ "$OPT_YES" -eq 0 ]; then
        if ! confirm "已确认 lk_a / lk_b 均为已打补丁状态？"; then
          die "$E_FLASH" "用户未能确认 LK 状态，中止（避免刷完卡在第一屏）。可用 --no-lk-check 跳过检测。"
        fi
      fi
      ;;
  esac
  printf '%s lk_status=%s\n' "$(date '+%F %T')" "$LK_STATUS" >>"$LOGS_DIR/manifest.txt"
  step_done "LK 检测: $LK_STATUS"
}

# -----------------------------------------------------------------------------
# 14. 重启 + 验证
# -----------------------------------------------------------------------------
wait_for_adb() {
  local deadline=$((SECONDS + ADB_BOOT_TIMEOUT))
  info "等待 adb 设备上线（超时 ${ADB_BOOT_TIMEOUT}s）..."
  while [ "$SECONDS" -lt "$deadline" ]; do
    local st
    st=$(timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" get-state 2>/dev/null || true)
    if [ "$st" = "device" ]; then
      ok "adb 设备已上线"
      return 0
    fi
    sleep 3
  done
  return 1
}

verify_boot_and_root() {
  CURRENT_STAGE="verify"
  banner "重启与验证"

  if [ "$OPT_DRY_RUN" -eq 1 ] || [ "$OPT_SKIP_FLASH" -eq 1 ]; then
    info "本模式不重启/不验证。产物：$PATCHED_DIR"
    return 0
  fi

  if ! confirm "刷写与 AVB 已就绪，现在重启设备？"; then
    warn "用户拒绝重启。请手动 'fastboot reboot' 后自行验证。"
    return 0
  fi

  local rc=0
  timeout_run "$FASTBOOT_TIMEOUT" fastboot "${SERIAL_ARGS[@]}" reboot || rc=$?
  if [ "$rc" -ne 0 ]; then
    warn "fastboot reboot 返回 $rc，继续等待 adb..."
  fi
  step_done "已触发重启"

  if ! wait_for_adb; then
    err "开机验证失败：${ADB_BOOT_TIMEOUT}s 内 adb 未上线（可能是 bootloop 或卡 logo）"
    print_rollback_hint >&2
    die "$E_BOOT" "开机验证失败（adb 未上线）"
  fi

  # sys.boot_completed
  local deadline=$((SECONDS + ADB_BOOT_TIMEOUT))
  local bc=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    bc=$(adb_q getprop sys.boot_completed)
    if [ "$bc" = "1" ]; then break; fi
    sleep 3
  done
  if [ "$bc" != "1" ]; then
    err "开机验证失败：sys.boot_completed='${bc}'（期望 1）"
    print_rollback_hint >&2
    die "$E_BOOT" "设备未能完成开机"
  fi
  ok "开机完成（sys.boot_completed=1）"

  # root：su -c id
  local who=""
  who=$(adb_q sh -c 'su -c id' || true)
  if ! printf '%s' "$who" | grep -q 'uid=0'; then
    # 第二次尝试（SukiSU 首次授权可能弹窗，需平板点允许）
    warn "su -c id 未返回 root（'${who}'）。若平板弹出授权提示请点允许，15s 后重试..."
    sleep 15
    who=$(adb_q sh -c 'su -c id' || true)
  fi
  if printf '%s' "$who" | grep -q 'uid=0'; then
    ok "Root 生效：$who"
    step_done "Root 验证通过"
    return 0
  fi

  err "Root 验证失败：su -c id 输出 '${who}'"
  err "可能原因：init_boot/boot 注入目标选错、AVB 回滚、SukiSU 模块未授权"
  print_rollback_hint >&2
  die "$E_ROOT" "Root 未生效"
}

# -----------------------------------------------------------------------------
# 15. 回滚
# -----------------------------------------------------------------------------
find_latest_backup() {
  local d="" cand
  if [ ! -d "$BACKUP_DIR" ]; then return 1; fi
  # 备份目录名为时间戳 YYYYMMDD-HHMMSS；去掉 - 后是纯数字，取数值最大者即为最新
  for cand in "$BACKUP_DIR"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]; do
    [ -d "$cand" ] || continue
    local n="${cand##*/}"; n="${n/-/}"
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    if [ -z "$d" ] || [ "$n" -gt "$d" ]; then d="$n"; fi
  done
  [ -n "$d" ] || return 1
  # 还原目录名
  local name="${d:0:8}-${d:8:6}"
  printf '%s' "$BACKUP_DIR/$name"
}

# 校验一个镜像确实是原厂镜像：必须是 ANDROID!（boot/init_boot）或 VNDRBOOT（vendor_boot）
# 注意：必须写成 if/return，不能用 `case ... esac` 直接当返回值 ——
#       空分支返回的是上一条 info 的 0，会误判成功。
is_stock_image() { # is_stock_image <partition> <file>  → 0=是原厂镜像
  local part="$1" f="$2"
  [ -f "$f" ] || return 1
  case "$part" in
    vendor_boot*)
      if check_vendor_boot_magic "$f"; then return 0; fi
      return 1
      ;;
    *)
      if check_android_magic "$f"; then return 0; fi
      return 1
      ;;
  esac
}

find_stock_image() { # find_stock_image <partition> —— 命中时写入 STOCK_FOUND 并返回 0
  local part="$1"
  local ts_dir f
  # 1) 最近一次备份目录（备份目录里可能放在 patched/ 或 pre-existing/ 子目录）
  ts_dir=$(find_latest_backup || true)
  if [ -n "${ts_dir:-}" ]; then
    for f in "$ts_dir/$part.img" "$ts_dir/pre-existing/$part.img"; do
      if is_stock_image "$part" "$f"; then STOCK_FOUND="$f"; return 0; fi
    done
  fi
  # 2) dump 目录（dump 里始终只保存原厂镜像，修补产物在 patched/）
  f="$DUMP_DIR/${part}.img"
  if is_stock_image "$part" "$f"; then STOCK_FOUND="$f"; return 0; fi
  return 1
}

do_rollback() {
  CURRENT_STAGE="rollback"
  banner "回滚（回刷原厂镜像 + 关闭 AVB）"
  detect_slot
  resolve_partitions

  local partitions=()
  [ -n "$BOOT_PART" ] && partitions+=("$BOOT_PART")
  [ -n "$VENDOR_PART" ] && partitions+=("$VENDOR_PART")

  local -a plan=()
  local p
  for p in "${partitions[@]}"; do
    # 不能用 $(...) 捕获：函数内的 info() 也写 stdout，会污染路径
    STOCK_FOUND=""
    if ! find_stock_image "$p"; then
      die "$E_ROLLBACK" "找不到原厂镜像 $p（$BACKUP_DIR/<ts>/ 与 $DUMP_DIR/ 都没有）。请先从原厂全量包解出 ${p}.img。"
    fi
    plan+=("$p:$STOCK_FOUND")
  done

  info "回滚计划（dry-run 预览，确认后执行写盘）："
  for item in "${plan[@]}"; do
    printf '    %-28s ← %s（sha256 %s）\n' "${item%%:*}" "${item#*:}" "$(sha256_of "${item#*:}" | cut -c1-16)"
  done
  if [ "$OPT_DRY_RUN" -eq 1 ]; then
    info "--dry-run：不写盘"
    return 0
  fi
  if ! confirm "确认回滚刷写上述分区？"; then
    die "$E_ROLLBACK" "用户拒绝回滚"
  fi

  # 必须进 fastboot
  local st
  st=$(timeout_capture "$ADB_QUERY_TIMEOUT" adb "${SERIAL_ARGS[@]}" get-state 2>/dev/null || true)
  if [ "$st" = "device" ]; then
    info "adb reboot bootloader ..."
    adb "${SERIAL_ARGS[@]}" reboot bootloader || true
  fi
  local deadline=$((SECONDS + 120))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if timeout_capture "$FASTBOOT_TIMEOUT" fastboot devices 2>/dev/null | grep -q .; then break; fi
    sleep 2
  done

  pre_flash_checks
  for item in "${plan[@]}"; do
    flash_one "${item%%:*}" "${item#*:}"
  done
  ok "原厂镜像已回刷"
  step_done "回滚刷写完成"

  disable_avb
  if confirm "回滚完成，重启设备？"; then
    fastboot "${SERIAL_ARGS[@]}" reboot || true
    if wait_for_adb; then ok "回滚后 adb 已上线"; else warn "回滚后 adb 未在 ${ADB_BOOT_TIMEOUT}s 上线，请人工介入"; fi
  fi
  ok "回滚流程结束"
}

print_rollback_hint() {
  local def_slot="${SLOT:-<slot>}"
  local bp="${BOOT_PART:-boot_${def_slot}}"
  local vp="${VENDOR_PART:-vendor_boot_${def_slot}}"
  local bak
  bak=$(find_latest_backup 2>/dev/null || true)
  # 注意：本函数由 ERR trap 调用；仅含 cat 与纯变量赋值，不会被 $(...) 捕获污染路径
  if [ -z "$bak" ]; then bak="$BACKUP_DIR/<时间戳>"; fi
  if [ ! -f "$bak/$bp.img" ]; then bak="$DUMP_DIR（dump/ 里是本次提取的原厂镜像）"; fi

  cat <<EOF

────────────────────────── 回滚指引 ──────────────────────────
自动回滚（推荐）：
    bash "$SCRIPT_DIR/$SCRIPT_NAME" --workdir "$ROOT_STATE_DIR" --slot "${SLOT:-<slot>}" --rollback
手动回滚（设备在 fastboot 时）：
    fastboot flash ${bp}  "${bak}/${bp}.img"
    fastboot flash ${vp}  "${bak}/${vp}.img"
    fastboot reboot
手工回滚（设备在 fastbootd / BROM 时）：
    1. 进入 bootloader，按上面两条 fastboot flash 回刷原厂镜像
    2. BROM 下执行: python3 mtk.py da vbmeta 3   （重新关闭 AVB）
    3. fastboot reboot bootloader; fastboot reboot
若设备无法进 fastboot：断电 → 按住 音量+/- 插 USB 进 BROM → 用 mtkclient 重刷原厂 boot/vendor_boot
──────────────────────────────────────────────────────────────
EOF
}

# -----------------------------------------------------------------------------
# 16. 可选：解锁 seccfg（默认关闭）
# -----------------------------------------------------------------------------
maybe_unlock() {
  CURRENT_STAGE="unlock"
  if [ "$OPT_UNLOCK" -ne 1 ]; then
    return 0
  fi
  banner "可选：seccfg unlock（用户显式 --unlock）"
  warn "--unlock 已显式传入。此操作会永久清除设备安全状态（不可逆）。"
  if ! confirm "确认执行 'python3 mtk.py da seccfg unlock'？"; then
    warn "用户取消 seccfg unlock，继续后续流程"
    return 0
  fi
  [ -n "$MTK_PY" ] || die "$E_ENV" "mtk.py 不可用，无法 seccfg unlock"
  local rc=0
  set +e
  ( cd -- "$(dirname -- "$MTK_PY")" && timeout_run "$MTK_TIMEOUT" "$PY_BIN" "$(basename -- "$MTK_PY")" da seccfg unlock )
  rc=$?
  set -e
  [ "$rc" -eq 0 ] || die "$E_ENV" "seccfg unlock 失败（rc=$rc）"
  ok "seccfg unlock 完成"
  step_done "seccfg unlock"
}

# -----------------------------------------------------------------------------
# 17. 收尾 / 清理
# -----------------------------------------------------------------------------
cleanup() {
  CURRENT_STAGE="cleanup"
  if [ "$OPT_KEEP" -ne 1 ]; then
    # 默认也不删除 dump/patched/backup；这里只清理临时文件
    rm -f -- "$DUMP_DIR"/.write-test.* "$PATCHED_DIR"/.*.tmp 2>/dev/null || true
  fi
  info "产物目录："
  info "  dump/    : $DUMP_DIR"
  info "  patched/ : $PATCHED_DIR"
  info "  backup/  : $BACKUP_DIR"
  info "  manifest : $LOGS_DIR/manifest.txt"
}

# -----------------------------------------------------------------------------
# 18. 主流程
# -----------------------------------------------------------------------------
main() {
  parse_args "$@"
  init_dirs

  banner "TB330FU OTA Root 重注入开始（$TS）"
  info "命令行: $SCRIPT_NAME $*"
  info "主机: $(uname -srm 2>/dev/null || echo unknown)"

  preflight

  if [ "$OPT_ROLLBACK" -eq 1 ]; then
    do_rollback
    cleanup
    ok "回滚流程结束"
    flush_log
    exit 0
  fi

  detect_slot
  resolve_partitions
  extract_images
  lk_check      # OTA 后必须复查 LK：未打补丁 = 关 verity 也起不来
  patch_all
  maybe_unlock

  if [ "$OPT_DRY_RUN" -eq 1 ]; then
    banner "DRY-RUN 结束"
    info "已完成：前置检查 + 槽位检测 + 分区映射 + 提取 + 校验 + LK 检测。未修补、未刷写。"
    info "请同时人工确认：①LK 已打补丁 ②原厂 vbmeta${SLOT_SUFFIX}.img 已放到 dump/（可选，双保险）"
    cleanup
    flush_log
    exit 0
  fi

  if [ "$OPT_SKIP_FLASH" -eq 1 ]; then
    banner "SKIP-FLASH 结束"
    info "已完成：提取 + 修补。未刷写。产物在 $PATCHED_DIR"
    cleanup
    flush_log
    exit 0
  fi

  flash_all
  disable_avb
  verify_boot_and_root
  cleanup

  banner "全部完成"
  ok "Root 已重新注入并验证通过（slot ${SLOT}）"
  flush_log
}

main "$@"
