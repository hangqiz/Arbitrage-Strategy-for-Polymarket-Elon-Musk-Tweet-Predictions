"""市场状态：刷新各区间报价、定位热门区间、校验零和约束。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..data.clob_client import MidpriceClient
from ..data.models import Market


@dataclass
class MarketSnapshot:
    market: Market
    price_sum_c: float        # 各区问 Yes 价格之和（应≈100¢）
    hot: Optional[object]     # Interval
    hot_price_c: float
    hot_ask_c: float
    hot_bid_c: float
    errors: List[str]

    @property
    def hot_spread_c(self) -> Optional[float]:
        if self.hot is None:
            return None
        return self.hot_ask_c - self.hot_bid_c


def build_snapshot(market: Market, quotes: MidpriceClient) -> MarketSnapshot:
    """批量读取所有区间 Yes token 报价并刷新到 Market 对象。"""
    errors: List[str] = []
    total = 0.0
    for interval in market.intervals:
        try:
            q = quotes.quote(interval.yes_clob_token)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{interval.bucket_id}: {e}")
            continue
        interval.price_c = q.mid_c
        interval.ask_c = q.ask_c
        interval.bid_c = q.bid_c
        total += q.mid_c

    hot = market.find_hot_interval()
    return MarketSnapshot(
        market=market,
        price_sum_c=round(total, 2),
        hot=hot,
        hot_price_c=hot.price_c if hot else 0.0,
        hot_ask_c=hot.ask_c if hot else 0.0,
        hot_bid_c=hot.bid_c if hot else 0.0,
        errors=errors,
    )