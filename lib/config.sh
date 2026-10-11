#!/usr/bin/env bash
# 参数原子写入、合法性检查及应用失败回滚。

config_get() {
    local key=$1 file=${2:-$CONFIG_FILE}
    [[ -r $file ]] || return 0
    awk -v key="$key" '$1==key {sub(/^[^[:space:]]+[[:space:]]*/, ""); sub(/[[:space:]]+#.*$/, ""); sub(/[[:space:]]*$/, ""); value=$0} END {print value}' "$file"
}

validate_value() {
    local key=$1 value=$2 n
    case $key in
        TIMEOUT|BATTERYLEVEL|MINUTES|ONBATTERYDELAY|POLLTIME|NISPORT|KILLDELAY)
            [[ $value =~ ^[0-9]{1,6}$ ]] || { err "$key 必须是非负整数。"; return 1; }
            n=$((10#$value))
            case $key in
                TIMEOUT) ((n<=86400)) || { err '等待时间必须为 0～1440 分钟。'; return 1; } ;;
                BATTERYLEVEL) ((n>=1 && n<=100)) || { err '电量保护阈值必须为 1～100%。'; return 1; } ;;
                MINUTES) ((n>=1 && n<=1440)) || { err '续航保护阈值必须为 1～1440 分钟。'; return 1; } ;;
                ONBATTERYDELAY|KILLDELAY) ((n==0)) || { err '本方案要求停电事件延迟及提前切断输出延迟均为 0。'; return 1; } ;;
                POLLTIME) ((n>=1 && n<=300)) || { err '查询间隔必须为 1～300 秒。'; return 1; } ;;
                NISPORT) ((n>=1 && n<=65535)) || { err '本地状态端口无效。'; return 1; } ;;
            esac ;;
        UPSNAME) [[ $value =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || { err '设备名称请使用 1～32 个字母、数字、点、横线或下划线。'; return 1; } ;;
        UPSCABLE|UPSTYPE) [[ $value == usb ]] || { err '本方案采用 USB 连接。'; return 1; } ;;
        DEVICE) [[ -z $value ]] || { err 'USB 自动识别时设备路径必须留空。'; return 1; } ;;
        NETSERVER) [[ $value == on ]] || { err '本地状态查询必须启用。'; return 1; } ;;
        NISIP) [[ $value == 127.0.0.1 ]] || { err '状态查询仅监听本机地址。'; return 1; } ;;
        UPSCLASS) [[ $value == standalone ]] || return 1 ;;
        UPSMODE) [[ $value == disable ]] || return 1 ;;
        *) err "暂不支持修改此参数：$key。"; return 1 ;;
    esac
}

config_set() {
    local key=$1 value=$2 file=${3:-$CONFIG_FILE} temp
    validate_value "$key" "$value" || return 1
    [[ -f $file && ! -L $file ]] || { err "配置文件不存在或为符号链接：$file。"; return 1; }
    temp=$(mktemp "$(dirname -- "$file")/.pve-ups-config.XXXXXX") || return 1
    if ! awk -v key="$key" -v value="$value" '
        $1==key {if (!found++) print key " " value; next} {print}
        END {if (!found) print key " " value}
    ' "$file" > "$temp" || ! chmod --reference="$file" "$temp" ||
        ! chown --reference="$file" "$temp" || ! mv -f -- "$temp" "$file"; then
        rm -f -- "$temp"; return 1
    fi
}

validate_config_file() {
    local file=$1 key value count
    [[ -f $file && ! -L $file ]] || { err '配置必须是普通文件。'; return 1; }
    # 配置只按数据读取；本工具只允许本机 USB 监控模式。
    for key in UPSCABLE UPSTYPE DEVICE UPSCLASS UPSMODE NETSERVER NISIP NISPORT TIMEOUT BATTERYLEVEL MINUTES ONBATTERYDELAY KILLDELAY; do
        count=$(awk -v key="$key" '$1==key {n++} END {print n+0}' "$file")
        [[ $count == 1 ]] || { err "$key 必须且只能出现一次。"; return 1; }
        value=$(config_get "$key" "$file")
        validate_value "$key" "$value" || return 1
    done
    for key in SCRIPTDIR PWRFAILDIR LOCKFILE NOLOGINDIR; do
        count=$(awk -v key="$key" '$1==key {n++} END {print n+0}' "$file")
        [[ $count == 1 ]] || { err "$key 必须且只能出现一次。"; return 1; }
        value=$(config_get "$key" "$file")
        case $key:$value in
            SCRIPTDIR:/etc/apcupsd|PWRFAILDIR:/etc/apcupsd|LOCKFILE:/var/lock|LOCKFILE:/run/lock|NOLOGINDIR:/etc|NOLOGINDIR:/etc/apcupsd) ;;
            *) err "$key 不符合本方案的原生关机目录配置。"; return 1 ;;
        esac
    done
}

