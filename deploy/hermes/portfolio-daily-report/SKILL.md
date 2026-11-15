---
name: portfolio-daily-report
description: >
  投资组合日报：调用 portfolio MCP 的 get_daily_summary 取数据，写一份简短的中文日报，
  用 save_daily_report 保存（网页同步显示），并把日报正文作为最终回复（定时任务会推送到 Telegram）。
  用户说"写今天的日报""组合日报""持仓日报""今天组合怎么样"时也用本 skill。
version: 1.0.0
---

# 投资组合日报

数字全部来自 portfolio MCP，本 skill 只负责把数字组织成一份好读的日报。

## 步骤

1. 调用 `get_daily_summary`（不传日期 = 最新估值日）。
2. 如果 `report_saved` 为 true，而且用户没有明确要求重写：调用 `get_daily_report` 取已保存的日报，
   最终回复只写一行"{as_of} 的日报已经发过"，不要重复发送正文。
3. 按下面的格式写日报。
4. 调用 `save_daily_report(date=as_of, content=日报正文)` 保存。
5. 最终回复只输出日报正文，不要加"已保存"之类的说明。

## 格式

```
📊 组合日报 {as_of}（周几）

一句话：{当天最重要的变化，一句话}

收益：投资资产 {nav_investable_usd} 美元，当日 {day_change_usd} 美元（{day_return}）；今年 {ytd_return}，累计 {since_inception_return}，距高点 {drawdown}
账户：变动最大的 2–3 个账户，"名称 市值（当日 ±变动）"
主要变动：movers 前 3 个，"名称 ±变动金额（±变动%）· 所在账户"
风险提示：先列 new 或 escalated 的，"⚠ 严重 / 提示：说明（新 / 升级为严重）"；持续中的合并成一行"持续中：…"；没有就写"无"
资金进出：有才写，"账户 入金 / 出金 金额 币种"
数据状态：有 stale 的账户或失败的数据源才写，说明是哪个、是否沿用了旧数据
```

## 规则

- 只用 `get_daily_summary` 返回的数字，不估算、不编造；金额四舍五入到整数美元并加千分位，比例写成保留两位小数的百分数。
- `day_change_usd` 已经剔除了入金出金，不要把入金说成盈利。
- 字段为空就如实说明，比如 `previous_date` 为空时写"没有上一估值日的数据"。
- 只陈述事实，不给买卖建议，不预测走势。
- 不写账户号、token 或任何凭据。
- 总长控制在 20 行以内。
