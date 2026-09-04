"""回测引擎：沿每条事件的真实时间线，复现生产策略逻辑并结算。

复用的生产模块：
- features.burst.BurstTracker / evaluate_burst        → 爆发/安静/过热度 x
- strategy.legs.LegSelector                            → 三腿预算与数量
- probability.p_success                                → 回落概率（决定入场）

门控在引擎内按回测时刻显式执行（避免生产 GateChecker 依赖墙钟 utcnow）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..config import AppConfig
from ..data.models import Market, Interval
from ..features.burst import BurstTracker, evaluate_burst
from ..features.market_state import MarketSnapshot
from ..probability import p_success
from ..strategy.legs import Leg, LegSelector
from ..utils import get_logger

log = get_logger(__name__)


def _slope(vals: List[float]) -> float:
    """最小二乘斜率（每单位 time-step，即 ¢/分钟）。只有一个点或全平摊不开数值则返回 0。"""
    n = len(vals)
    if n < 2:
        return 0.0
    xs = list(range(n))
    xm = (n - 1) / 2.0
    ym = sum(vals) / n
    denom = sum((x - xm) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - xm) * (v - ym) for x, v in zip(xs, vals)) / denom


@dataclass
class Trade:
    event_slug: str
    entry_time_ts: float
    x_c: float
    p: float
    hours_to_end: float
    hot_bucket: str
    legs: List[Leg]
    budget_usd: float
    # 结算结果
    hot_won: bool = False
    pnl_usd: float = 0.0
    fee_usd: float = 0.0
    exit: str = "SETTLE"   # SETTLE=持有到结算 | TP=止盈 | SL=止损

    @property
    def win(self) -> bool:
        return self.pnl_usd > 0.0


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    skipped_reasons: List[str] = field(default_factory=list)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    def summary(self) -> dict:
        n = self.n_trades
        if n == 0:
            return {"n_trades": 0, "win_rate": None, "pnl": 0.0, "max_drawdown": 0.0}
        wins = sum(1 for t in self.trades if t.win)
        pnls = [t.pnl_usd for t in self.trades]
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        avg_pnl = 0.0
        for pnl in pnls:
            cum += pnl
            avg_pnl += pnl / n
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        return {
            "n_trades": n,
            "win_rate": round(wins / n, 4),
            "pnl": round(sum(pnls), 2),
            "avg_pnl": round(avg_pnl, 2),
            "max_drawdown": round(max_dd, 2),
            "avg_x": round(sum(t.x_c for t in self.trades) / n, 2),
            "avg_p": round(sum(t.p for t in self.trades) / n, 3),
        }


class BacktestEngine:
    def __init__(self, cfg: AppConfig, trade_cost_c: float = 1.0,
                 cost_on_buy_pct: float = 0.0):
        self.cfg = cfg
        self.trade_cost_c = trade_cost_c        # 每份摩擦成本 (¢)
        self.cost_on_buy_pct = cost_on_buy_pct  # 便于按中性价计费（0 表示用 cost_c）
        self.leg_selector = LegSelector(cfg)

    def run_event(self, ev, dataset, existing_burst: Optional[BurstTracker] = None
                  ) -> BacktestResult:
        if self.cfg.roundtrip.enabled:
            return self._run_roundtrip(ev, dataset, existing_burst)
        return self._run_hold(ev, dataset, existing_burst)

    def _entry_signal(self, dataset, burst, market, ev, i, t, hours_to_end,
                      days_elapsed, res):
        """入场信号：返回 (BurstState, hot_interval, x, ps) 或 None。"""
        g = self.cfg.gate; p = self.cfg.prob
        burst_w = self.cfg.burst.burst_window_h
        baseline_days = self.cfg.burst.baseline_days
        if not ev.live[i]:
            res.skipped_reasons.append("dead")
            return None
        if days_elapsed < g.night_day or hours_to_end <= g.settle_buffer_h:
            res.skipped_reasons.append("calendar")
            return None
        rate = dataset.post_counts(t, burst_w) / burst_w
        base = dataset.post_counts(t, 24.0 * baseline_days) / (24.0 * baseline_days)
        bursting, quiet = evaluate_burst(rate, base, self.cfg)
        self._apply_prices(ev, market, i)
        hot = market.find_hot_interval()
        if hot is None:
            return None
        price_map = {it.bucket_id: it.price_c for it in market.intervals}
        st = burst.update(ev.slug, rate, base, price_map, hot.bucket_id,
                          quiet, bursting)
        if not (quiet and st.quiet_start_price_c is not None):
            res.skipped_reasons.append("quiet")
            return None
        x = st.overheat_c(hot.price_c)
        if x <= 0:
            res.skipped_reasons.append("x<=0")
            return None
        # ---- 入场：检测 YES 上涨趋势的终结（非机械固定回落）----
        # 前段窗口斜率 ≥ slope_up（确在上涨），当前窗口斜率 ≤ slope_end
        # （上涨刚结束）→ 此时 NO 接近最低，买入。全用历史数据，无前视。
        rt = self.cfg.roundtrip
        hot_idx = market.intervals.index(hot)
        win = rt.trend_win_m
        cur_lo = max(0, i - win + 1)
        prev_lo = max(0, i - 2 * win + 1)
        try:
            _slope_now = _slope(
                [float(ev.probs[k, hot_idx]) * 100 for k in range(cur_lo, i + 1)])
            _slope_prev = _slope(
                [float(ev.probs[k, hot_idx]) * 100 for k in range(prev_lo, cur_lo)])
        except ValueError:
            res.skipped_reasons.append("slope-short")
            return None
        if not (_slope_prev >= rt.slope_up_c and _slope_now <= rt.slope_end_c):
            res.skipped_reasons.append("no-trend-end")
            return None
        ps = p_success(x, hours_to_end, p)
        if ps < p.p_min:
            res.skipped_reasons.append("p_min")
            return None
        return st, hot, x, ps

    def _run_roundtrip(self, ev, dataset, existing_burst: Optional[BurstTracker] = None
                       ) -> BacktestResult:
        """滚动出场：持仓中按 NO 涨跌触发止盈/止损，平掉后可再次入场，不等到结算。

        - 入场：同四门 + 概率门槛，买热门 NO（过热做空）。
        - 止盈/止损/结算强制平仓均按“NO 当前市值 - 成本 - 双边费用”计盈亏。
        - 提前出场即规避「热门最终获胜」的巨额尾部损失。
        """
        res = BacktestResult()
        burst: BurstTracker = existing_burst or BurstTracker(self.cfg)
        rt = self.cfg.roundtrip
        market = self._build_market(ev)
        pos = None
        n_entries = 0
        last_i = ev.n_steps() - 1

        for i in range(ev.n_steps()):
            t = float(ev.timestamps[i])
            hours_to_end = ev.hours_to_end(i)
            days_elapsed = (t - ev.start_ts) / 86400.0

            if pos is not None:
                self._apply_prices(ev, market, i)
                # 用入场时那个桶自己的 Yes 价度量 NO 现值（而非全市场最大热门价）
                own = market.intervals[pos["bucket_idx"]]
                cur_no = 100.0 - own.price_c
                pos["hist"].append(cur_no)
                h = pos["hist"]
                rt = self.cfg.roundtrip
                rise = cur_no - pos["entry_no"]
                exit_lbl = None

                # ---- 止盈：已回落 + 进入平缓平台期 ----
                if rise >= rt.rise_min_c and len(h) >= rt.plateau_win_m:
                    win = h[-rt.plateau_win_m:]
                    recent = max(win) - min(win)
                    drift = win[-1] - win[0]
                    if recent <= rt.plateau_range_c and abs(drift) <= rt.plateau_drift_c:
                        exit_lbl = "TP"

                # ---- 确认式止损：真·持续走高（默认关闭）----
                if exit_lbl is None and rt.sl_enabled:
                    if cur_no <= pos["entry_no"] - rt.sl_enter_c:
                        if "sl_arm" not in pos:
                            pos["sl_arm"] = t
                        # 确认期内需一直低沉（未回到 触发点+recover）
                        if t - pos["sl_arm"] >= rt.sl_dwell_m * 60:
                            exit_lbl = "SL"
                    else:
                        pos.pop("sl_arm", None)   # 收回 → 非真跌 → 解除

                # ---- 结算强制平仓 ----
                if exit_lbl is None and i == last_i:
                    exit_lbl = "SETTLE"

                if exit_lbl is not None:
                    if exit_lbl in ("TP", "SL"):
                        close_price = cur_no
                    else:
                        close_price = 100.0 if not pos["hot_won"] else 0.0
                    qty = pos["qty"]
                    cost = qty * pos["entry_no"] / 100.0
                    proceeds = qty * close_price / 100.0
                    # 双边手续费（买+卖）
                    fee = qty * (self.trade_cost_c / 100.0) * 2
                    pnl = proceeds - cost - fee
                    res.trades.append(Trade(
                        event_slug=ev.slug, entry_time_ts=pos["entry_ts"],
                        x_c=pos["x"], p=pos["p"], hours_to_end=pos["h2end"],
                        hot_bucket=pos["hot_bucket"], legs=[pos["leg"]],
                        budget_usd=cost, hot_won=pos["hot_won"],
                        pnl_usd=pnl, fee_usd=fee, exit=exit_lbl,
                    ))
                    pos = None
                continue

            sig = self._entry_signal(dataset, burst, market, ev, i, t,
                                     hours_to_end, days_elapsed, res)
            if sig is None:
                continue
            _st, hot, x, ps = sig
            if n_entries >= rt.max_roundtrips:
                res.skipped_reasons.append("max_roundtrips")
                continue
            budget = min(self.cfg.sizing.max_trade_notional_usd,
                         self.cfg.prob.risk_pct * 10_000.0)
            no_price = max(100.0 - hot.price_c, 1.0)
            qty = budget / (no_price / 100.0)
            leg = Leg(leg=1, side="NO", bucket_id=hot.bucket_id,
                      token_id=hot.no_clob_token, price_c=no_price,
                      budget_usd=budget, quantity=qty,
                      reason=f"滚动做空过热 x={x:.1f}¢ no={no_price:.0f}¢")
            pos = {"entry_no": no_price, "qty": qty, "entry_ts": t, "x": x,
                   "p": ps, "h2end": hours_to_end, "hist": [no_price],
                   "hot_bucket": hot.bucket_id, "bucket_idx": market.intervals.index(hot),
                   "hot_won": (ev.winner_bracket_idx == market.intervals.index(hot)),
                   "leg": leg}
            n_entries += 1
            # 触发后才 refresh halt（避免连开）；下一轮从信号状态继续
        return res

    def _run_hold(self, ev, dataset, existing_burst: Optional[BurstTracker] = None
                  ) -> BacktestResult:
        res = BacktestResult()
        burst: BurstTracker = existing_burst or BurstTracker(self.cfg)
        g = self.cfg.gate
        p = self.cfg.prob
        burst_w = self.cfg.burst.burst_window_h
        baseline_days = self.cfg.burst.baseline_days

        # 事件 Market（区间骨架固定，价格每步刷新）
        market = self._build_market(ev)

        for i in range(ev.n_steps()):
            t = float(ev.timestamps[i])
            # 非活跃时刻（概率质量全 0，市场未开盘/已停 或模型未产出）跳过状态机，避免锚定 0 价
            if not ev.live[i]:
                res.skipped_reasons.append("dead")
                continue
            # repost 检查（结算前缓冲/第 N 天已由当日匹配管理）
            hours_to_end = ev.hours_to_end(i)
            days_elapsed = (t - ev.start_ts) / 86400.0
            # 日历门（仍要在冲撞前再查一次是否仍满足，防止结算末段入场）
            if days_elapsed < g.night_day or hours_to_end <= g.settle_buffer_h:
                res.skipped_reasons.append("calendar")
                continue

            # 发帖速率
            rate = dataset.post_counts(t, burst_w) / burst_w
            base = dataset.post_counts(t, 24.0 * baseline_days) / (24.0 * baseline_days)
            bursting, quiet = evaluate_burst(rate, base, self.cfg)

            # 刷新区间价格 → 快照
            self._apply_prices(ev, market, i)
            hot = market.find_hot_interval()
            if hot is None:
                continue
            price_map = {it.bucket_id: it.price_c for it in market.intervals}
            st = burst.update(ev.slug, rate, base, price_map, hot.bucket_id,
                              quiet, bursting)

            # 门控（引擎内显式）
            if not (quiet and st.quiet_start_price_c is not None):
                res.skipped_reasons.append("quiet")
                continue

            x = st.overheat_c(hot.price_c)
            if x <= 0:
                res.skipped_reasons.append("x<=0")
                continue
            ps = p_success(x, hours_to_end, p)
            if ps < p.p_min:
                res.skipped_reasons.append("p_min")
                continue

            # 命中：三腿
            snap = self._snapshot(market, hot)
            legs = self.leg_selector.select(snap, st, equity=10_000.0)
            hot_idx = market.intervals.index(hot)
            hot_won = (ev.winner_bracket_idx == hot_idx)

            trade = Trade(
                event_slug=ev.slug, entry_time_ts=t,
                x_c=round(x, 2), p=ps, hours_to_end=round(hours_to_end, 2),
                hot_bucket=hot.bucket_id, legs=legs.legs,
                budget_usd=legs.total_budget(),
                hot_won=hot_won,
            )
            trade.pnl_usd, trade.fee_usd = self._settle(ev, legs)
            res.trades.append(trade)
            # 每事件只取首个达标信号入场
            break

        return res

    # ---- 辅助 ----
    def _build_market(self, ev) -> Market:
        start = _dt(ev.start_ts)
        end = _dt(ev.end_ts)
        intervals = []
        for bi, (lo, hi) in enumerate(ev.brackets):
            intervals.append(Interval(
                bucket_id=f"b{bi}", low=lo, high=hi,
                yes_clob_token=f"y-{ev.slug}-{bi}", no_clob_token=f"n-{ev.slug}-{bi}",
                price_c=0.0, ask_c=0.0, bid_c=0.0,
            ))
        return Market(slug=ev.slug, start=start, end=end, intervals=intervals)

    @staticmethod
    def _apply_prices(ev, market: Market, i: int) -> None:
        for bi, it in enumerate(market.intervals):
            pr = float(ev.probs[i, bi]) * 100.0   # prob → ¢
            it.price_c = round(pr, 2)
            it.ask_c = it.price_c                 # 无盘口，用中性价
            it.bid_c = it.price_c

    @staticmethod
    def _snapshot(market: Market, hot: Interval) -> MarketSnapshot:
        return MarketSnapshot(
            market=market, price_sum_c=100.0, hot=hot,
            hot_price_c=hot.price_c, hot_ask_c=hot.ask_c, hot_bid_c=hot.bid_c,
            errors=[],
        )

    def _settle(self, ev, legs) -> tuple[float, float]:
        """结算：热点是否胜出 → 分腿赔付 → PnL。"""
        winner_idx = ev.winner_bracket_idx
        panl = 0.0
        fee = 0.0
        for leg in legs.legs:
            bucket_idx = int(leg.bucket_id.lstrip("b")) if leg.bucket_id.startswith("b") else -1
            if leg.side == "NO":       # 腿1：热门输则 No 赔付 1
                payout = 0.0 if bucket_idx == winner_idx else 1.0
            else:                      # YES：该区间胜则赔付 1
                payout = 1.0 if bucket_idx == winner_idx else 0.0
            cost = leg.price_c / 100.0
            shares = leg.quantity
            fee_here = shares * (self.trade_cost_c / 100.0)
            fee += fee_here
            panl += shares * (payout - cost) - fee_here
        return panl, fee


def _dt(ts: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)