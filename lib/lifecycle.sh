#!/usr/bin/env bash
# 宿主机生命周期管理：保留首次基线、限制软件包范围，并支持断网撤销。

source "${APP_ROOT}/lib/state.sh"

lifecycle_is_managed() {
    [[ $(state_get "$STATE_DIR/manifest.tsv" app_id 2>/dev/null) == pve-ups-v1 ]] || {
        err '当前系统没有本工具的安装记录，请先执行安装。'; return 1;
    }
    [[ $(state_get "$STATE_DIR/manifest.tsv" config_file) == "$CONFIG_FILE" &&
       $(state_get "$STATE_DIR/manifest.tsv" defaults_file) == "$DEFAULTS_FILE" &&
       $(state_get "$STATE_DIR/manifest.tsv" install_directory) == "$INSTALL_DIR" &&
       $(state_get "$STATE_DIR/manifest.tsv" command_path) == "$COMMAND_PATH" ]] || {
        err '当前路径与首次安装记录不一致，已取消操作。'; return 1;
    }
}

lifecycle_create_baseline() (
    local manifest package_present=no staging entry
    if [[ -e $STATE_DIR/manifest.tsv ]]; then lifecycle_is_managed; return; fi
    if [[ -d $INSTALL_DIR ]] && [[ ! -f $INSTALL_DIR/.pve-ups-owned || $(cat "$INSTALL_DIR/.pve-ups-owned") != pve-ups-v1 ]]; then
        err '安装目录已被其他内容占用，无法安全覆盖。'; return 1
    fi
    if [[ -e $COMMAND_PATH || -L $COMMAND_PATH ]]; then
        [[ -L $COMMAND_PATH && $(readlink "$COMMAND_PATH") == "$INSTALL_DIR/bin/pve-ups" ]] || {
            err '管理命令路径已被其他程序占用。'; return 1;
        }
    fi
    if [[ -e $STATE_DIR ]]; then
        [[ -d $STATE_DIR ]] || { err '状态路径不是目录。'; return 1; }
        shopt -s nullglob dotglob
        for entry in "$STATE_DIR"/*; do
            err "状态目录含有未知文件，无法建立安全基线：${entry##*/}"; return 1
        done
    fi
    mkdir -p -- "${STATE_DIR%/*}" || return 1
    staging=$(mktemp -d "${STATE_DIR}.new.XXXXXX") || return 1
    trap 'rm -rf -- "$staging"' EXIT
    chmod 700 "$staging" || return 1
    manifest=$staging/manifest.tsv
    state_snapshot_files "$staging/baseline" || return 1
    state_packages > "$staging/packages-before.tsv" || return 1
    apt-mark showauto > "$staging/packages-auto-before.list" || return 1
    if state_package_installed apcupsd; then package_present=yes; fi
    state_service_snapshot "$manifest" || return 1
    state_put "$manifest" apcupsd_present "$package_present" || return 1
    state_put "$manifest" config_file "$CONFIG_FILE" || return 1
    state_put "$manifest" defaults_file "$DEFAULTS_FILE" || return 1
    state_put "$manifest" install_directory "$INSTALL_DIR" || return 1
    state_put "$manifest" command_path "$COMMAND_PATH" || return 1
    state_put "$manifest" initial_version "$APP_VERSION" || return 1
    : > "$staging/packages-added.list"
    state_put "$manifest" app_id pve-ups-v1 || return 1
    printf '%s\n' pve-ups-v1 > "$staging/.pve-ups-owned" || return 1
    # 整份基线一次性提交，备份失败不会留下无法重试的半成品。
    if [[ -d $STATE_DIR ]]; then rmdir -- "$STATE_DIR" || return 1; fi
    mv -- "$staging" "$STATE_DIR" || return 1
)

