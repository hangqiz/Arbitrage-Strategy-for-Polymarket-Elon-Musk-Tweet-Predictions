"""四个硬门控（plan 四）。任一不满足则不入场。

1. 日历门 ：市场开始第 3 天之后、结算前 6 小时之外。
2. 安静门 ：速率 <= 1.25×λ_b 已确认并锚定价格。
3. 催化剂门：未来 H 小时内无已知事件。
4. 流动性门：热门区间买卖价差 <= 1.5¢。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..config import AppConfig
from ..data.events import CatalystCalendar
from ..data.models import Market, utcnow
from ..features.burst import BurstState
from ..features.market_state import MarketSnapshot


@dataclass
class GateResult:
    passed: bool
    failed: List[str] = field(default_factory=list)

    def as_dict(self):
        return {"passed": self.passed, "failed": self.failed}


class GateChecker:
    def __init__(
        self,
        cfg: AppConfig,
        market: Market,
        market_days_elapsed: float,
        burst: BurstState,
        snapshot: MarketSnapshot,
        calendar: Optional[CatalystCalendar] = None,
    ):
        self.cfg = cfg
        self.market = market
        self.market_days_elapsed = market_days_elapsed
        self.burst = burst
        self.snapshot = snapshot
        self.calendar = calendar

    def calendar_gate(self) -> bool:
        """第 3 天之后且结算前 6 小时之外。"""
        g = self.cfg.gate
        if self.market.end is not None:
            hours_left = (self.market.end - utcnow()).total_seconds() / 3600.0
            if hours_left <= g.settle_buffer_h:
                return False
        return self.market_days_elapsed >= g.night_day

    def quiet_gate(self) -> bool:
        """已确认进入安静（BurstState 已锚定安静开始价）。"""
        return self.burst.quiet_start_price_c is not None and not self.burst.in_burst

    def catalyst_gate(self) -> bool:
        if self.calendar is None:
            return True
        ev = self.calendar.event_within(utcnow(), self.cfg.gate.event_overlap_h)
        return ev is None

    def liquidity_gate(self) -> bool:
        spread = self.snapshot.hot_spread_c
        if spread is None:
            return False
        return spread <= self.cfg.gate.max_spread_c

    def check(self) -> GateResult:
        failed: List[str] = []

        if not self.calendar_gate():
            failed.append("calendar")
        if not self.quiet_gate():
            failed.append("quiet")
        if not self.catalyst_gate():
            failed.append("catalyst")
        if not self.liquidity_gate():
            failed.append("liquidity")

        return GateResult(passed=len(failed) == 0, failed=failed)