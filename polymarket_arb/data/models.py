"""数据模型：马斯克周发帖数「区间」预测市场。

Polymarket 用一个二元市场（Yes/No）代表一个发帖数区间（bucket）。
所有区间的 Yes 价格之和 ≈ 1（零和/分割约束）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class Interval:
    """一个发帖量区间及其 CLOB token。"""

    bucket_id: str            # 例如 "low", "mid-1", "high"
    low: Optional[float]      # 区间下界（发帖数），None 表示无下界
    high: Optional[float]     # 区间上界，None 表示无上界
    yes_clob_token: str       # 买入该区间的 Yes token 的 clobTokenId
    no_clob_token: str        # 买入该区间的 No ttoken 的 clobTokenId
    price_c: float = 0.0      # Yes 当前中间价 (¢)
    ask_c: float = 0.0        # Yes 卖单价 (¢)
    bid_c: float = 0.0        # Yes 买单价 (¢)

    def label(self) -> str:
        if self.low is None:
            return f"<{int(self.high)}"
        if self.high is None:
            return f">={int(self.low)}"
        return f"{int(self.low)}-{int(self.high)}"


@dataclass
class Market:
    """整个预测市场 = 多个互斥区间。"""

    slug: str
    question: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    intervals: List[Interval] = field(default_factory=list)

    def interval_map(self) -> Dict[str, Interval]:
        return {i.bucket_id: i for i in self.intervals}

    @property
    def is_format_live(self) -> bool:
        """简单判断市场是否有效（有区间且有结算时点）。"""
        return len(self.intervals) > 0 and self.end is not None

    def sorted_by_price(self) -> List[Interval]:
        """按当前 Yes 价格升序。"""
        return sorted(self.intervals, key=lambda i: i.price_c)

    def find_hot_interval(self) -> Optional[Interval]:
        """当前价格最高的区间（即过热门区间）。"""
        if not self.intervals:
            return None
        return max(self.intervals, key=lambda i: i.price_c)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)