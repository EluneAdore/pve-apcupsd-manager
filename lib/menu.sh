#!/usr/bin/env bash
# 菜单空闲时刷新；一旦输入字符，等用户完成输入后再重绘。

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
    if [[ $UPS_QUERY_OK != 1 || ! $value =~ ^([0-9]+([.][0-9]+)?) ]]; then
        msg '未提供 / 无法确认'
    else
        printf '%s %s\n' "${BASH_REMATCH[1]}" "$unit"
    fi
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
            printf '%02d 分 %02d 秒（估算；电量 / 续航不足可提前关机）\n' "$((seconds / 60))" "$((seconds % 60))"
        else
            msg '无法确认'
        fi
    else
        msg '无法确认'
    fi
}

render_status() {
    local version monitoring model
    version=$(package_version) || version=''
    version=${version:-未安装}
    monitoring='已停止'
    service_active && monitoring='运行中'
    read_ups_snapshot || :
    model=$(ups_value MODEL); model=${model:-未提供 / 无法确认}
    msg "──────── PVE UPS 管理 $APP_VERSION ────────"
    msg "apcupsd：$version    监控：$monitoring"
    msg "UPS 型号：$model"
    msg "供电状态：$(ups_status_chinese)"
    msg "电池电量：$(ups_measurement BCHARGE '%')    剩余续航：$(ups_measurement TIMELEFT '分钟')"
    msg "负载比例：$(ups_measurement LOADPCT '%')"
    if [[ $UPS_QUERY_OK == 1 && " $(ups_value STATUS) " == *' ONBATT '* && $(ups_value STATUS) != *COMMLOST* ]]; then
        msg "本次停电时长：$(ups_measurement TONBATT '秒')"
    fi
    msg "关机倒计时：$(render_countdown)"
    [[ $monitoring == 运行中 ]] || msg '监控未运行，当前没有自动关机保障。'
}

render_details() {
    local key label value
    render_status
    msg ''
    msg '──────── 设备详细读数 ────────'
    for key in UPSNAME SERIALNO LINEV OUTPUTV LINEFREQ ITEMP BATTV NOMBATTV BATTDATE LASTXFER XONBATT XOFFBATT; do
        case $key in
            UPSNAME) label='设备名称' ;; SERIALNO) label='设备序列号' ;;
            LINEV) label='输入电压' ;; OUTPUTV) label='输出电压' ;;
            LINEFREQ) label='输入频率' ;; ITEMP) label='设备温度' ;;
            BATTV) label='电池电压' ;; NOMBATTV) label='额定电池电压' ;;
            BATTDATE) label='电池安装日期' ;; LASTXFER) label='上次切换原因（设备原文）' ;;
            XONBATT) label='上次转入电池时间' ;; XOFFBATT) label='上次恢复市电时间' ;;
        esac
        value=$(ups_value "$key")
        if [[ -z $value ]]; then value='未提供 / 无法确认'
        else
            case $key in
                LINEV|OUTPUTV|BATTV|NOMBATTV) value=$(ups_measurement "$key" '伏') ;;
                LINEFREQ) value=$(ups_measurement "$key" '赫兹') ;;
                ITEMP) value=$(ups_measurement "$key" '℃') ;;
            esac
        fi
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
            elif [[ $value =~ ^[0-9]+$ ]]; then printf '%s 分 %s 秒（%s）\n' "$((10#$value/60))" "$((10#$value%60))" "$origin"
            else msg '配置无效'; fi ;;
        BATTERYLEVEL) printf '%s %%（%s）\n' "$value" "$origin" ;;
        MINUTES) printf '%s 分钟（%s）\n' "$value" "$origin" ;;
    esac
}

autostart_chinese() {
    local state
    state=$(systemctl is-enabled apcupsd.service 2>/dev/null) || :
    case $state in enabled) msg '开启' ;; disabled) msg '关闭' ;;
        masked|masked-runtime) msg '已屏蔽' ;; enabled-runtime) msg '临时开启（重启后不保留）' ;;
        *) msg '未安装 / 无法确认' ;; esac
}

