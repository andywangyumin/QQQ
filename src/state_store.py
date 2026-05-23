"""
SQLite 状态存储
- 加仓冷却期（last_bear_add_date）
- 推送去重（已发送的信号记录，防止同一天重复推送）
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
