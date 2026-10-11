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
       ( ! -e $HISTORY_FILE || -f $HISTORY_FILE ) && ! -L $HISTORY_FILE.cleared ]] || return 1
    printf '%s\n' "$HISTORY_FILE"
}

history_with_lock() {
    local file lockfile generation
    file=$(history_file_path) || return 1
    lockfile=$file.lock
    [[ ! -L $lockfile && ( ! -e $lockfile || -f $lockfile ) ]] || return 1
    generation=$(stat -c '%d:%i:%y:%z' "$STATE_DIR/.pve-ups-owned") || return 1
    (
        flock -x 9 || return 1
        history_file_path >/dev/null || return 1
        [[ ! -L $lockfile && -f $lockfile &&
           $(stat -c '%d:%i:%y:%z' "$STATE_DIR/.pve-ups-owned") == "$generation" ]] || return 1
        "$@" "$file"
    ) 9>>"$lockfile"
}

history_events_path() {
    local events_log
    events_log=$(config_get EVENTSFILE 2>/dev/null) || events_log=''
    events_log=${events_log:-/var/log/apcupsd.events}
    [[ -z ${PVE_ROOT:-} || $events_log == "$PVE_ROOT/"* ]] || events_log=$PVE_ROOT$events_log
    printf '%s\n' "$events_log"
}

history_log_records() {
    local events_log
    events_log=$(history_events_path) || return 1
    [[ -f $events_log && -r $events_log ]] || return 0
    awk '
        /Power failure\./ {
            next_start = $1 " " $2 " " $3
            if (start != "" && start != next_start) print start "\t-"
            start = next_start
        }
        /Mains returned\.|Power is back\. UPS running on mains\./ {
            if (start != "") { print start "\t" $1 " " $2 " " $3; start = "" }
        }
        END { if (start != "") print start "\t-" }
    ' "$events_log"
}

history_read_boundary() {
    local file=$1
    HISTORY_CLEARED_EPOCH=0
    HISTORY_CLEARED_START=''
    if [[ -f $file.cleared ]]; then
        IFS=$'\t' read -r HISTORY_CLEARED_EPOCH HISTORY_CLEARED_START < "$file.cleared" || :
        [[ $HISTORY_CLEARED_EPOCH =~ ^[0-9]+$ ]] || return 1
    fi
}

history_start_allowed() {
    local start=$1 epoch
    (( HISTORY_CLEARED_EPOCH > 0 )) || return 0
    # 常见的同一时区 ISO 时间可直接比较，避免每五秒重读旧日志时反复调用 date。
    if [[ $start =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ [+-][0-9]{4}$ &&
          $HISTORY_CLEARED_START =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ [+-][0-9]{4}$ &&
          ${start: -5} == "${HISTORY_CLEARED_START: -5}" ]]; then
        [[ $start > $HISTORY_CLEARED_START ]]
        return
    fi
    epoch=$(date -d "$start" +%s 2>/dev/null) || return 1
    (( epoch > HISTORY_CLEARED_EPOCH ))
}

history_clear_locked() (
    local file=$1 boundary_start boundary_epoch start epoch temporary xon
    history_read_boundary "$file" || return 1
    boundary_epoch=$HISTORY_CLEARED_EPOCH
    boundary_start=$HISTORY_CLEARED_START
    # 边界取实际清除的事件起点，不取清空时的墙钟，允许同一秒内稍后出现的新事件。
    while IFS=$'\t' read -r start _; do
        [[ -n $start ]] || continue
        epoch=$(date -d "$start" +%s 2>/dev/null) || continue
        if (( epoch > boundary_epoch )); then boundary_epoch=$epoch; boundary_start=$start; fi
    done < <({ [[ ! -f $file ]] || cat -- "$file"; history_log_records; })
    if [[ ${UPS_QUERY_OK:-0} == 1 ]]; then
        xon=$(ups_value XONBATT)
        if [[ $xon =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2} ]]; then
            epoch=$(date -d "$xon" +%s 2>/dev/null) || epoch=0
            if (( epoch > boundary_epoch )); then boundary_epoch=$epoch; boundary_start=$xon; fi
        fi
    fi
    temporary=$(mktemp "$file.clear.XXXXXX") || return 1
    trap 'rm -f -- "$temporary"' EXIT
    printf '%s\t%s\n' "$boundary_epoch" "$boundary_start" > "$temporary" || return 1
    chmod 600 "$temporary" || return 1
    mv -f -- "$temporary" "$file.cleared" || return 1
    # 锁文件一直保留，排队中的同步使用同一个锁；原生事件日志不删除。
    : > "$file" || return 1
    chmod 644 "$file"
)

