# Quantify：美股交易信号研究工具

扫描纳斯达克 100，按汇丰 Trade25 的真实收费（免佣额度、超额佣金、月费、滑点）回测日内和波段策略。全部策略只做多。

## 回调观察台（每日模拟记录）

网页：**[lazuli111.github.io/Quantify](https://lazuli111.github.io/Quantify/)**

直接打开某个策略：[三连跌回调](https://lazuli111.github.io/Quantify/#pullback3) · [Martin Luk 风格回调](https://lazuli111.github.io/Quantify/#luk)

同时观察两个策略，各有一个独立的 1,300 美元模拟账户：
- **三连跌回调**（`swing_strategies.Pullback3`）：消除幸存者偏差后唯一通过检验的策略；
- **Martin Luk 风格回调**（`swing_strategies.LukPullback`，回调到 9 日 EMA 买、跌破 21 日 EMA 卖、止损不超过 4%）：回测证据很弱，用来观察。

GitHub Actions 每个交易日美股收盘后（UTC 23:00，美东晚上 7 点）自动运行 `uv run quantify paper`：
1. 用 Yahoo 日线按回测里同样的信号代码找信号；
2. 模拟交易：下一个交易日按开盘价成交，Luk 策略另有盘中止损，计入 0.05% 滑点和汇丰 Trade25 的费用与免佣额度；
3. 把各策略的账户状态存到 `paper/<策略>.json`，网页数据存到 `docs/data.json`，并发布到 GitHub Pages。

每次更新后会通过 Telegram 推送当晚的操作、上一个交易日的成交和账户现状（`uv run quantify notify`）；更新失败也会推送提醒。需要在 GitHub 仓库的 Secrets 里设置 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，本地测试则写在 `.env`。

也可以在 GitHub 仓库的 Actions 页面手动点 **Run workflow** 立即更新。在本地运行前，先 `git pull` 拿到最新记录，避免和自动运行冲突。网页源文件是 `src/quantify/dashboard.html`。

这是模拟记录，用来观察策略在真实行情中的表现，不是投资建议。

## 常用命令（在项目文件夹里运行）

```bash
uv run quantify download                 # 下载或更新 5 分钟 K 线（只补新的部分）
uv run quantify backtest                 # 按 config.yaml 回测日内策略，明细在 data/trades.csv
uv run quantify sweep all                # 日内策略参数检验（3 小时窗口 + 全天窗口）
uv run quantify noise-sweep              # QQQ 全天日内动量检验
uv run quantify swing-sweep all --pit    # 波段策略检验，用当时的成分股（消除幸存者偏差）
uv run quantify wide-sweep               # 全市场强势股回调买入检验（需要先下载全市场日线）
uv run quantify paper                    # 每日模拟更新（--reset 重新开始，--only luk 只针对一个策略）
```

数据来源在 `config.yaml` 的 `data.source` 里设置：`alpaca`（2016 年至今，需要在 `.env` 中填写 `ALPACA_API_KEY` 和 `ALPACA_SECRET_KEY`）或 `yahoo`（最近 60 天，不需要账户）。

## 检验方法

- **样本内/样本外**：2016–2022 年选参数，2023 年至今检验。
- **通过标准**（事先定好）：样本内最佳参数在样本外扣费后每笔为正；过半参数组合在样本外为正；样本外至少 100 笔（日内）或 50 笔（波段）。
- **消除幸存者偏差**（`--pit`）：样本内用 2016 年初的成分股，样本外用 2023 年初的成分股，名单来自维基百科历史版本，包括后来退市或被收购的公司。代码改名（FB→META、PCLN→BKNG）和代码被重新使用的情况已处理，个别公司行为做了人工修正（见 `swing.py` 的 `MANUAL_FIXES`）。

## 策略与结果（2026-10-14）

| 类型 | 策略 | 代号 | 结论 |
|---|---|---|---|
| 日内 | 开盘区间突破 | `orb` | 未通过（样本外 0/36 组为正） |
| 日内 | 热门股开盘跟进 | `in_play_orb` | 未通过 |
| 日内 | VWAP 回踩 | `vwap_pullback` | 未通过 |
| 日内 | 低开反转 | `gap_reversal` | 未通过（方向为正，但样本外只有 27 笔） |
| 日内全天 | 尾盘动量 | `late_momentum` | 未通过 |
| 日内全天 | QQQ 噪声区间突破（Zarattini 等，2024） | `noise-sweep` | 未通过 |
| 波段 | RSI(2) 超跌反弹 | `rsi2` | 消除幸存者偏差后未通过 |
| 波段 | **三连跌回调** | `pullback3` | **消除幸存者偏差后仍通过**，但统计证据偏弱（t≈1.4），且 2025–2026 年为负 |
| 波段 | 新高突破 | `donchian` | 消除幸存者偏差后亏损 |
| 波段 | 动量轮动 | `momentum` | 收益为正，但交易次数太少 |
| 参考 | QQQ 均线择时 | `qqq_trend` | 交易次数太少 |
| 波段 | 强势股回调买入（Martin Luk 风格近似） | `luk` | 技术上通过（样本外 +0.29%/笔，113 笔），但 t≈0.4，去掉最好 3 笔即转负，复利账户样本外 -2% |
| 波段 | 强势股回调买入（全市场，含已退市股票） | `wide-sweep` | 未通过：样本外每笔 -1.91%（t=-2.55），盈利集中在 2020–2021 年 |

## 参考资料

- 历史成分股：维基百科 Nasdaq-100 条目的历史版本；逐日成分股可参考 [jmccarrell/n100tickers](https://github.com/jmccarrell/n100tickers)
- QQQ 全天动量：Zarattini, Aziz & Barbon, *Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)*, 2024
- Martin Luk 的方法：只有二手资料，规则是近似还原，例如 [FinancialWisdom 的整理](https://www.financialwisdomtv.com/post/martin-luk-s-pullback-trading-strategy-how-the-2025-u-s-investing-champion-finds-low-risk-high-re)
- 回测框架清单：[awesome-quant](https://github.com/wilsonfreitas/awesome-quant)

## 待办

- [x] 每日收盘后生成信号，先只观察不交易（回调观察台：三连跌回调 + Martin Luk 风格回调）
- [ ] 实时扫描，Telegram 推送
- [ ] 网页仪表盘（含额度追踪）
