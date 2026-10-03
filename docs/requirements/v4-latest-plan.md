可以。下面是整合后的 **V4 最终开发计划书**。三项修改已经不是局部替换，而是同步修改到了数据架构、Signal、监控池、Dashboard、Backtest 和开发阶段中。

:::writing{variant="document" id="48173" title="加密货币智能研判与早期预警系统 V4 最终开发需求"}
# 加密货币智能研判与早期预警系统 V4

## 0. 项目定位

开发一个长期运行的专业级加密货币：

**Intelligence + Monitoring + Research + Early Warning System**

系统不是普通行情看板，不是技术指标合集，也不是自动交易机器人。

第一阶段完成：

**数据采集 → 清洗 → 标准化 → 特征计算 → 异常检测 → 多源交叉验证 → 市场状态判断 → 机会发现 → 风险过滤 → 研判 → Web 预警 → 事后验证 → 回测 → 动态优化。**

暂时不做：

自动下单  
自动开仓  
自动杠杆交易  
自动资金管理  
Telegram 通知

---

# 1. 三项确定性数据策略

本版本确定以下架构，不再反复修改。

## 1.1 现货市场

主要使用：

**Binance Public API**

获取：

行情  
成交  
盘口  
Kline  
Volume  
现货订单流等。

---

## 1.2 合约市场

所有主要合约数据统一改为：

**OKX / 欧易公开数据**

不再使用 Binance Futures 作为主要合约数据源。

优先获取：

Open Interest  
OI History  
Funding Rate  
Funding History  
Mark Price  
Index Price  
Basis  
Contract Volume  
Taker Buy/Sell  
Long/Short Ratio  
Top Trader / Elite Trader 数据  
合约盘口  
合约成交  
永续合约 Kline。

所有接口必须先依据最新 OKX 官方文档验证。

禁止猜测 Endpoint 或字段。

---

## 1.3 通知方式

完全移除 Telegram。

第一阶段预警统一进入：

**Web Alert Center**

支持：

站内实时预警  
Alert Badge  
Alert Feed  
声音提示（用户可关闭）  
浏览器 Notification（可选）

以后可以增加：

Email  
Mobile Push

但 Telegram 不再属于项目架构。

---

# 2. 监控规模

Altcoin 主监控 Universe：

**100 个币种。**

BTC 和 ETH 单独作为核心市场资产监控：

**不占这 100 个 Altcoin 名额。**

因此系统实际上至少长期监控：

BTC  
ETH  
+
100 Altcoins。

Meme DEX Scanner 属于另一套 Discovery Universe：

**不受 100 个限制。**

---

# 3. 100 币监控池设计

不能人工固定写死 100 个币。

建立：

**Dynamic Top-100 Monitoring Universe**

每天自动重新计算。

选择依据包括：

流动性  
24h 成交额  
30d 成交活跃度  
Binance Spot 是否存在  
OKX SWAP 是否存在  
盘口深度  
历史连续性  
数据完整度。

优先选择：

Binance Spot

+

OKX USDT 永续合约

同时存在的币种。

---

# 4. 监控池过滤

默认排除：

Stablecoin

Leveraged Token

Wrapped stable assets

极低流动性币

即将下架币

数据明显异常币。

保留：

User Watchlist。

用户手动加入的重要币种不应因为排名下降自动消失。

例如：

PEPE  
SXT  
其他重点标的

可以设置：

PINNED = TRUE。

---

# 5. 100 币动态调整

每日计算：

Universe Score。

建议考虑：

30% Liquidity  
25% Spot Volume  
20% Derivatives Activity  
10% Volatility  
10% Market Cap / Market Importance  
5% Data Quality

具体权重配置化。

Universe 每天只允许有限数量变更，

例如：

最多替换 5～10 个币。

避免每天大量变动导致：

历史数据无法连续比较。

---

# 6. Universe 分层

将 100 个币划分为：

TIER 1

约 20 个。

高流动性、高重要性。

采集频率最高。

TIER 2

约 30 个。

主要山寨。

TIER 3

约 50 个。

Early Warning Discovery Pool。

不同层可以采用不同：

Order Book Sampling Frequency

Feature Frequency

Storage Frequency。

目的是在 100 币规模下控制：

API Rate Limit

数据库压力

计算资源。

---

# 7. 最高设计原则

整个系统始终遵循：

**Early > Late**

提前发现比涨完解释更重要。

**Evidence > Narrative**

证据优先。

**Multiple Independent Signals > Single Indicator**

重要信号必须多源交叉验证。

**Point-In-Time > Retrospective Explanation**

禁止未来数据泄漏。

**Precision > Alert Quantity**

减少垃圾提醒。

**Risk Before Opportunity**

尤其是 Meme。

**Explainable > Black Box**

必须知道为什么产生 Signal。

**Backtest Everything**

没有验证的信号不能成为核心信号。

**Raw Data > Third-Party Score**

尽量保存原始数据。

**Data Lineage**

结论必须能反查数据来源。

---

# 8. 系统总体架构

系统分为九个 Intelligence Layer：

