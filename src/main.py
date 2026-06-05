#!/usr/bin/env python3
"""
QQQ LEAPS Call 无限续杯 — 每日监控主程序

用法：
  python main.py              # 全流程：拉数据 + 渲染 + 推送飞书（兼容模式）
  python main.py --prepare    # 只准备：拉数据 + 渲染 + 存入 DB，不推送
  python main.py --notify     # 只推送：从 DB 读取已准备卡片 → 发送飞书
  python main.py --dry-run    # 只打印卡片，不推送（全流程）
  python main.py --force      # 跳过去重检查强制推送
"""
import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

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
        iv_ov  = p.get("iv_override")
        positions.append(Position(
            id=p["id"],
            strike=float(p["strike"]),
            expiry=expiry,
            quantity=int(p["quantity"]),
            cost_per_share=float(p["cost_per_share"]),
            entry_date=entry,
            note=p.get("note", ""),
            exempt_rollout=bool(p.get("exempt_rollout", False)),
            iv_override=float(iv_ov) if iv_ov is not None else None,
        ))
    cash     = float(pos_raw["portfolio"]["cash"])
    baseline = float(pos_raw["portfolio"].get("baseline", cash))
    return positions, cash, baseline


# ── 阶段一：准备卡片 ────────────────────────────────────────

def phase_prepare(dry_run: bool = False) -> None:
    """拉取数据、计算信号、渲染图片，将卡片存入 state.db。"""
    settings, pos_raw       = load_config()
    positions, cash, baseline = build_positions(pos_raw)
    log.info(f"已加载 {len(positions)} 个持仓，现金 ${cash:,.0f}，基准 ${baseline:,.0f}")

    ss.init_db()

    log.info("拉取 QQQ 行情...")
    quote = df.fetch_qqq_quote()
    log.info(f"QQQ 收盘：${quote['close']:.2f}  涨跌：{quote['change_pct']:+.2%}  "
             f"HV20：{quote['hv20']:.1%}  数据日期：{quote['date']}")

    if not df.is_market_open_today(quote["date"]):
        log.warning(f"数据日期 {quote['date']} 超过3天前，可能今日为假期，跳过")
        return

    upsert_day(quote["date"], quote["close"], quote["hv20"])

    log.info("计算各持仓 Greeks...")
    for pos in positions:
        iv = df.fetch_option_iv(
            strike=pos.strike,
            expiry_str=pos.expiry.strftime("%Y-%m-%d"),
            fallback_hv=quote["hv20"],
            iv_override=pos.iv_override,
            position_id=pos.id,
        )
        pos.greeks = compute_greeks(
            S=quote["close"],
            K=pos.strike,
            dte=pos.dte,
            r=settings["risk_free_rate"],
            iv=iv,
        )
        if pos.iv_override:
            iv_tag = " [手动IV]"
        elif ss.get_iv_cache(pos.id) is not None:
            iv_tag = " [DB缓存]"
        else:
            iv_tag = ""
        log.info(
            f"  {pos.id}: DTE={pos.dte}  Delta={pos.greeks.delta:.3f}  "
            f"价格≈${pos.greeks.price:.2f}  IV={iv:.1%}{iv_tag}"
        )

    bs_anchor_options = float(pos_raw["portfolio"].get("bs_anchor_options", 0.0))
    pf = PortfolioState(
        positions=positions,
        cash=cash,
        qqq_close=quote["close"],
        qqq_change_pct=quote["change_pct"],
        quote_date=quote["date"],
        baseline=baseline,
        bs_anchor_options=bs_anchor_options,
    )
    log.info(f"组合总值：${pf.total_value:,.0f}  现金占比：{pf.cash_pct:.1%}")

    last_add = ss.get_last_bear_add_date()
    results  = evaluate(pf, settings, last_add)

    action_count = sum(1 for r in results if r.signal_type in ("HARVEST", "ROLL_OUT", "BEAR_ADD"))
    log.info(f"信号判断完成：{action_count} 个操作信号")
    for r in results:
        log.info(f"  [{r.signal_type}] {r.position_id}: {r.reason}")

    all_hold = all(r.signal_type == "HOLD" for r in results)

    # 构建卡片（prepare 阶段始终生成，包括图片）
    harvest_credits       = ss.get_cumulative_harvest_credits()
    initial_option_cost   = float(pos_raw["portfolio"].get("initial_option_cost", 0.0))
    total_option_invested = ss.get_total_option_invested(initial_option_cost)

    app_id     = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    log.info(f"图片推送凭证：LARK_APP_ID={'已配置' if app_id else '未配置'}  "
             f"LARK_APP_SECRET={'已配置' if app_secret else '未配置'}")

    img_key = None
    card    = None
    if not dry_run:
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
                log.info("图片日报渲染成功，卡片已准备（图片格式）")
        except Exception as e:
            log.warning(f"图片日报生成失败，降级为文字卡片：{e}")

    if card is None:
        card = ln.build_card(
            pf, results, quote["date"], baseline=baseline,
            harvest_credits=harvest_credits,
            total_option_invested=total_option_invested,
        )
        log.info("文字卡片已准备")

    if dry_run:
        log.info("[DRY RUN] 卡片内容（不存入 DB）：")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    # 序列化信号，供 --notify 步骤使用
    signals_data = [
        {
            "signal_type":  r.signal_type,
            "position_id":  r.position_id,
            "estimated_net": r.estimated_net,
            "reason":       r.reason,
        }
        for r in results
    ]
    ss.save_daily_card(card, signals_data, all_hold, settings["send_daily_report"])
    log.info("[PREPARE 完成] 卡片已存入 DB，等待 --notify 在 18:00 发送")


