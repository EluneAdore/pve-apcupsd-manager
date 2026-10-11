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
        'Automatic self test') msg '电池自动自检' ;;
        'Self test') msg '电池自检' ;;
        'Forced by software') msg '软件控制强制切换' ;;
        'Detection of line voltage fail') msg '检测到市电断电' ;;
        'Unacceptable line freq'|'Line frequency out of range') msg '输入市电频率超标' ;;
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

history_file_path() {
    mkdir -p -- "$(dirname -- "$HISTORY_FILE")" || return 1
    printf '%s\n' "$HISTORY_FILE"
}

history_clear() {
    local file
    file=$(history_file_path 2>/dev/null) || return 1
    rm -f -- "$file" "$file.lock"
}

history_sync() {
    local file lockfile temp xonbatt xoffbatt lastxfer status
    file=$(history_file_path 2>/dev/null) || return 0
    lockfile="$file.lock"
    [[ -w ${file%/*} || ! -e $file ]] || return 0
    (
        flock -x 9 2>/dev/null || return 0
        temp=$(mktemp "${file}.sync.XXXXXX" 2>/dev/null) || return 0
        [[ ! -f $file ]] || cp -a -- "$file" "$temp" 2>/dev/null || { rm -f -- "$temp"; return 0; }

        # 从原生事件日志导入历史
        local events_log
        events_log=$(config_get EVENTSFILE 2>/dev/null) || events_log=''
        events_log=${events_log:-/var/log/apcupsd.events}
        [[ -z ${PVE_ROOT:-} || $events_log == "$PVE_ROOT/"* ]] || events_log=$PVE_ROOT$events_log
        if [[ -f $events_log && -r $events_log ]]; then
            while IFS=$'\t' read -r log_start log_end; do
                [[ -n $log_start ]] || continue
                if ! awk -F '\t' -v s="$log_start" '$1 == s {found=1} END {exit !found}' "$temp" 2>/dev/null; then
                    local dur_sec='-'
                    if [[ $log_end != '-' ]]; then
                        dur_sec=$(history_calculate_duration "$log_start" "$log_end" 2>/dev/null) || dur_sec='-'
                    fi
                    printf '%s\t%s\t%s\t%s\t%s\n' "$log_start" "$log_end" "$dur_sec" '市电断电' 'Power failure' >> "$temp"
                fi
            done < <(awk '
                /Power failure\./ { start = $1 " " $2 " " $3 }
                /Mains returned\./ {
                    end = $1 " " $2 " " $3
                    if (start != "") { print start "\t" end; start = "" }
                }
                END { if (start != "") print start "\t-" }
            ' "$events_log")
        fi

        # 从当前 UPS 快照更新
        if [[ ${UPS_QUERY_OK:-0} == 1 ]]; then
            xonbatt=$(ups_value XONBATT)
            xoffbatt=$(ups_value XOFFBATT)
            lastxfer=$(ups_value LASTXFER)
            status=$(ups_value STATUS)
            if [[ $xonbatt =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2} ]]; then
                local reason_cn reason_raw dur_sec='-' end_time='-'
                reason_cn=$(history_translate_reason "$lastxfer")
                reason_raw=$lastxfer
                if [[ " $status " == *' ONBATT '* && $status != *'ONLINE'* ]]; then
                    end_time='-'
                    dur_sec='-'
                elif [[ $xoffbatt =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2} ]]; then
                    end_time=$xoffbatt
                    dur_sec=$(history_calculate_duration "$xonbatt" "$xoffbatt" 2>/dev/null) || dur_sec='-'
                fi

                if awk -F '\t' -v s="$xonbatt" '$1 == s {found=1} END {exit !found}' "$temp" 2>/dev/null; then
                    awk -F '\t' -v s="$xonbatt" -v e="$end_time" -v d="$dur_sec" -v rc="$reason_cn" -v rr="$reason_raw" '
                        $1 == s {
                            if (e != "-" && $2 == "-") { $2 = e; $3 = d }
                            if (rr != "" && rr != "Power failure") { $4 = rc; $5 = rr }
                        }
                        { print $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5 }
                    ' "$temp" > "$temp.updated" && mv -f -- "$temp.updated" "$temp"
                else
                    printf '%s\t%s\t%s\t%s\t%s\n' "$xonbatt" "$end_time" "$dur_sec" "$reason_cn" "$reason_raw" >> "$temp"
                fi
            fi
        fi

        if [[ -s $temp ]]; then
            LC_ALL=C sort -t $'\t' -u -k1,1 "$temp" > "$temp.sorted" 2>/dev/null || mv -f -- "$temp" "$temp.sorted"
            mv -f -- "$temp.sorted" "$file"
            chmod 644 "$file" 2>/dev/null || :
        fi
        rm -f -- "$temp" "$temp.updated" "$temp.sorted" 2>/dev/null || :
    ) 9>>"$lockfile"
}

history_record_event() {
    read_ups_snapshot || :
    history_sync
}

render_history() {
    local max_count=${1:-0}
    history_sync
    local file count total_sec model total_dur line_num
    file=$(history_file_path 2>/dev/null) || return 1
    msg '──────── 停电与供电历史记录 ────────'
    read_ups_snapshot || :
    model=$(ups_value MODEL)
    ups_has_value MODEL || model=''
    [[ -z $model ]] || msg "UPS 型号：$model    供电状态：$(ups_status_chinese)"
    if [[ ! -f $file || ! -s $file ]]; then
        msg '暂无停电或供电切换记录。'
        return
    fi

    count=0
    total_sec=0
    while IFS=$'\t' read -r _ _ dur _ _; do
        if [[ $dur =~ ^[0-9]+$ ]]; then
            (( count++ ))
            (( total_sec += dur ))
        fi
    done < "$file"

    total_dur=$(history_format_duration "$total_sec")
    msg "历史停电总计：$count 次    累计停电时长：$total_dur"
    msg ''

    line_num=1
    while IFS=$'\t' read -r xon xoff dur rc rr; do
        [[ -n $xon ]] || continue
        if (( max_count > 0 && line_num > max_count )); then
            msg "（仅显示最近 $max_count 次记录；完整记录保存在 $file）"
            break
        fi
        local dur_text reason_text xoff_text
        if [[ $xoff == '-' || $dur == '-' ]]; then
            xoff_text='持续停电中'
            dur_text='进行中'
        else
            xoff_text="$xoff"
            dur_text=$(history_format_duration "$dur")
        fi
        if [[ -n $rr && $rr != "$rc" && $rr != 'Power failure' ]]; then
            reason_text="${rc}（${rr}）"
        else
            reason_text="$rc"
        fi
        msg "[$line_num] 转入电池：$xon"
        msg "    恢复市电：$xoff_text    持续时长：$dur_text"
        msg "    切换原因：$reason_text"
        msg ''
        (( line_num++ ))
    done < <(LC_ALL=C sort -t $'\t' -r -k1,1 "$file")
}

