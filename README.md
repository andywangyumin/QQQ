# QQQ LEAPS Monitor 📈

An automated daily monitoring system for QQQ LEAPS Call options, implementing the "Infinite Refill" rolling strategy. Pushes actionable signals to Feishu (Lark) every weekday at 18:00 Beijing time via GitHub Actions — no server required.

一套全自动的 QQQ LEAPS Call 期权监控系统，实现"无限续杯"滚动策略。通过 GitHub Actions 每个工作日北京时间 18:00 自动推送操作信号至飞书，无需服务器。

---

## Strategy / 策略简介

The system monitors three signals based on option Delta and DTE (days to expiration):

| Signal | Trigger | Action |
|--------|---------|--------|
| 🟢 **HARVEST** | Delta ≥ 0.90 | Sell old contract, buy new at Delta ~0.70, DTE ~700d |
| 🟡 **ROLL OUT** | DTE < 300 | Sell old contract, buy same strike at DTE ~700d |
| 🔴 **BEAR ADD** | Delta < 0.50 + cash > 10% + 30d cooldown | Buy new contract at Delta ~0.80, DTE ~700d |
| ✅ **HOLD** | None of the above | No action |

**Core assumption:** Nasdaq (QQQ) will continue to appreciate long-term.  
**Position structure:** 60% LEAPS + 40% cash reserve (minimum 10% cash at all times).

---

## How It Works / 工作原理

```
Every weekday (UTC 10:00 = Beijing 18:00)
        │
        ▼
GitHub Actions runs main.py
        │
        ├── Fetch QQQ closing price + HV20 (yfinance)
        ├── Fetch option IV from yfinance chain, blend with HV20 (7:3)
        ├── Compute Greeks via Black-Scholes for each position
        ├── Evaluate signals (HARVEST / ROLL_OUT / BEAR_ADD / HOLD)
        └── Push Feishu card to all configured webhooks
```

**Lark card includes:**
- Today's action instructions (sell/buy orders with reference prices)
- Portfolio overview: total value vs baseline, P&L per position
- Delta, estimated price, DTE for each contract

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
    cost_per_share: 63.00   # option premium paid, not total cost
    entry_date: "2026-05-23"

portfolio:
  cash: 63651.0
  baseline: 100000.0        # total portfolio value at start date
```

### 3. Add GitHub Secret / 配置飞书 Webhook

Go to **Settings → Secrets → Actions → New repository secret**:

| Name | Value |
|------|-------|
| `LARK_WEBHOOK_URLS` | Comma-separated Feishu webhook URLs |

Example:
```
https://open.larkoffice.com/open-apis/bot/v2/hook/AAA,https://open.larkoffice.com/open-apis/bot/v2/hook/BBB
```

### 4. Enable GitHub Actions / 启用 Actions

The workflow runs automatically. To trigger manually:  
**Actions → QQQ LEAPS Monitor → Run workflow**

---

## Daily Workflow / 日常使用

**Fully automated** — just check Feishu at 18:00 Beijing time each weekday.

**After executing a trade** (the only manual step):  
Tell the AI assistant the new contract details, it will update `positions.yaml` and push to GitHub automatically.

```
Example / 示例:
Completed ROLL OUT:
  Sold: QQQ 2026-12-18 620C × 1
  Bought: QQQ 2028-01-15 620C × 1, price $185.00
```

---

## Project Structure / 项目结构

```
├── .github/workflows/monitor.yml   # GitHub Actions cron job
├── config/
│   ├── positions.yaml              # Holdings + cash (update after each trade)
│   └── settings.yaml              # Strategy thresholds
├── src/
│   ├── bs_model.py                 # Black-Scholes pricing model
│   ├── data_fetcher.py             # QQQ price + option IV (yfinance)
│   ├── signal_engine.py            # Signal evaluation logic
│   ├── lark_notifier.py            # Feishu card builder + sender
│   ├── state_store.py              # SQLite: cooldown + dedup
│   └── main.py                     # Entry point (--dry-run / --force)
├── PRD.md                          # Full product spec (Chinese)
└── requirements.txt
```

---

## Backtesting Results / 回测结果

| Period | Strategy CAGR | QQQ Buy & Hold |
|--------|--------------|----------------|
| 2021–2026 (5yr) | **48.4%** | 17.4% |
| 2026 YTD ($50k) | **+53.5%** | +17.2% |

> ⚠️ Backtest uses Black-Scholes with realized volatility. Actual results will differ due to IV premium, bid-ask spreads, and taxes. Maximum drawdown in backtest: **-72.6%**.

---

## Technical Notes / 技术说明

**IV Estimation:** yfinance IV for far-dated deep ITM LEAPS is systematically ~6% higher than market. The system blends yfinance IV (70%) with 20-day realized volatility HV20 (30%), reducing Delta error to < 0.5%.

**State persistence:** `logs/state.db` (SQLite) is preserved across GitHub Actions runs via `actions/cache`, maintaining BEAR_ADD cooldown tracking and push deduplication.

**Signal prices are estimates only.** Always use limit orders at market mid-price for actual execution.

---

## Requirements / 依赖

```
yfinance>=0.2.40
pandas>=2.0.0
numpy>=1.24.0
scipy>=1.10.0
requests>=2.31.0
pyyaml>=6.0
python-dotenv>=1.0.0
```

---

## License

MIT