1. Global Market Regime Engine
2. BTC Intelligence Engine
3. Altcoin Early Warning Engine
4. Meme Early Discovery Engine
5. Wallet Intelligence Engine
6. Research / News / Social Intelligence Engine
7. Signal Fusion Engine
8. Backtest / Validation Engine
9. Data Infrastructure / Web Dashboard / Alert Layer

关键关系：

Global Market

↓

BTC Regime

↓

Altcoin / Meme Risk Adjustment。

---

# 9. Global Market Regime

系统判断：

RISK_OFF

CAPITULATION

DELEVERAGING

ACCUMULATION

EARLY_RISK_ON

BTC_LEAD

ETH_ROTATION

ALT_EXPANSION

MEME_MANIA

LATE_BULL

DISTRIBUTION

EUPHORIA

BEAR_MARKET。

每次输出：

regime

confidence

regime_start_time

regime_duration

previous_regime

supporting_evidence

contradictory_evidence

invalidation_conditions。

---

# 10. 宏观经济模块

监控：

Fed Funds Rate  
FOMC  
CME FedWatch  
CPI  
Core CPI  
PCE  
Core PCE  
NFP  
Unemployment  
Initial Jobless Claims  
GDP  
ISM  
DXY  
US 2Y Yield  
US 10Y Yield  
2Y/10Y Spread  
VIX  
MOVE  
NASDAQ  
S&P500  
Gold。

同时关注：

Fed Balance Sheet  
TGA  
RRP  
Bank Reserves。

---

# 11. Macro Event Reaction Database

重要宏观事件保存：

actual

consensus

previous

surprise

release_time。

同时保存 BTC：

T-1h

发布时

+5m

+15m

+1h

+4h

+24h

表现。

目的不是简单判断：

CPI 低 = 利多。

而是建立：

**市场实际如何对宏观数据进行定价。**

---

# 12. BTC Intelligence Engine

BTC 是全系统最重要的风险开关。

输出：

BTC Regime

Bull Score

Bear Score

Spot Demand Score

Derivatives Score

Leverage Risk

Macro Risk

Liquidity Score

Research Bias

Overall Confidence。

禁止简单输出：

BUY / SELL。

---

# 13. BTC 现货数据

BTC 现货主要来自：

**Binance Public API**

采集：

Ticker

Kline

24h Statistics

Trades

Aggregate Trades

Order Book

Best Bid/Ask。

周期：

1m

5m

15m

1h

4h

1d。

---

# 14. BTC 现货 Feature

计算：

Price Momentum

Relative Volume

Volume Acceleration

ATR

RSI

MACD

EMA

SMA

VWAP

Anchored VWAP

Spread

Bid Depth

Ask Depth

Order Book Imbalance

Aggressive Buy

Aggressive Sell

Large Trade

Breakout

Fake Breakout

Compression

Volatility Expansion。

技术指标不能单独触发高级 Signal。

---

# 15. 合约统一使用 OKX

BTC、ETH 和监控的 100 个 Altcoin 的合约数据，

统一优先从：

**OKX Public API / OKX Public Trading Data**

获取。

建立：

OKXDerivativesProvider。

业务层禁止直接依赖 OKX HTTP Endpoint。

---

# 16. OKX 合约数据

优先采集：

Open Interest

Open Interest History

Funding Rate

Funding Rate History

Mark Price

Index Price

Basis

SWAP Ticker

Contract Volume

Contract Kline

Contract Trades

Order Book

Taker Buy/Sell

Long/Short Account Ratio

Elite Trader Long/Short

Elite Position Ratio。

只允许使用当前官方公开可获得的数据。

---

# 17. OKX 实际清算数据特别处理

不得假设 OKX 仍提供公开 Liquidation Orders API。

如果当前官方 Public API 无法直接获得真实市场爆仓数据：

字段状态设为：

UNAVAILABLE。

禁止：

模拟真实爆仓金额

估算后标成真实 Liquidation

使用虚构 API。

---

# 18. Deleveraging Proxy

虽然没有真实公开爆仓单数据，

系统仍可以计算：

**Deleveraging Proxy**

例如：

Price 急跌

+

OI 急降

+

主动卖出增加

+

Volume 激增

+

Funding 极端。

输出：

DELEVERAGING_PROBABILITY

或：

DELEVERAGING_SIGNAL。

必须明确：

这是推断指标，

不是实际爆仓金额。

---

# 19. OI Engine

基于 OKX 数据计算：

OI

OI Change

OI Velocity

OI Acceleration

OI Z-Score

OI Percentile

OI / Market Cap

Price / OI Divergence。

识别：

Price Up + OI Up

Price Up + OI Down

Price Down + OI Up

Price Down + OI Down。

---

# 20. Funding Engine

基于 OKX：

Current Funding

Historical Funding。

计算：

Funding Mean

Funding Z-Score

Funding Percentile

Funding Extreme

Funding Trend

Funding Acceleration。

重点识别：

Price Up

+

Funding Negative

可能形成：

Short Squeeze Candidate。

以及：

Price Up

+

OI Surge

+

Funding Extreme Positive

形成：

Leverage Overheat。

---

# 21. OKX Long/Short Intelligence

