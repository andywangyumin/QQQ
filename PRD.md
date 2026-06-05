# PRD：QQQ LEAPS Call 无限续杯自动化监控系统

**文档版本**：v5.0  
**最后更新**：2026-06-04  
**核心标的**：仅 QQQ（纳斯达克 100 指数 ETF）

---

## 一、策略核心逻辑

### 1.1 基本假设

> 本策略建立在**一个且唯一一个假设**上：  
> **美股大科技（纳斯达克）会继续像历史一样长期看涨。**  
> 如果不认可这个假设，该策略不适用。

### 1.2 仓位结构（黄金比例）

```
总资金 = 60% LEAPS Call 仓位 + 40% 现金储备

现金储备的核心作用：
  - 熊市抄底加仓（绝不乱动）
  - 为快到期合约续杯（Roll Out）提供资金
  - 最低保留 10%（低于此绝对不动）
```

### 1.3 建仓参数（唯一标的：QQQ）

| 参数 | 具体值 | 说明 |
|------|--------|------|
| 标的 | **QQQ only** | 不做个股，不做 TQQQ |
| 到期日 | **650 ~ 800 天**（约 2 年后） | 避免时间价值快速衰减 |
| Delta | **0.8 左右** | 深度实值，模拟 80% 持股效果 |
| 建仓时机 | **QQQ 当日下跌 ≥ 1%** | 微跌时入场，降低成本 |

---

## 二、三大核心操作信号

策略只有三种市场状态，对应三套操作：

```
市场状态
    │
    ├─── 🟢 稳步上涨  ──→  【收割利润】Roll Up & Out
    │
    ├─── 🟡 横盘震荡  ──→  【无限续杯】Roll Out
    │
    └─── 🔴 暴跌崩盘  ──→  【逆势狙击】加仓买入
```

---

### 信号一：🟢 收割利润（Roll Up & Out）HARVEST

**触发条件**：
```
持仓中任意一张合约的 Delta ≥ 0.9
```

**操作动作**：
1. **卖出**当前 Delta≥0.9 的贵合约（高价卖出）
2. **买入**一张新合约：
   - 行权价**更高**（Delta 降回 ~0.7）
   - 到期日**更远**（重新回到 650~800 天）
3. 卖出收入 - 买入成本 = **套现的现金利润**（归入现金仓位）

**经济意义**：每次 HARVEST 产生净收益，累积的净收益逐步摊低持仓成本基础，最终达成"零成本持仓"里程碑（见北极星指标）。

---

### 信号二：🟡 无限续杯（Roll Out）ROLL_OUT

**触发条件**：
```
任意一张合约的 DTE（剩余天数）< 300 天
且该合约 Delta < 0.9（未处于收割状态）
且该合约未设置 exempt_rollout 豁免标记
```

**操作动作**：
1. **卖出**当前快到期合约
2. **买入**新合约：
   - 行权价**相同**（维持原有方向）
   - 到期日**延伸至 700 天以后**（重回 2 年期）
3. 此操作通常需要**净支出一定现金**（Roll Debit），从现金仓位扣除

**豁免续杯（exempt_rollout）**：部分合约可在 `positions.yaml` 中标记 `exempt_rollout: true`，跳过 ROLL_OUT 信号，仅等待 Delta ≥ 0.9 触发 HARVEST。适用于近期 HARVEST 窗口更优的合约。

**注意**：就算在亏损状态下也必须执行，"买时间"是第一要务。现金 < 10% 时阻断执行。

---

### 信号三：🔴 逆势狙击（熊市加仓）BEAR_ADD

**触发条件**（需同时满足）：
```
条件 A：任意一张持仓合约的 Delta < 0.5
条件 B：现金仓位 > 10%（总账户价值的百分比）
条件 C：距上次加仓已过 30 天冷却期
```

**加仓模式判断**：

| 当前现金仓位 | 模式 | 每次加仓金额 |
|-------------|------|-------------|
| ≥ 40% 总账户 | 🔴 重炮模式 | 动用 **10% 总账户价值** |
| 10% ~ 40% 总账户 | 🟠 标准模式 | 动用 **5% 总账户价值** |
| < 10% 总账户 | ❌ 禁止加仓 | 保留现金用于续杯 |

