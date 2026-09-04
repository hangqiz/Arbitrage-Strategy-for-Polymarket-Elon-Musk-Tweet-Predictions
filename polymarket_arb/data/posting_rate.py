"""发帖速率数据源：估计 λ_b（基线）与当前速率，判定爆发/安静。

数据来源可配置：
- mock  : 确定性合成数据（测试 / 回测）
- csv   : 提供「发帖时间戳」的 CSV（列: ts）
真实生产可替换为自行采集的 X/Mastodon 发帖时序。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..config import AppConfig


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PostingRateProvider(ABC):
    """提供截止到某个时刻的发帖时间戳序列。"""

    @abstractmethod
    def post_timestamps(self, until: Optional[datetime] = None) -> List[datetime]:
        """返回 until（默认 now）之前的所有发帖时间戳（UTC）。"""

    def rate_per_hour(
        self, window_h: float, until: Optional[datetime] = None
    ) -> float:
        """计算 [until-window_h, until] 内的平均发帖速率（次/小时）。"""
        until = _to_utc(until) if until else datetime.now(timezone.utc)
        low = until - timedelta(hours=window_h)
        n = sum(1 for t in self.post_timestamps(until) if _to_utc(t) >= low)
        return n / window_h

    def baseline_rate(self, days: int, until: Optional[datetime] = None) -> float:
        """前 days 天的日均速率 λ_b（次/小时）。"""
        until = _to_utc(until) if until else datetime.now(timezone.utc)
        n = len(self.post_timestamps(until))
        # 回看窗口仅取最近 days 天（用时间过滤，避免算全部历史）
        low = until - timedelta(days=days)
        n = sum(1 for t in self.post_timestamps(until) if _to_utc(t) >= low)
        return n / (days * 24.0)


class MockPostingRateProvider(PostingRateProvider):
    """确定性合成数据：默认安静速率均匀分布 + 可注入爆发时段。

    用于离线回测，不访问任何外部接口。
    """

    def __init__(self, base_per_h: float = 2.0, seed: int = 42):
        import random

        self._rng = random.Random(seed)
        self.base_per_h = base_per_h
        # 用泊松近似生成每一天的时间戳
        self._cache: dict = {}

    def post_timestamps(self, until: Optional[datetime] = None) -> List[datetime]:
        until = _to_utc(until) if until else datetime.now(timezone.utc)
        out: List[datetime] = []
        # 覆盖最近 5 天
        for d in range(5, 0, -1):
            day_start = (until - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
            for _ in range(round(self.base_per_h * 24)):
                delta_s = self._rng.uniform(0, 24 * 3600)
                ts = day_start + timedelta(seconds=delta_s)
                if ts <= until:
                    out.append(ts)
        return sorted(out)


class CsvPostingRateProvider(PostingRateProvider):
    """从 CSV 读发帖时间戳。列：ts（ISO8601 UTC）。"""

    def __init__(self, path: str):
        self.path = path
        self._ts: Optional[List[datetime]] = None

    def post_timestamps(self, until: Optional[datetime] = None) -> List[datetime]:
        if self._ts is None:
            self._ts = []
            with open(self.path) as f:
                next(f, None)  # header
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    ts = datetime.fromisoformat(line.split(",")[0].replace("Z", "+00:00"))
                    self._ts.append(_to_utc(ts))
            self._ts.sort()
        until = _to_utc(until) if until else datetime.now(timezone.utc)
        return [t for t in self._ts if t <= until]


def make_rate_provider(cfg: AppConfig) -> PostingRateProvider:
    source = cfg.posting_source
    if source == "mock":
        return MockPostingRateProvider()
    if source == "csv":
        return CsvPostingRateProvider(cfg.posting_data_path)
    raise ValueError(f"未知发帖数据源: {source}")