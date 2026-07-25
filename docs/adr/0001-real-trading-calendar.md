# ADR-0001：真实交易日历实现

## 状态

Proposed

当前已完成候选库的隔离验证并形成选型建议，但尚未实现生产适配器和官方日期例外层。实测还发现 `exchange_calendars==4.13.2` 与 `pandas_market_calendars==5.4.0` 均将 KRX 的全国地方选举日 `2026-06-03` 判断为交易日，因此在该缺口被官方日期验收测试和明确的临时例外处理覆盖前，本决策不标记为 Accepted。

## 背景

MarketSentinel 当前的 `WeekdayCalendar` 只把周一至周五视为交易日，并允许为开发和测试注入少量假日。该实现不能准确表达中国、韩国和美国市场各自的法定节假日、调休、全国选举日、年末休市及交易所临时休市安排。

生产任务会在读取行情、执行风险检查、调用 LLM 和发送通知之前检查交易日。若日历判断错误，系统可能在休市日生成无效简报，或在真实交易日错误跳过任务。因此 production 不能继续使用 `WeekdayCalendar`，也不能在真实日历失败时退化为“工作日即交易日”。

本 ADR 的目标仅是：给定 `TradingMarket` 和带时区的 `datetime`，准确判断该市场本地日期是否为交易日。

## 决策

生产真实交易日历拟选择并固定使用 `exchange_calendars==4.13.2` 作为基础实现依赖。

选择理由如下：

- 直接支持 XSHG、XKRX 和 XNYS；
- 原生提供 `is_session`，并提供 `next_session`、`previous_session` 和 `date_to_session` 供边界验证；
- 能对受支持日期范围之外的查询抛出明确异常；
- 相比基于完整 Pandas schedule 推导布尔结果，更贴合当前单日判断需求；
- `pandas_market_calendars` 对这三个市场仍依赖并镜像 `exchange_calendars`，没有提供独立的权威日历数据。

使用时必须先拒绝没有时区的 `datetime`，再按目标市场的 IANA 时区转换并提取市场本地日期，最后把该日期传给对应 MIC 日历的 `is_session`。不得把原始带时区 `datetime` 直接交给第三方库解释。

第三方库只作为基础规则引擎。实现还必须能够表达经交易所官方公告确认的一次性休市例外。已知的首个必要例外是 XKRX 的 `2026-06-03`；在该日期被可靠覆盖前，真实日历实现不得进入 production。

当前公共抽象保持最小语义：

```text
is_trading_day(
    market: TradingMarket,
    moment: timezone-aware datetime,
) -> bool
```

下一交易日和前一交易日能力用于验收和诊断，本 ADR 不要求把它们加入当前公共 `TradingCalendar` 接口。

## 支持市场

- `TradingMarket.CHINA`：需求中的中国市场名称；当前代码实际枚举成员为 `TradingMarket.A_SHARE`，对应上海证券交易所日历 `XSHG`，本地时区为 `Asia/Shanghai`。本 ADR 不引入枚举重命名。
- `TradingMarket.KOREA`：对应 Korea Exchange 日历 `XKRX`，本地时区为 `Asia/Seoul`。
- `TradingMarket.US`：对应 New York Stock Exchange 日历 `XNYS`，本地时区为 `America/New_York`。

## 事实来源

交易所官方公告、交易规则和年度休市安排是交易日事实的最终来源。

`exchange_calendars` 等第三方 Python 软件包只是实现依赖，不是最终权威。库能够生成某一年度的 schedule，不代表该年度所有临时休市、选举日或特殊安排已经被正确收录。

每次安装或升级软件包后，仍必须运行以交易所官方公告日期为固定输入的验收测试。验收至少覆盖：

- 各市场普通交易日和周末；
- 每个官方全日休市日；
- 假期前后的最后和首个交易日；
- 中国市场调休周末仍为非交易日；
- KRX 全国选举日和年末休市；
- NYSE 提前收盘日仍属于交易日；
- 同一 UTC 时刻在三个市场可能对应不同本地日期。

## 日历更新策略

- 生产依赖必须固定到经过验证的明确版本，不使用无上限的浮动版本作为运行基准。
- 升级日历库或其日历数据依赖时，必须运行全部单元测试和官方日期验收测试。
- 每年交易所发布下一年度休市安排后，更新该年度的固定验收日期，并在允许生产任务跨入新年度前完成验证。
- XSHG 在已验证版本中的预计算范围止于 `2026-12-31`；进入 2027 年前必须获得并验收覆盖新年度的数据。
- 临时休市、全国选举日、极端天气、国丧或交易所特别决定的休市必须作为单独的官方例外处理，不能等待周期性规则自行推导。
- 例外数据必须记录适用市场、日期和官方来源；过期例外不得无依据地延续到其他年度。

## 失败策略

以下情况必须安全拒绝执行任务并暴露明确错误：

- 请求的交易所日历不存在；
- 查询日期超出该日历经过验证或库声明的支持范围；
- 第三方库导入、初始化或 schedule 构造失败；
- 收到未知的 `TradingMarket`；
- 输入 `datetime` 没有有效时区；
- 当前年度尚未完成官方日期验收。

这些错误不得转换为普通的“非交易日”，也不得退化为 `WeekdayCalendar` 或“周一至周五即交易日”。调用方必须能够区分确认的非交易日与日历系统故障。

## 备选方案

未选择 `pandas_market_calendars==5.4.0` 作为生产基础实现。

理由：

- 对 XSHG、XKRX 和 XNYS 的数据实际镜像自 `exchange_calendars`，不能消除相同的日历数据缺口；
- 没有直接的 `is_trading_day`、`is_session`、`next_session` 或 `previous_session` 方法，需要通过 `valid_days` 或 `schedule` 范围查询推导；
- 对当前只需要布尔交易日判断的目标增加了 Pandas schedule 层；
- 实测对 1800 年和 2200 年仍能生成结果，包括官方特殊休市仅维护到 2026 年的 XSHG，未保留所需的越界保护；
- 原始带时区 `datetime` 可能按 UTC 日期解释，不符合先转换为市场本地日期的要求；
- 其依赖仍包含 `exchange_calendars`，并未形成更轻或独立的数据来源。

继续使用 `WeekdayCalendar` 也不作为 production 备选方案。它只保留给 development 和 test。

## 不在本 ADR 范围

- 行情数据源；
- 新闻和官方公告采集；
- 自动交易或券商账户控制；
- scheduler 动态改期；
- 数据库及业务数据持久化。