**加仓合约参数**：到期 650~800 天，Delta ~0.8，深度实值 QQQ Call

---

## 三、信号优先级与阻断通知

```
1. 现金 < 10%              → 阻断净支出操作，发出 BLOCKED 通知
2. DTE < 300（ROLL_OUT）   → 最高优先，保命续杯（豁免合约跳过）
3. Delta ≥ 0.9（HARVEST）  → 收割利润
4. Delta < 0.5（BEAR_ADD） → 逆势加仓
5. 以上均不满足            → HOLD，无操作
```

### 完整信号类型（7 种）

| 信号 | 触发条件 | 推送颜色 | 说明 |
|------|---------|---------|------|
| `HARVEST` | Delta ≥ 0.90 | 🟢 绿色 | 收割利润，有操作指令 |
| `ROLL_OUT` | DTE < 300 且 Delta < 0.90 | 🟡 琥珀 | 续杯换期，有操作指令 |
| `ROLL_OUT_BLOCKED` | 同上但现金 < 10% | 🔴 红色 | 需续杯但现金不足，通知用户等待 |
| `BEAR_ADD` | Delta < 0.50，现金充足，无冷却 | 🔴 红色 | 逆势加仓，有操作指令 |
| `BEAR_ADD_BLOCKED` | Delta < 0.50 但现金 < 10% | 🟡 琥珀 | 加仓条件满足但现金不足 |
| `BEAR_ADD_COOLDOWN` | Delta < 0.50 但在冷却期内 | 🟡 琥珀 | 加仓冷却中，告知剩余天数 |
| `HOLD` | 以上均不满足 | ⚪ 灰色 | 无操作，继续持仓 |

**BLOCKED 通知机制**：现金 < 10% 时，ROLL_OUT 和 BEAR_ADD 均不会静默跳过，而是显式推送阻断通知，用户可知晓并等待现金回升后再操作。

---

## 四、北极星指标：零成本达成率

### 4.1 概念

每次 HARVEST 产生净收益（旧合约高价卖出 − 新合约低价买入）。当累计净收益覆盖历史全部期权投入时，实现**零成本持仓**里程碑——此后头寸等同于免费持有。

```
有效持仓成本 = 历史期权总投入 − 累计收割净收益

零成本达成率 = 累计收割净收益 / 历史期权总投入 × 100%
```

| 阶段 | 达成率 | 意义 |
|------|--------|------|
| 起步 | 0% | 尚未执行任何 HARVEST |
| 回本中 | 1–99% | 每次 HARVEST 推进进度 |
| 零成本 | ≥ 100% | 当前持仓完全由收益支撑 |

### 4.2 数据追踪

- **历史期权总投入**（分母）= `initial_option_cost`（positions.yaml 手动设定）+ 系统自动累加每次 BEAR_ADD / ROLL_OUT 的净支出
- **累计收割净收益**（分子）= 每次 HARVEST 推送成功后自动写入 `cost_tracking_log` 表

### 4.3 卡片展示

```
💰 零成本进度  ████░░░░░░  40.3%  · 已收割 $8,200 / 投入 $28,292  · 还差 $20,092
```

达成后变为：
```
🎉 零成本持仓已达成！累计收割 $30,000
```

---

## 五、系统不做的事

| ❌ 不做 | 原因 |
|---------|------|
| 不止损 | 期权有无限续杯保护，割肉就永久锁定亏损 |
| 不预测涨跌 | 系统只根据 Delta 和 DTE 机械执行 |
| 不做个股 LEAPS | 个股可能长期不回来甚至退市 |
| 不做 TQQQ | TQQQ 下跌是线性三倍，无 Gamma 保护 |
| 不在现金 < 10% 时操作 | 必须保留续杯资金 |

---

## 六、策略风险警示

**1. 执行压力**：熊市账户大幅缩水时，策略要求逆势加仓，克服恐惧机械执行是最高门槛。回测显示最大回撤可达 -72.6%。

**2. 税务拖累**：每次 HARVEST 都是卖出事件，持有不足一年产生短期资本利得税。建议使用 Roth IRA / IRA 操作。

