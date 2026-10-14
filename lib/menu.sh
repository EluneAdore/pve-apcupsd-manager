#!/usr/bin/env bash
# 菜单每五秒读取状态并原位更新；参数输入时保留正常终端行为。

ups_status_chinese() {
    local state token result=''
    state=$(ups_value STATUS)
    [[ -n $state ]] || { msg '无法确认'; return; }
    [[ $state == *'SHUTTING DOWN'* ]] && { msg '正在关机'; return; }
    for token in $state; do
        case $token in
            ONLINE) token='市电供电' ;; ONBATT) token='电池供电' ;;
            COMMLOST) token='通信中断' ;; LOWBATT) token='电量过低' ;;
            REPLACEBATT) token='需要更换电池' ;; CHARGING) token='正在充电' ;;
            OVERLOAD) token='负载过高' ;; NOBATT) token='未检测到电池' ;;
            BOOST) token='升压供电' ;; TRIM) token='降压供电' ;;
            SLAVE) token='从机模式' ;; CAL) token='校准中' ;;
            *) token="未识别状态（$token）" ;;
        esac
        result+="${result:+、}$token"
    done
    msg "$result"
}

ups_measurement() {
    local key=$1 unit=$2 value
    value=$(ups_value "$key")
    [[ $UPS_QUERY_OK == 1 && $value =~ ^([-+]?[0-9]+([.][0-9]+)?)([[:space:]]|$) ]] || return 1
    printf '%s %s\n' "${BASH_REMATCH[1]}" "$unit"
}

ups_power_number() {
    local key=$1 value
    [[ $UPS_QUERY_OK == 1 ]] || return 1
    value=$(ups_value "$key")
    value=${value,,}
    case $key in
        NOMPOWER)
            [[ $value =~ ^([0-9]+([.][0-9]+)?)([[:space:]]+(w|watts))?$ ]] || return 1 ;;
        LOADPCT)
            [[ $value =~ ^([0-9]+([.][0-9]+)?)([[:space:]]+(%|percent)([[:space:]]+load[[:space:]]+capacity)?)?$ ]] || return 1 ;;
        *) return 1 ;;
    esac
    printf '%s\n' "${BASH_REMATCH[1]}"
}

ups_estimated_power() {
    local nominal load state
    state=$(ups_value STATUS)
    if [[ $UPS_QUERY_OK != 1 || -z $state || $state == *COMMLOST* ]]; then
        msg '无法估算（UPS 通信或状态不可用）'; return
    fi
    if ! nominal=$(ups_power_number NOMPOWER) || ! load=$(ups_power_number LOADPCT); then
        msg '无法估算（额定功率或负载数据缺失／无效）'; return
    fi
    # 只读取当前快照；单位必须为 W，不能把 VA 当成有功功率。
    LC_ALL=C awk -v ups_nominal_watts="$nominal" -v ups_load_pct="$load" 'BEGIN {
        if (ups_nominal_watts <= 0) { print "无法估算（额定功率无效）"; exit }
        if (ups_load_pct < 0 || ups_load_pct > 100) { print "无法估算（负载比例异常）"; exit }
        ups_power_watts = ups_nominal_watts * ups_load_pct / 100
        if (tolower(sprintf("%g", ups_power_watts)) ~ /inf|nan/) { print "无法估算（功率数据异常）"; exit }
        printf "%.1f W%s\n", ups_power_watts, ups_load_pct == 0 ? "（低负载精度有限）" : ""
    }'
}

ups_has_value() {
    local value
    [[ $UPS_QUERY_OK == 1 ]] || return 1
    value=$(ups_value "$1")
    case ${value^^} in ''|N/A|NA|UNKNOWN|'NOT AVAILABLE') return 1 ;; esac
}

render_countdown() {
    local state seconds limit
    state=$(ups_value STATUS)
    if [[ $UPS_QUERY_OK != 1 || $state == *COMMLOST* || -z $state ]]; then
        msg '无法确认'
    elif [[ $state == *'SHUTTING DOWN'* ]]; then
        msg '已开始关机（来电后继续完成）'
    elif ups_online; then
        msg '未启动'
    elif [[ " $state " == *' ONBATT '* ]]; then
        limit=$(ups_number MAXTIME) || limit=''
        if [[ $limit == 0 ]]; then
            msg '等待时间条件已禁用；电池保护仍有效'
        elif seconds=$(countdown_seconds); then
            printf '%02d min %02d s（估算；电量 / 续航不足可提前关机）\n' "$((seconds / 60))" "$((seconds % 60))"
        else
            msg '无法确认'
        fi
    else
        msg '无法确认'
    fi
}

