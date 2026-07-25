# MarketSentinel 项目状态

## 1. 当前阶段

**阶段 0：Mock MVP 稳定化**

本阶段的目的是验证系统架构、任务编排、确定性风险检查和任务可观察性。当前结果证明开发环境中的 Mock 执行链可以按预期完成或跳过任务，但不代表系统已经具备真实行情采集、真实交易日识别或真实投研能力。

## 2. 已实现功能

- CLI 单次任务入口，可执行指定 `MarketPhase` 并输出结构化任务状态。
- FastAPI 健康检查接口和手工任务触发接口。
- APScheduler 跨时区定时任务，覆盖 A 股、韩国市场和美国市场的计划节点。
- `MarketPhase` 到 `TradingMarket` 的显式映射及逐项测试。
- `TradingCalendar` 交易日检查抽象。
- 仅用于 development/test 的 `WeekdayCalendar`。
- `MockMarketDataProvider` Mock 行情提供器。
- 由确定性 Python 代码执行的组合风险引擎。
- `MockAnalyst` Mock LLM 实现。
- `Notifier` 通知抽象，以及 Console/Webhook 实现。
- CLI、API 和 scheduler 对 `JobRunStatus` 的可观察性。
- Mock 交易日完成路径和非交易日跳过路径验收测试。
- 配置安全边界测试，包括环境限制、默认 Mock、付费模型缺少密钥、SecretStr 脱敏和无效组合配置路径。
- 统一的 `make verify` 本地验证命令。

当前代码中还存在 OpenAI 和 DeepSeek 适配器，但本阶段没有使用真实 API 对其进行日常运行验证。

## 3. 当前执行链

```text
CLI / API / Scheduler
        |
        v
MarketPhase -> TradingMarket
        |
        v
TradingCalendar
        |
        v
MarketDataProvider
        |
        v
RiskEngine
        |
        v
LLMAnalyst
        |
        v
Notifier
        |
        v
JobRunStatus
```

`ReportService.run()` 在读取行情之前调用 `TradingCalendar.is_trading_day()`。如果日历拒绝执行，任务在 `TradingCalendar` 之后立即返回 `skipped_non_trading_day`，不会调用行情提供器、风险分析、LLM 或通知器。

交易日路径只有在行情读取、风险检查、LLM 分析和通知发送全部完成后，才返回 `completed`。运行异常不会转换为成功状态。

## 4. 当前仍为 Mock 或开发实现的组件

### `MockMarketDataProvider`

- 文件：`src/market_sentinel/market_data/mock.py`
- 当前限制：只返回一条明确标记为 Mock 的事实，不连接交易所、数据商或真实行情服务，不能用于交易判断。

### `MockAnalyst`

- 文件：`src/market_sentinel/llm/mock_provider.py`
- 当前限制：根据输入生成固定结构的开发简报，不执行真实模型推理，不读取真实新闻、公告或研究资料。

### `WeekdayCalendar`

- 文件：`src/market_sentinel/trading_calendar/weekday.py`
- 当前限制：默认只区分周一至周五和周末；测试可注入节假日，但它不包含真实交易所休市、临时休市或特殊交易安排。生产环境会拒绝使用该实现。

### 示例持仓配置

- 文件：`config/portfolio.example.yaml`
- 当前限制：包含示例资金、风险阈值和示例持仓，不连接真实账户，也不会自动同步实际持仓。

### `ConsoleNotifier`

- 文件：`src/market_sentinel/notifications/console.py`
- 当前限制：默认通知行为是把结构化简报输出到标准输出，主要用于本地开发和测试，不是正式通知渠道。

### `WebhookNotifier`

- 文件：`src/market_sentinel/notifications/webhook.py`
- 当前限制：提供通用 HTTP Webhook 发送能力，但尚未确定、验证或运营任何正式通知渠道。

## 5. 当前对外接口

### CLI 单次任务

```bash
market-sentinel run-once a_share_close
```

命令接受 `MarketPhase` 的字符串值，并以 JSON 输出 `status` 和 `phase`。正常完成和主动跳过都正常退出；配置错误或运行异常继续以失败形式向上传递。

也可以通过模块入口执行：

```bash
python -m market_sentinel.cli run-once a_share_close
```

