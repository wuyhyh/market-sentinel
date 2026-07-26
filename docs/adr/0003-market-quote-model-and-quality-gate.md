# ADR-0003：行情领域模型与数据质量门

## 状态

Accepted

## 背景

MarketSentinel 当前的行情路径仍使用 `MockMarketDataProvider`。现有
`MarketSnapshot` 只包含市场阶段、生成时间、自然语言 `Fact` 和自由格式
`raw_metrics`，无法表达证券级身份、交易所、币种、价格、成交量、成交额、
供应商时间、系统接收时间、交易状态、缺失证券、过期证券和批次完整性。

现有 Mock provider 还直接生成面向 LLM 的自然语言 `Fact`，使行情获取、
数据质量判断和简报输入构造混合在同一层。该结构适合阶段 0 的 Mock 验收，
但不足以直接承载真实行情。

A 股行情供应商尚未选择。为了避免领域对象被某个供应商的证券代码、字段名、
时间语义或单位绑定，必须先建立稳定、供应商无关的行情领域模型和确定性数据
质量门，再进行供应商选型和适配。

本 ADR 记录已接受的设计，不表示 `MarketQuote`、`QuoteBatch`、质量门或真实
provider 已经在当前代码中实现。

## 决策原则

- 领域模型与供应商解耦，供应商字段只在适配器边界完成映射和规范化。
- 原始行情事实与面向 LLM 的自然语言 `Fact` 分离。
- 数值、单位、证券身份、来源和时间必须能够由确定性代码验证。
- 数据质量检查由确定性代码执行，不交给 LLM。
- LLM 只能消费通过质量门的数据和显式的质量摘要。
- `partial` 必须显式标记，不能呈现为完整行情判断。
- 供应商整体失败或整批数据不可用时必须 fail closed。
- 不允许 LLM、provider 或简报构造层补全、猜测或虚构缺失价格。
- provider 不能自行决定最终完整性；最终 `completeness` 由质量门计算。
- 数据质量完整性与任务运行状态是两个不同维度。

## MarketQuote

`MarketQuote` 表示一条经过供应商适配器规范化的单证券行情。第一版只消费
A 股，因此只接受上海证券交易所和深圳证券交易所证券、人民币币种以及指数、
ETF 和股票三类证券。

证券的内部唯一身份由 `(exchange, symbol)` 共同构成。`symbol` 不得直接使用
供应商专有代码；供应商代码仅保留在可选的 `provider_symbol` 中。

### 字段

