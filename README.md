# QQQ LEAPS Monitor

An automated daily monitoring system for QQQ LEAPS Call options, implementing the "Infinite Refill" rolling strategy. Pushes actionable signals and a live trend chart to Feishu (Lark) every weekday at 18:00 Beijing time via GitHub Actions — no server required.

一套全自动的 QQQ LEAPS Call 期权监控系统，实现"无限续杯"滚动策略。通过 GitHub Actions 每个工作日北京时间 18:00 自动推送操作信号 + QQQ 趋势图至飞书，无需服务器。

---

## Strategy / 策略简介

The system monitors three signals based on option Delta and DTE (days to expiration):

| Signal | Trigger | Action |
|--------|---------|--------|
| 🟢 **HARVEST** | Delta ≥ 0.90 | Sell old contract (high Delta), buy new at Delta ~0.70, DTE ~700d. Net credit → cash. |
| 🟡 **ROLL OUT** | DTE < 300 AND Delta < 0.90 AND not exempt | Sell old contract, buy same strike at DTE ~700d. Net debit from cash. |
| 🔴 **BEAR ADD** | Delta < 0.50 + cash > 10% + 30d cooldown | Buy new contract at Delta ~0.80, DTE ~700d. |
| ✅ **HOLD** | None of the above | No action. |

**Core assumption:** Nasdaq (QQQ) will continue to appreciate long-term.  
**Position structure:** ~60% LEAPS + ~40% cash reserve (minimum 10% cash at all times).  
**BEAR ADD sizing:** 10% of total portfolio (heavy mode, cash ≥ 40%) or 5% (standard mode, cash 10–40%).

---

## What You Receive Daily / 每日推送内容

Each weekday at 18:00 Beijing time, a Feishu card is pushed containing:

1. **QQQ 6-month trend chart** — auto-generated PNG with colored markers for past operations (🟢 HARVEST, 🟡 ROLL OUT, 🔴 BEAR ADD)
2. **Portfolio overview** — total value vs baseline, options value, cash ratio
3. **Zero-cost progress bar** — tracks how much of the original option investment has been recovered through HARVEST credits
4. **Action instructions** — specific sell/buy limit orders with reference prices (when a signal fires)
5. **Position status** — Delta, estimated price, DTE, and exempt-rollout status for each contract

---

## North Star Metric / 北极星指标

**Zero-Cost Achievement Rate (零成本达成率)**

```
Rate = Cumulative HARVEST Net Credits / Total Option Investment × 100%
```

Every HARVEST generates net cash (sell high Delta, buy lower Delta). When cumulative credits cover the total historical option investment, the position is effectively "free". Progress is shown in every card:

```
💰 零成本进度  ████░░░░░░  40.3%  · 已收割 $11,400 / 投入 $28,292  · 还差 $16,892
```

The system automatically logs every HARVEST credit to `state.db` and updates the bar on each run.

---

## How It Works / 工作原理

```
Every weekday
UTC 09:00 (Beijing 17:00) — Phase 1: Prepare
  ├── Fetch QQQ closing price + HV20 (yfinance)
  ├── Read IV: iv_override → DB cache (iv_refresh) → yfinance → HV20
  ├── Compute Greeks via Black-Scholes for each position
  ├── Evaluate signals (HARVEST / ROLL_OUT / BEAR_ADD / HOLD)
  ├── Generate QQQ 6-month trend chart (auto-backfills from yfinance if needed)
  ├── Playwright renders HTML card → PNG (--no-sandbox for CI)
  ├── Upload PNG to Feishu image API → img_key
  └── Save card to state.db daily_card table

UTC 10:00 (Beijing 18:00) — Phase 2: Notify
  └── Read card from state.db → push Feishu webhook

UTC 20:30 (Beijing next day 04:30) — IV Refresh
  └── Fetch option IVs from yfinance (market just closed) → save to state.db
```

---

## Setup / 部署

### 1. Fork or clone this repo

### 2. Configure positions / 配置持仓

Edit [`config/positions.yaml`](config/positions.yaml):

```yaml
positions:
  - id: "QQQ_261218_620C"
    strike: 620.0
    expiry: "2026-12-18"
    quantity: 1
    cost_per_share: 63.00    # option premium paid per share (not total cost)
    entry_date: "2026-05-23"
    exempt_rollout: true     # skip ROLL_OUT, wait for HARVEST only
    # iv_override: 0.32      # optional: manually entered IV from moomoo (most accurate)

portfolio:
  cash: 58365.0
  baseline: 100000.0         # total portfolio value at strategy start date
  initial_option_cost: 28292.0  # sum of all initial option costs (for zero-cost metric)
```

### 3. No manual history backfill needed / 无需手动回填历史数据

No manual setup needed — on first run, the system automatically backfills 6 months of QQQ price history from yfinance. Each subsequent daily run appends the latest price automatically.

### 4. Add GitHub Secrets / 配置 GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Value | Purpose |
|-------------|-------|---------|
| `LARK_WEBHOOK_URLS` | Comma-separated Feishu webhook URL(s) | Daily card push |
| `LARK_APP_ID` | Feishu custom app ID (`cli_xxx`) | Chart image upload |
| `LARK_APP_SECRET` | Feishu custom app secret | Chart image upload |

