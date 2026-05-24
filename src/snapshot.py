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
from dotenv import load_dotenv

import data_fetcher as df
from bs_model import compute_greeks
from lark_notifier import LarkNotifier
import state_store as ss

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)


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

    # 计算各持仓 Greeks
    positions_list:  list = []
    position_greeks: dict = {}

    for p in pos_raw["positions"]:
        expiry = date.fromisoformat(p["expiry"])
        dte    = max(0, (expiry - date.today()).days)
        iv     = df.fetch_option_iv(p["strike"], p["expiry"], fallback_hv=hv)
        g      = compute_greeks(S, p["strike"], dte, r, iv)
        val    = g.price * p["quantity"] * 100

        positions_list.append({
            "id":             p["id"],
            "strike":         p["strike"],
            "expiry":         p["expiry"],
            "quantity":       p["quantity"],
            "cost_per_share": p["cost_per_share"],
            "exempt_rollout": bool(p.get("exempt_rollout", False)),
        })
        position_greeks[p["id"]] = {
            "delta": g.delta,
            "price": g.price,
            "dte":   dte,
        }
        log.info(f"  {p['id']}  DTE={dte}  Delta={g.delta:.3f}  "
                 f"估价=${g.price:.2f}  市值=${val:,.0f}")

    # 计算汇总数值
    opt_val  = sum(position_greeks[p["id"]]["price"] * p["quantity"] * 100
                   for p in positions_list)
    total    = cash + opt_val
    cash_pct = cash / total if total else 0.0

    account = {
        "cash":           cash,
        "base_nav":       baseline,
        "qqq_change_pct": quote["change_pct"],
        "qqq_date":       str(quote["date"]),
    }

    # 读取北极星指标数据
    ss.init_db()
    harvest_credits      = ss.get_cumulative_harvest_credits()
    initial_option_cost  = float(pos_raw["portfolio"].get("initial_option_cost", 0.0))
    total_option_invested = ss.get_total_option_invested(initial_option_cost)

    # 构建并推送
    webhooks = [u.strip() for u in
                os.environ.get("LARK_WEBHOOK_URLS", "").split(",") if u.strip()]
    if not webhooks:
        log.error("未设置 LARK_WEBHOOK_URLS")
        sys.exit(1)

    for url in webhooks:
        notifier = LarkNotifier(url)
        card     = notifier._build_snapshot_card(
            account, positions_list, S, cash_pct, position_greeks,
            harvest_credits=harvest_credits,
            total_invested=total_option_invested,
        )
        ok = notifier.send(card)
        log.info(f"推送{'成功' if ok else '失败'}　{url[-20:]}")


if __name__ == "__main__":
    main()