render_status() {
    local version monitoring model battery runtime load power elapsed
    version=$(package_version) || version=''
    version=${version:-未安装}
    monitoring='已停止'
    service_active && monitoring='运行中'
    read_ups_snapshot || :
    history_sync 2>/dev/null || :
    model=$(ups_value MODEL)
    ups_has_value MODEL || model=''
    msg "──────── PVE UPS 管理 $APP_VERSION ────────"
    msg "apcupsd：$version    监控：$monitoring"
    msg "UPS 型号：$model"
    msg "供电状态：$(ups_status_chinese)"
    battery=$(ups_measurement BCHARGE '%') || battery=''
    runtime=$(ups_measurement TIMELEFT 'min') || runtime=''
    load=$(ups_measurement LOADPCT '%') || load=''
    power=$(ups_estimated_power)
    msg "电池电量：$battery    剩余续航：$runtime"
    msg "负载比例：$load"
    msg "接入设备总负载（估算）：$power"
    if [[ $UPS_QUERY_OK == 1 && " $(ups_value STATUS) " == *' ONBATT '* && $(ups_value STATUS) != *COMMLOST* ]]; then
        elapsed=$(ups_measurement TONBATT 's') || elapsed=''
        msg "本次停电时长：$elapsed"
    fi
    msg "关机倒计时：$(render_countdown)"
    [[ $monitoring == 运行中 ]] || msg '监控未运行，当前没有自动关机保障。'
}

render_details() {
    local key label value
    render_status
    msg ''
    msg '──────── 设备详细读数 ────────'
    for key in UPSNAME SERIALNO LINEV OUTPUTV LINEFREQ ITEMP BATTV NOMBATTV NOMPOWER BATTDATE LASTXFER XONBATT XOFFBATT; do
        case $key in
            UPSNAME) label='设备名称' ;; SERIALNO) label='设备序列号' ;;
            LINEV) label='输入电压' ;; OUTPUTV) label='输出电压' ;;
            LINEFREQ) label='输入频率' ;; ITEMP) label='设备温度' ;;
            BATTV) label='电池电压' ;; NOMBATTV) label='额定电池电压' ;;
            NOMPOWER) label='额定输出功率' ;;
            BATTDATE) label='设备记录的电池日期' ;; LASTXFER) label='上次切换原因（设备原文）' ;;
            XONBATT) label='上次转入电池时间' ;; XOFFBATT) label='上次恢复市电时间' ;;
        esac
        value=''
        if ups_has_value "$key"; then value=$(ups_value "$key"); fi
        case $key in
            LINEV|OUTPUTV|BATTV|NOMBATTV) value=$(ups_measurement "$key" 'V') || value='' ;;
            LINEFREQ) value=$(ups_measurement "$key" 'Hz') || value='' ;;
            ITEMP) value=$(ups_measurement "$key" '°C') || value='' ;;
            NOMPOWER)
                if value=$(ups_power_number NOMPOWER); then value+=' W'; else value=''; fi ;;
            LASTXFER)
                if [[ -n $value ]]; then
                    local translated
                    translated=$(history_translate_reason "$value")
                    if [[ $value == 'No transfers since turnon' ]]; then
                        label='上次切换原因'; value='开机以来没有供电切换记录'
                    elif [[ $translated != "$value" ]]; then
                        label='上次切换原因'; value="$translated（$value）"
                    fi
                fi ;;
        esac
        msg "$label：$value"
    done
}

current_setting() {
    local config_key=$1 status_key=$2 value origin
    value=''; origin='配置值'
    if service_active && [[ $UPS_QUERY_OK == 1 ]]; then
        value=$(ups_number "$status_key") || value=''
        [[ -z $value ]] || origin='运行值'
    fi
    [[ -n $value ]] || value=$(config_get "$config_key")
    if [[ -z $value ]]; then msg '未配置 / 无法确认'; return; fi
    case $config_key in
        TIMEOUT)
            if [[ $value == 0 ]]; then printf '禁用时间条件（%s）\n' "$origin"
            elif [[ $value =~ ^[0-9]+$ ]]; then printf '%s min %s s（%s）\n' "$((10#$value/60))" "$((10#$value%60))" "$origin"
            else msg '配置无效'; fi ;;
        BATTERYLEVEL) printf '%s %%（%s）\n' "$value" "$origin" ;;
        MINUTES) printf '%s min（%s）\n' "$value" "$origin" ;;
    esac
}