| 字段 | 数据类型 | 必填性 | 单位 | 字段语义与合法为空条件 |
| --- | --- | --- | --- | --- |
| `symbol` | `str` | 必填、非空 | 不适用 | 内部规范证券代码；与 `exchange` 共同构成唯一身份 |
| `provider_symbol` | `str \| None` | 可选 | 不适用 | 供应商使用的原始证券代码，仅用于适配器映射和审计；供应商未提供时可为空 |
| `exchange` | 枚举 | 必填 | MIC | 第一版只接受 `XSHG` 或 `XSHE` |
| `market` | `TradingMarket` | 必填 | 不适用 | 第一版必须为 `A_SHARE`，并与 `exchange` 一致 |
| `security_type` | 枚举 | 必填 | 不适用 | 第一版为 `INDEX`、`ETF` 或 `STOCK` |
| `currency` | ISO 4217 枚举或受限字符串 | 必填 | 不适用 | 第一版 A 股必须为 `CNY` |
| `source` | `str` | 必填、非空 | 不适用 | 稳定、可审计的数据源标识，不包含 API Key、Cookie 或令牌 |
| `source_time` | 时区感知 `datetime` | 必填 | 时间 | 供应商声明的行情时间；其准确语义由后续供应商 ADR 验证 |
| `received_at` | 时区感知 `datetime` | 必填 | 时间 | MarketSentinel 实际接收该记录的时间 |
| `previous_close` | `Decimal` | 必填、非空 | 元/份或指数点 | 第一版普通观察证券必须大于零；没有前收盘价的新上市证券暂不静默兼容 |
| `open` | `Decimal \| None` | 条件必填 | 元/份或指数点 | 09:25 后开盘确认必须存在；竞价尚未形成正式开盘价、停牌或无成交时可为空 |
| `high` | `Decimal \| None` | 条件必填 | 元/份或指数点 | 盘中或收盘行情通常必须存在；竞价、停牌或当日无成交时可为空 |
| `low` | `Decimal \| None` | 条件必填 | 元/份或指数点 | 盘中或收盘行情通常必须存在；竞价、停牌或当日无成交时可为空 |
| `last` | `Decimal \| None` | 条件必填 | 元/份或指数点 | 正常盘中和收盘行情必须存在；竞价未成交、停牌或明确无成交时可为空 |
| `volume` | `int \| None` | 可选 | 股 | 股票和 ETF 统一为股，不是手；供应商未提供、停牌、无成交或指数单位不明确时可为空 |
| `turnover` | `Decimal \| None` | 可选 | 人民币元 | 不使用千元或万元；供应商未提供、停牌或无成交时可为空 |
| `market_phase` | `MarketPhase` | 必填 | 不适用 | 该行情对应的任务阶段，必须与请求阶段一致 |
| `trading_status` | 枚举 | 必填 | 不适用 | 至少支持 `AUCTION`、`TRADING`、`SUSPENDED`、`HALTED`、`NO_TRADES`、`CLOSED` |

价格、指数点位、成交额等需要精确十进制语义的字段使用 `Decimal`。provider
应优先从字符串构造 `Decimal`，不得先转换为二进制 `float` 再转回
`Decimal`。成交量规范化为整数股，使用 `int`。

如果 provider 返回的成交量单位为手，适配器必须依据已验证的市场规则转换为
股。如果单位无法确认，该字段必须标记为 unsupported、缺失或无效，不得猜测。
指数成交量只有在供应商明确说明其含义和单位时才接收。

`last` 允许在集合竞价未形成成交、停牌或明确无成交时为 `None`。不得使用
`previous_close` 填充 `last` 来伪装有效最新价。正常交易、开盘确认或收盘阶段
是否允许字段为空，由质量门依据 `market_phase`、`security_type` 和
`trading_status` 确定。

`MarketQuote` 不保存完整 `raw_payload`。核心领域模型只允许可选的非敏感
`raw_reference`，例如供应商记录 ID、请求 ID 或内容校验摘要。原始响应是否
保存及保存期限必须服从供应商许可，并由后续决策单独确定。原始响应不得默认
写入日志或发送给 LLM。

### 时间与延迟

每条 quote 必须具有时区感知的 `source_time` 和 `received_at`。内部比较建议
统一转换为 UTC。

```text
delay_seconds = (received_at - source_time).total_seconds()
```

`delay_seconds` 表示从供应商声明的行情时间到系统接收记录的时间差。不得把
负值静默截断为零；负值必须进入未来时间质量检查。数据在质量门执行时的年龄
可以另行计算：

```text
age_seconds = (quality_check_time - source_time).total_seconds()
```

`delay_seconds` 和 `age_seconds` 含义不同，不能互相替代。

## 集合竞价扩展

第一版不要求连续保存 09:15—09:25 的全部变化，但核心模型必须允许后续增加
集合竞价数据。竞价字段使用独立的可选 `AuctionData` 扩展，避免污染普通行情
字段。

| 字段 | 数据类型 | 单位 | 约束 |
| --- | --- | --- | --- |
| `auction_reference_price` | `Decimal \| None` | 元/份或指数点 | 提供时必须大于零 |
| `auction_matched_volume` | `int \| None` | 股 | 提供时必须大于或等于零 |
| `auction_unmatched_volume` | `int \| None` | 股 | 提供时必须大于或等于零 |
| `auction_imbalance_side` | 枚举或 `None` | 不适用 | 仅接受供应商明确提供的 `BUY`、`SELL` 或 `BALANCED` |

