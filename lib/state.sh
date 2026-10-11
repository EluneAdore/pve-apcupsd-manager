#!/usr/bin/env bash
# 安装状态只使用固定格式的数据文件；绝不执行状态文件中的内容。

state_get() {
    local file=$1 key=$2
    [[ -f $file && ! -L $file ]] || return 1
    awk -F '\t' -v key="$key" '$1 == key { print $2; found=1; exit } END { if (!found) exit 1 }' "$file"
}

state_put() {
    local file=$1 key=$2 value=$3 temporary
    [[ $key =~ ^[a-z_]+$ && $value != *$'\n'* && $value != *$'\t'* ]] || return 1
    temporary=$(mktemp "${file}.XXXXXX") || return 1
    if [[ -f $file ]]; then
        awk -F '\t' -v key="$key" '$1 != key' "$file" > "$temporary" || { rm -f -- "$temporary"; return 1; }
    fi
    printf '%s\t%s\n' "$key" "$value" >> "$temporary"
    chmod 600 "$temporary"
    mv -f -- "$temporary" "$file"
}

state_packages() {
    dpkg-query -W -f='${binary:Package}\t${Status}\t${Version}\n' 2>/dev/null |
        awk -F '\t' '$2 == "install ok installed" { print $1 "\t" $3 }' | LC_ALL=C sort -u
}

state_package_installed() {
    state_packages | awk -F '\t' -v package="$1" '{ split($1, name, ":"); if (name[1] == package) found=1 } END { exit !found }'
}

state_new_packages() {
    local before=$1 after=$2 output=$3
    awk -F '\t' 'FILENAME == ARGV[1] { original[$1]=1; next } !($1 in original) { print $1 }' "$before" "$after" |
        LC_ALL=C sort -u > "$output"
}

state_merge_added_packages() {
    local new_file=$1 merged
    merged=$(mktemp "$STATE_DIR/.added.XXXXXX") || return 1
    { [[ ! -f $STATE_DIR/packages-added.list ]] || cat "$STATE_DIR/packages-added.list"; cat "$new_file"; } |
        LC_ALL=C sort -u > "$merged"
    chmod 600 "$merged"
    mv -f -- "$merged" "$STATE_DIR/packages-added.list"
}

