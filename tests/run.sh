#!/usr/bin/env bash
# 所有系统操作均被替换，不需要 root，也不接触真实软件包和服务。
set -Eeuo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 -m unittest discover -s "$project_root/tests" -p 'test_*.py' -v