模型必须能够区分：

- 供应商明确不支持；
- 当前证券或 phase 不适用；
- provider 响应缺失；
- provider 返回了明确的空值。

供应商不支持时，相应字段必须明确列入 `unsupported_fields` 或保持缺失并附带
结构化能力说明。不得根据买卖盘、未匹配量或价格变化推算这些字段，再冒充
供应商事实。

## QuoteBatch

`QuoteBatch` 表示同一次行情请求的标准化批次及其质量结果。第一版单个批次只
使用一个 `source`。

| 字段 | 数据类型 | 语义 |
| --- | --- | --- |
| `requested_symbols` | 有序、去重的证券身份集合 | 本次实际请求的全部内部证券 |
| `quotes` | `MarketQuote` 集合 | 质量门允许后续消费的行情 |
| `missing_symbols` | 证券身份集合 | provider 未返回记录的请求证券 |
| `stale_symbols` | 证券身份集合 | 超过拒绝阈值而不可使用的证券 |
| `invalid_symbols` | 证券身份集合 | 因结构、单位、市场、价格或其他质量错误被拒绝的证券 |
| `provider_errors` | 结构化错误集合 | 批次级或证券级 provider 错误，不包含敏感信息 |
| `completeness` | 枚举 | `complete`、`partial` 或 `failed` |
| `source` | `str` | 本批次的数据源标识 |
| `requested_at` | 时区感知 `datetime` | 系统开始本次请求的时间 |
| `completed_at` | 时区感知 `datetime` | provider 响应和批次规范化完成的时间 |

批次还应保留结构化 `quality_issues` 和数值型 `coverage_ratio`。使用
`coverage_ratio` 表示可用证券数量占请求数量的比例，避免让
`completeness` 同时承担枚举和百分比两种语义。

`provider_errors` 至少记录稳定错误代码、类别、可选证券身份、脱敏消息、
是否可重试、可选 `retry_after` 和非敏感请求 ID。

最终 `completeness` 由质量门计算：

- `complete`：每个请求证券恰好有一条可用 quote，且没有 missing、stale、
  invalid 或证券级 provider error。合法停牌或无成交可以是
  `complete` 并附带 warning。
- `partial`：至少有一条可用 quote，但存在 missing、stale、invalid 或部分
  证券错误。
- `failed`：没有任何可用 quote，或存在使整个批次身份、单位、时间或协议
  语义不可信的系统性错误，或供应商整体失败。

为符合当前架构命名，provider 可以先产生候选 `QuoteBatch`；候选批次在通过
质量门之前不具有可信的最终 `completeness`。实现时也可以使用单独的
`ProviderQuoteResult` 表示候选响应，再由质量门产生最终 `QuoteBatch`。无论
采用哪种内部命名，provider 都不得自行宣称批次为 `complete`。

## 数据质量门

数据质量门使用确定性代码，根据行情字段、请求上下文、市场阶段和可配置
新鲜度策略产生最终 `quotes`、质量问题和 `completeness`。