state_service_snapshot() {
    local file=$1 enabled active prefix relative entry
    enabled=$(systemctl is-enabled apcupsd.service 2>/dev/null) || :
    active=$(systemctl is-active apcupsd.service 2>/dev/null) || :
    case $enabled in enabled|enabled-runtime|disabled|static|indirect|masked|masked-runtime|not-found|alias|generated|transient) ;; *) enabled=not-found ;; esac
    case $active in active|inactive|failed|activating|deactivating) ;; *) active=inactive ;; esac
    state_put "$file" service_enabled "$enabled" || return 1
    state_put "$file" service_active "$active" || return 1
    : > "$file.links.list"
    # 屏蔽状态会隐藏开机启动链接，单独保存这些链接以便准确恢复。
    if [[ $enabled == masked || $enabled == masked-runtime ]]; then
        (
            shopt -s globstar nullglob
            for prefix in etc/systemd/system run/systemd/system; do
                for entry in "${PVE_ROOT:-}/$prefix"/**; do
                    [[ -L $entry ]] || continue
                    case $(readlink "$entry") in */apcupsd.service|apcupsd.service) ;; *) continue ;; esac
                    relative=${entry#"${PVE_ROOT:-}/"}
                    [[ $relative != *$'\n'* && $relative != *$'\t'* ]] || return 1
                    mkdir -p -- "$file.links/${relative%/*}" || return 1
                    cp -a -- "$entry" "$file.links/$relative" || return 1
                    printf '%s\n' "$relative" >> "$file.links.list" || return 1
                done
            done
            for entry in "${PVE_ROOT:-}"/etc/rc[0-6S].d/*apcupsd; do
                [[ -L $entry ]] || continue
                relative=${entry#"${PVE_ROOT:-}/"}
                mkdir -p -- "$file.links/${relative%/*}" || return 1
                cp -a -- "$entry" "$file.links/$relative" || return 1
                printf '%s\n' "$relative" >> "$file.links.list" || return 1
            done
        ) || return 1
    fi
}

state_copy_path() {
    local source=$1 destination=$2 manifest=$3 key=$4
    if [[ -e $source || -L $source ]]; then
        cp -a -- "$source" "$destination" || return 1
        state_put "$manifest" "$key" present
    else
        state_put "$manifest" "$key" absent
    fi
}

state_snapshot_files() {
    local directory=$1
    mkdir -p -- "$directory" || return 1
    chmod 700 "$directory"
    state_copy_path "${CONFIG_FILE%/*}" "$directory/apcupsd-directory" "$directory/files.tsv" config_directory || return 1
    state_copy_path "$DEFAULTS_FILE" "$directory/defaults" "$directory/files.tsv" defaults
}

state_strip_event_notifications() {
    local file=$1 output=$2
    # 只移除本工具的独立配置块；不执行原文件，也不触碰用户的事件脚本。
    awk '
        $0 == "# BEGIN pve-ups: event-notifications" {
            if (inside || ++starts > 1) invalid=1
            inside=1; next
        }
        $0 == "# END pve-ups: event-notifications" {
            if (!inside || ++ends > 1) invalid=1
            inside=0; next
        }
        !inside { print }
        END { if (invalid || inside || starts != ends) exit 1 }
    ' "$file" > "$output"
}

state_remove_event_notifications() {
    local file=$1 temporary
    [[ -e $file || -L $file ]] || return 0
    [[ -f $file && ! -L $file ]] || return 0
    awk '$0 == "# BEGIN pve-ups: event-notifications" {found=1} END {exit !found}' "$file" || return 0
    temporary=$(mktemp "${file}.XXXXXX") || return 1
    if ! state_strip_event_notifications "$file" "$temporary"; then
        rm -f -- "$temporary"; err '事件通知配置标记不完整，已保留原文件。'; return 1
    fi
    if awk 'NF {found=1} END {exit found}' "$temporary"; then
        # 初次不存在 config 时，只删除本工具生成且没有后来新增内容的文件。
        rm -f -- "$temporary" "$file"
    else
        if ! chmod --reference="$file" "$temporary" || ! chown --reference="$file" "$temporary" || ! mv -f -- "$temporary" "$file"; then
            rm -f -- "$temporary"; return 1
        fi
    fi
}

state_restore_files() {
    local directory=$1 configuration_directory=${CONFIG_FILE%/*}
    [[ -f $directory/files.tsv ]] || return 1
    # 旧版本的首次基线可能没有 config；先清除新增块，再按原有快照恢复。
    state_remove_event_notifications "$configuration_directory/config" || return 1
    # 路径由受信任的程序常量指定；状态文件只能声明存在或不存在。
    case $(state_get "$directory/files.tsv" config_directory) in
        present)
            [[ -e $directory/apcupsd-directory || -L $directory/apcupsd-directory ]] || return 1
            if [[ -L $directory/apcupsd-directory ]]; then
                [[ ! -d $configuration_directory || -L $configuration_directory ]] || return 1
                rm -f -- "$configuration_directory" || return 1
                cp -a -- "$directory/apcupsd-directory" "$configuration_directory" || return 1
            else
                [[ ! -L $configuration_directory ]] || return 1
                mkdir -p -- "$configuration_directory" || return 1
                cp -a --remove-destination -- "$directory/apcupsd-directory/." "$configuration_directory/" || return 1
            fi ;;
        absent)
            # 只移除本工具管理的配置，保留后来增加的无关文件。
            rm -f -- "$CONFIG_FILE" || return 1
            if [[ -d $configuration_directory ]]; then rmdir -- "$configuration_directory" 2>/dev/null || :; fi ;;
        *) return 1 ;;
    esac
    case $(state_get "$directory/files.tsv" defaults) in
        present)
            [[ -e $directory/defaults || -L $directory/defaults ]] || return 1
            mkdir -p -- "${DEFAULTS_FILE%/*}" || return 1
            rm -f -- "$DEFAULTS_FILE" || return 1
            cp -a -- "$directory/defaults" "$DEFAULTS_FILE" || return 1 ;;
        absent) rm -f -- "$DEFAULTS_FILE" || return 1 ;;
        *) return 1 ;;
    esac
}

state_restore_service() {
    local manifest=$1 enabled active relative destination
    enabled=$(state_get "$manifest" service_enabled) || return 1
    active=$(state_get "$manifest" service_active) || return 1
    # 先解除本次部署可能改变的屏蔽状态，然后重建首次安装前的状态。
    if [[ $enabled != not-found ]]; then
        systemctl unmask apcupsd.service >/dev/null 2>&1 || return 1
        case $enabled in
            enabled) systemctl enable apcupsd.service >/dev/null 2>&1 || return 1 ;;
            enabled-runtime) systemctl enable --runtime apcupsd.service >/dev/null 2>&1 || return 1 ;;
            disabled) systemctl disable apcupsd.service >/dev/null 2>&1 || return 1 ;;
            masked|masked-runtime)
                systemctl disable apcupsd.service >/dev/null 2>&1 || return 1
                [[ -f $manifest.links.list ]] || return 1
                (
                    shopt -s nullglob
                    for destination in "${PVE_ROOT:-}"/etc/rc[0-6S].d/*apcupsd; do
                        [[ ! -L $destination ]] || rm -f -- "$destination" || return 1
                    done
                ) || return 1
                while IFS= read -r relative; do
                    case $relative in etc/systemd/system/*|run/systemd/system/*|etc/rc[0-6S].d/*apcupsd) ;; *) return 1 ;; esac
                    [[ $relative != *'/../'* && $relative != */.. && -L $manifest.links/$relative ]] || return 1
                    destination=${PVE_ROOT:-}/$relative
                    mkdir -p -- "${destination%/*}" || return 1
                    rm -f -- "$destination" || return 1
                    cp -a -- "$manifest.links/$relative" "$destination" || return 1
                done < "$manifest.links.list" ;;
            static|indirect|alias|generated|transient) : ;;
            *) return 1 ;;
        esac
        if [[ $active == active || $active == activating ]]; then
            systemctl start apcupsd.service || return 1
        else
            systemctl stop apcupsd.service || return 1
        fi
        # 被屏蔽的服务也可能在屏蔽前已启动；先恢复运行状态，再恢复屏蔽。
        case $enabled in
            masked) systemctl mask apcupsd.service >/dev/null 2>&1 || return 1 ;;
            masked-runtime) systemctl mask --runtime apcupsd.service >/dev/null 2>&1 || return 1 ;;
        esac
    fi
}

