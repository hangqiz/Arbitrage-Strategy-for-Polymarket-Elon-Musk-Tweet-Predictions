"""月度参数校准命令行：从 sqlite 与价格增量导出建议参数。

用法：python scripts/calibrate.py --price-deltas 3,1,5,... （可选）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_arb.calibration import run_calibration
from polymarket_arb.config import AppConfig
from polymarket_arb.storage.sqlite_store import SqliteStore


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="./data/state.db")
    parser.add_argument("--price-deltas", default="", help="逗号分隔的安静期价格增量(¢)")
    args = parser.parse_args()

    deltas = [float(x) for x in args.price_deltas.split(",") if x.strip() != ""]
    store = SqliteStore(args.db)
    cfg = AppConfig(env="calibration")
    result = run_calibration(cfg, deltas, store)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    print(result.note)


if __name__ == "__main__":
    main()