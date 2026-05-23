"""
飞书 Lark Bot 推送模块 — 结构化卡片版 v2
LarkNotifier 类 + 模块级兼容函数（供 main.py 调用）
"""
import hashlib
import hmac
import base64
import logging
import time
from datetime import date
from typing import Optional, List, Dict

import requests

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
    "HOLD":              "持仓观望 HOLD",
}
# column_set background_style per signal type
SIGNAL_BG = {
    "HARVEST":           "green",
    "ROLL_OUT":          "yellow",
    "ROLL_OUT_BLOCKED":  "red",
    "BEAR_ADD":          "red",
    "BEAR_ADD_COOLDOWN": "grey",
    "HOLD":              "grey",
}


class LarkNotifier:
    """飞书 Webhook 推送器"""

    def __init__(self, webhook_url: str, secret: Optional[str] = None):
        self.webhook_url = webhook_url
        self.secret = secret

    # ── 基础组件工厂 ──────────────────────────────────────────────────────

    @staticmethod
    def _md(content: str) -> dict:
        return {"tag": "lark_md", "content": content}

    @staticmethod
    def _text(content: str) -> dict:
        return {"tag": "plain_text", "content": content}

    @staticmethod
    def _div(content: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    @staticmethod
    def _hr() -> dict:
        return {"tag": "hr"}

    @staticmethod
    def _note(content: str) -> dict:
        return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}

    @staticmethod
    def _column(width: str, content: str) -> dict:
        return {
            "tag":      "column",
            "width":    width,
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        }

    @staticmethod
    def _column_set(columns: list, bg: str = "default") -> dict:
        return {
            "tag":              "column_set",
            "flex_mode":        "stretch",
            "background_style": bg,
            "columns":          columns,
        }

    # ── 格式化工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _usd(v: float) -> str:
        return f"${v:,.0f}"

    @staticmethod
    def _usd2(v: float) -> str:
        return f"${v:,.2f}"

    # ── 主卡片构建 ────────────────────────────────────────────────────────

    def _build_portfolio_card(
        self,
        account: Dict,
        positions: List[Dict],
        signals: List[Dict],
        qqq_price: float,
        cash_pct: float,
        position_greeks: Dict[str, Dict] = None,
    ) -> Dict:
        """
        构建资产盘点飞书卡片。

        account:        {cash, base_nav, qqq_change_pct?, qqq_date?}
        positions:      [{id, strike, expiry, quantity, cost_per_share}]
        signals:        [{type, position_id?, reason, action_sell?, action_buy?,
                          estimated_net?}]
        qqq_price:      float
        cash_pct:       float  (0~1)
        position_greeks:{pos_id: {delta, price, dte}}
        """
        greeks   = position_greeks or {}
        cash     = float(account["cash"])
        baseline = float(account.get("base_nav", 100_000.0))
        qqq_chg  = float(account.get("qqq_change_pct", 0.0))
        qqq_date = account.get("qqq_date", str(date.today()))

        # ── 汇总数据 ──────────────────────────────────────────────────────
        opt_val = sum(
            greeks.get(p["id"], {}).get("price", 0.0) * p.get("quantity", 1) * 100
            for p in positions
        )
        total   = cash + opt_val
        pnl     = total - baseline
        pnl_pct = pnl / baseline if baseline else 0.0

        # ── 信号分类 ──────────────────────────────────────────────────────
        signal_map = {
            s["position_id"]: s
            for s in signals
            if s.get("type") not in ("BEAR_ADD", "BEAR_ADD_COOLDOWN")
            and "position_id" in s
        }
        bear_signals = [
            s for s in signals
            if s.get("type") in ("BEAR_ADD", "BEAR_ADD_COOLDOWN")
        ]

        # ── 标题颜色（有操作信号或可自动推断出操作信号时显示橙色）────────
        action_types = {"HARVEST", "ROLL_OUT", "BEAR_ADD"}
        has_action   = any(s.get("type") in action_types for s in signals)
        # signals=[] 时从持仓数据补充判断（与位置块内的自动推断逻辑保持一致）
        if not has_action:
            for p in positions:
                g2 = greeks.get(p["id"], {})
                d2, t2 = g2.get("delta", 0.0), g2.get("dte", 0)
                if (t2 < 300 and d2 < 0.90) or d2 >= 0.90 or d2 < 0.50:
                    has_action = True
                    break
        header_color = "orange" if has_action else "blue"

        elements: list = []

        # ── QQQ 行情行 ────────────────────────────────────────────────────
        qqq_arrow = "📈" if qqq_chg >= 0 else "📉"
        elements.append(self._div(
            f"**QQQ**　　${qqq_price:.2f}　　{qqq_arrow} 较前日 **{qqq_chg:+.2%}**"
        ))
        elements.append(self._hr())

        # ── 资产概览（两列 grey 背景块）───────────────────────────────────
        pnl_color = "green" if pnl >= 0 else "red"
        pnl_arrow = "🟢" if pnl >= 0 else "🔴"
        pnl_sign  = "+" if pnl >= 0 else ""
        cash_warn = "　⚠️ 低于安全线" if cash_pct < 0.10 else ""

        col_left = self._column("50%",
            f"**总资产**\n"
            f"**${total:,.0f}**\n"
            f"<font color='{pnl_color}'>"
            f"{pnl_arrow} {pnl_sign}${abs(pnl):,.0f}（{pnl_pct:+.2%}）"
            f"</font>"
        )
        col_right = self._column("50%",
            f"**期权市值**　{self._usd(opt_val)}\n"
            f"**现金**　{self._usd(cash)}\n"
            f"占比 {cash_pct:.1%}{cash_warn}"
        )
        elements.append(self._column_set([col_left, col_right], bg="grey"))
        elements.append(self._hr())

        # ── 持仓明细 ──────────────────────────────────────────────────────
        elements.append(self._div("**📋 持仓明细**"))

        for pos in positions:
            pos_id = pos["id"]

            g     = greeks.get(pos_id, {})
            delta = g.get("delta", 0.0)
            price = g.get("price", 0.0)
            dte   = g.get("dte", 0)

            # 优先用传入的信号；否则从持仓数据自动推断（供无信号场景如 snapshot.py 使用）
            if pos_id in signal_map:
                signal = signal_map[pos_id]
            elif dte < 300 and delta < 0.90:
                signal = {"type": "ROLL_OUT", "reason": f"DTE={dte}天 < 300，建议续杯换期"}
            elif delta >= 0.90:
                signal = {"type": "HARVEST",  "reason": f"Delta={delta:.3f} ≥ 0.90，建议收割"}
            elif delta < 0.50:
                signal = {"type": "BEAR_ADD", "reason": f"Delta={delta:.3f} < 0.50，可考虑加仓"}
            else:
                signal = {"type": "HOLD",     "reason": "无操作信号，继续持仓观望"}

            sig_type = signal.get("type", "HOLD")

            qty  = pos.get("quantity", 1)
            val  = price * qty * 100
            cost = pos.get("cost_per_share", 0.0) * qty * 100
            pos_pnl     = val - cost
            pos_pnl_pct = pos_pnl / cost if cost else 0.0

            bg       = SIGNAL_BG.get(sig_type, "grey")
            pc       = "green" if pos_pnl >= 0 else "red"
            ps       = "+" if pos_pnl >= 0 else ""
            pnl_icon = "🟢" if pos_pnl >= 0 else "🔴"

            expiry_str   = str(pos["expiry"])
            expiry_short = expiry_str[5:] if len(expiry_str) >= 7 else expiry_str

            # 左列固定4行，所有持仓格式完全一致：ID / 行权价 / 到期+DTE / 状态标签
            dte_tag = "⚠️ 需续杯（DTE < 300天）" if dte < 300 else "—"
            left = (
                f"**{pos_id}**\n"
                f"行权价　${pos['strike']:.0f}\n"
                f"到期　{expiry_short}　DTE **{dte}天**\n"
                f"{dte_tag}"
            )

            # 右列固定4行，与左列行数对齐：Delta / 估价 / 市值 / P&L
            right = (
                f"Delta　**{delta:.3f}**\n"
                f"估价　${price:.2f}\n"
                f"市值　{self._usd(val)}\n"
                f"<font color='{pc}'>"
                f"{pnl_icon} P&L　{ps}{self._usd(abs(pos_pnl))}（{pos_pnl_pct:+.1%}）"
                f"</font>"
            )

            elements.append(self._column_set(
                [self._column("50%", left), self._column("50%", right)],
                bg=bg,
            ))

            sig_emoji = SIGNAL_EMOJI.get(sig_type, "⬜")
            sig_cn    = SIGNAL_CN.get(sig_type, sig_type)
            elements.append(self._note(
                f"{sig_emoji} {sig_cn}　{signal.get('reason', '')}"
            ))
            elements += self._operation_block(signal)

        # ── BEAR_ADD（组合级，不依附单一持仓）───────────────────────────
        for sig in bear_signals:
            sig_type  = sig.get("type", "BEAR_ADD")
            sig_emoji = SIGNAL_EMOJI.get(sig_type, "⬜")
            sig_cn    = SIGNAL_CN.get(sig_type, sig_type)
            elements.append(self._note(
                f"{sig_emoji} {sig_cn}　{sig.get('reason', '')}"
            ))
            elements += self._operation_block(sig)

        elements.append(self._hr())

        # ── 底部备注（拆为两行，避免截断）────────────────────────────────
        elements.append(self._note(
            f"基准：{self._usd(baseline)}（2026-05-24 设定）　价格为 Black-Scholes 估算，仅供参考"
        ))
        elements.append(self._note(
            "实际操作以市场报价为准，深度实值 LEAPS 流动性有限，请使用限价单"
        ))

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title":    self._text(f"📊 资产盘点　[{qqq_date}]"),
                    "template": header_color,
                },
                "elements": elements,
            },
        }

    def _operation_block(self, signal: Dict) -> list:
        """返回操作指令 div 列表；无指令则返回空列表"""
        sig_type = signal.get("type", "HOLD")
        blocks: list = []

        if sig_type == "ROLL_OUT":
            s, b = signal.get("action_sell"), signal.get("action_buy")
            if s and b:
                blocks.append(self._div(
                    f"**【操作指令】**\n"
                    f"① 卖出限价单　K=${s['strike']:.0f} 到期 {s['expiry']} ×{s['quantity']}张"
                    f"　参考价 ≈ ${s['est_bid']:.2f}/股\n"
                    f"② 买入限价单　K=${b['strike']:.0f} 到期 {b['expiry']}"
                    f"（DTE≈{b['target_dte']}天）×{b['quantity']}张"
                    f"　参考价 ≈ ${b['est_ask']:.2f}/股\n"
                    f"③ 预估续杯成本　≈ {self._usd(abs(signal.get('estimated_net') or 0))}（现金支出）\n"
                    f"④ 完成后告知 AI 新合约详情，自动更新配置"
                ))

        elif sig_type == "HARVEST":
            s, b = signal.get("action_sell"), signal.get("action_buy")
            if s and b:
                net     = signal.get("estimated_net") or 0
                net_str = (f"预估净收入 ≈ {self._usd(net)}"
                           if net > 0 else f"预估净支出 ≈ {self._usd(abs(net))}")
                blocks.append(self._div(
                    f"**【操作指令】**\n"
                    f"① 卖出限价单　K=${s['strike']:.0f} 到期 {s['expiry']} ×{s['quantity']}张"
                    f"　参考价 ≈ ${s['est_bid']:.2f}/股\n"
                    f"② 买入限价单　K=${b['strike']:.0f} 到期 {b['expiry']}"
                    f"（DTE≈{b['target_dte']}天）×{b['quantity']}张"
                    f"　参考价 ≈ ${b['est_ask']:.2f}/股\n"
                    f"③ {net_str}\n"
                    f"④ 完成后告知 AI 新合约详情，自动更新配置"
                ))

        elif sig_type == "BEAR_ADD":
            b = signal.get("action_buy")
            if b:
                blocks.append(self._div(
                    f"**【操作指令 — {b.get('mode', '标准模式')}】**\n"
                    f"买入限价单　K=${b['strike']:.0f} 到期 {b['expiry']}"
                    f"（DTE≈{b['target_dte']}天）×{b['quantity']}张\n"
                    f"参考价 ≈ ${b['est_ask']:.2f}/股　预估成本 ≈ {self._usd(b.get('est_cost', 0))}\n"
                    f"完成后告知 AI，进入 30 天冷却期"
                ))

        elif sig_type == "ROLL_OUT_BLOCKED":
            blocks.append(self._div(f"⚠️ {signal.get('reason', '')}"))

        return blocks

    # ── 发送 ──────────────────────────────────────────────────────────────

    def send(self, card: dict) -> bool:
        """发送卡片 payload 到飞书 Webhook，返回是否成功"""
        payload = dict(card)
        if self.secret:
            ts = int(time.time())
            payload["timestamp"] = str(ts)
            payload["sign"]      = self._sign(ts)
        try:
            resp = requests.post(
                self.webhook_url, json=payload, timeout=10,
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

    def _sign(self, timestamp: int) -> str:
        msg    = f"{timestamp}\n{self.secret}".encode("utf-8")
        digest = hmac.new(
            self.secret.encode("utf-8"), msg, digestmod=hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode("utf-8")


# ── 模块级兼容函数（供 main.py 调用，接口不变）───────────────────────────

def build_card(pf, results, quote_date, baseline: float = 100_000.0) -> dict:
    """把 PortfolioState / List[SignalResult] 转为 Dict 接口后构建卡片"""
    account = {
        "cash":           pf.cash,
        "base_nav":       baseline,
        "qqq_change_pct": pf.qqq_change_pct,
        "qqq_date":       str(quote_date),
    }

    positions = [
        {
            "id":             pos.id,
            "strike":         pos.strike,
            "expiry":         str(pos.expiry),
            "quantity":       pos.quantity,
            "cost_per_share": pos.cost_per_share,
        }
        for pos in pf.positions
    ]

    signals = []
    for r in results:
        sig: dict = {
            "type":          r.signal_type,
            "reason":        r.reason,
            "action_sell":   getattr(r, "action_sell", None),
            "action_buy":    getattr(r, "action_buy", None),
            "estimated_net": getattr(r, "estimated_net", None),
        }
        if r.signal_type not in ("BEAR_ADD", "BEAR_ADD_COOLDOWN"):
            sig["position_id"] = r.position_id
        signals.append(sig)

    position_greeks: Dict[str, Dict] = {
        pos.id: {
            "delta": pos.greeks.delta,
            "price": pos.greeks.price,
            "dte":   pos.dte,
        }
        for pos in pf.positions
        if pos.greeks
    }

    notifier = LarkNotifier("_dummy_")
    return notifier._build_portfolio_card(
        account, positions, signals, pf.qqq_close, pf.cash_pct, position_greeks
    )


def send(webhook_url: str, card: dict, secret: Optional[str] = None) -> bool:
    """模块级发送函数，保持向后兼容"""
    return LarkNotifier(webhook_url, secret).send(card)
