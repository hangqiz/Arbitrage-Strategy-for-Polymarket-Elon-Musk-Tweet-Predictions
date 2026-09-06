"""全局配置。所有参数集中在 dataclass 中，便于月度校准。

对应 plan.md 第 二 节参数表（2.2）。除月度复核外，禁止临时修改。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    try:
        return int(val) if val is not None and val != "" else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ProbabilityParams:
    """plan 2.2 概率模型参数（每月校准一次）。"""

    tau: float = 6.0        # 情绪半衰期 (h)
    sigma_s: float = 3.0    # 安静漂移稳态标准差 (¢)
    delta: float = 4.0      # 判定"赢"的最小回落 (¢)，需 >= 2x 总摩擦成本
    nu: float = 5.0         # Student-t 自由度
    mu_quiet: float = 0.028  # 安静期爆发到达率 (次/h)
    k_shock: float = 1.5    # 余震安全系数
    p_min: float = 0.60     # 入场概率阈值
    risk_pct: float = 0.02  # 单笔风险占总资金比例

    def asdict(self):
        return asdict(self)


@dataclass
class BurstParams:
    """plan 1.2 / 四硬门控 的爆发与安静判定参数。"""

    burst_window_h: float = 6.0      # 爆发检测窗口
    baseline_days: int = 3           # 基线速率回看天数
    burst_mult: float = 2.0          # 爆发判定: 窗口速率 >= 2x 基线
    quiet_mult: float = 1.25         # 安静确认: 当前速率 <= 1.25x 基线
    quiet_min_h: float = 1.0         # 确认安静所需最短时长 (h)


@dataclass
class GateParams:
    """plan 四、四个硬门控。"""

    night_day: int = 3               # 日历门: 市场开始第 N 天之后才考虑
    settle_buffer_h: float = 6.0     # 日历门: 结算前缓冲 (h)
    event_overlap_h: float = 24.0    # 催化剂门: 未来 H 小时内无已知事件
    max_spread_c: float = 1.5        # 流动性门: 热门区间买卖价差上限 (¢)


@dataclass
class LegParams:
    """plan 三、三腿组合结构。

    精力集中在腿1（热门 No＝做空过热）：腿1 预算最高、价格合理（~50¢）；
    腿2/腿3 是放大与对冲，通过价格下限压住低价 token 的天量份数。
    """

    leg2_ratio: float = 0.30     # 腿2 放大仓位比例（下调→份数减少）
    leg2_min_drop: float = 0.5   # 腿2 要求：受害者区间回落 >= 0.5x
    leg2_floor_c: float = 8.0    # 腿2 成交价下限（¢），跳过更便宜的“受害者”
    hedge_enabled: bool = False  # 腿3 对冲默认关闭（低价赌博区间对冲无意义且烧费用）
    hot_levels_above: int = 2    # 腿3 查看热门上方 1-2 档
    leg3_price_max_c: float = 15.0  # 腿3 只选 <15¢ 的上档 Yes
    leg3_ratio: float = 0.08     # 腿3 对冲仓位比例（下调→份数减少）
    leg3_floor_c: float = 4.0    # 腿3 成交价下限，跳过近乎 0 价的 token


@dataclass
class PositionSizing:
    """单笔风险控制。"""

    max_positions: int = 3              # 同市场最大并发持仓数
    max_trade_notional_usd: float = 2000.0  # 单腿名义上限 (美元)
    min_equity_usd: float = 1000.0      # 低于此资金强制停止开新仓


@dataclass
class RoundTripParams:
    """滚动出场/入场：过热带做空 + 快速平台止盈（默认不下止损）。

    - 入场：① 收盘前 no_entry_within_h 小时停止入场（结算前大部分区间价格持续
      下行，不利做空）；② 等 YES 上涨趋势结束（非机械回落）且信号持续
      entry_confirm_m 分钟；③ 回看窗口内出现 ≥spike_jump_c 的单分钟跳变视为
      异常尖峰（场内机器人用大额单制造）→ 跳过；④ 腿1 是"热门 + 其上方区间"
      的一篮子做空，分散单区间"最终获胜"的尾部风险。
    - 止盈：NO 相对入场已上涨且价格进入短平台/停滞（快速检测，抢在二次飙升前）。
    - 止损：默认关闭（本策略反转前急杀，设止损会伤在坑底）；开启后为确认式。
    """

    enabled: bool = True
    # ---- 入场 ①：收盘前停止入场 ----
    no_entry_within_h: float = 12.0  # 结算前 12h 内不再开新仓
    # ---- 入场 ④：腿1 做空"热门 + 其上几档"一篮子 ----
    leg1_top_k: int = 4              # 热门 + 其上 3 档
    leg1_weight_by_price: bool = True  # True: 预算∝区间价格(≈量/流动性)，量大分更多
    # ---- 入场 ②/③：统一进场信号 ----
    # 兼容两种风格：热门"慢趋势"(量大多小时爬/降) 与 冷门"剧烈飙升"(量小快涨)。
    # 规则：区间在窗口内涨幅≥min_rise_c 且已见顶回落(从峰值回roll_confirm 或斜率≤0)，
    # → 上涨终结即进场。慢爬/快涨都被识别；瞬态尖峰因回落而 amp≈0 会自动被滤掉。
    froth_win_m: int = 240       # 回看窗口（分钟，够装下热门的多小时爬升）
    min_rise_c: float = 2.0      # 窗口内涨幅须 ≥2¢（放宽，避免漏掉小步爬升的窗口）
    roll_confirm_c: float = 1.0  # 从峰值回落 ≥1¢ 或当前斜率≤0 视为上涨终结（放宽）
    entry_confirm_m: int = 3     # 信号需连续成立 3 分钟（适度防闪烁，不再过于苛刻）
    # ---- 异常尖峰：仅用于持仓中不平仓（避免按人造报价成交）----
    spike_jump_c: float = 8.0    # 单分钟 |跳变| ≥8¢ 视为异常尖峰
    spike_win_m: int = 20        # 回看窗口（分钟）
    # ---- 止盈：快速平台（抢在二次飙升前）----
    rise_min_c: float = 3.0      # 篮子加权 NO 相对入场至少涨 3¢ 才算情绪回落
    fast_stall_m: int = 15       # 快速停滞窗口（分钟）
    fast_stall_range_c: float = 1.5  # 窗口振幅 ≤1.5¢ 视为停滞
    fast_slope_c: float = 0.02   # 窗口斜率 |≤|0.02¢/分 视为停
    plateau_win_m: int = 30      # 平台检测窗口（分钟）
    plateau_range_c: float = 3.0 # 窗口内振幅 ≤3¢ 视作平台
    plateau_drift_c: float = 2.0 # 窗口内漂移 ≤2¢ 视作平
    # 止损：默认关闭（本策略反转前急杀，止损伤在坑底）。开启后按确认式触发。
    sl_enabled: bool = False
    sl_enter_c: float = 15.0     # NO 跌破 入场-15¢ 触发止损确认
    sl_recover_c: float = 8.0    # 确认期若回升超 触发点+8¢ 解除（非真跌）
    sl_dwell_m: int = 60         # 持续低沉满 60 分钟未收回 → 确认真·持续走高
    max_roundtrips: int = 4      # 每事件最多开仓次数


@dataclass
class AppConfig:
    """全局配置文件。"""

    env: str = "production"  # production | backtest | test

    # 市场
    market_slug: str = os.getenv("MARKET_SLUG", "elon-musk-weekly-tweets")

    # 数据源
    posting_source: str = os.getenv("POSTING_DATA_SOURCE", "mock")
    posting_data_path: str = os.getenv("POSTING_DATA_PATH", "./data/posts.csv")
    events_csv_path: str = os.getenv("EVENTS_CSV_PATH", "./data/events.csv")

    # API
    clob_api_url: str = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
    private_key: Optional[str] = os.getenv("POLYMARKET_PRIVATE_KEY") or None
    wallet_address: Optional[str] = os.getenv("POLYMARKET_WALLET_ADDRESS") or None

    # 运行
    dry_run: bool = _env_bool("DRY_RUN", True)
    poll_interval_sec: int = _env_int("POLL_INTERVAL_SEC", 60)
    timezone: str = "UTC"

    # 告警
    telegram_bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN") or None
    telegram_chat_id: Optional[str] = os.getenv("TELEGRAM_CHAT_ID") or None

    # 数据存储
    sqlite_path: str = os.getenv("SQLITE_PATH", "./data/state.db")

    # 子配置
    prob: ProbabilityParams = field(default_factory=ProbabilityParams)
    burst: BurstParams = field(default_factory=BurstParams)
    gate: GateParams = field(default_factory=GateParams)
    legs: LegParams = field(default_factory=LegParams)
    sizing: PositionSizing = field(default_factory=PositionSizing)
    roundtrip: RoundTripParams = field(default_factory=RoundTripParams)

    def __post_init__(self):
        # 确保关键数值合理
        assert self.prob.delta >= 2.0, "δ 需 >= 2x 总摩擦成本才值得入场"
        assert 0.0 < self.prob.p_min < 1.0
        assert 0.0 < self.prob.risk_pct <= 0.05