# Polymarket 波动做空套利机器人

基于 [plan.md](plan.md) 的完整落地实现。面向 **Polymarket「马斯克周发帖数（区间）」预测市场**，在发帖量爆发结束后的**安静期**，利用公众对事件的过度反应做空过热区间（买 No），并辅以受害者区间（买 Yes）放大与上档便宜 Yes 对冲，构成三腿组合。

> 低频策略，天然避开 HFT 机器人竞争：只在「爆发→安静确认→临近结算仍有大过热度」的窄窗口入场。

---

## 策略要点（详见 plan.md）

1. **过热度 x**：安静期内热门区间累计涨幅（只算安静窗口内、不计爆发内的信息涨幅）。
2. **回落概率** `P_success(x,H) = exp(-k·μ_quiet·H) × F_{t,ν}((x(1-2^{-H/τ}) − δ)/(σ_s·√(1−4^{-H/τ})))`
   - 第一项显式惩罚「持有期内再次爆发」；第二项用 Student-t 承认肥尾、提高入场门槛。
3. **四硬门控**：日历 / 安静 / 催化剂 / 流动性，任一不满足即不入场。
4. **三腿组合**：
   | 腿 | 工具 | 角色 | 预算 |
   |:--|:--|:--|:--:|
   | 1 核心 | 热门区间 **No** | 做空过热 | 100% R |
   | 2 放大 | 受害者区间 **Yes** | 做多被错压反弹 | 60% R（无合格者跳过） |
   | 3 对冲 | 热门上方 1–2 档便宜 **Yes** (<15¢) | 新爆发尾部保险 | 12% R |
5. **风控**：单笔风险 ≤ 2% 权益、并发持仓上限、低资金强停。

---

## 目录结构

```
polymarket_arb/
├── config.py          # 全部参数（dataclass，月度校准）
├── probability.py     # 回落概率建模（plan 2.1）
├── main.py            # 主循环（5 分钟轮询）+ 离线 demo
├── data/              # 发帖速率、CLOB/Gamma、事件日历、市场模型
├── features/          # 爆发/安静检测、过热度 x、市场快照
├── strategy/          # 四门控、概率信号、三腿组合
├── execution/         # 下单（dry-run/真实）、风险状态机
├── storage/           # sqlite 持久化
├── alerts/            # Telegram 告警
├── calibration/       # 月度校准（μ_quiet、ν、分档复核）
└── utils/             # 日志
tests/                 # 单元测试（18 项）
scripts/               # backtest.py、calibrate.py
data/                  # posts.csv、events.csv 示例
```

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 离线演示（mock 数据，不下单）
python -m polymarket_arb --demo
# 或
bash run.sh demo

# 离线回测（输出一次可入场的三腿组合示例）
bash run.sh backtest

# 单元测试
bash run.sh test

# 月度校准
bash run.sh calibrate --price-deltas 1,2,-0.5,3,...
```

---

## 真实部署

1. 复制 `env` 配置：
   ```bash
   cp .env.example .env
   ```
2. 填写 `.env`：
   - `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_WALLET_ADDRESS`（真实下单必需）
   - `MARKET_SLUG`：目标市场
   - `POSTING_DATA_SOURCE`：`mock`（测试）或 `csv`（发帖时间戳文件）
   - `DRY_RUN=false`：关闭模拟，真实下单（`py-clob-client`）
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：告警（可空）
3. 启动持续运行：
   ```bash
   DRY_RUN=true python -m polymarket_arb --run
   ```

> 生产务必先以 `DRY_RUN=true` 观察信号质量，再做小资金实盘验证。

---

## 说明与边界

- **参数须月度校准**（见 `calibration/`）：μ_quiet、ν 由安静期数据重估；并用历史信号分档复核实际成功率是否单调接近预测。
- 默认参数（μ_quiet=0.028/h、k=1.5）较保守，导致入场窗口很窄（临近结算、x 需较大），符合「低频、只抓极端过度反应」的定位。
- 真实数据层的发帖速率来自 `POSTING_DATA_SOURCE`；如接入自有 X/Mastodon 采集，仅需实现 `PostingRateProvider` 子类。