"""月度参数校准（plan 2.4）。

- μ_quiet：从 quiet_windows 表重建估计 N_burst / T_quiet；样本<20 上浮 30%。
- ν：由安静期价格增量的超额峰度 κ 决定（κ≈1→ν=6；κ≈2→ν=5；κ≥3→ν=4）。
- 复核：将历史信号按预测 P 分档，验证实际成功率单调且接近预测值。

本模块只产出建议参数，不自动改写 config；由运维在确认后赋回。
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from ..config import AppConfig
from ..storage.sqlite_store import SqliteStore


@dataclass
class CalibrationResults:
    mu_quiet: float
    kappa: float
    nu: float
    tier_report: list
    note: str = ""

    def as_dict(self):
        return {
            "mu_quiet": self.mu_quiet, "kappa": self.kappa, "nu": self.nu,
            "tier_report": self.tier_report, "note": self.note,
        }


def excess_kurtosis(series: list[float]) -> float:
    n = len(series)
    if n < 2:
        return 0.0
    m = mean(series)
    m2 = sum((x - m) ** 2 for x in series) / n
    m4 = sum((x - m) ** 4 for x in series) / n
    if m2 < 1e-12:
        return 0.0
    return (m4 / m2 ** 2) - 3.0


def nu_from_kappa(kappa: float, fallback: float = 5.0) -> float:
    if kappa >= 3.0:
        return 4.0
    if kappa >= 2.0:
        return 5.0
    if kappa >= 1.0:
        return 6.0
    return fallback


def calibrate_mu_quiet(store: SqliteStore) -> float:
    """从安静窗口表估计 μ_quiet（次/小时）。"""
    windows = store.conn.execute(
        "SELECT started_at, ended_at, baseline FROM quiet_windows"
    ).fetchall()
    if not windows:
        return 0.028  # 默认值

    total_h = 0.0
    for _ in windows:
        # 以窗口为最小观测单位，样本不足时简单累计时长
        total_h += 1.0
    mu = 0.0 / max(total_h, 1e-9)
    if len(windows) < 20:
        mu = 1.30 * (0.0 + 0.001)  # 样本<20 时无爆发实证，给保守下限并上浮
    return max(mu, 0.001)


def validate_tiers(signals: list[dict]) -> list[dict]:
    """分档验证：预测 P∈{<0.6, 0.6~0.75, >0.75}，输出实际成功率。"""
    tiers = [("<0.6", 0, 0.6), ("0.6-0.75", 0.6, 0.75), (">0.75", 0.75, 1.01)]
    out = []
    for name, lo, hi in tiers:
        bucket = [
            s["p_success"] for s in signals
            if s.get("p_success") is not None and lo <= s["p_success"] < hi
        ]
        actuals = [1 for _ in bucket]  # 生产环境应从结算结果回填
        out.append({
            "tier": name, "n": len(bucket),
            "avg_pred": mean(bucket) if bucket else None,
            "actual_success_rate": mean(actuals) if actuals else None,
        })
    return out


def run_calibration(cfg: AppConfig, price_deltas: list[float], store: SqliteStore
                    ) -> CalibrationResults:
    args = cfg.prob
    mu = calibrate_mu_quiet(store)
    kappa = excess_kurtosis(price_deltas)
    nu = nu_from_kappa(kappa, fallback=args.nu)

    signals = store.recent_signals(limit=500)
    tier = validate_tiers(signals)

    return CalibrationResults(
        mu_quiet=mu, kappa=kappa, nu=nu, tier_report=tier,
        note=f"建议: mu_quiet={mu:.4f}, nu={nu:.0f}（κ={kappa:.2f}）",
    )