history_clear() {
    read_ups_snapshot || :
    history_with_lock history_clear_locked
}

history_merge_record() {
    local file=$1 start=$2 end=$3 duration=$4 reason_cn=$5 reason_raw=$6
    if awk -F '\t' -v s="$start" '$1 == s {found=1} END {exit !found}' "$file"; then
        awk -F '\t' -v s="$start" -v e="$end" -v d="$duration" -v rc="$reason_cn" -v rr="$reason_raw" '
            $1 == s {
                if (e != "-" && ($2 == "-" || $2 == "")) { $2 = e; $3 = d }
                if (e == $2 && d != "-" && ($3 == "-" || $3 == "")) $3 = d
                if (rr != "" && rr != "Power failure" && rr != "No transfers since turnon" &&
                    ($5 == "" || $5 == "Power failure" || $5 == "No transfers since turnon" || $5 == "UNKNOWN EVENT")) {
                    $4 = rc; $5 = rr
                }
            }
            { print $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5 }
        ' "$file" > "$file.updated" && mv -f -- "$file.updated" "$file"
    else
        printf '%s\t%s\t%s\t%s\t%s\n' "$start" "$end" "$duration" "$reason_cn" "$reason_raw" >> "$file"
    fi
}

history_sync_locked() (
    local file=$1 temp xonbatt xoffbatt lastxfer status log_start log_end duration row
    history_read_boundary "$file" || return 1
    temp=$(mktemp "${file}.sync.XXXXXX") || return 1
    trap 'rm -f -- "$temp" "$temp.updated" "$temp.sorted"' EXIT
    if [[ -f $file ]]; then
        while IFS= read -r row; do
            history_start_allowed "${row%%$'\t'*}" || continue
            printf '%s\n' "$row" >> "$temp" || return 1
        done < "$file"
    fi

    # 来电日志可补齐以前保存的未完成记录，即使当前快照失败或服务已经重启。
    while IFS=$'\t' read -r log_start log_end; do
        [[ -n $log_start ]] || continue
        history_start_allowed "$log_start" || continue
        duration='-'
        if [[ $log_end != '-' ]]; then
            duration=$(history_calculate_duration "$log_start" "$log_end") || { log_end='-'; duration='-'; }
        fi
        history_merge_record "$temp" "$log_start" "$log_end" "$duration" '市电断电' 'Power failure' || return 1
    done < <(history_log_records)

    if [[ ${UPS_QUERY_OK:-0} == 1 ]]; then
        xonbatt=$(ups_value XONBATT)
        xoffbatt=$(ups_value XOFFBATT)
        lastxfer=$(ups_value LASTXFER)
        status=$(ups_value STATUS)
        if [[ $xonbatt =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2} && $status != *COMMLOST* ]] && history_start_allowed "$xonbatt"; then
            local reason_cn reason_raw duration='-' end_time='-'
            reason_raw=${lastxfer//$'\t'/ }
            reason_raw=${reason_raw//$'\n'/ }
            if [[ -z $reason_raw || $reason_raw == N/A || $reason_raw == 'No transfers since turnon' ]]; then reason_raw='Power failure'; fi
            reason_cn=$(history_translate_reason "$reason_raw")
            if [[ " $status " != *' ONBATT '* && $xoffbatt =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2} ]]; then
                if duration=$(history_calculate_duration "$xonbatt" "$xoffbatt"); then end_time=$xoffbatt; else duration='-'; fi
            fi
            history_merge_record "$temp" "$xonbatt" "$end_time" "$duration" "$reason_cn" "$reason_raw" || return 1
        fi
    fi

    LC_ALL=C sort -t $'\t' -u -k1,1 "$temp" > "$temp.sorted" || return 1
    chmod 644 "$temp.sorted" || return 1
    mv -f -- "$temp.sorted" "$file"
)

