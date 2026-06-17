#!/usr/bin/env python3
"""
QQQ LEAPS — 盘中实时预警（每15分钟由 GitHub Actions 触发）

逻辑：
  1. 获取最新盘中 QQQ 价格（yfinance 5分钟线）
  2. 用 iv_override（或 HV20 回退）计算各持仓 Greeks
  3. 检查 BEAR_ADD 信号（任意 Delta < 0.50，且现金 > 10%）
  4. 去重：4小时内已推过且 QQQ 未再跌 2% → 跳过
  5. 触发则推送飞书文字预警卡，记录 intraday_alert_log

用法：
  python src/intraday_check.py              # 正常运行
  python src/intraday_check.py --dry-run    # 只打印，不推送
  python src/intraday_check.py --sim-price 683  # 模拟 QQQ 价格测试触发
"""
import argparse
import json
import logging
import os
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

warnings.filterwarnings("ignore")

import yfinance as yf
from dotenv import load_dotenv

import lark_notifier as ln
import state_store as ss
from bs_model import compute_greeks
from signal_engine import Position, PortfolioState, evaluate

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("intraday")


# ── 数据获取 ───────────────────────────────────────────────

def fetch_qqq_intraday(sim_price: float = None) -> dict:
    """
    获取 QQQ 最新盘中价格及日涨跌幅。
    sim_price: 若传入，跳过盘中数据拉取，使用模拟价格（仍需真实昨收和 HV20）。
    """
    # 日线：昨收盘 + HV20（两种情况都需要）
    raw_daily = yf.download("QQQ", period="30d", auto_adjust=True, progress=False)
    if raw_daily.empty:
        raise RuntimeError("yfinance 下载 QQQ 日线数据失败")
    if isinstance(raw_daily.columns, pd.MultiIndex):
        raw_daily.columns = raw_daily.columns.droplevel(1)
    close = raw_daily["Close"].dropna()
    prev_close = float(close.iloc[-1])
    log_ret = np.log(close / close.shift(1)).dropna()
    hv20 = float(np.clip(log_ret.tail(20).std() * np.sqrt(252), 0.10, 0.80))

    if sim_price is not None:
        log.info(f"[模拟模式] 使用模拟价格 ${sim_price:.2f}（昨收 ${prev_close:.2f}）")
        return {
            "price":      sim_price,
            "prev_close": prev_close,
            "change_pct": (sim_price - prev_close) / prev_close,
            "hv20":       hv20,
        }

    # 5分钟线：最新盘中价
    raw_intra = yf.download("QQQ", period="1d", interval="5m", auto_adjust=True, progress=False)
    if raw_intra.empty:
        raise RuntimeError("yfinance 无法获取 QQQ 盘中数据（可能非交易时段）")
    if isinstance(raw_intra.columns, pd.MultiIndex):
        raw_intra.columns = raw_intra.columns.droplevel(1)
    latest_price = float(raw_intra["Close"].dropna().iloc[-1])
    change_pct   = (latest_price - prev_close) / prev_close

    log.info(f"QQQ 盘中价：${latest_price:.2f}  较昨收 {change_pct:+.2%}  HV20：{hv20:.1%}")
    return {
        "price":      latest_price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "hv20":       hv20,
    }


# ── 配置加载 ───────────────────────────────────────────────

