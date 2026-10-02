# 数据源契约与限制

以下适配器已有接口契约测试。BTC/山寨的 Binance 官方只读现货、DEX Screener 公开池子数据与 GoPlus 公开 EVM 检查已取得真实响应；Binance 合约在当前云出口返回 451，因此 **合约在线验证未完成**。不能用离线接口样本替代在线验证。

| 来源 | 路径 | 用途 |
| --- | --- | --- |
| Binance Spot | `/api/v3/exchangeInfo` | 当前交易状态和 USDT 现货宇宙 |
| Binance Spot | `/api/v3/ticker/24hr` | 当前报价、24h 成交量、ticker 时间 |
| Binance Spot | `/api/v3/klines`，1m / 180 | 已收盘价格、成交量和主动成交方向 |
| Binance Spot | `/api/v3/depth`，100 档 | 顶部价差与采样深度 |
| Binance USD-M | `/fapi/v1/premiumIndex` | Funding、Mark/Index、Basis |
| Binance USD-M | `/futures/data/openInterestHist`，5m / 30 | OI 变化和短期 Z-score |
| Binance USD-M | `/fapi/v1/klines`，1m / 30 | 合约成交方向与窗口 CVD |
| DEX Screener | `/token-profiles/latest/v1` | 宣传/资料更新候选发现，不是全量新池事件流 |
| DEX Screener | `/token-pairs/v1/{chain}/{address}` | 公开池子价格、流动性、成交和池龄 |
| GoPlus | `/api/v1/token_security/{chain_id}` | EVM 部分合约、持仓与 LP 风险检查 |

所有 HTTP 请求保持 TLS 验证，并使用云平台的 CA 信任设置。代理失败、限流、非 JSON、格式错误都进入源状态；不补造价格。未上市合约或不支持的标的保留现货监控，衍生品字段为未知。

## 需进一步核实

- 各接口当前限流、地区限制和历史保留范围，以服务方最新文档为准。
- 深度快照不是增量订单簿；不能判断挂单持续时间、撤单归因或真实挂单意图。
- 没有完整清算流，逼空只能作为假设。
- DEX quote 缺少可靠市场时间，观察时间不等于成交时间。
- GoPlus 的 `holders` 列表不是完整实体识别，剔除合约地址后得到的集中度也可能低估关联持仓。
- 安全接口字段缺失不代表风险不存在。当前支持 Ethereum、Base、BSC、Arbitrum 的公开 EVM 检查入口；默认候选链为 Ethereum、Base、Solana。
- Solana 的 mint/freeze 权限、账户集中度、Token-2022 扩展权限和 LP 风险尚未接入。

## 官方参考

- Binance Spot：[官方文档](https://developers.binance.com/docs/binance-spot-api-docs)
- Binance Futures：[官方文档](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)
- DEX Screener：[API reference](https://docs.dexscreener.com/api/reference)
- GoPlus：[API reference](https://docs.gopluslabs.io/reference)

联网核验后，应记录接口响应差异、实际缺项和限流状态，增加必要的契约测试并升级适配器；不得通过关闭 TLS 或伪造字段让检查通过。
