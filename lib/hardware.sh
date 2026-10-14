#!/usr/bin/env bash
# 硬件页面按需只读采集；不启动服务、不保存硬件历史或更改系统配置。

render_hardware() {
    local report rc battery runtime load
    msg '──────── 宿主机硬件状态 ────────'
    if ! command -v python3 >/dev/null 2>&1; then
        msg '硬件采集不可用：系统未提供 python3。'
        rc=1
    else
        # 与终端共享前台进程组，Ctrl+C 同时中止采集；超时后强制结束。
        report=$(timeout --foreground --signal=TERM --kill-after=1s 5s \
            python3 -B "$APP_ROOT/lib/hardware.py" 2>/dev/null)
        rc=$?
        if (( rc == 0 )); then
            [[ -z $report ]] || msg "$report"
        elif (( rc == 124 || rc == 137 )); then
            msg '硬件采集超时，当前硬件读数不可用。'
        else
            msg '硬件采集失败，当前硬件读数不可用。'
        fi
    fi
    # 每次页面只查询一次 UPS，功率计算复用同一个快照，不同步历史。
    read_ups_snapshot || :
    msg ''
    msg '──────── UPS ────────'
    msg "供电状态：$(ups_status_chinese)"
    battery=$(ups_measurement BCHARGE '%') || battery='不可用'
    runtime=$(ups_measurement TIMELEFT 'min') || runtime='不可用'
    load=$(ups_measurement LOADPCT '%') || load='不可用'
    if [[ $(ups_value STATUS) == *COMMLOST* ]]; then
        battery='不可用'; runtime='不可用'; load='不可用'
    fi
    msg "电池电量：$battery    剩余续航：$runtime"
    msg "负载比例：$load"
    msg "接入设备总负载（估算）：$(ups_estimated_power)"
    msg '此项估算包含 UPS 所带全部设备；CPU 功耗域不能代表整机功耗。'
    return "$rc"
}

render_hardware_menu() {
    local report rows _columns page_size pages page offset
    local -a lines=()
    # 可选传感器或采集失败不退出菜单，仍然显示 UPS 和返回入口。
    report=$(render_hardware || :)
    mapfile -t lines <<< "$report"
    IFS=' ' read -r rows _columns < <(stty size 2>/dev/null) || rows=24
    [[ $rows =~ ^[0-9]+$ && $rows -ge 12 ]] || rows=24
    page_size=$((rows-7))
    pages=$(( (${#lines[@]}+page_size-1)/page_size ))
    (( pages > 0 )) || pages=1
    page=$(( (HARDWARE_PAGE_OFFSET%pages+pages)%pages ))
    offset=$((page*page_size))
    msg "──────── 宿主机硬件状态 $((page+1)) / $pages ────────"
    printf '%s\n' "${lines[@]:offset:page_size}"
    printf '\n1. 下一页    2. 上一页    0. 返回\n'
}

hardware_menu() {
    local HARDWARE_PAGE_OFFSET=0
    while :; do
        # 正常采集时间计入五秒周期；采集超时后仍为输入留出时间。
        menu_choice render_hardware_menu start || return 0
        case $MENU_CHOICE in
            0) return ;;
            1) HARDWARE_PAGE_OFFSET=$((HARDWARE_PAGE_OFFSET+1)) ;;
            2) HARDWARE_PAGE_OFFSET=$((HARDWARE_PAGE_OFFSET-1)) ;;
        esac
    done
}
