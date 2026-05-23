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
from typing import Optional, List

import requests

from signal_engine import SignalResult, PortfolioState

log = logging.getLogger(__name__)

SIGNAL_EMOJI = {
    "HARVEST":           "🟢",
    "ROLL_OUT":          "🟡",
    "ROLL_OUT_BLOCKED":  "🔴",
    "BEAR_ADD":          "🔴",
    "BEAR_ADD_COOLDOWN": "🟠",
    "HOLD":              "✅",
}
SIGNAL_CN = {
    "HARVEST":           "收割利润 (HARVEST)",
    "ROLL_OUT":          "无限续杯 (ROLL OUT)",
    "ROLL_OUT_BLOCKED":  "需续杯但现金不足",
    "BEAR_ADD":          "逆势狙击 (BEAR ADD)",
    "BEAR_ADD_COOLDOWN": "加仓冷却中",
    "HOLD":              "持仓观望",
}


def _sign(secret: str, timestamp: int) -> str:
    msg    = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _md(text: str) -> dict:
    return {"tag": "lark_md", "content": text}

def _div(text: str) -> dict:
    return {"tag": "div", "text": _md(text)}

def _hr() -> dict:
    return {"tag": "hr"}

def _note(text: str) -> dict:
    return {"tag": "note", "elements": [_md(text)]}


# ── 格式化工具 ────────────────────────────────────────────────────────────

def _usd(v: float) -> str:
    return f"${v:,.0f}"

def _usd2(v: float) -> str:
    return f"${v:,.2f}"

def _pct(v: float, sign: bool = True) -> str:
    prefix = "+" if sign and v >= 0 else ""
    return f"{prefix}{v*100:.2f}%"

def _pnl_str(v: float) -> str:
    arrow = "📈" if v >= 0 else "📉"
    sign  = "+" if v >= 0 else ""
    return f"{arrow} {sign}{_usd(v)}"


# ── 资产盘点区块 ──────────────────────────────────────────────────────────

def _portfolio_block(pf: PortfolioState, baseline: float) -> str:
    total     = pf.total_value
    pnl       = total - baseline
    pnl_pct   = pnl / baseline
    qqq_arrow = "📈" if pf.qqq_change_pct >= 0 else "📉"

    lines = [
        f"**QQQ**　{_usd2(pf.qqq_close)}　{qqq_arrow} {_pct(pf.qqq_change_pct)}",
        "",
        f"**总资产**　{_usd(total)}　　{_pnl_str(pnl)}（{_pct(pnl_pct)}，vs 基准 {_usd(baseline)}）",
        f"　期权市值　{_usd(pf.options_value)}　　现金　{_usd(pf.cash)}　（现金占比 {pf.cash_pct:.1%}"
        + ("　⚠️ 低于安全线 10%" if pf.cash_pct < 0.10 else "）"),
        "",
        "**持仓明细**",
    ]

    for pos in pf.positions:
        g = pos.greeks
        if g is None:
            continue
        pnl_pos  = pos.pnl
        cost_tot = pos.total_cost
        lines.append(
            f"　{pos.id}　"
            f"K={_usd2(pos.strike)} exp={pos.expiry} DTE={pos.dte}天　"
            f"Delta={g.delta:.3f}　"
            f"估价={_usd2(g.price)}/股　市值={_usd(pos.current_value)}　"
            f"{_pnl_str(pnl_pos)}（成本 {_usd(cost_tot)}）"
        )

    return "\n".join(lines)


# ── 信号操作区块 ──────────────────────────────────────────────────────────