**3. 滑点损耗**：深度实值期权流动性较差，买卖价差大。**永远使用限价单，挂中间价，耐心等待成交，切勿使用市价单。**

**4. 模型估价偏差**：系统使用 Black-Scholes 理论定价，与实际市价存在约 2–5% 偏差，信号判断（Delta 阈值）不受影响，实际执行以市场报价为准。

---

## 七、回测验证结果（2021-2026，$100k 起始）

| 指标 | 策略 | QQQ 持股 |
|------|------|---------|
| 最终价值 | $717,600 | $284,600 |
| 年化收益 CAGR | **48.4%** | 17.4% |
| 最大回撤 | **-72.6%** | -35.1% |
| HARVEST 次数 | 105 次 | — |
| BEAR_ADD 次数 | 17 次 | — |
| ROLL_OUT 次数 | 4 次 | — |

**合理预期（税后 + 摩擦成本）**：

| 账户类型 | 调整后年化 |
|---------|-----------|
| 税优账户（IRA/Roth IRA） | 25–30% |
| 普通账户 | 18–22% |

---

## 八、实现架构（已上线）

### 8.1 文件结构

```
monitor/
├── .github/
│   └── workflows/
│       ├── monitor.yml           # GitHub Actions：UTC 09:00 prepare + sleep + UTC 10:00 notify
│       └── iv_refresh.yml        # GitHub Actions：UTC 20:30 收盘后自动抓取 IV → state.db
├── config/
│   ├── positions.yaml            # 持仓 + 现金 + initial_option_cost（每次操作后更新）
│   └── settings.yaml             # 策略参数（阈值、比例等）
├── src/
│   ├── main.py                   # 主入口（--prepare / --notify / --dry-run / --force）
│   ├── iv_refresh.py             # 独立 IV 抓取脚本（收盘后运行，写入 option_iv_cache）
│   ├── bs_model.py               # Black-Scholes 定价模型（Delta、Gamma、价格）
│   ├── data_fetcher.py           # QQQ 行情 + 期权 IV（yfinance）
│   ├── signal_engine.py          # 7 类信号判断引擎（含 BLOCKED / COOLDOWN 阻断通知）
│   ├── state_store.py            # SQLite：冷却期 + 推送去重 + 零成本追踪 + IV 缓存 + 日报卡片
│   ├── history_store.py          # SQLite：QQQ 历史价格（market_history.db）
│   ├── chart_generator.py        # QQQ 6 个月趋势图（DB 数据不足时自动从 yfinance 回填）
│   ├── card_renderer.py          # 图片日报：React/JSX + Playwright（--no-sandbox for CI）
│   ├── feishu_uploader.py        # 飞书图片上传（tenant_access_token → img_key）
│   ├── lark_notifier.py          # 飞书卡片构建 + 推送（图片优先，文字降级）
│   └── snapshot.py               # 手动触发：资产快照推送
├── templates/
│   └── report_card.html          # React/JSX 日报模板（Playwright 截图用）
├── charts/                       # 趋势图 + 日报 PNG（.gitignore 排除，运行时生成）
├── logs/                         # state.db + market_history.db（.gitignore 排除）
├── requirements.txt
├── .env.example
└── .gitignore
```

### 8.2 技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| 语言 | Python 3.11 | GitHub Actions ubuntu-latest 默认 |
| 股价数据 | yfinance | 免费，无需注册 |
| 期权 Greeks | Black-Scholes + IV 优先链 | 自行计算，无需 Tradier |
| IV 来源 | iv_override → DB 缓存 → yfinance 期权链 → HV20 | 四级优先链，确保准确 Delta |
| 历史价格 | SQLite `market_history.db`（QQQ 日线） | 用于趋势图；每日自动追加；DB 数据不足时自动回填 |
| 趋势图 | matplotlib（Agg 后端）→ 飞书图床 | 6 个月价格 + 操作标注 |
| 飞书图片 | 自定义应用 API（App ID + Secret → img_key） | `LARK_APP_ID` / `LARK_APP_SECRET` |
| 日报图片 | React/JSX + Playwright（--no-sandbox）→ PNG → 飞书图床 | 图片优先推送；失败自动降级文字卡片 |
| 持仓状态 | SQLite `state.db`（冷却期 + 去重 + 零成本 + IV 缓存 + 日报卡片） | Artifact（90 天备份） |
| 推送 | 飞书 Lark Webhook（支持多个，逗号分隔） | `LARK_WEBHOOK_URLS` |
| 定时任务 | monitor.yml：UTC 09:00 prepare，UTC 10:00 notify；iv_refresh.yml：UTC 20:30 | 周一至周五自动运行 |
| 代码托管 | GitHub 私有仓库 | https://github.com/andywangyumin/QQQ |

