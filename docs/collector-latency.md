# 采集延迟与历史验证：Muse 更新指引

2026-10-05 云端诊断显示：单轮约 325 秒，历史验证占 218 秒；102 个币的质量诊断全部 WAIT，盘口完整覆盖不足。此次修复将实时采集与历史评估分开，并改善查询和诊断；不调低策略阈值，不声称获利能力已经提高。

## 代码行为

- 市场轮次完成行情保存、排名与信号判断后即结束，不再等待全部历史期限验证。`market_last_result.validation_status=INDEPENDENT_WORKER`，其中 `outcomes=0` 仅表示该市场轮次不计算结果，不能解读为历史评估停机。
- `serve` 默认启动独立的 `validation-worker-v1`；它只读取归档并计算价格标签，不请求行情、不执行研究检查、不发送邮件。与扩展 `IntelligenceWorker` 使用独立租约。
- 默认每 10 秒处理最多 16 个期限任务，合作式预算 2 秒；预算在任务间检查，不能强制中断正在执行的 SQL。旧信号每批回填最多 64 条，回填游标和任务进度保存到数据库，重启继续。
- 未到期任务不加载配置或价格。到期而终点尚可能到达的任务延期重试；超过原配置的终点容忍窗口仍无数据则标为 MISSING，不再每轮反复扫描。报告继续列出缺失，不能计为零收益。
- `muse validate` 手动处理全部当前可到期任务，可能耗时较长。导入缺失的历史快照后用 `muse validate --retry-missing` 显式重查关闭窗口；保留原始时点，不能用今天的报价补历史终点。
- 增加 `snapshots(available_at)` 时间索引，历史价格标签只投影报价字段，不解析整段 K 线/盘口；同一信号多个到期期限复用价格路径和当时配置。实时生命周期只读取最新状态仍 ACTIVE 的候选，排名背景按批读取。
- 保留归档费用、信号版本、时间点可用性、重复报价去重、断档限制和原始结果；已有结果不重写。所有新增队列表/索引为 schema v4 的增量扩展。

## 升级步骤

1. 保留 `.env`、代理、邮箱连接、游标和云端本地修改。检查 `git status`，创建一致性数据库备份；不要把数据库、密钥和邮件内容提交 GitHub。
2. PR 合并后更新 `main` 并运行 `bash scripts/install.sh`。首次初始化将为现有快照创建时间索引，大库可能需要额外时间和临时磁盘空间；在升级窗口停止旧进程，避免多个实例同时建索引。无需删除历史数据。
3. 在现有 `.env` 核对新增默认参数：

```dotenv
MUSE_ENABLE_BACKGROUND_VALIDATION=true
MUSE_VALIDATION_BATCH_SIZE=16
MUSE_VALIDATION_BUDGET_SECONDS=2
MUSE_VALIDATION_TICK_SECONDS=10
```

4. 按原方式启动 `serve`。如果 Muse 只使用单轮 `muse collect` 的外部调度，另以现有进程管理器运行 `.venv/bin/muse validation-worker`；保持一个独立评估实例。已有实例会通过租约阻止重复工作，不需要创建 Codex 定时任务。
5. 用 `.venv/bin/muse runtime` 检查 `validation_worker` 的心跳、last_batch 和 queue；`worker` 仍是宏观/链上/期权等扩展执行器。研究监控按用户要求保持暂停。

## 盘口刷新预算

默认 `MUSE_DETAIL_BATCH_SIZE=100` 每轮采集 100 个山寨币详情，另加 BTC/ETH。若云端曾调成 20，在 120 秒轮询下估算完整轮转需 600 秒，超出 300 秒质量窗口；不能期望全部目标持续满足两次新观测确认。

先修复处理耗时，再在请求权重与限流允许的情况下由 Muse 显式恢复足够的详情预算。程序不会擅自提高配置预算；既有较小预算继续公平轮换，并报告 `estimated_detail_refresh_seconds` / `confirmation_cadence_feasible`。该估算基于配置轮询周期，不含实际慢轮次和失败，不是覆盖保证。

`/api/quality.collection_plan` 展示配置及最新采集覆盖。CLI quality-report 中每个币展示源数据年龄、允许年龄、确认状态和 failure_codes。市场共振同时报告 required_assets 与 exclusion_reason_counts；默认仍为 observe，不改变入场门控。

`BOOK_NOT_COLLECTED` / `NEAR_DEPTH_MISSING_OR_INVALID` 是数据缺失；`BOOK_STALE_OR_INVALID` 是过期或不合法；`CONFIRMATION_OBSERVATIONS_INSUFFICIENT` 是窗口内不足不同源时点；`TAKER_BUY_INPUTS_MISSING` 是主动买入占比缺失；`CONFIRMED_FLOW_OR_DEPTH_INSUFFICIENT` 才是有效连续观测未满足成交/金额要求。`CHASE_INPUTS_MISSING` 不能当作真实涨幅过高。完整 0.1% 盘口覆盖仍不足时继续 UNKNOWN，不缩小分母制造共振。

## 云端验收

- `/health.collector.validation_worker.version` 应为 `validation-worker-v1`；主轮次 phase_seconds 不再有 VALIDATING。验证停顿时进程存活与市场数据新鲜度分别判断。
- 连续记录至少 10 个主轮次的耗时、轮次间隔及 FETCHING/PERSISTING/EVALUATING，占比和异常均保留。不能把一次快轮次当作长期运行证明。
- `runtime.validation_worker` 应持续更新，队列 due_count 和 oldest_due_at 可判断积压；MISSING 是关闭的缺失终点，PENDING 可能含尚未到期任务。旧库回填期间结果会逐批补充。
- 在足够详情刷新后检查质量确认与各失败原因、真实共振覆盖。不要为了出现入场候选降低 freshness、买入占比或完整盘口要求。
- 保存新的 validation-v2，比较 1h/4h/24h 缺失和断档；已有断档结果不会伪装成改善。真实价格序列修复后，新增版本/时间段的结果应单独评估。

当前只完成本地回归和合成归档验证。云端 7.25 GB 数据库的处理耗时、完整覆盖、邮件送达及策略收益须由 Muse 更新后提供真实结果。

本地测试和规模实验的具体记录见[验证记录](verification/collector-latency.md)。
