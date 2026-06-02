"""
SQLite 状态存储
- 加仓冷却期（last_bear_add_date）
- 推送去重（已发送的信号记录，防止同一天重复推送）
- 北极星指标：累计交易金额追踪（cost_tracking_log）
"""
import sqlite3
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "logs" / "state.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(str(DB_PATH))


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS bear_add_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                add_date  TEXT NOT NULL,
                note      TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS signal_sent_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_date   TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                position_id TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS cost_tracking_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                log_date      TEXT NOT NULL,
                signal_type   TEXT NOT NULL,
                position_id   TEXT NOT NULL,
                estimated_net REAL NOT NULL,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS option_iv_cache (
                position_id TEXT PRIMARY KEY,
                iv          REAL NOT NULL,
                fetched_at  TEXT NOT NULL
            )
        """)


# ── 冷却期管理 ─────────────────────────────────────────────

def get_last_bear_add_date() -> Optional[date]:
    with _conn() as c:
        row = c.execute(
            "SELECT add_date FROM bear_add_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row:
        return date.fromisoformat(row[0])
    return None


def record_bear_add(add_date: date, note: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO bear_add_log (add_date, note) VALUES (?, ?)",
            (add_date.isoformat(), note)
        )
    log.info(f"记录 BEAR_ADD：{add_date}")


def is_in_cooldown(cooldown_days: int) -> bool:
    last = get_last_bear_add_date()
    if last is None:
        return False
    return (date.today() - last).days < cooldown_days


def days_since_last_bear_add() -> Optional[int]:
    last = get_last_bear_add_date()
    if last is None:
        return None
    return (date.today() - last).days


# ── 推送去重 ───────────────────────────────────────────────

def already_sent_today(signal_type: str, position_id: str) -> bool:
    today = date.today().isoformat()
    with _conn() as c:
        row = c.execute(
            """SELECT 1 FROM signal_sent_log
               WHERE sent_date=? AND signal_type=? AND position_id=?""",
            (today, signal_type, position_id)
        ).fetchone()
    return row is not None


def mark_sent(signal_type: str, position_id: str) -> None:
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO signal_sent_log (sent_date, signal_type, position_id)
               VALUES (?, ?, ?)""",
            (today, signal_type, position_id)
        )


# ── 北极星指标：成本回收追踪 ───────────────────────────────────

def log_transaction(signal_type: str, position_id: str, amount: float) -> None:
    """记录一笔操作的估算金额（HARVEST 为正值净收益，ROLL_OUT/BEAR_ADD 为负值支出）"""
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO cost_tracking_log (log_date, signal_type, position_id, estimated_net)
               VALUES (?, ?, ?, ?)""",
            (today, signal_type, position_id, amount)
        )
    log.info(f"记录交易金额：{signal_type} / {position_id}  净额 ${amount:+,.0f}")


def get_cumulative_harvest_credits() -> float:
    """累计收割净收益（仅统计 HARVEST 的正值，即卖旧买新的差价收入）"""
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(estimated_net), 0) FROM cost_tracking_log WHERE signal_type='HARVEST'"
        ).fetchone()
    return float(row[0]) if row else 0.0


def get_total_option_invested(initial_cost: float) -> float:
    """
    历史期权总投入 = initial_cost（初始建仓成本）
                   + BEAR_ADD 累计支出（绝对值）
                   + ROLL_OUT 累计支出（绝对值）
    HARVEST 的净收益不计入此处（它减少的是分子，不是分母）。
    """
    with _conn() as c:
        row = c.execute(
            """SELECT COALESCE(SUM(ABS(estimated_net)), 0)
               FROM cost_tracking_log
               WHERE signal_type IN ('BEAR_ADD', 'ROLL_OUT')"""
        ).fetchone()
    extra = float(row[0]) if row else 0.0
    return initial_cost + extra


# ── IV 缓存（由 iv_refresh.py 写入，main.py 读取）────────────────

def save_iv_cache(position_id: str, iv: float) -> None:
    """保存一个持仓的 IV（从 iv_refresh.py 调用，每个交易日收盘后更新）"""
    from datetime import datetime
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        c.execute(
            """INSERT INTO option_iv_cache (position_id, iv, fetched_at)
               VALUES (?, ?, ?)
               ON CONFLICT(position_id) DO UPDATE SET iv=excluded.iv, fetched_at=excluded.fetched_at""",
            (position_id, iv, now)
        )
    log.info(f"IV 缓存更新：{position_id}  IV={iv:.2%}  时间={now}")


def get_iv_cache(position_id: str, max_age_hours: int = 28) -> Optional[float]:
    """
    读取缓存的 IV。若缓存不存在或超过 max_age_hours 小时则返回 None。
    28小时宽限：允许周末/节假日的日报使用上一个交易日的 IV。
    """
    from datetime import datetime, timedelta
    with _conn() as c:
        row = c.execute(
            "SELECT iv, fetched_at FROM option_iv_cache WHERE position_id=?",
            (position_id,)
        ).fetchone()
    if row is None:
        return None
    iv, fetched_at_str = row
    try:
        fetched_at = datetime.strptime(fetched_at_str, "%Y-%m-%dT%H:%M:%SZ")
        age_hours = (datetime.utcnow() - fetched_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            log.warning(f"IV 缓存过期：{position_id}  缓存时间={fetched_at_str}  已过 {age_hours:.1f}h")
            return None
    except ValueError:
        return None
    return float(iv)