使用欧易公开：

Long/Short Account Ratio

Elite Trader Ratio

Elite Position Ratio。

分析：

Retail Positioning

Large Trader Positioning

Position Divergence。

例如：

普通账户明显做多

但 Elite Position 开始下降，

属于需要关注的 Divergence。

这些指标只作为证据之一，

禁止单独作为买卖信号。

---

# 22. OKX Taker Flow

获取：

Contract Taker Buy Volume

Contract Taker Sell Volume。

计算：

Taker Buy/Sell Ratio

Taker Delta

Taker Acceleration

Aggressive Flow Z-Score。

用于判断：

主动追多

主动追空

多空力量变化。

---

# 23. Binance Spot + OKX Perp 联动

这是整个系统的核心之一。

Spot：

Binance。

Derivatives：

OKX。

建立：

CrossVenueSpotPerpEngine。

比较：

Binance Spot Price

Binance Spot Volume

Binance Spot Aggressive Flow

vs

OKX Perp Price

OKX Perp Volume

OKX OI

OKX Funding

OKX Taker Flow。

---

# 24. 跨交易所数据不可直接硬比较

因为：

Binance Spot

和：

OKX Perpetual

属于不同交易场所。

必须处理：

Symbol Mapping

Timestamp Alignment

Price Normalization

Quote Currency

Contract Multiplier

Instrument Type

Network / Token Identity。

禁止：

直接拿两个原始 Volume 数值比较。

---

# 25. Symbol Mapping

建立：

Instrument Registry。

例如：

BTCUSDT

↔

BTC-USDT-SWAP。

每个资产保存：

canonical_asset_id

binance_symbol

okx_inst_id

contract_address

chain

quote_currency

contract_type。

所有 Engine 使用：

canonical_asset_id。

---

# 26. Time Synchronization

所有 Binance 和 OKX 数据：

统一转换 UTC。

保存：

source_timestamp

receive_timestamp

processing_timestamp。

Signal Fusion 前必须：

Time Alignment。

不能拿：

12:00 Binance 数据

和：

12:05 OKX 数据

当成同一时刻比较。

---

# 27. Spot vs Perp Divergence

识别：

Spot Driven Rally

Perp Driven Rally

Short Squeeze

Long Squeeze

Healthy Accumulation

Leverage Pump

Potential Distribution。

理想 Early Signal：

Binance Spot Demand ↑

+

Token/BTC Strength ↑

+

OKX OI 温和 ↑

+

Funding Neutral

+

Perp 尚未过热。

---

# 28. CVD

如果逐笔数据足以构建：

计算：

Binance Spot CVD

OKX Perp CVD。

研究：

Price vs Spot CVD

Price vs Perp CVD。

例如：

价格横盘

但 Binance Spot CVD 持续增加，

可能属于：

Spot Accumulation。

---

# 29. Order Book Intelligence

Binance Spot 和 OKX Contract 分别计算：

0.5% Depth

1% Depth

2% Depth

5% Depth

Bid/Ask Imbalance

Spread

Wall

Wall Persistence

Cancellation Behavior。

禁止简单认为：

挂单墙 = 真正支撑。

---

# 30. BTC 链上研究

第一阶段不购买 Glassnode API。

使用：

Glassnode Newsletter

Glassnode Weekly

Glassnode Insights

Glassnode Email

Glassnode Public Research。

提取：

Thesis

MVRV

STH

LTH

Realized Price

SOPR

Profit/Loss

Supply Distribution

Key Levels

Bullish Evidence

Bearish Evidence

Invalidation

Time Horizon。

---

# 31. Research Provider

统一支持：

Glassnode

Santiment

Nansen

Arkham

CryptoQuant

Binance Research

OKX Research

Coinbase Research

机构 Research

Newsletter。

第一阶段：

使用公开内容与邮箱内容。

不购买专业 API。

---

# 32. Email Intelligence

流程：

Email

↓

Source Detection

↓

正文提取

↓

附件 / Link Detection

↓

LLM Structured Extraction

↓

Research Database

↓

Evidence Engine。

需要识别：

新观点

重复观点

观点变化

风险变化

Bullish → Bearish

Bearish → Bullish。

---

# 33. Research 不能取代实时数据

例如：

Glassnode Research：

中期链上结构健康。

OKX：

OI 急增

+

Funding 极端。

Binance Spot：

买盘减弱。

最终应该形成：

Medium-Term Positive

BUT

Short-Term Leverage Risk High。

---

# 34. Stablecoin Liquidity

监控：

USDT

USDC

FDUSD

DAI。

尽可能利用公开数据。

研究：

Supply

7D Change

30D Change

Mint/Burn

Exchange Liquidity。

建立：

Crypto Liquidity Index。

---

# 35. ETF / Institutional Flow

使用公开信息。

跟踪：

BTC ETF Daily Net Flow

ETF Holdings

AUM

Flow Acceleration。

寻找：

Price / ETF Flow Divergence。

---

# 36. Altcoin Early Warning Engine

第一阶段核心任务：

持续扫描：

**100 个 Altcoin。**

目的不是：

找已经涨得最多的币。

