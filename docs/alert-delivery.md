# Muse 告警推送接入与 2026-10-04 运维问题修复

新版评分分离、同币跨页合并及发送回执见 [策略运行与推送接入](strategy-observability.md)。新版流程先排空整个批次再合并发送，以下按单条通知的处理方式适用于未启用合并的旧消费者。

部署本次修复后，Muse 推送侧需要切换到增量通知接口。旧的“取最近 50 条、按告警 id 去重”会遗漏较早的记录，也无法区分同一告警的后续升级。项目提供通知记录和可发送状态；实际邮件发送仍由 Muse 的既有连接负责。

## 增量读取

首次请求（继续使用现有 Bearer 口令）：

```http
GET /api/alerts/notifications?after=0&limit=50
Authorization: Bearer YOUR_EXISTING_TOKEN
```

返回对象含 `generation`、`items`、`next_cursor`、`upper_cursor`、`has_more`、`as_of`。`items` 按 `sequence` 从小到大排列，只归档 STRONG / CRITICAL_RISK 的首次出现、升级或显著证据修订。每次修订有唯一 `notification_id`；证据中的数值小幅刷新、已读、置顶、解决操作不会生成推送记录。

Muse 的消费流程：

1. 首次从 `after=0` 开始，保存返回的 `generation` 和本轮 `upper_cursor`。无需把最近 50 条当作基线而丢弃旧记录。
2. 逐条处理每个 item。按 `notification_id` 查询持久化的已发送记录，检查 `delivery_status`；`READY` 才进入常规发送流程。发送内容需注明级别、通知时间、升级价格、通知价格、当前价格、价格变化和数据是否新鲜。
3. 实际发送成功、确认此前已发送或完成明确跳过后，将该 item 的 `sequence` 保存为游标。发送失败或结果不明时停止推进；重试同一记录。不要提前把游标更新到 `upper_cursor`。
4. `has_more=true` 时立即读取下一页：传 `after=next_cursor&through=upper_cursor&generation=GENERATION&limit=50`，直至排空。本轮期间新产生的记录留到下一轮；分页上界保证读取过程中不会不断追逐新增记录。
5. 下一轮只传保存的 `after`、`generation` 和 `limit`，让服务端给出新的 `upper_cursor`。空页保持游标不变。HTTP 失败保留原游标并退避重试；不要重置水位。
6. 游标与已发送 ID 必须保存到云端持久卷或数据库，随 Muse 推送程序重启恢复。`generation` 不匹配、游标超过数据库上界或续传未提供 generation 返回 **409**。核查数据库是否被替换或恢复，再从 0 对账读取，沿用已发送 ID 去重。

服务端告警、审计事件、不可变通知载荷在同一个 SQLite 事务中提交。告警 `id` 在未解决且未过期的 `(asset_id, rule_id)` 生命周期内保持稳定，解决或过期后重新触发使用新 id；通知修订的 `notification_id` 始终独立，即使两次变化发生在同一秒。

这里支持可续传、至少一次读取。邮件发送与外部水位不是同一个事务；如果 Muse 的发送通道支持幂等键，应传 `notification_id`。通道不支持时，“邮件已发出但水位尚未保存”的故障窗口仍可能重复发送，不能声称严格只发送一次。

## 价格与有效性

| 字段 | 含义 |
| --- | --- |
| `first_seen` / `first_price` | 当前告警生命周期首次发现的时间与参考价格 |
| `escalated_at` / `escalated_price` / `escalated_level` | 最近一次向更高级别升级的时间、参考价格与目标级别；首次直接出现为 STRONG / CRITICAL_RISK 也记录 |
| `notification_at` / `notification_price` | 本次通知修订的时间与参考价格；后续普通价格刷新不覆盖它们 |
| `notification_from_level` | 本次修订前的级别，首次发现为 null |
| `notification_price_market_time` / `notification_price_available_at` | 参考快照的市场时间与系统获取时间；通知时间是决策时间，不能当作逐笔成交时间 |
| `current_price` / `current_price_market_time` | 读取时最近的可用报价；报价过期时价格为 null |
| `price_change_since_notification_pct` | 当前价格相对本次通知价格的涨幅；缺少任一价格则为 null |
| `notification_age_seconds` / `notification_expires_at` | 通知年龄与有效期限 |
| `data_current` / `current_alert_state` / `current_level` | 读取时的证据新鲜度、告警状态与级别；不会改写通知归档中的旧级别与价格 |
| `delivery_status` | 本次读取的发送判断，取值见下表 |

