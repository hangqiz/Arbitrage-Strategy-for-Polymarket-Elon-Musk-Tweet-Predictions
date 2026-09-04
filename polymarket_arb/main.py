"""主循环：每 5 分钟轮询一次。

数据 → 特征 → 信号(门控+概率+三腿) → 执行 → 风控/日志/告警。
dry_run 默认开启，不下真实单。
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Optional

from .alerts.telegram import TelegramNotifier
from .config import AppConfig
from .data.clob_client import MidpriceClient, MockMidpriceClient
from .data.events import CatalystCalendar
from .data.models import Market, Interval, utcnow
from .data.posting_rate import PostingRateProvider, make_rate_provider
from .execution.orders import OrderPlacer, make_order_placer
from .execution.risk import PosState, RiskManager
from .features.burst import BurstTracker, evaluate_burst
from .features.market_state import build_snapshot
from .storage.sqlite_store import SqliteStore
from .strategy.signal import SignalGenerator
from .utils import get_logger

log = get_logger(__name__)


class StrategyBot:
    """把各层串起来的状态机机器人。"""

    def __init__(self, cfg: AppConfig, market: Market,
                 rate_provider: PostingRateProvider,
                 midprice: MidpriceClient,
                 calendar: Optional[CatalystCalendar] = None,
                 initial_equity: float = 10_000.0):
        self.cfg = cfg
        self.market = market
        self.rates = rate_provider
        self.midprice = midprice
        self.calendar = calendar

        self.bursts = BurstTracker(cfg)
        self.signal_gen = SignalGenerator(cfg)
        self.risk = RiskManager(cfg, initial_equity=initial_equity)
        self.placer: OrderPlacer = make_order_placer(cfg)
        self.store = SqliteStore(cfg.sqlite_path)
        self.notifier = TelegramNotifier(cfg)

    def market_days_elapsed(self) -> float:
        if self.market.start is None:
            return 999.0  # 未设置开始时间则放行
        return max(0.0, (utcnow() - self.market.start).total_seconds() / 86400.0)

    def poll_once(self) -> None:
        cfg = self.cfg
        slug = self.market.slug

        # 1) 发帖速率与基线
        baseline = self.rates.baseline_rate(cfg.burst.baseline_days)
        rate = self.rates.rate_per_hour(cfg.burst.burst_window_h)
        bursting, quiet = evaluate_burst(rate, baseline, cfg)
        log.debug("[%s] rate=%.2f/h baseline=%.2f bursting=%s quiet=%s",
                  slug, rate, baseline, bursting, quiet)

        # 2) 市场快照（刷新各区间价格）
        snap = build_snapshot(self.market, self.midprice)
        if snap.hot is None:
            log.warning("[%s] 无热门区间，跳过", slug)
            return

        # 3) 更新爆发状态
        prices = {i.bucket_id: i.price_c for i in self.market.intervals}
        burst_state = self.bursts.update(
            slug, rate, baseline, prices, snap.hot.bucket_id, quiet, bursting
        )
        # 记录确认安静窗口（用于 μ_quiet 校准）
        if not bursting and burst_state.quiet_start_price_c is not None \
                and burst_state.hot_bucket != "":
            self.store.mark_quiet(slug, baseline)

        # 4) 生成信号
        sig = self.signal_gen.evaluate(
            market_slug=slug,
            market_snapshot=snap,
            burst=burst_state,
            market_days_elapsed=self.market_days_elapsed(),
            calendar=self.calendar,
            equity=self.risk.equity,
            in_position_count=self.risk.open_count(),
        )
        self.store.log_signal(
            slug, sig.action, sig.x_c, sig.horizon_h, sig.p_success,
            sig.reason, ";".join(sig.gates.failed),
        )

        # 5) 执行
        if sig.action == "ENTER" and sig.legs:
            if not self.risk.begin_entry(slug, sig.legs):
                return
            result = self.placer.place_legs(slug, sig.legs)
            if result.ok:
                self.risk.confirm_open(slug)
                self._log_orders(slug, sig.legs)
                self.store.upsert_position(
                    slug, PosState.OPEN.value, utcnow().isoformat(),
                    sig.legs.total_budget(), sig.reason,
                )
                self.notifier.notify_signal(slug, "ENTER", sig.reason)
        else:
            log.info("[%s] 信号=%s (%s) x=%.1f¢ P=%.3f",
                     slug, sig.action, sig.reason, sig.x_c, sig.p_success)

    def _log_orders(self, slug: str, legs) -> None:
        for leg in legs.legs:
            self.store.log_order(
                slug, leg.leg, leg.side, leg.bucket_id, leg.token_id,
                leg.price_c, leg.quantity, leg.budget_usd, "open",
            )

    def run(self) -> None:
        log.info("启动策略：%s (dry_run=%s, 每 %ds 轮询)",
                 self.market.slug, self.cfg.dry_run, self.cfg.poll_interval_sec)
        while True:
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 - 单轮异常不应杀死进程
                log.exception("轮询异常: %s", e)
            time.sleep(self.cfg.poll_interval_sec)


# ---------------------------------------------------------------------------
# 演示环境（不联网、可直接跑）
# ---------------------------------------------------------------------------
def build_demo_market() -> Market:
    now = utcnow()
    start = now - timedelta(days=5)
    end = now + timedelta(days=11)
    # 5 个互斥区间，构造：价格很接近零和，soft-hot 中档
    defs = [
        ("low", None, 300, 8.0),
        ("mid1", 300, 450, 22.0),
        ("mid2", 450, 600, 40.0),   # 热门（当前最高）
        ("mid3", 600, 800, 22.0),
        ("high", 800, None, 8.0),
    ]
    intervals = [
        Interval(bucket_id=b, low=lo, high=hi,
                 yes_clob_token=f"yes-{b}:{p}", no_clob_token=f"no-{b}:{p}",
                 price_c=p)
        for b, lo, hi, p in defs
    ]
    return Market(slug="demo-musk-weekly", start=start, end=end,
                  question="马斯克本周发帖数区间（演示）", intervals=intervals)


def run_demo(polls: int = 3, quiet: bool = False) -> None:
    """离线演示：用 mock 数据 + mock 盘口跑几轮。"""
    cfg = AppConfig(env="test", posting_source="mock", dry_run=True)
    market = build_demo_market()
    bot = StrategyBot(cfg, market,
                      rate_provider=make_rate_provider(cfg),
                      midprice=MockMidpriceClient(
                          {f"yes-{i.bucket_id}:{i.price_c}": i.price_c
                           for i in market.intervals}
                      ),
                      initial_equity=10_000.0)
    # 手动推进几轮
    for _ in range(polls):
        bot.poll_once()
        if quiet:
            time.sleep(0)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket 波动做空套利")
    parser.add_argument("--demo", action="store_true", help="离线演示")
    parser.add_argument("--run", action="store_true", help="持续运行（真实模式）")
    parser.add_argument("--config", default=".env", help="env 文件路径")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return
    if not args.run:
        parser.print_help()
        return

    cfg = AppConfig(env="production")
    from .data.data_factory import build_production_env

    market, midprice, rates, calendar = build_production_env(cfg)
    StrategyBot(cfg, market, rates, midprice, calendar, initial_equity=10_000.0).run()


if __name__ == "__main__":  # 支持 `python -m polymarket_arb.main`
    main()