For `LARK_APP_ID` / `LARK_APP_SECRET`: create a custom app at [open.feishu.cn](https://open.feishu.cn), enable messaging permissions, then use its credentials. An existing app can be reused.

Multiple webhooks example:
```
https://open.larkoffice.com/open-apis/bot/v2/hook/AAA,https://open.larkoffice.com/open-apis/bot/v2/hook/BBB
```

### 5. Enable GitHub Actions / 启用 Actions

Two workflows run automatically on schedule:
- **monitor.yml** — weekdays UTC 09:00 (prepare) + UTC 10:00 (notify)
- **iv_refresh.yml** — weekdays UTC 20:30 (post-market IV fetch)

To trigger the monitor manually: **Actions → QQQ LEAPS Monitor → Run workflow** (skips sleep, pushes immediately).

---

## Daily Workflow / 日常使用

**Fully automated** — just check Feishu at 18:00 Beijing time each weekday.

**After executing a trade** (the only manual step):  
Tell the AI assistant the new contract details; it updates `positions.yaml` and pushes to GitHub automatically.

```
Example / 示例:
Completed HARVEST:
  Sold:   QQQ 2026-12-18 620C × 1 at $145.00
  Bought: QQQ 2028-06-15 680C × 1 at $118.50
  Net credit: $2,650
```

**Manual snapshot** (anytime, not on a schedule):
```bash
cd src
python snapshot.py
```

---

## Project Structure / 项目结构

```
├── .github/workflows/
│   ├── monitor.yml      # UTC 09:00 prepare + sleep + UTC 10:00 notify
│   └── iv_refresh.yml   # UTC 20:30 after market close, fetch IVs → state.db
├── config/
│   ├── positions.yaml              # Holdings + cash + initial_option_cost
│   └── settings.yaml              # Strategy thresholds and allocations
├── src/
│   ├── main.py          # --prepare / --notify / --dry-run / --force
│   ├── iv_refresh.py    # Standalone IV fetcher (runs post-market)
│   ├── signal_engine.py
│   ├── bs_model.py
│   ├── data_fetcher.py
│   ├── state_store.py   # SQLite: cooldown, dedup, zero-cost, IV cache, daily card
│   ├── history_store.py
│   ├── chart_generator.py  # Auto-backfills from yfinance if < 30 days in DB
│   ├── card_renderer.py    # Playwright with --no-sandbox for CI
│   ├── feishu_uploader.py
│   ├── lark_notifier.py
│   └── snapshot.py
├── templates/
│   └── report_card.html            # React/JSX card template for Playwright
├── charts/                         # Generated chart PNGs (gitignored)
├── logs/                           # state.db + market_history.db (gitignored, cached by Actions)
├── requirements.txt
└── .env.example
```

---

## Backtesting Results / 回测结果

| Period | Strategy CAGR | QQQ Buy & Hold | Max Drawdown |
|--------|--------------|----------------|-------------|
| 2021–2026 (5yr) | **48.4%** | 17.4% | -72.6% |
| 2026 YTD ($50k) | **+53.5%** | +17.2% | — |

Adjusted for taxes and friction costs, realistic expected CAGR: **25–30%** (tax-advantaged account) or **18–22%** (taxable account). Both significantly exceed QQQ buy-and-hold.

> ⚠️ Backtest uses Black-Scholes with realized volatility. Actual results differ due to IV premium, bid-ask spreads, and taxes. The -72.6% max drawdown is real — strategy requires holding through deep losses.

---

## Technical Notes / 技术说明

**IV Estimation:** Deep ITM QQQ LEAPS options have IV ~30%+, but yfinance returns IV=0 for far-dated contracts during pre-market hours (when the monitor runs). HV20 (~16%) also significantly underestimates true IV, causing Delta to be overstated (e.g., HV→Delta=0.96 vs actual 0.85). The system uses a priority chain: (1) `iv_override` in positions.yaml — manually entered from moomoo, most accurate; (2) DB cache from `iv_refresh.py` which runs post-market at UTC 20:30 when yfinance data is fresh; (3) live yfinance option chain IV; (4) HV20 fallback.

**State persistence:** `logs/state.db` is persisted across GitHub Actions runs via `actions/upload-artifact` (90-day retention). Contains: signal dedup log, BEAR_ADD cooldown, zero-cost tracking, IV cache (`option_iv_cache`), and today's prepared card (`daily_card`).

**Trend chart:** Generated fresh on every run from the local SQLite history database. If the DB has fewer than 30 days of data (e.g., fresh container), `chart_generator` automatically backfills 6 months of history from yfinance — self-healing, no manual intervention required. Markers for past operations (HARVEST / ROLL_OUT / BEAR_ADD) are loaded from `cost_tracking_log` and plotted in distinct colors and shapes.

**Prepare / Notify split:** `main.py --prepare` fetches data, computes Greeks, renders the PNG card, and saves everything to `state.db`. `main.py --notify` reads the saved card from DB and pushes to Feishu. If `--notify` finds no card for today, it automatically re-runs prepare first (self-healing). `workflow_dispatch` (manual trigger) skips the sleep and sends immediately.

**Exempt rollout:** Positions with `exempt_rollout: true` skip the ROLL_OUT signal entirely. Useful when a near-expiry contract is better handled by waiting for Delta ≥ 0.90 (HARVEST) rather than rolling at the same strike.

**Signal prices are estimates only.** Always use limit orders at market mid-price for actual execution.

---

## Requirements / 依赖

```
yfinance>=0.2.40
pandas>=2.0.0
numpy>=1.24.0
scipy>=1.10.0
matplotlib>=3.7.0
playwright>=1.40.0
requests>=2.31.0
pyyaml>=6.0
python-dotenv>=1.0.0
```

---

## License

MIT
