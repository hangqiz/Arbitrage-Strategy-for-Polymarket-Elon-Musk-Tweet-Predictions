from polymarket_arb.config import AppConfig
from polymarket_arb.data.models import Interval, Market
from polymarket_arb.features.burst import BurstState
from polymarket_arb.features.market_state import MarketSnapshot
from polymarket_arb.strategy.legs import LegSelector


def _market_current() -> Market:
    # 区间按 发帖数 下界有序
    defs = [
        ("low", None, 300, 8.0),
        ("mid1", 300, 450, 18.0),
        ("mid2", 450, 600, 62.0),   # 热门
        ("mid3", 600, 800, 6.0),    # 上档便宜 Yes（对冲）
        ("high", 800, None, 5.0),   # 上档便宜 Yes（对冲，>= leg3_floor）
    ]
    return Market(
        slug="m", start=None, end=None,
        intervals=[Interval(bucket_id=b, low=lo, high=hi,
                            yes_clob_token=f"yes-{b}", no_clob_token=f"no-{b}",
                            price_c=p, ask_c=p + 0.5, bid_c=p - 0.5)
                   for b, lo, hi, p in defs],
    )


def _burst_quiet() -> BurstState:
    """安静开始时：hot=55，mid1=30（之后跌到18 → 受害者）。"""
    st = BurstState()
    st.record_quiet_start({"low": 8.0, "mid1": 30.0, "mid2": 55.0, "mid3": 6.0, "high": 3.0}, "mid2")
    return st


def _snap(market: Market) -> MarketSnapshot:
    hot = market.find_hot_interval()
    return MarketSnapshot(market=market, price_sum_c=0, hot=hot,
                          hot_price_c=hot.price_c, hot_ask_c=hot.ask_c,
                          hot_bid_c=hot.bid_c, errors=[])


def test_two_leg_selection():
    cfg = AppConfig(env="test")
    market = _market_current()
    burst = _burst_quiet()
    snap = _snap(market)

    # 过热度 x = 62 - 55 = 7¢
    assert burst.overheat_c(snap.hot_price_c) == 7.0

    comp = LegSelector(cfg).select(snap, burst, equity=10_000.0)
    assert comp.is_valid is True

    by_leg = {l.leg: l for l in comp.legs}
    # 腿1 核心：热门 No
    assert by_leg[1].side == "NO"
    assert by_leg[1].bucket_id == "mid2"
    # 腿2 受害者：mid1
    assert by_leg[2].side == "YES"
    assert by_leg[2].bucket_id == "mid1"
    # 腿3 默认关闭 → 不出腿
    assert 3 not in by_leg
    # 预算比例（精力集中在腿1）：腿1=100%R=200，腿2=30%R=60
    assert abs(by_leg[1].budget_usd - 200.0) < 1.0
    assert abs(by_leg[2].budget_usd - 60.0) < 1.0
    assert abs(comp.total_budget() - 260.0) < 1.0


def test_hedge_leg_enabled():
    """打开 hedge_enabled（预留：拿到优质数据后再启用对冲）。"""
    cfg = AppConfig(env="test")
    cfg.legs.hedge_enabled = True
    market = _market_current()
    burst = _burst_quiet()
    snap = _snap(market)

    comp = LegSelector(cfg).select(snap, burst, equity=10_000.0)
    assert comp.is_valid is True
    # 腿3 对冲：上档 mid3/high 的 YES
    assert {l.bucket_id for l in comp.legs if l.leg == 3} >= {"mid3", "high"}
    # 腿3 预算 = 8%R = 16
    by_leg = {l.leg: l for l in comp.legs}
    assert abs(by_leg[3].budget_usd - 16.0) < 1.0


def test_skip_when_no_victim():
    cfg = AppConfig(env="test")
    market = _market_current()
    snap = _snap(market)
    # 伪造：所有区间都无下跌 → 无语受害者
    st2 = BurstState(); st2.record_quiet_start(
        {"low": 8.0, "mid1": 18.0, "mid2": 55.0, "mid3": 6.0, "high": 3.0}, "mid2")
    comp = LegSelector(cfg).select(snap, st2, equity=10_000.0)
    assert any(l.leg == 1 for l in comp.legs)      # 腿1 仍在
    assert not any(l.leg == 2 for l in comp.legs)  # 腿2 跳过
    assert "no-victim" in comp.skipped