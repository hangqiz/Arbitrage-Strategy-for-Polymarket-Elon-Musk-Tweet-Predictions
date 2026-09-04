from polymarket_arb.config import AppConfig
from polymarket_arb.features.burst import (
    BurstTracker,
    evaluate_burst,
    is_burst,
    is_quiet,
)


def test_is_burst_and_quiet():
    cfg = AppConfig(env="test")
    assert is_burst(5.0, 2.0, cfg.burst.burst_mult) is True   # 5 >= 4
    assert is_burst(2.0, 2.0, cfg.burst.burst_mult) is False
    assert is_quiet(2.0, 2.0, cfg.burst.quiet_mult) is True   # 2 <= 2.5
    assert is_quiet(4.0, 2.0, cfg.burst.quiet_mult) is False


def test_tracker_transitions_to_quiet_and_anchors():
    cfg = AppConfig(env="test")
    tr = BurstTracker(cfg)
    prices = {"low": 10.0, "mid1": 20.0, "hot": 60.0, "high": 10.0}

    # 爆发
    st = tr.update("m", rate=6.0, baseline=2.0, prices=prices,
                   hot_bucket="hot", quiet=False, bursting=True)
    assert st.in_burst is True
    assert st.quiet_start_price_c is None

    # 爆发结束 → 安静，锚定 hot=60
    st = tr.update("m", rate=1.5, baseline=2.0, prices=prices,
                   hot_bucket="hot", quiet=True, bursting=False)
    assert st.in_burst is False
    assert st.quiet_start_price_c == 60.0

    # 过热度：当前 hot 涨到 68 → x=8
    prices2 = dict(prices); prices2["hot"] = 68.0
    assert st.overheat_c(68.0) == 8.0
    # 受害者 mid1 自安静开始跌 20→14 → drop=+6（价格下跌幅度为正）
    assert st.quiet_change_c("low", 10.0) == 0.0  # 未变化


def test_evaluate_burst():
    cfg = AppConfig(env="test")
    b, q = evaluate_burst(6.0, 2.0, cfg)   # 6 >= 4 爆发
    assert b is True and q is False
    b2, q2 = evaluate_burst(1.5, 2.0, cfg)  # 1.5 <= 2.5 安静
    assert b2 is False and q2 is True