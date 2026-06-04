#!/usr/bin/env python3
"""
QQQ 6-month trend chart generator.
Plots price history + annotates HARVEST / ROLL_OUT / BEAR_ADD operations.
"""
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # non-interactive backend for cron / server use
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import pandas as pd

ROOT = Path(__file__).parent.parent
CHART_PATH = ROOT / "charts" / "qqq_trend.png"

log = logging.getLogger(__name__)

# Per-signal visual style
OP_STYLE = {
    "HARVEST":  {
        "color":  "#27AE60",
        "marker": "^",
        "size":   160,
        "zorder": 7,
        "label":  "HARVEST  (收割)",
    },
    "ROLL_OUT": {
        "color":  "#F39C12",
        "marker": "D",
        "size":   100,
        "zorder": 6,
        "label":  "ROLL OUT (续杯)",
    },
    "BEAR_ADD": {
        "color":  "#E74C3C",
        "marker": "v",
        "size":   160,
        "zorder": 7,
        "label":  "BEAR ADD (加仓)",
    },
}


def _load_6m_prices() -> pd.DataFrame:
    """Return last 6 months of QQQ daily close from history_store.db.
    Auto-backfills from yfinance when DB has insufficient data (e.g. fresh container)."""
    import yfinance as yf
    import history_store as hs

    cutoff = pd.Timestamp(date.today() - timedelta(days=183))
    df = hs.load_df()

    if df.empty or len(df[df.index >= cutoff]) < 30:
        log.info("history_store 数据不足，从 yfinance 回填近 6 个月历史数据...")
        try:
            raw = yf.download("QQQ", period="6mo", auto_adjust=True, progress=False)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.droplevel(1)
            if not raw.empty:
                hs.upsert_df(raw)
                df = hs.load_df()
                log.info(f"回填完成，共 {len(df)} 条记录")
        except Exception as e:
            log.warning(f"yfinance 回填失败：{e}")

    if df.empty:
        return df
    return df.loc[df.index >= cutoff].copy()


def _load_operations() -> list:
    """Return list of {date, type, net} dicts from cost_tracking_log."""
    db_path = ROOT / "logs" / "state.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as c:
            rows = c.execute(
                "SELECT log_date, signal_type, estimated_net "
                "FROM cost_tracking_log ORDER BY log_date"
            ).fetchall()
        return [
            {"date": date.fromisoformat(r[0]), "type": r[1], "net": r[2]}
            for r in rows
        ]
    except Exception as e:
        log.warning(f"无法读取操作记录：{e}")
        return []


def generate_trend_chart(output_path: str = None) -> str:
    """
    Generate the trend chart and save as PNG.
    Returns the absolute path of the saved file.
    """
    out = Path(output_path) if output_path else CHART_PATH
    out.parent.mkdir(parents=True, exist_ok=True)

    prices = _load_6m_prices()
    if prices.empty:
        raise RuntimeError("history_store 中无价格数据，无法生成图表")

    x_dt    = pd.to_datetime(list(prices.index))   # datetime64 for matplotlib
    y_close = prices["close"].values.astype(float)

    ops = _load_operations()

    # ── Figure & axes ─────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(11, 4.0))
    fig.patch.set_facecolor("#F7F8FA")
    ax.set_facecolor("#FFFFFF")

    # Price area fill + line
    y_floor = y_close.min() * 0.96
    ax.fill_between(x_dt, y_close, y_floor, alpha=0.10, color="#1565C0", zorder=1)
    ax.plot(x_dt, y_close, color="#1565C0", linewidth=1.8, zorder=2)

    # ── Operation markers ─────────────────────────────────────────────────
    price_by_date = dict(zip(prices.index, y_close))
    all_dates = sorted(prices.index)
    plotted_types = set()

    for op in ops:
        cfg = OP_STYLE.get(op["type"])
        if not cfg:
            continue
        # Snap to the nearest available price date on or after the operation date
        future = [d for d in all_dates if d >= op["date"]]
        if not future:
            continue
        snap_date = future[0]
        px = float(price_by_date[snap_date])

        lbl = cfg["label"] if op["type"] not in plotted_types else None
        plotted_types.add(op["type"])

        ax.scatter(
            pd.to_datetime(snap_date), px,
            color=cfg["color"], marker=cfg["marker"],
            s=cfg["size"], zorder=cfg["zorder"],
            edgecolors="white", linewidths=0.9,
            label=lbl,
        )

    # ── Axis formatting ───────────────────────────────────────────────────
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_minor_locator(mdates.WeekdayLocator(byweekday=0))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.grid(True, which="major", alpha=0.22, linestyle="--", color="#AAAAAA", zorder=0)
    ax.grid(True, which="minor", alpha=0.08, linestyle=":",  color="#CCCCCC", zorder=0)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.spines["left"].set_color("#CCCCCC")
    ax.spines["bottom"].set_color("#CCCCCC")
    ax.tick_params(colors="#666666", labelsize=9)
    plt.xticks(rotation=0)

    # ── Title ─────────────────────────────────────────────────────────────
    last_px  = y_close[-1]
    first_px = y_close[0]
    chg      = (last_px - first_px) / first_px
    chg_str  = f"+{chg:.1%}" if chg >= 0 else f"{chg:.1%}"
    chg_col  = "#27AE60" if chg >= 0 else "#E74C3C"
    start_s  = prices.index[0].strftime("%Y-%m-%d")
    end_s    = prices.index[-1].strftime("%Y-%m-%d")

    ax.set_title(
        f"QQQ  6-Month  {start_s} → {end_s}"
        f"    Current ${last_px:,.2f}",
        fontsize=10.5, fontweight="bold", color="#333333", pad=9,
    )
    # Inline change annotation at right of title
    ax.annotate(
        f"  {chg_str}",
        xy=(1, 1), xycoords="axes fraction",
        fontsize=10.5, fontweight="bold", color=chg_col,
        ha="right", va="bottom",
    )

    # ── Legend ────────────────────────────────────────────────────────────
    if plotted_types:
        ax.legend(
            loc="upper left", fontsize=8.5,
            framealpha=0.88, edgecolor="#DDDDDD", fancybox=False,
        )

    plt.tight_layout(pad=0.8)
    plt.savefig(str(out), dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)

    log.info(f"趋势图已生成：{out}")
    return str(out)
