"""
飞书 Lark Bot 推送模块
使用飞书交互式卡片（Interactive Card）格式
"""
import hashlib
import hmac
import json
import logging
import time
import base64
from datetime import date
from typing import Optional

import requests

from signal_engine import SignalResult, PortfolioState

log = logging.getLogger(__name__)

SIGNAL_EMOJI = {
    "HARVEST":          "🟢",
    "ROLL_OUT":         "🟡",
    "ROLL_OUT_BLOCKED": "🔴",
    "BEAR_ADD":         "🔴",
    "BEAR_ADD_COOLDOWN":"🟠",
    "HOLD":             "✅",
}

SIGNAL_CN = {
    "HARVEST":          "收割利润 (HARVEST)",
    "ROLL_OUT":         "无限续杯 (ROLL OUT)",
    "ROLL_OUT_BLOCKED": "需续杯但现金不足",
    "BEAR_ADD":         "逆势狙击 (BEAR ADD)",
    "BEAR_ADD_COOLDOWN":"加仓冷却中",
    "HOLD":             "持仓观望",
}

HEADER_COLOR = {
    "has_action": "orange",
    "all_hold":   "green",
    "warning":    "red",
}


def _sign(secret: str, timestamp: int) -> str:
    """飞书 Webhook 签名"""
    msg = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _md(text: str) -> dict:
    return {"tag": "lark_md", "content": text}


def _text(text: str) -> dict:
    return {"tag": "plain_text", "content": text}


def _div(content: str) -> dict:
    return {"tag": "div", "text": _md(content)}


def _hr() -> dict:
    return {"tag": "hr"}


def _note(text: str) -> dict:
    return {"tag": "note", "elements": [_md(text)]}


# ── 卡片构建 ───────────────────────────────────────────────

def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v*100:.2f}%"


def _position_block(r: SignalResult, qqq: float) -> str:
    pos = r.position
    g   = pos.greeks
    emoji = SIGNAL_EMOJI.get(r.signal_type, "⬜")
    signal_cn = SIGNAL_CN.get(r.signal_type, r.signal_type)

    lines = [
        f"**{emoji} {pos.id}**  →  **{signal_cn}**",
        f"行权价 K={_fmt_usd(pos.strike)}  |  到期 {pos.expiry}  |  "
        f"DTE={pos.dte}天  |  张数={pos.quantity}张",
    ]

    if g:
        pnl_sign = "📈" if pos.pnl >= 0 else "📉"
        lines.append(
            f"Delta={g.delta:.3f}  |  当前估价={_fmt_usd(g.price)}/股  |  "
            f"仓位市值={_fmt_usd(pos.current_value)}  |  "
            f"{pnl_sign} P&L={_fmt_usd(pos.pnl)}（{_fmt_pct(pos.pnl_pct)}）"
        )

    # 操作建议
    if r.signal_type == "ROLL_OUT" and r.action_sell and r.action_buy:
        lines += [
            "",
            "**【操作指引】**",
            f"① **卖出（限价单）**：K={_fmt_usd(r.action_sell['strike'])} "
            f"到期 {r.action_sell['expiry']}  ×{r.action_sell['quantity']}张  "
            f"参考买价 ≈ {_fmt_usd(r.action_sell['est_bid'])}/股",
            f"② **买入（限价单）**：K={_fmt_usd(r.action_buy['strike'])} "
            f"到期 {r.action_buy['expiry']}（DTE≈{r.action_buy['target_dte']}天）"
            f"  ×{r.action_buy['quantity']}张  参考卖价 ≈ {_fmt_usd(r.action_buy['est_ask'])}/股",
            f"③ **预估续杯成本**：{_fmt_usd(abs(r.estimated_net or 0))} "
            f"（从现金仓位支出）",
        ]

    elif r.signal_type == "HARVEST" and r.action_sell and r.action_buy:
        net = r.estimated_net or 0
        lines += [
            "",
            "**【操作指引】**",
            f"① **卖出（限价单）**：K={_fmt_usd(r.action_sell['strike'])} "
            f"到期 {r.action_sell['expiry']}  ×{r.action_sell['quantity']}张  "
            f"参考买价 ≈ {_fmt_usd(r.action_sell['est_bid'])}/股",
            f"② **买入（限价单）**：K={_fmt_usd(r.action_buy['strike'])} "
            f"到期 {r.action_buy['expiry']}（DTE≈{r.action_buy['target_dte']}天）"
            f"  ×{r.action_buy['quantity']}张  参考卖价 ≈ {_fmt_usd(r.action_buy['est_ask'])}/股",
            f"③ **预估净收入**：{_fmt_usd(net)}（归入现金仓位）" if net > 0
            else f"③ **预估净支出**：{_fmt_usd(abs(net))}",
        ]

    elif r.signal_type == "BEAR_ADD" and r.action_buy:
        b = r.action_buy
        lines += [
            "",
            f"**【操作指引 — {b['mode']}】**",
            f"**买入（限价单）**：K={_fmt_usd(b['strike'])} "
            f"到期 {b['expiry']}（DTE≈{b['target_dte']}天）"
            f"  ×{b['quantity']}张  参考卖价 ≈ {_fmt_usd(b['est_ask'])}/股",
            f"预估成本 ≈ {_fmt_usd(b['est_cost'])}（执行后进入30天冷却期）",
        ]

    elif r.signal_type == "ROLL_OUT_BLOCKED":
        lines.append(f"⚠️  {r.reason}")

    return "\n".join(lines)