menu_clear() { [[ ${TERM:-dumb} == dumb ]] || printf '\033[H\033[2J'; }

menu_choice() {
    local first tail result
    MENU_CHOICE=''
    printf '\n请选择（输入数字并回车）：'
    if IFS= read -r -n 1 -t 2 first; then
        [[ $first != $'\004' ]] || return 1
        if [[ -n $first ]]; then
            IFS= read -r tail || return 1
            MENU_CHOICE=$first$tail
        fi
        return 0
    else result=$?; fi
    # 超时返回循环刷新，终端关闭则退出。
    (( result > 128 )) && [[ -t 0 ]]
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

backup_menu() {
    local choice target
    printf '\n1. 备份配置\n2. 恢复配置\n0. 返回\n请选择：'
    IFS= read -r choice || return
    case $choice in
        1|2)
            printf '请输入完整文件路径（留空返回）：'; IFS= read -r target || return
            [[ -n $target ]] || return
            if [[ $choice == 1 ]]; then mutate config_export "$target"; else mutate config_import "$target"; fi ;;
    esac
}

settings_menu() {
    while :; do
        menu_clear
        read_ups_snapshot || :
        msg '──────── UPS 关机设置 ────────'
        msg "1. 停电多久后关机？           $(current_setting TIMEOUT MAXTIME)"
        msg "2. 电量低于多少时关机？       $(current_setting BATTERYLEVEL MBATTCHG)"
        msg "3. 剩余续航少于多久时关机？   $(current_setting MINUTES MINTIMEL)"
        msg "4. 开机自动监控 UPS           $(autostart_chinese)"
        msg '5. 备份 / 恢复配置'
        msg '0. 返回'
        menu_choice || return 0
        case $MENU_CHOICE in
            '') continue ;; 0) return ;;
            1) menu_edit TIMEOUT ;; 2) menu_edit BATTERYLEVEL ;; 3) menu_edit MINUTES ;;
            4)
                if [[ $(autostart_chinese) == 开启 ]]; then mutate service_action disable
                else mutate service_action enable; fi ;;
            5) backup_menu ;;
            *) msg '请输入菜单中的数字。' ;;
        esac
        menu_pause
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

render_diagnosis() {
    local candidate
    render_status
    msg ''
    if require_managed 2>/dev/null; then msg '安装记录：存在'; else msg '安装记录：不存在'; fi
    if [[ -f $CONFIG_FILE ]] && validate_config_file "$CONFIG_FILE" >/dev/null 2>&1; then msg '配置检查：通过'
    else msg '配置检查：缺失或不符合本机 USB 方案'; fi
    candidate=$(LC_ALL=C apt-cache policy apcupsd 2>/dev/null | awk '$1=="Candidate:" {print $2; exit}') || candidate=''
    msg "软件源可用版本：${candidate:-无法确认}（来自本机已有索引）"
    msg '下方为 apcupsd 原始系统日志：'
    journalctl -u apcupsd.service -n 60 --no-pager 2>/dev/null || msg '日志不可读取。'
}

main_menu() {
    [[ -t 0 && -t 1 ]] || { err '交互菜单需要终端；请使用 pve-ups help 查看命令行操作。'; return 1; }
    while :; do
        menu_clear; render_status
        printf '\n1. 查看 UPS 详细状态\n2. 修改关机设置\n3. 安装 / 修复 apcupsd\n4. 升级 apcupsd\n5. 管理监控服务\n6. 查看日志与诊断\n7. 更新管理工具\n8. 卸载并恢复原状态\n0. 退出\n'
        menu_choice || return 0
        case $MENU_CHOICE in
            '') continue ;; 0) return ;;
            1) render_details ;;
            2) settings_menu; continue ;;
            3) mutate lifecycle_install ;;
            4) mutate lifecycle_upgrade ;;
            5) service_menu ;;
            6) render_diagnosis ;;
            7) mutate lifecycle_tool_update ;;
            8) if confirm_uninstall && mutate lifecycle_uninstall; then return; fi ;;
            *) msg '请输入菜单中的数字。' ;;
        esac
        menu_pause
    done
}