lifecycle_transaction_begin() {
    LIFECYCLE_TX=$(mktemp -d "$STATE_DIR/.transaction.XXXXXX") || return 1
    # 尚未修改系统时，快照失败只需移除未完成的临时交易目录。
    trap 'rm -rf -- "$LIFECYCLE_TX"' EXIT
    chmod 700 "$LIFECYCLE_TX" || return 1
    state_snapshot_files "$LIFECYCLE_TX/files" || return 1
    state_service_snapshot "$LIFECYCLE_TX/service.tsv" || return 1
    state_packages > "$LIFECYCLE_TX/packages-before.tsv" || return 1
    apt-mark showauto > "$LIFECYCLE_TX/auto-before.list" || return 1
    if [[ -d $INSTALL_DIR ]]; then
        cp -a -- "$INSTALL_DIR" "$LIFECYCLE_TX/bundle-before" || return 1
    fi
    if [[ -e $COMMAND_PATH || -L $COMMAND_PATH ]]; then
        cp -a -- "$COMMAND_PATH" "$LIFECYCLE_TX/command-before" || return 1
    fi
    LIFECYCLE_COMMITTED=no
    LIFECYCLE_POLICY_ACTIVE=no
    trap 'lifecycle_transaction_finish "$?"' EXIT
}

lifecycle_quote_shell() {
    local value=$1
    value=${value//\'/\'\\\'\'}
    printf "'%s'" "$value"
}

lifecycle_policy_install() {
    local original=$LIFECYCLE_TX/policy-original executable=$LIFECYCLE_TX/policy-executable
    local policy=${POLICY_FILE:-/usr/sbin/policy-rc.d}
    mkdir -p -- "${policy%/*}" || return 1
    if [[ -e $policy || -L $policy ]]; then
        cp -a -- "$policy" "$original" || return 1
        if [[ -x $policy ]]; then cp -Lp -- "$policy" "$executable" || return 1; fi
    fi
    LIFECYCLE_POLICY_ACTIVE=yes
    rm -f -- "$policy" || return 1
    {
        # 以下字符串是要写入的脚本代码，必须保留其中的变量引用。
        # shellcheck disable=SC2016
        printf '%s\n' '#!/bin/sh' '# 软件包配置完成前，仅禁止 apcupsd 启停。' 'for argument do' '    case "$argument" in apcupsd|apcupsd.service|/etc/init.d/apcupsd) exit 101 ;; esac' 'done'
        if [[ -f $executable ]]; then
            printf 'exec '; lifecycle_quote_shell "$executable"; printf ' "$@"\n'
        else
            printf '%s\n' 'exit 0'
        fi
    } > "$policy" || return 1
    chmod 755 "$policy"
}

lifecycle_policy_restore() {
    local policy=${POLICY_FILE:-/usr/sbin/policy-rc.d}
    [[ ${LIFECYCLE_POLICY_ACTIVE:-no} == yes ]] || return 0
    rm -f -- "$policy" || return 1
    if [[ -e $LIFECYCLE_TX/policy-original || -L $LIFECYCLE_TX/policy-original ]]; then
        cp -a -- "$LIFECYCLE_TX/policy-original" "$policy" || return 1
    fi
    LIFECYCLE_POLICY_ACTIVE=no
}

lifecycle_record_added_packages() {
    state_packages > "$LIFECYCLE_TX/packages-after.tsv" || return 1
    state_new_packages "$LIFECYCLE_TX/packages-before.tsv" "$LIFECYCLE_TX/packages-after.tsv" "$LIFECYCLE_TX/packages-new.list" || return 1
    state_merge_added_packages "$LIFECYCLE_TX/packages-new.list"
}

lifecycle_restore_auto_marks() {
    local snapshot=$1 package
    while IFS= read -r package; do
        [[ $package =~ ^[a-z0-9][a-z0-9+.-]*(:[a-z0-9]+)?$ ]] || continue
        # apt 命令只显式安装 apcupsd；其他包的手动标记可能已由用户更改。
        [[ ${package%%:*} == apcupsd ]] || continue
        if state_package_installed "$package"; then apt-mark auto "$package" >/dev/null || return 1; fi
    done < "$snapshot"
}

lifecycle_can_purge_apcupsd() {
    local candidates=$1 simulation removed saw_main=no automatic package_name
    simulation=$(LC_ALL=C apt-get -s purge -- apcupsd 2>&1) || {
        err '无法确认监控软件的安全移除范围，已取消卸载。'; return 1;
    }
    automatic=$(apt-mark showauto) || return 1
    while IFS= read -r removed; do
        [[ -n $removed ]] || continue
        package_name=${removed%%:*}
        if [[ $package_name == apcupsd ]]; then saw_main=yes; continue; fi
        if ! awk -v package="$removed" '$0 == package { found=1 } END { exit !found }' "$candidates" ||
           ! awk -v package="$removed" '$0 == package { found=1 } END { exit !found }' <<< "$automatic"; then
            err "卸载监控软件会同时移除其他软件：$removed；请先解除该软件对 apcupsd 的依赖。"; return 1
        fi
    done < <(printf '%s\n' "$simulation" | awk '$1 == "Remv" || $1 == "Purg" { print $2 }')
    [[ $saw_main == yes ]] || { err '软件包模拟结果不完整，已取消卸载。'; return 1; }
}

lifecycle_backup_unowned_config() {
    local backup=$LIFECYCLE_TX/unowned-config directory=${CONFIG_FILE%/*} original_directory relative path package_files
    rm -rf -- "$backup" || return 1
    [[ -d $directory && ! -L $directory ]] || return 0
    cp -a -- "$directory" "$backup" || return 1
    package_files=$(dpkg-query -L apcupsd 2>/dev/null) || {
        err '无法读取监控软件文件归属，已取消清除软件包。'; return 1;
    }
    original_directory=${directory#"${PVE_ROOT:-}"}
    while IFS= read -r path; do
        [[ $path == "$original_directory/"* ]] || continue
        relative=${path#"$original_directory/"}
        [[ $relative != *'/../'* && $relative != ../* && $relative != */.. ]] || return 1
        # 软件包目录中可以存在用户文件，只移除明确属于软件包的文件和链接。
        if [[ -L $backup/$relative || -f $backup/$relative ]]; then rm -f -- "$backup/$relative" || return 1; fi
    done <<< "$package_files"
    # 这些是原生关机临时标记或包内脚本，不能带到下一次部署。
    rm -f -- "$backup/${CONFIG_FILE##*/}" "$backup/powerfail" "$backup/nologin" "$backup/killpower" || return 1
    # 只清理软件包明确拥有的空目录，用户后来创建的空目录也需要保留。
    while IFS= read -r relative; do
        [[ $relative != *'/../'* && $relative != ../* && $relative != */.. ]] || return 1
        if [[ -d $backup/$relative && ! -L $backup/$relative ]]; then
            rmdir -- "$backup/$relative" 2>/dev/null || :
        fi
    done < <(printf '%s\n' "$package_files" |
        awk -v prefix="$original_directory/" 'index($0, prefix) == 1 { print substr($0, length(prefix) + 1) }' |
        LC_ALL=C sort -r)
    rmdir -- "$backup" 2>/dev/null || :
}

