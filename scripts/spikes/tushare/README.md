# Tushare Pro 免费账户能力探测

这是一个隔离的、只读的能力探测实验，不是正式行情 Provider，也不会接入
`ReportService`、scheduler 或任何业务执行链。

## 安全边界

- 只从项目根目录 `.env` 的 `TUSHARE_TOKEN` 读取 Token。
- 使用 `ts.pro_api(token)` 创建客户端，不调用 `ts.set_token()`。
- 不输出 Token，不保存原始 DataFrame，不记录完整行情。
- 报告只写入 Git 已忽略的
  `data/spikes/tushare/free-tier-capabilities.json`。
- 不申请、购买或提升权限。
- 所有接口均为只读数据接口，每个接口只发送一次最小请求。

## 安装隔离实验依赖

实验依赖不加入 MarketSentinel 的正式 `pyproject.toml`：

```bash
python -m pip install -r scripts/spikes/tushare/requirements.txt
```

当前固定 SDK 版本为 `tushare==1.4.29`。Tushare 官方文档说明可以直接使用
`pro_api(token)` 初始化，参见：

- [Python SDK 调用方式](https://tushare.pro/document/1?doc_id=40)
- [官方数据接口目录](https://tushare.pro/document/2?doc_id=17)

## 配置

在项目根目录 `.env` 中加入：

```dotenv
TUSHARE_TOKEN=你的真实Token
```

不要把真实 Token 写入 `.env.example`、命令行参数、测试、文档或 fixture。

## 手工运行

必须由用户在项目根目录手工执行：

```bash
python scripts/spikes/tushare/probe.py
```

可显式替换实验证券和固定历史日期：

```bash
python scripts/spikes/tushare/probe.py \
  --sh-stock 600519.SH \
  --sz-stock 000001.SZ \
  --sh-etf 510300.SH \
  --index 000001.SH \
  --trade-date 20260724
```

参数集中在命令行入口，不会散落在各接口函数中。股票日线、实时快照和实时分钟
请求会同时包含一只上海证券和一只深圳证券。

Token 缺失或为空时，程序会在导入 Tushare SDK 和任何网络调用之前失败。

## 探测接口

接口名称均来自 Tushare 官方文档：

| 能力 | API | 官方文档 |
| --- | --- | --- |
| 交易日历 | `trade_cal` | [交易日历](https://tushare.pro/document/2?doc_id=26) |
| A 股基础信息 | `stock_basic` | [股票基础信息](https://tushare.pro/document/1?doc_id=25) |
| 股票日线 | `daily` | [A 股日线](https://tushare.pro/document/1?doc_id=27) |
| ETF 基础信息 | `fund_basic` | [公募基金列表](https://tushare.pro/document/1?doc_id=19) |
| ETF 日线 | `fund_daily` | [ETF 日线](https://tushare.pro/document/2?doc_id=127) |
| ETF 实时快照 | `rt_etf_k` | [ETF 实时日线](https://tushare.pro/document/2?doc_id=400) |
| 指数基础信息 | `index_basic` | [指数基础信息](https://tushare.pro/document/2?doc_id=94) |
| 指数日线 | `index_daily` | [指数日线](https://tushare.pro/document/1?doc_id=95) |
| 指数实时快照 | `rt_idx_k` | [指数实时日线](https://tushare.pro/document/2?doc_id=403) |
| 股票实时快照 | `rt_k` | [A 股实时日线](https://tushare.pro/document/2?doc_id=372) |
| 股票实时分钟 | `rt_min` | [A 股实时分钟](https://tushare.pro/document/2?doc_id=374) |
| 当日集合竞价 | `stk_auction` | [当日集合竞价](https://tushare.pro/document/2?doc_id=369) |

`trade_cal` 的结果同时用于判断 Token 是否已被服务端接受，因此不会为了单独
验证 Token 再重复调用同一接口。若交易日历本身被拒绝，程序会结合其他接口的
成功、权限不足或鉴权失败结果判断 Token 状态。

## 输出

终端只打印计数和报告相对路径：

```text
Tushare free-tier capability probe
success=3
permission_denied=5
failed=4
report=data/spikes/tushare/free-tier-capabilities.json
```

JSON 报告不包含行情行，只包含接口状态、字段名、行数、时间和单位观察等
元数据。`success` 只说明账号成功调用接口，不代表数据满足 ADR-0003，也不
代表已经通过交易时段验证。

## 测试

测试全部使用 fake Tushare 模块和 fake DataFrame，不导入 SDK、不读取真实
`.env`、不访问网络：

```bash
pytest -q tests/spikes/tushare
```

