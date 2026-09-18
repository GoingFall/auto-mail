"""便携版打包支持：首次运行引导与配置分发。

## 为什么需要这个模块

打包成 exe 后，使用者面对的是一个目录，里面缺少三样东西：

1. ``.env`` —— 配置（邮箱授权码、LLM key）
2. ``credentials.json`` / ``token.json`` —— Google 日历凭据
3. ``data/`` —— 数据目录（首次运行会自动创建）

如果什么都不做，双击 exe 只会看到「缺少凭据」而不知从何下手。
因此提供：

* :func:`ensure_portable_layout` —— 首次运行时创建目录、从模板生成 ``.env``
* :func:`find_config_sources` —— 从常见位置**发现**已有配置，避免重复填写

## 设计取舍：不自动复制凭据

发现已有 ``.env`` / ``token.json`` 时，只**提示**位置，不自动复制。
理由：自动复制会让同一份密钥出现在两个地方，使用者后来改了其中一个而
不知道哪个生效——对凭据而言这种不确定性比多点一次复制更糟。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("automail.portable")

#: exe 目录下应由用户放置的文件
USER_FILES = ("credentials.json", "token.json", ".env")


@dataclass(slots=True)
class LayoutReport:
    """便携版目录状态（供 CLI 展示）。"""

    base_dir: Path
    created_dirs: list[Path] = field(default_factory=list)
    env_created: bool = False
    env_template: Path | None = None
    missing: list[str] = field(default_factory=list)
    discovered: dict[str, Path] = field(default_factory=dict)
    """在其它位置发现的同名文件：``{"token.json": Path(...)}``。"""

    @property
    def ready(self) -> bool:
        return not self.missing


def ensure_portable_layout(base_dir: Path, *, template: Path | None = None) -> LayoutReport:
    """确保便携目录就绪：建目录、按需从模板生成 ``.env``。

    Args:
        base_dir: exe 所在目录（:func:`~automail.settings.app_base_dir` 的结果）。
        template: ``.env`` 模板路径。打包后随 exe 分发。
    """
    report = LayoutReport(base_dir=base_dir)

    for name in ("data", "out", "logs"):
        target = base_dir / name
        if not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
                report.created_dirs.append(target)
            except OSError as exc:
                logger.warning("无法创建 %s：%s", target, exc)

    env_path = base_dir / ".env"
    if not env_path.exists() and template is not None and template.is_file():
        try:
            shutil.copyfile(template, env_path)
            report.env_created = True
            report.env_template = template
        except OSError as exc:
            logger.warning("无法生成 .env：%s", exc)

    for name in USER_FILES:
        if not (base_dir / name).exists():
            report.missing.append(name)

    return report


def find_config_sources(base_dir: Path, *, search_roots: list[Path] | None = None) -> dict[str, Path]:
    """在其它位置查找同名配置文件，返回 ``{文件名: 路径}``。

    用途是**提示**使用者「你已经有这些文件，可以复制过来」，
    而不是自动复制（见模块文档的取舍说明）。
    """
    roots = search_roots or _default_search_roots(base_dir)
    found: dict[str, Path] = {}

    for root in roots:
        if not root.is_dir():
            continue
        for name in USER_FILES:
            if name in found:
                continue
            candidate = root / name
            if not candidate.is_file():
                continue
            # 跳过本目录：那是我们刚刚生成/本来就有的文件，
            # 提示「可复制到本目录」纯属噪音（实测踩到）。
            try:
                if candidate.resolve().parent == base_dir.resolve():
                    continue
            except OSError:
                pass
            found[name] = candidate
    return found


def _default_search_roots(base_dir: Path) -> list[Path]:
    """常见的配置存放位置。

    刻意保持**少而准**：只查当前工作目录与用户主目录下的少数约定位置，
    不做全盘扫描（那既慢，又可能在别人的项目里找到无关文件）。

    **不含 ``base_dir`` 本身**——那是复制目标，不是来源。
    """
    roots: list[Path] = []

    cwd = Path.cwd()
    try:
        if cwd.resolve() != base_dir.resolve():
            roots.append(cwd)
    except OSError:
        roots.append(cwd)

    home = Path.home()
    for name in ("auto-mail", "automail", "Documents/auto-mail"):
        roots.append(home / name)
    return roots


def describe_missing(report: LayoutReport) -> list[str]:
    """把缺失项翻译成可执行的操作说明。"""
    lines: list[str] = []
    missing = set(report.missing)

    if ".env" in missing:
        lines.append("缺少 .env —— 从 .env.example 复制并填写（邮箱授权码、LLM key）")
    if "credentials.json" in missing:
        lines.append(
            "缺少 credentials.json —— Google Cloud 创建的「桌面应用」OAuth 客户端"
        )
    if "token.json" in missing:
        lines.append("缺少 token.json —— 运行一次 auto-mail auth 完成 Google 授权")

    for name, path in sorted(report.discovered.items()):
        lines.append(f"发现已有 {name}：{path}（可复制到本目录）")

    return lines


def print_first_run_guide(report: LayoutReport) -> None:
    """首次运行时的引导（纯文本，不依赖 rich，因为可能在引导早期调用）。"""
    base = report.base_dir
    print()
    print("=" * 72)
    print("auto-mail 便携版：首次运行")
    print("=" * 72)
    print(f"数据目录：{base}")
    print()
    if report.env_created:
        print("已根据模板生成 .env —— 请填写：")
        print("  IMAP_USER / IMAP_AUTH_CODE    163 邮箱与授权码")
        print("  LLM_BASE_URL / LLM_API_KEY    可选，用于提升抽取准确率")
    print()

    lines = describe_missing(report)
    if lines:
        print("还需要：")
        for line in lines:
            print("  · " + line)
    else:
        print("配置齐全。建议先运行：auto-mail doctor")
    print()
    print("常用命令：")
    print("  auto-mail doctor              自检（检查配置与连通性）")
    print("  auto-mail auth                Google 授权")
    print("  auto-mail run --apply         同步 + 抽取 + 推送")
    print("  auto-mail digest              生成摘要到 out\\")
    print("=" * 72)