### 8.3 IV 估算说明

深度实值 QQQ LEAPS 的真实 IV 通常在 30%+ 水平，但以下几个因素导致直接使用 yfinance 或 HV20 均不可靠：

- **yfinance 盘前返回 IV=0**：监控任务在盘前运行（UTC 09:00），对远月深度实值合约，yfinance 期权链 IV 此时经常返回 0，无法使用。
- **HV20 严重低估真实 IV**：QQQ 20 日历史波动率约 16%，远低于 LEAPS 的实际隐含波动率（~30%+）。若直接使用 HV20 计算 Delta，会导致 Delta 明显高估（例如 HV20→Delta=0.96，实际市场 Delta≈0.85），产生错误的 HARVEST 信号。

**IV 优先链（从高到低）**：

```
1. iv_override（positions.yaml 手动字段）
   └── 用户从 moomoo 读取当日 IV 手动填入，最准确

2. DB 缓存（option_iv_cache 表，28h TTL）
   └── iv_refresh.py 在收盘后（UTC 20:30）自动抓取 yfinance IV 并写入
   └── 次日盘前 prepare 阶段直接读取，避免 IV=0 问题

3. yfinance 实时期权链 IV
   └── 仅在市场交易时段有效；盘前通常返回 0，作为兜底

4. HV20 fallback
   └── 最后备选，仅在以上全部失败时使用
```

**注**：信号判断以 Delta 为准，价格仅参考，实际执行以市场限价单为准。

### 8.4 策略参数（settings.yaml）

| 参数 | 值 | 说明 |
|------|-----|------|
| `target_dte` | 700 天 | 买入新合约目标剩余天数 |
| `delta_harvest` | 0.90 | HARVEST 触发 |
| `delta_harvest_new` | 0.70 | HARVEST 换仓目标 Delta |
| `dte_rollout` | 300 天 | ROLL_OUT 触发 |
| `delta_bear` | 0.50 | BEAR_ADD 触发 |
| `min_cash_pct` | 10% | 现金安全线（低于此阻断操作）|
| `heavy_cash_pct` | 40% | 重炮模式触发门槛 |
| `heavy_alloc` | 10% | 重炮模式每次动用比例（基于总资产）|
| `standard_alloc` | 5% | 标准模式每次动用比例（基于总资产）|
| `cooldown_days` | 30 天 | BEAR_ADD 冷却期 |
| `risk_free_rate` | 4% | 无风险利率（BS 模型用）|

---

## 九、飞书日报推送形式

### 9.1 图片日报（优先）

每日推送为 **PNG 图片卡片**（Playwright 截图 React/JSX 模板），包含：

```
┌─────────────────────────────────────────────┐
│ QQQ LEAPS · DAILY REPORT                    │
│ 2026-06-04 周四         [HOLD · 今日无操作]   │
├──────────────────────┬──────────────────────┤
│ 总资产 $100,369        │ QQQ $480.00          │
│ +$369 · +0.37% vs 基准│ +0.42% 较前日         │
├─────────────────────────────────────────────┤
│ [QQQ 6 个月走势图，含 ▲ HARVEST ▼ BEAR_ADD]   │
├─────────────────────────────────────────────┤
│ 仓位配置                                      │
│ 期权 ██████░░  X%  ·  现金 ░░██  X%          │
├─────────────────────────────────────────────┤
│ 零成本进度  ░░░░░░░░░░  0.0%                  │
│ 已收割 $0 / 投入 $28,292 · 还差 $28,292       │
├─────────────────────────────────────────────┤
│ 持仓 · 4 张                    无需操作       │
│ QQQ_261218_620C  [豁免续杯]    Delta 0.632    │
│ QQQ_261218_630C  [豁免续杯]    Delta 0.601    │
│ QQQ_270331_705C  [HOLD]        Delta 0.712    │
│ QQQ_270331_730C  [豁免续杯]    Delta 0.685    │
└─────────────────────────────────────────────┘
```

