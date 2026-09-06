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
        rt = self.cfg.roundtrip
        burst_w = self.cfg.burst.burst_window_h
        baseline_days = self.cfg.burst.baseline_days
        if not ev.live[i]:
            res.skipped_reasons.append("dead")
            return None
        if days_elapsed < g.night_day or hours_to_end <= g.settle_buffer_h:
            res.skipped_reasons.append("calendar")
            return None
        # 收盘前 no_entry_within_h 小时停止入场（结算前大部分区间价格持续下行）
        if hours_to_end <= rt.no_entry_within_h:
            res.skipped_reasons.append("near-close")
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
        hot_idx = market.intervals.index(hot)
        # ---- 统一进场信号（兼容两种风格）----
        # 热门：量大多小时缓爬/缓降（慢趋势）；其上冷门：量小价格剧烈上升（飙升）。
        # 二者只要满足"窗口内涨幅≥min_rise 且已见顶回落"即视为过热信号。
        # 瞬态尖峰（机器人打的 ±8¢ 单跳）因回落使 amp≈0、又不满足连续确认，自动被滤除。
        if not self._froth_any(ev, market, hot_idx, i):
            res.skipped_reasons.append("no-froth")
            return None
        if not all(self._froth_any(ev, market, hot_idx, i - b)
                   for b in range(1, max(1, rt.entry_confirm_m))):
            res.skipped_reasons.append("not-confirmed")
            return None
        ps = p_success(x, hours_to_end, p)
        if ps < p.p_min:
            res.skipped_reasons.append("p_min")
            return None
        return st, hot, x, ps

    def _froth_any(self, ev, market, hot_idx: int, i: int) -> bool:
        """热门及其上方区间中，是否存在"窗口内涨幅达标 + 已见顶回落"的过热信号。

        用 hot 所在桶的份额链（hot..hot+top_k-1）逐一探测，两种风格皆识别。
        """
        rt = self.cfg.roundtrip
        top = min(hot_idx + rt.leg1_top_k, len(market.intervals))
        return any(self._froth_roll(ev, j, i) for j in range(hot_idx, top))

    def _froth_roll(self, ev, idx: int, i: int) -> bool:
        """某区间在回看窗口(froth_win_m)内是否上涨终结：
        - 窗口内涨幅 ≥ min_rise_c（慢爬/快涨皆算）
        - 且已从峰值回落 ≥ roll_confirm_c 或 当前段斜率 ≤0（上涨终结）。
        全用历史数据，无前视；瞬态尖峰回落后涨幅≈0 故不命中。
        """
        rt = self.cfg.roundtrip
        win = rt.froth_win_m
        s = max(0, i - win + 1)
        if i - s + 1 < win:
            return False
        vals = [float(ev.probs[k, idx]) * 100 for k in range(s, i + 1)]
        cur = vals[-1]
        amp = cur - vals[0]
        if amp < rt.min_rise_c:
            return False
        roll = max(vals) - cur
        if roll >= rt.roll_confirm_c:
            return True
        return _slope(vals) <= 0.0

    def _has_spike(self, ev, hot_idx: int, i: int) -> bool:
        """回看 spike_win_m 分钟内，是否出现 ≥spike_jump_c 的单分钟跳变。"""
        rt = self.cfg.roundtrip
        lo = max(1, i - rt.spike_win_m + 1)
        for k in range(lo, i + 1):
            jump = abs(float(ev.probs[k, hot_idx]) - float(ev.probs[k - 1, hot_idx])) * 100.0
            if jump >= rt.spike_jump_c:
                return True
        return False

    def _trend_end(self, ev, hot_idx: int, i: int) -> bool:
        """YES 上涨趋势是否在 i 时刻终结：前段斜率 ≥slope_up，当前段斜率 ≤slope_end。"""
        rt = self.cfg.roundtrip
        win = rt.trend_win_m
        cur_lo = max(0, i - win + 1)
        prev_lo = max(0, i - 2 * win + 1)
        if cur_lo - prev_lo < 2 or i - cur_lo + 1 < 2:
            return False
        now = _slope([float(ev.probs[k, hot_idx]) * 100 for k in range(cur_lo, i + 1)])
        prev = _slope([float(ev.probs[k, hot_idx]) * 100 for k in range(prev_lo, cur_lo)])
        return prev >= rt.slope_up_c and now <= rt.slope_end_c

    def _run_roundtrip(self, ev, dataset, existing_burst: Optional[BurstTracker] = None
                       ) -> BacktestResult:
        """滚动出场：过热带一篮子做空，NO 上涨进入平台即止盈，平掉后可再入场。

        - 入场：四门 + 概率门槛 + 趋势终结 + 尖峰过滤，买入"热门 + 其上方区间"
          （腿1 篮子）的 NO → 做空过热区带，分散单区间最终获胜的尾部风险。
        - 止盈：NO 加权平均已上涨 ≥rise_min_c 且进入短平台/停滞 → 抢在二次飙升前卖出。
        - 结算：篮子内每个区间，赢家赔付 0、其余赔付 100¢。
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
                # 异常尖峰分钟不平仓（避免按人为报价成交）
                if self._has_spike(ev, pos["hot_idx"], i):
                    continue
                # 篮子加权平均 NO 现值（权重=各桶份数）
                wsum = wq = 0.0
                for b in pos["buckets"]:
                    cur_no = 100.0 - market.intervals[b["idx"]].price_c
                    wsum += cur_no * b["qty"]
                    wq += b["qty"]
                avg_no = wsum / wq if wq > 0 else 0.0
                pos["hist"].append(avg_no)
                h = pos["hist"]
                rise = avg_no - pos["entry_no"]
                exit_lbl = None

                # ---- 止盈：已回落 + 快速平台/停滞（抢在二次飙升前）----
                if rise >= rt.rise_min_c:
                    if len(h) >= rt.fast_stall_m:
                        win = h[-rt.fast_stall_m:]
                        if (max(win) - min(win) <= rt.fast_stall_range_c
                                and abs(_slope(win)) <= rt.fast_slope_c):
                            exit_lbl = "TP"
                    if exit_lbl is None and len(h) >= rt.plateau_win_m:
                        win = h[-rt.plateau_win_m:]
                        recent = max(win) - min(win)
                        drift = win[-1] - win[0]
                        if recent <= rt.plateau_range_c and abs(drift) <= rt.plateau_drift_c:
                            exit_lbl = "TP"

                # ---- 确认式止损：真·持续走高（默认关闭）----
                if exit_lbl is None and rt.sl_enabled:
                    if avg_no <= pos["entry_no"] - rt.sl_enter_c:
                        if "sl_arm" not in pos:
                            pos["sl_arm"] = t
                        if t - pos["sl_arm"] >= rt.sl_dwell_m * 60:
                            exit_lbl = "SL"
                    else:
                        pos.pop("sl_arm", None)

                # ---- 结算强制平仓 ----
                if exit_lbl is None and i == last_i:
                    exit_lbl = "SETTLE"

                if exit_lbl is not None:
                    cost = proceeds = qty_total = 0.0
                    for b in pos["buckets"]:
                        if exit_lbl in ("TP", "SL"):
                            close_no = 100.0 - market.intervals[b["idx"]].price_c
                        else:
                            close_no = 100.0 if b["idx"] != ev.winner_bracket_idx else 0.0
                        cost += b["qty"] * b["entry_no"] / 100.0
                        proceeds += b["qty"] * close_no / 100.0
                        qty_total += b["qty"]
                    fee = qty_total * (self.trade_cost_c / 100.0) * 2
                    pnl = proceeds - cost - fee
                    res.trades.append(Trade(
                        event_slug=ev.slug, entry_time_ts=pos["entry_ts"],
                        x_c=pos["x"], p=pos["p"], hours_to_end=pos["h2end"],
                        hot_bucket=pos["hot_bucket"], legs=pos["legs"],
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
            # ---- 腿1 篮子：热门 + 其上 top_k-1 个区间 ----
            # 预算不平均分：量越大(价格为代理)的区间分得越多 → 精力集中在热门。
            hi = market.intervals.index(hot)
            top = []
            for j in range(hi, min(hi + rt.leg1_top_k, len(market.intervals))):
                it = market.intervals[j]
                no_p = 100.0 - it.price_c
                if no_p < 1.0:      # NO 已 ≈100¢，无利润空间，不进
                    continue
                top.append(it)
            if not top:
                res.skipped_reasons.append("no-basket")
                continue
            if rt.leg1_weight_by_price:
                wts = [max(float(it.price_c), 1.0) for it in top]   # 预算∝价格≈量
                span = sum(wts)
                shares = [budget * w / span for w in wts]
            else:
                shares = [budget / len(top)] * len(top)
            buckets, legs = [], []
            for it, share in zip(top, shares):
                no_p = max(100.0 - it.price_c, 1.0)
                qty = share / (no_p / 100.0)
                buckets.append({"idx": market.intervals.index(it),
                                "bucket_id": it.bucket_id,
                                "entry_no": no_p, "qty": qty})
                legs.append(Leg(leg=1, side="NO", bucket_id=it.bucket_id,
                                token_id=it.no_clob_token, price_c=no_p,
                                budget_usd=share, quantity=qty,
                                reason=f"做空过热带 x={x:.1f}¢ no={no_p:.0f}¢"))
            pos = {"buckets": buckets, "legs": legs, "hist": [],
                   "entry_no": sum(b["qty"] * b["entry_no"] for b in buckets)
                               / sum(b["qty"] for b in buckets),
                   "entry_ts": t, "x": x, "p": ps, "h2end": hours_to_end,
                   "hot_bucket": hot.bucket_id, "hot_idx": hi,
                   "hot_won": (ev.winner_bracket_idx == hi)}
            n_entries += 1
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