def load_config():
    with open(ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    settings = raw["strategy"]
    with open(ROOT / "config" / "positions.yaml", encoding="utf-8") as f:
        pos_raw = yaml.safe_load(f)
    return settings, pos_raw


def build_positions(pos_raw: dict):
    positions = []
    for p in pos_raw["positions"]:
        iv_ov = p.get("iv_override")
        positions.append(Position(
            id=p["id"],
            strike=float(p["strike"]),
            expiry=date.fromisoformat(p["expiry"]),
            quantity=int(p["quantity"]),
            cost_per_share=float(p["cost_per_share"]),
            entry_date=date.fromisoformat(p["entry_date"]),
            note=p.get("note", ""),
            exempt_rollout=bool(p.get("exempt_rollout", False)),
            iv_override=float(iv_ov) if iv_ov is not None else None,
        ))
    cash      = float(pos_raw["portfolio"]["cash"])
    baseline  = float(pos_raw["portfolio"].get("baseline", cash))
    bs_anchor = float(pos_raw["portfolio"].get("bs_anchor_options", 0.0))
    return positions, cash, baseline, bs_anchor


# ── 去重逻辑 ───────────────────────────────────────────────

def should_send_alert(today: date, current_qqq: float,
                      cooldown_hours: int = 4, redrop_pct: float = 0.02) -> bool:
    """
    True  = 允许发送预警
    False = 冷却中，跳过

    规则：
    - 今日无预警记录 → 发送
    - 距上次预警 < 4小时 且 QQQ 未进一步下跌 2% → 跳过
    - 距上次预警 ≥ 4小时 或 QQQ 再跌了 ≥ 2%   → 发送
    """
    last = ss.get_last_intraday_alert(today)
    if last is None:
        return True

    try:
        last_dt = datetime.strptime(
            f"{today.isoformat()} {last['alert_time']}", "%Y-%m-%d %H:%M:%S"
        )
        elapsed_h = (datetime.utcnow() - last_dt).total_seconds() / 3600
    except ValueError:
        return True

    last_price   = last["qqq_price"]
    further_drop = (last_price - current_qqq) / last_price  # 正 = 进一步下跌

    if elapsed_h < cooldown_hours and further_drop < redrop_pct:
        log.info(
            f"去重：距上次预警 {elapsed_h:.1f}h（< {cooldown_hours}h），"
            f"且 QQQ 未进一步下跌 2%（当前额外跌幅 {further_drop:.1%}），跳过"
        )
        return False

    reason = f"距上次 {elapsed_h:.1f}h" if elapsed_h >= cooldown_hours else f"又下跌 {further_drop:.1%}"
    log.info(f"允许推送（{reason}）")
    return True


# ── 主流程 ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="QQQ LEAPS 盘中预警")
    parser.add_argument("--sim-price", type=float, default=None,
                        help="模拟 QQQ 价格（测试用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印卡片，不推送飞书")
    args = parser.parse_args()

    log.info("=" * 50)
    log.info(f"盘中预警检查  UTC {datetime.utcnow().strftime('%H:%M:%S')}")
    log.info("=" * 50)

    ss.init_db()
    settings, pos_raw = load_config()
    positions, cash, baseline, bs_anchor = build_positions(pos_raw)

    try:
        quote = fetch_qqq_intraday(sim_price=args.sim_price)
    except RuntimeError as e:
        log.warning(f"获取行情失败（{e}），跳过本次检查")
        sys.exit(0)

    qqq_price  = quote["price"]
    change_pct = quote["change_pct"]
    hv20       = quote["hv20"]

    # 计算 Greeks（iv_override 优先，其次 HV20；不调用 yfinance 期权链，保持速度）
    for pos in positions:
        iv = (pos.iv_override
              if pos.iv_override and 0.05 < pos.iv_override < 2.0
              else hv20)
        pos.greeks = compute_greeks(
            S=qqq_price, K=pos.strike, dte=pos.dte,
            r=settings["risk_free_rate"], iv=iv,
        )
        log.info(f"  {pos.id}: Delta={pos.greeks.delta:.3f}  iv={iv:.1%}")

    pf = PortfolioState(
        positions=positions,
        cash=cash,
        qqq_close=qqq_price,
        qqq_change_pct=change_pct,
        quote_date=date.today(),
        baseline=baseline,
        bs_anchor_options=bs_anchor,
    )
    log.info(f"现金占比：{pf.cash_pct:.1%}  总值（BS锚点法）：${pf.total_value:,.0f}")

    last_add = ss.get_last_bear_add_date()
    results  = evaluate(pf, settings, last_add)
    bear_add = next((r for r in results if r.signal_type == "BEAR_ADD"), None)

    if bear_add is None:
        log.info("当前无 BEAR_ADD 信号，无需推送")
        # 打印各仓位 delta 供参考
        for r in results:
            log.info(f"  [{r.signal_type}] {r.position_id}: {r.reason}")
        sys.exit(0)

    log.info(f"BEAR_ADD 信号触发！QQQ=${qqq_price:.2f}  {change_pct:+.2%}")

    today = date.today()
    if not args.dry_run and not should_send_alert(today, qqq_price):
        sys.exit(0)

    positions_hit = [
        {
            "id":     pos.id,
            "delta":  pos.greeks.delta,
            "strike": pos.strike,
            "expiry": pos.expiry.strftime("%Y-%m-%d"),
        }
        for pos in pf.positions
        if pos.greeks and pos.greeks.delta < settings["delta_bear"]
    ]

    card = ln.build_intraday_alert_card(
        qqq_price=qqq_price,
        change_pct=change_pct,
        positions_hit=positions_hit,
        action_buy=bear_add.action_buy,
    )

    if args.dry_run:
        log.info("[DRY RUN] 盘中预警卡片（不推送）：")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    webhooks_raw = (os.environ.get("LARK_WEBHOOK_URLS", "")
                    or os.environ.get("LARK_WEBHOOK_URL", ""))
    webhooks = [u.strip() for u in webhooks_raw.split(",") if u.strip()]

    if not webhooks:
        log.error("未设置 LARK_WEBHOOK_URLS，无法推送")
        sys.exit(1)

    ok = all(ln.send(url, card) for url in webhooks)
    if ok:
        ss.record_intraday_alert(today, qqq_price, "BEAR_ADD")
        log.info(f"盘中预警推送成功  QQQ=${qqq_price:.2f}  {change_pct:+.2%}")
    else:
        log.error("飞书推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
