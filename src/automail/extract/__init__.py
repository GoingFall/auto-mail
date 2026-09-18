"""抽取层：从清洗后的邮件文本中抽取事件候选。

来源优先级（冲突时按此取舍，**不取数值最高者**，见 docs/spec-event-state-machine.md）：

1. :mod:`automail.extract.ics`  —— ICS 直解。零 LLM 成本，最高置信度。
2. :mod:`automail.extract.rules` —— 规则匹配。确定性、可解释、带 evidence。
3. :mod:`automail.extract.llm`  —— LLM 兜底。仅对前两者未命中的邮件调用，
   且结果**恒进待审**（不自动入历）。

:mod:`automail.extract.pipeline` 负责串起三者并合并去重。
:mod:`automail.extract.prefilter` 在调用 LLM 之前做便宜的先验筛选，省 token。
"""