而是：

**找正在出现启动前异常，但价格还没有明显拉升的币。**

---

# 37. 100 币实时 Feature

每个资产至少维护：

Price

Price Change

Volume

Relative Volume

Volume Acceleration

Volatility

BTC Relative Strength

ETH Relative Strength

Binance Spot Flow

Binance Order Book

OKX OI

OKX OI Change

OKX OI Acceleration

OKX Funding

Funding Z-Score

OKX Taker Flow

OKX Long/Short

Spot/Perp Divergence

Liquidity

Market Structure。

---

# 38. Relative Strength Engine

计算：

TOKEN/USDT

TOKEN/BTC

TOKEN/ETH。

重点寻找：

BTC 横盘

+

TOKEN/BTC 连续增强。

这是潜在资金轮动的重要信号。

---

# 39. Pre-Pump Signal A

价格横盘

+

Relative Volume 持续增加。

---

# 40. Pre-Pump Signal B

价格只小幅上涨

+

OKX OI 快速增加

+

Funding 尚未过热。

标记：

Potential Position Build-up。

---

# 41. Pre-Pump Signal C

Binance Spot Buy Flow ↑

+

OKX Futures 尚未明显启动。

这种信号优先级较高。

---

# 42. Pre-Pump Signal D

Price ↑

+

Funding < 0

+

OI ↑。

可能存在：

Short Squeeze Setup。

---

# 43. Pre-Pump Signal E

BTC 横盘

+

TOKEN/BTC Strength ↑

+

Spot Volume ↑

+

Funding Neutral。

可能属于：

Altcoin Rotation。

---

# 44. Pre-Pump Signal F

Spot CVD ↑

+

Price 不涨

+

卖单深度下降。

可能存在：

Stealth Accumulation。

---

# 45. 100 币 Cross-Section Ranking

每隔固定时间对 100 个币进行横向比较。

计算：

Relative Volume Rank

OI Acceleration Rank

Funding Rank

Relative Strength Rank

Spot Flow Rank

Taker Flow Rank

Liquidity Rank

Order Book Rank

Volatility Rank。

最终得到：

Cross-Sectional Opportunity Rank。

---

# 46. Rank Velocity

不仅看：

现在排名第几。

还要看：

排名变化速度。

例如：

3 小时前：

Rank 73

现在：

Rank 11。

建立：

Rank Velocity。

这类币可能比一直排第一的热门币更具有：

Early Discovery Value。

---

# 47. Altcoin Opportunity Score

每个币：

0–100。

明确：

不是上涨概率。

输入：

Global Regime

BTC Regime

Relative Strength

Volume Acceleration

Spot Flow

OKX OI

OKX Funding

OKX Taker

OKX Long/Short

Spot/Perp Divergence

Order Book

Liquidity

Catalyst

Risk。

---

# 48. Opportunity Score Delta

必须保存：

Previous Score

Current Score

Score Delta。

例如：

APT

58 → 79

+21。

系统需要解释：

为什么突然增加 21 分。

---

# 49. Alert Level

使用：

INFO

WATCH

SETUP

STRONG

CRITICAL_RISK。

WATCH：

初步异常。

SETUP：

多个类别开始共振。

STRONG：

多源高质量证据同时出现。

CRITICAL_RISK：

风险快速增加。

---

# 50. Web Alert Center

所有预警统一进入：

Web Alert Center。

每条 Alert 包括：

Token

Time

Level

Score

Score Delta

Price

Signal Type

Supporting Evidence

Contradicting Evidence

Risk

Invalidation

Data Freshness。

支持：

Unread

Read

Pinned

Resolved

Expired。

---

# 51. Alert Escalation

同一资产：

WATCH

→

SETUP

→

STRONG

属于同一个：

Signal Event。

禁止生成三条完全独立的垃圾提醒。

保存：

first_seen

last_updated

escalated_at

max_score

resolved_at。

---

# 52. Alert 去重

支持：

Deduplication

Cooldown

Escalation

De-escalation。

只有出现：

新证据

Score 明显变化

Level 升级

风险变化

才生成新的重要提醒。

---

# 53. Meme Early Discovery

Meme Engine 与 100 个 Altcoin Universe 分开。

Meme Discovery：

**数量不限制为 100。**

实时扫描：

新 Token

新 Pool

新成交

新流动性。

---

# 54. Meme 支持链

第一阶段：

Solana

Base

BSC

Ethereum。

根据后续市场热点扩展。

---

# 55. Meme 数据策略

Meme 不依赖 Binance 或 OKX。

优先：

Public RPC

DEX Public Data

Chain Explorer Public Data

免费公开 Endpoint。

专业付费链上 API 后续评估。

---

# 56. New Token Scanner

监听：

New Token

New Pool

Liquidity Add

Liquidity Remove

First Trade

Launchpad Graduation。

Token 唯一标识：

chain + contract_address。

禁止只使用 ticker。

---

# 57. Rug Filter

先判断风险，

再讨论上涨潜力。

检查：

Mint Authority

Freeze Authority

Owner Privilege

Honeypot

Sell Restriction

Blacklist

