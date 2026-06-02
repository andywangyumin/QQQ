"""
数据拉取模块
- QQQ 收盘价、日涨跌幅、历史波动率（yfinance）
- 期权链隐含波动率 → 用于 Black-Scholes 计算 Delta（yfinance）
"""
import logging
import warnings
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
log = logging.getLogger(__name__)


# ── QQQ 行情 ──────────────────────────────────────────────

def fetch_qqq_quote() -> dict:
    """
    返回 QQQ 最近一个交易日的收盘数据：
      close, prev_close, change_pct, hv20（20日历史波动率）
    """
    raw = yf.download("QQQ", period="30d", auto_adjust=True, progress=False)
    if raw.empty:
        raise RuntimeError("yfinance 下载 QQQ 数据失败，请检查网络")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)

    close = raw["Close"].dropna()
    log_ret = np.log(close / close.shift(1)).dropna()
    hv20 = float(log_ret.tail(20).std() * np.sqrt(252))
    hv20 = float(np.clip(hv20, 0.10, 0.80))

    latest_close = float(close.iloc[-1])
    prev_close   = float(close.iloc[-2]) if len(close) >= 2 else latest_close
    change_pct   = (latest_close - prev_close) / prev_close

    last_date = close.index[-1]
    if isinstance(last_date, pd.Timestamp):
        last_date = last_date.date()

    return {
        "date":       last_date,
        "close":      latest_close,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "hv20":       hv20,
    }


# ── 期权 IV ────────────────────────────────────────────────

def _find_closest_expiry(available, target: date) -> Optional[str]:
    """从 yfinance 提供的到期日列表中找最近的一个"""
    if not available:
        return None
    closest = min(
        available,
        key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d").date() - target).days)
    )
    gap = abs((datetime.strptime(closest, "%Y-%m-%d").date() - target).days)
    if gap > 60:
        log.warning(f"最近可用到期日 {closest} 距目标 {target} 相差 {gap} 天，IV 参考价值有限")
    return closest


def fetch_option_iv(strike: float, expiry_str: str,
                    fallback_hv: float = 0.20,
                    iv_override: Optional[float] = None,
                    position_id: Optional[str] = None) -> float:
    """
    获取指定合约的隐含波动率。
    优先级：iv_override（手动填入）> DB 缓存（iv_refresh 收盘后写入）> yfinance > HV 回退。
    """
    # 1. 用户手动填入的 IV 优先（最准确）
    if iv_override is not None and 0.05 < iv_override < 2.0:
        log.info(f"使用手动 IV：{iv_override:.2%}（strike={strike}, expiry={expiry_str}）")
        return float(iv_override)

    # 2. 读取 iv_refresh 写入的收盘后缓存（28小时内有效）
    if position_id:
        try:
            import state_store as ss
            cached = ss.get_iv_cache(position_id)
            if cached is not None and 0.05 < cached < 2.0:
                log.info(f"使用 DB 缓存 IV：{cached:.2%}（{position_id}）")
                return cached
        except Exception as e:
            log.warning(f"读取 IV 缓存失败（{e}），继续尝试 yfinance")

    # 3. 尝试从 yfinance 期权链获取（仅在市场交易时段有效）
    target = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    try:
        ticker = yf.Ticker("QQQ")
        available = list(ticker.options)
        if not available:
            log.warning("yfinance 未返回 QQQ 期权到期日列表，使用 HV 回退")
            return fallback_hv

        closest_exp = _find_closest_expiry(available, target)
        chain = ticker.option_chain(closest_exp)
        calls = chain.calls[["strike", "impliedVolatility"]].dropna()

        if calls.empty:
            return fallback_hv

        sorted_calls = calls.iloc[(calls["strike"] - strike).abs().argsort()]
        iv_sample = sorted_calls["impliedVolatility"].head(3).values
        iv_raw = float(np.median(iv_sample))

        if iv_raw <= 0.01 or iv_raw > 2.0:
            log.warning(f"yfinance IV={iv_raw:.3f} 异常，使用 HV 回退（建议在 positions.yaml 填写 iv_override）")
            return fallback_hv

        iv_final = float(np.clip(iv_raw, 0.10, 0.80))
        log.info(f"yfinance IV：{iv_final:.2%}（strike={strike}, expiry={expiry_str}）")
        return iv_final

    except Exception as e:
        log.warning(f"获取期权 IV 失败（{e}），使用 HV 回退")
        return fallback_hv


def fetch_option_mid(strike: float, expiry_str: str) -> Optional[float]:
    """
    获取指定合约的市场中间价（(bid+ask)/2），用于估算操作成本。
    返回 None 表示获取失败。
    """
    target = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    try:
        ticker = yf.Ticker("QQQ")
        available = list(ticker.options)
        closest_exp = _find_closest_expiry(available, target)
        chain = ticker.option_chain(closest_exp)
        calls = chain.calls[["strike", "bid", "ask"]].dropna()

        if calls.empty:
            return None

        idx = (calls["strike"] - strike).abs().idxmin()
        bid = float(calls.loc[idx, "bid"])
        ask = float(calls.loc[idx, "ask"])
        if bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / 2.0

    except Exception as e:
        log.warning(f"获取期权市价失败（{e}）")
        return None


def is_market_open_today(quote_date: date) -> bool:
    """判断 quote_date 是否是有效交易日（yfinance 返回了数据则视为交易日）"""
    today = date.today()
    # 如果 yfinance 返回的最新数据日期是昨天或今天，视为正常
    return (today - quote_date).days <= 3
