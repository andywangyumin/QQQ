#!/usr/bin/env python3
"""
QQQ LEAPS Call 无限续杯 — 每日监控主程序

用法：
  python main.py              # 正式运行，拉数据 + 推送飞书
  python main.py --dry-run    # 只打印，不推送
  python main.py --force      # 强制推送（跳过去重检查）
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

# 加入 src 目录到 path（兼容直接运行和 cron 调用）
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from dotenv import load_dotenv

import data_fetcher as df
import state_store  as ss
import lark_notifier as ln
from bs_model import compute_greeks
from signal_engine import Position, PortfolioState, evaluate
from history_store import upsert_day

load_dotenv(ROOT / ".env")

# ── 日志配置 ───────────────────────────────────────────────
LOG_FILE = ROOT / "logs" / f"monitor_{date.today().isoformat()}.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")


# ── 配置加载 ───────────────────────────────────────────────

def load_config() -> tuple[dict, dict]:
    with open(ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings_raw = yaml.safe_load(f)
    settings = settings_raw["strategy"]
    settings["send_daily_report"] = settings_raw["notify"]["send_daily_report"]
    settings["mention_user_id"]   = settings_raw["notify"]["mention_user_id"]

    with open(ROOT / "config" / "positions.yaml", encoding="utf-8") as f:
        pos_raw = yaml.safe_load(f)

    return settings, pos_raw


def build_positions(pos_raw: dict) -> tuple[list[Position], float, float]:
    positions = []
    for p in pos_raw["positions"]:
        expiry = date.fromisoformat(p["expiry"])
        entry  = date.fromisoformat(p["entry_date"])
        positions.append(Position(
            id=p["id"],
            strike=float(p["strike"]),
            expiry=expiry,
            quantity=int(p["quantity"]),
            cost_per_share=float(p["cost_per_share"]),
            entry_date=entry,
            note=p.get("note", ""),
            exempt_rollout=bool(p.get("exempt_rollout", False)),
        ))
    cash     = float(pos_raw["portfolio"]["cash"])
    baseline = float(pos_raw["portfolio"].get("baseline", cash))
    return positions, cash, baseline


# ── 核心流程 ───────────────────────────────────────────────

def run(dry_run: bool = False, force: bool = False) -> None:
    log.info("=" * 60)
    log.info(f"QQQ LEAPS 监控启动  dry_run={dry_run}  force={force}")
    log.info("=" * 60)

    # 1. 加载配置
    settings, pos_raw       = load_config()
    positions, cash, baseline = build_positions(pos_raw)
    log.info(f"已加载 {len(positions)} 个持仓，现金 ${cash:,.0f}，基准 ${baseline:,.0f}")

    # 2. 初始化数据库
    ss.init_db()

    # 3. 拉取 QQQ 行情
    log.info("拉取 QQQ 行情...")
    quote = df.fetch_qqq_quote()
    log.info(f"QQQ 收盘：${quote['close']:.2f}  涨跌：{quote['change_pct']:+.2%}  "
             f"HV20：{quote['hv20']:.1%}  数据日期：{quote['date']}")

    if not df.is_market_open_today(quote["date"]):
        log.warning(f"数据日期 {quote['date']} 超过3天前，可能今日为假期，跳过推送")
        return

    # 写入历史数据库（幂等，每日一行）
    upsert_day(quote["date"], quote["close"], quote["hv20"])

    # 4. 为每个持仓计算 Greeks
    log.info("计算各持仓 Greeks...")
    for pos in positions:
        iv = df.fetch_option_iv(
            strike=pos.strike,
            expiry_str=pos.expiry.strftime("%Y-%m-%d"),
            fallback_hv=quote["hv20"],
        )
        pos.greeks = compute_greeks(
            S=quote["close"],
            K=pos.strike,
            dte=pos.dte,
            r=settings["risk_free_rate"],
            iv=iv,
        )
        log.info(
            f"  {pos.id}: DTE={pos.dte}  Delta={pos.greeks.delta:.3f}  "
            f"价格≈${pos.greeks.price:.2f}  IV={iv:.1%}"
        )

    # 5. 构建组合状态
    pf = PortfolioState(
        positions=positions,
        cash=cash,
        qqq_close=quote["close"],
        qqq_change_pct=quote["change_pct"],
        quote_date=quote["date"],
    )
    log.info(f"组合总值：${pf.total_value:,.0f}  现金占比：{pf.cash_pct:.1%}")

    # 6. 信号判断
    last_add = ss.get_last_bear_add_date()
    results  = evaluate(pf, settings, last_add)

    action_count = sum(1 for r in results
                       if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD"))
    log.info(f"信号判断完成：{action_count} 个操作信号")
    for r in results:
        log.info(f"  [{r.signal_type}] {r.position_id}: {r.reason}")

    # 7. 推送去重（--force 时跳过）
    if not force and not dry_run:
        new_results = []
        for r in results:
            if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD"):
                if ss.already_sent_today(r.signal_type, r.position_id):
                    log.info(f"  跳过重复推送：{r.signal_type} / {r.position_id}")
                    continue
            new_results.append(r)
        results = new_results

    # 若全为 HOLD 且不发日报，提前退出
    all_hold = all(r.signal_type == "HOLD" for r in results)
    if all_hold and not settings["send_daily_report"]:
        log.info("无信号且关闭了日报推送，今日跳过")
        return

    # 8. 构建并发送飞书卡片
    harvest_credits       = ss.get_cumulative_harvest_credits()
    initial_option_cost   = float(pos_raw["portfolio"].get("initial_option_cost", 0.0))
    total_option_invested = ss.get_total_option_invested(initial_option_cost)

    # 渲染图片日报（Playwright）→ 上传飞书图床 → 图片卡片
    # 失败时自动降级为原文字卡片
    img_key  = None
    card     = None
    if not dry_run:
        app_id     = os.environ.get("LARK_APP_ID", "")
        app_secret = os.environ.get("LARK_APP_SECRET", "")
        try:
            from chart_generator import generate_trend_chart
            from feishu_uploader  import upload_chart
            from card_renderer    import build_report_data, render_card

            chart_path  = generate_trend_chart()
            report_data = build_report_data(
                pf, results, quote["date"], baseline,
                harvest_credits, total_option_invested, chart_path,
            )
            card_png = render_card(report_data)
            img_key  = upload_chart(card_png, app_id, app_secret)
            if img_key:
                card = ln.build_image_card(img_key)
                log.info("图片日报渲染成功，使用图片卡片推送")
        except Exception as e:
            log.warning(f"图片日报生成失败，降级为文字卡片：{e}")

    if card is None:
        card = ln.build_card(
            pf, results, quote["date"], baseline=baseline,
            harvest_credits=harvest_credits,
            total_option_invested=total_option_invested,
        )

    if dry_run:
        log.info("[DRY RUN] 卡片内容（不实际发送）：")
        import json
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    webhooks_raw = os.environ.get("LARK_WEBHOOK_URLS", "") or os.environ.get("LARK_WEBHOOK_URL", "")
    secret       = os.environ.get("LARK_SECRET", "") or None
    webhooks     = [u.strip() for u in webhooks_raw.split(",") if u.strip()]

    if not webhooks:
        log.error("未设置 LARK_WEBHOOK_URLS，无法推送。请检查 .env 文件。")
        sys.exit(1)

    ok = all(ln.send(url, card, secret) for url in webhooks)
    log.info(f"已推送至 {len(webhooks)} 个 Webhook")

    # 9. 推送成功后记录状态
    if ok and not force:
        for r in results:
            if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD"):
                ss.mark_sent(r.signal_type, r.position_id)
                if r.estimated_net is not None:
                    ss.log_transaction(r.signal_type, r.position_id, r.estimated_net)
            if r.signal_type == "BEAR_ADD":
                ss.record_bear_add(date.today(), r.reason)

    log.info("运行完成")


# ── 入口 ───────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QQQ LEAPS 每日监控")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印卡片内容，不推送飞书")
    parser.add_argument("--force", action="store_true",
                        help="强制推送，跳过去重检查")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force=args.force)
