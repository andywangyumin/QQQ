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
# 所有持仓统一使用无色背景，通过 hr 分隔；信号由卡片内文字图标区分
SIGNAL_BG = {
    "HARVEST":           "default",
    "ROLL_OUT":          "default",
    "ROLL_OUT_BLOCKED":  "default",
    "BEAR_ADD":          "default",
    "BEAR_ADD_COOLDOWN": "default",
    "HOLD":              "default",
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
        report_mode: str = "daily",
    ) -> Dict:
        """
        构建飞书卡片。

        report_mode: "daily"（每日日报，主标题 QQQ LEAPS 日报）
                     "snapshot"（手动盘点，主标题 资产盘点）
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

        # ── 标题 & 颜色 ───────────────────────────────────────────────────
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

        if report_mode == "snapshot":
            title        = f"📊 资产盘点　[{qqq_date}]"
            header_color = "orange" if has_action else "blue"
        else:  # daily report
            if has_action:
                title        = f"🔔 QQQ LEAPS 日报 | ⚠️ 今日有操作 [{qqq_date}]"
                header_color = "orange"
            else:
                title        = f"✅ QQQ LEAPS 日报 | 今日无操作 [{qqq_date}]"
                header_color = "green"

        elements: list = []

        # ── QQQ 行情行 ────────────────────────────────────────────────────
        qqq_arrow = "📈" if qqq_chg >= 0 else "📉"
        elements.append(self._div(
            f"**QQQ**　　${qqq_price:.2f}　　{qqq_arrow} 较前日 **{qqq_chg:+.2%}**"
        ))
        elements.append(self._hr())

        # ── 资产概览（三列 grey：总资产 40% | 期权 30% | 现金 30%）─────────
        pnl_color = "green" if pnl >= 0 else "red"
        pnl_arrow = "🟢" if pnl >= 0 else "🔴"
        pnl_sign  = "+" if pnl >= 0 else ""
        opt_pct   = opt_val / total if total else 0.0
        cash_warn = "  ⚠️ 低于安全线" if cash_pct < 0.10 else ""

        col_total = self._column("40%",
            f"**总资产**\n"
            f"**${total:,.0f}**\n"
            f"<font color='{pnl_color}'>"
            f"{pnl_arrow} {pnl_sign}${abs(pnl):,.0f} ({pnl_pct:+.2%})"
            f"</font>\n"
            f"vs 基准 {self._usd(baseline)}"
        )
        col_opt = self._column("30%",
            f"**期权市值**\n"
            f"**{self._usd(opt_val)}**\n"
            f"占比 {opt_pct:.1%}"
        )
        col_cash = self._column("30%",
            f"**现金**\n"
            f"**{self._usd(cash)}**\n"
            f"占比 {cash_pct:.1%}{cash_warn}"
        )
        elements.append(self._column_set([col_total, col_opt, col_cash], bg="grey"))
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

            pc       = "green" if pos_pnl >= 0 else "red"
            ps       = "+" if pos_pnl >= 0 else "-"
            pnl_icon = "🟢" if pos_pnl >= 0 else "🔴"

            expiry_str   = str(pos["expiry"])
            expiry_short = expiry_str[5:] if len(expiry_str) >= 7 else expiry_str

            # 左列第4行：信号类型明确显示，格式统一为 "图标 类型：说明"
            if sig_type == "ROLL_OUT":
                sig_tag = f"🟡 ROLL OUT：DTE {dte}天，需续杯换期"
            elif sig_type == "ROLL_OUT_BLOCKED":
                sig_tag = f"⚠️ ROLL OUT：DTE {dte}天，现金不足"
            elif sig_type == "HARVEST":
                sig_tag = f"🟢 HARVEST：Delta {delta:.3f}，触发收割"
            elif sig_type == "BEAR_ADD":
                sig_tag = f"🔴 BEAR ADD：Delta {delta:.3f}，触发加仓"
            elif sig_type == "BEAR_ADD_COOLDOWN":
                sig_tag = f"🟠 BEAR ADD：冷却中，Delta {delta:.3f}"
            else:
                sig_tag = f"✅ HOLD：DTE {dte}天，持仓观望"

            # 左列固定4行：ID / 行权价 / 到期+DTE / 信号标签
            left = (
                f"**{pos_id}**\n"
                f"行权价　${pos['strike']:.0f}\n"
                f"到期　{expiry_short}　DTE **{dte}天**\n"
                f"{sig_tag}"
            )

            # 右列固定4行：Delta / 估价 / 市值 / P&L（去掉"P&L"标签，用ASCII括号）
            right = (
                f"Delta　**{delta:.3f}**\n"
                f"估价　${price:.2f}\n"
                f"市值　{self._usd(val)}\n"
                f"<font color='{pc}'>"
                f"{pnl_icon} {ps}{self._usd(abs(pos_pnl))} ({pos_pnl_pct:+.1%})"
                f"</font>"
            )

            elements.append(self._column_set(
                [self._column("50%", left), self._column("50%", right)],
                bg="default",
            ))
            # 持仓之间用 hr 分隔（代替深色背景块）
            elements.append(self._hr())
            # 有操作指令时输出（如 ROLL_OUT/HARVEST/BEAR_ADD 的买卖单详情）
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

        # ── 底部备注（合并为一行）────────────────────────────────────────
        elements.append(self._note(
            f"基准：{self._usd(baseline)}（2026-05-24）　BS估算价格仅供参考，操作以市场报价为准，请使用限价单"
        ))

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title":    self._text(title),
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
                cost_net = abs(signal.get("estimated_net") or 0)
                blocks.append(self._div(
                    f"**【操作步骤 — 无限续杯（ROLL OUT）】**\n"
                    f"合约快到期了，需要把它【滚动】到 2 年后的新合约，保持持仓不中断。\n"
                    f"\n"
                    f"**第一步：卖出旧合约（回收资金）**\n"
                    f"　在 IBKR 下**限价卖单**：\n"
                    f"　QQQ Call　行权价 ${s['strike']:.0f}　到期 {s['expiry']}　×{s['quantity']} 张\n"
                    f"　参考挂价 ≈ **${s['est_bid']:.2f}/股**（每张合约约 {self._usd(s['est_bid']*100)}）\n"
                    f"\n"
                    f"**第二步：买入新合约（延期 2 年）**\n"
                    f"　在 IBKR 下**限价买单**：\n"
                    f"　QQQ Call　行权价 ${b['strike']:.0f}　到期 {b['expiry']}（约 {b['target_dte']} 天后）　×{b['quantity']} 张\n"
                    f"　参考挂价 ≈ **${b['est_ask']:.2f}/股**（每张合约约 {self._usd(b['est_ask']*100)}）\n"
                    f"\n"
                    f"**第三步：确认资金缺口**\n"
                    f"　新合约比旧合约贵，差价约 **{self._usd(cost_net)}**（从现金账户扣除）\n"
                    f"　操作前请确认账户现金 > {self._usd(cost_net)}\n"
                    f"\n"
                    f"**第四步：完成后告知 AI**\n"
                    f"　示例：『完成了 ${s['strike']:.0f}C 续杯，新合约到期 {b['expiry']}，买入价 $XXX』\n"
                    f"　AI 自动更新持仓记录\n"
                    f"\n"
                    f"⚠️ **务必使用限价单**，挂买卖中间价，耐心等待成交，**切勿使用市价单**"
                ))

        elif sig_type == "HARVEST":
            s, b = signal.get("action_sell"), signal.get("action_buy")
            if s and b:
                net = signal.get("estimated_net") or 0
                if net > 0:
                    net_desc = f"操作后现金增加约 **{self._usd(net)}**（净收入归入现金仓位）"
                else:
                    net_desc = f"操作后现金减少约 **{self._usd(abs(net))}**（差价从现金扣除）"
                blocks.append(self._div(
                    f"**【操作步骤 — 收割利润（HARVEST）】**\n"
                    f"合约 Delta ≥ 0.90，价格已很贵了。卖掉换成更高行权价的新合约，锁定部分利润。\n"
                    f"\n"
                    f"**第一步：卖出旧合约（套现）**\n"
                    f"　在 IBKR 下**限价卖单**：\n"
                    f"　QQQ Call　行权价 ${s['strike']:.0f}　到期 {s['expiry']}　×{s['quantity']} 张\n"
                    f"　参考挂价 ≈ **${s['est_bid']:.2f}/股**\n"
                    f"\n"
                    f"**第二步：买入新合约（换更高行权价，延期 2 年）**\n"
                    f"　在 IBKR 下**限价买单**：\n"
                    f"　QQQ Call　行权价 ${b['strike']:.0f}　到期 {b['expiry']}（约 {b['target_dte']} 天后）　×{b['quantity']} 张\n"
                    f"　参考挂价 ≈ **${b['est_ask']:.2f}/股**\n"
                    f"\n"
                    f"**第三步：收益情况**\n"
                    f"　{net_desc}\n"
                    f"\n"
                    f"**第四步：完成后告知 AI**\n"
                    f"　示例：『完成了收割，新合约行权价 ${b['strike']:.0f}，到期 {b['expiry']}，买入价 $XXX』\n"
                    f"　AI 自动更新持仓记录\n"
                    f"\n"
                    f"⚠️ **务必使用限价单**，挂买卖中间价，耐心等待成交，**切勿使用市价单**"
                ))

        elif sig_type == "BEAR_ADD":
            b = signal.get("action_buy")
            if b:
                mode_cn = "重炮模式（现金充足，加倍买入）" if "重炮" in b.get("mode", "") else "标准模式"
                blocks.append(self._div(
                    f"**【操作步骤 — 逆势加仓（BEAR ADD · {mode_cn}）】**\n"
                    f"QQQ 下跌 Delta < 0.50，趁低价增持新合约，摊低成本。\n"
                    f"\n"
                    f"**在 IBKR 下限价买单**：\n"
                    f"　QQQ Call　行权价 ${b['strike']:.0f}　到期 {b['expiry']}（约 {b['target_dte']} 天后）　×{b['quantity']} 张\n"
                    f"　参考挂价 ≈ **${b['est_ask']:.2f}/股**（预计总成本约 {self._usd(b.get('est_cost', 0))}）\n"
                    f"\n"
                    f"**完成后告知 AI**：实际买入价 + 到期日，系统自动进入 **30 天冷却期**\n"
                    f"（冷却期内即使 Delta 仍低，也不会再触发加仓信号）\n"
                    f"\n"
                    f"⚠️ **务必使用限价单**，挂买卖中间价，耐心等待成交，**切勿使用市价单**"
                ))

        elif sig_type == "ROLL_OUT_BLOCKED":
            blocks.append(self._div(
                f"⚠️ **需续杯但现金不足**\n"
                f"当前现金低于安全线（< 10% 总资产），暂时无法执行续杯操作。\n"
                f"建议等待现金回升后再操作，或联系 AI 评估处理方案。"
            ))

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
        account, positions, signals, pf.qqq_close, pf.cash_pct, position_greeks,
        report_mode="daily",
    )


def send(webhook_url: str, card: dict, secret: Optional[str] = None) -> bool:
    """模块级发送函数，保持向后兼容"""
    return LarkNotifier(webhook_url, secret).send(card)
