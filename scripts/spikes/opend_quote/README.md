# OpenD A 股实时行情能力探测

这是一个隔离、只读的能力探测，不是正式 `MarketDataProvider`，不会接入
`ReportService`、scheduler、LLM、通知、数据库或任何交易执行链。

## 安全边界

- OpenD 登录由用户在 MarketSentinel 之外手工完成。
- Spike 只创建 `OpenQuoteContext`，不导入或创建交易上下文。
- 不接收账号、密码、Cookie、Token、交易解锁密码或资金账号。
- 不读取项目 `.env`，也不读取 `TUSHARE_TOKEN`。
- 每次真实执行最多调用一次 `get_market_state` 和一次
  `get_market_snapshot`。
- 创建 SDK context 前先使用最长 2 秒的 TCP 预检；端口拒绝或 OpenD
  未启动时不会构造 SDK context，也不会触发 SDK 自动重连。
- 不订阅、不循环、不逐证券重试、不调用下单 API。
- 报告写入 Git 已忽略的
  `data/spikes/opend_quote/a-share-realtime-capabilities.json`。

## 官方接口依据

- [获取快照](https://openapi.futunn.com/futu-api-doc/quote/get-market-snapshot.html)
- [获取标的市场状态](https://openapi.futunn.com/futu-api-doc/quote/get-market-state.html)
- [权限与额度](https://openapi.futunn.com/futu-api-doc/intro/authority.html)

当前隔离依赖固定为 `futu-api==10.9.6908`。它只安装在 Spike 环境，不加入
MarketSentinel 的正式依赖。

## 安装

```bash
python -m pip install -r scripts/spikes/opend_quote/requirements.txt
```

OpenD 由用户按照官方文档安装、登录并启动本地行情服务。默认连接地址为
`127.0.0.1:11111`。不要把 OpenD 登录凭证放进项目文件或命令行。
若端口不可达，命令会立即以非零状态退出，输出
`connection_refused` 或 `opend_unavailable`，并提示启动并登录 OpenD。

## 无网络计划检查

```bash
python scripts/spikes/opend_quote/probe.py --dry-run
```

不传 `--execute` 时也默认执行 dry-run。dry-run 不导入 `futu-api`，不连接
OpenD。

## 休市手工探测

```bash
python scripts/spikes/opend_quote/probe.py --execute
```

休市探测可以验证连接、行情登录、一次批量覆盖、字段和错误语义，但报告必须
保持：

```text
live_freshness_verified=false
freshness_assessment=not_verified_outside_continuous_trading
continuous_updates_verified=false
```

## 交易时段单快照验收

仅在用户确认 A 股处于上午或下午连续交易时段时手工执行：

```bash
python scripts/spikes/opend_quote/probe.py --execute --expect-live
```

此模式要求 `MORNING` 或 `AFTERNOON`、可解析且不在未来的
`provider_update_time`，以及三只关键持仓全部返回。即使通过，也只表示一个
静态快照通过检查；报告始终保留 `continuous_updates_verified=false`。

本 Spike 不提供默认循环或两次自动采样，以遵守每次真实探测最多一次快照调用
的请求预算。后续若需要验证持续更新，应由用户分两次明确执行并人工比较两份
报告。
