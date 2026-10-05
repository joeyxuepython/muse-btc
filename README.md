# MUSE · 加密市场研判与 Web 预警

按[最新 V4 计划](docs/requirements/v4-latest-plan.md)开发：**BTC、ETH 独立监控，加 100 个动态 USDT 山寨币，共 102 个现货标的**。币安提供现货，OKX 提供合约。Phase 1 已实现，V0.2 增加 Phase 2–6 可运行框架；真实来源、凭据、历史样本和后续适配需云端验收。Telegram 已取消。

云端无响应问题的修复与更新步骤见[采集响应性说明](docs/collector-freeze-fix.md)，包括保护 Muse 临时修改、重新安装、健康探针和验收。

最新升级：[云端可靠性、事件与 BTC 综合研判](docs/reliability-upgrade.md)。邮件沿用 Muse 已接好的连接，提供待审阅队列；后台扩展采集默认关闭，待云端显式启用。

新入口：网页“研究与扩展”。[云端运行步骤](docs/cloud-run.md) · [框架和未验收能力](docs/phase2-6-framework.md)。研究检查手动触发、首次只建基线，无新增定时任务。Meme / X 默认关闭，模型不自动晋级。

[免费 BTC 数据](docs/free-btc-data.md)已提供日线 MVRV、延迟 SOPR / 持有人成本、Deribit 期权和采样清算的手动采集、归档与网页入口。云端可执行 `.venv/bin/muse free-data --scope all`；宏观、稳定币和 ETF 同时复用现有免费适配器。

[策略运行与推送接入](docs/strategy-observability.md)说明评分分离、同币合并、A–F 子模式诊断和 Muse 发送回执。BTC 宏观、估值、持有人成本和期权波动率新增 WATCH 观察验证，使用已归档数据，不自动推广为入场策略。

[信号决策解释](docs/decision-explanations.md)说明触发规则、风险门控、缺失/过期数据、研究背景与未参与模块的实际用途，以及 Muse 如何采用完整中文正文。

[最新 Muse 指引：策略质量与效果评估](docs/strategy-quality.md)说明连续确认、盘口覆盖、发送前复核、买盘支撑撤销、市场共振研究对照和分版本去重评估。新增 `/api/quality` 与 `muse quality-report`；准确性提升须用云端真实归档验证。

## 已实现的第一阶段

- Canonical 资产注册表、每日动态名单、固定观察名单、最多 10 次常规替换和 20/30/50 分层。固定名单占用山寨币名额，BTC/ETH 不占用。
- 每轮刷新所有选中币的价格、24h 成交额和 OKX 当前 OI/Mark；BTC/ETH 每轮采集细节，默认每轮覆盖 100 个山寨币细节；较慢的 OKX 统计短暂缓存，保留原始数据时间。可配置较小预算并按最久未更新优先轮换。
- 实际覆盖、报价与细节更新时间、各组件源时间和接收时间、缺失指标及请求健康统计；不完整或不对齐的数据只能进入观察。
- 100 币排行、排名与评分变化、解释评分贡献、BTC 基础风险背景、中文雷达/热图及 Web 预警中心。
- 持久化预警升级历史、已读/未读、固定/解决状态；通过认证的 WebSocket 更新与轮询补充。浏览器通知和声音均由用户主动开启，仅通知 STRONG/CRITICAL_RISK。
- 原始响应、名单、快照、排行和信号归档；前瞻收益、MFE/MAE、采样极值时间及按当时名单重放。
- SQLite 增量迁移、备份接口、Docker/Compose、认证、优雅停止和 CI。

指标评分是固定规则的证据评分，**不是上涨概率**。新规则均为 `OBSERVATION_ONLY`。近期成交样本不能冒充完整连续 CVD；去杠杆假设不能冒充真实清算金额。项目没有自动交易功能。

## 本地启动

需要 Python 3.12 和 uv，开发检查另需 Node.js。

```bash
bash scripts/install.sh
bash scripts/start.sh
```

默认仅监听本机 8000，目标每 120 秒启动一轮，扣除本轮耗时；超过周期时等待本轮完成。原 `.env` 保持优先；如要使用 V4 默认规模，设置 `MUSE_MAX_ALTCOINS=100`。旧值 99 会明确显示目标 101，不静默覆盖已有配置。新安装可参考 [.env.example](.env.example)，请勿提交凭据。

```bash
# 对外监听必须先在 .env 配置 MUSE_API_TOKEN
bash scripts/start.sh --host 0.0.0.0 --port 8000
```

网页要求访问口令，API 使用 `Authorization: Bearer ...`；WebSocket 口令通过首条消息发送。浏览器页面关闭后不会继续发通知；项目没有内置 Telegram、手机推送或外发邮件功能。Muse 的既有邮件输入连接保持不变。