Transfer Tax

Proxy

Upgradeability

Liquidity Lock

Liquidity Burn

Creator Holdings

Insider Holdings

Top Holder Concentration

Bundled Supply

Sniper Wallet

Same Block Buyer

Dev Selling

Liquidity Removal。

---

# 58. Rug Risk Score

独立维护：

Rug Risk Score 0–100。

不能与：

Discovery Score

合并。

例如：

Discovery = 95

Rug Risk = 87。

依然应该提示：

EXTREME RISK。

---

# 59. Holder Intelligence

计算：

Holder Growth Velocity

Unique Buyers

Unique Sellers

Repeat Buyer

Median Position

Top10

Top20

Insider Concentration

Smart Wallet Concentration。

排除：

LP

Burn

Router

Program

Exchange。

---

# 60. Wallet Intelligence

建立长期钱包历史库。

记录：

wallet

chain

first_seen

historical trades

entry

exit

hold time

MFE

MAE

median return

win rate

token category。

一个钱包押中过一次不能称作 Smart Money。

---

# 61. Early Buyer Database

每个新 Meme 保存：

First 10 Buyers

First 20

First 50

First 100。

跟踪它们未来表现。

这将成为系统最重要的长期资产之一。

---

# 62. Meme Smart Wallet Score

计算：

Early Entry Frequency

MFE

MAE

Median Return

Hit Rate

Rug Exposure

Average Lead Time。

寻找：

多个独立高质量 Early Wallet

是否同时进入同一个 Meme。

---

# 63. Wallet Cluster

判断多个地址是否实际上属于：

同一主体。

研究：

funding source

transfer graph

entry timing

same block

coordinated transaction。

防止：

一个操盘者拆 30 个钱包

被系统误判成：

30 个聪明钱包。

---

# 64. Meme Organic Growth

研究：

Unique Buyers

Unique Sellers

Holder Growth

Liquidity Growth

Volume Growth

Repeat Buyers

Wallet Diversity

Funding Diversity

Trade Size Distribution。

理想形态：

Price 温和上涨

Holder 快增

Unique Buyer 快增

Liquidity 增长

Smart Wallet 进入

Social 开始扩散。

---

# 65. Meme Social

监控：

X

公开社区信息

项目官方账号。

计算：

Unique Authors

Mention Velocity

Mention Acceleration

Engagement Quality

KOL Cascade

Bot Probability

Text Similarity

Repeated Marketing

Paid Promotion。

---

# 66. Narrative Engine

识别：

AI

动物

政治

名人

文化

技术

突发新闻

网络热点

其他 Meme Narrative。

计算：

Narrative Velocity

Novelty

Saturation

Token Count

Leader。

重点寻找：

Narrative 正在扩散

但 Token 尚未完全 Price In。

---

# 67. Meme Discovery Score

输出：

Token

Chain

Contract

Age

Market Cap

Liquidity

Volume

Holder Growth

Unique Buyers

Smart Wallet

Narrative

Social Velocity

Insider Concentration

Rug Risk

Discovery Score

First Detected

Return Since Detection。

---

# 68. Signal Evidence Engine

禁止输出：

“某币要涨。”

每个重要 Signal 必须解释：

为什么是它？

为什么现在？

发生了什么变化？

谁在买？

现货还是合约推动？

OI 如何？

Funding 如何？

相对 BTC 是否增强？

风险是什么？

什么情况下失效？

---

# 69. Supporting vs Contradictory Evidence

任何研判同时保留：

Supporting Evidence

和：

Contradictory Evidence。

例如：

Positive：

Spot demand 强。

Negative：

OKX Funding 已经偏高。

最终：

Signal Confidence 降低。

避免模型只寻找支持自己结论的数据。

---

# 70. Signal Delta Engine

系统必须具有状态记忆。

例如：

昨天：

BTC Leverage Risk = 46。

今天：

74。

必须解释：

哪些 Feature 造成：

+28。

Altcoin 同理。

---

# 71. Multi-Signal Confirmation

STRONG Alert 原则上要求：

至少三个不同类别信号。

例如：

Relative Strength

+

Spot Flow

+

OKX Derivatives

+

Order Book。

禁止：

RSI 超卖 = STRONG

Funding 负 = STRONG

OI 增长 = STRONG。

---

# 72. Backtest Engine

所有 Signal 保存：

signal_time

asset

price

feature_snapshot

raw_data_reference

source_timestamp

feature_version

rule_version

signal_version

model_version。

---

# 73. Forward Performance

保存：

15m

1h

4h

24h

3d

7d

14d

30d。

计算：

Return

MFE

MAE

Time to MFE

Time to MAE。

---

# 74. Lead Time

Lead Time 是系统核心 KPI。

目标不仅是：

预测方向正确。

更重要的是：

**提前多久发现。**

例如：

首次 WATCH

↓

40 分钟

↓

SETUP

↓

2 小时

↓

价格开始明显上涨。

保存：

First Signal Lead Time。

---

# 75. Ranking Backtest

由于我们有 100 币监控池，

必须测试：

每天 Rank Top 5

Top 10

Top 20。

未来：

4h