autostart_chinese() {
    local state
    state=$(systemctl is-enabled apcupsd.service 2>/dev/null) || :
    case $state in enabled) msg '开启' ;; disabled) msg '关闭' ;;
        masked|masked-runtime) msg '已屏蔽' ;; enabled-runtime) msg '临时开启（重启后不保留）' ;;
        *) msg '未安装 / 无法确认' ;; esac
}

menu_clear() { [[ ${TERM:-dumb} == dumb ]] || printf '\033[H\033[J'; }

menu_terminal_restore() {
    [[ -z ${MENU_TTY_STATE:-} ]] || stty "$MENU_TTY_STATE" 2>/dev/null || :
    MENU_TTY_STATE=''
}

menu_draw() {
    local renderer=$1 frame line output=$'\033[H'
    frame=$("$renderer") || return 1
    frame+=$'\n\n请选择（输入数字并回车）：'
    frame+="$MENU_INPUT"
    [[ $frame != "$MENU_LAST_FRAME" ]] || return 0
    while IFS= read -r line; do output+=$'\033[K'; output+="$line"; output+=$'\n'; done <<< "$frame"
    # 一次输出整页，不清屏；抹去缩短页面后留下的旧行，并回到输入行。
    printf '%s\033[J' "${output%$'\n'}"
    MENU_LAST_FRAME=$frame
}

menu_choice() {
    local renderer=$1 character deadline remaining remaining_us result escape count
    local cadence=${2:-completion}
    MENU_CHOICE=''
    MENU_INPUT=''; MENU_LAST_FRAME=''
    if [[ ${TERM:-dumb} == dumb ]]; then
        "$renderer"
        printf '\n请选择（输入数字并回车）：'
        IFS= read -r MENU_CHOICE
        return
    fi
    MENU_TTY_STATE=$(stty -g) || return 1
    stty -echo -icanon min 1 time 0 || { menu_terminal_restore; return 1; }
    deadline=$(( ${EPOCHREALTIME/./}+5000000 ))
    menu_draw "$renderer" || { menu_terminal_restore; return 1; }
    if [[ $cadence != start ]] || (( deadline <= ${EPOCHREALTIME/./} )); then
        deadline=$(( ${EPOCHREALTIME/./}+5000000 ))
    fi
    while :; do
        remaining_us=$((deadline-${EPOCHREALTIME/./}))
        if (( remaining_us <= 0 )); then
            deadline=$(( ${EPOCHREALTIME/./}+5000000 ))
            menu_draw "$renderer" || { menu_terminal_restore; return 1; }
            if [[ $cadence != start ]] || (( deadline <= ${EPOCHREALTIME/./} )); then
                deadline=$(( ${EPOCHREALTIME/./}+5000000 ))
            fi
            continue
        fi
        printf -v remaining '%d.%06d' "$((remaining_us/1000000))" "$((remaining_us%1000000))"
        character=''
        IFS= read -r -N 1 -t "$remaining" character; result=$?
        if (( result > 128 )); then continue; fi
        if (( result != 0 )); then menu_terminal_restore; return 1; fi
        case $character in
            $'\n'|$'\r') MENU_CHOICE=$MENU_INPUT; menu_terminal_restore; printf '\n'; return 0 ;;
            $'\004')
                if [[ -z $MENU_INPUT ]]; then menu_terminal_restore; return 1; fi
                MENU_CHOICE=$MENU_INPUT; menu_terminal_restore; printf '\n'; return 0 ;;
            $'\177'|$'\b') MENU_INPUT=${MENU_INPUT%?} ;;
            $'\025') MENU_INPUT='' ;;
            $'\033')
                # 忽略方向键等终端转义序列，避免把其中的数字当作菜单选择。
                for ((count=0; count<16; count++)); do
                    IFS= read -r -N 1 -t 0.05 escape || break
                    [[ $escape != [A-Za-z~] ]] || break
                done
                continue ;;
            [0-9]) (( ${#MENU_INPUT} >= 8 )) || MENU_INPUT+=$character ;;
            *) continue ;;
        esac
        printf '\r\033[K请选择（输入数字并回车）：%s' "$MENU_INPUT"
    done
}

menu_pause() { printf '\n按回车返回…'; IFS= read -r _ || :; }

menu_edit() {
    local key=$1 value converted
    require_managed && guard_mains || return 1
    case $key in
        TIMEOUT) printf '停电等待多少分钟（0 禁用，1～1440）：' ;;
        BATTERYLEVEL) printf '电量保护阈值（1～100 %%）：' ;;
        MINUTES) printf '续航保护阈值（1～1440 分钟）：' ;;
    esac
    IFS= read -r value || return 1
    [[ -n $value ]] || { msg '已取消，保留原值。'; return; }
    if [[ $key == TIMEOUT ]]; then
        if [[ ! $value =~ ^[0-9]{1,4}$ ]] || ((10#$value>1440)); then
            err '请输入 0～1440 的整数分钟数。'; return 1
        fi
        converted=$((10#$value*60))
    else converted=$value; fi
    mutate config_change "$key" "$converted"
}

render_settings_menu() {
    read_ups_snapshot || :
    msg '──────── UPS 关机设置 ────────'
    msg "1. 停电多久后关机？           $(current_setting TIMEOUT MAXTIME)"
    msg "2. 电量低于多少时关机？       $(current_setting BATTERYLEVEL MBATTCHG)"
    msg "3. 剩余续航少于多久时关机？   $(current_setting MINUTES MINTIMEL)"
    msg "4. 开机自动监控 UPS           $(autostart_chinese)"
    msg '0. 返回'
}

settings_menu() {
    while :; do
        menu_choice render_settings_menu || return 0
        case $MENU_CHOICE in
            '') continue ;; 0) return ;;
            1) menu_edit TIMEOUT ;; 2) menu_edit BATTERYLEVEL ;; 3) menu_edit MINUTES ;;
            4)
                if [[ $(autostart_chinese) == 开启 ]]; then mutate service_action disable
                else mutate service_action enable; fi ;;
            *) msg '请输入菜单中的数字。' ;;
        esac
        menu_pause
    done
}

