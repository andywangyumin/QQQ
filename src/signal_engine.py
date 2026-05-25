"""
三大信号判断引擎 + 操作建议生成
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, List

from bs_model import OptionGreeks, strike_for_delta, bs_price, compute_greeks


# ── 数据结构 ───────────────────────────────────────────────

@dataclass
class Position:
    id: str
    strike: float
    expiry: date
    quantity: int
    cost_per_share: float
    entry_date: date
    note: str = ""
    exempt_rollout: bool = False  # True = 豁免续杯，仅 HARVEST 触发
    # 由 signal engine 填充
    greeks: Optional[OptionGreeks] = None

    @property
    def dte(self) -> int:
        return max(0, (self.expiry - date.today()).days)

    @property
    def total_cost(self) -> float:
        return self.cost_per_share * self.quantity * 100

    @property
    def current_value(self) -> float:
        if self.greeks is None:
            return 0.0
        return self.greeks.price * self.quantity * 100

    @property
    def pnl(self) -> float:
        return self.current_value - self.total_cost

    @property
    def pnl_pct(self) -> float:
        return self.pnl / self.total_cost if self.total_cost > 0 else 0.0


@dataclass
class PortfolioState:
    positions: List[Position]
    cash: float
    qqq_close: float
    qqq_change_pct: float
    quote_date: date

    @property
    def options_value(self) -> float:
        return sum(p.current_value for p in self.positions)

    @property
    def total_value(self) -> float:
        return self.cash + self.options_value

    @property
    def cash_pct(self) -> float:
        tv = self.total_value
        return self.cash / tv if tv > 0 else 0.0


@dataclass
class SignalResult:
    signal_type: str          # HARVEST / ROLL_OUT / BEAR_ADD / HOLD
    position_id: str
    position: Position
    reason: str
    # 操作建议
    action_sell: Optional[dict] = None   # 卖出指引
    action_buy: Optional[dict] = None    # 买入指引
    estimated_net: Optional[float] = None  # 预估净收入（+）或净支出（-）


# ── 信号判断 ───────────────────────────────────────────────

def _new_expiry(target_dte: int) -> date:
    return date.today() + timedelta(days=target_dte)


def _roll_out_advice(pos: Position, qqq: float, settings: dict) -> dict:
    """生成 ROLL OUT 买入建议（同行权价，目标 DTE）"""
    target_dte = settings["target_dte"]
    r = settings["risk_free_rate"]
    iv = pos.greeks.iv if pos.greeks else 0.20
    new_exp = _new_expiry(target_dte)
    T_new = target_dte / 365.0
    new_price = bs_price(qqq, pos.strike, T_new, r, iv)
    return {
        "strike":      pos.strike,
        "expiry":      new_exp.strftime("%Y-%m-%d"),
        "target_dte":  target_dte,
        "est_ask":     round(new_price * 1.005, 2),   # 加 0.5% 买入价差
        "quantity":    pos.quantity,
    }


def _harvest_advice(pos: Position, qqq: float, settings: dict) -> tuple[dict, dict]:
    """生成 HARVEST 卖出旧 + 买入新建议"""
    target_dte = settings["target_dte"]
    r = settings["risk_free_rate"]
    iv = pos.greeks.iv if pos.greeks else 0.20
    new_exp = _new_expiry(target_dte)
    T_new = target_dte / 365.0

    new_strike = strike_for_delta(qqq, settings["delta_harvest_new"], T_new, r, iv)
    new_price  = bs_price(qqq, new_strike, T_new, r, iv)

    sell = {
        "strike":   pos.strike,
        "expiry":   pos.expiry.strftime("%Y-%m-%d"),
        "est_bid":  round(pos.greeks.price * 0.995, 2) if pos.greeks else None,
        "quantity": pos.quantity,
    }
    buy = {
        "strike":     new_strike,
        "expiry":     new_exp.strftime("%Y-%m-%d"),
        "target_dte": target_dte,
        "est_ask":    round(new_price * 1.005, 2),
        "quantity":   pos.quantity,
    }
    return sell, buy


def _bear_add_advice(pf: PortfolioState, settings: dict) -> dict:
    """生成 BEAR ADD 买入建议"""
    tv = pf.total_value
    cp = pf.cash_pct
    if cp >= settings["heavy_cash_pct"]:
        alloc = tv * settings["heavy_alloc"]
        mode  = "重炮模式"
    else:
        alloc = tv * settings["standard_alloc"]
        mode  = "标准模式"

    r  = settings["risk_free_rate"]
    iv = max((p.greeks.iv for p in pf.positions if p.greeks), default=0.20)
    target_dte = settings["target_dte"]
    new_exp    = _new_expiry(target_dte)
    T_new      = target_dte / 365.0
    new_strike = strike_for_delta(pf.qqq_close, settings["delta_entry"], T_new, r, iv)
    est_ask    = bs_price(pf.qqq_close, new_strike, T_new, r, iv) * 1.005
    qty        = max(1, int(alloc // (est_ask * 100)))

    return {
        "mode":       mode,
        "alloc":      alloc,
        "strike":     new_strike,
        "expiry":     new_exp.strftime("%Y-%m-%d"),
        "target_dte": target_dte,
        "est_ask":    round(est_ask, 2),
        "quantity":   qty,
        "est_cost":   round(est_ask * qty * 100, 2),
    }


# ── 主入口 ─────────────────────────────────────────────────

def evaluate(pf: PortfolioState, settings: dict,
             last_bear_add_date: Optional[date]) -> List[SignalResult]:
    """
    按优先级检查所有信号，返回触发的 SignalResult 列表。
    不触发时返回每个持仓的 HOLD 状态。
    """
    results: List[SignalResult] = []
    cash_ok = pf.cash_pct > settings["min_cash_pct"]

    # ── PASS 1: ROLL OUT（保命优先）──────────────────────
    for pos in pf.positions:
        if pos.greeks is None:
            continue
        if pos.exempt_rollout:
            continue  # 豁免续杯：跳过 ROLL_OUT，等待 HARVEST 信号
        dte = pos.dte
        delta = pos.greeks.delta

        if dte < settings["dte_rollout"] and delta < settings["delta_harvest"]:
            if not cash_ok:
                results.append(SignalResult(
                    signal_type="ROLL_OUT_BLOCKED",
                    position_id=pos.id,
                    position=pos,
                    reason=f"DTE={dte} < {settings['dte_rollout']}，需续杯，"
                           f"但现金仓位 {pf.cash_pct:.1%} < 10%，资金不足！",
                ))
                continue

            buy_advice = _roll_out_advice(pos, pf.qqq_close, settings)
            sell_advice = {
                "strike":   pos.strike,
                "expiry":   pos.expiry.strftime("%Y-%m-%d"),
                "est_bid":  round(pos.greeks.price * 0.995, 2),
                "quantity": pos.quantity,
            }
            est_debit = (buy_advice["est_ask"] - sell_advice["est_bid"]) * pos.quantity * 100
            results.append(SignalResult(
                signal_type="ROLL_OUT",
                position_id=pos.id,
                position=pos,
                reason=f"DTE={dte} 天 < {settings['dte_rollout']} 天，需续杯延期",
                action_sell=sell_advice,
                action_buy=buy_advice,
                estimated_net=-est_debit,
            ))

    # ── PASS 2: HARVEST（收割）──────────────────────────
    for pos in pf.positions:
        if pos.greeks is None:
            continue
        if pos.greeks.delta >= settings["delta_harvest"]:
            sell_adv, buy_adv = _harvest_advice(pos, pf.qqq_close, settings)
            old_bid = sell_adv["est_bid"] or 0
            est_credit = (old_bid - buy_adv["est_ask"]) * pos.quantity * 100
            results.append(SignalResult(
                signal_type="HARVEST",
                position_id=pos.id,
                position=pos,
                reason=f"Delta={pos.greeks.delta:.3f} ≥ {settings['delta_harvest']}，触发收割",
                action_sell=sell_adv,
                action_buy=buy_adv,
                estimated_net=est_credit,
            ))

    # ── PASS 3: BEAR ADD（逆势加仓）────────────────────
    any_low_delta = any(
        p.greeks.delta < settings["delta_bear"]
        for p in pf.positions if p.greeks
    )
    if any_low_delta and cash_ok:
        cooldown_days = settings["cooldown_days"]
        in_cooldown = (
            last_bear_add_date is not None
            and (date.today() - last_bear_add_date).days < cooldown_days
        )
        if in_cooldown:
            days_left = cooldown_days - (date.today() - last_bear_add_date).days
            results.append(SignalResult(
                signal_type="BEAR_ADD_COOLDOWN",
                position_id="portfolio",
                position=pf.positions[0],
                reason=f"Delta < {settings['delta_bear']}，但冷却期未满（还剩 {days_left} 天）",
            ))
        else:
            buy_adv = _bear_add_advice(pf, settings)
            results.append(SignalResult(
                signal_type="BEAR_ADD",
                position_id="portfolio",
                position=pf.positions[0],
                reason=f"持仓 Delta 低于 {settings['delta_bear']}，现金充足，触发逆势加仓",
                action_buy=buy_adv,
                estimated_net=-buy_adv["est_cost"],
            ))
    elif any_low_delta and not cash_ok:
        results.append(SignalResult(
            signal_type="BEAR_ADD_BLOCKED",
            position_id="portfolio",
            position=pf.positions[0],
            reason=f"Delta < {settings['delta_bear']}，但现金仓位 {pf.cash_pct:.1%} < {settings['min_cash_pct']:.0%}，加仓被阻断",
        ))

    # ── PASS 4: HOLD（无信号）──────────────────────────
    triggered_ids = {r.position_id for r in results}
    for pos in pf.positions:
        if pos.id not in triggered_ids:
            if pos.exempt_rollout and pos.dte < settings["dte_rollout"]:
                reason = (f"DTE={pos.dte}天 < {settings['dte_rollout']}，"
                          f"已豁免续杯，等待 Delta ≥ {settings['delta_harvest']} 触发 HARVEST")
            else:
                reason = "无信号，继续持仓"
            results.append(SignalResult(
                signal_type="HOLD",
                position_id=pos.id,
                position=pos,
                reason=reason,
            ))

    return results