lifecycle_restore_unowned_config() {
    local backup=$LIFECYCLE_TX/unowned-config directory=${CONFIG_FILE%/*}
    [[ -d $backup ]] || return 0
    [[ ! -L $directory ]] || return 1
    mkdir -p -- "$directory" || return 1
    cp -a --remove-destination -- "$backup/." "$directory/"
}

lifecycle_purge_apcupsd_preserving_config() {
    local candidates=$1 status=0
    lifecycle_can_purge_apcupsd "$candidates" || return 1
    lifecycle_backup_unowned_config || return 1
    # Debian 的原生 postrm 会删除整个配置目录，必须在其结束后恢复用户文件。
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get purge -y -- apcupsd || status=$?
    lifecycle_restore_unowned_config || return 1
    return "$status"
}

# 清单在整个函数中只读；模拟结果写入的是另一份固定文件。
# shellcheck disable=SC2094
lifecycle_purge_new_packages() {
    local candidates=$1 package simulation removed allowed output
    local automatic=$LIFECYCLE_TX/current-auto.list
    apt-mark showauto > "$automatic" || return 1
    while IFS= read -r package; do
        [[ $package =~ ^[a-z0-9][a-z0-9+.-]*(:[a-z0-9]+)?$ ]] || { err '忽略安装记录中的非法软件包名称。' || :; continue; }
        [[ ${package%%:*} != apcupsd ]] || continue
        state_package_installed "$package" || continue
        # 人工改为手动安装的包留在系统中；模拟结果不得牵连记录外的软件。
        awk -v package="$package" '$0 == package { found=1 } END { exit !found }' "$automatic" || continue
        simulation=$(LC_ALL=C apt-get -s purge -- "$package" 2>&1) || { msg "保留依赖 $package：无法确认安全移除范围。"; continue; }
        output=$LIFECYCLE_TX/remove-simulation.list
        printf '%s\n' "$simulation" | awk '$1 == "Remv" || $1 == "Purg" { print $2 }' > "$output"
        allowed=yes
        while IFS= read -r removed; do
            awk -v package="$removed" '$0 == package { found=1 } END { exit !found }' "$candidates" || allowed=no
            awk -v package="$removed" '$0 == package { found=1 } END { exit !found }' "$automatic" || allowed=no
        done < "$output"
        if [[ $allowed == yes && -s $output ]]; then
            DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get purge -y -- "$package" || return 1
        else
            msg "保留依赖 $package：当前仍被其他软件使用或已转为手动安装。"
        fi
    done < "$candidates"
}

lifecycle_bundle_restore() {
    if [[ -d $LIFECYCLE_TX/bundle-before ]]; then
        rm -rf -- "$INSTALL_DIR" || return 1
        cp -a -- "$LIFECYCLE_TX/bundle-before" "$INSTALL_DIR" || return 1
    elif [[ -f $INSTALL_DIR/.pve-ups-owned && $(cat "$INSTALL_DIR/.pve-ups-owned") == pve-ups-v1 ]]; then
        rm -rf -- "$INSTALL_DIR" || return 1
    fi
    if [[ -e $LIFECYCLE_TX/command-before || -L $LIFECYCLE_TX/command-before ]]; then
        rm -f -- "$COMMAND_PATH" || return 1
        cp -a -- "$LIFECYCLE_TX/command-before" "$COMMAND_PATH" || return 1
    elif [[ -L $COMMAND_PATH && $(readlink "$COMMAND_PATH") == "$INSTALL_DIR/bin/pve-ups" ]]; then
        rm -f -- "$COMMAND_PATH" || return 1
    fi
}

lifecycle_transaction_finish() {
    local status=$1 rollback_failed=no
    trap - EXIT
    if [[ ${LIFECYCLE_COMMITTED:-no} != yes ]]; then
        err '操作未完成，正在恢复操作前的配置和服务状态。' || :
        # 即便供电不允许回滚，也记录软件包增量，便于来电后修复或卸载。
        lifecycle_record_added_packages || rollback_failed=yes
        # 失败时先重新确认市电，避免在新发生的停电中停止正在工作的监控。
        if guard_mains; then
            if ! awk -F '\t' '$1 == "apcupsd" || $1 ~ /^apcupsd:/ { found=1 } END { exit !found }' "$LIFECYCLE_TX/packages-before.tsv" && state_package_installed apcupsd; then
                lifecycle_purge_apcupsd_preserving_config "$LIFECYCLE_TX/packages-new.list" || rollback_failed=yes
            fi
            lifecycle_purge_new_packages "$LIFECYCLE_TX/packages-new.list" || rollback_failed=yes
            lifecycle_restore_auto_marks "$LIFECYCLE_TX/auto-before.list" || rollback_failed=yes
            state_restore_files "$LIFECYCLE_TX/files" || rollback_failed=yes
            state_restore_service "$LIFECYCLE_TX/service.tsv" || rollback_failed=yes
            lifecycle_bundle_restore || rollback_failed=yes
        else
            rollback_failed=yes
            err '当前供电不安全，已保留恢复记录，避免停止电池供电期间的监控服务。' || :
        fi
    fi
    lifecycle_policy_restore || rollback_failed=yes
    if [[ -n ${LIFECYCLE_BUNDLE_STAGING:-} && -d $LIFECYCLE_BUNDLE_STAGING ]]; then
        rm -rf -- "$LIFECYCLE_BUNDLE_STAGING" || rollback_failed=yes
    fi
    if [[ $rollback_failed == yes ]]; then
        err "部分恢复未完成，记录保留在：$LIFECYCLE_TX" || :
        return 1
    fi
    rm -rf -- "$LIFECYCLE_TX"
    return "$status"
}

lifecycle_check_bundle_ownership() {
    if [[ -e $COMMAND_PATH || -L $COMMAND_PATH ]]; then
        [[ -L $COMMAND_PATH && $(readlink "$COMMAND_PATH") == "$INSTALL_DIR/bin/pve-ups" ]] || {
            err '管理命令已被其他文件替换，无法安全覆盖。'; return 1;
        }
    fi
    if [[ -e $INSTALL_DIR ]]; then
        [[ -d $INSTALL_DIR && -f $INSTALL_DIR/.pve-ups-owned && $(cat "$INSTALL_DIR/.pve-ups-owned") == pve-ups-v1 ]] || {
            err '安装目录的归属标记不匹配，无法安全覆盖。'; return 1;
        }
    fi
}

lifecycle_deploy_bundle() {
    local staging item
    [[ -f $APP_ROOT/bin/pve-ups ]] || { err '安装包缺少管理入口。'; return 1; }
    lifecycle_check_bundle_ownership || return 1
    if [[ $APP_ROOT == "$INSTALL_DIR" ]]; then
        [[ -f $INSTALL_DIR/.pve-ups-owned && $(cat "$INSTALL_DIR/.pve-ups-owned") == pve-ups-v1 ]] || {
            err '安装目录的归属标记不匹配，无法安全更新管理入口。'; return 1;
        }
        mkdir -p -- "${COMMAND_PATH%/*}" || return 1
        ln -sfn -- "$INSTALL_DIR/bin/pve-ups" "$COMMAND_PATH"
        return
    fi
    mkdir -p -- "${INSTALL_DIR%/*}" || return 1
    staging=$(mktemp -d "${INSTALL_DIR}.new.XXXXXX") || return 1
    LIFECYCLE_BUNDLE_STAGING=$staging
    chmod 755 "$staging" || return 1
    for item in VERSION bin lib bootstrap.sh README.md; do
        if [[ -e $APP_ROOT/$item ]]; then cp -a -- "$APP_ROOT/$item" "$staging/$item" || { rm -rf -- "$staging"; return 1; }; fi
    done
    printf '%s\n' pve-ups-v1 > "$staging/.pve-ups-owned"
    chmod 755 "$staging/bin/pve-ups" || { rm -rf -- "$staging"; return 1; }
    if [[ -e $INSTALL_DIR ]]; then
        [[ -f $INSTALL_DIR/.pve-ups-owned && $(cat "$INSTALL_DIR/.pve-ups-owned") == pve-ups-v1 ]] || { rm -rf -- "$staging"; return 1; }
        rm -rf -- "$INSTALL_DIR" || { rm -rf -- "$staging"; return 1; }
    fi
    mv -- "$staging" "$INSTALL_DIR" || return 1
    LIFECYCLE_BUNDLE_STAGING=''
    mkdir -p -- "${COMMAND_PATH%/*}" || return 1
    ln -sfn -- "$INSTALL_DIR/bin/pve-ups" "$COMMAND_PATH"
}

lifecycle_write_defaults() {
    local temporary
    mkdir -p -- "${DEFAULTS_FILE%/*}" || return 1
    temporary=$(mktemp "${DEFAULTS_FILE}.XXXXXX") || return 1
    if [[ -f $DEFAULTS_FILE ]]; then
        cp -p -- "$DEFAULTS_FILE" "$temporary" || return 1
        awk '/^[[:space:]]*ISCONFIGURED=/ { if (!written++) print "ISCONFIGURED=yes"; next } { print } END { if (!written) print "ISCONFIGURED=yes" }' "$DEFAULTS_FILE" > "$temporary" || return 1
    else
        printf '%s\n' '# 由不间断电源管理工具配置。' 'ISCONFIGURED=yes' > "$temporary" || return 1
        chmod 644 "$temporary" || return 1
    fi
    mv -f -- "$temporary" "$DEFAULTS_FILE"
}

lifecycle_write_config() {
    local source=$1 temporary
    mkdir -p -- "${CONFIG_FILE%/*}" || return 1
    temporary=$(mktemp "${CONFIG_FILE}.XXXXXX") || return 1
    if [[ -f $CONFIG_FILE ]]; then cp -p -- "$CONFIG_FILE" "$temporary" || return 1; else chmod 644 "$temporary" || return 1; fi
    cat "$source" > "$temporary" || { rm -f -- "$temporary"; return 1; }
    mv -f -- "$temporary" "$CONFIG_FILE"
}

lifecycle_configure_event_notifications() {
    local directory=${CONFIG_FILE%/*} file control temporary prepared
    file=$directory/config
    control=$directory/apccontrol
    LIFECYCLE_EVENTS_CONFIG_CHANGED=no
    guard_mains || return 1
    if [[ ! -f $control ]] || ! awk 'index($0, "SCRIPTDIR/config") || index($0, "SCRIPTDIR}/config") {found=1} END {exit !found}' "$control"; then
        err '原生事件脚本未提供 config 配置入口，无法安全设置菜单事件通知。'; return 1;
    fi
    if [[ -e $file || -L $file ]]; then
        [[ -f $file && ! -L $file ]] || { err '事件通知配置必须是普通文件，已取消修改。'; return 1; }
        /bin/sh -n "$file" || { err '原有事件通知配置存在语法错误，已取消修改。'; return 1; }
    fi
    temporary=$(mktemp "$directory/.pve-ups-events.XXXXXX") || return 1
    if [[ -f $file ]]; then
        if ! state_strip_event_notifications "$file" "$temporary" || ! chmod --reference="$file" "$temporary" || ! chown --reference="$file" "$temporary"; then
            rm -f -- "$temporary"; return 1
        fi
    else
        chmod 644 "$temporary" || { rm -f -- "$temporary"; return 1; }
    fi
    # config 在每个原生事件发生时读取；仅静默常规广播，关机和断电分支保持原样。
    cat >> "$temporary" <<'EOF'
# BEGIN pve-ups: event-notifications
case "${1:-}" in
    onbattery|offbattery|commfailure|commok) WALL=/bin/true ;;
esac
case "${1:-}" in
    onbattery|offbattery)
        if [ -x /usr/local/sbin/pve-ups ]; then
            /usr/local/sbin/pve-ups history-record "${1}" >/dev/null 2>&1 &
        fi
        ;;
esac
# END pve-ups: event-notifications
EOF
    if ! /bin/sh -n "$temporary" || ! guard_mains || ! mv -f -- "$temporary" "$file"; then
        rm -f -- "$temporary"; return 1
    fi
    # 事件始终写入 apcupsd 原生日志；不建立额外日志或后台进程。
    if [[ -z $(config_get EVENTSFILE) ]]; then
        prepared=$(mktemp "$directory/.pve-ups-event-log.XXXXXX") || return 1
        if ! awk '$1 == "EVENTSFILE" {if (!written++) print "EVENTSFILE /var/log/apcupsd.events"; next} {print} END {if (!written) print "EVENTSFILE /var/log/apcupsd.events"}' "$CONFIG_FILE" > "$prepared" ||
           ! guard_mains || ! lifecycle_write_config "$prepared"; then
            rm -f -- "$prepared"; return 1
        fi
        rm -f -- "$prepared"
        # 由管理工具更新模块读取，决定是否需要重新加载新增的日志配置。
        # shellcheck disable=SC2034
        LIFECYCLE_EVENTS_CONFIG_CHANGED=yes
    fi
}

lifecycle_check_shutdown_hook() {
    local hook paths
    paths=$(dpkg-query -L apcupsd 2>/dev/null) || { msg '提示：无法读取软件包文件清单，请检查原生关机末期断电脚本。'; return 0; }
    while IFS= read -r hook; do
        case $hook in */system-shutdown/apcupsd_shutdown|*/system-shutdown/apcupsd)
            [[ -x $hook ]] && { msg '已检测到软件包提供的关机末期断电脚本。'; return 0; } ;;
        esac
    done <<< "$paths"
    msg '提示：未发现可执行的原生关机末期断电脚本；宿主机关机与电源切断输出需分别验收。'
}