render_details_menu() {
    render_details
    printf '\n0. 返回\n'
}

details_menu() {
    while :; do
        menu_choice render_details_menu || return 0
        case $MENU_CHOICE in 0) return ;; esac
    done
}

history_menu_page_size() {
    local rows=24 _columns size
    IFS=' ' read -r rows _columns < <(stty size 2>/dev/null) || rows=24
    [[ $rows =~ ^[0-9]+$ && $rows -ge 24 ]] || rows=24
    size=$(((rows-16)/4))
    (( size <= 8 )) || size=8
    printf '%s\n' "$size"
}

render_history_menu() {
    render_history "$HISTORY_PAGE_SIZE" "$HISTORY_PAGE"
    printf '\n1. 刷新记录\n2. 下一页\n3. 上一页\n4. 清空历史记录\n0. 返回\n'
}

history_menu() {
    local answer HISTORY_PAGE=1 HISTORY_PAGE_SIZE pages
    HISTORY_PAGE_SIZE=$(history_menu_page_size)
    while :; do
        pages=$(history_page_count "$HISTORY_PAGE_SIZE")
        (( HISTORY_PAGE <= pages )) || HISTORY_PAGE=$pages
        menu_choice render_history_menu || return 0
        case $MENU_CHOICE in
            '') continue ;;
            0) return ;;
            1) continue ;;
            2)
                pages=$(history_page_count "$HISTORY_PAGE_SIZE")
                (( HISTORY_PAGE >= pages )) || HISTORY_PAGE=$((HISTORY_PAGE+1))
                continue ;;
            3)
                (( HISTORY_PAGE <= 1 )) || HISTORY_PAGE=$((HISTORY_PAGE-1))
                continue ;;
            4)
                printf '\n确认清空所有停电与供电历史记录？输入“清空”继续：'
                IFS= read -r answer || return
                if [[ $answer == 清空 ]]; then
                    if history_clear; then
                        HISTORY_PAGE=1
                        msg '历史记录已清空。'
                    else
                        err '历史记录未能清空，请查看安装状态或稍后重试。' || :
                    fi
                else
                    msg '已取消清空。'
                fi
                menu_pause ;;
            *) msg '请输入菜单中的数字。'; menu_pause ;;
        esac
    done
}

