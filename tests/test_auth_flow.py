"""P4 授权流程的实测约束与错误说明测试。

**这里的每一条都来自真实授权尝试的反馈**，不是理论推演：

* ``403 access_denied``：Google 在授权页直接拒绝，浏览器不回跳，
  程序只能观察到「超时」——若不解释，使用者会以为是程序坏了
* 根因：``calendar.events`` 属**敏感** scope，应用处于「测试」发布状态时
  只有**测试用户名单**里的账号能授权，而**项目所有者不会自动进入该名单**
* 测试状态下的 refresh token 约 7 天失效（这是 Google 的规则，非缺陷）

这些约束无法通过代码消除，只能在报错时把原因说清楚。
"""

from __future__ import annotations

from automail.calendar.auth import (
    _explain_flow_failure,
    describe_setup_steps,
)

# ──────────────────────────────────────────────────────────────
# 错误说明：必须指向真实根因
# ──────────────────────────────────────────────────────────────


def test_timeout_explains_access_denied_possibility() -> None:
    """**实测的核心问题**：超时最常见的原因是 Google 侧直接拒绝了。

    Google 拒绝时不回跳到 http://localhost:<port>，因此我们的本地服务一直
    等不到回调，最终只能报「超时」。真正的原因在浏览器页面上，
    若不解释，使用者无从查起。
    """
    msg = _explain_flow_failure(
        Exception("Timed out waiting for response from authorization server")
    )
    assert "403" in msg
    assert "access_denied" in msg
    assert "不会回跳" in msg
    # 必须解释"为什么这里只看到超时"
    assert "超时" in msg


def test_timeout_gives_test_user_instructions() -> None:
    """必须给出「添加测试用户」的具体步骤——这是最常见的修复动作。"""
    msg = _explain_flow_failure(Exception("Timed out waiting for response"))
    assert "测试用户" in msg
    assert "Add users" in msg
    assert "Audience" in msg
    assert "外部" in msg


def test_timeout_warns_owner_is_not_auto_test_user() -> None:
    """最易忽略的一点：**项目所有者不会自动成为测试用户**。

    使用者通常认为「我是项目创建者，当然有权限」，从而在这一步卡住。
    """
    msg = _explain_flow_failure(Exception("timeout"))
    assert "项目所有者不会自动成为测试用户" in msg


def test_timeout_mentions_incognito_for_multi_login() -> None:
    """浏览器同时登录多个 Google 账号时会自动选错账号。

    错误页最后一行「联系开发者 <邮箱>」写的就是它，但使用者往往忽略。
    """
    msg = _explain_flow_failure(Exception("timeout"))
    assert "无痕" in msg


def test_timeout_explains_unverified_app_is_expected() -> None:
    """「Google 尚未验证此应用」是正常的，必须说明可继续，避免中途放弃。"""
    msg = _explain_flow_failure(Exception("timeout"))
    assert "尚未验证" in msg
    assert "继续前往" in msg


def test_timeout_states_seven_day_token_expiry() -> None:
    """测试状态下 refresh token 约 7 天失效——必须提前说明，否则会被当成 bug。"""
    msg = _explain_flow_failure(Exception("timeout"))
    assert "7 天" in msg
    assert "In production" in msg


def test_explicit_access_denied_gets_targeted_message() -> None:
    """若异常消息本身含 access_denied，应给出针对性说明而非通用超时文案。"""
    msg = _explain_flow_failure(Exception("access_denied"))
    assert "access_denied" in msg
    assert "测试用户" in msg
    # 不应把超时文案硬套上去
    assert "不会回跳" not in msg


def test_unknown_error_is_reported_verbatim() -> None:
    """未知错误不得被吞掉——原文必须保留，否则无从排查。"""
    msg = _explain_flow_failure(Exception("some brand new failure"))
    assert "some brand new failure" in msg


def test_explain_is_case_insensitive() -> None:
    assert "测试用户" in _explain_flow_failure(Exception("TIMED OUT"))
    assert "access_denied" in _explain_flow_failure(Exception("Access_Denied"))


# ──────────────────────────────────────────────────────────────
# 环境准备说明
# ──────────────────────────────────────────────────────────────


def test_setup_steps_are_complete() -> None:
    """通用步骤须覆盖全部前置动作。"""
    steps = describe_setup_steps()
    assert "Calendar API" in steps
    assert "桌面应用" in steps
    assert "credentials.json" in steps
    assert "auth" in steps


def test_setup_steps_mention_test_user_and_publishing() -> None:
    """必须提到测试用户与发布状态——否则使用者会卡在同一个 403 上。"""
    steps = describe_setup_steps()
    assert "测试用户" in steps or "测试" in steps
    assert "production" in steps or "In production" in steps


# ──────────────────────────────────────────────────────────────
# 交互式命令的默认行为
# ──────────────────────────────────────────────────────────────


def test_timeout_message_mentions_incomplete_authorization_first() -> None:
    """**实测教训**：超时最常见的原因是「人还没点完」，不是 403。

    之前把 403 排在第一位，导致真实情况（用户还没完成授权）被误导向
    「去查 Google 配置」。现在必须把「还没完成授权」列为第一种可能，
    并解释为什么浏览器会显示「localhost 拒绝连接」。
    """
    msg = _explain_flow_failure(Exception("Timed out waiting for response"))
    lines = [line for line in msg.splitlines() if line.strip()]

    # 「还没完成授权」必须出现在 403 之前
    incomplete_idx = next(
        i for i, line in enumerate(lines) if "还没在浏览器里完成授权" in line
    )
    denied_idx = next(i for i, line in enumerate(lines) if "403" in line)
    assert incomplete_idx < denied_idx, "「未完成授权」应排在 403 之前"

    # 必须解释「localhost 拒绝连接」的成因
    assert "拒绝" in msg
    assert "重新运行 automail auth" in msg


def test_auth_command_defaults_to_no_timeout() -> None:
    """``auth`` 是交互式命令，默认应不限时。

    `gcloud auth login` 这类命令都不会因为用户思考久了而失败。
    默认 300 秒曾导致真实的授权失败：用户在浏览器里操作的时间超过了它。
    """
    import typer

    from automail import cli as cli_module

    command = typer.main.get_command(cli_module.app)
    auth_cmd = command.commands["auth"]

    # 取出 --timeout 参数的默认值
    timeout_param = next(
        p for p in auth_cmd.params if getattr(p, "name", None) == "timeout"
    )
    assert timeout_param.default == 0, (
        "auth --timeout 默认应为 0（不限时）——它是交互式命令"
    )