lifecycle_wait_for_ups() {
    local attempt status
    # USB 初始化可能稍晚于服务启动，最多等待五次，不使用无限等待。
    for attempt in 1 2 3 4 5; do
        if read_ups_snapshot; then
            status=$(ups_value STATUS)
            if [[ -n $status && $status != *COMMLOST* ]]; then return 0; fi
        fi
        if [[ $attempt != 5 ]]; then sleep 1; fi
    done
    err '服务已启动，但暂未确认电源通信；已保留安装与恢复记录，请检查 USB 和详细状态。'
}

lifecycle_show_upgrade_versions() {
    local policy installed candidate
    policy=$(LC_ALL=C apt-cache policy apcupsd 2>/dev/null) || {
        err '无法查询监控软件当前版本和候选版本，已取消升级。'; return 1;
    }
    installed=$(awk '$1 == "Installed:" { print $2; exit }' <<< "$policy")
    candidate=$(awk '$1 == "Candidate:" { print $2; exit }' <<< "$policy")
    [[ -n $installed && -n $candidate && $candidate != '(none)' ]] || {
        err '软件源未提供有效的监控软件候选版本，已取消升级。'; return 1;
    }
    msg "当前监控软件版本：$installed"
    msg "软件源可用版本：$candidate"
}

lifecycle_install() (
    local prepared installed_before=no
    require_root && require_pve && state_validate_paths && guard_mains || return 1
    local legacy_command=${LEGACY_COMMAND_PATH:-${PVE_ROOT:-}/usr/local/bin/pve-ups}
    local legacy_timer=${LEGACY_TIMER_PATH:-${PVE_ROOT:-}/etc/systemd/system/pve-ups-hid.timer}
    if [[ -e $legacy_command || -L $legacy_command || -e $legacy_timer ]]; then
        err '检测到旧版电源监控工具，请先停用并卸载旧工具，避免重复关机监控。'; return 1
    fi
    lifecycle_check_bundle_ownership || return 1
    lifecycle_create_baseline || return 1
    lifecycle_transaction_begin || return 1
    state_package_installed apcupsd && installed_before=yes
    lifecycle_policy_install || return 1
    msg '正在刷新软件索引并安装监控软件；只操作 apcupsd 及其必要依赖。'
    apt-get update || return 1
    guard_mains || return 1
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold apcupsd || return 1
    lifecycle_record_added_packages || return 1
    prepared=$LIFECYCLE_TX/prepared.conf
    prepare_config "$CONFIG_FILE" "$prepared" || return 1
    if [[ $installed_before == no ]]; then
        config_set TIMEOUT 1800 "$prepared" || return 1
        config_set BATTERYLEVEL 50 "$prepared" || return 1
        config_set MINUTES 30 "$prepared" || return 1
    fi
    validate_config_file "$prepared" || return 1
    # 新装软件的初始默认配置不会在此之前启动。
    guard_mains || return 1
    lifecycle_write_config "$prepared" && lifecycle_write_defaults && lifecycle_configure_event_notifications || return 1
    # 通信暂不可确认时仍保留本地诊断和卸载入口，便于断网恢复。
    lifecycle_deploy_bundle || return 1
    systemctl daemon-reload || return 1
    systemctl unmask apcupsd.service && systemctl enable apcupsd.service || return 1
    lifecycle_policy_restore || return 1
    guard_mains || return 1
    systemctl restart apcupsd.service || return 1
    systemctl is-active --quiet apcupsd.service || { err '监控服务未正常运行。'; return 1; }
    lifecycle_wait_for_ups || return 1
    lifecycle_check_shutdown_hook
    state_put "$STATE_DIR/manifest.tsv" current_version "$APP_VERSION" || return 1
    state_put "$STATE_DIR/manifest.tsv" deployment_state installed || return 1
    LIFECYCLE_COMMITTED=yes
    msg '监控软件已安装并启用；请在详细状态页确认电源通信正常。'
)

