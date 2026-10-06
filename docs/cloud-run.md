# 云端运行 Phase 2–6 框架

最新入场确认修复见[历史锚点恢复](entry-anchor-recovery.md)：当前盘口保持新鲜，历史观测按当时质量核验，断档明确展示。对应 PR 合并后更新。

评估口径的更新与验收步骤见[策略期限与五分钟观察](strategy-validation.md)：按规则声明期限，风险告警不计持仓收益，旧结果保留。

性能修复的更新与验收步骤见[采集延迟与历史验证](collector-latency.md)。评估真实改善时保留云端配置、数据库和邮件连接。

Phase 2–6 框架和免费数据已合并到 `main`。本次可靠性升级的 PR 合并后按[升级说明](reliability-upgrade.md)更新；尚未合并时只在隔离测试目录检出相应 PR。需要 Python 3.12、uv；开发检查另需 Node.js。不要将密钥、邮箱正文或数据库提交 GitHub。

2026-10-04 运维问题修复及 Muse 推送侧需调整的增量读取、去重、价格与轮询间隔，见[告警投递接入](alert-delivery.md)。对应 PR 合并后部署，保留既有邮件连接。

## 拉取和框架检查

新目录：

```bash
git clone --branch main https://github.com/joeyxuepython/muse-btc.git
cd muse-btc
cp .env.example .env
bash scripts/install.sh
.venv/bin/muse framework
bash scripts/check.sh
```

已有目录先备份 `.env` 和数据库并检查 `git status`，再 fetch / 切换分支，不覆盖已有代理、密钥和数据。install 只安装和迁移数据库，不启动采集；framework 不访问网络来源。测试使用本地 / 合成数据。

安装脚本以普通 wheel 重新安装项目，避免 editable 路径导致原生 CLI 无法导入；更新代码后重新执行 install。

## 启动网页

```bash
bash scripts/start.sh --host 127.0.0.1 --port 8000
```

从本机 SSH 转发：

```bash
ssh -L 8000:127.0.0.1:8000 USER@CLOUD_HOST
```

打开本机 `http://127.0.0.1:8000` → “研究与扩展”。默认 Phase 1 行情采集开启；先验收空框架可设置 `MUSE_ENABLE_COLLECTOR=false`。默认研究检查为手动；本次保持用户暂停的研究定时监控。

Docker 需先在 `.env` 设置随机 `MUSE_API_TOKEN`（容器对外监听需要访问口令）：

```bash
docker compose up --build -d
docker compose logs --tail 100 muse
```

Compose 仅映射回环端口，也通过 SSH 转发访问。数据卷 `muse-data`，升级前备份，保持一个市场采集实例。`/health` 表示进程存活；`/ready` 验收全部目标行情及细节，内置扩展 worker 启用时也检查心跳，但不表示各扩展来源全部验收。

## 按需运行

Phase 2：

```bash
.venv/bin/muse intelligence --scope macro
.venv/bin/muse intelligence --scope research --sources Glassnode Coinbase CoinShares arXiv
.venv/bin/muse import-email /PATH/newsletter.eml
.venv/bin/muse import-context /PATH/evidence.json
.venv/bin/muse review-research DOCUMENT_ID /PATH/chinese-review.json
```

首次研究检查安静建基线，新研究需中文审阅才提醒。失败查看检查记录、修正适配 / 网络；不要删除基线制造新研究。沿用 Muse 的既有邮件连接；通过 EML 导入、研究队列和审阅 API 串接已有分析流程，见[升级说明](reliability-upgrade.md)。

Phase 3：`MUSE_ENABLE_INTELLIGENCE=true` 启用分项排名与融合；设 false 保留 Phase 1 行为。`MUSE_RULE_THRESHOLDS` 支持部分覆盖，例如 `{"rvol":2.0,"oi_build_pct":2.0}`；修改同时更新 `MUSE_THRESHOLD_VERSION`。催化剂 / 解锁 / 基本面通过导入接入，缺失不补值。

Phase 4：明确配置 `MUSE_ENABLE_MEME_DISCOVERY=true`，可选 `MUSE_ENABLE_GOPLUS=true`，重启后随市场循环分批刷新，或手动：

```bash
.venv/bin/muse intelligence --scope meme
```

总库不限 100，批次由 `MUSE_MEME_DISCOVERY_BATCH_SIZE` 控制。公开 profiles 不是全链索引；Solana 权限 / RPC 监听仍需 adapter，未知检查不当作安全。

Phase 5：配置 `MUSE_ENABLE_SOCIAL=true`、具有对应权限的 `MUSE_X_BEARER_TOKEN`，或导入 social / social_thesis。不会购买 X 权限，X 不进入自动行情循环。

```bash
.venv/bin/muse intelligence --scope social
```

Phase 6：积累成熟历史后显式运行：

```bash
.venv/bin/muse experiment-report
.venv/bin/muse train --horizon 14400
```

历史不足返回状态，不生成模型。结果不会自动升级规则。长历史计算可能较慢，建议手动在运维时段运行；当前为 Logistic，未增加梯度提升依赖。

## 输入与验收

免费 BTC 链上、期权和采样清算已提供手动适配器，宏观 / 稳定币 / ETF 沿用公开适配器。云端先按 [免费数据说明](free-btc-data.md) 分来源检查，再归档历史；定时研究仍保持暂停。

网页“导入证据 JSON”和 `/docs` 展示输入。顶层 `kind / key / source_url / market_time / data`，时间含时区，程序写实际获取时间。完整字段与待接入项见 [框架说明](phase2-6-framework.md)。

云端验收顺序：框架检查 → 空库网页 → Phase 1 行情 → 宏观缺失清单 → 研究基线 → 新增 / 重复 / 失败 → 中文审阅 → 可选 Meme / 社交 → 成熟历史后实验。长期覆盖和交易意义需另行验证。
