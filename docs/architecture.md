# 架构说明（V4）

当前实现以 [SYSTEM_ARCHITECTURE.md](../SYSTEM_ARCHITECTURE.md) 为准。最新计划将核心模块调整为 BTC + ETH + 100 个动态山寨币，币安仅采集现货，OKX 采集合约。Phase 1 使用独立适配器、规范资产 ID、不可变名单/排行历史和持久化 Web 预警；SQLite 增量迁移保留旧历史。

详见[数据库](../DATABASE_SCHEMA.md)、[特征契约](../FEATURE_DESIGN.md)及[实施范围与验收](../IMPLEMENTATION_PLAN.md)。旧架构可通过 Git 历史查询。
