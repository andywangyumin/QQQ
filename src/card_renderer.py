#!/usr/bin/env python3
"""
将监控数据渲染为 PNG 日报图片（通过 Playwright 无头浏览器截图）。

pipeline：
  build_report_data() → REPORT_DATA dict
  render_card()       → Playwright 截图 → PNG path
"""
import base64
import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT       = Path(__file__).parent.parent
TEMPLATE   = ROOT / "templates" / "report_card.html"
OUTPUT_PNG = ROOT / "charts" / "daily_report.png"
TMP_HTML   = ROOT / "charts" / "_tmp_card.html"

_WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_SIGNAL_PRIORITY = ["HARVEST", "ROLL_OUT", "ROLL_OUT_BLOCKED", "BEAR_ADD", "BEAR_ADD_BLOCKED", "BEAR_ADD_COOLDOWN", "HOLD"]


def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def build_report_data(
    pf,
    results,
    quote_date,
    baseline: float,
    harvest_credits: float,
    total_option_invested: float,
    chart_path: Optional[str] = None,
) -> dict:
    """从监控数据构建 JSX 模板需要的 REPORT_DATA dict。"""

    # 最高优先级信号
    action = next(
        (r for r in sorted(
            results,
            key=lambda r: _SIGNAL_PRIORITY.index(r.signal_type)
                          if r.signal_type in _SIGNAL_PRIORITY else 99
        ) if r.signal_type in ("HARVEST", "ROLL_OUT", "ROLL_OUT_BLOCKED",
                               "BEAR_ADD", "BEAR_ADD_BLOCKED", "BEAR_ADD_COOLDOWN")),
        None,
    )
    signal = action.signal_type if action else "HOLD"

    signal_labels = {
        "HOLD":             "今日无操作",
        "HARVEST":          "执行收割",
        "ROLL_OUT":         "续杯换期",
        "ROLL_OUT_BLOCKED": "续杯受阻（现金不足）",
        "BEAR_ADD":         "逆势加仓",
        "BEAR_ADD_BLOCKED": "加仓受阻（现金不足）",
        "BEAR_ADD_COOLDOWN": "加仓冷却中",
    }

    total   = pf.total_value
    opt_val = total - pf.cash
    pnl     = total - baseline

    # 零成本指标
    zc_pct       = min(100.0, harvest_credits / total_option_invested * 100) if total_option_invested > 0 else 0.0
    zc_remaining = max(0.0, total_option_invested - harvest_credits)

    # 现金受阻的持仓 id 集合（用于覆盖备注）
    roll_out_blocked_ids = {r.position_id for r in results if r.signal_type == "ROLL_OUT_BLOCKED"}

    # 持仓列表
    positions = []
    for pos in pf.positions:
        delta = pos.greeks.delta if pos.greeks else 0.0
        price = pos.greeks.price if pos.greeks else 0.0
        cost  = pos.cost_per_share * pos.quantity * 100
        val   = price * pos.quantity * 100
        pnl_p = val - cost

        if delta >= 0.90:
            state, state_label = "HARVEST", "触发 HARVEST"
        elif getattr(pos, "exempt_rollout", False) and pos.dte < 300:
            state, state_label = "HARVEST_WAIT", "豁免续杯"
        elif pos.dte < 300 and delta < 0.90:
            state, state_label = "ROLL_OUT", "需要续杯"
        else:
            state, state_label = "HOLD", "HOLD"

        note = None
        if pos.id in roll_out_blocked_ids:
            note = "⚠ 需续杯但现金 < 10%，暂无法操作，等待现金回升后执行"
        elif getattr(pos, "exempt_rollout", False) and pos.dte < 300:
            note = "仅等待 Delta ≥ 0.90 触发 HARVEST"

        positions.append({
            "id":         pos.id,
            "strike":     pos.strike,
            "expiry":     pos.expiry.strftime("%Y-%m-%d"),
            "dte":        pos.dte,
            "delta":      round(delta, 3),
            "price":      f"{price:.2f}",
            "cost":       round(cost),
            "value":      round(val),
            "pnl":        round(pnl_p),
            "pnlPct":     pnl_p / cost * 100 if cost else 0.0,
            "state":      state,
            "stateLabel": state_label,
            "note":       note,
        })

    qd = quote_date if isinstance(quote_date, date) else date.fromisoformat(str(quote_date))

    return {
        "date":           str(quote_date),
        "weekday":        _WEEKDAYS_CN[qd.weekday()],
        "signal":         signal,
        "signalLabel":    signal_labels.get(signal, "今日无操作"),
        "totalAssets":    round(total),
        "baselinePnL":    round(pnl),
        "baselinePnLPct": round(pnl / baseline * 100, 2) if baseline else 0.0,
        "qqq": {
            "price":  f"{pf.qqq_close:.2f}",
            "change": round(pf.qqq_change_pct * 100, 2),
        },
        "optionsValue": round(opt_val),
        "cash":         round(pf.cash),
        "optionsPct":   round(opt_val / total * 100, 1) if total else 0.0,
        "cashPct":      round(pf.cash / total * 100, 1) if total else 0.0,
        "zeroCost": {
            "pct":       round(zc_pct, 1),
            "harvested": round(harvest_credits),
            "invested":  round(total_option_invested),
            "remaining": round(zc_remaining),
        },
        "positions": positions,
        "chartB64":  _b64(chart_path) if chart_path and Path(chart_path).exists() else "",
        "footer":    "基准 $100,000（2026-05-24）· 估价为 BS 估算 · moomoo 操作请使用限价单（买卖中间价）",
    }


def render_card(report_data: dict, output_path: Optional[str] = None) -> str:
    """
    将 REPORT_DATA 注入 HTML 模板，用 Playwright 截图，返回 PNG 路径。
    """
    from playwright.sync_api import sync_playwright

    out = Path(output_path) if output_path else OUTPUT_PNG
    out.parent.mkdir(parents=True, exist_ok=True)

    template = TEMPLATE.read_text(encoding="utf-8")
    html = template.replace('"__REPORT_DATA__"', json.dumps(report_data, ensure_ascii=False))

    TMP_HTML.parent.mkdir(parents=True, exist_ok=True)
    TMP_HTML.write_text(html, encoding="utf-8")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 700, "height": 2400})
            page.goto(f"file://{TMP_HTML.resolve()}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)   # 等待字体 + Babel JSX 编译完成
            card = page.locator("#card-root > div").first
            card.screenshot(path=str(out))
            browser.close()
    finally:
        TMP_HTML.unlink(missing_ok=True)

    log.info(f"日报图片已渲染：{out}")
    return str(out)