| 检查项目 | 严重程度 | 单条 quote 处理 | 批次处理 | 是否允许继续生成简报 |
| --- | --- | --- | --- | --- |
| 未来时间 | warning 或 high | 小于可配置时钟偏差容忍时允许并 warning；超过容忍时拒绝 | 有其他可用 quote 时 partial；全部被拒绝时 failed | 仅 warning 或 partial 时允许 |
| 时间倒退 | high | 相对同一证券上一条已接受 `source_time` 倒退时拒绝 | 有其他可用 quote 时 partial；全部倒退时 failed | partial 时允许并警告 |
| 过期 | high | 超过 warning 阈值时标记；超过拒绝阈值时拒绝并加入 `stale_symbols` | 有其他可用 quote 时 partial；全部过期时 failed | 仅仍有可用 quote 时允许 |
| 非法价格 | high | 任一非空价格小于或等于零时拒绝 | partial；无可用 quote 时 failed | partial 时允许并警告 |
| OHLC 关系错误 | high | `high < low`，或非空 `open`、`last` 超出 `[low, high]` 时拒绝 | partial；无可用 quote 时 failed | partial 时允许并警告 |
| 负成交量或成交额 | high | 拒绝 | partial；无可用 quote 时 failed | partial 时允许并警告 |
| 市场或币种不一致 | high 或 critical | 单证券不一致时拒绝 | 单条错误为 partial；批次系统性不一致为 failed | 仅 partial 时允许 |
| 单位错误 | high 或 critical | 无法可靠转换到规范单位时拒绝 | 单条错误为 partial；系统性单位错误为 failed | 仅 partial 时允许 |
| 重复证券 | high | 同一身份的重复记录全部拒绝，不选择第一条或最后一条 | partial；无可用 quote 时 failed | partial 时允许并警告 |
| 缺少证券 | medium；关键证券为 high | 加入 `missing_symbols` | 有其他可用 quote 时 partial；全部缺失时 failed | partial 时允许，关键证券缺失需高等级警告 |
| 停牌 | warning | 状态明确、时间有效且字段一致时允许；不得伪造 `last` | 可保持 complete 并附 warning | 允许 |
| 无成交 | warning | 状态明确且量额为零或空时允许；不得把前收盘价当最新价 | 可保持 complete 并附 warning | 允许 |
| provider timeout | high 或 critical | 分块请求中只影响部分证券时记录证券级错误 | 已有可用 quote 时 partial；整批超时为 failed | 仅 partial 时允许 |
| 429 | high 或 critical | 已完成部分分块时记录失败证券和 `retry_after` | 已有可用 quote 时 partial；整批限流为 failed | 仅 partial 时允许 |
| 授权失败 | critical | 不产生可信 quote | failed，不得盲目重试 | 不允许 |
| 协议错误 | high 或 critical | 单条无法解析时拒绝 | 单条错误为 partial；响应结构或时间语义整体不可信时 failed | 仅 partial 时允许 |
| 整体失败 | critical | 无可用 quote | failed | 不允许 |

价格区间关系只在相关字段均存在时检查。provider 使用零值代表缺失时，适配器
必须先转换为 `None` 或结构化错误，不能让零价格作为合法领域值通过。

跨批次时间倒退检查需要同一证券上一条已接受行情的时间。当前项目没有数据库，
第一版只能使用调用方注入的上一时间或进程内短期状态。进程重启后无法保证跨
运行检查，这一限制必须明确记录，不能被描述为已经具备持久化防倒退能力。

## 关键证券与普通证券

未来 watchlist 可以将实际持仓、主要指数或其他用户指定证券标记为关键证券。
关键性由用户配置和确定性代码解释，不由 provider 或 LLM 推断。

规则如下：

- 普通证券缺失时，存在其他可用行情即可生成 `partial` 简报。
- 关键证券缺失时仍可以生成 `partial` 简报，但必须产生高等级警告。
- 关键证券缺失时，置信度上限和警告内容由确定性规则设置，不能由 LLM 自行
  忽略或调整。
- 供应商整体失败必须判定为 `failed`。
- `partial` 必须向 LLM、通知层和最终用户显式展示，不能显示为完整行情判断。
- `partial` 不能通过填充前收盘价、缓存旧值或模型推测变成 `complete`。

关键证券的准确清单、置信度上限和最低可用覆盖率不在本 ADR 中写死，后续由
用户配置和数据源验证结果确定。

## 新鲜度策略

本 ADR 不编造具体供应商 SLA，也不写死秒数。新鲜度阈值由后续数据源 ADR、
真实供应商时间戳语义和配置共同确定。

