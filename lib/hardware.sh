#!/usr/bin/env bash
# 精简硬件概览；只有主菜单刷新或一次性 hardware 命令才读取。

render_hardware() {
    local mode=${1:-sample} report rc _rows=24 columns=80 budget=2s
    local -a arguments=()
    if [[ -t 0 ]]; then
        IFS=' ' read -r _rows columns < <(stty size 2>/dev/null) || columns=80
    fi
    [[ $columns =~ ^[0-9]+$ && $columns -gt 0 ]] || columns=80
    arguments=(--width "$columns")
    if [[ $mode == quick ]]; then arguments+=(--no-sample); budget=0.5s; fi
    if command -v python3 >/dev/null 2>&1; then
        # 同一前台进程组；退出时中止采集。缺失指标只影响概览。
        report=$(timeout --foreground --signal=TERM --kill-after=0.1s "$budget" \
            python3 -B "$APP_ROOT/lib/hardware.py" "${arguments[@]}" 2>/dev/null)
        rc=$?
        if (( rc == 0 )); then msg "$report"; return 0; fi
    else rc=1; fi
    msg '──────── PVE 硬件状态 ────────'
    msg 'CPU 使用率：不可用    温度：不可用'
    msg 'CPU Package 功耗：不可用'
    msg '内存：不可用'
    msg '磁盘温度：不可用（本次未获得完整列表）'
    return "$rc"
}
