# awesome-muse-connectors 对本项目的适用性

评估日期：2026-10-03。已读取[仓库说明](https://github.com/anil-matcha/awesome-muse-connectors)、[连接器目录](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/README.md)，以及下面 8 个连接器的 SKILL.md；另检查 Telegram、NewsAPI、Sentry 的 Python 脚本。仅作源码与文档评估，未安装、授权或调用这些服务。

这是一份为 Meta Muse 整理的社区技能目录，与本项目 muse-btc 名称相似，但不是本项目的插件系统。检查过的 Python CLI 依赖 `/opt/hatch/skills/skill-creator/bin/dynamic_credentials` 和 Muse 的凭据替换运行时，不能直接复制到当前 FastAPI 服务运行。适合借鉴公开 API 和接口设计，重新实现符合本项目配置、脱敏、归档及异步任务机制的适配器。

| 连接器 | 项目用途与优先级 | 文档所述成熟度及限制 |
| --- | --- | --- |
| [Telegram](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/telegram/SKILL.md) | 高：手机提醒，解决当前仅浏览器通知的限制 | Draft，未端到端实测。需 Bot token、指定 chat ID，用户先启动 bot。新增持久化发送队列、冷却、去重、重试和发送状态；token 在 URL 路径中，必须从日志及原始归档中脱敏。 |
| [NewsAPI](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/newsapi/SKILL.md) | 中：BTC/交易所公告/监管新闻侧栏与日报 | Draft。目录列明免费开发额度 100 次/日；不适合每 120 秒采集，生产权益和时效须另核验。脚本输出标题、来源与 URL，不代表完整报道正文可得。保留发表时间与首次采集时间，新闻不能单独触发入场。 |
| [Exa](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/exa/SKILL.md) | 中：研究报告和官方公告检索；与 Tavily 等搜索源择一 | Draft，需要 API key。可检索页面文本，适合低频研究；检索结果须回查原文，不能替代行情和真实链上资金流。 |
| [Sentry](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/sentry/SKILL.md) | 中：长期运行后的错误归因与告警 | Draft；该技能读取/处理已有 Sentry issue，本身不采集应用异常。先需集成 Sentry SDK 并脱敏，采集失败比例/延迟仍应由本项目统计。技能针对 sentry.io，不覆盖欧盟或自建域名。 |
| [GitHub](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/github/SKILL.md) | 低：开发 issue 管理 | Draft，仅 profile/repos/issues/create-issue，未提供 PR/CI 完整流程。当前 git + gh 已满足发布与审阅，不必再引入其 classic PAT 权限。 |
| [Slack](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/slack/SKILL.md) | 视团队需求：替代 Telegram 的团队频道推送 | 作者标注 Live-tested，但私有频道权限未实测；非本项目验证。只发提醒可使用更窄的凭据范围，不需要读完整频道历史。 |
| [Resend](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/resend/SKILL.md) | 低：日报邮件备选 | Draft，需验证发件域名。该技能逐封确认的流程不适合无人值守提醒；若开发应用级邮件发送，应另外明确订阅、收件人、频率和发送授权。 |
| [X](https://github.com/anil-matcha/awesome-muse-connectors/blob/main/connectors/x/SKILL.md) | 后置：低频舆情研究 | Draft，读取付费，价格需核验；当前技能还申请发帖/私信等范围。项目只需读取，且必须考虑刷量、机器人和宣传偏差，不应作为交易信号的独立依据。 |

建议顺序：先完成当前 100 对现货的真实覆盖和长期运行验证，再接 Telegram；随后选择一个新闻/研究源，长期运行时补 Sentry。现阶段不批量安装目录，不恢复 DEX/GoPlus，也不接入支付、券商或自动交易功能。目录中没有可替代当前 Binance/OKX 行情、K 线、盘口和 OI 适配器的专用连接器，也无法解决 Binance 出口 IP 限制。
