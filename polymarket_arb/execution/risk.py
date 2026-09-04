"""风控层：持仓状态机 + 硬性风险约束。

状态：IDLE -> ENTERING -> OPEN -> EXITING -> CLOSED（每市场独立）。
约束：
- 单笔风险 = risk_pct × equity，绝不超投。
- 并发持仓 <= max_positions。
- 低资金（<<min_equity）强制不开新仓。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..config import AppConfig
from ..strategy.legs import LegComposition
from ..utils import get_logger

log = get_logger(__name__)


class PosState(str, Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"


@dataclass
class Position:
    market_slug: str
    state: PosState = PosState.IDLE
    legs: Optional[LegComposition] = None
    entered_at: Optional[datetime] = None
    notes: str = ""

    def as_dict(self):
        return {
            "market": self.market_slug, "state": self.state.value,
            "entered_at": self.entered_at.isoformat() if self.entered_at else None,
            "notes": self.notes,
            "budget_usd": round(self.legs.total_budget(), 2) if self.legs else 0.0,
        }


class RiskManager:
    def __init__(self, cfg: AppConfig, initial_equity: float):
        self.cfg = cfg
        self.equity = initial_equity
        self._positions: dict[str, Position] = {}

    # ---- 查询 ----
    def positions(self) -> List[Position]:
        return list(self._positions.values())

    def open_count(self) -> int:
        return sum(1 for p in self._positions.values()
                   if p.state in (PosState.ENTERING, PosState.OPEN))

    def get(self, market_slug: str) -> Position:
        return self._positions.setdefault(market_slug, Position(market_slug))

    # ---- 门 ----
    def can_open(self) -> tuple[bool, str]:
        if self.equity < self.cfg.sizing.min_equity_usd:
            return False, f"equity<min({self.equity:.0f}$)"
        if self.open_count() >= self.cfg.sizing.max_positions:
            return False, "max_positions"
        return True, "ok"

    # ---- 状态流转 ----
    def begin_entry(self, market_slug: str, legs: LegComposition) -> bool:
        ok, why = self.can_open()
        if not ok:
            log.warning("[%s] 拒绝开仓: %s", market_slug, why)
            return False
        pos = self.get(market_slug)
        if pos.state == PosState.OPEN:
            log.warning("[%s] 已存在 OPEN 持仓，拒绝重复入场", market_slug)
            return False
        pos.legs = legs
        pos.state = PosState.ENTERING
        pos.entered_at = datetime.now(timezone.utc)
        log.info("[%s] 进入 ENTERING，预算=%.2f$", market_slug, legs.total_budget())
        return True

    def confirm_open(self, market_slug: str) -> None:
        pos = self.get(market_slug)
        pos.state = PosState.OPEN
        # 从此单风险资金中预留预算
        self.equity -= max(0.0, self._deployed(pos))
        log.info("[%s] 已 OPEN，剩余权益=%.2f$", market_slug, self.equity)

    def begin_exit(self, market_slug: str) -> None:
        pos = self.get(market_slug)
        pos.state = PosState.EXITING

    def confirm_closed(self, market_slug: str, realized: float) -> None:
        pos = self.get(market_slug)
        self.equity += realized
        pos.state = PosState.CLOSED
        log.info("[%s] 已 CLOSED，实现盈亏=%.2f$，权益=%.2f$",
                 market_slug, realized, self.equity)

    @staticmethod
    def _deployed(pos: Position) -> float:
        return pos.legs.total_budget() if pos.legs else 0.0