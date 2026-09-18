"""命令行入口。

约定
----
* **所有写操作默认 dry-run**，必须显式 ``--apply`` 才真正执行。
* 每个命令都写一条 ``runs`` 记录（含失败），便于事后审计与排错。
* 退出码见 :mod:`automail.exits`。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .audit import render_detail, render_markdown, run_audit
from .db import (
    DatabaseError,
    RunRepository,
    open_db,
    sanitize_error,
)
from .digest import DigestBuilder
from .doctor import compute_exit_code, run_checks
from .exits import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from .extract.runner import ExtractRunner
from .logging_setup import sanitize_text, setup_logging
from .mail.backend import MailAuthError, MailError, UnsafeLoginError
from .mail.imap_backend import ImapBackend
from .mail.sync import SyncEngine
from .models import EventStatus, ReadinessStatus
from .pipeline import Pipeline
from .push import PushEngine
from .read_state import mark_read, unmark_read
from .report import (
    console,
    render_audit,
    render_audit_detail,
    render_auth_status,
    render_backup,
    render_digest,
    render_extract,
    render_mark_read,
    render_pipeline,
    render_push,
    render_push_window,
    render_readiness,
    render_readiness_json,
    render_review_action,
    render_review_queue,
    render_runs,
    render_setup,
    render_stats,
    render_sync,
    render_threads,
)
from .review import (
    ReviewError,
    ReviewQueue,
    ReviewStats,
    annotate_probable_duplicates,
    parse_event_ids,
)
from .settings import Settings, SettingsError, load_settings
from .stats import StatsCollector
from .threads import ThreadBuilder

app = typer.Typer(
    name="automail",
    help="自用网易邮箱(163)管理助手：抽取邮件中的事件时间并写入 Google 日历。",
    add_completion=False,
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"auto-mail {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def main_callback(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="显示版本并退出"),
    ] = None,
) -> None:
    """auto-mail 命令组。"""


def _load() -> Settings:
    """加载配置；非法配置直接以退出码 2 终止。"""
    try:
        return load_settings()
    except SettingsError as exc:
        console().print(f"[red]配置非法[/red]\n{exc}")
        raise typer.Exit(EXIT_FATAL) from exc


def _new_run_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def recorded_run(
    settings: Settings,
    command: str,
    *,
    stats: dict[str, object] | None = None,
) -> Iterator[str]:
    """给命令加一条 ``runs`` 记录，返回本次的 run_id。

    退出码从 ``typer.Exit`` 异常中取出，因此命令内部只需照常
    ``raise typer.Exit(code)``，无需关心记录逻辑。

    ``stats`` 传入一个可变字典，命令体可在执行过程中填充；退出时会读到最终内容。

    数据库不可用时**不阻断命令**：审计失败不应让主流程无法运行，
    仅在 stderr 提示一句。
    """
    run_id = _new_run_id()
    row_id: int | None = None
    try:
        with open_db(settings) as conn:
            row_id = RunRepository(conn).start(run_id, command)
    except DatabaseError as exc:
        console().print(f"[yellow]警告[/yellow] 无法写入运行记录：{sanitize_error(exc)}")

    exit_code = EXIT_OK
    error: str | None = None
    try:
        yield run_id
    except typer.Exit as exc:
        exit_code = int(exc.exit_code or 0)
        raise
    except BaseException as exc:  # noqa: BLE001 - 记录下来后原样抛出
        exit_code = EXIT_FATAL
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if row_id is not None:
            try:
                with open_db(settings) as conn:
                    RunRepository(conn).finish(
                        row_id,
                        ok=exit_code == 0,
                        exit_code=exit_code,
                        stats=stats,
                        error=sanitize_error(error) if error else None,
                    )
            except DatabaseError:
                pass


# ──────────────────────────────────────────────────────────────
# doctor
# ──────────────────────────────────────────────────────────────

@app.command()
def doctor(
    live: Annotated[
        bool,
        typer.Option("--live", help="额外检查 IMAP / Google / LLM 连通性（需凭据）"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出机器可读的 JSON，便于计划任务判断"),
    ] = False,
) -> None:
    """自检：配置、依赖、目录、数据库、时区。

    缺少密钥报告为 MISSING 并返回退出码 1，**不视为失败**——
    这样在没有凭据时也能验证项目本身是否可运行。
    """
    settings = _load()
    setup_logging(settings, console=False)
    settings.apply_proxy_env()

    stats: dict[str, object] = {}
    with recorded_run(settings, "doctor", stats=stats) as run_id:
        items = run_checks(settings, live=live)
        code = compute_exit_code(items)
        stats.update(
            {
                "run_id": run_id,
                "exit_code": code,
                "ok": sum(1 for i in items if i.status is ReadinessStatus.OK),
                "missing": sum(1 for i in items if i.status is ReadinessStatus.MISSING),
            }
        )

        if as_json:
            typer.echo(render_readiness_json(items, exit_code=code))
        else:
            render_readiness(items)
            summary = {
                EXIT_OK: "[green]全部就绪[/green]",
                EXIT_PARTIAL: "[yellow]部分项缺失（不影响离线开发）[/yellow]",
                EXIT_FATAL: "[red]存在致命问题，需先修复[/red]",
            }[code]
            console().print(f"\n{summary} · 退出码 {code}")

        raise typer.Exit(code)


# ──────────────────────────────────────────────────────────────
# runs
# ──────────────────────────────────────────────────────────────

@app.command()
def runs(
    limit: Annotated[int, typer.Option("--limit", "-n", help="显示条数")] = 10,
) -> None:
    """查看最近的运行记录。"""
    settings = _load()
    setup_logging(settings, console=False)
    try:
        with open_db(settings) as conn:
            records = RunRepository(conn).recent(limit=limit)
    except DatabaseError as exc:
        console().print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_FATAL) from exc

    if not records:
        console().print("暂无运行记录。")
        raise typer.Exit(EXIT_OK)
    render_runs(records)
    raise typer.Exit(EXIT_OK)


# ──────────────────────────────────────────────────────────────
# 占位命令：P1 起逐步实现
# ──────────────────────────────────────────────────────────────

def _not_implemented(settings: Settings, command: str, phase: str, detail: str) -> None:
    """统一的占位行为：如实说明尚未实现，并以退出码 1 退出。

    不用退出码 0，因为「没做」与「做成功」必须可区分。
    同时记一条 runs，使「哪些命令被调用过」在审计上不出现盲区。
    """
    console().print(
        f"[yellow]{command} 尚未实现[/yellow]（计划于 {phase} 落地）\n{detail}"
    )
    with recorded_run(settings, command, stats={"not_implemented": True, "phase": phase}):
        raise typer.Exit(EXIT_PARTIAL)


@app.command()
def auth(
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="不自动打开浏览器，改为打印授权链接")
    ] = False,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="等待授权的秒数；默认 0 表示不限时（交互式命令，等你点完为止）",
        ),
    ] = 0,
    status: Annotated[
        bool, typer.Option("--status", help="只检查授权状态，不发起授权")
    ] = False,
    revoke: Annotated[
        bool, typer.Option("--revoke", help="删除本地 token.json（换账号用）")
    ] = False,
) -> None:
    """一次性 Google OAuth 授权，生成 token.json。

    会在浏览器中打开 Google 授权页，**需要你手动点「允许」**——这一步无法
    由程序代替：credentials.json 只说明「这个客户端程序是谁」，只有你点过
    允许，Google 才会发令牌。

    授权范围仅 ``calendar.events``（查看与编辑日历事件），不含其他 Google 数据。
    令牌写入 token.json 后，后续运行会用 refresh token 静默续期。
    """
    settings = _load()
    setup_logging(settings, console=False)

    from .calendar import auth as auth_module

    stats: dict[str, object] = {}
    with recorded_run(settings, "auth", stats=stats):
        with open_db(settings) as conn:
            if status:
                info = auth_module.inspect_status(settings, conn)
                render_auth_status(info)
                raise typer.Exit(EXIT_OK if info.ready else EXIT_PARTIAL)

            if revoke:
                removed = auth_module.revoke_local_token(settings, conn=conn)
                stats["revoked"] = removed
                if removed:
                    console().print(
                        "[green]已删除本地令牌[/green]。"
                        "注意：Google 侧的授权记录仍在（可在账号设置的"
                        "「第三方应用」中撤销）。"
                    )
                else:
                    console().print("本地没有令牌文件，无需删除。")
                raise typer.Exit(EXIT_OK)

            info = auth_module.inspect_status(settings, conn)
            if info.token_present and not info.needs_reauth:
                console().print(f"[green]已授权[/green]（{info.token_file}）")
                console().print(
                    "如需重新授权，先运行 [bold]automail auth --revoke[/bold] 删除旧令牌。"
                )
                raise typer.Exit(EXIT_OK)

            # 开始授权
            console().print(
                "即将打开浏览器完成 Google 授权。"
            )
            console().print(
                "授权范围：[bold]calendar.events[/bold]（查看与编辑日历事件）"
            )
            console().print(
                "回调地址：http://localhost:<随机端口>（程序临时启动的本地服务）"
            )
            console().print("")
            if no_browser:
                console().print(
                    "[yellow]已指定 --no-browser[/yellow]：请手动打开下面打印的链接。"
                )
            else:
                console().print("如果浏览器没有自动打开，请手动访问下面打印的链接。")
            console().print("")

            try:
                token_file = auth_module.run_authorization(
                    settings,
                    conn=conn,
                    open_browser=not no_browser,
                    timeout_seconds=(timeout or None),
                )
            except auth_module.CredentialsMissingError as exc:
                console().print("[red]凭据有问题[/red]")
                console().print(str(exc))
                stats["error"] = "credentials"
                raise typer.Exit(EXIT_FATAL) from exc
            except auth_module.AuthError as exc:
                # 不加「授权失败」前缀：_explain_flow_failure 返回的是自包含的
                # 完整说明（含排查步骤），再加前缀会变成「授权失败 授权失败：…」
                # 这种重复。同时也不再附 describe_setup_steps——说明里已含
                # 针对性步骤，堆砌通用步骤只会淹没重点。
                console().print(f"[red]{sanitize_text(str(exc), limit=4000)}[/red]")
                stats["error"] = "auth_flow"
                raise typer.Exit(EXIT_FATAL) from exc

        stats["token_file"] = str(token_file)
        console().print("")
        console().print(f"[green]授权成功[/green] 令牌已写入 {token_file}")
        console().print(
            "现在可以运行 [bold]automail push --approved --apply[/bold] "
            "把已批准的事件写入日历。"
        )
        raise typer.Exit(EXIT_OK)


@app.command()
def sync(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="真正写入数据库（默认 dry-run）"),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="本轮最多取回的新邮件数（默认不限）"),
    ] = None,
) -> None:
    """从 163 增量同步邮件到本地数据库（只读邮箱）。

    **只读保证**：使用 ``EXAMINE`` 与 ``BODY.PEEK[]``，不会改变邮件的已读状态，
    也不会移动或删除任何邮件。

    默认 dry-run，只报告将要发生的变化；加 ``--apply`` 才写库。
    """
    settings = _load()
    setup_logging(settings, console=False)
    settings.apply_proxy_env()

    stats: dict[str, object] = {"dry_run": not apply}
    # 先进入记录上下文：即使因缺凭据提前退出，也要留下审计痕迹
    with recorded_run(settings, "sync", stats=stats):
        if not settings.imap_user.strip() or not settings.imap_auth_code_value:
            console().print(
                "[yellow]缺少 163 凭据[/yellow]：请在 .env 中填写 IMAP_USER 与 "
                "IMAP_AUTH_CODE（16 位客户端授权码，非网页登录密码）"
            )
            stats["error"] = "missing_credentials"
            raise typer.Exit(EXIT_PARTIAL)

        with open_db(settings) as conn:
            try:
                with ImapBackend(settings) as backend:
                    engine = SyncEngine(settings, conn, backend)
                    result = engine.sync(apply=apply, limit=limit)
            except MailAuthError as exc:
                console().print(f"[red]认证失败[/red] {sanitize_text(str(exc))}")
                stats["error"] = "auth"
                raise typer.Exit(EXIT_FATAL) from exc
            except UnsafeLoginError as exc:
                console().print(f"[red]被服务端拒绝[/red] {sanitize_text(str(exc))}")
                stats["error"] = "unsafe_login"
                raise typer.Exit(EXIT_FATAL) from exc
            except MailError as exc:
                console().print(f"[red]同步失败[/red] {sanitize_text(str(exc))}")
                stats["error"] = type(exc).__name__
                raise typer.Exit(EXIT_FATAL) from exc

        stats.update(result.as_dict())
        render_sync(result)
        raise typer.Exit(EXIT_OK)


@app.command()
def extract(
    apply: Annotated[
        bool, typer.Option("--apply", help="真正写入候选事件（默认 dry-run）")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="本轮最多处理几封（默认 200）")
    ] = None,
) -> None:
    """从已同步邮件抽取事件候选（ICS → 规则 → LLM）。

    来源优先级 ICS > 规则 > LLM；LLM 结果**恒进待审**（不自动入历）。
    LLM 未配置时降级为「仅规则 + ICS」并在统计中如实报告降级数量。

    默认 dry-run，只报告将产生的候选；加 ``--apply`` 才写库。
    """
    settings = _load()
    setup_logging(settings, console=False)
    settings.apply_proxy_env()

    stats: dict[str, object] = {"dry_run": not apply}
    with recorded_run(settings, "extract", stats=stats):
        llm = _build_llm(settings)
        with open_db(settings) as conn:
            runner = ExtractRunner(settings, conn, llm=llm)
            result = runner.run(apply=apply, limit=limit)

        stats.update(result.as_dict())
        render_extract(result)
        raise typer.Exit(EXIT_OK)


@app.command("mark-read")
def mark_read_cmd(
    apply: Annotated[
        bool, typer.Option("--apply", help="真正标记（默认 dry-run，只报告）")
    ] = False,
    undo: Annotated[
        bool,
        typer.Option("--undo", help="撤销：把本程序标记过的邮件恢复为未读"),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="每个文件夹最多处理几封（首次建议给小值）"),
    ] = None,
) -> None:
    """把处理完的邮件在邮箱里标为已读。

    **这是本项目唯一的邮箱写操作**（此前全程只读）。它只改 ``\\Seen`` 一个
    标志——不删邮件、不移动、不改别的标志。

    默认 **dry-run**：只报告将标记哪些，不向邮箱发送任何 STORE。
    加 ``--apply`` 才真正标记。

    需要在 ``.env`` 里显式开启（默认关闭）：

        MARK_READ_POLICY=resolved   # off | resolved | processed

    ``resolved`` 只在「没有任何事件等着你」时才标已读，让「未读」继续表示
    「需要你处理」；``processed`` 则所有抽取完成的邮件都标。

    标错了可以回退（用 ``--undo``），且只影响本程序标记过的邮件——
    你自己在别的客户端读过的不会被碰。
    """
    settings = _load()
    setup_logging(settings, console=False)

    if not undo and not settings.mark_read_enabled:
        console().print(
            "[yellow]未开启。默认关闭是因为这是唯一会改变邮箱状态的操作。[/yellow]\n"
            "在 .env 中设置后重试：\n"
            "  MARK_READ_POLICY=resolved    # 推荐：没有待处理事件时才标\n"
            "  MARK_READ_POLICY=processed   # 抽取完成即标\n"
        )
        raise typer.Exit(EXIT_OK)

    if not settings.imap_user.strip() or not settings.imap_auth_code_value:
        console().print("[red]未配置 163 凭据（IMAP_USER / IMAP_AUTH_CODE）[/red]")
        raise typer.Exit(EXIT_FATAL)

    from .mail.imap_backend import ImapBackend

    stats: dict[str, object] = {"dry_run": not apply, "undo": undo}
    with recorded_run(settings, "mark-read undo" if undo else "mark-read", stats=stats):
        action = unmark_read if undo else mark_read
        try:
            with open_db(settings) as conn, ImapBackend(settings) as backend:
                result = action(settings, conn, backend, apply=apply, limit=limit)
        except (MailError, OSError) as exc:
            console().print(f"[red]邮箱操作失败[/red]：{sanitize_error(exc)}")
            raise typer.Exit(EXIT_FATAL) from exc

        stats.update(result.as_dict())
        render_mark_read(result)
        raise typer.Exit(EXIT_OK)


@app.command()
def audit(
    hours: Annotated[
        int, typer.Option("--hours", help="回看最近几小时收到的邮件（默认 24）")
    ] = 24,
    show: Annotated[
        int | None,
        typer.Option("--show", help="只详情打印某一封邮件（id），含完整正文片段"),
    ] = None,
    with_llm: Annotated[
        bool, typer.Option("--with-llm", help="同时调用 LLM 比对（会消耗额度）")
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", help="最多复查几封（默认 200）")
    ] = 200,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 到 stdout")] = False,
    open_file: Annotated[
        bool, typer.Option("--open", help="生成后在默认程序中打开报告")
    ] = False,
) -> None:
    """抽取复盘：找出最近可能没被提取对的邮件。

    用途是**迭代**：每天固定时间跑一次，看「最近的邮件理解得对不对」。
    报告列出可疑项（重点是**预筛说该抽、结果什么都没有**这类静默漏抽），
    以及已抽出的候选，方便一眼扫过。

    **只读数据库、只写本地报告**——不碰邮箱、不碰日历、不改库。
    默认也**不调用 LLM**（复盘是给人看的，不是再花一次 token 得到同样答案）；
    加 ``--with-llm`` 才比对 LLM 的结论。
    """
    settings = _load()
    setup_logging(settings, console=False)

    if show is not None:
        with open_db(settings) as conn:
            text = render_detail(conn, settings, show)
        render_audit_detail(text)
        raise typer.Exit(EXIT_OK)

    stats: dict[str, object] = {"hours": hours, "with_llm": with_llm}
    with recorded_run(settings, "audit", stats=stats):
        llm = _build_llm(settings) if with_llm else None
        with open_db(settings) as conn:
            report = run_audit(settings, conn, hours=hours, limit=limit, llm=llm)

        content = render_markdown(report)
        path = _write_audit_report(settings, report, content)
        stats.update(report.counts())
        stats["path"] = str(path)

        if as_json:
            console().print_json(report.to_json())
        else:
            render_audit(report)
            console().print(f"\n[green]报告已写入[/green] {path}")

        if open_file:
            _open_in_default_app(path)
        raise typer.Exit(EXIT_OK)


def _write_audit_report(
    settings: Settings, report: object, content: str
) -> Path:
    """把复盘报告写到 ``out/audit-<日期>.md``。"""
    stamp = str(getattr(report, "window_end", "") or "")[:10] or "unknown"
    path = Path(settings.out_dir) / f"audit-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _build_llm(settings: Settings):
    """构造 LLM 抽取器；凭据缺失时返回 None（降级为仅规则）。"""
    if not settings.llm_base_url.strip() or not settings.llm_api_key_value:
        return None
    from .extract.llm import LlmExtractor

    return LlmExtractor(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key_value,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        max_calls_per_run=settings.llm_max_calls_per_run,
        excerpt_max_chars=settings.llm_excerpt_max_chars,
        payload_fields=tuple(settings.llm_payload_field_list),
    )


# ──────────────────────────────────────────────────────────────
# events：审核队列
# ──────────────────────────────────────────────────────────────

events_app = typer.Typer(
    name="events",
    help="事件审核：列出、批准、否决、忽略、修正、接管。",
    no_args_is_help=True,
)
app.add_typer(events_app, name="events")


@events_app.command("list")
def events_list(
    status: Annotated[
        str,
        typer.Option("--status", "-s", help="按状态过滤：pending/approved/pushed/frozen/all"),
    ] = "pending",
    limit: Annotated[int, typer.Option("--limit", "-n", help="显示条数")] = 50,
    since_days: Annotated[
        int | None, typer.Option("--since", help="只看最近 N 天产生的候选")
    ] = None,
) -> None:
    """列出事件（默认待审）。"""
    settings = _load()
    setup_logging(settings, console=False)

    statuses = _resolve_statuses(status)
    with open_db(settings) as conn:
        queue = ReviewQueue(conn)
        items = queue.list_items(statuses=statuses, limit=limit, since_days=since_days)
        # 标注「可能重复」（只提示，不合并、不隐藏）
        annotate_probable_duplicates(items)

    if status == "all":
        render_review_queue(items, title="事件列表 · 全部", out=console())
    else:
        render_review_queue(
            items, title=f"事件列表 · {'/'.join(s.value for s in statuses)}"
        )
    raise typer.Exit(EXIT_OK)


def _resolve_statuses(raw: str) -> tuple[EventStatus, ...]:
    """把 ``--status`` 参数解析成状态元组。

    ``frozen`` 是**组合状态**（``externally_modified`` + ``conflict`` +
    ``missing`` + ``not_owned`` 语义），单独提供是因为它是使用者最关心的
    「需要我裁决」那一类。
    """
    key = raw.strip().lower()
    if key == "all":
        return tuple(EventStatus)
    if key == "frozen" or key == "needs_attention":
        return (EventStatus.EXTERNALLY_MODIFIED, EventStatus.CONFLICT, EventStatus.MISSING)
    if key == "failed":
        return (EventStatus.PUSH_FAILED, EventStatus.UNCERTAIN)
    try:
        return (EventStatus(key),)
    except ValueError as exc:
        valid = ", ".join(s.value for s in EventStatus)
        console().print(f"[red]未知状态 {raw!r}[/red]；可用：{valid}, frozen, failed, all")
        raise typer.Exit(EXIT_FATAL) from exc


def _run_review_action(action: str, ids: list[str], **kwargs: object) -> None:
    """执行审批动作的公共外壳（记录运行 + 渲染结果）。"""
    settings = _load()
    setup_logging(settings, console=False)

    try:
        event_ids = parse_event_ids(ids)
    except ReviewError as exc:
        console().print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_FATAL) from exc

    if not event_ids:
        console().print("[yellow]未提供事件 id[/yellow]")
        raise typer.Exit(EXIT_PARTIAL)

    stats: dict[str, object] = {"action": action, "ids": event_ids}
    with recorded_run(settings, f"events {action}", stats=stats):
        with open_db(settings) as conn:
            queue = ReviewQueue(conn)
            if action == "edit":
                result = queue.edit(
                    event_ids[0],
                    title=kwargs.get("title"),  # type: ignore[arg-type]
                    start_ts=kwargs.get("start_ts"),  # type: ignore[arg-type]
                )
            else:
                handler = {
                    "approve": queue.approve,
                    "reject": queue.reject,
                    "ignore": queue.ignore,
                    "adopt": _adopt_many(queue),
                    "retry": queue.retry,
                }[action]
                result = handler(event_ids)  # type: ignore[operator]

        stats.update(result.as_dict())
        label = {
            "approve": "批准", "reject": "否决", "ignore": "忽略",
            "edit": "修正", "adopt": "接管", "retry": "重置重试",
        }.get(action, action)
        render_review_action(result, action=label)
        raise typer.Exit(EXIT_OK)


def _adopt_many(queue: ReviewQueue):
    """``adopt`` 需要逐个处理（每个事件独立判定是否冻结态）。"""
    def run(event_ids: list[int]):
        total = ReviewStats(requested=len(event_ids))
        for event_id in event_ids:
            one = queue.adopt(event_id)
            total.changed += one.changed
            total.skipped += one.skipped
            total.not_found += one.not_found
            total.reasons.extend(one.reasons)
        return total

    return run


@events_app.command("approve")
def events_approve(
    ids: Annotated[list[str], typer.Argument(help="事件 id，支持 1,2,3 或 1-5")],
) -> None:
    """批准事件（批准后由 push 写入日历）。"""
    _run_review_action("approve", ids)


@events_app.command("reject")
def events_reject(
    ids: Annotated[list[str], typer.Argument(help="事件 id，支持 1,2,3 或 1-5")],
) -> None:
    """否决事件（终态，不再提示）。"""
    _run_review_action("reject", ids)


@events_app.command("ignore")
def events_ignore(
    ids: Annotated[list[str], typer.Argument(help="事件 id，支持 1,2,3 或 1-5")],
) -> None:
    """忽略事件（终态；同指纹不再入队）。"""
    _run_review_action("ignore", ids)


@events_app.command("edit")
def events_edit(
    event_id: Annotated[int, typer.Argument(help="事件 id")],
    title: Annotated[str | None, typer.Option("--title", help="修正标题")] = None,
    start: Annotated[
        str | None, typer.Option("--start", help="修正开始时间（ISO8601，如 2026-09-20T10:00:00Z）")
    ] = None,
) -> None:
    """人工修正事件内容，并置为已批准。

    被修正的字段会记为 ``human`` 来源，后续 ICS 更新不会静默覆盖它。
    """
    _run_review_action("edit", [str(event_id)], title=title, start_ts=start)


@events_app.command("adopt")
def events_adopt(
    ids: Annotated[list[str], typer.Argument(help="事件 id（须处于冻结态）")],
) -> None:
    """接管被外部修改的事件：以当前日历内容为新基线并解冻。"""
    _run_review_action("adopt", ids)


@events_app.command("retry")
def events_retry(
    ids: Annotated[list[str], typer.Argument(help="事件 id（须为失败/待确认态）")],
) -> None:
    """把推送失败的事件重置为已批准，等待下次推送。"""
    _run_review_action("retry", ids)


# ──────────────────────────────────────────────────────────────
# push
# ──────────────────────────────────────────────────────────────


@app.command()
def push(
    approved: Annotated[
        bool, typer.Option("--approved", help="推送已批准的事件")
    ] = False,
    apply: Annotated[
        bool, typer.Option("--apply", help="真正写入日历（默认 dry-run）")
    ] = False,
    cancel: Annotated[
        str | None,
        typer.Option("--cancel", help="撤销延迟窗口内的推送（事件 id 或队列 id）"),
    ] = None,
    due: Annotated[
        bool, typer.Option("--due", help="处理到点的延迟窗口项")
    ] = False,
    archive: Annotated[
        int | None, typer.Option("--archive", help="归档式取消某事件（置为 cancelled）")
    ] = None,
    hard_delete: Annotated[
        int | None, typer.Option("--hard-delete", help="硬删除某事件（不可恢复）")
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="覆盖日历后端：auto/google/fake（演练用 fake）"),
    ] = None,
) -> None:
    """把已批准的事件写入日历。

    **默认 dry-run**。写入路径有四个安全保证：只处理已批准；创建前按
    ``auto_mail_key`` 反查（幂等，避免重跑产生重复）；检测到用户手改则冻结；
    删除默认归档而非硬删。

    需要 ``--apply`` 才真正写入。
    """
    settings = _load()
    setup_logging(settings, console=False)
    settings.apply_proxy_env()

    stats: dict[str, object] = {"dry_run": not apply}
    with recorded_run(settings, "push", stats=stats):
        with open_db(settings) as conn:
            # 后端必须在连接建立之后构造：它需要 conn 读取 needs_reauth 标记
            calendar = _resolve_calendar(settings, conn, backend)
            queue = ReviewQueue(conn)
            engine = PushEngine(settings, conn, calendar)

            # 撤销：即使 dry-run 也执行（它是本地状态回退，无外部副作用）
            if cancel is not None:
                result = queue.cancel_push(cancel)
                render_review_action(result, action="撤销推送")
                stats["cancel"] = cancel
                if result.changed == 0:
                    raise typer.Exit(EXIT_PARTIAL)
                raise typer.Exit(EXIT_OK)

            if archive is not None:
                result = engine.archive(archive, apply=apply)
                render_push(result)
                stats.update(result.as_dict())
                raise typer.Exit(EXIT_OK)

            if hard_delete is not None:
                result = engine.hard_delete(hard_delete, apply=apply)
                render_push(result)
                stats.update(result.as_dict())
                raise typer.Exit(EXIT_OK)

            # 展示延迟窗口内即将自动入历的事件（可撤销对象）
            window = queue.pending_window(within_minutes=60)
            if window:
                render_push_window(window)

            if due:
                result = engine.dispatch_due(apply=apply)
            else:
                result = engine.push_approved(apply=apply)

        stats.update(result.as_dict())
        render_push(result)
        raise typer.Exit(EXIT_OK)


def _is_verbose() -> bool:
    """是否在终端里打印摘要全文（环境变量控制，默认不打印）。"""
    import os

    return os.environ.get("AUTOMAIL_VERBOSE", "").strip().lower() in {"1", "true", "yes"}


def _open_in_default_app(path: object) -> None:
    """在系统默认程序中打开文件（Windows 用 os.startfile）。"""
    import os

    target = str(path)
    try:
        if hasattr(os, "startfile"):  # Windows
            os.startfile(target)  # type: ignore[attr-defined]
        else:  # pragma: no cover - macOS/Linux 非本项目的目标平台
            import subprocess

            subprocess.run(["xdg-open", target], check=False)
    except OSError as exc:
        console().print(f"[yellow]无法自动打开文件[/yellow] {exc}")


def _resolve_calendar(settings: Settings, conn, override: str | None):
    """按 ``--backend`` 覆盖或配置选择日历后端。

    覆盖时用 ``model_copy`` 而不是改动原配置对象——避免一次命令行的临时选择
    污染后续逻辑（例如摘要与推送用不同后端）。

    **``google`` 模式必须前置校验凭据**：后端是惰性构造的，若没有事件要推，
    它根本不会被触碰，于是「缺 token」会被静默报告为成功。那会让使用者以为
    配置没问题。强制模式下必须当场失败。
    """
    if override is None:
        return _build_calendar(settings, conn)

    choice = override.strip().lower()
    if choice not in {"auto", "google", "fake"}:
        console().print(
            f"[red]未知后端 {override!r}[/red]；可选：auto、google、fake"
        )
        raise typer.Exit(EXIT_FATAL)

    resolved = settings.model_copy(update={"calendar_backend": choice})

    if choice == "google":
        from .calendar.auth import inspect_status

        info = inspect_status(resolved, conn)
        if not info.ready:
            console().print("[red]已指定 --backend google，但授权未就绪[/red]")
            render_auth_status(info)
            raise typer.Exit(EXIT_FATAL)

    return _build_calendar(resolved, conn)


def _build_calendar(settings: Settings, conn=None, *, announce: bool = True):
    """构造日历后端。

    ``auto``（默认）按凭据自动选择：有 credentials.json + token.json 就用真实
    Google，否则退回内存实现。**必须如此**——若默认用内存实现，使用者会以为
    事件写进了真实日历，实际只写进了内存，且下次运行就消失。

    ``fake`` 用于演练：可以完整走一遍审批与推送流程而不触碰真实日历。
    """
    choice = settings.calendar_backend

    if choice == "fake":
        from .calendar.fake import FakeCalendar

        return FakeCalendar()

    if choice == "auto":
        from .calendar.auth import inspect_status

        info = inspect_status(settings, conn)
        if not info.ready:
            if announce:
                console().print(
                    f"[yellow]日历未接入[/yellow]（{info.detail}）——"
                    "本轮使用内存后端，事件不会写入真实日历。"
                )
                console().print(
                    "[dim]运行 automail auth 完成授权后可写入真实日历；"
                    "或用 automail push --backend fake 明确演练。[/dim]"
                )
            from .calendar.fake import FakeCalendar

            return FakeCalendar()

    from .calendar.gcal import GoogleCalendarBackend

    return GoogleCalendarBackend(settings, conn=conn)


# ──────────────────────────────────────────────────────────────
# threads / digest / stats
# ──────────────────────────────────────────────────────────────


@app.command()
def threads(
    apply: Annotated[
        bool, typer.Option("--apply", help="真正写入线程数据（默认 dry-run）")
    ] = False,
) -> None:
    """从 References / In-Reply-To 重建邮件线程（163 不支持服务端 THREAD）。

    全量重建，结果只取决于输入，因此不会随运行次数漂移。
    默认 dry-run，只报告会分出多少线程。
    """
    settings = _load()
    setup_logging(settings, console=False)

    stats: dict[str, object] = {"dry_run": not apply}
    with recorded_run(settings, "threads", stats=stats):
        with open_db(settings) as conn:
            result = ThreadBuilder(settings, conn).rebuild(apply=apply)

        stats.update(result.as_dict())
        render_threads(result)
        raise typer.Exit(EXIT_OK)


@app.command()
def digest(
    open_file: Annotated[
        bool, typer.Option("--open", help="生成后在默认程序中打开")
    ] = False,
) -> None:
    """生成每日摘要（Markdown，落本地 out/ 目录）。

    包含：今日日历事件、延迟窗口内即将入历（可撤销）、待审事件、
    需要关注、近期邮件、活跃线程、系统状态。

    **只写本地文件，不发邮件**——发信属不可逆的对外动作，v1 不做。
    """
    settings = _load()
    setup_logging(settings, console=False)

    stats: dict[str, object] = {}
    with recorded_run(settings, "digest", stats=stats):
        with open_db(settings) as conn:
            builder = DigestBuilder(settings, conn, calendar=_build_calendar(settings, conn))
            path, content = builder.build()

        stats["path"] = str(path)
        stats["bytes"] = len(content.encode("utf-8"))
        render_digest(path, content if _is_verbose() else None)

        if open_file:
            _open_in_default_app(path)
        raise typer.Exit(EXIT_OK)


@app.command()
def stats(
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON")] = False,
) -> None:
    """只读统计：同步量、抽取状态、事件来源分布、线程、降级次数。

    **全部只读**，没有任何副作用。
    """
    settings = _load()
    setup_logging(settings, console=False)

    with open_db(settings) as conn:
        snapshot = StatsCollector(settings, conn).collect()

    if as_json:
        import json

        typer.echo(json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2))
    else:
        render_stats(snapshot)
    raise typer.Exit(EXIT_OK)


@app.command()
def setup(
    quiet: Annotated[
        bool, typer.Option("--quiet", help="只报告状态，不打印引导")
    ] = False,
) -> None:
    """便携版首次运行引导：创建目录、生成 .env、提示缺失的凭据。

    打包成 exe 后双击运行时也会自动执行一次（见下方 _maybe_first_run_guide）。
    它不是必需步骤——`doctor` 同样能告诉你缺什么。
    """
    settings = _load()
    setup_logging(settings, console=False)

    from .portable import (
        describe_missing,
        ensure_portable_layout,
        find_config_sources,
        print_first_run_guide,
    )
    from .settings import app_base_dir

    base = app_base_dir()
    report = ensure_portable_layout(base, template=_env_template_path())
    report.discovered = find_config_sources(base)

    if quiet:
        render_setup(report)
    else:
        print_first_run_guide(report)

    _ = describe_missing  # 已在引导函数内使用
    raise typer.Exit(EXIT_OK if report.ready else EXIT_PARTIAL)


def _env_template_path() -> Path | None:
    """``.env.example`` 的位置（源码目录或打包后的资源目录）。

    PyInstaller 的 onedir 布局下，spec 里写成 ``datas=[(..., ".")]`` 的条目
    实际落在资源目录 ``_internal/``（``sys._MEIPASS`` 指向它），
    而不是 exe 同级——因此两处都要探测。
    """
    import sys as _sys

    candidates: list[Path] = []

    bundle = getattr(_sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / ".env.example")

    if getattr(_sys, "frozen", False):
        exe_dir = Path(_sys.executable).resolve().parent
        candidates.append(exe_dir / ".env.example")
        # onedir 布局：资源在 _internal 下
        candidates.append(exe_dir / "_internal" / ".env.example")

    candidates.append(Path(__file__).resolve().parents[2] / ".env.example")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


@app.command()
def backup(
    prune_only: Annotated[
        bool, typer.Option("--prune-only", help="只清理旧备份，不新建")
    ] = False,
) -> None:
    """备份数据库并清理超期备份。

    **为什么需要它**：自动备份只在「检测到待执行迁移」时触发，正常使用中
    可能很久不备份。而数据库里累积的是邮件片段与已抽取事件，值得定期留一份。

    保留策略由 ``DB_BACKUP_KEEP``（数量）与 ``DB_BACKUP_MAX_AGE_DAYS``
    （年龄）控制，两者任一超限即清理。

    注意：备份同样**不含邮件正文**（库里只存脱敏片段），因此备份文件
    不会扩大隐私面，但也意味着它不能替代原始邮件。
    """
    settings = _load()
    setup_logging(settings, console=False)

    stats: dict[str, object] = {"prune_only": prune_only}
    with recorded_run(settings, "backup", stats=stats):
        from .db import backup_db, prune_backups

        with open_db(settings) as conn:
            if prune_only:
                removed = prune_backups(settings)
                stats["removed"] = len(removed)
            else:
                target = backup_db(settings, conn)
                remaining = sorted(
                    p.name for p in settings.backup_dir.glob("automail-*.db")
                )
                stats["path"] = str(target)
                stats["size_bytes"] = target.stat().st_size
                stats["kept"] = len(remaining)

        render_backup(stats, settings)
        raise typer.Exit(EXIT_OK)


@app.command("pause")
def pause_cmd(
    resume: Annotated[
        bool, typer.Option("--resume", help="恢复自动运行（默认是暂停）")
    ] = False,
) -> None:
    """暂停/恢复计划任务的自动运行。

    暂停只是创建一个标记文件，**不卸载计划任务**——卸载了还得记得装回来。

    只影响计划任务触发的 ``run``；图形界面里手点的「立即同步」不受影响
    （点它说明你现在就想跑一次，与"别在我不知情时自动跑"是两回事）。
    """
    from .pause import is_paused, pause_file, set_paused

    settings = _load()
    setup_logging(settings, console=False)

    want_paused = not resume
    try:
        changed = set_paused(settings, want_paused)
    except OSError as exc:
        console().print(f"[red]无法修改暂停状态[/red]：{exc}")
        raise typer.Exit(EXIT_FATAL) from exc

    state = "已暂停" if want_paused else "已恢复"
    if changed:
        console().print(f"[green]{state}[/green]自动运行")
    else:
        console().print(f"[dim]本来就是{state}状态[/dim]")
    console().print(f"标记文件：{pause_file(settings)}（当前{'存在' if is_paused(settings) else '不存在'}）")


@app.command()
def run(
    apply: Annotated[
        bool, typer.Option("--apply", help="真正执行（默认 dry-run，只报告）")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="同步阶段的本轮上限")
    ] = None,
    digest: Annotated[
        bool, typer.Option("--digest", help="末尾同时生成摘要")
    ] = False,
    no_sync: Annotated[
        bool, typer.Option("--no-sync", help="跳过同步（只处理本地已有邮件）")
    ] = False,
    no_extract: Annotated[
        bool, typer.Option("--no-extract", help="跳过抽取")
    ] = False,
    no_push: Annotated[
        bool, typer.Option("--no-push", help="跳过推送（只同步与抽取）")
    ] = False,
    ignore_pause: Annotated[
        bool, typer.Option("--ignore-pause", help="忽略暂停标记，强制跑一轮")
    ] = False,
) -> None:
    """串起 sync → extract → push [→ digest] 的主流程（供计划任务调用）。

    **阶段之间互不阻塞**：同步失败仍会抽取本地已有邮件，抽取失败仍会推送
    已批准事件。整体退出码取最严重的那一个阶段。

    用 TTL 单实例锁防止计划任务重叠——拿不到锁不算错误（说明上次还在跑），
    退出码为 1。

    默认 dry-run；加 ``--apply`` 才真正写库与写日历。
    """
    settings = _load()
    setup_logging(settings, console=False)
    settings.apply_proxy_env()

    # 暂停标记只挡**计划任务**触发的这一条路径。
    # 图形界面里的「立即同步」直接调 Pipeline，因此不受影响——点它说明
    # 使用者现在就想跑一次，与"别在我不知情时自动跑"是两回事。
    from .pause import is_paused, pause_file

    if is_paused(settings) and not ignore_pause:
        console().print(
            "[yellow]自动运行已暂停[/yellow]，本轮未执行任何操作。\n"
            f"标记文件：{pause_file(settings)}\n"
            "删除该文件即可恢复（或用图形界面的「暂停自动运行」开关）。"
        )
        raise typer.Exit(EXIT_OK)

    stats: dict[str, object] = {"dry_run": not apply}
    with recorded_run(settings, "run", stats=stats) as run_id:
        with open_db(settings) as conn:
            pipeline = Pipeline(settings, conn, run_id=run_id)
            result = pipeline.run(
                apply=apply,
                limit=limit,
                with_digest=digest,
                llm=_build_llm(settings),
                calendar=_build_calendar(settings, conn),
                skip_sync=no_sync,
                skip_extract=no_extract,
                skip_push=no_push,
            )

        stats.update(result.as_dict())
        render_pipeline(result)
        raise typer.Exit(result.exit_code)


# ──────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────

def _render_cli_error(exc: BaseException) -> None:
    """渲染解析层错误。

    ``standalone_mode=False`` 时 click/typer 不打印用法错误而是直接抛出，
    因此这里需要自己渲染，否则用户只看到退出码没有原因。

    例外：不带参数调用时 Typer 已自行打印过帮助，且异常消息为空，
    此时不能再补一行空错误。
    """
    formatter = getattr(exc, "format_message", None)
    message = formatter() if callable(formatter) else str(exc)
    if not message or not message.strip():
        return
    console().print(f"[red]参数错误[/red] {sanitize_text(message)}")


def main() -> int:
    """``pyproject`` 的 console_scripts 入口。

    退出码有两个来源，都必须处理：
    * 命令内部 ``raise typer.Exit(code)`` —— 非 standalone 模式下会被
      click **返回**（不是抛出），所以必须取 ``app()`` 的返回值；
    * 解析层错误（未知子命令、缺参）—— 会带着 ``exit_code`` 抛出。
    """
    # 中文控制台默认 GBK，邮件主题里的 emoji 会让输出抛 UnicodeEncodeError
    # （实测 stats 因此完全不可用）。console_scripts 入口不经过
    # automail.__main__，所以这里也要做一次。
    from .__main__ import _make_output_lenient

    _make_output_lenient()

    try:
        result = app(standalone_mode=False)
    except typer.Exit as exc:  # 防御性分支：某些路径仍可能直接抛出
        return int(exc.exit_code or 0)
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        console().print("[yellow]已中断[/yellow]")
        return EXIT_FATAL
    except Exception as exc:  # noqa: BLE001 - 兜底，避免把栈打到用户脸上
        code = getattr(exc, "exit_code", None)
        if isinstance(code, int) and code != 0:
            _render_cli_error(exc)
            return code
        if isinstance(exc, DatabaseError):
            console().print(f"[red]数据库错误[/red] {sanitize_error(exc)}")
            return EXIT_FATAL
        console().print(f"[red]未预期的错误[/red] {sanitize_error(exc)}")
        return EXIT_FATAL

    if isinstance(result, int):
        return result
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