lifecycle_upgrade() (
    require_root && require_pve && state_validate_paths && lifecycle_is_managed && guard_mains || return 1
    state_package_installed apcupsd || { err '监控软件尚未安装，请先执行安装。'; return 1; }
    lifecycle_transaction_begin || return 1
    lifecycle_policy_install || return 1
    msg '正在升级监控软件，保留当前参数；不会执行全系统升级。'
    apt-get update || return 1
    lifecycle_show_upgrade_versions || return 1
    guard_mains || return 1
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --only-upgrade --no-install-recommends -o Dpkg::Options::=--force-confold apcupsd || return 1
    lifecycle_record_added_packages || return 1
    lifecycle_configure_event_notifications || return 1
    lifecycle_policy_restore || return 1
    guard_mains || return 1
    state_restore_service "$LIFECYCLE_TX/service.tsv" || return 1
    # 运行中的旧进程必须重启，以加载升级后的程序。
    if [[ $(state_get "$LIFECYCLE_TX/service.tsv" service_active) == active ]]; then
        guard_mains && systemctl restart apcupsd.service && systemctl is-active --quiet apcupsd.service || return 1
        lifecycle_wait_for_ups || return 1
    fi
    lifecycle_check_shutdown_hook
    LIFECYCLE_COMMITTED=yes
    msg '监控软件升级完成，配置和服务开机设置已保留。'
)

