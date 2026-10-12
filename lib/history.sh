#!/usr/bin/env bash
# 停电与供电历史记录持久化、切换原因中文化及统计。

HISTORY_FILE=${HISTORY_FILE:-$STATE_DIR/history.tsv}

history_translate_reason() {
    local raw=${1:-}
    raw=$(awk '{$1=$1};1' <<< "$raw")
    case $raw in
        'No transfers since turnon') msg '开机以来无供电切换' ;;
        'Low line voltage') msg '输入市电电压过低' ;;
        'High line voltage') msg '输入市电电压过高' ;;
        'Line voltage notch or spike') msg '市电电压出现陷波或尖峰' ;;
        'Line voltage sag or blackout') msg '市电电压骤降或停电' ;;
        'Line voltage sag') msg '市电电压骤降' ;;
        'Line voltage spike') msg '市电电压尖峰脉冲' ;;
        'Line voltage notch') msg '市电电压陷波凹陷' ;;
        'Unacceptable line voltage changes') msg '输入市电电压波动超出允许范围' ;;
        'Automatic or explicit self test') msg '自动或手动触发电池自检' ;;
        'UNKNOWN EVENT') msg '未知切换事件' ;;
        'Automatic self test') msg '电池自动自检' ;;
        'Self test') msg '电池自检' ;;
        'Forced by software') msg '软件控制强制切换' ;;
        'Detection of line voltage fail') msg '检测到市电断电' ;;
        'Unacceptable line freq'|'Line frequency out of range'|'Input frequency out of range') msg '输入市电频率超标' ;;
        'Under frequency') msg '市电频率过低' ;;
        'Over frequency') msg '市电频率过高' ;;
        'Input line failure') msg '输入市电故障' ;;
        'Blackout'|'Power failure') msg '市电断电' ;;
        'Sag') msg '市电电压骤降' ;;
        'Spike') msg '市电电压尖峰' ;;
        'Deep sag') msg '市电电压严重下陷' ;;
        'Notch') msg '市电电压凹陷' ;;
        *) msg "$raw" ;;
    esac
}

history_format_duration() {
    local diff=$1 h m s
    [[ $diff =~ ^[0-9]+$ ]] || { printf '%s\n' "$diff"; return; }
    if (( diff == 0 )); then printf '0 秒\n'; return; fi
    h=$((diff / 3600))
    m=$(( (diff % 3600) / 60 ))
    s=$((diff % 60))
    if (( h > 0 )); then
        printf '%d 小时 %d 分 %d 秒\n' "$h" "$m" "$s"
    elif (( m > 0 )); then
        printf '%d 分 %d 秒\n' "$m" "$s"
    else
        printf '%d 秒\n' "$s"
    fi
}

history_calculate_duration() {
    local start=$1 end=$2 t1 t2 diff
    t1=$(date -d "$start" +%s 2>/dev/null) || return 1
    t2=$(date -d "$end" +%s 2>/dev/null) || return 1
    (( t2 >= t1 )) || return 1
    diff=$((t2 - t1))
    printf '%d\n' "$diff"
}

