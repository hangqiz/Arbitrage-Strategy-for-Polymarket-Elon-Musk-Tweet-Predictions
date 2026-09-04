"""回落概率建模（plan 2.1 最终公式）。

    P_success(x, H) = exp(-k·μ_quiet·H)          # 安静存活
                      × F_{t,ν}( (x(1-2^{-H/τ}) - δ) / (σ_s sqrt(1-4^{-H/τ})) )  # 安静前提下回落≥δ

- 第一项把「持有期内再次爆发」从隐性假设变成显式惩罚（×P_survive）。
- 第二项用 Student-t 替代正态，承认肥尾，提高入场门槛。

注意：所有价格 / 参数以「美分」(¢) 为单位（如 x=8 表示 8¢、δ=4 表示 4¢）。
"""
from __future__ import annotations

import math

try:
    from scipy.stats import t as student_t
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - 回退路径
    _HAVE_SCIPY = False
    student_t = None


def set_mu_quiet(params, mu_quiet: float, k_shock: float) -> None:
    """外部导入时注入校准后的 μ_quiet（仅当样本>=20 时覆盖固定值，否则上浮 30%）。"""
    params.mu_quiet = mu_quiet
    params.k_shock = k_shock


def p_survive(H: float, mu_quiet: float, k_shock: float) -> float:
    """持有 H 小时内不再爆发的概率（含余震缓冲）。"""
    return math.exp(-k_shock * mu_quiet * H)


def p_revert_student(
    x: float, H: float, tau: float, sigma_s: float, delta: float, nu: float
) -> float:
    """安静前提下，H 小时内价格回落 >= δ 的概率（Student-t 替代正态）。

    x: 过热度 (¢)。H: 持有时长/剩余结算时间 (h)。
    """
    drift = x * (1.0 - 2.0 ** (-H / tau))
    scale = sigma_s * math.sqrt(max(1e-9, 1.0 - 4.0 ** (-H / tau)))
    z = (drift - delta) / scale
    if _HAVE_SCIPY:
        return float(student_t.cdf(z, df=nu))
    # 回退：nu 较大时 t 逼近正态，用 erf 近似（生产环境应安装 scipy）
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_success(x: float, H: float, params) -> float:
    """最终入场概率 = 安静存活 × 安静前提下的回落概率。

    params: config.ProbabilityParams
    """
    surv = p_survive(H, params.mu_quiet, params.k_shock)
    revt = p_revert_student(
        x, H, params.tau, params.sigma_s, params.delta, params.nu
    )
    return surv * revt