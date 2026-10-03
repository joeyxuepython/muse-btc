# 免费 BTC 数据接入

新增来源只响应 CLI / 网页 / API 的显式请求，不随 `serve` 启动采集、不创建日程、不运行论文研究检查或模型训练。开发验证使用合成响应和少量接口样本，长期采集与数据验收由 Muse 云端完成。

| 来源 | 已接入 | 范围与限制 |
|---|---|---|
| Coin Metrics Community | BTC 日线 MVRV、流通市值、供应量 | 无 Key；默认最近 365 天，可设 8–1460 天；保留供应商口径，缺失指标单独失败 |
| BGeometrics Free | SOPR、STH/LTH SOPR、Realized Price、STH/LTH Realized Price | 六条序列；按官方免费限制只请求截至七天前的历史；无 Token；官方免费历史约四年 |
| Deribit Public | BTC 期权元数据、全链 summary 中的 IV / OI / 买卖与 Mark 价格、限额 ticker Greeks | 单所 BTC 期权；OI 单位 BTC，价格单位逐合约保留；默认最多 12 个合约的 Greeks，其余为空；不是完整历史曲面或做市商净头寸 |
| FRED | 利率、通胀、就业、收益率、美元指数代理、Fed 资产负债表 / TGA / RRP | 沿用无需 Key 的公开 CSV；只归档实际取得的修订版，不回填为历史 vintage |
| DeFiLlama | USDT / USDC / FDUSD / DAI 供应快照和 7/30 日变化 | 沿用免费稳定币接口；缺失比较值为空，不补零 |
| Farside | BTC ETF 已公布每日净流量 | 沿用公开表格适配；未公布或抓取失败保持缺失，未推断持仓和 AUM |
| Binance Public WebSocket | BTCUSDT / ETHUSDT 的 UM 清算快照与监听窗口 | 显式有限时长监听；每币每秒最多一笔；排除 CM 合约；不能当作完整清算量，无历史回补 |

## 云端执行

从 `feat/v4-phase2-6` 分支更新并安装，按 [cloud-run.md](cloud-run.md) 配置数据库和访问口令。无需购买上述 API 或添加付费 Token。

```bash
.venv/bin/muse free-data --scope onchain
.venv/bin/muse free-data --scope options
.venv/bin/muse free-data --scope macro
.venv/bin/muse free-data --scope liquidations --seconds 300
# 一次检查全部来源；清算监听 10 秒，未成功的分项会列出并返回非零退出码。
.venv/bin/muse free-data --scope all
```

也可以使用 `muse intelligence --scope btc`。网页“BTC 免费数据”展示链上指标、期权、清算事件、监听窗口和分项错误；宏观 / 稳定币 / ETF 在“宏观与资金流”展示。数据采集不会调用 `ResearchEngine.check`。

`GET /api/btc` 为归档摘要。`GET /api/btc/onchain/mvrv?source=Coin%20Metrics` 返回该供应商的日线证据；其他指标名称见上表对应 provider 常量。`POST /api/btc/collect` 请求体：

```json
{"scope":"all","liquidation_seconds":10}
```

`scope` 可为 `all / onchain / options / macro / liquidations`；网页/API 监听上限 60 秒，CLI 为 3600 秒。已有 API 口令与同源规则同样生效。

## 缓存、额度与数据语义

- 链上成功结果缓存至少 24 小时，期权默认缓存 300 秒；显示 `CACHED` 并保留原检查时间，不把缓存当成新获取。请求历史天数改变后重新获取。
- BGeometrics 本地额度持久化、事务预留，失败也计数：最多 8 次/滚动小时、15 次/滚动日。六条默认序列一轮六次。其他程序共享同一出口 IP 的请求不在本地计数中；服务端 429 仍记录实际失败。
- `onchain / options / liquidation / liquidation_window` 使用现有只追加证据库，无新 schema 迁移。每个链上日期和来源独立标识，修订保留旧版本，重复值去重，`available_at` 为实际获取时间；原始响应可追溯。
- 不把未来、非有限数值、重复冲突日期、错误资产或无效清算成交字段入库。BGeometrics 分页被截断、缺失指标、ticker 失败等显示分项状态；不会静默补值。
- `DELAYED` 是免费链上时间限制，`STALE` 是观测过旧；期权 snapshot summary 按获取时间归档，ticker 保留交易所报价时间。没有历史链上数据不等于零值。
- 清算使用累计成交数量 `z × ap`，不用原始订单数量 `q` 推算成交。数据没有稳定订单 ID，快照不能相加为全市场清算总额；`full_market_total_usd` 始终为空。没有监听、监听无事件、连接失败是不同状态。
- 数据尚未自动融合进 BTC 风险规则，未补齐项目书全局 13 状态模型；接通接口不代表数据覆盖、交易效果或云端长期运行已验收。

## 原始文档

- [Coin Metrics Community](https://gitbook-docs.coinmetrics.io/packages/coin-metrics-community-data)
- [BGeometrics Free](https://bitcoin-data.com/bguser/free-features.html)、[七日限制](https://bitcoin-data.com/bguser/changelog)
- [Deribit Summary](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency)、[Ticker](https://docs.deribit.com/api-reference/market-data/public-ticker)
- [Binance 公共行情流](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)
- [FRED](https://fred.stlouisfed.org/docs/api/fred/)、[DeFiLlama](https://api-docs.defillama.com/)、[Farside](https://farside.co.uk/btc/)
