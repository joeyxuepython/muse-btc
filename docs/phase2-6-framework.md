# Phase 2–6 可运行框架

本次交付供云端运行与继续接入的工程框架，没有在开发电脑启动真实研究检查、行情采集、社交采集或真实模型训练，没有购买 API 或建立日程。Phase 1 市场服务保留，新增来源采用手动触发或明确配置开关。

## 阶段与实际边界

| 阶段 | 已接通的框架 | 云端后续验收 / 扩展 |
|---|---|---|
| 2 | 研究索引与正文适配器、文档版本与成功检查基线、中文审阅与研究提醒、EML 导入、宏观 / 稳定币 / ETF 适配器与事件反应、成交去重归档、采样 CVD、多档盘口覆盖 | 来源可访问性、动态页面 / 分页、IMAP 与 LLM 提取、历史宏观版本、完整逐笔流、盘口墙 / 撤单、全局 13 状态模型 |
| 3 | 九类横截面分项排名、排名每小时变化、A–F 规则融合、可配置阈值、催化剂 / Tokenomics / 基本面导入、风险冲突与证据变化 | 大样本阈值验证、价格比率时间序列、基本面 / 解锁供应商、完整 Opportunity 模型与校准 |
| 4 | 独立 DEX 队列、四链 Token 标识、批次轮换、GoPlus 可选适配、Discovery 与 Rug 分开、Holder 角色过滤、早期买家名单、钱包 FIFO 历史与关联图 | RPC 全链新币 / 新池监听、Solana 权限与 Token-2022 检查、全量 Holder、失败币留存、钱包 MFE/MAE、样本外 Smart Wallet 身份 |
| 5 | X Recent Search 凭据接口 / 导入、独立作者、提及变化、文本相似度、推广标记、词典叙事、KOL 观点验证 | X 权限与分页、社交覆盖、Bot 校准、KOL Cascade、LLM 叙事 / 观点提取 |
| 6 | 每日首次 UTC 排名 Top 5/10/20、成熟标签与缺失保留、Precision、采样 Lead Time、周误报、按时间隔离的 Logistic 实验、重要性与校准、付费数据评估导入 | 长期真实历史、滚动样本外验证、成交成本、LightGBM/XGBoost/CatBoost、策略晋级与供应商增益复核 |

框架接通不等于阶段全部验收完成。模块明确显示需来源验收、需凭据、历史不足或覆盖不完整；研究模型不能自动成为核心信号。

## 数据与扩展接口

`intelligence_records` 只追加证据版本，包含 `kind / key / source / market_time / available_at / raw_ids / data / version`。输入由 `context.py` 的 Pydantic 模型验证。`available_at` 使用实际导入或获取时间，外部数据不能回填到过去；历史查询只读取当时可见的版本，后来得到的钱包、叙事与宏观修订不能进入旧时点。

`research_checks` 保存成功与失败检查，只有成功推进基线。`research_alerts` 保存中文提醒。`trade_observations` 按市场、资产、成交 ID 去重，保存源时间、获取时间与原始引用。`collection_leases` 防止多进程同时进行研究检查或模型训练。SQLite schema 2 增量升级到 3，旧行情、原始数据与预警保留；升级前备份，降级前恢复备份。

接入供应商时先 `Store.save_raw`，再生成 `EvidenceRecord`。凭据来自环境变量，不进入响应、日志或导出。统一导入 `POST /api/context/import`；邮件 `POST /api/research/import-email` 或 CLI EML 导入。API 的 `/docs` 展示请求模型。

上下文类型：`catalyst / tokenomics / fundamental / macro_event / etf / holder / wallet_trade / wallet_link / social / social_thesis`。链上身份使用 `chain + address`，Solana 保留大小写；钱包关联仅为假设，同区块买入不认定同一主体。缺失不能换算为安全或 Smart Money。

## 研究模板契约

来源：Glassnode Research、Coinbase Institutional Research / Trading Insights、CoinShares Research、arXiv，另保留 V4 Santiment 接入位；忽略 CBT。主题：市场微观结构、链上数据、衍生品头寸、宏观流动性、DEX / Intent / Solver。

1. 首次成功检查安静建立基线；后续标记新发布或实质修改。失败不推进基线，抓取受限、索引不可验证、窗口超过上限均记录失败。
2. 保存文档身份、正文 hash、发表 / 修订 / 获取时间、原站链接与旧版本。重复抓取和轻微修改不重复提醒。
3. 原文是数据，不执行指令。只访问允许的原站域名，逐跳验证重定向并限制大小。新闻 / 营销筛选为初筛，实质增量仍需审阅；未审阅不自动通知。
4. 中文审阅包含标题、核心结论、关键数据 / 方法、交易意义、局限、增量说明与来自存储原文的摘录。审阅后只有新研究 / 实质版本进入 INFO 提醒；重复结论与方法去重。
5. arXiv 默认摘要，显示 `ABSTRACT_ONLY`；不得假装读过全文。动态正文、图表与 PDF 未自动提取。EML 的 From 可伪造，附件仅登记，需对照原站。LLM 提取与语义去重尚需单独接入。

检查只由按钮或 `muse intelligence --scope research` 触发，不进入行情自动循环，没有每日任务。通知仅在网页研究库产生，没有发送邮件、Slack 或 Telegram。

## 宏观与市场口径

FRED 保留观测与获取日期；最新修订不能作为历史 vintage。`DTWEXBGS` 是广义贸易加权美元指数，不是 DXY。CME FedWatch、ISM、MOVE、Gold 与事件共识默认缺失。WALCL / WTREGEN 为百万美元，RRPONTSYD 为十亿美元；净流动性代理 `WALCL - TGA - RRP` 不表示全部可投资资金。四币供应变化均值仅在数据齐备时产生；Mint/Burn 与交易所供应未推断。

ETF 只解析 Date / Total 栏的已公布净流量，破折号保持缺失，不推断持仓或 AUM。事件必须含 release_time、units、vintage、actual，可缺 consensus；发布前不能录入实际值，BTC 反应只取归档附近的采样。

成交分市场归档，OKX 名义金额要求已验证乘数。REST 缺口展示，`full_market_cvd` 保持空；不同市场不硬比较绝对 CVD。0.5/1/2/5% 盘口展示采样深度与完整区间覆盖。

## 实验与付费来源

Logistic 目标是未来截面 Top 10% 收益标签。标签完成时间必须早于下一分区，标准化仅拟合训练集，L2 使用验证集选择，最终重要性与校准在测试集计算。默认至少 200 成熟样本和 10 决策批次；不足返回 `INSUFFICIENT_HISTORY`。全部结果 `EXPERIMENTAL`，不会自动改权重或打开交易。系数权重建议不等于规则最优权重。

付费评估只接受带数据集 hash、匹配样本外区间、至少 100 样本、Precision / Recall / Lead Time / 风险召回 / 误报率的对照；入口不购买服务，导入结果仍需原始数据复核。

## 接口文档

- [DEX Screener](https://docs.dexscreener.com/api/reference)
- [GoPlus](https://docs.gopluslabs.io/reference/token-security-api)
- [FRED](https://fred.stlouisfed.org/docs/api/fred/)
- [DeFiLlama](https://defillama.com/docs/api)
- [arXiv](https://info.arxiv.org/help/api/user-manual.html)
- [Glassnode](https://research.glassnode.com/)
- [Coinbase](https://www.coinbase.com/institutional/research-insights/research)
- [CoinShares](https://coinshares.com/insights/research-data/)

链接用于接口设计，本次未执行真实研究检查。访问与覆盖留待云端验收。
