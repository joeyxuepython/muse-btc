# 云端无响应修复与更新步骤

2026-10-04。针对 Muse 反馈的 `5a8a0ca` 服务 `/health` 超时及重复重启。报告是云端观测材料；本次没有访问云机、下载其 1.9 GB 数据库或改动其守护程序。

## 代码核查与修复

1. **已确认同步阻塞路径。** 网络响应的 JSON 解析、原始归档（序列化、哈希、压缩、SQLite）、整轮成交归档/信号计算/前瞻验证原本直接运行在事件循环中。现在将这些工作交给线程，并同步覆盖宏观、链上批量历史、研究及其他扩展来源的写库路径。停止时等待已经启动的线程结束，再释放采集锁；防止取消请求后仍写库且另一轮提前进入。
2. **已确认历史引用增长及重复解压。** 完整细节刷新曾继续携带旧快照全部 raw IDs，运行越久引用越多；现在重建本轮引用，仍保留实际复用的缓存来源，旧 raw 数据不删除。 成交归档原本遍历快照全部 raw IDs，包含复用的全市场元数据和 ticker 响应。现在先读取端点元数据，仅对现货/合约成交端点加载、解压正文；同一快照的成交批量插入，继续保留 ID 去重和源时间检查。
3. **已确认大 JSON 参与历史排序。** 最新快照与证据查询现在先用行号/时间筛选最新版本，再读取正文，保持历史时点可见性和同时间后写入优先。采集内复用名单、最新快照和排名，减少重复查询。
4. **健康探针与采集阶段分开。** `/health` 为异步、无数据库访问的进程存活检查，`database` 改为 `NOT_CHECKED`，不再误称检查过数据库。`collector` 返回 `FETCHING / PERSISTING / EVALUATING / VALIDATING / IDLE`、本轮开始时间及耗时；完成后分阶段耗时写入 `/api/runtime` 的 `market_last_result`。请求统计增加 `last_archive_seconds`。
5. **并发可配置。** `MUSE_REQUEST_CONCURRENCY` 允许 1–32，默认仍为 4。HTTP 限流和退避机制保留，并发增加不能替代来源限速验收。该限制按 Providers 实例生效，市场采集和扩展 worker 使用不同实例。
6. **保留证据。** 原始归档继续开启，未删除历史。新 raw gzip 使用 level 1 以降低压缩开销，可能略增存储体积，格式和原始哈希保持兼容。`muse storage-report` 新增表及索引占用（SQLite 支持 dbstat 时）；报告是只读操作，不自动清理。

**需要纠正的归因：** 异步 HTTP 等待本身会让出事件循环，单轮超过 120 秒不等于 `/health` 必须排队。同步数据库、压缩、解析及计算才是这里确认的阻塞路径。CPU 百分比和 1.9 GB 文件大小不能单独证明某一个函数是唯一根因；进程“消失”也可能涉及守护终止、异常退出、OOM 等，需要退出码和云机日志进一步确认。

## Muse 更新时保护现有改动

先按已有方式停止市场服务及其 keepalive 自动拉起，避免旧/新代码并行写库。保存 `.env`，并使用 `muse backup` 或 SQLite backup API 做一致性备份；复制运行中的单个 `.db` 不能替代 WAL 一致性备份。不更改 Muse 已有邮件或信号推送定时任务。

报告提到 `src/muse_btc/providers/__init__.py` 有未提交并发修改。合并本修复 PR 后，先保存该修改再拉取，**不要执行 reset --hard**：

```bash
git status --short
git diff -- src/muse_btc/providers/__init__.py
# 仅暂存报告中这个已知文件的临时修改；其他文件有改动时先核对。
git stash push -m 'preserve Muse concurrency workaround before freeze fix' -- src/muse_btc/providers/__init__.py
git switch main
git pull --ff-only
```

保留现有 `.env` 中的 `MUSE_DETAIL_BATCH_SIZE=20` 和其他凭据/代理设置。若 Muse 决定沿用其临时 16 并发，用以下配置表达，不再把旧源码修改 apply 回去：

```dotenv
MUSE_REQUEST_CONCURRENCY=16
```

随后重新安装，再按原启动方式重启：

```bash
bash scripts/install.sh
bash scripts/check.sh
.venv/bin/python -c 'import muse_btc.providers as p; from muse_btc.config import Settings; print(p.__file__); print("request_concurrency=", Settings().request_concurrency)'
```

`install.sh` 使用 `uv sync --no-editable --reinstall-package muse-btc`。因此只编辑源码或 `git pull` 后不重新安装，运行中的 CLI 可能仍加载 site-packages 的旧副本。重启后 `/health` 应出现 `scope=PROCESS_LIVENESS`、`database=NOT_CHECKED` 和 `collector.phase`，据此识别本修复已加载。不要用仍为 `0.2.0` 的包版本号单独判断部署成功。

## 云端验收与 keepalive

- 在完整采集周期内反复请求 `/health`，同时观察阶段、轮次总耗时、`last_archive_seconds`、429/退避和实际细节覆盖。先验收现有 20 槽位配置，稳定后再逐步增加，不能凭配置宣称 102 项细节都新鲜。
- `/health` 200 只说明事件循环能响应；`collector_running=false`、长时间停在一个阶段或 `/api/runtime` 最近成功时间不前进都需要调查。`/ready` 503 可由数据缺失/过期导致，不能拿它作无条件重启依据。
- 外部 keepalive 不在本仓库中，本次没有修改。建议保留启动宽限和连续多次失败判定，重启前保存阶段、CPU/RSS、退出码、服务尾部日志；**不要设置“只要正在采集就永不重启”的无限豁免**，否则真实死锁会被掩盖。
- 若进程仍消失，记录是否被守护发出 SIGTERM/SIGKILL、退出状态，以及系统 OOM/内存限制证据。代码修复不能代替这些现场信息。
- 检查 `muse storage-report` 的实际大表及增长率，再规划可追溯归档/保留策略；本次不丢弃 raw、快照或历史回测证据，也不运行 VACUUM。

## 本地验证范围

`bash scripts/check.sh`：125 项测试通过（新增 8 项针对性回归），Ruff、JS 语法和 CLI 检查通过；wheel 构建通过。GitHub CI 以修复 PR 当前提交为准。

新增隔离测试故意阻塞原始归档及整轮后处理，确认 `/health` 在阻塞未解除时可返回；取消及重复取消不会提前释放数据库采集锁。其余测试覆盖完整刷新时引用数量保持有界且正确保留缓存来源、线程异常传播、可配置并发上限、无关压缩正文不会被解压、成交幂等性以及最新快照的历史时点/并列时间顺序。

本地合成数据库基准：2,040 条快照、102 个标的、每条 180 根 K 线，数据库约 92.3 MB。同一查询各运行 3 次，旧查询中位数 0.0330 秒，新查询 0.0112 秒，返回的 102 个最新 ID 完全一致。此结果只验证减少大正文排序的方向，不代表云端 1.9 GB 库或长期负载性能；没有据此声称云端故障已经消失。