## 容器运行

先配置 `.env` 中的 `MUSE_API_TOKEN`，再运行：

```bash
docker compose up --build -d
```

Compose 默认只发布本机 8000 端口，使用持久化 `muse-data` 卷。它不会自动导入工作区的旧数据库。需要迁移旧数据时先使用 SQLite backup API 备份，再将备份导入卷；不要只复制运行中的主数据库而遗漏 WAL，也不要执行 `down -v` 删除历史。

当前受管理云环境可显式使用 [云容器配置](docker-compose.cloud.yml)：

```bash
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up --build -d
```

该配置把既有代理与 CA 信任传入容器，不更改云代理设置。更多见[本地和云环境启动](docs/local-run.md)。长期稳定性仍须在持续在线实例上观察，短时测试不能证明无人值守运行质量。

## 数据与就绪状态

公开核心采集无需交易所账户凭据。默认币安现货源为官方只读 `data-api.binance.vision`；核心合约指标只请求 `www.okx.com`。旧 `MUSE_FUTURES_SOURCE=binance/auto` 可继续读取，但 V4 核心合约指标实际统一使用 OKX。免费清算模块仅在显式命令中连接 Binance 公共 WebSocket，不进入自动行情采集。

OKX 已按[官方能力矩阵](OKX_CAPABILITY_MATRIX.md)核验公共接口。各币的 API 支持和网络状态不同，缺失数据会显式显示。币安出口/IP 问题仍由用户处理，程序不会以测试样本替代失败采集。

`/health` 表示服务存活；`/ready` 检查全部目标报价和细节是否可用；启用内置 worker 时也检查其心跳。`derivatives_ready` 单独表示完整合约覆盖，不能把 HTTP 200 解读为全部 OKX 指标可用。

默认细节预算为 100；旧 `.env` 的 20 仍会生效，需要显式更新。实际网络和限流可能使轮次超过 120 秒，过期数据不进入强机会信号；配置全覆盖不能替代云端吞吐验收。较慢的 OKX 历史统计可缓存 240 秒，保留源时间和接收时间。

DEX/GoPlus 新采集、分析和提醒在 Phase 1 保持停用，旧历史仍可查看和导出；新计划把链上发现安排在 Phase 4。

## 验证与重放

```bash
.venv/bin/muse collect
.venv/bin/muse validate
.venv/bin/muse replay --start 2026-10-01T00:00:00Z --end 2026-10-02T00:00:00Z
bash scripts/check.sh
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel
# 手动真实网络验收：创建独立临时库，连续两轮并重启适配器
.venv/bin/python scripts/verify_live.py
```

后台采集运行时，通过网页“立即采集”或 `/api/collect` 共用单轮锁；独立 CLI 采集应先停止后台服务。

前瞻窗口：15m、1h、4h、24h、3d、7d、14d、30d，兼容旧 5m。缺少未来价格或采样覆盖不足时不产生成功结论；报价有效即可测量结果，无需同一轮再次取得完整技术指标。手续费和滑点只用于入场候选的纸面净变化，不模拟实际订单执行。

重放仅使用采集时已可用的真实数据和名单，保留退市/移出标的历史。当前基础验证不等于完整历史重建、Top-K 排名绩效、提前量校准或 ML 回测。这些能力与预测有效性都不能通过测试数量来证明。

## 架构与验收资料

[系统架构与审计](SYSTEM_ARCHITECTURE.md) · [数据源矩阵](DATA_SOURCE_MATRIX.md) · [OKX 能力](OKX_CAPABILITY_MATRIX.md) · [动态名单](UNIVERSE_DESIGN.md) · [数据库](DATABASE_SCHEMA.md) · [特征](FEATURE_DESIGN.md) · [信号与预警](SIGNAL_DESIGN.md) · [验证](BACKTEST_DESIGN.md) · [实施与验收](IMPLEMENTATION_PLAN.md) · [V4 全章状态审计](docs/v4-requirements-audit.md) · [连接器评估](docs/connector-assessment.md)。

默认数据库 `data/muse.db`，适用于单实例；原始响应与研究历史没有自动清理策略，需监控磁盘并备份。PostgreSQL/Timescale、跨主机分布式部署和长期运行 SLA 尚未实现；Phase 2–6 的框架、已实现功能及剩余范围见[路线图](docs/roadmap.md)。

容器运行依赖由 `uv.lock` 导出并校验哈希。修改依赖后以 `uv export --frozen --no-dev --no-emit-project --output-file requirements.txt` 同步锁定清单。
