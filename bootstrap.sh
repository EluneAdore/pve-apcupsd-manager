#!/usr/bin/env bash
# 可通过 curl | bash 运行；先解析确切提交，再获取同一提交的完整安装包。

bootstrap_error() { printf '错误：%s\n' "$*" >&2; return 1; }

bootstrap_fetch() {
    local reference=$1 resolved archive listing member root_name
    local repository=EluneAdore/pve-apcupsd-manager
    [[ $reference =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$ ]] || { bootstrap_error '版本引用仅允许字母、数字、点、横线及下划线。'; return 1; }
    for member in curl tar mktemp; do
        command -v "$member" >/dev/null 2>&1 || { bootstrap_error "缺少工具：$member。"; return 1; }
    done
    BOOTSTRAP_TMP=$(mktemp -d /tmp/pve-ups-download.XXXXXX) || return 1
    chmod 700 "$BOOTSTRAP_TMP" || return 1
    if [[ $reference =~ ^[a-fA-F0-9]{40}$ ]]; then
        resolved=$reference
    else
        resolved=$(curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 60 \
            -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$repository/commits/$reference") || {
            bootstrap_error '无法解析版本；请确认仓库已发布、引用存在且 GitHub 可连接。'; return 1;
        }
    fi
    [[ $resolved =~ ^[a-fA-F0-9]{40}$ ]] || { bootstrap_error 'GitHub 返回的提交号无效。'; return 1; }
    archive=$BOOTSTRAP_TMP/bundle.tar.gz
    curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 120 \
        "https://codeload.github.com/$repository/tar.gz/$resolved" -o "$archive" || {
        bootstrap_error '下载安装包失败。'; return 1;
    }
    listing=$(tar -tzf "$archive") || return 1
    root_name=${listing%%/*}
    [[ $root_name == pve-apcupsd-manager-* ]] || { bootstrap_error '安装包目录不匹配。'; return 1; }
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
    printf '已取得提交：%s\n' "$resolved"
}

bootstrap_main() (
    set -uo pipefail
    local reference=main imported='' tool_only=0 info version major
    while (( $# )); do
        case $1 in
            --ref) [[ $# -ge 2 ]] || return 2; reference=$2; shift 2 ;;
            --config) [[ $# -ge 2 ]] || return 2; imported=$2; shift 2 ;;
            --tool-only) tool_only=1; shift ;;
            --help|-h)
                printf '%s\n' '部署：bash bootstrap.sh [--ref 标签或提交号] [--config 备份文件]' '更新工具：bash bootstrap.sh --ref 标签或提交号 --tool-only'
                return 0 ;;
            *) bootstrap_error "未知参数：$1。"; return 2 ;;
        esac
    done
    [[ $(id -u) == 0 ]] || { bootstrap_error '请在 PVE 宿主机使用 root 运行。'; return 1; }
    command -v pveversion >/dev/null 2>&1 || { bootstrap_error '当前系统不是 PVE，未修改系统。'; return 1; }
    info=$(pveversion) || return 1
    version=${info#pve-manager/}; major=${version%%.*}
    [[ $major == 8 || $major == 9 ]] || { bootstrap_error '暂支持 PVE 8、9。'; return 1; }
    [[ $tool_only == 0 || -z $imported ]] || { bootstrap_error '更新工具不能同时导入配置。'; return 2; }
    trap '[[ -z ${BOOTSTRAP_TMP:-} ]] || rm -rf -- "$BOOTSTRAP_TMP"' EXIT
    bootstrap_fetch "$reference" || return 1
    if (( tool_only )); then bash "$BOOTSTRAP_BUNDLE/bin/pve-ups" tool-install
    elif [[ -n $imported ]]; then bash "$BOOTSTRAP_BUNDLE/bin/pve-ups" install --config "$imported"
    else bash "$BOOTSTRAP_BUNDLE/bin/pve-ups" install; fi
)

if [[ ${PVE_UPS_BOOTSTRAP_SOURCE_ONLY:-0} != 1 ]]; then bootstrap_main "$@"; fi
