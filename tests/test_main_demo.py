from polymarket_arb.main import build_demo_market, run_demo


def test_demo_market_consistency():
    m = build_demo_market()
    assert len(m.intervals) >= 3
    assert m.find_hot_interval() is not None
    # 近似零和：价格和接近 100
    total = sum(i.price_c for i in m.intervals)
    assert 90 <= total <= 110, total


def test_demo_runs_and_logs_signals():
    run_demo(polls=2)
    # 至少没有崩溃即通过