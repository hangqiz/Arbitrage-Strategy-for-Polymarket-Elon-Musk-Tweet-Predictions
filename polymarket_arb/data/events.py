"""催化剂事件日历：判断未来 H 小时内是否有已知事件。

事件来源：人工维护的事件表（财报、发射、重大发布等）。启动时加载，
main 循环按需查询：若未来 H 小时内存在事件则拒绝入场（催化剂门）。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..utils import get_logger

log = get_logger(__name__)


@dataclass
class CatalystEvent:
    ts: datetime
    label: str


def _parse(dt: str) -> datetime:
    dt = dt.replace("Z", "+00:00")
    if "+" not in dt and "Z" not in dt:
        dt += "+00:00"
    return datetime.fromisoformat(dt).astimezone(timezone.utc)


class CatalystCalendar:
    """事件日历，提供「未来 H 小时内是否有事件」查询。"""

    def __init__(self, events: Optional[List[CatalystEvent]] = None):
        self.events: List[CatalystEvent] = events or []

    @classmethod
    def from_csv(cls, path: str) -> "CatalystCalendar":
        events: List[CatalystEvent] = []
        with open(path) as f:
            for row in csv.DictReader(f):
                events.append(CatalystEvent(ts=_parse(row["ts"]), label=row["label"]))
        log.info("加载 %d 个催化剂事件", len(events))
        return cls(events)

    def event_within(self, now: datetime, window_h: float) -> Optional[CatalystEvent]:
        """返回 now+window_h 之前最近的未来事件（不含已过期）。"""
        horizon = now + timedelta(hours=window_h)
        upcoming = [e for e in self.events if now <= e.ts <= horizon]
        return min(upcoming, key=lambda e: e.ts) if upcoming else None


def make_calendar(path: Optional[str]) -> CatalystCalendar:
    if path:
        try:
            return CatalystCalendar.from_csv(path)
        except FileNotFoundError:
            log.warning("事件表不存在: %s，催化剂门视为通过", path)
    return CatalystCalendar()