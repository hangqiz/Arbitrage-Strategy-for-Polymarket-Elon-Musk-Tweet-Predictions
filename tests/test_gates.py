from datetime import timedelta

from polymarket_arb.config import AppConfig
from polymarket_arb.data.events import CatalystCalendar, CatalystEvent
from polymarket_arb.data.models import Market, utcnow
from polymarket_arb.features.burst import BurstState
from polymarket_arb.features.market_state import MarketSnapshot
from polymarket_arb.strategy.gates import GateChecker


def _snap(prices: dict) -> MarketSnapshot:
    market = Market(slug="m", start=utcnow() - timedelta(days=4),
                    end=utcnow() + timedelta(days=10))
    for b, p in prices.items():
        from polymarket_arb.data.models import Interval
        market.intervals.append(Interval(bucket_id=b, low=None, high=None,
                                         yes_clob_token=f"y-{b}", no_clob_token=f"n-{b}",
                                         price_c=p, ask_c=p + 0.5, bid_c=p - 0.5))
    hot = max(market.intervals, key=lambda i: i.price_c)
    return MarketSnapshot(market=market, price_sum_c=sum(prices.values()), hot=hot,
                          hot_price_c=hot.price_c, hot_ask_c=hot.ask_c,
                          hot_bid_c=hot.bid_c, errors=[])


def _burst(quiet_price: float = 60.0, in_burst=False) -> BurstState:
    st = BurstState()
    if not in_burst:
        st.record_quiet_start({"hot": quiet_price}, "hot")
    return st


def test_all_gates_pass():
    cfg = AppConfig(env="test")
    snap = _snap({"hot": 60.0, "low": 30.0, "high": 10.0})
    checker = GateChecker(cfg, snap.market, market_days_elapsed=4.0,
                          burst=_burst(), snapshot=snap)
    res = checker.check()
    assert res.passed is True
    assert res.failed == []


def test_calendar_gate_blocks_early():
    cfg = AppConfig(env="test")
    snap = _snap({"hot": 60.0, "low": 40.0})
    checker = GateChecker(cfg, snap.market, market_days_elapsed=1.0,
                          burst=_burst(), snapshot=snap)
    assert checker.calendar_gate() is False
    assert "calendar" in checker.check().failed


def test_quiet_gate_blocks_when_still_bursting():
    cfg = AppConfig(env="test")
    snap = _snap({"hot": 60.0, "low": 40.0})
    checker = GateChecker(cfg, snap.market, market_days_elapsed=4.0,
                          burst=_burst(in_burst=True), snapshot=snap)
    assert "quiet" in checker.check().failed


def test_catalyst_gate_blocks_with_event():
    cfg = AppConfig(env="test")
    snap = _snap({"hot": 60.0, "low": 40.0})
    cal = CatalystCalendar([CatalystEvent(utcnow() + timedelta(hours=3), "财报")])
    checker = GateChecker(cfg, snap.market, market_days_elapsed=4.0,
                          burst=_burst(), snapshot=snap, calendar=cal)
    assert "catalyst" in checker.check().failed


def test_liquidity_gate_blocks_wide_spread():
    cfg = AppConfig(env="test")
    snap = _snap({"hot": 60.0, "low": 40.0})
    snap.hot_ask_c = 62.0
    snap.hot_bid_c = 60.0  # spread=2 > 1.5
    checker = GateChecker(cfg, snap.market, market_days_elapsed=4.0,
                          burst=_burst(), snapshot=snap)
    assert checker.liquidity_gate() is False