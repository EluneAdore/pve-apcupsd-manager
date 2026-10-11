#!/usr/bin/env bash
# 可通过 curl | bash 运行；下载最新的完整安装包。

bootstrap_error() { printf '错误：%s\n' "$*" >&2; return 1; }

bootstrap_fetch() {
    local archive listing member root_name
    local repository=EluneAdore/pve-apcupsd-manager
    for member in curl tar mktemp; do
        command -v "$member" >/dev/null 2>&1 || { bootstrap_error "缺少工具：$member。"; return 1; }
    done
    BOOTSTRAP_TMP=$(mktemp -d /tmp/pve-ups-download.XXXXXX) || return 1
    chmod 700 "$BOOTSTRAP_TMP" || return 1
    archive=$BOOTSTRAP_TMP/bundle.tar.gz
    curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 120 \
        "https://codeload.github.com/$repository/tar.gz/refs/heads/main" -o "$archive" || {
        bootstrap_error '下载安装包失败。'; return 1;
    }
    listing=$(tar -tzf "$archive") || return 1
    root_name=${listing%%/*}
    [[ $root_name == pve-apcupsd-manager-main ]] || { bootstrap_error '安装包目录不匹配。'; return 1; }
    while IFS= read -r member; do
        [[ $member == "$root_name/"* && $member != /* && $member != *'/../'* && $member != */.. ]] || {
            bootstrap_error '安装包含有非法路径。'; return 1;
        }
    done <<< "$listing"
    # GitHub 生成的源码包；禁用归档中的属主恢复。
    tar -xzf "$archive" --no-same-owner -C "$BOOTSTRAP_TMP" || return 1
    BOOTSTRAP_BUNDLE=$BOOTSTRAP_TMP/$root_name
    [[ -f $BOOTSTRAP_BUNDLE/bin/pve-ups && -f $BOOTSTRAP_BUNDLE/VERSION ]] || {
        bootstrap_error '安装包缺少必要文件。'; return 1;
    }
    printf '%s\n' '已下载最新管理工具。'
}

bootstrap_main() (
    set -uo pipefail
    local tool_only=0 info version major
    while (( $# )); do
        case $1 in
            --tool-only) tool_only=1; shift ;;
            --help|-h)
                printf '%s\n' '部署：bash bootstrap.sh' '仅更新管理工具：bash bootstrap.sh --tool-only'
                return 0 ;;
            *) bootstrap_error "未知参数：$1。"; return 2 ;;
        esac
    done
    [[ $(id -u) == 0 ]] || { bootstrap_error '请在 PVE 宿主机使用 root 运行。'; return 1; }
    command -v pveversion >/dev/null 2>&1 || { bootstrap_error '当前系统不是 PVE，未修改系统。'; return 1; }
    info=$(pveversion) || return 1
    version=${info#pve-manager/}; major=${version%%.*}
    [[ $major == 8 || $major == 9 ]] || { bootstrap_error '暂支持 PVE 8、9。'; return 1; }
    trap '[[ -z ${BOOTSTRAP_TMP:-} ]] || rm -rf -- "$BOOTSTRAP_TMP"' EXIT
    bootstrap_fetch || return 1
    if (( tool_only )); then bash "$BOOTSTRAP_BUNDLE/bin/pve-ups" tool-install
    else bash "$BOOTSTRAP_BUNDLE/bin/pve-ups" install; fi
)

if [[ ${PVE_UPS_BOOTSTRAP_SOURCE_ONLY:-0} != 1 ]]; then bootstrap_main "$@"; fi
