#!/usr/bin/env bash
# 静态检查和隔离回归的退出状态均写入独立报告；任何一项失败则返回非零。
set -uo pipefail
test_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || exit 1
report=${1:-$(mktemp /tmp/pve-ups-test-report.XXXXXX)}
[[ $report == /* ]] || report=$PWD/$report
overall=0
cd -- "$test_root" || exit 1
printf 'PVE UPS 隔离验证\n版本：%s\n' "$(cat VERSION)" > "$report" || exit 1

record() {
    local label=$1 status=$2
    printf '%s 退出状态：%s\n' "$label" "$status" | tee -a "$report"
    (( status == 0 )) || overall=1
}

syntax=0
while IFS= read -r file; do
    bash -n "$file" >> "$report" 2>&1 || syntax=1
done < <(printf '%s\n' bootstrap.sh bin/pve-ups lib/*.sh tests/*.sh)
record 'bash -n' "$syntax"

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -x bootstrap.sh bin/pve-ups lib/*.sh tests/*.sh >> "$report" 2>&1
    static=$?
else
    printf 'shellcheck 未安装\n' >> "$report"
    static=127
fi
record shellcheck "$static"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v >> "$report" 2>&1
regression=$?
record '完整隔离回归' "$regression"
printf '报告：%s\n' "$report"
exit "$overall"