def _signal_block(r: SignalResult) -> str:
    pos   = r.position
    emoji = SIGNAL_EMOJI.get(r.signal_type, "⬜")
    name  = SIGNAL_CN.get(r.signal_type, r.signal_type)

    lines = [f"**{emoji} {pos.id}　→　{name}**"]
    lines.append(f"原因：{r.reason}")

    if r.signal_type == "ROLL_OUT" and r.action_sell and r.action_buy:
        s, b = r.action_sell, r.action_buy
        lines += [
            "",
            "**【今日操作指令】**",
            f"① 卖出（限价单）　K={_usd2(s['strike'])} 到期 {s['expiry']} ×{s['quantity']}张",
            f"　　参考挂单价 ≈ {_usd2(s['est_bid'])}/股",
            f"② 买入（限价单）　K={_usd2(b['strike'])} 到期 {b['expiry']}（DTE≈{b['target_dte']}天）×{b['quantity']}张",
            f"　　参考挂单价 ≈ {_usd2(b['est_ask'])}/股",
            f"③ 预估续杯成本　≈ {_usd(abs(r.estimated_net or 0))}（从现金支出）",
            f"④ 操作完成后告知我新合约详情，我来更新系统配置",
        ]

    elif r.signal_type == "HARVEST" and r.action_sell and r.action_buy:
        s, b  = r.action_sell, r.action_buy
        net   = r.estimated_net or 0
        lines += [
            "",
            "**【今日操作指令】**",
            f"① 卖出（限价单）　K={_usd2(s['strike'])} 到期 {s['expiry']} ×{s['quantity']}张",
            f"　　参考挂单价 ≈ {_usd2(s['est_bid'])}/股",
            f"② 买入（限价单）　K={_usd2(b['strike'])} 到期 {b['expiry']}（DTE≈{b['target_dte']}天）×{b['quantity']}张",
            f"　　参考挂单价 ≈ {_usd2(b['est_ask'])}/股",
            f"③ 预估净收入　≈ {_usd(net)}（计入现金）" if net > 0 else f"③ 预估净支出　≈ {_usd(abs(net))}",
            f"④ 操作完成后告知我新合约详情，我来更新系统配置",
        ]

    elif r.signal_type == "BEAR_ADD" and r.action_buy:
        b = r.action_buy
        lines += [
            "",
            f"**【今日操作指令 — {b['mode']}】**",
            f"买入（限价单）　K={_usd2(b['strike'])} 到期 {b['expiry']}（DTE≈{b['target_dte']}天）×{b['quantity']}张",
            f"　　参考挂单价 ≈ {_usd2(b['est_ask'])}/股　预估成本 ≈ {_usd(b['est_cost'])}",
            f"操作完成后告知我新合约详情，进入 30 天冷却期",
        ]

    elif r.signal_type == "ROLL_OUT_BLOCKED":
        lines.append(f"⚠️  {r.reason}")

    elif r.signal_type == "BEAR_ADD_COOLDOWN":
        lines.append(f"🟠  {r.reason}")

    return "\n".join(lines)


# ── 主卡片构建 ────────────────────────────────────────────────────────────

def build_card(pf: PortfolioState, results: List[SignalResult],
               quote_date: date, baseline: float = 100_000.0) -> dict:

    action_signals  = [r for r in results if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD")]
    warning_signals = [r for r in results if r.signal_type in ("ROLL_OUT_BLOCKED",)]
    all_hold        = not action_signals and not warning_signals

    # 标题
    if action_signals:
        n = len(action_signals)
        title_prefix = f"⚡ {n} 个操作信号"
        header_color = "orange"
    elif warning_signals:
        title_prefix = "⚠️ 需关注"
        header_color = "red"
    else:
        title_prefix = "✅ 今日无操作"
        header_color = "green"

    title = f"QQQ LEAPS 日报　{title_prefix}　[{quote_date}]"

    elements = []

    # ── 一、今日操作指令（有信号时置顶）─────────────────────────
    if action_signals:
        elements.append(_div(f"**{'═'*20} 今日操作 {'═'*20}**"))
        for r in action_signals:
            elements.append(_div(_signal_block(r)))
            elements.append(_hr())

    # ── 二、资产盘点 ──────────────────────────────────────────────
    elements.append(_div(f"**{'─'*20} 资产盘点 {'─'*20}**"))
    elements.append(_div(_portfolio_block(pf, baseline)))
    elements.append(_hr())

    # ── 三、其他信号（HOLD / 冷却）───────────────────────────────
    other = [r for r in results if r.signal_type not in ("HARVEST", "ROLL_OUT", "BEAR_ADD")]
    if other:
        hold_lines = []
        for r in other:
            emoji = SIGNAL_EMOJI.get(r.signal_type, "⬜")
            hold_lines.append(f"{emoji} {r.position_id}　{r.reason}")
        elements.append(_div("\n".join(hold_lines)))
        elements.append(_hr())

    # ── 四、底部提示 ──────────────────────────────────────────────
    elements.append(_note(
        "价格为 Black-Scholes 估算，实际操作以市场报价为准。"
        "深度实值 LEAPS 流动性有限，请用限价单耐心等待成交，切勿使用市价单。"
    ))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title":    {"tag": "plain_text", "content": title},
                "template": header_color,
            },
            "elements": elements,
        },
    }


# ── 发送 ──────────────────────────────────────────────────────────────────

def send(webhook_url: str, card: dict, secret: Optional[str] = None) -> bool:
    payload = dict(card)
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"]      = _sign(secret, ts)
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10,
                             headers={"Content-Type": "application/json"})
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
