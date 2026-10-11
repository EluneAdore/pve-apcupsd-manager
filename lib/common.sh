#!/usr/bin/env bash
# 公共路径、环境检查及只读电源状态。配置和状态记录绝不作为脚本执行。

APP_ROOT=${APP_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}
# 由入口、菜单和更新模块共用。
# shellcheck disable=SC2034
APP_VERSION=$(cat "$APP_ROOT/VERSION")
PVE_ROOT=''
if [[ ${PVE_UPS_TESTING:-0} == 1 ]]; then
    [[ ${PVE_UPS_TEST_ROOT:-} == /tmp/* && -d ${PVE_UPS_TEST_ROOT} ]] || {
        printf '%s\n' '错误：隔离测试根目录必须是 /tmp 中已存在的目录。' >&2
        return 1
    }
    PVE_ROOT=${PVE_UPS_TEST_ROOT%/}
fi
STATE_DIR=${STATE_DIR:-$PVE_ROOT/var/lib/pve-ups}
CONFIG_FILE=${CONFIG_FILE:-$PVE_ROOT/etc/apcupsd/apcupsd.conf}
DEFAULTS_FILE=${DEFAULTS_FILE:-$PVE_ROOT/etc/default/apcupsd}
APPCONFIG_DIR=${APPCONFIG_DIR:-$(dirname -- "$CONFIG_FILE")}
INSTALL_DIR=${INSTALL_DIR:-$PVE_ROOT/opt/pve-ups}
COMMAND_PATH=${COMMAND_PATH:-$PVE_ROOT/usr/local/sbin/pve-ups}
POLICY_FILE=${POLICY_FILE:-$PVE_ROOT/usr/sbin/policy-rc.d}
APT_LOG_DIR=${APT_LOG_DIR:-$STATE_DIR/logs}
HISTORY_FILE=${HISTORY_FILE:-$STATE_DIR/history.tsv}
UPS_DATA=''
UPS_QUERY_OK=0

msg() { printf '%s\n' "$*"; }
err() { printf '错误：%s\n' "$*" >&2; return 1; }

require_root() {
    [[ ${PVE_UPS_TESTING:-0} == 1 || $(id -u) == 0 ]] || err '请在 PVE 宿主机使用 root 运行。'
}

require_pve() {
    local info version major os_id os_version
    if [[ ${PVE_UPS_TESTING:-0} == 1 ]]; then return 0; fi
    command -v pveversion >/dev/null 2>&1 || { err '当前系统不是 PVE，未修改系统。'; return 1; }
    info=$(pveversion 2>/dev/null) || return 1
    version=${info#pve-manager/}; major=${version%%.*}
    [[ $major == 8 || $major == 9 ]] || { err "暂支持 PVE 8、9；当前版本：$info。"; return 1; }
    os_id=$(awk -F= '$1=="ID" {gsub(/"/,"",$2); print $2}' /etc/os-release)
    os_version=$(awk -F= '$1=="VERSION_ID" {gsub(/"/,"",$2); print $2}' /etc/os-release)
    [[ $os_id == debian && ( $major:$os_version == 8:12 || $major:$os_version == 9:13 ) ]] || {
        err '当前系统不符合 PVE 8 / Debian 12 或 PVE 9 / Debian 13 的支持范围。'; return 1;
    }
    [[ -d /run/systemd/system ]] || { err '当前系统未运行 systemd。'; return 1; }
    for info in apt-get dpkg-query systemctl flock timeout awk; do
        command -v "$info" >/dev/null 2>&1 || { err "缺少系统工具：$info。"; return 1; }
    done
}

service_active() { systemctl is-active --quiet apcupsd.service 2>/dev/null; }

read_ups_snapshot() {
    UPS_DATA=''; UPS_QUERY_OK=0
    command -v apcaccess >/dev/null 2>&1 || return 1
    local port=3551 addr=127.0.0.1
    if [[ -r $CONFIG_FILE ]]; then
        port=$(awk '$1=="NISPORT" {v=$2} END {print v}' "$CONFIG_FILE")
        port=${port:-3551}
    fi
    [[ $port =~ ^[0-9]+$ ]] || return 1
    UPS_DATA=$(timeout 3 apcaccess status "$addr:$port" 2>/dev/null) || return 1
    [[ -n $UPS_DATA ]] || return 1
    UPS_QUERY_OK=1
}

wait_ups_snapshot() {
    local attempt
    # USB 服务刚重启时可能尚未就绪；等待有明确上限。
    for attempt in {1..10}; do
        if read_ups_snapshot && [[ -n $(ups_value STATUS) && $(ups_value STATUS) != *COMMLOST* ]]; then return 0; fi
        [[ $attempt == 10 ]] || sleep 1
    done
    return 1
}

ups_value() {
    local key=$1
    awk -F: -v key="$key" '$1 ~ "^[[:space:]]*"key"[[:space:]]*$" {
        sub(/^[^:]*:[[:space:]]*/, ""); sub(/[[:space:]]*$/, ""); print; exit
    }' <<< "$UPS_DATA"
}

