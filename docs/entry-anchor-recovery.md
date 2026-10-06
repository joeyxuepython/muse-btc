# Muse 指引：entry-quality 历史锚点恢复

更新日期：2026-10-06。对应 PR 合并后部署，云端验收由 Muse 执行。

## 已核实的问题

原 `entry-quality-v1` 将当前数据新鲜度和历史确认的时间上限都绑定到 `stale_seconds`，默认 300 秒；确认窗口内的所有成员还必须相对当前时刻新鲜。停机十分钟后的旧快照虽然仍在数据库，却不能继续当 anchor。

本地合成归档复现：停机十分钟后第一条合格新观测为 WAIT，120 秒后第二条合格观测可 PASS；没有重启、但每 325 秒才有独立观测也会持续 WAIT。因此窗口限制真实存在，但云端零入场还必须结合每币刷新间隔、缓存源时间和 failure_codes 判断。

## v2 行为和参数

```dotenv
MUSE_STALE_SECONDS=300
MUSE_ENTRY_CONFIRMATION_SECONDS=60
MUSE_ENTRY_ANCHOR_MAX_AGE_SECONDS=1800
```

- 最新快照继续按当前时刻检查报价、盘口和 K 线的新鲜度、对齐与质量。过期数据不能靠旧 anchor 放行。
- 历史成员按各自 `available_at` 检查当时是否新鲜、源时间是否对齐、组件是否已收到。采集时已经过期、使用未来信息或不合法的成员不能确认当前候选。
- 历史查询回看默认 1800 秒；新老盘口、K 线源时间差都必须处于 `[entry_confirmation_seconds, entry_anchor_max_age_seconds]`。取最近的合格跨度，并保留其间同来源的全部观测；不能跨过弱买盘、薄盘口、缺失或无效输入寻找有利样本。
- 最小源时间差硬限定至少 60 秒，最大 anchor 参数允许 60～3600 秒，且不能小于确认下限。旧 `.env` 若配置 `MUSE_ENTRY_CONFIRMATION_SECONDS=30`，需显式改为至少 60；非法配置会报错，不自动替换。
- 复用历史 anchor 只延长入场证据的回看范围。“买入支撑消失”的候选撤销仍要求两次相对当前时刻新鲜的弱买盘观测；价格失效、BTC 风险等原检查保持生效。

版本为 `entry-quality-v2`，新参数进入质量 policy_id、信号 rule_version 和 decision_configs 归档。新旧结果分版本评估，已有信号、快照和 Outcome 不回写。仍使用数据库快照，无内存恢复状态、无新增状态表、无数据迁移。

重放使用归档参数；旧 decision_configs 没有新字段时，anchor 上限沿用其归档 `stale_seconds`，不会静默变成今天的 1800 秒。旧归档若记录 30 秒确认，v2 回放也执行至少 60 秒的下限，原始配置不回写。重放执行当前 v2 代码，并非完整复现过去 v1 的可执行实现。

## 断档的含义与展示

默认 1800 秒是可配置的工程设计，没有证明它是最佳确认跨度或能改善收益。旧、新两个支持样本不等于停机期间买盘持续成立；没有观测到反证，不等于没有反证。

`/api/quality`、`muse quality-report` 和信号 decision 中的 entry_quality 提供：

| 字段 | 含义 |
| --- | --- |
| `maximum_age_seconds` | 当前输入的新鲜度上限，默认 300 秒 |
| `anchor_max_age_seconds` | 历史回看及新老组件间隔上限，默认 1800 秒 |
| `anchor_snapshot_id` / `anchor_age_seconds` | 选中的旧快照及其接收时点距当前的年龄 |
| `snapshot_ids` | 全部参与判定的成员，包括中间反证 |
| `max_observation_gap_seconds` | 窗口内相邻快照的最大接收时间间隔 |
| `max_component_gap_seconds` | 相邻观测的盘口/K 线最大源时间间隔，缓存不制造新源时点 |
| `has_observation_gap` / `observation_continuity` | 任一上述最大间隔超过当前 freshness 上限时标为 GAPPED；没有确认窗口为 UNCONFIRMED |
| `failure_codes` | 等待原因；有 anchor 不代表成交、深度、追涨和当前行情检查全部通过 |

未超过 freshness 上限只标为 `WITHIN_FRESHNESS_WINDOW`，不声称连续监测。网页和中文正文显示锚点年龄、最大间隔和断档说明；市场共振也保存各成员最大采样间隔，默认仍为 observe，不能解读为连续同期资金流。

## Muse 更新及云端验收

1. PR 合并后按[云端运行指引](cloud-run.md)备份数据库、检查本地修改，更新 main、重新安装并重启。保留原 `.env`、数据库、邮件连接和通知回执；研究定时监控保持暂停。
2. 核对 `/health.entry_quality_version=entry-quality-v2`，新参数默认为 1800。需要缩短回看时显式调整新参数，不提高当前 `MUSE_STALE_SECONDS` 来补偿断档。
3. 保存一个合格旧快照的 ID、源时间和当时质量结果。停服务十分钟后恢复，在首次取得合格新快照时检查 snapshot_ids 是否能包含停机前 anchor，确认断档字段与实际时间一致。
4. 验收目标是：**在最新数据和旧 anchor 均合格、源时间跨度合法、已有中间观测无反证且其他检查通过时，恢复后五分钟内可以复用旧快照转为 PASS**。真实行情条件不满足时仍可 WAIT；不能强制制造入场候选作为验收。
5. 对照验证：当前行情过期、anchor 当时已过期/未对齐、缓存 book 或 candles 未产生至少 60 秒源时间差，以及中间弱买盘/缺失/薄盘口/无效输入，都不能被绕过。
6. 保存配置、代码提交号、每币源时点刷新间隔、failure_codes、采集 phase_seconds 和样例通知。回看历史扩大后读取更多归档，需观察云端处理耗时；若仍 WAIT，按原因排查，不能全部归因于重启。

本地验证记录见[检查结果](verification/entry-anchor-recovery.md)。恢复能力与代码正确性不证明交易优势；样本外及公平对照仍需按研究协议另行检验。
