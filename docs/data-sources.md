# 数据源说明（V4）

当前来源和范围见 [DATA_SOURCE_MATRIX.md](../DATA_SOURCE_MATRIX.md) 与 [OKX_CAPABILITY_MATRIX.md](../OKX_CAPABILITY_MATRIX.md)。

币安官方只读现货：exchangeInfo、ticker/24hr、klines、aggTrades、depth。OKX 官方公共合约：元数据、OI/Mark、Funding 和历史、5m OI、USD 主动买卖、账户/大户比例、指数、盘口、成交及 K 线。记录源时间、接收时间、原始响应与缺失状态；不请求币安合约，不用模拟行情填补生产数据。

HTTP 451/403、限流或断网时显示实际状态。配置文件和代理出口保留，币安 IP 由用户处理。DEX/GoPlus 只保留旧历史；宏观/研究/邮箱为 Phase 2，链上为 Phase 4，社交为 Phase 5。当前没有 Telegram 或邮件发送路径。
