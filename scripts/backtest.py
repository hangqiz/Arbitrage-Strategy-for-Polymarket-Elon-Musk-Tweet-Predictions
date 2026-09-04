"""离线回测：在合成价格路径上跑 门控 + 概率 + 三腿，统计入场与假设 PnL。

用法：python scripts/backtest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import timedelta

from polymarket_arb.config import AppConfig
from polymarket_arb.data.models import Interval, Market, utcnow
from polymarket_arb.features.burst import BurstState
from polymarket_arb.features.market_state import MarketSnapshot
from polymarket_arb.strategy.gates import GateChecker
from polymarket_arb.strategy.legs import LegSelector


def build_market() -> Market:
    now = utcnow()
    defs = [
        ("low", None, 300, 8.0),
        ("mid1", 300, 450, 22.0),
        ("mid2", 450, 600, 48.0),   # 热门
        ("mid3", 600, 800, 7.0),    # 上档便宜（对冲）
        ("high", 800, None, 4.0),   # 上档便宜（对冲）
    ]
    return Market(
        slug="bt-musk", start=now - timedelta(days=5), end=now + timedelta(hours=10),
        intervals=[Interval(bucket_id=b, low=lo, high=hi, yes_clob_token=f"y-{b}",
                            no_clob_token=f"n-{b}", price_c=p)
                   for b, lo, hi, p in defs],
    )


def simulate() -> dict:
    """模拟：临近结算的一次大过热度（x=16¢），路程到安静已确认。"""
    cfg = AppConfig(env="test")
    market = build_market()
    hot = market.find_hot_interval()
    burst = BurstState()
    # 安静开始热门=44，随后情绪把热门推到 60（x=16），临近结算只剩 10h
    anchor = {i.bucket_id: i.price_c for i in market.intervals}
    anchor[hot.bucket_id] = 44.0
    burst.record_quiet_start(anchor, hot.bucket_id)
    hot.price_c = 60.0; hot.ask_c = 60.5; hot.bid_c = 59.5

    snap = MarketSnapshot(market=market, price_sum_c=100.0, hot=hot,
                          hot_price_c=60.0, hot_ask_c=60.5, hot_bid_c=59.5, errors=[])

    from polymarket_arb.probability import p_success
    stock_x = burst.overheat_c(hot.price_c)
    hours = (market.end - utcnow()).total_seconds() / 3600.0
    p = p_success(stock_x, hours, cfg.prob)

    gates = GateChecker(cfg, market, market_days_elapsed=5.0, burst=burst,
                        snapshot=snap).check()
    pgates = gates.passed

    legsel = LegSelector(cfg).select(snap, burst, equity=10_000.0)

    return {
        "x_c": stock_x, "outstanding_h": round(hours, 1), "p": round(p, 4),
        "gates_passed": pgates,
        "signal": "ENTER" if (pgates and p >= cfg.prob.p_min and legsel.is_valid) else "PASS",
        "legs": [l.as_dict() for l in legsel.legs],
        "skipped": legsel.skipped,
        "leg1_pnl_c": round((100 - 44.0) - (100 - 59.5), 1),  # No 在回吐至 44 时 +15.5¢/份
    }


if __name__ == "__main__":
    result = simulate()
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))