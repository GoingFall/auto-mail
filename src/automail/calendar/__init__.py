"""日历后端：抽象接口 + 实现。

为什么要抽象：P3 阶段真实 Google 接入尚未配置，但审核队列与推送逻辑必须能
完整测试。因此定义窄接口，配一个 :class:`~automail.calendar.fake.FakeCalendar`
用于离线测试；P4 再补 Google 实现。

* :mod:`automail.calendar.normalize` —— 规范化哈希（三方比对的地基）
* :mod:`automail.calendar.backend`   —— 接口与数据载体
* :mod:`automail.calendar.fake`      —— 内存实现（测试用）
"""