# ── 阶段二：推送卡片 ────────────────────────────────────────

def phase_notify(force: bool = False) -> None:
    """从 state.db 读取已准备卡片，执行去重检查后发送飞书。"""
    ss.init_db()

    loaded = ss.load_daily_card()
    if loaded is None:
        log.warning("未找到今日已准备的卡片，尝试立即重新准备...")
        phase_prepare()
        loaded = ss.load_daily_card()
    if loaded is None:
        log.error("prepare 后仍无卡片，退出")
        sys.exit(1)

    card              = loaded["card"]
    signals           = loaded["signals"]
    all_hold          = loaded["all_hold"]
    send_daily_report = loaded["send_daily_report"]

    if all_hold and not send_daily_report:
        log.info("无信号且关闭了日报推送，今日跳过")
        return

    # 去重检查
    if not force:
        filtered = []
        for s in signals:
            if s["signal_type"] in ("HARVEST", "ROLL_OUT", "BEAR_ADD"):
                if ss.already_sent_today(s["signal_type"], s["position_id"]):
                    log.info(f"  跳过重复推送：{s['signal_type']} / {s['position_id']}")
                    continue
            filtered.append(s)
        signals = filtered

    webhooks_raw = os.environ.get("LARK_WEBHOOK_URLS", "") or os.environ.get("LARK_WEBHOOK_URL", "")
    secret       = os.environ.get("LARK_SECRET", "") or None
    webhooks     = [u.strip() for u in webhooks_raw.split(",") if u.strip()]

    if not webhooks:
        log.error("未设置 LARK_WEBHOOK_URLS，无法推送。请检查 .env 文件。")
        sys.exit(1)

    ok = all(ln.send(url, card, secret) for url in webhooks)
    log.info(f"已推送至 {len(webhooks)} 个 Webhook")

    if ok and not force:
        for s in signals:
            if s["signal_type"] in ("HARVEST", "ROLL_OUT", "BEAR_ADD"):
                ss.mark_sent(s["signal_type"], s["position_id"])
                if s["estimated_net"] is not None:
                    ss.log_transaction(s["signal_type"], s["position_id"], s["estimated_net"])
            if s["signal_type"] == "BEAR_ADD":
                ss.record_bear_add(date.today(), s.get("reason", ""))

    log.info("推送完成")


# ── 全流程（兼容旧用法）────────────────────────────────────

def run(dry_run: bool = False, force: bool = False) -> None:
    """向后兼容入口：prepare + notify 一次执行。"""
    log.info("=" * 60)
    log.info(f"QQQ LEAPS 监控启动  dry_run={dry_run}  force={force}")
    log.info("=" * 60)

    phase_prepare(dry_run=dry_run)

    if not dry_run:
        phase_notify(force=force)

    log.info("运行完成")


# ── 入口 ───────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QQQ LEAPS 每日监控")
    parser.add_argument("--prepare", action="store_true",
                        help="只准备：拉数据+渲染图片，存入 DB，不推送")
    parser.add_argument("--notify", action="store_true",
                        help="只推送：从 DB 读取已准备卡片，发送飞书")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印卡片内容，不推送也不存入 DB")
    parser.add_argument("--force", action="store_true",
                        help="强制推送，跳过去重检查")
    args = parser.parse_args()

    log.info("=" * 60)
    if args.prepare:
        log.info("QQQ LEAPS 监控 — PREPARE 阶段")
        log.info("=" * 60)
        phase_prepare(dry_run=args.dry_run)
    elif args.notify:
        log.info("QQQ LEAPS 监控 — NOTIFY 阶段")
        log.info("=" * 60)
        phase_notify(force=args.force)
    else:
        run(dry_run=args.dry_run, force=args.force)
