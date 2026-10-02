# 在本地启动完整采集与监控

本地电脑已能访问 Binance 合约接口时，可以直接运行同一套系统。云端与本地出口相互独立，更换本地 IP 不会改变云端的 HTTP 451。

## 准备

解压源码包，进入包含 `pyproject.toml` 和 `uv.lock` 的目录。安装 Python 3.12 和 uv；uv 官方安装说明：[docs.astral.sh/uv/getting-started/installation](https://docs.astral.sh/uv/getting-started/installation/)。Python 与包下载保持正常 TLS 校验。

Windows PowerShell、macOS 和 Linux 均可使用：

```text
uv sync --frozen
uv run --frozen muse collect
uv run --frozen muse serve
```

首次运行会安装依赖。无需 Binance API key，程序不访问账户接口。服务默认监听本机 8000 端口，浏览器打开本机对应端口即可使用监控页面。云环境 onboarding 页面不提供 localhost 预览链接。

`collect` 是一次性采集验证，运行完再启动 `serve`；不要同时运行两个采集进程。

## 检查

- “Binance Spot”应显示真实 BTC 与山寨币的采集数。
- “Binance Futures”应显示完整衍生品特征数，BTC 的 Funding、OI 5m、合约主动成交应非空。
- 一些现货标的没有对应合约，该标的衍生品缺失属于正常覆盖限制，不代表所有采集失败。
- 如果仍显示 451，则该程序使用的网络出口仍被服务方限制；检查本机终端的网络环境和服务方支持范围。
- 网页没有测试行情；没有满足条件时可以没有提醒。

所有数据保存在本机 `data/muse.db`。运行期间保留进程和网络连接；关闭浏览器不影响采集，关闭终端会停止服务。电脑休眠、断网期间不会生成虚构数据来填补缺口。

## 长期运行

后续部署到可合法访问所需数据源的常驻服务器，使用同一个 `uv.lock`、配置和程序。公开监听前需设置 `MUSE_API_TOKEN`、HTTPS 与访问限制；持续运行需要进程管理、磁盘监控和数据库备份。

当前版本的采集器与网页在同一实例运行，**尚未实现本地采集向云页面中继**，两个实例不会自动同步数据库。后续拆分采集器时必须保留来源、真实可用时间、身份验证与重放约束。
