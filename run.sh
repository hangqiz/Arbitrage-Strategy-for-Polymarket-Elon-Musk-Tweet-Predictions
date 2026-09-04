#!/usr/bin/env bash
set -euo pipefail

# 安装依赖
python3 -m pip install -r requirements.txt >/dev/null 2>&1 || true

# 常用命令
case "${1:-}" in
  demo)
    python3 -m polymarket_arb --demo
    ;;
  backtest)
    python3 scripts/backtest.py
    ;;
  calibrate)
    shift
    python3 scripts/calibrate.py "$@"
    ;;
  test)
    python3 -m pytest tests/ -q
    ;;
  *)
    python3 -m polymarket_arb --demo
    ;;
esac