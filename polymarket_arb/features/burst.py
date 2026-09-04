"""爆发 / 安静 / 过热度 x 检测（plan 1.2）。

定义：
- 爆发点 t_b：某 6h 窗口速率 >= 2 × λ_b。
- 安静确认：爆发后当前速率回落至 <= 1.25 × λ_b。
- 过热度 x = 热门区间当前价 − 安静开始时的价格，只累计安静窗口内涨幅。

爆发本身是信息驱动，不计入 x；安静期内「发帖已停、价格仍涨」才是情绪。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from ..config import AppConfig
from ..utils import get_logger

log = get_logger(__name__)


def is_burst(rate: float, baseline: float, burst_mult: float) -> bool:
    """当前 6h 窗口速率是否达到爆发阈值。"""
    return rate >= burst_mult * max(baseline, 1e-9)


def is_quiet(rate: float, baseline: float, quiet_mult: float) -> bool:
    """速率是否已回到安静阈值以下。"""
    return rate <= quiet_mult * max(baseline, 1e-9)


def evaluate_burst(
    rate: float, baseline: float, cfg: AppConfig
) -> tuple[bool, bool]:
    """返回 (是否爆发中, 是否已进入安静)。"""
    bursting = is_burst(rate, baseline, cfg.burst.burst_mult)
    quiet = is_quiet(rate, baseline, cfg.burst.quiet_mult)
    return bursting, quiet


@dataclass
class BurstState:
    """追踪一个市场的爆发 / 安静 / 过热度状态。缓存跨轮询。"""

    baseline: float = 0.0
    in_burst: bool = False
    quiet_start_price_c: Optional[float] = None  # 安静开始时热门区间 Yes 价
    quiet_start_prices: Dict[str, float] = None  # 安静开始时所有区间价格
    hot_bucket: Optional[str] = None

    def __post_init__(self):
        if self.quiet_start_prices is None:
            self.quiet_start_prices = {}

    def record_burst(self, baseline: float) -> None:
        self.baseline = baseline
        self.in_burst = True
        self.quiet_start_price_c = None
        self.quiet_start_prices = {}

    def record_quiet_start(self, prices: Dict[str, float], hot_bucket: str) -> None:
        """锚定所有区间在安静开始时的价格。"""
        self.in_burst = False
        self.quiet_start_prices = dict(prices)
        self.quiet_start_price_c = prices.get(hot_bucket)
        self.hot_bucket = hot_bucket

    def overheat_c(self, current_price_c: float) -> float:
        """安静期累计涨幅 = 当前价 − 安静开始价（仅当进入安静后才有意义）。"""
        if self.quiet_start_price_c is None:
            return 0.0
        return max(0.0, current_price_c - self.quiet_start_price_c)

    def quiet_change_c(self, bucket_id: str, current_price_c: float) -> float:
        """某区间自安静开始以来的价格变化（¢），用于识别受害者区间。"""
        anchor = self.quiet_start_prices.get(bucket_id)
        if anchor is None:
            return 0.0
        return current_price_c - anchor


class BurstTracker:
    """维护所有市场的 BurstState，供特征层与门控复用。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._states: Dict[str, BurstState] = {}

    def state_for(self, market_slug: str) -> BurstState:
        return self._states.setdefault(market_slug, BurstState())

    def update(self, market_slug: str, rate: float, baseline: float,
               prices: Dict[str, float], hot_bucket: str,
               quiet: bool, bursting: bool) -> BurstState:
        """每次轮询调用，更新状态机。prices 为所有区间当前价。返回该市场最新 BurstState。"""
        st = self.state_for(market_slug)
        # 若尚未进入过爆发或突发变化，更新基线
        if st.baseline <= 0 or abs(st.baseline - baseline) / max(st.baseline, 1e-9) > 0.3:
            st.baseline = baseline

        if bursting:
            if not st.in_burst:
                log.info("[%s] 检测到爆发：rate=%.2f baseline=%.2f", market_slug, rate, baseline)
                st.record_burst(baseline)
        else:
            if st.in_burst and quiet:
                # 从爆发转入安静，锚定安静开始价
                hot_price = prices.get(hot_bucket, 0.0)
                log.info("[%s] 爆发结束并确认安静，热门[%s]锚定价=%.2f¢",
                         market_slug, hot_bucket, hot_price)
                st.record_quiet_start(prices, hot_bucket)
            elif not st.in_burst and st.quiet_start_price_c is not None and not quiet:
                # 安静确认后又在安静门阈值之上 -> 视为再次进入爆发
                st.record_burst(baseline)
        return st