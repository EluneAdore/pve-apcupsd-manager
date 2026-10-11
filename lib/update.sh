#!/usr/bin/env bash
# 管理代码更新不覆盖首次安装基线，也不升级软件包。

tool_install_bundle() (
    require_root && require_pve && state_validate_paths && lifecycle_is_managed && guard_mains || return 1
    lifecycle_transaction_begin || return 1
    lifecycle_deploy_bundle || return 1
    lifecycle_configure_event_notifications || return 1
    if [[ ${LIFECYCLE_EVENTS_CONFIG_CHANGED:-no} == yes ]] && service_active; then
        guard_mains && systemctl restart apcupsd.service && service_active && lifecycle_wait_for_ups || return 1
    fi
    state_put "$STATE_DIR/manifest.tsv" current_version "$APP_VERSION" || return 1
    # 生命周期模块的退出处理读取此变量。
    # shellcheck disable=SC2034
    LIFECYCLE_COMMITTED=yes
    msg "管理工具已更新为 $APP_VERSION；关机参数和首次安装基线已保留。"
    msg '如当前打开了菜单，请退出后重新运行 pve-ups 使用新版。'
)

bootstrap_tool_update() (
    local module
    # 引导脚本读取此变量，作为库加载。
    # shellcheck disable=SC2034
    PVE_UPS_BOOTSTRAP_SOURCE_ONLY=1
    # shellcheck source=bootstrap.sh
    source "$APP_ROOT/bootstrap.sh" || return 1
    trap '[[ -z ${BOOTSTRAP_TMP:-} ]] || rm -rf -- "$BOOTSTRAP_TMP"' EXIT
    bootstrap_fetch || return 1
    guard_mains || return 1
    APP_ROOT=$BOOTSTRAP_BUNDLE
    # 当前操作已持有管理锁，加载新版模块后在同一操作内更新，避免重复取锁。
    for module in common config lifecycle history update; do
        # shellcheck source=/dev/null
        source "$APP_ROOT/lib/$module.sh" || return 1
    done
    tool_install_bundle
)
