"""两腿组合（plan 三，腿3 对冲默认关闭）。

- 腿1 核心：热门区间 No（做空过热），预算 = 100% R。
- 腿2 放大：受害者区间 Yes（做多被错压的反弹），预算 = 30% R；安静期内跌幅>=0.5x 且价>=8¢ 的跌幅最大区间，无合格者跳过。
- 腿3 对冲（预留，默认关）：热门上方 1-2 档便宜 Yes，预算 = 8% R；等拿到优质数据再评估其价值。

预算 = ratio × R（R = 单笔风险美元）。数量 = 预算 / 单价。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..config import AppConfig
from ..data.models import Interval
from ..features.burst import BurstState
from ..features.market_state import MarketSnapshot


@dataclass
class Leg:
    leg: int                     # 1 / 2 / 3
    side: str                    # "YES" | "NO"
    bucket_id: str
    token_id: str
    price_c: float               # 成交参考价（¢）
    budget_usd: float            # 本腿预算（美元）
    quantity: float              # 买入份数
    reason: str = ""

    def as_dict(self):
        return {
            "leg": self.leg, "side": self.side, "bucket": self.bucket_id,
            "token": self.token_id, "price_c": self.price_c,
            "budget_usd": round(self.budget_usd, 2),
            "quantity": round(self.quantity, 4),
            "reason": self.reason,
        }


@dataclass
class LegComposition:
    legs: List[Leg] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.legs) >= 1

    def total_budget(self) -> float:
        return sum(l.budget_usd for l in self.legs)


def _eff_ask(i: Interval) -> float:
    """买入可成交价：用 ask；ask 缺失/为 0 时回退到中间价。"""
    return i.ask_c if i.ask_c and i.ask_c > 0 else i.price_c


def _qty(budget_usd: float, price_c: float) -> float:
    """预算 → 份数，防御 0 价。"""
    price_c = max(price_c, 1e-6)
    return budget_usd / (price_c / 100.0)


class LegSelector:
    """根据市场快照 + BurstState，选出三腿组合并给出预算。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def select(self, snapshot: MarketSnapshot, burst: BurstState, equity: float
               ) -> LegComposition:
        leg_cfg = self.cfg.legs
        r_usd = self.cfg.prob.risk_pct * equity  # 单笔风险 R
        if r_usd <= 0:
            return LegComposition(skipped=["R<=0"])

        comp = LegComposition()
        if snapshot.hot is None:
            comp.skipped.append("no-hot")
            return comp

        hot: Interval = snapshot.hot
        x = burst.overheat_c(hot.price_c)
        if x <= 0:
            comp.skipped.append("x<=0")
            # 即便 x=0 仍允许纯对冲式入场？不，禁止。返回空。
            return comp

        # ---- 腿1：热门 No，预算 R ----
        no_price_c = max(100.0 - _eff_ask(hot), 1.0)
        budget1 = min(r_usd, self.cfg.sizing.max_trade_notional_usd)
        comp.legs.append(Leg(
            leg=1, side="NO", bucket_id=hot.bucket_id,
            token_id=hot.no_clob_token, price_c=no_price_c,
            budget_usd=budget1, quantity=_qty(budget1, no_price_c),
            reason=f"做空过热 x={x:.1f}¢",
        ))

        # ---- 腿2：受害者 Yes，预算 0.6R ----
        victim = self._pick_victim(snapshot, burst, x)
        if victim is not None:
            v_price = _eff_ask(victim)
            budget2 = min(round(leg_cfg.leg2_ratio * r_usd, 2),
                          self.cfg.sizing.max_trade_notional_usd)
            comp.legs.append(Leg(
                leg=2, side="YES", bucket_id=victim.bucket_id,
                token_id=victim.yes_clob_token, price_c=v_price,
                budget_usd=budget2, quantity=_qty(budget2, v_price),
                reason=f"做多被错压反弹 drop={burst.quiet_change_c(victim.bucket_id, victim.price_c):.1f}¢",
            ))
        else:
            comp.skipped.append("no-victim")

        # ---- 腿3：上档便宜 Yes 对冲（默认关闭——单项对冲无意义且烧费用）----
        if leg_cfg.hedge_enabled:
            hedge_legs = self._pick_hedge(snapshot, hot)
            for h in hedge_legs:
                h_price = _eff_ask(h)
                budget3 = min(round(leg_cfg.leg3_ratio * r_usd, 2),
                              self.cfg.sizing.max_trade_notional_usd)
                comp.legs.append(Leg(
                    leg=3, side="YES", bucket_id=h.bucket_id,
                    token_id=h.yes_clob_token, price_c=h_price,
                    budget_usd=budget3, quantity=_qty(budget3, h_price),
                    reason="新爆发尾部对冲",
                ))
            if not hedge_legs:
                comp.skipped.append("no-hedge")
        else:
            comp.skipped.append("hedge-disabled")

        return comp

    def _pick_victim(self, snapshot: MarketSnapshot, burst: BurstState, x: float
                     ) -> Optional[Interval]:
        """安静期内被错压的区间：Δp 最负且跌幅(anchor-current) >= 0.5x。

        价格下限：受害者 Yes 需 >= leg2_floor_c，避免买入近乎无价值的区间
        → 控制份数、抑制费用（预算不再去买几百美分以下的巨大份数）。
        """
        leg_cfg = self.cfg.legs
        candidates = []
        for interval in snapshot.market.intervals:
            if not burst.quiet_start_prices:
                continue
            if interval.bucket_id == snapshot.hot.bucket_id:
                continue
            price = _eff_ask(interval)
            if price < leg_cfg.leg2_floor_c:        # 太便宜的“受害者”不进，防空转
                continue
            change = burst.quiet_change_c(interval.bucket_id, interval.price_c)
            drop = -change                       # 下跌为正
            if drop < leg_cfg.leg2_min_drop * x:  # 跌幅不足 0.5x，不作为受害者
                continue
            candidates.append((drop, interval))
        if not candidates:
            return None
        candidates.sort(key=lambda t: -t[0])      # 取下跌幅度最大的受害者
        return candidates[0][1]

    def _pick_hedge(self, snapshot: MarketSnapshot, hot: Interval) -> List[Interval]:
        """热门上方 1-2 档且 Yes 价 ∈ [leg3_floor_c, 15¢) 的区间（按 low 升序取最多 2 个）。

        用 leg3_floor_c 兜底，避免尾段近乎 0 价的 token 以天量份数入场放大费用。
        """
        leg_cfg = self.cfg.legs
        above = [
            i for i in snapshot.market.intervals
            if i.low is not None and hot.low is not None
            and i.low > hot.low
            and i.price_c >= leg_cfg.leg3_floor_c
            and i.price_c < leg_cfg.leg3_price_max_c
        ]
        above.sort(key=lambda i: i.price_c)
        # 只取上方紧邻的 1-2 档：按 low 升序，取前 leg3_levels 个
        by_low = sorted(above, key=lambda i: i.low)
        return by_low[: int(leg_cfg.hot_levels_above)]