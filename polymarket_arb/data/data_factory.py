"""生产环境装配：加载真实市场、报价客户端、事件日历。"""
from __future__ import annotations

from ..config import AppConfig
from ..data.clob_client import HttpClobClient, HttpMarketLoader
from ..data.posting_rate import make_rate_provider
from ..utils import get_logger

log = get_logger(__name__)


def build_production_env(cfg: AppConfig):
    """返回 (market, midprice/报价客户端, rates, calendar)。"""
    from ..data.events import make_calendar

    loader = HttpMarketLoader()
    market = loader.load_market(cfg.market_slug)
    midprice = HttpClobClient(cfg)
    rates = make_rate_provider(cfg)
    calendar = make_calendar(cfg.events_csv_path)
    log.info("已加载市场 %s，区间数=%d", market.slug, len(market.intervals))
    return market, midprice, rates, calendar