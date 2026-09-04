"""执行层：下单。dry-run 模式下只记录；真实模式接入 py-clob-client。

CLOB 下单需持币、签名、做市；生产必须配置 POLYMARKET_PRIVATE_KEY。
本模块在 private_key 缺失时自动退回 DryRunOrderPlacer。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

from ..config import AppConfig
from ..strategy.legs import LegComposition
from ..utils import get_logger

log = get_logger(__name__)


@dataclass
class OrderResult:
    ok: bool
    message: str
    order_ids: List[str]

    def __bool__(self):
        return self.ok


class OrderPlacer(ABC):
    @abstractmethod
    def place_legs(self, market_slug: str, legs: LegComposition) -> OrderResult:
        """提交一个信号的三腿订单。"""


class DryRunOrderPlacer(OrderPlacer):
    def place_legs(self, market_slug: str, legs: LegComposition) -> OrderResult:
        log.info("[DRY RUN] 市场 %s 提交 %d 腿：", market_slug, len(legs.legs))
        for leg in legs.legs:
            log.info(
                "  腿%d %s %s  token=%s price=%.1fc qty=%.2f budget=%.2f$ %s",
                leg.leg, leg.side, leg.bucket_id, leg.token_id,
                leg.price_c, leg.quantity, leg.budget_usd, leg.reason,
            )
        return OrderResult(ok=True, message="dry-run", order_ids=[])


class HttpOrderPlacer(OrderPlacer):
    """真实下单：惰性导入 py-clob-client，失败则抛错（不静默）。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._client = None

    def place_legs(self, _market_slug: str, legs: LegComposition) -> OrderResult:
        if self._client is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import OrderArgs
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "真实下单需要 py-clob-client 与 POLYMARKET_PRIVATE_KEY，当前不可用"
                ) from e
            if not self.cfg.private_key or not self.cfg.wallet_address:
                raise RuntimeError("缺少 POLYMARKET_PRIVATE_KEY / WALLET_ADDRESS")
            self._client = ClobClient(
                self.cfg.clob_api_url, key=self.cfg.private_key, chain_id=137
            )

        order_ids = []
        for leg in legs.legs:
            price = leg.price_c / 100.0
            size = leg.quantity
            order_ids.append(str(
                self._client.create_and_post_order(
                    OrderArgs(asset_id=leg.token_id, price=price, size=size, side="BUY")
                )
            ))
            log.info("订单 %s/%s 已提交", leg.bucket_id, leg.side)
        return OrderResult(ok=True, message="ords-submitted", order_ids=order_ids)


def make_order_placer(cfg: AppConfig) -> OrderPlacer:
    if cfg.dry_run or not cfg.private_key:
        return DryRunOrderPlacer()
    return HttpOrderPlacer(cfg)