ups_number() {
    local value
    value=$(ups_value "$1")
    [[ $value =~ ^([0-9]+)(\.[0-9]+)?([[:space:]]|$) ]] || return 1
    printf '%s\n' "${BASH_REMATCH[1]}"
}

ups_online() {
    local state
    [[ $UPS_QUERY_OK == 1 ]] || return 1
    state=$(ups_value STATUS)
    [[ " $state " == *' ONLINE '* && " $state " != *' ONBATT '* &&
       " $state " != *' COMMLOST '* && $state != *'SHUTTING DOWN'* ]]
}

guard_mains() {
    # 已停止的服务没有正在运行的倒计时，可进行维护和恢复监控。
    service_active || return 0
    read_ups_snapshot && ups_online && return 0
    err '停电或无法确认市电状态时，禁止修改参数、升级、重启和停止监控；请恢复市电及通信后重试。'
}

countdown_seconds() {
    local state limit elapsed
    [[ $UPS_QUERY_OK == 1 ]] || return 1
    state=$(ups_value STATUS)
    [[ " $state " == *' ONBATT '* && $state != *'COMMLOST'* && $state != *'SHUTTING DOWN'* ]] || return 1
    limit=$(ups_number MAXTIME) || return 1
    elapsed=$(ups_number TONBATT) || return 1
    (( limit > 0 )) || return 1
    (( elapsed < limit )) && printf '%s\n' "$((limit - elapsed))" || printf '0\n'
}

with_lock() {
    # 锁位于运行时目录，不与卸载删除的恢复记录共用目录。
    local lockfile=$PVE_ROOT/run/lock/pve-ups.lock rc
    mkdir -p -- "$(dirname -- "$lockfile")" || return 1
    if [[ ! -e $lockfile ]]; then
        # /run/lock 可能允许其他用户写入，创建时禁止覆盖已有路径。
        (umask 077; set -o noclobber; : > "$lockfile") 2>/dev/null || {
            err '无法安全创建管理锁，请检查运行时目录。'; return 1;
        }
    fi
    [[ -f $lockfile && ! -L $lockfile ]] || { err '管理锁不是普通文件，已取消操作。'; return 1; }
    if [[ ${PVE_UPS_TESTING:-0} != 1 && $(stat -c '%u' "$lockfile") != 0 ]]; then
        err '管理锁不属于 root，已取消操作。'; return 1
    fi
    (
        flock -n 9 || { err '另一个管理操作正在执行，请稍后重试。'; exit 1; }
        "$@"
    ) 9>>"$lockfile"
    rc=$?
    return "$rc"
}

require_managed() {
    if [[ ! -f $STATE_DIR/.pve-ups-owned ]] || [[ $(cat "$STATE_DIR/.pve-ups-owned") != pve-ups-v1 ]]; then
        err '尚未由本工具接管，请先选择“安装或修复监控软件”。'; return 1
    fi
    if declare -F lifecycle_is_managed >/dev/null; then lifecycle_is_managed; fi
}

package_version() {
    dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' apcupsd 2>/dev/null |
        awk '$1=="ii" {print $2; exit}'
}
