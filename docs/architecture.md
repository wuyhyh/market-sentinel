# 架构草案

```text
Scheduler / Event Trigger ──► Job Guard ◄── Trading Calendar
                                  │
                                  ▼
交易所/数据商/官方公告/新闻
              │
              ▼
       Collectors（采集）
              │
              ▼
      Normalizer（统一时间、代码、来源）
              │
     ┌────────┴─────────┐
     ▼                  ▼
PostgreSQL         Event Detector
     │                  │
     ▼                  ▼
Risk Engine      LLM Analyst Adapter
（确定性规则）      OpenAI / DeepSeek
     └────────┬─────────┘
              ▼
       Brief Composer
              │
              ▼
 Console / Webhook / 后续通知渠道
              │
              ▼
          用户人工交易
```

## 为什么不是“一个 Agent 包办一切”

- 实时价格与指标计算需要可复现；
- 风险限制必须强制执行；
- LLM 只适合处理公告、新闻和解释；
- 任何结论都必须能追溯到原始数据；
- 模型切换不能改变业务逻辑。

## 24 小时运行策略

系统进程 24 小时存活，但不是 24 小时持续调用模型。

- 定时任务：固定市场节点；
- 任务防线：读取行情前按市场本地时区检查交易日；开发使用 `WeekdayCalendar`，
  生产必须接入真实交易所日历；
- 事件任务：价格、成交量、公告或新闻超过阈值时；
- 去重：相同消息只处理一次；
- 冷却时间：防止异常行情造成通知风暴；
- 降级：数据源故障时只报告故障，不生成交易判断；
- 成本控制：先用规则过滤，再调用 LLM。
