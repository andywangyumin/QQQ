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
    "BEAR_ADD_BLOCKED":  "🔴",
    "HOLD":              "✅",
}
SIGNAL_CN = {
    "HARVEST":           "收割利润 HARVEST",
    "ROLL_OUT":          "无限续杯 ROLL OUT",
    "ROLL_OUT_BLOCKED":  "需续杯但现金不足",
    "BEAR_ADD":          "逆势狙击 BEAR ADD",
    "BEAR_ADD_COOLDOWN": "加仓冷却中",
    "BEAR_ADD_BLOCKED":  "加仓信号被阻断（现金不足）",
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
    def _wcol(weight: int, content: str) -> dict:
        """等比权重列：相同 weight 值则等宽，Feishu 官方推荐方式"""
        return {
            "tag":      "column",
            "width":    "weighted",
            "weight":   weight,
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

    # ── 北极星指标 ────────────────────────────────────────────────────────

    def _cost_recovery_line(self, harvest_credits: float, total_invested: float) -> dict:
        """零成本持仓达成率进度行"""
        if total_invested <= 0:
            return self._div("💰 零成本进度　— 暂无数据（initial_option_cost 未配置）")
        pct    = min(1.0, harvest_credits / total_invested)
        filled = int(pct * 10)
        bar    = "█" * filled + "░" * (10 - filled)
        remaining = max(0.0, total_invested - harvest_credits)
        if pct >= 1.0:
            text = (f"<font color='green'>🎉 零成本持仓已达成！"
                    f"累计收割 ${harvest_credits:,.0f}</font>")
        else:
            text = (f"💰 零成本进度　{bar}　{pct:.1%}"
                    f"　· 已收割 ${harvest_credits:,.0f}"
                    f" / 投入 ${total_invested:,.0f}"
                    f"　· 还差 ${remaining:,.0f}")
        return {"tag": "div", "text": {"tag": "lark_md", "content": text}}

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
        harvest_credits: float = 0.0,
        total_invested: float = 0.0,
        img_key: Optional[str] = None,
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
            if s.get("type") in ("BEAR_ADD", "BEAR_ADD_COOLDOWN", "BEAR_ADD_BLOCKED")
        ]

        # ── 标题 & 颜色 ───────────────────────────────────────────────────
        action_types = {"HARVEST", "ROLL_OUT", "BEAR_ADD"}
        has_action   = any(s.get("type") in action_types for s in signals)
        # signals=[] 时从持仓数据补充判断（与位置块内的自动推断逻辑保持一致）
        if not has_action:
            for p in positions:
                g2     = greeks.get(p["id"], {})
                d2, t2 = g2.get("delta", 0.0), g2.get("dte", 0)
                exempt = p.get("exempt_rollout", False)
                if d2 >= 0.90 or d2 < 0.50 or (t2 < 300 and not exempt):
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

        # ── 趋势图（飞书图床 img_key，可选）──────────────────────────────
        if img_key:
            elements.append({
                "tag":     "img",
                "img_key": img_key,
                "alt":     {"tag": "plain_text", "content": "QQQ 6-month trend"},
                "mode":    "fit_horizontal",
                "preview": True,
            })
            elements.append(self._hr())

        # ── QQQ 行情行（左右各占 1/2，weight 等分）────────────────────────
        qqq_arrow = "📈" if qqq_chg >= 0 else "📉"
        qqq_chg_color = "green" if qqq_chg >= 0 else "red"
        elements.append(self._column_set([
            self._wcol(1, f"**QQQ**　　**${qqq_price:.2f}**"),
            self._wcol(1, f"{qqq_arrow} 较前日 　<font color='{qqq_chg_color}'>**{qqq_chg:+.2%}**</font>"),
        ]))
        elements.append(self._hr())

        # ── 资产概览（三列等宽 grey，weight=1 三等分）──────────────────────
        pnl_color = "green" if pnl >= 0 else "red"
        pnl_sign  = "+" if pnl >= 0 else ""
        opt_pct   = opt_val / total if total else 0.0
        cash_warn = "  ⚠️ 低于安全线" if cash_pct < 0.10 else ""

        col_total = self._wcol(1,
            f"**总资产**\n"
            f"**${total:,.0f}**\n"
            f"<font color='{pnl_color}'>{pnl_sign}${abs(pnl):,.0f} ({pnl_pct:+.2%})</font>"
        )
        col_opt = self._wcol(1,
            f"**期权市值**\n"
            f"**{self._usd(opt_val)}**\n"
            f"占比 {opt_pct:.1%}"
        )
        col_cash = self._wcol(1,
            f"**现金**\n"
            f"**{self._usd(cash)}**\n"
            f"占比 {cash_pct:.1%}{cash_warn}"
        )
        elements.append(self._column_set([col_total, col_opt, col_cash], bg="grey"))
        elements.append(self._cost_recovery_line(harvest_credits, total_invested))
        elements.append(self._hr())

        # ── 日报：先输出需要操作的信号（HARVEST / ROLL_OUT），逐合约 ──────
        # 无操作的 HOLD 合约收集到后面单独汇总
        hold_positions = []

        for pos in positions:
            pos_id = pos["id"]
            g      = greeks.get(pos_id, {})
            delta  = g.get("delta", 0.0)
            price  = g.get("price", 0.0)
            dte    = g.get("dte", 0)

            exempt = pos.get("exempt_rollout", False)
            if pos_id in signal_map:
                signal = signal_map[pos_id]
            elif delta >= 0.90:
                signal = {"type": "HARVEST",  "reason": f"Delta={delta:.3f} ≥ 0.90，建议收割"}
            elif delta < 0.50:
                signal = {"type": "BEAR_ADD", "reason": f"Delta={delta:.3f} < 0.50，可考虑加仓"}
            elif dte < 300 and not exempt:
                signal = {"type": "ROLL_OUT", "reason": f"DTE={dte}天 < 300，建议续杯换期"}
            elif dte < 300 and exempt:
                signal = {"type": "HOLD",     "reason": f"DTE={dte}天 < 300，已豁免续杯，等待 Delta ≥ 0.90"}
            else:
                signal = {"type": "HOLD",     "reason": "无操作信号，继续持仓观望"}

            sig_type = signal.get("type", "HOLD")

            qty         = pos.get("quantity", 1)
            val         = price * qty * 100
            cost        = pos.get("cost_per_share", 0.0) * qty * 100
            pos_pnl     = val - cost
            pos_pnl_pct = pos_pnl / cost if cost else 0.0
            pc          = "green" if pos_pnl >= 0 else "red"
            ps          = "+" if pos_pnl >= 0 else "-"
            expiry_str  = str(pos["expiry"])

            if sig_type in ("HARVEST", "ROLL_OUT", "ROLL_OUT_BLOCKED", "BEAR_ADD", "BEAR_ADD_COOLDOWN"):
                left = (
                    f"**{pos_id}**\n"
                    f"行权价 ${pos['strike']:.0f}　到期 {expiry_str}　DTE {dte}天"
                )
                right = (
                    f"Delta **{delta:.3f}**　估价 ${price:.2f}\n"
                    f"市值 {self._usd(val)}　"
                    f"<font color='{pc}'>{ps}{self._usd(abs(pos_pnl))} ({pos_pnl_pct:+.1%})</font>"
                )
                elements.append(self._column_set(
                    [self._wcol(11, left), self._wcol(9, right)],
                    bg="default",
                ))
                elements.append(self._hr())
                # 操作详情（步骤说明）
                op_blocks = self._operation_block(signal)
                elements += op_blocks
                if op_blocks:
                    elements.append(self._hr())
            else:
                hold_positions.append((pos, signal, g))

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

        # ── HOLD 合约：紧凑汇总 ────────────────────────────────────────
        if hold_positions:
            elements.append(self._div("**📋 持仓观望（无需操作）**"))
            for pos, signal, g in hold_positions:
                pos_id  = pos["id"]
                delta   = g.get("delta", 0.0)
                price   = g.get("price", 0.0)
                dte     = g.get("dte", 0)
                qty     = pos.get("quantity", 1)
                val     = price * qty * 100
                cost    = pos.get("cost_per_share", 0.0) * qty * 100
                pos_pnl = val - cost
                pos_pnl_pct = pos_pnl / cost if cost else 0.0
                pc      = "green" if pos_pnl >= 0 else "red"
                ps      = "+" if pos_pnl >= 0 else "-"
                expiry_str = str(pos["expiry"])

                exempt = pos.get("exempt_rollout", False)
                if exempt and dte < 300:
                    left = (
                        f"⏳ **{pos_id}**\n"
                        f"行权价 ${pos['strike']:.0f}　到期 {expiry_str}　DTE {dte}天\n"
                        f"<font color='orange'>豁免续杯 — 仅等待 Delta ≥ 0.90 触发 HARVEST</font>"
                    )
                else:
                    left = (
                        f"✅ **{pos_id}**\n"
                        f"行权价 ${pos['strike']:.0f}　到期 {expiry_str}　DTE {dte}天"
                    )
                right = (
                    f"Delta **{delta:.3f}**　估价 ${price:.2f}\n"
                    f"市值 {self._usd(val)}　"
                    f"<font color='{pc}'>{ps}{self._usd(abs(pos_pnl))} ({pos_pnl_pct:+.1%})</font>"
                )
                elements.append(self._column_set(
                    [self._wcol(11, left), self._wcol(9, right)],
                    bg="default",
                ))
                elements.append(self._hr())

        # ── 底部备注 ────────────────────────────────────────────────────
        elements.append(self._note(
            f"基准 {self._usd(baseline)}（2026-05-24）　价格为 BS 估算，moomoo 操作请挂限价单（买卖中间价）"
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
                    f"**🟡 操作指令 — 无限续杯（ROLL OUT）**\n"
                    f"卖出　QQQ Call ${s['strike']:.0f}　{s['expiry']}　×{s['quantity']}张"
                    f"　参考价 ≈ **${s['est_bid']:.2f}**（约 {self._usd(s['est_bid']*100)}）\n"
                    f"买入　QQQ Call ${b['strike']:.0f}　{b['expiry']}（+{b['target_dte']}天）　×{b['quantity']}张"
                    f"　参考价 ≈ **${b['est_ask']:.2f}**（约 {self._usd(b['est_ask']*100)}）\n"
                    f"差价约 **{self._usd(cost_net)}** 从现金扣除　|　moomoo 限价单，挂买卖中间价"
                ))

        elif sig_type == "HARVEST":
            s, b = signal.get("action_sell"), signal.get("action_buy")
            if s and b:
                net = signal.get("estimated_net") or 0
                net_desc = (f"净收入约 **{self._usd(net)}** 归入现金" if net > 0
                            else f"差价约 **{self._usd(abs(net))}** 从现金扣除")
                blocks.append(self._div(
                    f"**🟢 操作指令 — 收割利润（HARVEST）**\n"
                    f"卖出　QQQ Call ${s['strike']:.0f}　{s['expiry']}　×{s['quantity']}张"
                    f"　参考价 ≈ **${s['est_bid']:.2f}**\n"
                    f"买入　QQQ Call ${b['strike']:.0f}　{b['expiry']}（+{b['target_dte']}天）　×{b['quantity']}张"
                    f"　参考价 ≈ **${b['est_ask']:.2f}**\n"
                    f"{net_desc}　|　moomoo 限价单，挂买卖中间价"
                ))

        elif sig_type == "BEAR_ADD":
            b = signal.get("action_buy")
            if b:
                mode_cn = "重炮" if "重炮" in b.get("mode", "") else "标准"
                blocks.append(self._div(
                    f"**🔴 操作指令 — 逆势加仓（BEAR ADD · {mode_cn}模式）**\n"
                    f"买入　QQQ Call ${b['strike']:.0f}　{b['expiry']}（+{b['target_dte']}天）　×{b['quantity']}张"
                    f"　参考价 ≈ **${b['est_ask']:.2f}**（约 {self._usd(b.get('est_cost', 0))}）\n"
                    f"完成后告知 AI，进入 30 天冷却期　|　moomoo 限价单，挂买卖中间价"
                ))

        elif sig_type == "ROLL_OUT_BLOCKED":
            blocks.append(self._div(
                f"⚠️ **需续杯但现金不足**\n"
                f"当前现金低于安全线（< 10% 总资产），暂时无法执行续杯操作。\n"
                f"建议等待现金回升后再操作，或联系 AI 评估处理方案。"
            ))

        elif sig_type == "BEAR_ADD_BLOCKED":
            blocks.append(self._div(
                f"⚠️ **加仓信号触发但现金不足**\n"
                f"当前持仓 Delta < 0.50，具备逆势加仓条件，但现金低于安全线（< 10% 总资产）。\n"
                f"等待现金恢复（如 HARVEST 后）再考虑加仓。"
            ))

        return blocks

    # ── 资产盘点卡片（手动触发，仅罗列资产，无操作指令）─────────────────────

    def _build_snapshot_card(
        self,
        account: Dict,
        positions: List[Dict],
        qqq_price: float,
        cash_pct: float,
        position_greeks: Dict[str, Dict] = None,
        harvest_credits: float = 0.0,
        total_invested: float = 0.0,
    ) -> Dict:
        """
        手动盘点卡片：简洁罗列资产状况，无操作指令。
        account:        {cash, base_nav, qqq_change_pct?, qqq_date?}
        positions:      [{id, strike, expiry, quantity, cost_per_share}]
        qqq_price:      float
        cash_pct:       float  (0~1)
        position_greeks:{pos_id: {delta, price, dte}}
        """
        greeks   = position_greeks or {}
        cash     = float(account["cash"])
        baseline = float(account.get("base_nav", 100_000.0))
        qqq_chg  = float(account.get("qqq_change_pct", 0.0))
        qqq_date = account.get("qqq_date", str(date.today()))

        opt_val = sum(
            greeks.get(p["id"], {}).get("price", 0.0) * p.get("quantity", 1) * 100
            for p in positions
        )
        total   = cash + opt_val
        pnl     = total - baseline
        pnl_pct = pnl / baseline if baseline else 0.0
        opt_pct = opt_val / total if total else 0.0

        elements: list = []

        # QQQ 行情（左50%价格 | 右50%涨跌）
        qqq_arrow = "📈" if qqq_chg >= 0 else "📉"
        qqq_chg_color = "green" if qqq_chg >= 0 else "red"
        elements.append(self._column_set([
            self._wcol(1, f"**QQQ**　　**${qqq_price:.2f}**"),
            self._wcol(1, f"{qqq_arrow} 较前日 　<font color='{qqq_chg_color}'>**{qqq_chg:+.2%}**</font>"),
        ]))
        elements.append(self._hr())

        # 资产概览（三列等宽 grey，weight=1 三等分）
        pnl_color = "green" if pnl >= 0 else "red"
        pnl_sign  = "+" if pnl >= 0 else ""
        cash_warn = "  ⚠️" if cash_pct < 0.10 else ""

        col_total = self._wcol(1,
            f"**总资产**\n"
            f"**${total:,.0f}**\n"
            f"<font color='{pnl_color}'>{pnl_sign}${abs(pnl):,.0f} ({pnl_pct:+.2%})</font>"
        )
        col_opt = self._wcol(1,
            f"**期权市值**\n"
            f"**{self._usd(opt_val)}**\n"
            f"占比 {opt_pct:.1%}"
        )
        col_cash = self._wcol(1,
            f"**现金**\n"
            f"**{self._usd(cash)}**\n"
            f"占比 {cash_pct:.1%}{cash_warn}"
        )
        elements.append(self._column_set([col_total, col_opt, col_cash], bg="grey"))
        elements.append(self._cost_recovery_line(harvest_credits, total_invested))
        elements.append(self._hr())

        # 持仓明细（紧凑，只显示数据 + 单行状态，无操作指令）
        elements.append(self._div("**📋 持仓明细**"))

        for pos in positions:
            pos_id = pos["id"]
            g      = greeks.get(pos_id, {})
            delta  = g.get("delta", 0.0)
            price  = g.get("price", 0.0)
            dte    = g.get("dte", 0)

            qty     = pos.get("quantity", 1)
            val     = price * qty * 100
            cost    = pos.get("cost_per_share", 0.0) * qty * 100
            pos_pnl = val - cost
            pos_pnl_pct = pos_pnl / cost if cost else 0.0

            pc         = "green" if pos_pnl >= 0 else "red"
            ps         = "+" if pos_pnl >= 0 else "-"
            expiry_str = str(pos["expiry"])

            # 单行信号状态
            exempt = pos.get("exempt_rollout", False)
            if delta >= 0.90:
                status = f"🟢 HARVEST — Delta {delta:.3f} ≥ 0.90，可收割"
            elif delta < 0.50:
                status = f"🔴 BEAR ADD — Delta {delta:.3f} < 0.50，可加仓"
            elif dte < 300 and not exempt:
                status = f"🟡 ROLL OUT — DTE {dte}天 < 300，需续杯换期"
            elif dte < 300 and exempt:
                status = f"⏳ 豁免续杯 — DTE {dte}天，等待 Delta ≥ 0.90"
            else:
                status = f"✅ HOLD — DTE {dte}天，持仓观望"

            left = (
                f"**{pos_id}**\n"
                f"行权价 ${pos['strike']:.0f}　到期 {expiry_str}　DTE **{dte}天**\n"
                f"{status}"
            )
            right = (
                f"Delta **{delta:.3f}**\n"
                f"估价 ${price:.2f}　市值 {self._usd(val)}\n"
                f"<font color='{pc}'>{ps}{self._usd(abs(pos_pnl))} ({pos_pnl_pct:+.1%})</font>"
            )

            elements.append(self._column_set(
                [self._wcol(11, left), self._wcol(9, right)],
                bg="default",
            ))
            elements.append(self._hr())

        elements.append(self._note(
            f"基准 {self._usd(baseline)}（2026-05-24）　BS估算价格仅供参考，以市场报价为准"
        ))

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title":    self._text(f"📊 资产盘点　[{qqq_date}]"),
                    "template": "blue",
                },
                "elements": elements,
            },
        }

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

def build_card(
    pf, results, quote_date,
    baseline: float = 100_000.0,
    harvest_credits: float = 0.0,
    total_option_invested: float = 0.0,
    img_key: Optional[str] = None,
) -> dict:
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
            "exempt_rollout": getattr(pos, "exempt_rollout", False),
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
        if r.signal_type not in ("BEAR_ADD", "BEAR_ADD_COOLDOWN", "BEAR_ADD_BLOCKED"):
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
        harvest_credits=harvest_credits,
        total_invested=total_option_invested,
        img_key=img_key,
    )


def build_image_card(img_key: str) -> dict:
    """构建仅含单张图片的飞书卡片（用于图片日报）"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag":     "img",
                    "img_key": img_key,
                    "alt":     {"tag": "plain_text", "content": "QQQ LEAPS 日报"},
                    "mode":    "fit_horizontal",
                    "preview": True,
                }
            ],
        },
    }


def send(webhook_url: str, card: dict, secret: Optional[str] = None) -> bool:
    """模块级发送函数，保持向后兼容"""
    return LarkNotifier(webhook_url, secret).send(card)
