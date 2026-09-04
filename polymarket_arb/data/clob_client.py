"""公开数据：CLOB 价格、Gamma 市场解析。仅读取，不含下单。

下单逻辑在 execution/orders.py；这里只负责拉取中间价/盘口。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import requests

from ..config import AppConfig


@dataclass
class Quote:
    token_id: str
    bid_c: float      # 买一价 (¢)
    ask_c: float      # 卖一价 (¢)
    mid_c: float      # 中间价 (¢)

    @property
    def spread_c(self) -> float:
        return self.ask_c - self.bid_c


class MidpriceClient(ABC):
    @abstractmethod
    def quote(self, token_id: str) -> Quote:
        """获取某 token 的盘口报价。"""


class HttpClobClient(MidpriceClient):
    """真实 CLOB 报价（GET /midpoint 与 /book）。"""

    def __init__(self, cfg: AppConfig, timeout: float = 10.0):
        self.base = cfg.clob_api_url.rstrip("/")
        self.sess = requests.Session()
        self.timeout = timeout

    def quote(self, token_id: str) -> Quote:
        mid = self.sess.get(
            f"{self.base}/midpoint", params={"token_id": token_id}, timeout=self.timeout
        ).json()
        book = self.sess.get(
            f"{self.base}/book", params={"token_id": token_id}, timeout=self.timeout
        ).json()
        best_bid = book.get("bids", [{}])[0].get("price", 0.0)
        best_ask = book.get("asks", [{}])[0].get("price", 0.0)
        mid_price = mid.get("prices", [None])[0]
        mid_c = float(mid_price) * 100 if mid_price is not None else (best_ask + best_bid) / 2 * 100
        return Quote(
            token_id=token_id,
            bid_c=round(float(best_bid) * 100, 2),
            ask_c=round(float(best_ask) * 100, 2),
            mid_c=round(mid_c, 2),
        )


class MockMidpriceClient(MidpriceClient):
    """离线盘口，供测试/回测。token_id 形如 yes-<bucket>:<price_c>。"""

    def __init__(self, price_map: Optional[dict] = None):
        self.price_map = price_map or {}

    def quote(self, token_id: str) -> Quote:
        if token_id in self.price_map:
            mid = self.price_map[token_id]
            return Quote(token_id, round(mid - 0.5, 2), round(mid + 0.5, 2), round(mid, 2))
        return Quote(token_id, 0.0, 0.0, 0.0)


class GammaClient(ABC):
    """市场解析：给定 slug 得到区间 / token / 结算时间。"""

    @abstractmethod
    def load_market(self, slug: str):
        """返回 data.models.Market；失败抛出异常。"""


class HttpMarketLoader(GammaClient):
    def __init__(self, base: str = "https://gamma-api.polymarket.com"):
        self.base = base.rstrip("/")
        self.sess = requests.Session()

    def load_market(self, slug: str):
        from .models import Market, Interval

        # 示例：真实 Gamma 返回需要按 bulk market 拆分区间，这里保留占位实现
        url = f"{self.base}/markets?slug={slug}"
        data = self.sess.get(url).json()
        m = data[0]
        intervals = []
        for i, outcome in enumerate(m.get("clobTokenIds", "").split(",")):
            yes_id, no_id = (outcome, "") if i % 2 == 0 else ("", outcome)
            intervals.append(
                Interval(
                    bucket_id=f"b{i // 2}",
                    low=None,
                    high=None,
                    yes_clob_token=yes_id,
                    no_clob_token=no_id,
                )
            )
        return Market(slug=slug, intervals=intervals)