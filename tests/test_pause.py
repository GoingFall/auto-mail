"""暂停自动运行的测试。

设计要点（用测试钉住，避免以后被"顺手"改掉）：

* 暂停**只挡计划任务**触发的 ``run``，不挡图形界面里的手动同步
* 标记是文件，不是数据库字段或计划任务改动——便于手工创建/删除，
  也不依赖可能被锁住的数据库
* 幂等：重复设置同一状态不报错，且告知"有没有变化"
"""

from __future__ import annotations

from pathlib import Path

from automail import pause
from automail.settings import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        account="163",
        user_timezone="Asia/Shanghai",
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
    )


def test_default_is_not_paused(tmp_path) -> None:
    """默认可运行——不能默认暂停，否则装完没反应会被当成坏掉。"""
    assert pause.is_paused(_settings(tmp_path)) is False


def test_pause_and_resume_roundtrip(tmp_path) -> None:
    settings = _settings(tmp_path)

    assert pause.set_paused(settings, True) is True, "首次暂停应报告有变化"
    assert pause.is_paused(settings) is True
    assert pause.pause_file(settings).is_file()

    assert pause.set_paused(settings, False) is True, "恢复应报告有变化"
    assert pause.is_paused(settings) is False
    assert not pause.pause_file(settings).exists()


def test_set_paused_is_idempotent(tmp_path) -> None:
    """重复设置同一状态不算变化——界面据此避免每次都弹提示。"""
    settings = _settings(tmp_path)
    pause.set_paused(settings, True)
    assert pause.set_paused(settings, True) is False, "已是暂停状态，不应报告变化"

    pause.set_paused(settings, False)
    assert pause.set_paused(settings, False) is False
    assert pause.set_paused(settings, True) is True


def test_toggle_flips_and_returns_new_state(tmp_path) -> None:
    settings = _settings(tmp_path)
    assert pause.toggle(settings) is True
    assert pause.toggle(settings) is False


def test_marker_file_explains_itself(tmp_path) -> None:
    """标记文件要能自我解释。

    使用者可能在 ``data/`` 目录里翻到这个文件——没有人手工创建一个
    看不懂的文件才是负责任的做法。内容必须说明**怎么恢复**与**作用范围**。
    """
    settings = _settings(tmp_path)
    pause.set_paused(settings, True)
    text = pause.pause_file(settings).read_text(encoding="utf-8")

    assert "删除" in text, "要说明如何恢复"
    assert "手动" in text or "立即同步" in text, "要说明不影响手动同步"


def test_manual_run_path_does_not_check_pause(tmp_path) -> None:
    """**核心语义**：暂停不挡手动运行。

    图形界面的「立即同步」直接调用 ``Pipeline``，而 ``Pipeline`` **不检查**
    暂停标记。这样"点一下就跑"始终有效——点它说明使用者现在就想跑，
    与"别在我不知情时自动跑"是两件事。

    若哪天有人在 ``Pipeline`` 里加了暂停检查，这条用例会失败：
    界面上的「立即同步」会变得时灵时不灵，使用者只能靠猜。
    """
    import inspect

    from automail import pipeline

    source = inspect.getsource(pipeline)
    assert "is_paused" not in source, (
        "Pipeline 不应检查暂停标记：它同时服务手动同步，"
        "暂停只该拦 CLI 的 run（计划任务路径）"
    )


def test_cli_run_checks_pause() -> None:
    """反过来，CLI 的 ``run``（计划任务路径）必须检查。"""
    import inspect

    from automail import cli

    source = inspect.getsource(cli.run)
    assert "is_paused" in source, "run 必须检查暂停标记"
    assert "ignore_pause" in source, "要提供强制跑一轮的开关"


def test_marker_lives_in_data_dir(tmp_path) -> None:
    """标记放在 ``data/`` 下（已被 .gitignore 覆盖），不污染项目根。"""
    settings = _settings(tmp_path)
    marker = pause.pause_file(settings)
    assert marker.parent == settings.data_dir
    assert marker.name == pause.PAUSE_FILE_NAME


def test_pause_creates_data_dir_if_missing(tmp_path) -> None:
    """``data/`` 还不存在时也能暂停（首次运行前就想先关掉自动跑）。"""
    settings = _settings(tmp_path)
    assert not settings.data_dir.exists()
    pause.set_paused(settings, True)
    assert pause.is_paused(settings) is True


def test_settings_pause_flag_visible_after_reload(tmp_path, monkeypatch) -> None:
    """暂停状态是纯文件状态，重载配置不会丢失（也不需要重新加载）。"""
    settings = _settings(tmp_path)
    pause.set_paused(settings, True)
    # 新建 Settings 实例（模拟重载）后状态仍在
    again = _settings(tmp_path)
    assert pause.is_paused(again) is True
    _ = Path  # 保持导入语义
    monkeypatch.undo()
