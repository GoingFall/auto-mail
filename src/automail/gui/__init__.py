"""图形界面层。

分层（**为了可测性**，都是从"能不能在没有显示会话的机器上测"倒推的）：

* :mod:`automail.gui.state` —— 数据与配置，**不含 Tk**，可用普通单测覆盖
* :mod:`automail.gui.worker` —— 后台线程与事件队列，**不含 Tk**
* :mod:`automail.gui.viewmodels` —— 纯函数：把数据整形为表格行/标签
* :mod:`automail.gui.app` —— 主窗口与控件（需要显示会话）
* :mod:`automail.gui.panels.*` —— 各标签页

前两层刻意不导入 tkinter：CI 与构建机通常没有显示会话，把它们与窗口分开
才能让"配置重载后是否重建了依赖对象""进度回调是否真的触发"这类关键行为
在本机与 CI 上都被覆盖到。
"""