def build_card(pf: PortfolioState, results: list[SignalResult],
               quote_date: date) -> dict:
    action_signals = [r for r in results
                      if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD")]
    warning_signals = [r for r in results
                       if r.signal_type in ("ROLL_OUT_BLOCKED",)]
    all_hold = not action_signals and not warning_signals

    # 标题
    if action_signals:
        title_prefix = f"⚡ {len(action_signals)} 个操作信号"
        header_color = "orange"
    elif warning_signals:
        title_prefix = "⚠️ 需关注"
        header_color = "red"
    else:
        title_prefix = "✅ 今日无操作"
        header_color = "green"

    title = f"QQQ LEAPS 监控日报  {title_prefix}  [{quote_date}]"

    # 顶部摘要
    qqq_chg_str = _fmt_pct(pf.qqq_change_pct)
    qqq_arrow   = "📈" if pf.qqq_change_pct >= 0 else "📉"
    summary = (
        f"**QQQ 收盘**：{_fmt_usd(pf.qqq_close)}  {qqq_arrow} {qqq_chg_str}\n"
        f"**组合总值**：{_fmt_usd(pf.total_value)}  "
        f"（期权市值 {_fmt_usd(pf.options_value)} + 现金 {_fmt_usd(pf.cash)}）\n"
        f"**现金仓位**：{pf.cash_pct:.1%}"
        + ("  ⚠️ 低于安全线" if pf.cash_pct < 0.10 else "")
    )

    elements = [
        _div(summary),
        _hr(),
        _div("**持仓状态与操作信号**"),
    ]

    # 每个持仓一块
    for r in results:
        block_text = _position_block(r, pf.qqq_close)
        elements.append(_div(block_text))
        elements.append(_hr())

    # 底部提示
    elements.append(_note(
        "⚠️ 以上价格为 Black-Scholes 模型估算，实际操作请以市场限价单为准。"
        "深度实值期权流动性有限，请耐心等待成交，切勿使用市价单。"
    ))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title":    _text(title),
                "template": header_color,
            },
            "elements": elements,
        },
    }


# ── 发送 ───────────────────────────────────────────────────

def send(webhook_url: str, card: dict, secret: Optional[str] = None) -> bool:
    payload = dict(card)
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(secret, ts)

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code", 0) != 0:
            log.error(f"飞书返回错误：{result}")
            return False
        log.info("飞书推送成功")
        return True
    except Exception as e:
        log.error(f"飞书推送失败：{e}")
        return False