state_validate_paths() {
    local path first second
    for path in "$STATE_DIR" "$CONFIG_FILE" "$DEFAULTS_FILE" "$INSTALL_DIR" "$COMMAND_PATH"; do
        [[ $path == /* && $path != / && $path != *$'\n'* && $path != *$'\t'* && $path != *'/../'* && $path != */.. ]] || {
            err '安装路径不合法，已取消操作。'; return 1;
        }
    done
    # 防止错误的环境映射把整个系统配置目录作为可恢复目录。
    [[ ${CONFIG_FILE%/*} != /etc && ${CONFIG_FILE%/*} != / ]] || {
        err '监控配置必须位于专属目录中。'; return 1;
    }
    [[ ! -L $STATE_DIR && ! -L $INSTALL_DIR ]] || { err '状态目录和安装目录不能是符号链接。'; return 1; }
    [[ ! -L ${CONFIG_FILE%/*} ]] || { err '监控配置目录不能是符号链接，避免修改未备份的外部目录。'; return 1; }
    for first in "$STATE_DIR" "$INSTALL_DIR"; do
        for second in "$STATE_DIR" "$INSTALL_DIR" "${CONFIG_FILE%/*}"; do
            [[ $first != "$second" ]] || continue
            if [[ $first == "$second/"* || $second == "$first/"* ]]; then
                err '安装、配置和状态目录不能互相包含。'; return 1
            fi
        done
    done
    [[ $STATE_DIR != "$INSTALL_DIR" && $STATE_DIR != "${CONFIG_FILE%/*}" && $INSTALL_DIR != "${CONFIG_FILE%/*}" ]] || {
        err '安装、配置和状态目录必须相互独立。'; return 1;
    }
}