history_sync() { history_with_lock history_sync_locked 2>/dev/null || :; }

history_record_event() {
    read_ups_snapshot || :
    history_sync
}

history_page_count() {
    local size=${1:-3} file count
    [[ $size =~ ^[1-9][0-9]*$ ]] || return 1
    file=$(history_file_path) || { printf '1\n'; return; }
    count=$(awk -F '\t' 'NF && $1 != "" {n++} END {print n+0}' "$file" 2>/dev/null) || count=0
    printf '%d\n' "$(( count > 0 ? (count + size - 1) / size : 1 ))"
}

render_history() {
    local page_size=${1:-0} page=${2:-1}
    [[ $page_size =~ ^[0-9]+$ && $page =~ ^[1-9][0-9]*$ ]] || return 1
    # 先查询再同步，CLI 和首次进入菜单时也能保存这次读取到的切换原因。
    read_ups_snapshot || :
    history_sync
    local file count=0 total_sec=0 model total_dur line_num=0 first=1 last=0 xon xoff dur rc rr status current_xon
    msg '──────── 停电与供电历史记录 ────────'
    model=$(ups_value MODEL)
    ups_has_value MODEL || model=''
    [[ -z $model ]] || msg "UPS 型号：$model    供电状态：$(ups_status_chinese)"
    file=$(history_file_path) || { msg '暂无停电或供电切换记录。'; return; }
    if [[ ! -s $file ]]; then msg '暂无停电或供电切换记录。'; return; fi

    while IFS=$'\t' read -r xon _ dur _ _; do
        [[ -n $xon ]] || continue
        (( count += 1 ))
        [[ ! $dur =~ ^[0-9]+$ ]] || (( total_sec += dur ))
    done < "$file"
    if (( count == 0 )); then msg '暂无停电或供电切换记录。'; return; fi
    total_dur=$(history_format_duration "$total_sec")
    msg "历史停电总计：$count 次    累计已记录停电时长：$total_dur"
    if (( page_size > 0 )); then
        local total_pages=$(( (count + page_size - 1) / page_size ))
        (( page <= total_pages )) || page=$total_pages
        first=$(( (page - 1) * page_size + 1 ))
        last=$(( page * page_size ))
        msg "第 $page / $total_pages 页（每页 $page_size 条）"
    fi
    msg ''
    status=$(ups_value STATUS)
    current_xon=$(ups_value XONBATT)
    while IFS=$'\t' read -r xon xoff dur rc rr; do
        [[ -n $xon ]] || continue
        (( line_num += 1 ))
        (( line_num >= first )) || continue
        (( last == 0 || line_num <= last )) || break
        local dur_text reason_text xoff_text
        if [[ $xoff == '-' || -z $xoff ]]; then
            if [[ ${UPS_QUERY_OK:-0} == 1 && " $status " == *' ONBATT '* && $status != *COMMLOST* && $xon == "$current_xon" ]]; then
                xoff_text='持续停电中'; dur_text='进行中'
            else
                xoff_text='未记录恢复时间'; dur_text='未记录'
            fi
        else
            xoff_text=$xoff
            if [[ $dur =~ ^[0-9]+$ ]]; then dur_text=$(history_format_duration "$dur"); else dur_text='未记录'; fi
        fi
        if [[ -n $rr && $rr != 'Power failure' ]]; then
            rc=$(history_translate_reason "$rr")
            if [[ $rr != "$rc" ]]; then reason_text="${rc}（${rr}）"; else reason_text=$rc; fi
        else
            reason_text=${rc:-市电断电}
        fi
        msg "[$line_num] 转入电池：$xon"
        msg "    恢复市电：$xoff_text    持续时长：$dur_text"
        msg "    切换原因：$reason_text"
        msg ''
    done < <(LC_ALL=C sort -t $'\t' -r -k1,1 "$file")
}