24h

3d

7d

实际表现。

计算：

Precision@5

Precision@10

Precision@20。

---

# 76. False Positive Analysis

每周统计：

哪些信号经常：

看起来漂亮

但后面没有上涨。

例如：

OI 增长

但只是：

Short Build-up。

或者：

Volume 增长

只是：

高位换手。

系统需要不断降低这些 False Signal 权重。

---

# 77. Look-Ahead Bias

所有回测必须严格：

Point-In-Time。

禁止：

未来价格

未来标签

未来钱包身份

后来更新的数据

未来 Narrative

未来 Market Cap。

失败币必须保留。

避免：

Survivorship Bias。

---

# 78. ML 策略

第一阶段：

Rule Engine

Z-Score

Percentile

Anomaly Detection

Cross-Section Ranking

Regime Model。

历史数据足够后再使用：

Logistic Regression

LightGBM

XGBoost

CatBoost

Ranking Model。

---

# 79. ML 目标

不要预测：

“明天精确价格 1.342 美元”。

更适合预测：

未来 4 小时是否进入 Top 10% Return

未来 24 小时 Ranking

Breakout Probability

Risk Probability。

---

# 80. LLM 职责

LLM 主要负责：

Research Extraction

Newsletter Extraction

News Understanding

Narrative

KOL Thesis

Evidence Summary。

LLM 不负责：

凭感觉预测价格。

---

# 81. Data Quality

每条数据保存：

provider

endpoint

canonical_asset_id

exchange_symbol

source_timestamp

ingestion_timestamp

interval

raw_value

normalized_value

unit

definition

scope

quality_status。

---

# 82. Data Health

状态：

FRESH

DELAYED

STALE

BROKEN

UNAVAILABLE

DISABLED。

检测：

API 断线

字段变化

延迟

时间错位

异常值

WebSocket 掉线

数据静止

Rate Limit。

---

# 83. 数据口径

严格禁止：

Fees = Revenue

Protocol Revenue = Holder Revenue

Announcement = 实际 Buyback

Announcement = 实际 Burn

TVL Growth = Token Value Capture

Third-party Estimate = Fact。

无法确认：

UNKNOWN / UNVERIFIED。

---

# 84. Provider Architecture

建立：

providers/binance_spot

providers/okx_derivatives

providers/macro

providers/glassnode_research

providers/santiment_research

providers/dex

providers/onchain

providers/social。

未来：

providers/glassnode_api

providers/nansen_api

providers/arkham_api。

业务逻辑不能直接绑定第三方 Endpoint。

---

# 85. BinanceSpotProvider

职责：

Ticker

Kline

Trades

AggTrades

Order Book

Spot Market Metadata。

不得负责合约。

---

# 86. OKXDerivativesProvider

负责：

SWAP

FUTURES

OI

Funding

Mark

Index

Basis

Taker Flow

Long/Short

Elite Trader

Contract Order Book

Contract Trades。

实现前：

必须核验当前 OKX 官方 API Documentation。

---

# 87. OKX Capability Registry

为每项指标建立：

AVAILABLE

UNAVAILABLE

AUTH_REQUIRED

PUBLIC

DEPRECATED。

例如：

Funding = PUBLIC

OI = PUBLIC

Public Liquidation Orders = UNAVAILABLE

不得让不可用数据静默进入 Signal。

---

# 88. Database

建议：

PostgreSQL：

metadata

universe

wallet

research

signal

alert

config

backtest。

TimescaleDB / ClickHouse：

spot kline

spot trades

OKX OI

OKX funding

taker data

order book

feature snapshots。

Redis：

cache

realtime state

lock

rate limit。

---

# 89. 100 币规模存储优化

不能所有数据都永久按 tick 保存。

制定 Retention Policy。

例如：

High Frequency Raw Data：

短期保存。

Aggregated Data：

长期保存。

长期必须保留：

1m / 5m aggregate

Feature Snapshot

Signal Snapshot

OI

Funding

重要 Order Flow。

---

# 90. Raw Data Asset

从第一天开始积累：

Binance Spot

+

OKX Derivatives。

长期形成：

Crypto Historical Intelligence Dataset。

这是系统未来最重要的资产之一。

---

# 91. Backend

建议：

Python

FastAPI

asyncio

REST

WebSocket

Pydantic

SQLAlchemy

Alembic。

Collector 支持：

Retry

Timeout

Backoff

Reconnect

Rate Limit

Idempotency

Graceful Shutdown

Checkpoint。

---

# 92. Dashboard

主页面只展示：

MARKET REGIME

BTC STATUS

TOP ALTCOIN SIGNALS

100-COIN HEATMAP

MEME RADAR

RISK

ACTIVE ALERTS。

---

# 93. 100-Coin Radar

专门设计：

100-Coin Radar 页面。

支持：

Score 排序

Rank Change

Price Change

Relative Strength

Spot Flow

OI Change

Funding

Taker Flow

Signal Level

Data Quality。

可快速查看：

谁正在悄悄变强。

---

# 94. Opportunity Heatmap

100 个币以 Heatmap 展示。

维度：

Spot Strength