策略必须能够按照以下维度配置不同阈值：

- `market_phase`；
- `security_type`；
- 集合竞价；
- 开盘价确认；
- 正常盘中；
- 收盘后；
- 指数；
- 股票和 ETF。

每组策略至少可以配置：

- `warn_after`；
- `reject_after`；
- `max_future_clock_skew`。

每条 quote 必须能够通过 `source_time` 和 `received_at` 计算
`delay_seconds`。质量门还应使用检查时间计算 `age_seconds`。过期数据必须被
明确标记或拒绝，不能静默当作当前行情。

## 异常模型

推荐的领域异常层次为：

```text
MarketDataProviderError
├── MarketDataTimeoutError
├── MarketDataRateLimitError
├── MarketDataAuthorizationError
├── MarketDataProtocolError
└── MarketDataQualityError
```

- `MarketDataProviderError`：所有行情 provider 领域异常的基类。
- `MarketDataTimeoutError`：请求超过明确时间预算。部分分块已有可信结果时
  可以形成 partial；整批无结果时必须 failed。
- `MarketDataRateLimitError`：供应商限流，可携带 `retry_after`。部分请求已
  成功时可以 partial；整批 429 时必须 failed。
- `MarketDataAuthorizationError`：凭证缺失、无效或权限不足。必须 failed，
  默认不可重试。
- `MarketDataProtocolError`：响应结构、字段类型、时间或供应商协议不符合
  约定。单条错误可以 partial，批次级协议错误必须 failed。
- `MarketDataQualityError`：规范化后的数据未通过确定性质量规则。单条错误
  可以 partial，系统性错误或零可用行情必须 failed。

异常应携带稳定错误代码、source、是否可重试、可选 `retry_after` 和脱敏请求
ID。异常消息、repr、日志和测试输出不得包含 API Key、Cookie、访问令牌或
完整原始响应。

供应商整体异常不能转换为空的 `complete` 或普通成功状态。即使生成结构化
失败日志或确定性失败通知，真实异常仍应向 CLI、API 和 scheduler 传播，不得
静默吞掉。

## 与现有架构集成

目标执行链为：

```text
MarketDataProvider
        |
        v
QuoteBatch（候选批次）
        |
        v
Quality Gate
        |
        v
QuoteBatch（最终 complete / partial / failed）
        |
        v
MarketSnapshot / Facts
        |
        v
Risk Engine
        |
        v
LLM
        |
        v
Notifier
```

`MarketDataProvider` 保持异步抽象和依赖注入，但后续接口必须显式接收市场、
phase 和用户配置的证券清单，并输出统一行情模型，而不是直接生成自然语言
`Fact`。

质量门之后，由确定性 builder 将可用 quote 转换为带 source、source_time 和
完整性摘要的 `Fact` 及 `MarketSnapshot`。自由格式 `raw_metrics` 不再作为
真实行情的主要载体。

风险引擎不直接消费 provider 原始响应。当前风险引擎继续对配置持仓执行确定性
规则；未来若需要用行情重估持仓，应先经过独立、确定性的估值层，再把有明确
时间的数据交给风险引擎。

LLM 只接收质量门之后的数据：

- `complete` 可以生成正常简报；
- `partial` 可以生成带明显完整性警告的简报；
- `failed` 不调用 LLM，也不得生成正常市场态势判断。

通知层必须能够看到 `complete`、`partial` 或 `failed`。`failed` 通知只能是
确定性失败说明，不能是 LLM 生成的行情判断。任务运行状态与行情完整性保持
分离：成功生成 partial 简报仍可以是任务完成，但必须同时携带
`completeness=partial`；供应商整体失败不能返回正常完成。

Mock provider 后续也必须输出相同的 `MarketQuote` 和批次结构，并通过同一个
质量门和 Fact builder。Mock 数据使用固定时间、固定输入和固定结果，不访问
网络，不保留一条绕过生产质量门的独立执行路径。