**图片状态 Pill 颜色对照**：

| 信号 | Pill 颜色 | 持仓节区副标题 |
|------|----------|--------------|
| HARVEST | 绿色 | ⚠ 需执行操作 |
| ROLL_OUT | 琥珀色 | ⚠ 需执行操作 |
| ROLL_OUT_BLOCKED | 红色 | ⚠ 续杯受阻，等待现金回升 |
| BEAR_ADD | 红色 | ⚠ 需执行操作 |
| BEAR_ADD_BLOCKED | 琥珀色 | ⚠ 加仓受阻，等待现金回升 |
| BEAR_ADD_COOLDOWN | 琥珀色 | ⏳ 加仓冷却中 |
| HOLD | 灰色 | 无需操作 |

### 9.2 文字卡片（降级 fallback）

当 Playwright / 飞书图床上传失败时，自动降级为结构化飞书文字卡片，内容相同，格式为飞书 interactive card（Markdown 列宽布局）。

### 9.3 操作指令格式（有信号时）

- ① 卖出限价单：合约详情 + 参考价
- ② 买入限价单：合约详情 + 参考价
- ③ 预估成本 / 净收益
- ④ 操作完成后告知 AI 更新配置

---

## 十、当前持仓状态（基准日：2026-05-24）

**总资产基准**：$100,000（设定于 2026-05-24）  
**现金**：$58,365  
**历史期权总投入基准**：$28,292（四张合约建仓成本之和）  
**bs_anchor_options**：$42,004

| 合约 ID | 行权价 | 到期日 | 张数 | 买入均价/份 | DTE（≈2026-06-04）| 豁免续杯 |
|---------|--------|--------|------|-----------|-------------------|---------|
| QQQ_261218_620C | $620 | 2026-12-18 | 1 | $63.00 | ≈196 | ✅ 是 |
| QQQ_261218_630C | $630 | 2026-12-18 | 1 | $64.29 | ≈196 | ✅ 是 |
| QQQ_270331_705C | $705 | 2027-03-31 | 1 | $76.23 | ≈299 | — |
| QQQ_270331_730C | $730 | 2027-03-31 | 1 | $79.40 | ≈299 | ✅ 是（建仓时 DTE=299）|

**620C / 630C 豁免说明**：DTE 虽已 < 300，但两张合约设置了 `exempt_rollout: true`，系统跳过 ROLL_OUT，仅等待 Delta ≥ 0.90 触发 HARVEST。

**730C 豁免说明**：2026-06-04 追加买入，建仓时 DTE=299，直接设置 `exempt_rollout: true`，跳过 ROLL_OUT，等待 HARVEST 窗口。

---

## 十一、日常操作工作流

### 自动运行（无需手动）

每个工作日，GitHub Actions 按如下时序自动执行：

1. **UTC 09:00（北京 17:00）**：从 Artifact 恢复 `logs/state.db`
2. 从 yfinance 拉取 QQQ 最新收盘价和 HV20
3. 按优先链读取各持仓 IV：`iv_override` → DB 缓存（`option_iv_cache`）→ yfinance 期权链 → HV20
4. 通过 Black-Scholes 计算各持仓 Greeks（Delta、价格、DTE）
5. 判断全部 7 类信号（含 BLOCKED / COOLDOWN 阻断通知）
6. 生成 QQQ 6 个月趋势图（DB 数据不足时自动从 yfinance 回填历史数据，自愈）
7. Playwright 渲染 React/JSX 日报模板 → PNG（使用 `--no-sandbox` 适配 CI 环境）→ 上传飞书图床
8. 将日报卡片（含信号、图片、持仓数据）保存至 `state.db` 的 `daily_card` 表
9. **Sleep 至 UTC 10:00（北京 18:00）**
10. 从 `state.db` 读取已准备好的日报卡片，推送至飞书 Webhook（`workflow_dispatch` 手动触发时跳过 sleep，立即推送）
11. 将更新后的 `state.db` 上传为 Artifact（90 天备份）

