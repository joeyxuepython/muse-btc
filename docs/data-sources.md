# 数据源说明（V4）

当前来源和范围见 [DATA_SOURCE_MATRIX.md](../DATA_SOURCE_MATRIX.md) 与 [OKX_CAPABILITY_MATRIX.md](../OKX_CAPABILITY_MATRIX.md)。

币安官方只读现货：exchangeInfo、ticker/24hr、klines、aggTrades、depth。OKX 官方公共合约：元数据、OI/Mark、Funding 和历史、5m OI、USD 主动买卖、账户/大户比例、指数、盘口、成交及 K 线。记录源时间、接收时间、原始响应与缺失状态；不请求币安合约，不用模拟行情填补生产数据。

HTTP 451/403、限流或断网时显示实际状态。配置文件和代理出口保留，币安 IP 由用户处理。Phase 1 的 OI / Funding 等合约来源继续使用 OKX。新增的免费清算归档仅在手动命令中连接 Binance 公共 WebSocket，不请求其合约 REST，不进入自动行情循环。

免费 BTC 数据的接口、口径、额度和云端运行步骤见 [free-btc-data.md](free-btc-data.md)。宏观、稳定币、ETF 和研究按需采集；没有 Telegram 或邮件发送路径。