Derivative Build-up

Relative Strength

Volume Anomaly

Risk。

点击任何币进入：

Token Detail。

---

# 95. Token Detail

回答：

为什么发现？

什么时候发现？

当时价格？

现在价格？

Rank 如何变化？

Binance Spot Flow 怎么样？

OKX OI 怎么样？

Funding 怎么样？

Taker Flow 怎么样？

谁先启动：

Spot

还是：

Perp？

Signal 是否增强？

最大风险是什么？

---

# 96. Web Alert Center

因为不使用 Telegram，

Alert Center 必须成为核心产品模块。

页面支持：

Live Feed

Filter

Severity

Asset

Signal Type

Search

Pinned

Unread

Resolved。

---

# 97. Browser Notification

可选实现：

Browser Notification API。

只允许：

STRONG

CRITICAL_RISK

触发系统级浏览器通知。

用户可以关闭。

WATCH 不弹系统通知，

只进入 Web Alert Center。

---

# 98. Daily Intelligence Brief

Dashboard 自动生成：

Market Regime

BTC Regime

Macro

Spot Flow

OKX Derivatives

Top 100 Altcoin Ranking

Top Movers in Rank

Top Early Signals

Meme Candidates

Major Risk

Upcoming Events。

---

# 99. Weekly Review

每周自动复盘：

Signal 数量

成功

失败

MFE

MAE

Lead Time

Precision@5

Precision@10

Top Signal Type

Worst Signal Type

False Positive Reason

Feature Weight Change Suggestion。

---

# 100. Signal Versioning

所有模型版本化：

feature_version

rule_version

signal_version

threshold_version

model_version。

未来必须能比较：

V1

vs

V2

vs

V3。

---

# 101. Configuration

配置化：

100 币 Universe

Pinned Token

Threshold

Weight

Sampling Frequency

Rank Frequency

Alert Level

Cooldown

Data Retention。

禁止硬编码。

---

# 102. 安全

API Key 只允许：

.env。

即便目前主要使用 Public API，

仍然保留 Secret Management 架构。

禁止：

hardcode

Git commit

前端暴露

日志输出。

---

# 103. Production Stability

支持：

Docker

Healthcheck

Structured Logging

Metrics

Database Migration

Backup

Retry

Timeout

Circuit Breaker

Graceful Shutdown

Single Instance / Lock

UTC Storage

Timezone Conversion。

---

# 104. Phase 1

第一阶段先做真正可以长期运行的骨架。

完成：

Repo Audit

Architecture

Database

Canonical Asset Registry

Dynamic 100-Coin Universe

Binance Spot Provider

OKX Derivatives Provider

Kline

Spot Trades

Spot Order Book

OI

Funding

Taker Flow

Long/Short

Feature Engine

BTC Basic Regime

Altcoin Scanner

100-Coin Ranking

Web Alert Center

Basic Backtest

Data Health。

---

# 105. Phase 2

增加：

Macro

ETF

Stablecoin

Email Research

Glassnode Research

Santiment Research

Research Knowledge Base

Spot/Perp Divergence

CVD

Advanced OI/Funding Analysis。

---

# 106. Phase 3

增强 Altcoin：

Rank Velocity

Signal Fusion

Signal Delta

Catalyst

Tokenomics

Fundamental

Advanced Cross-Section Model。

---

# 107. Phase 4

Meme：

New Token

New Pool

Rug Filter

Holder

Early Buyer

Wallet History

Wallet Cluster

Smart Wallet

Discovery Score。

---

# 108. Phase 5

增加：

X Intelligence

Social

Narrative

KOL Reputation

Organic Growth Detection。

---

# 109. Phase 6

增加：

Advanced Backtest

ML Ranking

Feature Importance

Weight Calibration

Signal Calibration

Paid API Evaluation。

---

# 110. Paid API 原则

未来只有当数据能够明显提升：

Precision

Recall

Lead Time

Risk Detection

False Positive Rate

才考虑购买：

Glassnode

Nansen

Arkham

CoinGlass

Santiment

等专业 API。

---

# 111. 第一阶段核心实验

最终要验证：

仅仅通过：

Binance Spot

+

OKX Derivatives

+

公开 Macro

+

公开 Research

+

Email Intelligence

+

自建历史数据

是否能够发现：

Early Accumulation

Position Build-up

Spot-Led Rally

Short Squeeze Setup

Altcoin Rotation

Leverage Risk

Distribution Risk。

---

# 112. 最重要的 Alpha 假设

重点回测以下假设：

### Hypothesis A

Binance Spot demand

领先

OKX Perp demand。

### Hypothesis B

TOKEN/BTC Relative Strength

领先 USDT 价格突破。

### Hypothesis C

OI 增加

+

Funding Neutral

+

Price Compression

可能领先趋势启动。

### Hypothesis D

Spot CVD ↑

+

Price Flat

可能代表吸筹。

### Hypothesis E

Rank Velocity

比绝对 Rank

更有 Early Warning Value。

### Hypothesis F

Funding 极端

+

OI 极端

更适合做风险指标，

而不是继续追涨指标。

这些假设必须通过数据验证，

