# 策略覆盖、评分与 Muse 推送接入

同一 GIGGLE 的 `spot-led-momentum` 与 `pre-pump-fusion` 曾同时显示机会排行 81.2，风险提醒又使用同一个排行分。这不能证明独立策略确认，也不能按分数衡量风险。本次把分数含义、逐规则运行状态、BTC 上下文观察及发送回执明确分开。

## 评分与同币合并

| 字段 | 使用方式 |
| --- | --- |
| `opportunity_score` | 当轮横向机会排行；同币合并不相加 |
| `rule_evidence_score` | 各规则自己的描述分；未校准，不是概率，跨规则不可直接比较 |
| `risk_level` / `risk_type` | 风险独立展示；失效通知标注 `SIGNAL_INVALIDATED` |
| `score` | 兼容别名：机会提醒取规则证据分，风险及上下文观察为 null；新消费者使用上面字段 |
| `original_signal_evidence_score` | 失效通知保留的原信号证据分；不是当前风险评分 |
| `patterns` | 实际匹配的 A–F 子模式。属于一条融合规则，不能算六套独立策略 |

旧载荷返回 `score_schema=LEGACY_UNSEPARATED` 和 `legacy_score`，不猜测旧分数含义。历史归档保留原样。网页把同币、同状态、同观察期限的机会条件合并，保留各规则详情；风险和 BTC 研究观察独立显示。

## 查看哪些规则运行了

网页“扩展研究 → 策略运行诊断”，或者 `GET /api/strategies?asset_id=binance:GIGGLEUSDT&limit=100`。不传资产时默认最多 100 行，可设置最多 5000；扩展页面读取最多 1500 行。

每轮主采集保存四个基础规则、融合规则及适用的 A–F 子模式、BTC 的四项上下文观察。结果包括未启用、不适用、数据缺失、未触发、匹配、触发观察/候选/风险。输入中缺失与过期值保持 null。每行有快照、规则版本、信号、告警、通知 ID、冷却状态及发布原因。

发布阶段区分实际级别、BTC 风险门控、pre-pump 确认保护、冷却期。强提醒进入通知队列之后为 `AWAITING_RECEIPT`；有 Muse 回执才显示 `SENT` / `FAILED` / `SKIPPED`。`SENT` 是 Muse 报告已发送，不是独立证明邮件送达。

每资产/规则只保存最近评估；批次计数保留历史。没有完成新批次的资产或超过两轮采集时间的评估标为 `NOT_EVALUATED_IN_CURRENT_BATCH`，不会把旧结果说成本轮运行。诊断是观察行情规则的执行记录；研究检查、社交、Meme 和模型训练继续在已有任务状态/各模块查看，并未变成自动入场策略。

## BTC 新增的观察验证

| 观察规则 | 条件、时效与验证期限 |
| --- | --- |
| `btc-macro-stress` | WALCL−TGA−RRP 最近变化为负，同时最近五个已公布 ETF 观测净流出；24h。保留已有 CAUTION 门控 |
| `btc-mvrv-elevated` | Coin Metrics MVRV 最近值达到至多 365 个已知日样本的 90 分位；至少 30 个不同日样本、最新值不超过 3 天；24h |
| `btc-holder-loss` | 同一天的历史 SOPR < 1，且当前 BTC 价格低于该日短持者实现价格；两项历史数据不超过 10 天；24h。免费源约延迟 7 天，明确标注历史背景 |
| `btc-options-volatility` | Deribit 7–30 天到期、行权价距标的 5% 内，至少四项包含看涨及看跌；IV 中位数 ≥ 80%，快照不超过两倍期权刷新间隔；4h |

观察要求新鲜 BTC 行情，只生成 WATCH、单个上下文证据组、不设入场价、不输出规则证据分、不进入 STRONG/CRITICAL_RISK 队列。来源 ID、来源时点、可用时点、输入阈值与局限随信号保存。MVRV 保留所有样本 ID，展示最新来源时点。后续复用现有多期限验证，记录 BTC 涨跌、最大有利/不利幅度及样本间隔；观察结果不计算入场收益，不自动推广成交易策略。

日级估值高分位、历史 SOPR 以及单市场 IV 都不能证明当下价格方向。两项默认阈值是待检验假设。缺数据、不足样本、来源过期、日期不匹配或未来才可用时，明确显示缺失。配置和来源可用时间进入共享实时/回放入口，回放不会写入运行诊断。

```dotenv
MUSE_ENABLE_CONTEXT_OBSERVATIONS=true
MUSE_CONTEXT_MVRV_PERCENTILE=90
MUSE_CONTEXT_OPTIONS_IV_PCT=80
```

此开关只评估已归档数据，依赖 `MUSE_ENABLE_INTELLIGENCE=true`；不启动采集、研究检查、云端任务或邮件通道。

## Muse 推送程序需要采用的新流程

先排空固定上界的整个通知批次，再合并。同币相关规则可能分布在不同页；`delivery_groups_preview` 只是当前页预览，禁止按页预览直接发送。仓库提供只读帮助函数：

```python
from muse_btc.delivery import fetch_notification_batch

# client 是带 base_url、已有 API token 和合理超时的 httpx.AsyncClient。
# after/generation 是 Muse 在持久卷保存的水位；第一次 after=0, generation=None。
batch = await fetch_notification_batch(client, after=after, generation=generation)
for group in batch["delivery_groups"]:
    # 先把组及所有成员 ID 写入 Muse 自己的持久发送队列。
    # 使用 delivery_id 作通道幂等键；成员分数逐条展示，不求和。
    # 发送前复核成员状态/截止时间；再调用已经接好的邮件通道。
    # 成功后 POST /api/alerts/notifications/receipts，报告所有成功发送的成员。
    pass
# 仅当每条通知已发送或明确处理跳过后，才持久保存 next_cursor/generation。
```

该函数最多排空 200 页；任何 HTTP/游标错误都抛出，不提交水位。风险逐条保留，机会按资产及观察期限合并，`member_notification_ids` 包含该组的全部成员，`independent_strategy_count=null`。已 SENT/SKIPPED 的成员不会重新进入发送组；FAILED 可以重试。

回执请求示例：

```http
POST /api/alerts/notifications/receipts
Authorization: Bearer <已有 token>
Content-Type: application/json

{"notification_ids":["成员通知ID1","成员通知ID2"],"status":"SENT","message_id":"邮件通道返回ID"}
```

发送失败上报 FAILED 和原因，并保留待重试组。确定跳过才上报 SKIPPED；旧库人工对账要求不能静默跳过。成功发送后回执请求失败，应使用本地持久队列重试回执，不能把已发组再次发邮件。SENT 回执不会被迟到的失败覆盖；整组包含未知 ID 时全部拒绝。发送邮件与保存队列/回执仍不属于同一个事务，通道不支持幂等时仍存在故障重复窗口。

合并 PR、更新安装并重启后，Muse 需要修改现有邮件程序使用上述字段、批次合并和回执；本项目不会自行改写 Muse 的外部邮件程序。云端验收检查一个完整行情批次的诊断、跨页同币合并、发送失败/回执失败重试、过期信号跳过、重启后的水位与回执。研究定时任务保持原状态。