| `delivery_status` | Muse 处理 |
| --- | --- |
| `READY` | 可按已有渠道发送观察提醒。CRITICAL_RISK 可以说明已发生的风险；`data_current=false` 必须明确标注，不能当作当前可交易报价 |
| `LEGACY_REVIEW_REQUIRED` | 旧库回补记录，先人工对账 / 单独说明历史风险，不伪装成新鲜 STRONG |
| `SKIP_SUPERSEDED` / `SKIP_LEVEL_CHANGED` | 新修订或级别已覆盖该通知，保留记录并推进游标 |
| `SKIP_RESOLVED` / `SKIP_EXPIRED` / `SKIP_MISSING` | 已解决、过期或告警不存在，记录跳过并推进 |
| `SKIP_STALE_DATA` | STRONG 证据不新鲜或 BTC 当前风险状态不允许，记录跳过并推进 |
| `SKIP_PRICE_EXTENDED` | pre-pump 的当前涨幅已超过追涨限制，记录跳过并推进 |

这些判断是请求时的状态。Muse 应在发送前立即读取，不将 READY 结果缓存到后续轮次；必要时再次请求同一页复核。

升级旧库时，仅回补当前仍在 `web_alerts` 中的 STRONG / CRITICAL_RISK 行，并标注 `price_provenance=LEGACY_UNKNOWN`。历史升级价格 / 通知价格设为 null，不拿最新价格冒充。已经被旧版本覆盖的历次修订无法完整重建；当天漏推清单仍需 Muse 按历史记录人工对账。

## pre-pump 延迟与追涨限制

新增通知保护配置，默认值：

```dotenv
MUSE_PRE_PUMP_CONFIRMATION_SECONDS=300
MUSE_PRE_PUMP_MAX_CHASE_PCT=3
```

同一 `pre-pump-fusion` 告警需要在首次发现后的 5 分钟内确认 STRONG；确认时，相对首次价格的涨幅、当前 5 分钟涨幅和 15 分钟涨幅均不得超过 3%。超时、涨幅过大或旧告警缺少首次价格 / 确认快照时，网页告警最多 SETUP，保留 `requested_level`、`confirmation_status` 和原因。CRITICAL_RISK 不受看涨通知限制。

确认窗口以当前告警生命周期为单位，普通刷新不延长；通知的有效期也不得越过这个窗口。已解决 / 过期后重新触发才开启新生命周期。读取通知时，再检查当前价格相对首次价格和通知价格的涨幅，防止升级时价格尚可、发送时已经大涨。

网页列表、详情和 overview 也在读取时检查同样的窗口与涨幅，违反限制则返回 `state=PAUSED`、`unread=false` 与 `delivery_guard` 原因，即使采集尚在 FETCHING、还未重新发布 SETUP。这防止浏览器将过期的 STRONG 再次提醒。

这是尚未校准的**通知保护策略**，可能减少提醒；不代表提高收益。原始策略 Signal、回放和验证结果保持原语义，不能用已有回测评价这项投递保护的收益。该告警的 `validation_status` 仍为 OBSERVATION_ONLY。

**Muse 推送侧需将读取间隔从 15 分钟改为建议 60 秒或更短。** 15 分钟轮询无法及时消费 5 分钟信号；过期 STRONG 会明确跳过。市场采集默认仍为 120 秒，这也限制了发现速度。本次代码没有更改 Muse 云端任务或创建 Codex 定时任务。

## 查询性能与重启

`/api/alerts` 继续返回列表，新增真正的 `limit`（默认 200，最大 1000）、`offset`、`since`（必须含时区，按最近通知修订时间、含边界）以及数据库侧过滤。`since` 是兼容的当前状态查询，会合并同一告警的历史变化；可靠推送应使用 notifications 接口。

列表查询先在 SQL 中限定数量，批量读取对应快照；每个请求最多计算一次 BTC 背景。获取最新报价按所需资产走索引取最近一条，避免读取整段历史快照。网页 overview 最多展示最近 200 条告警；完整推送记录由增量接口读取。接口不获取外部行情，不等待采集锁，不缓存并伪装过期数据。

通知历史和 generation 保存在现有 SQLite 中；数据库及 Muse 消费水位留在持久卷上，VM 替换后可续传。已有行情快照、原始数据与 Universe 也保存在数据库。平台整体替换 VM、重启后尚未完成的新行情采集及进程内缓存失效仍需 Muse 运维处理；本次没有声称消除其约 10 分钟盲区。

## 更新与云端验收

合并本次 PR 后：检查云端 `git status`，备份 `.env` 与 SQLite，再快进更新 `main`；重新运行 `bash scripts/install.sh` 安装代码和添加表 / 索引，按原部署方式重启。保持单个市场采集实例，保留现有邮件连接、代理和密钥。

本地回归覆盖：超过 50 条分页、固定读取上界期间新增记录、重启恢复、同秒修订、读 / 置顶去重、告警 / 事件 / 通知事务回滚、旧库回补、换库 / 恢复冲突、延迟确认、发送时追涨、鉴权及 FETCHING 与 WAL 写入期间查询。全部使用离线数据；不证明云端时延、真实邮件送达或交易效果。

Muse 云端还需验收：替换推送查询和去重规则，持久保存水位；分多页补读并对账当天 15 条漏推；模拟发送失败后重试与 VM 重启；在 FETCHING 阶段测接口延迟。PUMP 的旧案例只作为迟到案例复核，历史价格未知就注明未知。
