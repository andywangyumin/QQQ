"""Black-Scholes 期权定价与 Greeks 计算"""
import numpy as np
from scipy.stats import norm
from dataclasses import dataclass


@dataclass
class OptionGreeks:
    delta: float
    price: float
    iv: float       # 使用的波动率
    dte: int        # 剩余天数


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T < 1e-9 or sigma < 1e-9:
        return 1e9 if S >= K else -1e9
    return (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))


def bs_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return float(norm.cdf(_d1(S, K, T, r, sigma)))


def bs_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T < 1e-9:
        return max(0.0, S - K)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def strike_for_delta(S: float, target_delta: float, T: float,
                     r: float, sigma: float) -> float:
    """给定目标 Delta 反推行权价，四舍五入至最近 $5"""
    if T < 1e-9 or sigma < 1e-9:
        return round(S / 5) * 5
    d1_target = norm.ppf(np.clip(target_delta, 0.01, 0.99))
    K = S * np.exp(-(d1_target * sigma * np.sqrt(T) - (r + 0.5 * sigma ** 2) * T))
    K = max(K, S * 0.30)
    K = min(K, S * 2.00)
    return round(K / 5) * 5


def compute_greeks(S: float, K: float, dte: int,
                   r: float, iv: float) -> OptionGreeks:
    T = max(dte, 0) / 365.0
    return OptionGreeks(
        delta=bs_delta(S, K, T, r, iv),
        price=bs_price(S, K, T, r, iv),
        iv=iv,
        dte=dte,
    )