lifecycle_remove_owned_bundle() {
    if [[ -L $COMMAND_PATH && $(readlink "$COMMAND_PATH") == "$INSTALL_DIR/bin/pve-ups" ]]; then
        rm -f -- "$COMMAND_PATH" || return 1
    elif [[ -e $COMMAND_PATH || -L $COMMAND_PATH ]]; then
        msg '管理命令已被修改，保留该文件。'
    fi
    if [[ -d $INSTALL_DIR && -f $INSTALL_DIR/.pve-ups-owned && $(cat "$INSTALL_DIR/.pve-ups-owned") == pve-ups-v1 ]]; then
        rm -rf -- "$INSTALL_DIR" || return 1
    elif [[ -e $INSTALL_DIR ]]; then
        msg '安装目录的归属标记不匹配，保留该目录。'
    fi
}

lifecycle_uninstall() (
    local originally_present
    require_root && require_pve && state_validate_paths || return 1
    if [[ ! -f $STATE_DIR/manifest.tsv && ! -e $COMMAND_PATH && ! -d $INSTALL_DIR ]]; then
        msg '本工具已卸载，无需重复清理。'; return 0
    fi
    lifecycle_is_managed && guard_mains || return 1
    originally_present=$(state_get "$STATE_DIR/manifest.tsv" apcupsd_present) || return 1
    if [[ $originally_present == yes ]] && ! state_package_installed apcupsd; then
        err '原有监控软件已被外部移除，无法在断网卸载时恢复软件包；请先执行安装或修复。'; return 1
    fi
    if [[ $originally_present == no ]] && state_package_installed apcupsd; then
        lifecycle_can_purge_apcupsd "$STATE_DIR/packages-added.list" || return 1
    fi
    lifecycle_transaction_begin || return 1
    lifecycle_policy_install || return 1
    systemctl stop apcupsd.service || return 1
    if [[ $originally_present == no ]]; then
        systemctl disable apcupsd.service >/dev/null 2>&1 || :
        if state_package_installed apcupsd; then
            msg '正在清除本项目安装的监控软件；卸载不刷新软件索引。'
            lifecycle_purge_apcupsd_preserving_config "$STATE_DIR/packages-added.list" || return 1
        fi
        lifecycle_purge_new_packages "$STATE_DIR/packages-added.list" || return 1
    elif [[ $originally_present != yes ]]; then
        err '首次安装记录不完整，已取消卸载。'; return 1
    fi
    state_restore_files "$STATE_DIR/baseline" || return 1
    lifecycle_restore_auto_marks "$STATE_DIR/packages-auto-before.list" || return 1
    lifecycle_policy_restore || return 1
    systemctl daemon-reload || return 1
    state_restore_service "$STATE_DIR/manifest.tsv" || return 1
    lifecycle_remove_owned_bundle || return 1
    LIFECYCLE_COMMITTED=yes
    # 生命周期 trap 还需要交易目录；先显式结束，再清除持久状态目录。
    lifecycle_transaction_finish 0 || return 1
    rm -rf -- "$STATE_DIR" || return 1
    msg '卸载完成，已恢复首次部署前的配置及服务状态。'
)

service_action() {
    local action=$1
    require_root && require_pve && state_validate_paths && lifecycle_is_managed || return 1
    case $action in
        start) guard_mains && systemctl start apcupsd.service ;;
        stop|restart) guard_mains && systemctl "$action" apcupsd.service ;;
        enable) guard_mains && systemctl enable apcupsd.service ;;
        disable) guard_mains && systemctl disable apcupsd.service ;;
        *) err '不支持的服务操作。'; return 1 ;;
    esac
}

lifecycle_tool_update() {
    require_root && require_pve && state_validate_paths && lifecycle_is_managed && guard_mains || return 1
    if declare -F bootstrap_tool_update >/dev/null; then
        bootstrap_tool_update
    else
        err '当前安装包未提供管理工具更新入口，请使用发布版更新命令。'; return 1
    fi
}
