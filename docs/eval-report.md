# 抽取质量评估报告

> 由 `python -m tests.evaluate` 生成；语料为合成数据（`tests/corpus.py`）。

## 总体指标

| 指标 | 值 | 说明 |
|---|---|---|
| 召回率 | **100.0%** | 真值事件被抽出的比例（漏识别） |
| 精确率 | **100.0%** | 候选中命中真值的比例（误报） |
| F1 | **1.000** | 召回与精确的调和平均 |
| 自动入历准确率 | **100.0%** | 8/8 —— 最关键指标 |

## 计数

- 语料邮件数：17
- 真值事件数：12
- 抽出候选数：12
- 正确匹配：12
- 可自动入历：8

## 逐样本明细

| 样本 | 真值 | 候选 | 匹配 | 自动入历 | 待审 |
|---|---:|---:|---:|---:|---:|
| `ics_zoom_invite` | 1 | 1 | 1 | 1 | 0 |
| `alibaba_domain_expiry` | 1 | 1 | 1 | 0 | 1 |
| `hk_immigration_appointment` | 1 | 1 | 1 | 1 | 0 |
| `interview_next_wednesday` | 1 | 1 | 1 | 1 | 0 |
| `registration_deadline` | 1 | 1 | 1 | 1 | 0 |
| `flight_itinerary` | 1 | 1 | 1 | 1 | 0 |
| `credit_card_due` | 1 | 1 | 1 | 0 | 1 |
| `html_only_meeting` | 1 | 1 | 1 | 1 | 0 |
| `ics_rescheduled` | 1 | 1 | 1 | 1 | 0 |
| `quoted_old_date` | 1 | 1 | 1 | 1 | 0 |
| `no_year_date` | 1 | 1 | 1 | 0 | 1 |
| `marketing_promo` | 0 | 0 | 0 | 0 | 0 |
| `auto_reply` | 0 | 0 | 0 | 0 | 0 |
| `meeting_minutes_past` | 1 | 1 | 1 | 0 | 1 |
| `verification_code` | 0 | 0 | 0 | 0 | 0 |
| `plain_notice` | 0 | 0 | 0 | 0 | 0 |
| `meeting_without_time` | 0 | 0 | 0 | 0 | 0 |