不能先认定为正确。

---

# 113. 最终 Signal 输出

每个 Altcoin Signal 必须包含：

Asset

Current Rank / 100

Previous Rank

Rank Change

Opportunity Score

Score Delta

Signal Level

First Seen

Current Price

Binance Spot Evidence

OKX Derivatives Evidence

Relative Strength

Risk

Contradictory Evidence

Invalidation

Data Freshness。

---

# 114. 系统最终需要回答的问题

1. BTC 当前是什么阶段？

2. 和昨天相比什么发生了变化？

3. BTC 最大风险是什么？

4. 100 个山寨币里面谁正在变强？

5. 谁的 Rank 上升最快？

6. 谁正在出现现货吸筹？

7. 谁正在出现 OI 异常？

8. 谁 Funding 尚未过热？

9. 谁是 Binance Spot 领先 OKX Perp？

10. 谁主要依靠合约拉升？

11. 谁可能存在 Short Squeeze？

12. 哪些 Signal 有真正提前量？

13. 哪些指标只是事后解释？

14. 当前哪些 Meme 出现异常早期增长？

15. 哪些早期钱包正在进入？

16. 哪些 Meme 可能是内部人盘？

17. 系统过去的预警到底准确不准确？

---

# 115. Codex 第一项任务

现在首先：

**审计当前 repo。**

不要立即大规模编码。

输出：

SYSTEM_ARCHITECTURE.md

DATA_SOURCE_MATRIX.md

OKX_CAPABILITY_MATRIX.md

UNIVERSE_DESIGN.md

DATABASE_SCHEMA.md

FEATURE_DESIGN.md

SIGNAL_DESIGN.md

BACKTEST_DESIGN.md

IMPLEMENTATION_PLAN.md

.env.example

docker-compose.yml

README.md。

---

# 116. OKX Capability Matrix 特别要求

必须逐项核验：

Open Interest

OI History

Funding

Funding History

Mark Price

Index Price

Basis

Taker Buy/Sell

Long/Short Ratio

Elite Trader Ratio

Elite Position Ratio

Contract Order Book

Contract Trades

Liquidation。

每项写：

Official Endpoint

Public / Auth

Rate Limit

Historical Range

Available

Deprecated

Verified Date。

无法确认：

NEEDS_VERIFICATION。

---

# 117. Data Source Matrix

至少包括：

Binance Spot

OKX Derivatives

Macro

Research

Email

DEX

On-chain

Social。

字段：

Provider

Data

Free/Paid

REST/WS

Historical

Frequency

Rate Limit

Authentication

Fallback

Status。

---

# 118. Codex 禁止事项

禁止：

编造 OKX Endpoint

编造 Binance Endpoint

编造 API 字段

编造 Liquidation 数据

Mock 冒充 Production

静默填充缺失数据

用未来数据回测

用涨完之后的数据证明系统提前预测。

---

# 119. 验收重点

Phase 1 完成后，不以：

“页面做得好不好看”

作为主要成功标准。

必须首先证明：

系统能够稳定监控：

BTC

ETH

+

100 个 Altcoins。

并且：

Binance Spot 数据稳定

OKX Derivatives 数据稳定

Universe 正常更新

Feature 正常计算

Ranking 正常

Signal 有解释

Web Alert 正常

Backtest 可以追踪

Data Health 可以发现异常。

---

# 120. 最终目标

长期最重要的资产不是 Dashboard。

而是：

Binance Spot Historical Dataset

OKX Derivatives Historical Dataset

100-Coin Feature History

Cross-Section Ranking History

Signal History

Wallet History

Meme Early Buyer History

Research History

Backtest History。

随着运行时间增加，

系统应该：

**越来越聪明，而不是永远依赖外部网站给答案。**

现在按照以上 V4 需求执行。

先完成 Repo Audit 和架构设计。

架构确认无明显问题后直接进入 Phase 1。

对于安全的本地读取、测试、重构和修复：

自行执行，不要频繁要求人工确认。

遇到无法确定的数据源、字段或 Endpoint：

查官方资料验证。

无法确认则标记：

NEEDS_VERIFICATION。

绝对禁止猜测。
:::

这次有一个很重要的架构变化：以后是 **“币安看现货，欧易看合约”**。这反而很适合我们要做的“上涨前预警”，因为可以专门研究 **Binance Spot 是否先出现资金异动，而 OKX 永续合约是否随后跟进**。

另外我把山寨监控明确成了 **100 个动态币种 + BTC/ETH 独立监控**，所以实际上是至少 102 个中心化市场资产；Meme 新币扫描则不受这 100 个名额限制。

欧易官方当前确实公开提供 OI、资金费率、标记价格等数据，也新增过 OI 历史、主动买卖量、多空人数比以及精英交易员相关公开交易数据；而公共爆仓单接口早已下线，所以计划书中特意把这部分改成了“真实值缺失 + 去杠杆代理信号”，不会伪造。citeturn206579search0turn111301search0

---

If you want, I can:

- 继续详细展开信号融合引擎设计
- 补充示例说明多源交叉验证机制
- 介绍下一阶段功能规划及任务安排