# 历史属于安装记录；读取或刷新不能重新创建已卸载的状态目录。
history_file_path() {
    require_managed 2>/dev/null || return 1
    [[ -d $STATE_DIR && ! -L $STATE_DIR && ! -L $STATE_DIR/.pve-ups-owned &&
       ${HISTORY_FILE%/*} == "$STATE_DIR" && ! -L $HISTORY_FILE &&
       ( ! -e $HISTORY_FILE || -f $HISTORY_FILE ) ]] || return 1
    printf '%s\n' "$HISTORY_FILE"
}

history_with_lock() {
    local worker=$1 file lockfile generation
    shift
    file=$(history_file_path) || return 1
    lockfile=$file.lock
    [[ ! -L $lockfile && ( ! -e $lockfile || -f $lockfile ) ]] || return 1
    generation=$(stat -c '%d:%i:%y:%z' "$STATE_DIR/.pve-ups-owned") || return 1
    (
        flock -x 9 || return 1
        history_file_path >/dev/null || return 1
        [[ ! -L $lockfile && -f $lockfile &&
           $(stat -c '%d:%i:%y:%z' "$STATE_DIR/.pve-ups-owned") == "$generation" ]] || return 1
        "$worker" "$file" "$@"
    ) 9>>"$lockfile"
}

history_events_path() {
    local events_log
    events_log=$(config_get EVENTSFILE 2>/dev/null) || events_log=''
    events_log=${events_log:-/var/log/apcupsd.events}
    printf '%s\n' "$events_log"
}

history_engine_locked() {
    local file=$1 command=$2 events
    shift 2
    command -v python3 >/dev/null 2>&1 || { err '历史功能需要 python3；原生 UPS 监控继续运行。'; return 1; }
    events=$(history_events_path) || return 1
    python3 -B "$APP_ROOT/lib/history_store.py" --state-dir "$STATE_DIR" --events "$events" \
        --legacy "$file" "$command" "$@"
}

history_call() {
    local failure
    if failure=$(history_with_lock history_engine_locked "$@" 2>&1); then
        HISTORY_LAST_ERROR=''
        [[ -z $failure ]] || printf '%s\n' "$failure"
        return 0
    fi
    HISTORY_LAST_ERROR=${failure:-历史存储无法访问，请检查安装状态、权限或诊断日志。}
    # 数据库自身错误由存储引擎保留；目录/解释器错误也进入系统日志。
    if command -v logger >/dev/null 2>&1; then
        timeout 1 logger -t pve-ups-history -- "$HISTORY_LAST_ERROR" 2>/dev/null || :
    fi
    return 1
}

history_sync() {
    require_managed 2>/dev/null || return 0
    history_call sync <<< "${UPS_DATA:-}"
}

history_clear() {
    local request_uptime='' request_boot='' request_pid='' request_birth='' request_clock
    # 发起请求时保存时钟；UPS 查询及排队取锁不能移动清空边界。
    # 专用进程身份区分同一内核 tick 内的清空和回调；此操作只在用户清空时运行。
    request_clock=$(python3 -B "$APP_ROOT/lib/history_store.py" clock) || return 1
    IFS=$'\t' read -r request_boot request_uptime request_pid request_birth <<< "$request_clock"
    read_ups_snapshot || :
    history_call clear --captured-uptime "$request_uptime" --boot-id "$request_boot" \
        --clear-pid "$request_pid" --clear-birth "$request_birth" <<< "${UPS_DATA:-}"
}

history_record_event() {
    local event=${1:-} metadata=()
    shift || return 1
    case $event in powerout|mainsback|onbattery|offbattery) ;; *) return 2 ;; esac
    while (( $# )); do
        case $1 in
            --captured-ns|--captured-uptime|--boot-id|--token|--session|--log-dev|--log-ino|--log-offset)
                (( $# >= 2 )) || return 2
                metadata+=("$1" "$2"); shift 2 ;;
            *) return 2 ;;
        esac
    done
    require_managed 2>/dev/null || return 0
    # 先保存调用时的事件身份和时刻，查询故障不延迟事件落盘。
    history_call capture --event "$event" "${metadata[@]}" < /dev/null || :
    read_ups_snapshot || :
    history_sync || :
    # 此后台回调永不替代或干扰 apcupsd 原生事件返回值。
    return 0
}

history_page_count() {
    local size=${1:-3} result
    [[ $size =~ ^[1-9][0-9]*$ ]] || return 1
    require_managed 2>/dev/null || { printf '1\n'; return; }
    result=$(history_call pages --size "$size" < /dev/null) || result=1
    [[ $result =~ ^[1-9][0-9]*$ ]] || result=1
    printf '%s\n' "$result"
}

render_history() {
    local page_size=${1:-0} page=${2:-1} records failure='' model
    local kind id start end duration reason_cn reason_raw state _evidence
    [[ $page_size =~ ^[0-9]+$ && $page =~ ^[1-9][0-9]*$ ]] || return 1
    read_ups_snapshot || :
    history_sync || failure=${HISTORY_LAST_ERROR:-历史同步失败。}
    msg '──────── 电池供电事件记录 ────────'
    model=$(ups_value MODEL)
    ups_has_value MODEL || model=''
    [[ -z $model ]] || msg "UPS 型号：$model    供电状态：$(ups_status_chinese)"
    if ! require_managed 2>/dev/null; then msg '暂无电池供电事件记录。'; return; fi
    if ! records=$(history_call query --page "$page" --size "$page_size" < /dev/null); then
        msg "历史功能异常：${HISTORY_LAST_ERROR:-请查看诊断。}"
        return 0
    fi
    [[ -z $failure ]] || msg "历史同步失败：$failure"
    while IFS=$'\t' read -r kind id start end duration reason_cn reason_raw state _evidence; do
        case $kind in
            summary)
                msg "转入电池总次数：$id    累计已记录电池供电时长：$(history_format_duration "$start")"
                msg "当前进行中的事件：$end    恢复时间未记录的事件：$duration"
                if (( page_size > 0 )); then
                    (( page <= reason_cn )) || page=$reason_cn
                    msg "第 $page / $reason_cn 页（每页 $page_size 条）"
                fi
                if [[ $id == 0 ]]; then msg '暂无电池供电事件记录。'; fi
                msg '' ;;
            notice) msg "历史功能提示：$id" ;;
            row)
                # 完整证据身份保存在数据库中；菜单缩短过长的旧记录标识。
                (( ${#id} <= 24 )) || id="${id:0:12}…${id: -8}"
                [[ $reason_cn != '-' ]] || reason_cn='原因未记录'
                if [[ $reason_raw != '-' && -n $reason_raw && $reason_raw != "$reason_cn" ]]; then
                    reason_cn+="（$reason_raw）"
                fi
                case $state in
                    active) state='正在电池供电'; end='尚未恢复' ;;
                    restored) state='已恢复市电' ;;
                    *) state='恢复时间未知'; [[ $end != '-' ]] || end='未记录' ;;
                esac
                [[ $duration != '-' ]] && duration=$(history_format_duration "$duration") || duration='未记录'
                msg "[$id] 转入电池：$start"
                msg "    恢复市电：$end    电池供电时长：$duration"
                msg "    状态：$state    原因：$reason_cn"
                msg '' ;;
        esac
    done <<< "$records"
}

history_diagnostics() {
    require_managed 2>/dev/null || return 0
    if ! history_call diagnostics < /dev/null; then
        msg "历史功能异常：${HISTORY_LAST_ERROR:-历史存储不可读取。}"
    fi
    msg '历史功能最近错误（系统日志）：'
    journalctl -t pve-ups-history -n 10 --no-pager 2>/dev/null || msg '系统错误日志不可读取。'
}
