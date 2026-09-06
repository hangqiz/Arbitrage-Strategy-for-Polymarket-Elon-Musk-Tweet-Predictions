# Polymarket 波动做空套利机器人

基于 [plan.md](plan.md) 的落地实现。面向 **Polymarket「马斯克周发帖数（区间）」预测市场**，在发帖量爆发过后的**安静期与回落期**，利用公众对事件的过度反应 **做空过热带（买 No）**，并在情绪降温、价格进入平台时**提前止盈**，滚动多次吃利、规避「买入区间最终获胜」的巨额尾部风险。

> 低频策略，天然避开 HFT 机器人竞争：只在「爆发 → 安静/回落 → 情绪见顶」的窄窗口入场。
> 当前为 dry-run / 回测实现；真实下单需配置钱包（`py-clob-client`）。

---

## 策略要点（详见 plan.md）

1. **过热度 x**：安静期内热门区间累计涨幅（只算安静窗口内、不计爆发内的信息涨幅）。
2. **回落概率** `P_success(x,H) = exp(−k·μ_quiet·H) × F_{t,ν}((x(1−2^{-H/τ}) − δ)/(σ_s·√(1−4^{-H/τ})))`
   - 第一项显式惩罚「持有期内再次爆发」；第二项用 Student-t 承认肥尾、提高入场门槛。
3. **入场门控**（叠加，非互斥）：
   - 日历门：市场开始第 N 天之后 + 结算前缓冲
   - 收盘前 `no_entry_within_h=12h` 停止入场（结算前大部分区间价格持续下行，不利做空）
   - 安静确认（有安静锚定价）
   - 过热度 `x > 0`
   - 回落概率 `P_success ≥ p_min`
   - **统一 froth 信号**：热门「慢趋势」或其上冷门「剧烈飙升」，满足「窗口内涨幅 ≥ `min_rise_c` 且已见顶回落」即视为过热窗口，并连续确认（兼容慢爬/快涨两种节奏；瞬态尖峰会自行回落、`amp≈0`，自动被滤除）
4. **腿1 核心：做空「热门 + 其上档」一篮子 No**
   - `leg1_top_k=4`：热门 + 其上 3 档
   - 预算 **按量加权**（`leg1_weight_by_price=True`，价格为量/流动性代理）→ 热门分得最多
   - 分散单区间「最终获胜」的尾部风险（结算时赢家赔 0、其余赔 100）
5. **滚动出场（回测引擎 v2）**：
   - **止盈**：篮子加权 No 已上涨 ≥ `rise_min_c` 且进入**短平台/停滞**（快平台窗口，抢在二次飙升前）→ 平仓；未触发平台则持到结算
   - **止损默认关闭**：回调前必有急杀，止损会伤在坑底；`sl_enabled=True` 可开确认式止损
   - 持仓中若出现**人为尖峰**（单分钟 ±跳变 ≥ `spike_jump_c`）不平仓，避免按人造报价成交
6. **风控**：单笔风险 ≤ 2% 权益、并发持仓上限、低资金强停；止盈后再入场（`max_roundtrips`）控制重仓。

> 腿2（受害者 Yes 放大）与腿3（对冲）已弱化：腿3 默认关闭，腿2 默认 30%R。当前主线聚焦腿1。

---

## 目录结构

```
polymarket_arb/
├── config.py          # 全部参数（dataclass，月度校准）
├── probability.py     # 回落概率建模（plan 2.1）
├── main.py            # 主循环（1 分钟轮询）+ 离线 demo
├── data/              # 发帖速率、CLOB/Gamma、事件日历、市场模型
├── features/          # 爆发/安静检测、过热度 x、市场快照
├── strategy/          # 信号、腿组合（legs）
├── execution/         # 下单（dry-run/真实）、风险状态机
├── storage/           # sqlite 持久化
├── alerts/            # Telegram 告警
├── calibration/       # 月度校准（μ_quiet、ν、分档复核）
├── backtest/          # 回测：loader(数据加载/1分钟重采样) + engine(回放) + result
└── utils/             # 日志
tests/                 # 单元测试（19 项）
scripts/               # run_backtest.py、sweep、calibrate.py
data/                  # posts.csv、events.csv 示例、state.db
results/               # 回测输出（backtest.json）
```

---

## 快速开始（venv）

```bash
# 创建并激活虚拟环境（已配 requirements*.txt）
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # 运行时
pip install -r requirements-dev.txt    # 含 pytest

# 离线演示（mock 数据，不下单）
python -m polymarket_arb --demo

# 单元测试
pytest tests/ -q

# 真实历史回测（需将 tweets.db 放回项目根目录，数据库过大不入库）
python scripts/run_backtest.py --poll-sec 60 --out results/backtest.json
# 只回测部分事件
python scripts/run_backtest.py --poll-sec 60 --slugs "february-20,february-24"
# 关键参数灵敏度扫描
python scripts/run_backtest.py --poll-sec 60 --sweep
```

> IDE 中请选择 `.venv/bin/python` 作为解释器，消除第三方库解析波浪线。

---

## 回测说明（现状与边界）

基于 `tweets.db`（`events` / `history_pricing` / `tweets`）回放每条周事件，1 分钟重采样逐步触发信号并结算。

**已知边界（务必过目）：**
- **价格数据**：当前仅 `history_pricing.prob`（pricer 模型输出，非真实盘口），用其作中间价、无滑点/深度/价差 —— 真实 CL 值需接入真实订单簿后再验证。
- **样本很小**：131 个周事件中仅约 19 个可完整回测（需同时具备时间窗与定价行），回测触发的交易数常在个位数 —— 结论属**诊断而非实证**。
- **模型概率偏乐观**：模型 `P_success≈0.6` 高于实测胜率，入场前宜对结果打折看。
- **成本模型**：当前为 `1¢/份 × 份数` 的简化摩擦；对低价冷门 token 的份数/费用会被高估。
- **催化剂门、流动性门（max_spread）尚未在回测引擎内强制执行**，待优质数据含真实盘口/价差/成交量后补齐。
- `tweets.db` 因体积过大不入 git；回测前请自行放回项目根目录。

---

## 参数校准

- 见 `calibration/`：μ_quiet、ν 由安静期数据重估；并用历史信号分档复核实际成功率是否单调接近预测。
- 核心可调旋钮集中在 `RoundTripParams`（入场 froth、止盈平台、止损开关）与 `ProbabilityParams`，**月度复核，勿临时改**。

---

## 真实部署

1. 复制配置：`cp .env.example .env`
2. 填写 `.env`：
   - `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_WALLET_ADDRESS`（真实下单必需）
   - `MARKET_SLUG`：目标市场
   - `POSTING_DATA_SOURCE`：`mock`（测试）或 `csv`（发帖时间戳文件）
   - `DRY_RUN=false`：关闭模拟，真实下单（`py-clob-client`）
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：告警（可空）
3. 启动：`DRY_RUN=true python -m polymarket_arb --run`

> 生产务必先以 `DRY_RUN=true` 观察信号质量，再做小资金实盘验证。