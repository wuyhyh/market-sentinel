# MarketSentinel（市场哨兵）

一个面向个人投资者的 **24 小时市场监控、投研摘要与风险控制系统**。

它的定位不是自动荐股或自动下单，而是：

- 在固定市场节点生成结构化简报；
- 汇总 A 股、韩国市场和美股的影响；
- 对持仓结构执行确定性的风险检查；
- 使用大模型阅读非结构化消息；
- 保留完整审计记录，由用户人工决定是否交易。

## 设计原则

1. **人工交易**：系统不接券商下单接口。
2. **风险规则由代码执行**：仓位、集中度、止损与现金比例不交给大模型计算。
3. **事实与推断分离**：任何简报都必须标注数据时间和来源。
4. **模型可替换**：开发期可用 OpenAI，日常运行可切换 DeepSeek。
5. **事件驱动**：只有固定时点或重大事件才调用大模型，避免 24 小时持续烧 Token。
6. **默认拒绝过度交易**：输出可以是“无动作”。

## 当前 MVP

- FastAPI 健康检查接口；
- APScheduler 定时任务；
- 基于 `exchange-calendars==4.13.2` 的真实交易日历适配器；
- 中国 A 股 `XSHG`、韩国 `XKRX`、美国 `XNYS` 三个市场日历；
- OpenAI Responses API 结构化输出适配器；
- DeepSeek OpenAI-compatible API 适配器；
- 可替换行情数据接口；
- 控制台与通用 Webhook 通知；
- 确定性组合风险检查；
- Docker / Docker Compose 部署；
- 单元测试；
- Codex 项目说明 `AGENTS.md`。

> 当前行情数据源仍是 Mock，仅用于开发，尚未接入实时行情。接入真实资金前，必须更换为有授权、可验证、带来源和数据时间的行情及公告数据源。
> `WeekdayCalendar` 仅供 development/test 使用；production 必须显式配置 `TRADING_CALENDAR=exchange`，使用真实交易日历，且不会在日历错误时回退到简单的周一至周五判断。
> 真实日历以交易所官方公告为事实来源；项目维护 `tests/fixtures/trading_calendar_2026.yaml`，固定交叉验证 2026 年三个市场的已知交易日、周末和官方休市日。

## 快速开始

### 1. 本机开发

要求 Python 3.12。

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
market-sentinel run-once a_share_close
```

### 2. 使用 OpenAI

编辑 `.env`：

```dotenv
LLM_PROVIDER=openai
TRADING_CALENDAR=weekday
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6
```

### 3. 切换 DeepSeek

业务代码不变，只修改配置：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

从项目目录之外启动时，需将 `PORTFOLIO_CONFIG_PATH` 设置为组合 YAML 的绝对路径。

### 4. 启动 24 小时服务

```bash
docker compose up -d --build
docker compose logs -f app
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

手工触发一份简报：

```bash
curl -X POST http://127.0.0.1:8000/jobs/a_share_close
```

## 推荐部署方式

- **开发**：MacBook 或 Windows + WSL2。
- **长期运行**：稳定联网的 Linux 云服务器或自有 2280 服务器。
- MacBook/Windows 不建议作为唯一生产节点，因为睡眠、更新和网络切换会造成漏报。
- 生产环境使用 Docker、自动重启、NTP 校时、数据库备份和健康监控。

## 计划中的市场节点

所有时间都通过 IANA 时区处理，不手工计算夏令时。

- 中国 A 股：`Asia/Shanghai`
  - 08:35 隔夜全球摘要
  - 09:15 集合竞价开始观察
  - 09:25:30 集合竞价结果
  - 11:35 午间简报
  - 14:32 韩国市场收盘影响
  - 15:05 A 股收盘简报
- 韩国市场：`Asia/Seoul`
  - 09:00 开盘
  - 15:30 收盘
- 美股：`America/New_York`
  - 09:30 正常交易开盘
  - 16:05 收盘摘要

正式版必须使用真实交易日历，不能只依赖“周一至周五”。

当前 production 日历基线使用 `exchange-calendars==4.13.2`：

- 中国 A 股：`XSHG`；
- 韩国股票市场：`XKRX`；
- 美国股票市场：`XNYS`。

运行 production 配置时必须设置：

```dotenv
APP_ENV=production
TRADING_CALENDAR=exchange
```

## 目录

```text
src/market_sentinel/
├── app.py                 # FastAPI 服务
├── cli.py                 # 命令行
├── config.py              # 环境变量配置
├── jobs.py                # 简报任务编排
├── scheduler.py           # 跨时区任务
├── risk_engine.py         # 确定性风险规则
├── domain/models.py       # 结构化数据模型
├── trading_calendar/      # 开发日历与真实交易所日历
├── llm/                   # OpenAI / DeepSeek 适配器
├── market_data/           # 行情数据接口
└── notifications/         # 通知接口
```

## 下一阶段

**阶段 2：调研、设计并接入有授权、可验证、带来源和数据时间的实时行情数据源。**

真实持仓导入、官方公告、数据库、真实模型日常运行和影子模式不与该目标同时展开。

## 重要声明

本项目是个人信息整理和风险管理工具，不构成证券投资咨询，不保证收益。任何交易由用户独立判断并人工执行。