上述接口和模型当前尚未实现，本 ADR 不修改现有 `MarketDataProvider`、
`ReportService`、风险引擎、LLM 或通知代码。

## 测试策略

测试不得依赖当前日期、网络、真实供应商、付费 API 或 scheduler 时间推进。
所有价格和金额 fixture 使用十进制字符串构造 `Decimal`，所有时间使用固定、
时区感知的 datetime。

### 固定 fixture

固定 fixture 至少覆盖：

- 上海和深圳的指数、ETF 和股票；
- 完整批次；
- 缺少普通证券；
- 缺少关键证券；
- 过期和未来时间；
- 时间倒退；
- 非法价格和 OHLC 关系错误；
- 单位、市场和币种错误；
- 停牌和无成交；
- 集合竞价字段支持、不支持和缺失；
- 部分证券错误和供应商整体失败。

fixture 不保存真实凭证、Cookie、令牌或许可不允许再分发的完整原始响应。

### 单元测试

至少验证：

- `MarketQuote` 字段和跨字段约束；
- `Decimal` 精度不会经过 `float` 丢失；
- `delay_seconds` 和 `age_seconds`；
- 每条质量规则的 quote 与批次处置；
- `complete`、`partial` 和 `failed` 的确定性计算；
- 关键证券缺失的高等级警告；
- failed 路径不调用 LLM；
- partial 状态进入 LLM 输入和通知；
- provider 异常类型、重试语义和敏感信息脱敏；
- 不同输入顺序产生稳定、可重复的批次结果；
- 重复证券不会通过任意选择第一条或最后一条而被静默消解。

### Provider contract test

每个未来 provider 必须通过同一组 contract test，验证：

- 内部 symbol 与 `provider_symbol` 的双向映射；
- XSHG、XSHE、A_SHARE 和 CNY 一致性；
- 价格、成交量和成交额单位规范化；
- source 和时间字段语义；
- 停牌、无成交和缺失字段映射；
- unsupported 竞价字段的明确表达；
- timeout、429、授权和协议错误映射为统一领域异常；
- provider 不自行把 partial 或 failed 数据标记为 complete；
- 不在日志、异常或模型输出中泄露凭证和原始敏感响应。

Mock provider 必须运行相同 contract test 中与网络无关的部分。

## 与 ADR-0002 的关系

本 ADR 与 ADR-0002 的实质范围一致：

- 阶段 2 首版只实现 A 股行情；
- 同时支持上海和深圳；
- 证券清单由用户配置；
- 缺失证券允许 partial；
- 整体失败 fail closed；
- 供应商选择与领域模型分离；
- 当前实现仍为 Mock。

存在一处后续 ADR 编号顺序差异：ADR-0002 原先计划将供应商选择列为
ADR-0003、模型和质量门列为 ADR-0004。本 ADR 作为后续 Accepted 决策，将
顺序调整为先固定模型、再选择供应商：

- ADR-0003：行情领域模型与数据质量门；
- ADR-0004：A 股行情数据供应商选择；
- ADR-0005：A 股集合竞价采集策略。

本 ADR 只替代 ADR-0002 中上述编号和顺序，不改变 ADR-0002 的其他范围、
授权、成本、数据缺失和运行原则。

## 不在本 ADR 范围

- 具体行情供应商；
- 任何真实网络调用；
- 港股、美股和韩国行情实现；
- 新闻和公告数据；
- 数据库或行情持久化；
- LLM provider 切换或真实 LLM 日常运行；
- 券商交易；
- 自动下单；
- scheduler 时间修改。

## 后续工作

下一项只进行：

**ADR-0004：A 股行情数据供应商选择。**

在供应商选定、固定 fixture 和 provider contract test 通过之前，不得让
scheduler 使用未经验证的真实 provider 自动生成生产简报。
