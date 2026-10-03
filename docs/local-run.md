# 本地与云环境运行

```bash
bash scripts/install.sh
bash scripts/start.sh
```

需要 Python 3.12/uv；默认本机 8000。已有 `.env` 会继续生效。V4 标准规模是 `MUSE_MAX_ALTCOINS=100`（另加 BTC/ETH），旧值不会被覆盖。默认现货域名 `data-api.binance.vision`、合约 `www.okx.com`，代理和 CA 由运行环境提供；程序不修改它们。

对外监听必须配置 `MUSE_API_TOKEN`。WebSocket 与 API 同样认证，不把口令放在 URL。单实例采集使用轮次锁，网页手动采集与后台共用锁；不要另外启动第二个采集进程写同一库。

Docker/Compose 方法见 [README](../README.md)。受管理云环境使用 `docker-compose.cloud.yml` 显式传递既有代理/CA；构建目录权限受限时可使用 `BUILDX_CONFIG=/tmp/muse-buildx`，不用更换 `DOCKER_CONFIG` 或凭据目录。

升级旧实例前以 SQLite backup API 备份主库及已提交 WAL 内容，再使用副本验证迁移。旧数据支持增量迁移；无历史删除。Compose 默认 named volume 与工作区数据库是两个位置，不会自动同步。

云环境中的进程不会随快照恢复，需重新启动；长期运行需要持续在线实例。HTTP 451 是服务方地区/出口限制，本地换 IP 不会改变云端出口。用户处理币安 IP，页面显示失败并自动重试。
