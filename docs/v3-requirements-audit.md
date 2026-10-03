> 历史审计：基于 V3 收到的节选与提交 b1b5004；当前开发已转为 V4，本文不代表 V4 实现状态。

# V3 立项说明书与当前实现核对

核查日期：2026-10-03（UTC+8）。核查代码：`b1b5004ebee82b42c0e1b2cb7e6ea44f4f736207`，分支 `feat/spot-100-retire-dex`，[PR #1](https://github.com/joeyxuepython/muse-btc/pull/1) 核查时仍为 OPEN，CI 成功。提交审阅不等于已合并或长期运行验收通过。

结论：**原始 V3 尚未全部实现。当前是具备实际数据适配、规则和归档能力的开发版本，不能按完整专业研判系统验收。50 个自动化测试通过不等于 50 个需求已完成。**

## 需求来源和后续调整

本次用户附件《已粘贴的文本.txt》共 8,974 字节，包含完整第 0～16 节和第 17 节开头；最后为“统一字段：- source -”，后文缺失。第 17 节余下内容及后续章节未出现在本次附件中，也没有完整的 50 项编号清单，不能推测其要求或填写完成状态。[保留收到的原文](requirements/v3-received-excerpt.md)。附件用于确定需求，不作为自动执行命令或自动交易的授权。

本次按用户后续确认的范围覆盖原文中的旧范围：

- 默认监控 BTC 固定加 99 个按成交额选出的 USDT 现货；交易所上市 Meme 可以入选。
- DEX Meme / GoPlus 的新采集、分析和提醒已取消，历史保留；不能再把 DEX 新功能当作待完成任务。
- Telegram 手机提醒已取消，不接入；这不等于取消研究报告或新闻的读取。
- 币安 IP/出口配置由用户处理；不能以接口测试代替真实在线验收。
- 原文暂不做自动下单、开仓、杠杆交易、资金调度，当前也没有这些功能。

状态含义：“部分实现”指存在相应代码但未覆盖章节要求；“未实现”指只有占位状态或未发现对应实现；“待验证”指代码/测试存在而实际运行效果尚不能确认。缺失的原文不计入任何完成率。

## 按原文章节核对

| 原文章节 | 状态 | 当前实现及缺口 |
| --- | --- | --- |
| 0 项目定位 | 部分实现 | 有采集、标准化、特征、少量证据规则、提醒、前瞻观察、归档重放。未完成完整市场研判、领先价值验证和动态优化；长期生产运行尚未验收。DEX 目标按后续指令取消。 |
| 1 最高设计原则 | 部分实现 | 原始响应、时间点边界、证据追溯、解释和观察期标签已有。重要入场候选需价格/现货方向/衍生品等条件；风险规则仍可来自单类证据。Early、Precision、Leading Value、多源独立验证和 Backtest Everything 尚无充分效果证明。 |
| 2 第一阶段数据源原则 | 部分实现 | Binance 现货/合约及 OKX 公开适配器已实现；未接入的源标记 DISABLED，测试样本未用于运行行情。Newsletter、Email、公开研究/博客/X 的研究读取未实现，Provider 目前是组合类而非完整独立插件体系。 |
| 3 九个核心层与联动 | 部分实现 | BTC 风险可影响现货机会条件和提醒失效；有山寨规则、验证、数据和页面基础。Global、Wallet、Research 引擎未实现，融合是固定条件，未实现稳定币流入、Dominance、Breadth 联动。Meme 新发现层已取消。 |
| 4 Global Market Regime | 未实现 | 现有 NORMAL/CAUTION/RISK_OFF/LEVERAGE_OVERHEAT/UNKNOWN 是 BTC 局部风险状态，不能代表原文的全市场阶段。缺 CAPITULATION、ACCUMULATION、ETH_ROTATION 等阶段，以及 confidence、起止/持续时间、前一阶段、阶段失效条件。 |
| 5 宏观经济 | 未实现 | Macro 明确 DISABLED；未采集 Fed/CPI/NFP/DXY/利率/VIX/美元流动性等数据。没有 actual/consensus/previous/surprise/release_time 模型与 Macro Event Reaction Database。提醒后的 5m～24h 观察并非宏观事件反应数据库。 |
| 6 BTC Intelligence | 部分实现 | 有 BTC 风险状态、EMA 趋势、支持与反向证据。缺 Bull/Bear/Liquidity/Spot Demand/Derivatives/Macro/Research 等完整评分和 Overall Confidence。没有简单 BUY/SELL 指令。 |
| 7 Binance Spot | 部分实现 | exchangeInfo、24h ticker、1m K 线、100 档盘口及部分自行计算特征已有。缺 recent/aggregate trades、完整多周期、MACD/SMA/VWAP/Anchored VWAP、突破/假突破、大额成交等。详见下表。 |
| 8 Binance Futures | 部分实现，在线待验证 | Funding、Mark/Index、Basis、5m OI 历史、1m 合约 K 线及窗口成交方向已实现。缺逐笔成交、合约盘口、Long/Short、Top Trader ratios、清算流。默认 OKX 不提供这些接口的等价完整覆盖。 |
| 9 OI Engine | 部分实现，存在采样缺口 | 5m OI Change 和短窗口 Z-score 已实现；缺 Velocity、Acceleration、Percentile、OI/Market Cap、完整 Price/OI 分类。轮换与 OKX 本地 OI 采样存在问题，见复现记录。 |
| 10 Funding Engine | 部分实现 | 当前费率、固定过热阈值、负 Funding 的逼空候选已有；缺历史均值、Z-score、Percentile、Trend 及统计型 Extreme。 |
| 11 Spot vs Perp Divergence | 部分实现 | 有现货动量候选、卖压、杠杆过热与潜在逼空。未完整区分 Perp Driven Rally、Long Squeeze、Healthy Accumulation、Distribution 等；默认 OKX 无合约主动成交数据，不能等同完整现货/合约交叉分析。 |
| 12 Spot/Futures CVD | 部分实现 | 通过最近 15 个收盘 1m K 线计算报价币窗口 CVD；Binance 合约模式有对应特征。不是逐笔连续 CVD，没有 Price/CVD 背离或横盘吸筹引擎；默认 OKX 合约 CVD 为空。 |
| 13 Order Book Intelligence | 部分实现 | 顶部价差、采样 1% 买卖深度和 imbalance 已有。缺 0.5%/2%/5% 深度、流动性集中度、挂单墙识别/持续时间、撤单和成交行为；100 档快照不能当完整订单簿。 |
| 14 Liquidation Intelligence | 未实现 | Liquidations 为 DISABLED。无多空清算、速度、聚类或级联分析；现有“潜在逼空”不能作为已观测清算的证据。 |
| 15 技术结构 | 部分实现 | EMA20/50、ATR14、RSI14、价格变化、短窗口波动率已有。缺 HH/HL/LH/LL、支撑阻力、MACD、SMA、VWAP、Volume Profile、Compression、Breakout 与完整 Volatility Expansion。 |
| 16 BTC 链上专业研究 | 未实现 | Research 为 DISABLED。没有 Glassnode Newsletter/Weekly/Insights/Email 解析，以及 MVRV/STH/LTH/SOPR 等结构化提取和 source_time/metric_time/publish_time 字段。 |
| 17 Research Intelligence | 已收到部分未实现，后文待补 | 已收到的多机构/Newsletter 研究处理目标未实现。原文统一字段在 source 后截断，不能完整核对本章字段。 |
| 后续章节及完整 50 项清单 | 原文未收到，无法核对 | 本次附件没有完整内容，不能声明已完成、未完成或计算完成百分比。 |

## 第 7 节的采集和特征细分

| 要求 | 对照结果 |
| --- | --- |
| ticker、24h statistics、exchange info | 已实现；100 个目标的连续真实在线覆盖待验证。 |
| OHLCV/Kline | 仅采 1m；详细采集轮换，其他 5m/15m/1h/4h/1d K 线未实现。用 1m 收盘价计算 5m/15m/1h 变化不等于提供这些周期 K 线。 |
| order book、best bid/ask | 采 100 档快照；内部可推导顶部价差，但没有独立 bookTicker 存储/流或持续 best bid/ask 更新。 |
| recent trades、aggregate trades、大额成交 | 未实现。 |
| Price Momentum、Relative Volume、Volume Acceleration | 有 5m/15m/1h 收盘变化、相对成交量与两段成交量比；缺更完整的成交量变化引擎。 |
| Volatility、ATR、RSI、EMA | 有短窗口 realized volatility、ATR14、RSI14、EMA20/50。 |
| MACD、SMA、VWAP、Anchored VWAP | 未实现。 |
| Breakout、Fake Breakout、Market Structure | 未实现完整结构；仅 EMA 方向不能替代这些要求。 |
| Spread、Bid/Ask Depth、Order Book Imbalance | 有采样 1% 深度和价差，不是完整流动性画像。 |
| Aggressive Buy/Sell | 通过 K 线 taker_buy_quote_volume 推导最近 15 分钟方向和窗口 CVD；缺逐笔买卖量与持续成交监控。 |

## 第 8～12 节的衍生品细分

| 要求 | 对照结果 |
| --- | --- |
| Open Interest、Funding、Mark/Index、Basis、Futures Kline | Binance 适配器有公开接口实现；实际覆盖受源和出口限制。OKX 用当前 OI 本地累积，跨所 Basis 只作方向参考。 |
| Futures Trades、Futures Order Book、Long/Short、Top Trader ratios | 未实现。 |
| Taker Buy/Sell | Binance 收盘 K 线可推导；默认 OKX 无对应数据。 |
| Liquidation 及自行积累 | 未实现 WebSocket 或其他清算积累。DISABLED 标记不等于完成积累功能。 |
| OI Change、Z-score | 有实现；短历史不足时为空。默认轮换可能持续缺少 5m 参照点。 |
| OI Velocity/Acceleration/Percentile/OI-Market-Cap/完整 Divergence | 未实现；只有部分规则用价格和 OI 条件。 |
| Funding Mean/Z-score/Percentile/Trend | 未实现；当前费率阈值不是完整 Funding Engine。 |
| Spot Driven/Leverage Overheat/Squeeze Candidate | 存在固定条件的研究规则；没有证明分类准确性、提前量或投资有效性。 |
| Spot/Perp CVD 与背离 | 窗口特征部分实现，完整连续累计和背离检测未实现。 |

## 本轮现货扩容与取消项

| 后续要求 | 实现状态与验收边界 |
| --- | --- |
| BTC 固定 + 默认 99 个成交额排序的 USDT 现货 | 已实现；实读配置为 max_altcoins=99。名单不排斥上市 Meme；排除稳定币和杠杆代币。100 个真实在线覆盖尚待验证。 |
| 每轮价格/成交量；详细数据分批 | 已实现每轮价格/24h 成交额、BTC 详细数据与 20 个其他标的轮换。逐笔成交量/多周期成交量未因此自动完成。 |
| 实际数量、时间、缺失、观察限制 | API 和页面有本轮 coverage、报价/详细更新时间、质量缺项；未轮到或失败时不套用旧指标。缺详细数据的报价仍会归档。 |
| DEX/GoPlus 停新采集、分析和提醒，保留历史 | 已实现关闭采集路径、停止新规则/失效/前瞻结果处理、归档视图；旧数据仍能读取。 |
| 调整就绪检查 | /ready 改为现货源、目标数量新鲜报价和 BTC 可用数据；不再依赖 DEX。它不是完整 V3 的就绪检查。 |
| GitHub 审阅 | PR #1 已发布、CI 成功，未合并。 |
| Telegram | 用户已取消，未接入，不列入开发优先级。 |

## 实际复核证据与已发现问题

1. GitHub PR 的两项 `test` 检查均 SUCCESS；本次没有把测试数当需求验收数，也没有重复执行与文档核对无关的全套测试。
2. 本次只读打开现有 SQLite：14,370 条 raw_observations，8,504 条 snapshots，248 条 signals，375 条 outcomes，522 条 regimes。历史中有 BTC/ALT/MEME，说明旧归档仍在。375 条 outcomes 是已归档的时间窗测量数量，不能解释为 375 次独立盈利验证。
3. 配置实读：100 个目标、detail_batch_size=20、poll_seconds=120、futures_source=okx。并未改动 .env、代理、数据库或应用代码。
4. **轮换与 OI 5m 的采样冲突已复现。** 默认 99 个山寨币、每轮 20 个，大约 5 轮才采一次详细数据；每轮至少等待 120 秒，总间隔约 10 分钟或更长。OKX 从本地读数选距 5 分钟目标不超过 150 秒的参照点，这种间隔通常没有合格参照。独立临时数据库中对同一标的按第 0、10、20、30 分钟采样，四次 `oi_change_5m_pct` 均 None，`complete_derivatives` 均 False。当前规则要求 OI 风险核实才能形成入场候选，因此这不能按“完整 OI 与机会发现能力”验收。需要后续另行修复采样策略；本次仅记录问题。
5. **报价前瞻验证的覆盖有待改进。** `measure_signal` 只使用 `usable` 的快照，而轮换外的最新报价带 INSUFFICIENT_CANDLE_HISTORY。有效报价也会被排除出前瞻测量。默认山寨币详细间隔又可能超过 stale_seconds=300，导致覆盖统计稀疏；应区分“报价可用于收益测量”与“指标可用于决策”。
6. SQLite 能保存原始响应、快照和提醒，但无生产规模存储/备份/恢复验收。`derivatives_oi` 短窗口表会清理超过 6 小时的读数，不能直接当完整长期 OI 数据库；原始响应另行保留。
7. **归档重放不等于完整历史回测。** replay 对当时已存快照应用当前规则，不重建采集前的盘口、逐笔成交、宏观、链上状态；无样本外有效性证明、指标/阈值优化、动态权重或自动升级核心信号。
8. 旧文档仍有历史 DEX 描述和原来的规则版本。当前规则入口实际为 `rules-v2-spot`；核对以当前代码和本表为准。取消项只保留历史说明，不应从旧路线图重新启用。

## 代码与测试入口

- [配置与默认范围](../src/muse_btc/config.py)、[现货/衍生品适配与轮换](../src/muse_btc/providers.py)。
- [可计算特征](../src/muse_btc/features.py)、[实际输出字段](../src/muse_btc/models.py)、[当前规则与 BTC 联动](../src/muse_btc/rules.py)。
- [采集与禁用状态](../src/muse_btc/service.py)、[页面 API 与就绪](../src/muse_btc/api.py)。
- [时间点/原始证据存储](../src/muse_btc/storage.py)、[前瞻测量和归档重放](../src/muse_btc/validation.py)。
- [100 对和 DEX 停用测试](../tests/test_spot_universe.py)、[特征测试](../tests/test_features.py)、[规则测试](../tests/test_rules.py)、[存储与验证测试](../tests/test_storage_validation.py)、[决策时间屏障](../tests/test_replay_barrier.py)。

## 建议的后续实施顺序

1. 修复轮换与 OI、收益测量覆盖问题；用户处理出口后验证真实 100 对、采集周期、缺项、限流及长期运行。修复前不扩大已完成声明。
2. 补齐现货多周期与所需成交数据、衍生品历史统计和现货/合约结构分类；挂单行为与清算需要真实增量流及断线恢复。
3. 实现宏观、研究数据的首次可用时间、发布时间和修订模型，再加入全市场状态与 BTC 评分、现货风险联动。
4. 建立足量、可复核的历史样本与滚动样本外验证，再评估提前量、误报率、成本和参数/权重优化。
5. 补全未收到的原始章节后继续逐项验收。不得自行补写未收到的需求，DEX/GoPlus 与 Telegram 仍按已取消处理。
