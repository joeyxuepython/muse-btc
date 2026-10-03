# 加密货币智能研判与早期预警系统 V3

## 0. 项目定位

开发一个长期运行的专业级加密货币 Intelligence / Monitoring / Research / Early Warning System。

系统不是普通行情看板，不是简单技术指标合集，也不是自动交易机器人。

第一阶段只做：

**数据采集 → 清洗 → 标准化 → 特征计算 → 异常检测 → 多源交叉验证 → 市场状态判断 → 机会发现 → 风险过滤 → 预警 → 事后验证 → 回测 → 动态优化。**

暂时不做：

- 自动下单
- 自动开仓
- 自动杠杆交易
- 自动资金调度

系统核心目标：

1. 判断 BTC 当前处于什么市场阶段。
2. 尽可能提前发现可能启动的山寨币。
3. 尽可能早发现潜在高倍 Meme 候选。
4. 区分真实买盘、杠杆推动、逼空、吸筹和出货。
5. 对所有信号进行历史验证。
6. 逐步形成自己的高价值历史数据库。
7. 最终建立真正具有 Leading Value 的信号体系，而不是事后解释市场。

---

# 1. 最高设计原则

系统必须始终遵守以下原则：

**Early > Late**

我们关心的是提前发现，不是涨完之后解释原因。

**Evidence > Narrative**

所有判断必须优先基于证据。

**Multiple Independent Signals > Single Indicator**

任何重要信号必须由多个独立类别数据交叉验证。

**Point-In-Time > Retrospective Explanation**

必须保存当时真正可获得的数据，禁止未来数据泄漏。

**Precision > Alert Quantity**

宁可少发，也不要大量垃圾预警。

**Risk Detection Before Opportunity Detection**

尤其是 Meme。

**Explainable > Black Box**

任何结论必须知道为什么。

**Backtest Everything**

没有经过历史验证的指标不能成为核心信号。

**Raw Data > Third-Party Scores**

优先保存原始数据，自行计算 Feature。

**Data Lineage**

任何结论都必须可以追溯到数据来源和计算逻辑。

---

# 2. 第一阶段数据源原则

第一阶段优先使用免费、公开、稳定的数据。

核心实时市场数据：

**Binance Public API**

包括：

- Binance Spot
- Binance Futures

第一阶段暂时不强制购买：

- Glassnode API
- Nansen API
- Arkham API
- Santiment API
- CryptoQuant API
- CoinGlass API

这些平台第一阶段使用：

- Newsletter
- Email
- 官方公开报告
- 官方公开 Research
- 公开网页
- 官方博客
- 官方 X 内容

架构必须预留：

Provider Adapter

但未启用的数据源明确标记：

DISABLED / OPTIONAL / NEEDS_VERIFICATION

禁止 Mock 数据伪装成真实数据。

---

# 3. 系统总体架构

系统拆分为以下九个核心层：

1. Global Market Regime Engine
2. BTC Intelligence Engine
3. Altcoin Early Warning Engine
4. Meme Early Discovery Engine
5. Wallet Intelligence Engine
6. Research / News / Social Intelligence Engine
7. Signal Fusion Engine
8. Backtest / Validation Engine
9. Data Infrastructure / Dashboard / Alert Layer

各模块不能独立运行。

最重要的联动逻辑：

**Global Regime → BTC Regime → Altcoin/Meme Risk Adjustment**

例如：

BTC 高杠杆风险上升时，

即使某个 Altcoin 局部信号很强，也需要降低最终机会评分。

当：

BTC 稳定

+

Stablecoin 流入

+

BTC Dominance 下降

+

Altcoin Breadth 扩张

时，

可以提高 Altcoin Risk-On 权重。

---

# 4. Global Market Regime Engine

目标：

判断整个加密市场目前处于什么阶段。

至少支持：

- RISK_OFF
- CAPITULATION
- DELEVERAGING
- ACCUMULATION
- EARLY_RISK_ON
- BTC_LEAD
- ETH_ROTATION
- ALT_EXPANSION
- MEME_MANIA
- LATE_BULL
- DISTRIBUTION
- EUPHORIA
- BEAR_MARKET

每次输出：

- regime
- confidence
- regime_start_time
- regime_duration
- previous_regime
- key_supporting_evidence
- contradictory_evidence
- invalidation_conditions

---

# 5. 宏观经济模块

关注：

- Fed Funds Rate
- FOMC
- CME FedWatch
- CPI
- Core CPI
- PCE
- Core PCE
- NFP
- Unemployment
- Initial Jobless Claims
- GDP
- ISM
- DXY
- US 2Y Yield
- US 10Y Yield
- 2Y/10Y Spread
- VIX
- MOVE
- Nasdaq
- S&P 500
- Gold

进一步关注美元流动性：

- Fed Balance Sheet
- TGA
- RRP
- Bank Reserves

宏观事件必须记录：

- actual
- consensus
- previous
- surprise
- release_time

并保存：

BTC 在事件发布后：

- 5m
- 15m
- 1h
- 4h
- 24h

的市场反应。

建立：

**Macro Event Reaction Database**

目的是研究：

不是某个数据“理论上利好还是利空”，

而是：

**市场实际上如何反应。**

---

