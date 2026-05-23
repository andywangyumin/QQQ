"""
飞书 Lark Bot 推送模块 — 结构化卡片版
使用 column_set / note / hr 等飞书原生组件
"""
import hashlib
import hmac
import base64
import logging
import time
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
    "HARVEST":           "收割利润 HARVEST",
    "ROLL_OUT":          "无限续杯 ROLL OUT",
    "ROLL_OUT_BLOCKED":  "需续杯但现金不足",
    "BEAR_ADD":          "逆势狙击 BEAR ADD",
    "BEAR_ADD_COOLDOWN": "加仓冷却中",
    "HOLD":              "持仓观望",
}


# ── 签名 ──────────────────────────────────────────────────────────────────

def _sign(secret: str, timestamp: int) -> str:
    msg    = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


# ── 基础组件工厂 ──────────────────────────────────────────────────────────

def _md(content: str) -> dict:
    return {"tag": "lark_md", "content": content}

def _text(content: str) -> dict:
    return {"tag": "plain_text", "content": content}

def _div(content: str) -> dict:
    return {"tag": "div", "text": _md(content)}

def _hr() -> dict:
    return {"tag": "hr"}

def _note(content: str) -> dict:
    return {"tag": "note", "elements": [_text(content)]}

def _note_md(content: str) -> dict:
    return {"tag": "note", "elements": [_md(content)]}

def _column(width: str, content: str) -> dict:
    return {
        "tag":      "column",
        "width":    width,
        "elements": [{"tag": "div", "text": _md(content)}],
    }

def _column_set(columns: list, bg: str = "default") -> dict:
    return {
        "tag":              "column_set",
        "flex_mode":        "stretch",
        "background_style": bg,
        "columns":          columns,
    }


# ── 格式化工具 ────────────────────────────────────────────────────────────

def _usd(v: float) -> str:
    return f"${v:,.0f}"

def _usd2(v: float) -> str:
    return f"${v:,.2f}"

def _pct(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v*100:.1f}%"


# ── 资产概览区块（三列 column_set）────────────────────────────────────────

def _portfolio_overview(pf: PortfolioState, baseline: float) -> list:
    pnl     = pf.total_value - baseline
    pnl_pct = pnl / baseline
    arrow   = "🟢" if pnl >= 0 else "🔴"
    qqq_dir = "📈" if pf.qqq_change_pct >= 0 else "📉"

    header_line = (
        f"**QQQ**  {_usd2(pf.qqq_close)}  "
        f"{qqq_dir} {_pct(pf.qqq_change_pct)}"
    )

    col_total = _column("33%",
        f"**总资产**\n"
        f"{_usd(pf.total_value)}\n"
        f"{arrow} {_pct(pnl_pct)}（vs 基准 {_usd(baseline)}）"
    )
    col_opt = _column("33%",
        f"**期权市值**\n"
        f"{_usd(pf.options_value)}"
    )
    col_cash = _column("34%",
        f"**现金**\n"
        f"{_usd(pf.cash)}\n"
        f"占比 {pf.cash_pct:.0%}"
        + ("  ⚠️ 低于安全线" if pf.cash_pct < 0.10 else "")
    )

    return [
        _div(header_line),
        _hr(),
        _column_set([col_total, col_opt, col_cash], bg="grey"),
    ]


# ── 单持仓区块（两列 column_set + 可选信号 note）─────────────────────────

