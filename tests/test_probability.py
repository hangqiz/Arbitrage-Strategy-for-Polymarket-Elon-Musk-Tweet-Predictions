import math

from polymarket_arb.config import ProbabilityParams
from polymarket_arb.probability import (
    p_survive,
    p_revert_student,
    p_success,
)


def test_p_survive_decreasing_in_h():
    params = ProbabilityParams()
    s_short = p_survive(1.0, params.mu_quiet, params.k_shock)
    s_long = p_survive(48.0, params.mu_quiet, params.k_shock)
    assert s_short > s_long
    assert 0.0 < s_short <= 1.0
    # μ=0.028, k=1.5, H=48 → exp(-2.016)≈0.133
    assert abs(s_long - math.exp(-params.k_shock * params.mu_quiet * 48.0)) < 1e-9


def test_p_revert_increases_with_x():
    params = ProbabilityParams()
    p0 = p_revert_student(3.0, 24.0, params.tau, params.sigma_s, params.delta, params.nu)
    p1 = p_revert_student(9.0, 24.0, params.tau, params.sigma_s, params.delta, params.nu)
    assert p1 > p0
    assert 0.0 <= p0 <= 1.0


def test_p_revert_increases_with_h():
    params = ProbabilityParams()
    p0 = p_revert_student(6.0, 6.0, params.tau, params.sigma_s, params.delta, params.nu)
    p1 = p_revert_student(6.0, 48.0, params.tau, params.sigma_s, params.delta, params.nu)
    assert p1 >= p0


def test_p_success_bounded_and_monotone():
    params = ProbabilityParams()
    assert 0.0 <= p_success(8.0, 24.0, params) <= 1.0
    # 更大的 x ⇒ 更高成功率；更久持有 ⇒ 存活惩罚使曲线可能在长端回落
    assert p_success(12.0, 24.0, params) > p_success(5.0, 24.0, params)


def test_survive_penalizes_long_holding():
    """两项优化之一：×P_survive 会把持有期很久的信号系统性压低。"""
    params = ProbabilityParams()
    revert_long = p_revert_student(8.0, 300.0, params.tau, params.sigma_s, params.delta, params.nu)
    combined = p_success(8.0, 300.0, params)
    assert combined <= revert_long
    assert combined < 0.4  # 高久期 + 存活惩罚 ⇒ 基本不会过入场线


def test_student_fat_tail_lower_than_normal():
    """肥尾承认参数误差：同等 x，t-分布给出更低（更保守）概率。"""
    from math import erf

    def norm_cdf(z):
        return 0.5 * (1.0 + erf(z / math.sqrt(2.0)))

    x, H = 6.0, 24.0
    drift = x * (1.0 - 2.0 ** (-H / 6.0))
    scale = 3.0 * math.sqrt(1.0 - 4.0 ** (-H / 6.0))
    z = (drift - 4.0) / scale
    t_prob = p_revert_student(x, H, 6.0, 3.0, 4.0, 5.0)
    n_prob = norm_cdf(z)
    assert t_prob < n_prob