"""
QQQ 历史行情数据库
- SQLite 存储，表：qqq_daily
- 支持批量 upsert 和增量追加
- 供回测、分析工具读取
"""
import sqlite3
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DB = Path(__file__).parent.parent / "logs" / "market_history.db"

DDL = """
CREATE TABLE IF NOT EXISTS qqq_daily (
    date       TEXT PRIMARY KEY,   -- YYYY-MM-DD
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL NOT NULL,
    volume     INTEGER,
    hv20       REAL,               -- 20日已实现波动率（年化）
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qqq_daily_date ON qqq_daily(date);
"""


def init_db(db_path: Path = DEFAULT_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.executescript(DDL)
    log.debug(f"历史数据库初始化：{db_path}")


def upsert_df(df: pd.DataFrame, db_path: Path = DEFAULT_DB) -> int:
    """
    将 DataFrame 写入数据库（INSERT OR REPLACE）。
    df 必须包含列：close（大写 Close 亦可），索引为日期或含 date 列。
    可选列：open/Open, high/High, low/Low, volume/Volume, hv20。
    返回写入行数。
    """
    init_db(db_path)
    if df.empty:
        return 0

    # 统一列名为小写
    work = df.copy()
    work.columns = [c.lower() for c in work.columns]

    # 日期列：优先从列中取，否则从 index 取
    if "date" in work.columns:
        dates = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d").tolist()
    else:
        dates = pd.to_datetime(work.index).strftime("%Y-%m-%d").tolist()

    def col(name: str):
        return work[name].tolist() if name in work.columns else [None] * len(work)

    opens   = col("open")
    highs   = col("high")
    lows    = col("low")
    closes  = col("close")
    volumes = col("volume")
    hv20s   = col("hv20")

    rows = list(zip(dates, opens, highs, lows, closes, volumes, hv20s))
    sql  = """
        INSERT OR REPLACE INTO qqq_daily
            (date, open, high, low, close, volume, hv20, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """
    with sqlite3.connect(db_path) as con:
        con.executemany(sql, rows)
    log.info(f"历史数据库：写入 {len(rows)} 行")
    return len(rows)


def upsert_day(quote_date: date, close: float, hv20: float,
               db_path: Path = DEFAULT_DB) -> None:
    """从 main.py 每日调用：追加当天收盘数据"""
    init_db(db_path)
    sql = """
        INSERT OR REPLACE INTO qqq_daily (date, close, hv20, updated_at)
        VALUES (?, ?, ?, datetime('now'))
    """
    with sqlite3.connect(db_path) as con:
        con.execute(sql, (str(quote_date), close, hv20))
    log.info(f"历史数据库：追加 {quote_date}  close={close:.2f}  hv20={hv20:.3f}")


def load_df(db_path: Path = DEFAULT_DB,
            start: Optional[str] = None,
            end: Optional[str] = None) -> pd.DataFrame:
    """
    读取历史数据，返回以 date 为索引的 DataFrame。
    可按 start / end（YYYY-MM-DD）过滤。
    """
    init_db(db_path)
    where = []
    params = []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT date, open, high, low, close, volume, hv20 FROM qqq_daily {clause} ORDER BY date"
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(sql, con, params=params, parse_dates=["date"])
    df.set_index("date", inplace=True)
    return df


def row_count(db_path: Path = DEFAULT_DB) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as con:
        return con.execute("SELECT COUNT(*) FROM qqq_daily").fetchone()[0]


def date_range(db_path: Path = DEFAULT_DB) -> tuple:
    """返回 (最早日期, 最新日期) 字符串"""
    init_db(db_path)
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT MIN(date), MAX(date) FROM qqq_daily"
        ).fetchone()
    return row if row else (None, None)