**每个工作日 UTC 20:30（北京次日 04:30）**：`iv_refresh.yml` 单独运行，在美市收盘后从 yfinance 抓取最新 IV，写入 `state.db` 的 `option_iv_cache` 表，供次日 prepare 阶段使用。

### 执行操作后（唯一需要手动的步骤）
在券商完成操作后，将以下信息告知 AI：
```
操作类型：ROLL_OUT / HARVEST / BEAR_ADD
卖出：合约名称、张数
买入：行权价、到期日、张数、买入均价（期权报价，非总成本）
```
AI 自动更新 `positions.yaml`，commit 并 push 到 GitHub，次日起生效。

### 快照推送（手动）
```bash
cd monitor/src
python snapshot.py
```
推送当前完整资产快照（不受定时任务限制，随时可触发）。

---

## 十二、已完成开发里程碑

- [x] **回测验证**：backtest.py，5 年 Black-Scholes 模拟，CAGR 48.4%
- [x] **PDF 评估报告**：generate_report.py，14 页图文报告
- [x] **YTD 回放模拟**：simulate_ytd.py，基于实际合约的 2026 年信号回放
- [x] **组合模拟**：simulate_portfolio.py，$50k 起始完整交易模拟 vs QQQ 对比
- [x] **MVP 监控系统**：全套 Python 模块，dry-run 验证通过
- [x] **飞书推送**：Webhook 推送，卡片含操作指令 + 资产盘点
- [x] **GitHub Actions 部署**：UTC 10:00 自动运行，DBs 通过 Artifact 持久化
- [x] **$100k 基准设定**：2026-05-24 起，持续追踪 vs 基准盈亏
- [x] **北极星指标**：零成本达成率，cost_tracking_log 自动追踪，卡片进度条展示
- [x] **豁免续杯**：exempt_rollout 字段，620C / 630C / 730C 跳过 ROLL_OUT 仅等待 HARVEST
- [x] **历史价格数据库**：history_store.py，QQQ 日线入库
- [x] **QQQ 趋势图**：chart_generator.py，6 个月价格 + 操作标注，每日自动生成
- [x] **飞书图床上传**：feishu_uploader.py，自定义应用 API，卡片顶部展示趋势图
- [x] **卡片 UI 优化**：weighted 列宽（QQQ 行 1:1，资产总览 1:1:1，持仓行对齐）
- [x] **图片日报**：React/JSX 模板 + Playwright 截图 → PNG → 飞书图床，含 KPI、仓位图、趋势图、零成本进度、逐仓明细
- [x] **全信号覆盖**：7 类信号（HARVEST / ROLL_OUT / ROLL_OUT_BLOCKED / BEAR_ADD / BEAR_ADD_BLOCKED / BEAR_ADD_COOLDOWN / HOLD）在图片和文字两套卡片中均完整展示
- [x] **BLOCKED 通知机制**：现金不足时不再静默跳过，ROLL_OUT_BLOCKED / BEAR_ADD_BLOCKED 显式推送阻断原因和等待指引
- [x] **state.db Artifact 备份**：Artifact 90 天保留，防容器重置导致冷却期/零成本记录丢失
- [x] **准确 IV 估算**：iv_override 手动字段 + iv_refresh 自动收盘后更新，消除 yfinance 盘前 IV=0 的问题
- [x] **推送时序分离**：prepare（UTC 09:00 渲染图片）+ sleep + notify（UTC 10:00 推送），数据尽早准备，推送时间精确到整点
- [x] **history_store 自愈**：GitHub Actions 新容器检测到 DB 数据不足时自动从 yfinance 回填 6 个月历史，趋势图始终正常
- [x] **第4张持仓**：2026-06-04 追加买入 QQQ_270331_730C（$79.40，1张），系统自动更新锚点和零成本基数
