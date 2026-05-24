#!/usr/bin/env python3
"""
一次性历史数据回填脚本
- 从 yfinance 下载 QQQ 5年+历史 OHLCV
- 计算 HV20（20日已实现波动率）
- 写入 market_history.db

用法：python backfill_history.py
每次运行均为幂等操作（INSERT OR REPLACE），可安全重复执行。
"""
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
import yfinance as yf

from history_store import upsert_df, row_count, date_range, DEFAULT_DB

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)


def fetch_and_prepare(start: str = "2021-01-01") -> pd.DataFrame:
    """下载 QQQ 历史数据，计算 HV20，返回 DataFrame"""
    log.info(f"从 yfinance 下载 QQQ 历史数据（{start} 至今）...")
    raw = yf.download("QQQ", start=start, auto_adjust=True, progress=False)
    if raw.empty:
        raise RuntimeError("yfinance 下载失败，请检查网络")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.dropna(subset=["Close"], inplace=True)

    # 计算 HV20（同 data_fetcher.py 的逻辑）
    log_ret    = np.log(df["Close"] / df["Close"].shift(1))
    hv20_raw   = log_ret.rolling(20).std() * np.sqrt(252)
    df["hv20"] = np.clip(hv20_raw, 0.10, 0.80)

    log.info(f"  共 {len(df)} 个交易日，QQQ ${df['Close'].min():.0f} ~ ${df['Close'].max():.0f}")
    return df


def main():
    log.info(f"目标数据库：{DEFAULT_DB}")
    before = row_count()
    log.info(f"回填前：数据库已有 {before} 行")

    df = fetch_and_prepare(start="2021-01-01")
    written = upsert_df(df)

    after = row_count()
    lo, hi = date_range()
    log.info(f"回填完成：写入 {written} 行，数据库共 {after} 行")
    log.info(f"  覆盖区间：{lo} → {hi}")


if __name__ == "__main__":
    main()
