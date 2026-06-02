#!/usr/bin/env python3
"""
IV 自动刷新脚本 — 在美股收盘后（UTC 20:30）运行
从 yfinance 获取各持仓的实际隐含波动率，写入 state.db 的 option_iv_cache 表。
次日 UTC 09:00 日报脚本读取此缓存，无需手动更新 positions.yaml 的 iv_override。

用法：
  python iv_refresh.py           # 正式刷新
  python iv_refresh.py --dry-run # 只打印，不写入 DB
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

import data_fetcher as df
import state_store as ss

LOG_FILE = ROOT / "logs" / f"iv_refresh_{date.today().isoformat()}.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("iv_refresh")


def run(dry_run: bool = False) -> None:
    log.info("=" * 50)
    log.info(f"IV 刷新启动  dry_run={dry_run}")
    log.info("=" * 50)

    with open(ROOT / "config" / "positions.yaml", encoding="utf-8") as f:
        pos_raw = yaml.safe_load(f)

    # 拉 QQQ 行情（用于 HV 兜底）
    try:
        quote = df.fetch_qqq_quote()
        hv20 = quote["hv20"]
        log.info(f"QQQ 收盘 ${quote['close']:.2f}，HV20={hv20:.1%}")
    except Exception as e:
        log.error(f"QQQ 行情获取失败：{e}")
        sys.exit(1)

    ss.init_db()

    success = 0
    for p in pos_raw["positions"]:
        pos_id     = p["id"]
        strike     = float(p["strike"])
        expiry_str = p["expiry"]

        # iv_override 仍为优先（若用户明确设置则不覆盖）
        iv_override = p.get("iv_override")
        if iv_override is not None:
            iv = float(iv_override)
            source = "positions.yaml iv_override（跳过 yfinance）"
        else:
            iv = df.fetch_option_iv(
                strike=strike,
                expiry_str=expiry_str,
                fallback_hv=hv20,
            )
            source = "yfinance / HV 回退"

        log.info(f"  {pos_id}: IV={iv:.2%}  来源={source}")

        if not dry_run:
            ss.save_iv_cache(pos_id, iv)
            success += 1

    if dry_run:
        log.info("[DRY RUN] 未写入数据库")
    else:
        log.info(f"IV 缓存已更新：{success} 个持仓")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IV 自动刷新")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
