"""回测数据加载：从 tweets.db 组装「事件×(区间×时间)」价格矩阵 + 结算标签。

- 事件窗口 / 结算数来自 events.event_json（startTime/endDate/tweetCount）。
- 区间价格取 history_pricing（按 bracket_lower/upper），默认 pricer 可配置。
- 发帖速率用 tweets.created_at 全局时间序列。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

DEFAULT_PRICER = "SimpleMonteCarlo"
WEEKLY_PREFIX = "elon-musk-of-tweets%"


def _parse_ts(ts) -> float:
    """ISO8601 → unix 秒。"""
    s = ts.replace("Z", "+00:00")
    if "+" not in s:
        s += "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()


@dataclass
class BacktestEvent:
    slug: str
    start_ts: float
    end_ts: float
    tweet_count: int          # 官方结算数 → 胜出区间
    brackets: List[Tuple[int, int]]     # (lo,hi)
    timestamps: np.ndarray              # (T,)  unix 秒
    probs: np.ndarray                   # (T, n_brackets)  0-1
    live: np.ndarray                    # (T,) bool 该时刻是否有任一活跃概率质量
    winner_bracket_idx: int

    def n_steps(self) -> int:
        return len(self.timestamps)

    def hours_to_end(self, i: int) -> float:
        return max(0.0, (self.end_ts - self.timestamps[i]) / 3600.0)


@dataclass
class BacktestDataset:
    events: List[BacktestEvent]
    tweet_timestamps: np.ndarray  # (M,) unix 秒，发帖时序

    def post_counts(self, t: float, window_h: float) -> int:
        """[t-window, t] 内发帖数。"""
        lo = t - window_h * 3600.0
        return int(np.searchsorted(self.tweet_timestamps, t, "right") -
                   np.searchsorted(self.tweet_timestamps, lo, "left"))


def _load_tweets(db: str) -> np.ndarray:
    con = sqlite3.connect(db)
    rows = con.execute("SELECT created_at FROM tweets ORDER BY created_at").fetchall()
    con.close()
    ts = np.fromiter(
        (datetime.fromisoformat(r.replace("Z", "+00:00"))
         .replace(tzinfo=timezone.utc).timestamp() for r, in rows),
        dtype=np.float64,
    )
    return np.sort(ts)


def _load_event(db: str, slug: str, pricer: str,
                tweets: np.ndarray) -> Optional[BacktestEvent]:
    con = sqlite3.connect(db)
    try:
        rec = con.execute("SELECT event_json FROM events WHERE slug=?", (slug,)).fetchone()
        if not rec:
            return None
        j = json.loads(rec[0])
        tc = j.get("tweetCount")
        st = j.get("startTime") or j.get("startDate")
        en = j.get("endDate")
        if tc in (None, 0) or not (st and en):
            return None
        start_ts = _parse_ts(st)
        end_ts = _parse_ts(en)

        rows = con.execute(
            "SELECT bracket_lower, bracket_upper, timestamp, prob "
            "FROM history_pricing WHERE event_slug=? AND pricer_name=? "
            "ORDER BY timestamp, bracket_lower",
            (slug, pricer),
        ).fetchall()
        if not rows:
            return None

        brackets: List[Tuple[int, int]] = []
        bracket_idx: Dict[Tuple[int, int], int] = {}
        time_map: Dict[float, Dict[int, float]] = {}
        for bl, bu, tick, prob in rows:
            key = (bl, bu)
            if key not in bracket_idx:
                bracket_idx[key] = len(brackets)
                brackets.append(key)
            time_map.setdefault(tick, {})[bracket_idx[key]] = prob
        timestamps = np.array(sorted(time_map.keys()), dtype=np.float64)
        n_b = len(brackets)
        probs = np.zeros((len(timestamps), n_b), dtype=np.float64)
        for jt, t in enumerate(timestamps):
            for bi, pb in time_map[t].items():
                probs[jt, bi] = pb
        # 活跃掩码：原始行是否有任一概率质量（避免全 0 的"市场未开盘"时刻污染状态机）
        raw_sums = probs.sum(axis=1)
        live = raw_sums > 1e-9
        # 归一化防御
        row_sums = probs.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        probs = probs / row_sums

        winner = next(i for i, (lo, hi) in enumerate(brackets) if lo <= tc <= hi)
        return BacktestEvent(
            slug=slug, start_ts=start_ts, end_ts=end_ts, tweet_count=tc,
            brackets=brackets, timestamps=timestamps, probs=probs,
            live=live, winner_bracket_idx=winner,
        )
    finally:
        con.close()


def resample(ev: BacktestEvent, dt_sec: float = 60.0) -> BacktestEvent:
    """把事件价格重采样为固定步长网格（默认 1 分钟），满足盘中逐分钟回放。

    - 原生数据约 10 分钟一档，且有开盘前缺数据；这里按步长前向填充，
      使「每个时间窗口」都有完整 30 档价格矢量。
    - 开盘缺口（早于首个价格时刻）用首个观测分布回填；尾段用末个观测顺延。
    - 逐行归一化到 1（维持零和约束）。
    """
    ts = ev.timestamps
    P = ev.probs                          # (T, n_b)
    n_b = P.shape[1]
    if n_b == 0 or len(ts) == 0:
        return ev
    lo = min(float(ts[0]), ev.start_ts)
    hi = max(float(ts[-1]), ev.end_ts)
    grid = np.arange(lo, hi + dt_sec, dt_sec)
    idx = np.searchsorted(ts, grid, side="right") - 1    # 每个网格点的最近已有观测
    out = np.empty((len(grid), n_b), dtype=np.float64)
    out[:] = P[idx]                                      # 尾段自动夹到末行
    out[idx < 0] = P[0]                                  # 开盘前缺口回填首个分布
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    out = out / row_sum
    return BacktestEvent(
        slug=ev.slug, start_ts=ev.start_ts, end_ts=ev.end_ts,
        tweet_count=ev.tweet_count, brackets=ev.brackets,
        timestamps=grid, probs=out, live=np.ones(len(grid), dtype=bool),
        winner_bracket_idx=ev.winner_bracket_idx,
    )


def load_dataset(db: str, pricer: str = DEFAULT_PRICER,
                 resample_sec: Optional[float] = None) -> BacktestDataset:
    db = str(Path(db).resolve())
    tweets = _load_tweets(db)
    con = sqlite3.connect(db)
    slugs = [r[0] for r in con.execute(
        "SELECT slug FROM events WHERE slug LIKE ? ORDER BY slug", (WEEKLY_PREFIX,))]
    con.close()
    events: List[BacktestEvent] = []
    for slug in slugs:
        ev = _load_event(db, slug, pricer, tweets)
        if ev is not None:
            if resample_sec:
                ev = resample(ev, float(resample_sec))
            events.append(ev)
    return BacktestDataset(events=events, tweet_timestamps=tweets)