### FastAPI 健康检查

```http
GET /health
```

成功响应：

```json
{"status": "ok"}
```

### FastAPI 手工任务触发

```http
POST /jobs/{phase}
```

示例：

```bash
curl -X POST http://127.0.0.1:8000/jobs/a_share_close
```

响应包含任务最终 `status` 和规范化后的 `phase`。未知 phase 返回 HTTP 400。

### 本地统一验证

```bash
make verify
```

该命令依次运行 pytest、Ruff、Mypy 和 Python compileall；任一步失败都会使 Make 立即返回失败。

## 6. 配置与安全边界

- 默认 `LLM_PROVIDER` 是 `mock`，默认执行不会选择付费模型。
- `development` 和 `test` 环境允许使用 `WeekdayCalendar`。
- `production` 环境明确拒绝 `WeekdayCalendar`；当前尚无真实交易所日历适配器，因此生产日历配置不会降级到简单工作日判断。
- OpenAI Key 通过环境变量 `OPENAI_API_KEY` 提供；选择 OpenAI 但缺少 Key 时明确失败。
- DeepSeek Key 通过环境变量 `DEEPSEEK_API_KEY` 提供；选择 DeepSeek 但缺少 Key 时明确失败。
- API Key 在 Settings 中使用 `SecretStr`，常规字符串、repr 和 JSON 输出会脱敏。
- 本地 `.env` 文件由 `.gitignore` 排除，不进入 Git；`.env.example` 只保留空 Key 和安全开发默认值。
- 当前组合配置来自本地 YAML 示例，不连接券商账户。
- 当前项目没有券商接口、自动下单函数或账户控制能力。

## 7. 最近验证结果

- 验证日期：2026-07-25（Asia/Shanghai）
- 执行命令：`make verify`
- pytest：48 passed
- Ruff：All checks passed
- Mypy：Success，35 个源文件未发现问题
- compileall：成功，无错误输出

## 8. 已知问题

### 阶段 0 已解决

- CLI 会输出 `completed` 或 `skipped_non_trading_day`，不再丢失任务状态。
- API 会返回 `ReportService.run()` 的实际任务状态。
- Scheduler 会为成功执行或主动跳过生成包含 status、phase 和 market 的结构化日志；真实异常仍由 APScheduler 记录为失败。
- 所有 `MarketPhase` 都有显式、逐项验证的 `TradingMarket` 映射。
- Mock 完成路径已验证行情、风险、Mock LLM、通知和 `completed` 返回值。
- 非交易日跳过路径已验证不会调用行情、LLM 或通知，并返回 `skipped_non_trading_day`。
- 配置测试已覆盖 development/test/production 日历边界、默认 Mock、付费模型缺少密钥、无效 provider、SecretStr 脱敏和无效组合配置路径。
- `make verify` 已统一项目的本地测试、Lint、类型检查和字节码编译检查。

### 后续阶段处理

- 真实交易所日历尚未实现。
- 真实行情数据尚未接入。
- 官方公告和新闻数据尚未接入。
- 真实通知渠道尚未确定。
- PostgreSQL 尚未用于业务数据持久化。
- OpenAI 和 DeepSeek 适配器尚未用于真实日常运行。
- 非法 YAML、空 YAML、缺失字段等组合配置错误尚未统一为同一种带路径配置异常。

### 部署前必须处理

- 多 worker 部署可能在每个进程中重复启动 scheduler，造成重复任务或通知。
- Docker Compose 默认 PostgreSQL 密码只适用于开发，不能直接用于生产。
- 生产密钥必须使用适合实际部署环境的安全管理和轮换方式。
- 主机和容器必须配置并监控 NTP 校时。
- 必须建立日志轮转、业务数据备份和恢复验证。
- 必须定义真实数据源故障、超时、重试、降级和数据过期策略。

## 9. 明确不做

当前项目不做：

- 自动下单；
- 券商账户控制；
- 由 LLM 直接计算仓位、资金限制或交易规则；
- 允许 LLM 虚构行情、新闻、公告或来源；
- 对外荐股服务；
- 未经人工确认的交易决策。

## 10. 下一阶段唯一目标

**阶段 1：调研、设计并接入真实交易日历。**
