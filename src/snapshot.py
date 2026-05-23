#!/usr/bin/env python3
"""
资产盘点快照 — 手动触发，推送飞书
用法：python snapshot.py
"""
import sys, os, logging
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
import requests
from dotenv import load_dotenv

import data_fetcher as df
from bs_model import compute_greeks

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)


# ── 卡片组件 ──────────────────────────────────────────────────────────────

def _md(t):   return {"tag": "lark_md",    "content": t}
def _text(t): return {"tag": "plain_text", "content": t}
def _div(t):  return {"tag": "div", "text": _md(t)}
def _hr():    return {"tag": "hr"}
def _note(t): return {"tag": "note", "elements": [_text(t)]}

def _col(width, content):
    return {"tag": "column", "width": width,
            "elements": [{"tag": "div", "text": _md(content)}]}

def _cols(cols, bg="default"):
    return {"tag": "column_set", "flex_mode": "stretch",
            "background_style": bg, "columns": cols}


# ── 卡片构建 ──────────────────────────────────────────────────────────────

def build_snapshot_card(quote, positions_data, cash, baseline):
    S         = quote["close"]
    chg       = quote["change_pct"]
    q_date    = quote["date"]
    chg_arrow = "📈" if chg >= 0 else "📉"

    opt_val = sum(p["val"] for p in positions_data)
    total   = cash + opt_val
    pnl     = total - baseline
    pnl_pct = pnl / baseline
    pnl_arrow = "📈" if pnl >= 0 else "📉"
    pnl_color = "green" if pnl >= 0 else "red"

    elements = []

    # ── QQQ 行情 ──────────────────────────────────────────────
    elements.append(_div(
        f"**QQQ**　　${S:.2f}　　"
        f"{chg_arrow} **{chg:+.2%}**　　"
        f"数据日期 {q_date}"
    ))
    elements.append(_hr())

    # ── 总资产概览（三列）─────────────────────────────────────
    pnl_sign = "+" if pnl >= 0 else ""
    elements.append(_cols([
        _col("34%",
             f"**总资产**\n"
             f"**${total:,.0f}**\n"
             f"<font color='{pnl_color}'>"
             f"{pnl_arrow} {pnl_sign}${pnl:,.0f}（{pnl_pct:+.2%}）"
             f"</font>"),
        _col("33%",
             f"**期权市值**\n"
             f"${opt_val:,.0f}"),
        _col("33%",
             f"**现金**\n"
             f"${cash:,.0f}\n"
             f"占比 {cash/total:.1%}"
             + ("　⚠️ 低于安全线" if cash/total < 0.10 else "")),
    ], bg="grey"))
    elements.append(_hr())

    # ── 持仓明细 ──────────────────────────────────────────────
    elements.append(_div("**📋 持仓明细**"))

    for p in positions_data:
        pnl_p    = p["val"] - p["cost"]
        pnl_p_pct= pnl_p / p["cost"]
        pc       = "green" if pnl_p >= 0 else "red"
        ps       = "+" if pnl_p >= 0 else ""
        dte_warn = "　⚠️ 需续杯" if p["dte"] < 300 else ""
        delta_warn = "　🟢 触发收割" if p["delta"] >= 0.90 else (
                     "　🔴 触发加仓" if p["delta"] < 0.50 else "")

        left = (
            f"**{p['id']}**\n"
            f"行权价　${p['strike']:.0f}\n"
            f"到期　{p['expiry']}　DTE **{p['dte']}天**{dte_warn}"
        )
        right = (
            f"Delta　**{p['delta']:.3f}**{delta_warn}\n"
            f"估价　${p['price']:.2f} / 股　市值　${p['val']:,.0f}\n"
            f"成本　${p['cost']:,.0f}　"
            f"<font color='{pc}'>"
            f"P&L　{ps}${pnl_p:,.0f}（{pnl_p_pct:+.1%}）"
            f"</font>"
        )
        elements.append(_cols([_col("50%", left), _col("50%", right)]))

    elements.append(_hr())

    # ── vs 基准说明 ────────────────────────────────────────────
    elements.append(_note(
        f"基准：${baseline:,.0f}（2026-05-24 设定）　"
        f"价格为 Black-Scholes 估算，实际操作以市场报价为准"
    ))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title":    _text(f"📊 资产盘点　[{date.today()}]"),
                "template": "blue",
            },
            "elements": elements,
        },
    }


# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    # 加载配置
    with open(ROOT / "config" / "positions.yaml", encoding="utf-8") as f:
        pos_raw = yaml.safe_load(f)
    with open(ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)["strategy"]

    cash     = float(pos_raw["portfolio"]["cash"])
    baseline = float(pos_raw["portfolio"].get("baseline", 100_000))
    r        = settings["risk_free_rate"]

    # 拉取行情
    log.info("拉取 QQQ 行情...")
    quote = df.fetch_qqq_quote()
    S, hv = quote["close"], quote["hv20"]
    log.info(f"QQQ ${S:.2f}  {quote['change_pct']:+.2%}  HV20={hv:.1%}")

    # 计算各持仓
    positions_data = []
    for p in pos_raw["positions"]:
        from datetime import date as _date
        expiry = _date.fromisoformat(p["expiry"])
        dte    = max(0, (expiry - _date.today()).days)
        iv     = df.fetch_option_iv(p["strike"], p["expiry"], fallback_hv=hv)
        g      = compute_greeks(S, p["strike"], dte, r, iv)
        val    = g.price * p["quantity"] * 100
        cost   = p["cost_per_share"] * p["quantity"] * 100
        positions_data.append({
            "id":     p["id"],
            "strike": p["strike"],
            "expiry": p["expiry"],
            "qty":    p["quantity"],
            "dte":    dte,
            "delta":  g.delta,
            "price":  g.price,
            "val":    val,
            "cost":   cost,
        })
        log.info(f"  {p['id']}  DTE={dte}  Delta={g.delta:.3f}  "
                 f"估价=${g.price:.2f}  市值=${val:,.0f}")

    # 构建并推送
    card     = build_snapshot_card(quote, positions_data, cash, baseline)
    webhooks = [u.strip() for u in
                os.environ.get("LARK_WEBHOOK_URLS", "").split(",") if u.strip()]

    if not webhooks:
        log.error("未设置 LARK_WEBHOOK_URLS")
        sys.exit(1)

    for url in webhooks:
        try:
            resp = requests.post(url, json=card,
                                 headers={"Content-Type": "application/json"},
                                 timeout=10)
            resp.raise_for_status()
            res = resp.json()
            ok  = res.get("code", 0) == 0
            log.info(f"推送{'成功' if ok else '失败：'+str(res)}　{url[-20:]}")
        except Exception as e:
            log.error(f"推送失败：{e}")


if __name__ == "__main__":
    main()