service_menu() {
    local choice
    printf '\n1. 启动监控\n2. 停止监控\n3. 重启监控\n0. 返回\n请选择：'
    IFS= read -r choice || return
    case $choice in
        1) mutate service_action start ;; 2) mutate service_action stop ;; 3) mutate service_action restart ;;
    esac
}

confirm_uninstall() {
    local answer
    [[ -t 0 ]] || { err '非交互卸载请使用：pve-ups uninstall --yes。'; return 1; }
    msg '卸载会恢复首次安装前的配置和服务状态；原先没有 apcupsd 时会移除本项目安装的软件。'
    printf '输入“卸载”继续，其他输入取消：'
    IFS= read -r answer && [[ $answer == 卸载 ]]
}

render_events() {
    local file
    file=$(config_get EVENTSFILE)
    file=${file:-/var/log/apcupsd.events}
    msg '──────── UPS 事件记录 ────────'
    if [[ ! -f $file || ! -r $file || ! -s $file ]]; then
        msg '暂无可读取的事件记录。'
        return
    fi
    tail -n 30 -- "$file" | awk '
        { gsub(/Power failure\./, "市电中断。")
          gsub(/Running on UPS batteries\./, "已转为电池供电。")
          gsub(/Mains returned\. No longer on UPS batteries\./, "市电恢复，已结束电池供电。")
          gsub(/Power is back\. UPS running on mains\./, "市电恢复，UPS 使用市电供电。")
          gsub(/Communications with UPS lost\./, "UPS 通信中断。")
          gsub(/Communications with UPS restored\./, "UPS 通信恢复。")
          gsub(/Initiating system shutdown!/, "开始执行系统关机！")
          gsub(/Reached run time limit on batteries\./, "停电等待时间已到。")
          gsub(/Battery charge below low limit\./, "电量低于保护阈值。")
          gsub(/Reached remaining time percentage limit on batteries\./, "剩余续航低于保护阈值。")
          gsub(/Battery power exhausted\./, "电池电量已耗尽。")
          print }
    '
}

render_diagnosis() {
    local candidate
    render_status
    msg ''
    render_events
    msg ''
    if require_managed 2>/dev/null; then msg '安装记录：存在'; else msg '安装记录：不存在'; fi
    history_diagnostics
    if [[ -f $CONFIG_FILE ]] && validate_config_file "$CONFIG_FILE" >/dev/null 2>&1; then msg '配置检查：通过'
    else msg '配置检查：缺失或不符合本机 USB 方案'; fi
    candidate=$(LC_ALL=C apt-cache policy apcupsd 2>/dev/null | awk '$1=="Candidate:" {print $2; exit}') || candidate=''
    msg "软件源可用版本：${candidate:-无法确认}（来自本机已有索引）"
    msg '下方为 apcupsd 原始系统日志：'
    journalctl -u apcupsd.service -n 60 --no-pager 2>/dev/null || msg '日志不可读取。'
}

render_main_menu() {
    render_status
    printf '\n1. 查看 UPS 详细状态\n2. 查看停电与供电记录\n3. 修改关机设置\n4. 安装 / 修复 apcupsd\n5. 升级 apcupsd\n6. 管理监控服务\n7. 查看事件记录与诊断\n8. 更新管理工具\n9. 卸载并恢复原状态\n10. 查看宿主机硬件状态\n0. 退出\n'
}

main_menu() {
    [[ -t 0 && -t 1 ]] || { err '交互菜单需要终端；请使用 pve-ups help 查看命令行操作。'; return 1; }
    MENU_TTY_STATE=''
    trap 'menu_terminal_restore' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    menu_clear
    while :; do
        menu_choice render_main_menu || return 0
        case $MENU_CHOICE in
            '') continue ;; 0) return ;;
            1) details_menu; continue ;;
            2) history_menu; continue ;;
            3) settings_menu; continue ;;
            4) mutate lifecycle_install ;;
            5) mutate lifecycle_upgrade ;;
            6) service_menu ;;
            7) render_diagnosis ;;
            8) mutate lifecycle_tool_update ;;
            9) if confirm_uninstall && mutate lifecycle_uninstall; then return; fi ;;
            10) hardware_menu; continue ;;
            *) msg '请输入菜单中的数字。' ;;
        esac
        menu_pause
    done
}