# 6. BTC Intelligence Engine

BTC 是整个系统最核心的 Risk Engine。

输出至少包括：

- BTC Regime
- Bull Score
- Bear Score
- Liquidity Score
- Leverage Risk
- Spot Demand Score
- Derivatives Risk
- Macro Risk
- Research Bias
- Overall Confidence

禁止简单输出：

BUY / SELL。

---

# 7. Binance Spot 数据

第一阶段核心实时数据源。

采集：

- ticker
- OHLCV / Kline
- 24h statistics
- recent trades
- aggregate trades
- order book
- best bid / ask
- exchange info

支持周期：

- 1m
- 5m
- 15m
- 1h
- 4h
- 1d

自行计算：

- Price Momentum
- Volume Change
- Relative Volume
- Volume Acceleration
- Volatility
- ATR
- RSI
- MACD
- EMA
- SMA
- VWAP
- Anchored VWAP
- Breakout
- Fake Breakout
- Market Structure
- Spread
- Bid Depth
- Ask Depth
- Order Book Imbalance
- Large Trade Detection
- Aggressive Buy
- Aggressive Sell

---

# 8. Binance Futures 数据

采集：

- Open Interest
- Funding Rate
- Mark Price
- Index Price
- Basis
- Futures Kline
- Futures Trades
- Futures Order Book
- Long/Short Ratio
- Top Trader Long/Short Ratio
- Taker Buy/Sell
- Liquidation

如果某项 Binance Public API 不支持：

不要伪造。

必须：

1. 标记 unavailable；
2. 检查官方文档；
3. 系统上线后自行积累。

---

# 9. OI Engine

自行计算：

- OI Change
- OI Velocity
- OI Acceleration
- OI Z-Score
- OI Percentile
- OI / Market Cap
- Price / OI Divergence

识别：

### Price Up + OI Up

潜在：

新杠杆多头建立。

### Price Up + OI Down

潜在：

Short Squeeze 或空头平仓推动。

### Price Down + OI Up

潜在：

新空头建立。

### Price Down + OI Down

潜在：

Long Liquidation / Deleveraging。

必须结合 Funding 和 Taker Flow 进一步确认。

---

# 10. Funding Engine

计算：

- Funding Current
- Funding Mean
- Funding Z-Score
- Funding Percentile
- Funding Extreme
- Funding Trend

重点识别：

价格上涨

+

Funding 仍负

可能意味着：

Short Squeeze Candidate。

价格上涨

+

Funding 极度正

+

OI 快速上涨

可能意味着：

Leverage Overheat。

---

# 11. Spot vs Perp Divergence Engine

这是核心模块。

同币种同时分析：

Spot：

- volume
- aggressive buy
- aggressive sell
- order book

Futures：

- volume
- OI
- funding
- taker flow
- basis

识别：

- Spot Driven Rally
- Perp Driven Rally
- Short Squeeze
- Long Squeeze
- Healthy Accumulation
- Leverage Pump
- Distribution

重点寻找：

**现货领先、合约尚未过热**

这种结构。

---

# 12. BTC Spot / Futures CVD

如果 API 数据允许：

自行计算：

- Spot CVD
- Futures CVD

研究：

Price vs Spot CVD

Price vs Perp CVD

例如：

价格横盘

但 Spot CVD 持续上升，

属于重要潜在吸筹信号。

---

# 13. BTC Order Book Intelligence

实时计算：

- 0.5% depth
- 1% depth
- 2% depth
- 5% depth
- bid/ask imbalance
- spread
- liquidity concentration
- wall detection
- wall persistence

必须防止简单把“挂单墙”当成真实买卖意愿。

记录：

挂单持续时间

撤单行为

成交行为。

---

# 14. BTC Liquidation Intelligence

监控：

- Long Liquidation
- Short Liquidation
- Liquidation Velocity
- Liquidation Clusters

识别：

- Panic Long Flush
- Short Squeeze
- Cascading Liquidation
- Healthy Deleveraging

---

# 15. BTC 技术结构

技术分析只作为一层。

支持：

- Trend
- HH / HL
- LH / LL
- Support
- Resistance
- ATR
- RSI
- MACD
- EMA
- SMA
- VWAP
- Volume Profile
- Compression
- Breakout
- Volatility Expansion

禁止：

单独使用 RSI 或 MACD 做核心判断。

---

# 16. BTC 链上专业研究

第一阶段不接 Glassnode API。

改为：

Glassnode Research Intelligence Provider。

来源：

- Glassnode Newsletter
- Glassnode Weekly
- Insights
- Email
- Public Research

结构化提取：

- Thesis
- Evidence
- Bullish Evidence
- Bearish Evidence
- MVRV
- STH
- LTH
- Realized Price
- SOPR
- Profit/Loss
- Supply Distribution
- Market Regime
- Key Levels
- Invalidation
- Time Horizon

必须标记：

source_time

metric_time

publish_time。

不能把报告里的指标当成实时 API 数据。

---

# 17. Research Intelligence Provider

统一处理：

- Glassnode
- Santiment
- Nansen
- Arkham
- CryptoQuant
- Binance Research
- OKX Research
- Coinbase Research
- Institutional Research
- Crypto Newsletter

统一字段：

- source
-