def _position_block(r: SignalResult) -> list:
    pos   = r.position
    g     = pos.greeks
    emoji = SIGNAL_EMOJI.get(r.signal_type, "⬜")
    name  = SIGNAL_CN.get(r.signal_type, r.signal_type)

    # 左列：合约基本信息
    left_text = (
        f"**{pos.id}**\n"
        f"行权价　{_usd2(pos.strike)}\n"
        f"到期日　{pos.expiry}　DTE {pos.dte} 天"
    )

    # 右列：Greeks + P&L
    if g:
        pnl_color = "green" if pos.pnl >= 0 else "red"
        pnl_sign  = "+" if pos.pnl >= 0 else ""
        right_text = (
            f"Delta　**{g.delta:.3f}**　估价　{_usd2(g.price)}\n"
            f"市值　{_usd(pos.current_value)}\n"
            f"<font color='{pnl_color}'>"
            f"P&L　{pnl_sign}{_usd(pos.pnl)}（{_pct(pos.pnl_pct)}）"
            f"</font>"
        )
    else:
        right_text = "Greeks 数据不可用"

    blocks: list = [
        _column_set([
            _column("50%", left_text),
            _column("50%", right_text),
        ])
    ]

    # 信号 note
    blocks.append(_note(f"{emoji} {name}　{r.reason}"))

    # 操作指令（有 action 的信号）
    if r.signal_type == "ROLL_OUT" and r.action_sell and r.action_buy:
        s, b = r.action_sell, r.action_buy
        instructions = (
            f"**【操作指令】**\n"
            f"① 卖出限价单　K={_usd2(s['strike'])} 到期 {s['expiry']} ×{s['quantity']}张"
            f"　参考价 ≈ {_usd2(s['est_bid'])}/股\n"
            f"② 买入限价单　K={_usd2(b['strike'])} 到期 {b['expiry']}"
            f"（DTE≈{b['target_dte']}天）×{b['quantity']}张"
            f"　参考价 ≈ {_usd2(b['est_ask'])}/股\n"
            f"③ 预估续杯成本　≈ {_usd(abs(r.estimated_net or 0))}（现金支出）\n"
            f"④ 完成后告知 AI 新合约详情，自动更新配置"
        )
        blocks.append(_div(instructions))

    elif r.signal_type == "HARVEST" and r.action_sell and r.action_buy:
        s, b  = r.action_sell, r.action_buy
        net   = r.estimated_net or 0
        net_str = f"预估净收入 ≈ {_usd(net)}" if net > 0 else f"预估净支出 ≈ {_usd(abs(net))}"
        instructions = (
            f"**【操作指令】**\n"
            f"① 卖出限价单　K={_usd2(s['strike'])} 到期 {s['expiry']} ×{s['quantity']}张"
            f"　参考价 ≈ {_usd2(s['est_bid'])}/股\n"
            f"② 买入限价单　K={_usd2(b['strike'])} 到期 {b['expiry']}"
            f"（DTE≈{b['target_dte']}天）×{b['quantity']}张"
            f"　参考价 ≈ {_usd2(b['est_ask'])}/股\n"
            f"③ {net_str}\n"
            f"④ 完成后告知 AI 新合约详情，自动更新配置"
        )
        blocks.append(_div(instructions))

    elif r.signal_type == "ROLL_OUT_BLOCKED":
        blocks.append(_div(f"⚠️ {r.reason}"))

    return blocks


# ── BEAR_ADD 区块（组合级别，不依附于单个持仓）───────────────────────────

def _bear_add_block(r: SignalResult) -> list:
    emoji = SIGNAL_EMOJI.get(r.signal_type, "⬜")
    name  = SIGNAL_CN.get(r.signal_type, r.signal_type)

    blocks: list = [_note(f"{emoji} {name}　{r.reason}")]

    if r.signal_type == "BEAR_ADD" and r.action_buy:
        b = r.action_buy
        instructions = (
            f"**【操作指令 — {b['mode']}】**\n"
            f"买入限价单　K={_usd2(b['strike'])} 到期 {b['expiry']}"
            f"（DTE≈{b['target_dte']}天）×{b['quantity']}张\n"
            f"参考价 ≈ {_usd2(b['est_ask'])}/股　预估成本 ≈ {_usd(b['est_cost'])}\n"
            f"完成后告知 AI，进入 30 天冷却期"
        )
        blocks.append(_div(instructions))

    return blocks


# ── 主卡片构建 ────────────────────────────────────────────────────────────

def build_card(pf: PortfolioState, results: List[SignalResult],
               quote_date: date, baseline: float = 100_000.0) -> dict:

    action_signals = [r for r in results
                      if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD")]
    has_action     = len(action_signals) > 0

    # 动态标题
    if has_action:
        title        = f"🔔 QQQ LEAPS 日报 | ⚠️ 今日有操作 [{quote_date}]"
        header_color = "orange"
    else:
        title        = f"✅ QQQ LEAPS 日报 | 今日无操作 [{quote_date}]"
        header_color = "green"

    elements: list = []

    # ── 资产概览 ──────────────────────────────────────────────
    elements += _portfolio_overview(pf, baseline)
    elements.append(_hr())

    # ── 持仓明细 ──────────────────────────────────────────────
    elements.append(_div("**📊 持仓明细**"))

    # 按 position_id 建立信号映射（BEAR_ADD 是组合级，单独处理）
    signal_by_pos = {
        r.position_id: r for r in results
        if r.signal_type not in ("BEAR_ADD", "BEAR_ADD_COOLDOWN")
    }
    bear_results = [r for r in results
                    if r.signal_type in ("BEAR_ADD", "BEAR_ADD_COOLDOWN")]

    for pos in pf.positions:
        r = signal_by_pos.get(
            pos.id,
            SignalResult("HOLD", pos.id, pos, "无信号，继续持仓"),
        )
        elements += _position_block(r)
        elements.append(_hr())

    # ── BEAR_ADD（组合级）─────────────────────────────────────
    for r in bear_results:
        elements += _bear_add_block(r)
        elements.append(_hr())

    # ── 底部免责提示 ──────────────────────────────────────────
    elements.append(_note(
        "💡 价格为 Black-Scholes 估算，实际操作以市场报价为准。"
        "深度实值 LEAPS 流动性有限，请用限价单耐心等待成交，切勿使用市价单。"
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