prepare_config() {
    local source_file=$1 output=$2 key value
    if [[ -f $source_file && ! -L $source_file ]]; then
        cp -a -- "$source_file" "$output" || return 1
    else
        printf '%s\n' '# 本机 APC USB 监控配置，由不间断电源管理工具维护。' > "$output" || return 1
        chmod 640 "$output" || return 1
    fi
    for key in UPSCABLE UPSTYPE DEVICE UPSCLASS UPSMODE NETSERVER NISIP NISPORT ONBATTERYDELAY KILLDELAY; do
        case $key in
            UPSCABLE|UPSTYPE) value=usb ;;
            DEVICE) value='' ;;
            UPSCLASS) value=standalone ;;
            UPSMODE) value=disable ;;
            NETSERVER) value=on ;;
            NISIP) value=127.0.0.1 ;;
            NISPORT) value=3551 ;;
            ONBATTERYDELAY|KILLDELAY) value=0 ;;
        esac
        config_set "$key" "$value" "$output" || return 1
    done
    for key in TIMEOUT BATTERYLEVEL MINUTES; do
        value=$(config_get "$key" "$output")
        if [[ -z $value ]]; then
            case $key in TIMEOUT) value=1800 ;; BATTERYLEVEL) value=5 ;; MINUTES) value=3 ;; esac
            config_set "$key" "$value" "$output" || return 1
        fi
    done
    # 安装器传入新配置时必须保留完整关机目录，供发行版晚期断电钩子使用。
    for key in LOCKFILE SCRIPTDIR PWRFAILDIR NOLOGINDIR; do
        if [[ $key == NOLOGINDIR && -n $(config_get "$key" "$output") ]]; then continue; fi
        case $key in LOCKFILE) value=/var/lock ;; SCRIPTDIR) value=/etc/apcupsd ;; PWRFAILDIR|NOLOGINDIR) value=/etc/apcupsd ;; esac
        # 目录不是交互可调参数，按原生 USB 安装布局规范化。
        awk -v key="$key" -v value="$value" '$1==key {if (!found++) print key " " value; next} {print} END {if (!found) print key " " value}' "$output" > "$output.directories" || return 1
        cat "$output.directories" > "$output" || return 1
        rm -f -- "$output.directories"
    done
    validate_config_file "$output"
}

restore_config_backup() {
    local backup=$1
    cp -a -- "$backup" "$CONFIG_FILE" || return 1
    systemctl restart apcupsd.service || { err '原配置已恢复，但服务恢复失败，请查看诊断。'; return 1; }
}

config_apply_file() {
    local file=$1 backup was_active=0
    require_managed && guard_mains && validate_config_file "$file" || return 1
    mkdir -p -- "$STATE_DIR/changes" || return 1
    backup=$(mktemp "$STATE_DIR/changes/配置修改前.XXXXXX") || return 1
    cp -a -- "$CONFIG_FILE" "$backup" || return 1
    service_active && was_active=1
    # 写入前再确认一次市电，避免用户输入期间供电发生变化。
    guard_mains || return 1
    cp -a -- "$file" "$CONFIG_FILE" || return 1
    if (( was_active )); then
        if ! systemctl restart apcupsd.service || ! service_active; then
            restore_config_backup "$backup"
            err '参数应用失败，已尝试恢复修改前配置。'; return 1
        fi
        if ! wait_ups_snapshot; then
            cp -a -- "$backup" "$CONFIG_FILE" || return 1
            # 如果此刻已停电，不再次重启，以免延长刚启动的倒计时。
            if read_ups_snapshot && ups_online; then systemctl restart apcupsd.service || true; fi
            err '通信验证失败；旧配置已写回，请查看诊断确认服务运行参数。'; return 1
        fi
        local key status_key actual wanted
        for key in TIMEOUT BATTERYLEVEL MINUTES; do
            case $key in TIMEOUT) status_key=MAXTIME ;; BATTERYLEVEL) status_key=MBATTCHG ;; MINUTES) status_key=MINTIMEL ;; esac
            actual=$(ups_number "$status_key") || actual=''
            wanted=$(config_get "$key" "$file")
            if [[ -n $actual && $actual != "$((10#$wanted))" ]]; then
                cp -a -- "$backup" "$CONFIG_FILE" || return 1
                if ups_online; then systemctl restart apcupsd.service || :; fi
                err '读回的运行参数与保存值不符；旧配置已写回，请查看诊断。'; return 1
            fi
        done
    fi
    if declare -F state_record_config >/dev/null; then state_record_config || return 1; fi
    if (( was_active )); then msg '参数已自动保存并应用。'
    else msg '参数已自动保存，将在监控服务启动时生效。'; fi
}

config_change() {
    local key=$1 value=$2 temp
    require_managed && guard_mains && validate_value "$key" "$value" || return 1
    [[ $(config_get "$key") == "$value" ]] && { msg '参数未改变。'; return 0; }
    temp=$(mktemp "$APPCONFIG_DIR/.pve-ups-edit.XXXXXX") || return 1
    cp -a -- "$CONFIG_FILE" "$temp" || { rm -f "$temp"; return 1; }
    if ! config_set "$key" "$value" "$temp" || ! config_apply_file "$temp"; then
        rm -f -- "$temp"; return 1
    fi
    rm -f -- "$temp"
}
