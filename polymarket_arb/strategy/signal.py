"""信号层：门控 + 概率 + 三腿 → 最终入场信号。

任一门控不通过 → 不入场。入场还需 P_success >= P_MIN。
只有腿1（核心）必然在组合中；腿2/腿3 为可选增强。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import AppConfig
from ..data.events import CatalystCalendar
from ..features.burst import BurstState
from ..features.market_state import MarketSnapshot
from ..probability import p_success
from .gates import GateChecker, GateResult
from .legs import LegComposition, LegSelector


@dataclass
class Signal:
    market_slug: str
    action: str                     # "ENTER" | "HOLD" | "SKIP"
    x_c: float = 0.0                # 过热度 (¢)
    horizon_h: float = 0.0          # 剩余持有时间到结算 (h)
    p_success: float = 0.0
    gates: GateResult = field(default_factory=GateResult)
    legs: Optional[LegComposition] = None
    reason: str = ""

    def as_dict(self):
        return {
            "market": self.market_slug,
            "action": self.action,
            "x_c": self.x_c,
            "horizon_h": self.horizon_h,
            "p_success": round(self.p_success, 4),
            "gates": self.gates.as_dict(),
            "reason": self.reason,
        }


class SignalGenerator:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.leg_selector = LegSelector(cfg)

    def evaluate(
        self,
        market_slug: str,
        market_snapshot: MarketSnapshot,
        burst: BurstState,
        market_days_elapsed: float,
        calendar: Optional[CatalystCalendar],
        equity: float,
        in_position_count: int,
    ) -> Signal:
        market = market_snapshot.market
        # 1) 门控
        gates = GateChecker(
            cfg=self.cfg,
            market=market,
            market_days_elapsed=market_days_elapsed,
            burst=burst,
            snapshot=market_snapshot,
            calendar=calendar,
        ).check()

        if not gates.passed:
            return Signal(
                market_slug=market_slug,
                action="SKIP",
                reason=(";".join(gates.failed)),
                gates=gates,
            )

        # 2) 过热度与持有期
        hot = market_snapshot.hot
        x = burst.overheat_c(hot.price_c) if hot else 0.0
        if x <= 0:
            return Signal(market_slug=market_slug, action="SKIP",
                          x_c=x, gates=gates, reason="x<=0")

        hours_to_settle = 0.0
        if market.end is not None:
            hours_to_settle = _hours_until(market.end)
        x, hours_to_settle = _sanitize_meta(x, hours_to_settle)

        # 3) 概率门槛
        p = p_success(x, hours_to_settle, self.cfg.prob)
        if p < self.cfg.prob.p_min:
            return Signal(market_slug=market_slug, action="SKIP",
                          x_c=x, horizon_h=hours_to_settle, p_success=p,
                          gates=gates, reason=f"P={p:.3f} < P_MIN")

        # 4) 并发持仓上限
        if in_position_count >= self.cfg.sizing.max_positions:
            return Signal(market_slug=market_slug, action="SKIP",
                          x_c=x, horizon_h=hours_to_settle, p_success=p,
                          gates=gates, reason="max_positions")

        # 5) 三腿组合
        legs = self.leg_selector.select(market_snapshot, burst, equity)
        if not legs.is_valid:
            return Signal(market_slug=market_slug, action="SKIP",
                          x_c=x, horizon_h=hours_to_settle, p_success=p,
                          gates=gates, legs=legs, reason="no-legs")

        return Signal(
            market_slug=market_slug, action="ENTER",
            x_c=x, horizon_h=hours_to_settle, p_success=p,
            gates=gates, legs=legs, reason="viable-entry",
        )


def _hours_until(dt) -> float:
    from ..data.models import utcnow

    return max(0.0, (dt - utcnow()).total_seconds() / 3600.0)


def _sanitize_meta(x: float, h: float) -> tuple[float, float]:
    h = max(h, 1e-6)
    return x, h