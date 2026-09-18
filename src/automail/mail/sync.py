"""增量同步算法。

实现 docs/spec-imap-sync.md §2 的算法。这是 P1 的核心，因此把每个决策点都
写成可独立验证的步骤，而不是塞进一个大循环：

1. 取 ``UIDVALIDITY`` 与 ``UIDNEXT``
2. UIDVALIDITY 变化 → 标 stale（**不删**）+ 重置游标
3. **预判门**：``UIDNEXT - 1 <= highest_uid`` 时无新邮件，跳过 SEARCH
4. 增量搜索 ``UID <highest+1>:*``（容忍 UID 不连续）
5. ``highest_uid`` 用 ``max()`` 累积
6. 新邮件先按内容匹配旧记录做 **reactivate**，未命中才完整入库
7. 检测已从前文件夹消失的邮件 → 标 ``folder_moved``（不删、不重抽）
8. 补偿扫描
9. 写 sync 层台账

第 3 步的必要性：RFC 3501 §6.4.8 明确规定 ``559:*`` **始终包含最后一封邮件**，
即使 559 高于任何已分配 UID。不先判断就会在追平后反复重取最后一封。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from typing import Any

from ..models import ExtractStatus, ProcessedMailStatus
from ..progress import ProgressReporter
from ..sanitize import sanitize_error
from ..settings import Settings
from ..store import (
    MessageRepository,
    ProcessedMailRepository,
    SenderRepository,
    SyncStateRepository,
)
from .backend import MailBackend, MailError, MailProtocolError, RawMessage
from .mime import ParsedMail, parse_message

logger = logging.getLogger("automail.mail.sync")


@dataclass(slots=True)
class FolderSyncStats:
    """单个文件夹的同步统计。"""

    folder: str = ""
    uid_validity: int = 0
    scanned: int = 0
    """本轮实际取回的邮件数。"""

    inserted: int = 0
    reactivated: int = 0
    duplicated: int = 0
    """跨文件夹副本（标记为 duplicate_of，不重抽）。"""

    skipped_known: int = 0
    """已在库中且无需更新。"""

    flags_refreshed: int = 0
    moved: int = 0
    """已从本文件夹消失（移走或删除），仅标记不删除。"""

    fetch_failed: int = 0
    uidvalidity_changed: bool = False
    gate_skipped: bool = False
    """被预判门拦下（无新邮件）。"""

    error: str | None = None
    """整个文件夹同步失败时的原因（不含正文）。

    没有这个字段时，文件夹级失败只能表现为 ``uid_validity=0`` + ``fetch_failed=1``，
    真实原因只落在日志里——使用者看到的是一行莫名其妙的零值。这是刻意补上的
    可观测性修复：**失败必须说明为什么失败**。
    """

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "folder": self.folder,
            "uid_validity": self.uid_validity,
            "scanned": self.scanned,
            "inserted": self.inserted,
            "reactivated": self.reactivated,
            "duplicated": self.duplicated,
            "skipped_known": self.skipped_known,
            "flags_refreshed": self.flags_refreshed,
            "moved": self.moved,
            "fetch_failed": self.fetch_failed,
            "uidvalidity_changed": self.uidvalidity_changed,
            "gate_skipped": self.gate_skipped,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class SyncStats:
    """整轮同步的统计。

    聚合属性必须覆盖**所有**会被报告展示的字段。曾经漏了 ``scanned``，
    导致摘要里出现「扫描 0，新增 1」这种自相矛盾的显示——使用者会以为
    统计坏了，进而怀疑整个同步结果。
    """

    folders: list[FolderSyncStats] = field(default_factory=list)
    compensated: bool = False
    zombies_reclaimed: int = 0
    dry_run: bool = True

    @property
    def scanned(self) -> int:
        """本轮实际取回的邮件数（跨文件夹合计）。"""
        return sum(f.scanned for f in self.folders)

    @property
    def inserted(self) -> int:
        return sum(f.inserted for f in self.folders)

    @property
    def reactivated(self) -> int:
        return sum(f.reactivated for f in self.folders)

    @property
    def duplicated(self) -> int:
        """跨文件夹副本数（已标记 duplicate_of，不重抽）。"""
        return sum(f.duplicated for f in self.folders)

    @property
    def moved(self) -> int:
        return sum(f.moved for f in self.folders)

    @property
    def fetch_failed(self) -> int:
        return sum(f.fetch_failed for f in self.folders)

    @property
    def flags_refreshed(self) -> int:
        return sum(f.flags_refreshed for f in self.folders)

    @property
    def has_errors(self) -> bool:
        """是否有文件夹级失败或其条目取回失败。"""
        return self.fetch_failed > 0 or any(f.error for f in self.folders)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "folders": [f.as_dict() for f in self.folders],
            "scanned": self.scanned,
            "inserted": self.inserted,
            "reactivated": self.reactivated,
            "duplicated": self.duplicated,
            "moved": self.moved,
            "fetch_failed": self.fetch_failed,
            "flags_refreshed": self.flags_refreshed,
            "compensated": self.compensated,
            "zombies_reclaimed": self.zombies_reclaimed,
        }


def parse_raw_message(raw: bytes):
    """把原始字节解析成 ``email.message.Message``。

    用 ``policy.default`` 以获得较好的 RFC 2047 与容错处理；解析出问题时会
    在 ``msg.defects`` 中体现，由 :mod:`automail.mail.mime` 记录而非抛出。
    """
    return BytesParser(policy=policy.default).parsebytes(raw)


class SyncEngine:
    """按文件夹增量同步的引擎。

    依赖注入 :class:`~automail.mail.backend.MailBackend`，因此可以用假后端
    做完整测试，不触碰网络。
    """

    def __init__(
        self,
        settings: Settings,
        conn,
        backend: MailBackend,
        *,
        account: str | None = None,
        reporter: ProgressReporter | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._backend = backend
        self._account = account or settings.account
        self._messages = MessageRepository(conn)
        self._processed = ProcessedMailRepository(conn)
        self._sync_state = SyncStateRepository(conn)
        self._senders = SenderRepository(conn)
        # 进度上报。默认 None → 全部空操作，行为与引入前完全一致。
        self._reporter = reporter or ProgressReporter()

    # ── 对外入口 ──────────────────────────────────────────

    def sync(self, *, apply: bool = False, limit: int | None = None) -> SyncStats:
        """同步所有配置的文件夹。

        Args:
            apply: ``False``（默认）为 dry-run——不写数据库，只报告将要发生的
                变化。dry-run 下仍会读服务端，因为「有多少新邮件」本身就是要
                回答的问题。
            limit: 本轮最多取回的新邮件数（跨文件夹合计）。``None`` 表示不限。
                用于首次同步大邮箱时分批进行，避免一次性拉取过多触发风控。
        """
        stats = SyncStats(dry_run=not apply)
        remaining = limit if limit is not None else -1

        if apply:
            # 崩溃留下的 running 中间态若不回退，抽取将永远卡住
            stats.zombies_reclaimed = self._messages.reclaim_zombies()

        for folder in self._settings.imap_folder_list:
            try:
                folder_stats = self._sync_folder(folder, apply=apply, remaining=remaining)
            except (MailError, OSError) as exc:
                # 单个文件夹失败不应中断其余文件夹，但**必须把原因带出来**：
                # 只报 uid_validity=0 + fetch_failed=1 会让人无从判断是凭据、
                # 风控、还是账号问题。原文经 sanitize 后只保留一行摘要。
                reason = sanitize_error(exc)
                logger.warning("文件夹 %s 同步失败：%s", folder, reason)
                failed = FolderSyncStats(folder=folder)
                failed.fetch_failed += 1
                failed.error = reason
                stats.folders.append(failed)
                continue
            stats.folders.append(folder_stats)

            if remaining > 0:
                remaining -= folder_stats.scanned
                if remaining <= 0:
                    logger.info("已达本轮上限 %s，剩余邮件留待下轮", limit)
                    break

            if apply and folder_stats.uidvalidity_changed:
                stats.compensated = True

        if apply and self._should_compensate():
            stats.compensated = True
            self._record_full_scan()
            logger.info("已触发补偿扫描窗口（下轮将重扫最近 %s 天）",
                        self._settings.compensate_days)

        return stats

    def _should_compensate(self) -> bool:
        for folder in self._settings.imap_folder_list:
            if self._sync_state.should_compensate(
                self._account, folder, scans=self._settings.compensate_scans
            ):
                return True
        return False

    def _record_full_scan(self) -> None:
        for folder in self._settings.imap_folder_list:
            state = self._sync_state.get(self._account, folder)
            if state is None:
                continue
            self._sync_state.upsert(
                self._account,
                folder,
                uid_validity=state.uid_validity,
                highest_uid=state.highest_uid,
                syncs_since_full=0,
                mark_full=True,
            )

    # ── 单文件夹同步 ──────────────────────────────────────

    def _sync_folder(
        self, folder: str, *, apply: bool, remaining: int = -1
    ) -> FolderSyncStats:
        stats = FolderSyncStats(folder=folder)

        status = self._backend.select_folder(folder, readonly=True)
        stats.uid_validity = status.uid_validity

        state = self._sync_state.get(self._account, folder)

        # 步骤 2：UIDVALIDITY 变化 → 标 stale + 重置游标
        uidvalidity_changed = state is not None and state.uid_validity != status.uid_validity
        if uidvalidity_changed:
            stats.uidvalidity_changed = True
            logger.warning(
                "文件夹 %s 的 UIDVALIDITY 变化（%s → %s）：旧记录标记 stale 并重扫",
                folder,
                state.uid_validity if state else None,
                status.uid_validity,
            )
            if apply:
                self._messages.mark_stale(self._account, folder)
            highest_uid = 0
            syncs_since_full = 0
        else:
            highest_uid = state.highest_uid if state else 0
            syncs_since_full = state.syncs_since_full if state else 0

        # 步骤 3–5：取新邮件
        #
        # 两条路径，取决于服务端是否提供 UIDNEXT：
        #
        # A) 提供 UIDNEXT（多数服务端）：预判门可以**完全跳过** SEARCH。
        #    RFC 3501 §6.4.8 规定 `n:*` 始终包含最后一封邮件，即使 n 高于任何
        #    已分配 UID；不预判就会在追平后反复重取最后一封。
        #
        # B) 不提供 UIDNEXT（**163 实测如此**：连显式
        #    `STATUS INBOX (UIDNEXT)` 都会被静默丢弃，只回 MESSAGES/UIDVALIDITY）：
        #    无法预判，每次都必须 SEARCH，然后靠**客户端过滤** `uid > highest_uid`
        #    剔除 range 带回来的那封。因此对 163 而言，客户端过滤不是"兜底"而是
        #    主路径——这点已在真实服务器上验证。
        #
        # 两条路径的正确性相同；差别只是 B 会多一次 SEARCH（便宜，且无法避免），
        # 但**不会多取正文**（正文才是昂贵的部分，见下方 new_uids 为空时跳过）。
        new_uids: list[int] = []
        gate_skipped = False

        uid_next_known = status.uid_next is not None
        if uid_next_known and status.uid_next - 1 <= highest_uid:  # type: ignore[operator]
            gate_skipped = True
            logger.debug(
                "文件夹 %s 无新邮件（UIDNEXT=%s, highest=%s），跳过 SEARCH",
                folder,
                status.uid_next,
                highest_uid,
            )
        else:
            candidates = self._backend.search_uids(f"{highest_uid + 1}:*")
            # 客户端过滤：剔除 range 带回的最后一封（路径 B 的核心步骤）
            new_uids = [uid for uid in candidates if uid > highest_uid]

            if not new_uids:
                # SEARCH 已发，但确实没有新邮件。如实记为"无新邮件"，
                # 让统计与 digest 能看出「本轮没有变化」而不是「没检查」。
                gate_skipped = True

            # 本轮上限：截断后只取前 N 封，游标停在这批最后一封，
            # 剩余部分留待下轮（防止首次同步大邮箱一次性拉爆）
            if remaining > 0 and len(new_uids) > remaining:
                new_uids = new_uids[:remaining]

        stats.gate_skipped = gate_skipped

        if new_uids:
            stats.scanned = len(new_uids)
            self._ingest(new_uids, folder, status.uid_validity, stats, apply=apply)
            # 游标用 max() 累积：UIDNEXT-1 是「历史最大分配值」，
            # 可能大于当前存在的最大 UID，直接赋值会倒退
            highest_uid = max(highest_uid, max(new_uids))

        # 步骤 7：检测从本文件夹消失的邮件
        #
        # 必须**无条件**执行：邮件被移走时通常没有新邮件到达，此时预判门会命中，
        # 若把移动检测挂在「有增量」条件下，最常见的场景反而永远不会被检测到。
        if apply:
            stats.moved = self._detect_moves(folder, status.uid_validity, apply=apply)

        # 步骤 8：写游标
        if apply:
            self._sync_state.upsert(
                self._account,
                folder,
                uid_validity=status.uid_validity,
                highest_uid=highest_uid,
                syncs_since_full=syncs_since_full + 1,
                mark_full=uidvalidity_changed,
            )

        return stats

    # ── 入库 ──────────────────────────────────────────────

    def _ingest(
        self,
        uids: list[int],
        folder: str,
        uid_validity: int,
        stats: FolderSyncStats,
        *,
        apply: bool,
    ) -> None:
        """分批取回并入库。

        **必须分批**：一次性 ``FETCH`` 大量 UID 时连接更容易被服务端中断。

        **必须能重连续传**：163 会在任意时刻使会话失效，实测的原因包括
        ``Autologout; idle for too long``（空闲超时，实测约 2~4 分钟）、
        另一客户端登录把本会话踢掉、以及限流断连。遇到这类情况若直接放弃，
        整轮同步就会报出大量无意义的失败——实测曾出现「89 封里 69 封失败」。
        因此每批取回都带「重连 + 重试」：会话失效属**预期事件**，不是故障。
        """
        batch_size = max(1, self._settings.imap_fetch_batch_size)
        fetched_any = False

        for start in range(0, len(uids), batch_size):
            batch = uids[start : start + batch_size]
            try:
                fetched = self._fetch_batch_resilient(batch, folder)
            except MailError as exc:
                # 重试耗尽：记录下来，继续尝试后续批次（也许后续批能成功）
                reason = sanitize_error(exc)
                logger.warning(
                    "取回批次放弃（%s 起 %s 封）：%s", batch[0], len(batch), reason
                )
                stats.fetch_failed += len(batch)
                if apply:
                    for uid in batch:
                        self._processed.mark(
                            self._account, folder, uid_validity, uid,
                            ProcessedMailStatus.FETCH_FAILED, error=reason,
                        )
                # 失败的批次也要计入进度，否则进度条会永远差一截
                self._reporter.progress("sync", start + len(batch), len(uids))
                continue

            fetched_any = True
            self._ingest_batch(fetched, batch, folder, uid_validity, stats, apply=apply)
            # 逐批上报：一个文件夹可能有几百封，只报阶段级的话界面会长时间静止
            self._reporter.progress("sync", start + len(batch), len(uids))

        if not fetched_any and uids:
            # 全部批次都失败——向上抛出，让文件夹级处理报出真实原因，
            # 而不是伪装成「0 封新邮件」
            raise MailProtocolError(
                f"{folder}: 全部 {len(uids)} 封邮件取回失败，可能是服务端风控限流"
            )

    def _fetch_batch_resilient(self, batch: list[int], folder: str) -> dict[int, RawMessage]:
        """取回一批邮件；会话失效时重连并重试。

        这是应对 163 会话随时失效的核心机制。重试次数与退避由配置控制，
        重试耗尽才向上抛出，交由批次级处理记录失败。
        """
        attempts = max(0, self._settings.imap_reconnect_attempts)
        last_error: MailError | None = None

        for attempt in range(attempts + 1):
            try:
                return self._backend.fetch_messages(batch)
            except MailError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = self._settings.imap_reconnect_backoff_seconds * (attempt + 1)
                logger.warning(
                    "取回失败，%.0fs 后重连重试（第 %s/%s 次）：%s",
                    delay, attempt + 1, attempts, sanitize_error(exc),
                )
                if delay:
                    time.sleep(delay)
                try:
                    self._reconnect(folder)
                except MailError as reconnect_exc:
                    # 重连本身失败：继续下一轮尝试（可能服务端只是短暂拒绝）
                    logger.warning("重连失败：%s", sanitize_error(reconnect_exc))
                    last_error = reconnect_exc

        assert last_error is not None
        raise last_error

    def _reconnect(self, folder: str) -> None:
        """关闭旧会话并建立新会话，重新选中文件夹。

        163 断连后旧 socket 已不可用，**必须**重建连接——在原连接上重试
        只会继续拿到 ``Autologout``。
        """
        try:
            self._backend.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响重连
            pass
        self._backend.connect()
        self._backend.select_folder(folder, readonly=True)

    def _ingest_batch(
        self,
        fetched: dict[int, RawMessage],
        uids: list[int],
        folder: str,
        uid_validity: int,
        stats: FolderSyncStats,
        *,
        apply: bool,
    ) -> None:
        """处理一个已成功取回的批次。"""
        for uid in uids:
            message = fetched.get(uid)
            if message is None:
                # 服务端未返回该 UID（可能在搜索后被删除）
                stats.fetch_failed += 1
                if apply:
                    self._processed.mark(
                        self._account, folder, uid_validity, uid,
                        ProcessedMailStatus.FETCH_FAILED,
                        error="服务端未返回该 UID",
                    )
                continue

            try:
                self._ingest_one(message, folder, uid_validity, stats, apply=apply)
            except Exception as exc:  # noqa: BLE001 - 单封失败不拖垮整批
                logger.warning("处理 UID %s 失败：%s", uid, exc)
                stats.fetch_failed += 1
                if apply:
                    self._processed.mark(
                        self._account, folder, uid_validity, uid,
                        ProcessedMailStatus.FETCH_FAILED,
                        error=_brief(exc),
                    )

    def _ingest_one(
        self,
        message: RawMessage,
        folder: str,
        uid_validity: int,
        stats: FolderSyncStats,
        *,
        apply: bool,
    ) -> None:
        parsed = parse_message(
            parse_raw_message(message.raw), max_chars=self._settings.excerpt_max_chars
        )
        flags_text = " ".join(message.flags) or None
        is_unread = not any(f.lower() == "\\seen" for f in message.flags)

        known = self._messages.find_by_uid(self._account, folder, uid_validity, message.uid)
        if known is not None:
            # 已入库：只刷新 flags（步骤 7 的要求）
            stats.skipped_known += 1
            if apply and known.uid != message.uid:  # pragma: no cover - 不可能
                pass
            if apply:
                self._messages.update_flags(known.id, flags_text)
                stats.flags_refreshed += 1
            return

        # 步骤 6：先找可复用的旧记录（UIDVALIDITY 变化 / 邮件被移动）
        reusable = self._messages.find_reusable(
            self._account,
            folder,
            body_sha256=parsed.body.sha256,
            normalized_message_id=parsed.message_id,
        )
        if reusable is not None and (reusable.stale or reusable.folder_moved):
            stats.reactivated += 1
            logger.debug(
                "UID %s 命中旧记录 id=%s（stale=%s, moved=%s），复活并跳过抽取",
                message.uid, reusable.id, reusable.stale, reusable.folder_moved,
            )
            if apply:
                self._messages.reactivate(
                    reusable.id,
                    account=self._account,
                    folder=folder,
                    uid_validity=uid_validity,
                    uid=message.uid,
                    flags=flags_text,
                )
                self._processed.mark(
                    self._account, folder, uid_validity, message.uid,
                    ProcessedMailStatus.SYNCED,
                )
            self._record_sender(parsed, is_unread=is_unread, apply=apply)
            return

        # 跨文件夹副本：记录 + 标记 + 不重跑抽取
        duplicate = self._messages.find_duplicate_elsewhere(
            self._account,
            body_sha256=parsed.body.sha256,
            normalized_message_id=parsed.message_id,
        )
        is_canonical = duplicate is None
        if duplicate is not None:
            stats.duplicated += 1

        stats.inserted += 1
        if not apply:
            return

        new_id = self._messages.insert(
            self._account,
            folder,
            uid_validity,
            message.uid,
            normalized_message_id=parsed.message_id,
            is_canonical=1 if is_canonical else 0,
            duplicate_of=duplicate.id if duplicate else None,
            in_reply_to=parsed.in_reply_to,
            references_ids=" ".join(parsed.references) or None,
            subject=parsed.subject,
            from_addr=parsed.from_addr,
            from_name=parsed.from_name,
            to_addrs=", ".join(parsed.to_addrs) or None,
            sent_at=parsed.sent_at,
            received_at=message.internal_date,
            has_ics=1 if parsed.has_ics else 0,
            has_unsubscribe=1 if parsed.has_unsubscribe else 0,
            list_id=parsed.list_id,
            auto_submitted=parsed.auto_submitted,
            body_excerpt=parsed.body.text,
            body_sha256=parsed.body.sha256,
            flags=flags_text,
        )

        # 副本不参与抽取（内容与规范记录相同）
        if not is_canonical:
            self._messages.set_extract_status(new_id, ExtractStatus.DONE)

        self._processed.mark(
            self._account, folder, uid_validity, message.uid, ProcessedMailStatus.SYNCED
        )
        self._record_sender(parsed, is_unread=is_unread, apply=apply)

    def _record_sender(self, parsed: ParsedMail, *, is_unread: bool, apply: bool) -> None:
        if not apply or not parsed.from_addr:
            return
        self._senders.record(
            self._account,
            parsed.from_addr,
            name=parsed.from_name or None,
            has_unsubscribe=parsed.has_unsubscribe,
            unread=is_unread,
            last_seen_at=parsed.sent_at,
        )

    # ── 消失邮件检测 ──────────────────────────────────────

    def _detect_moves(self, folder: str, uid_validity: int, *, apply: bool) -> int:
        """把「已不在本文件夹」的邮件标记为 folder_moved。

        **不删除记录**：事件关联必须保留。目标文件夹若也在同步范围内，会在那边
        按内容匹配到这条记录并 reactivate。
        """
        try:
            present = set(self._backend.search_uids("ALL"))
        except MailError as exc:
            logger.warning("无法获取 %s 的完整 UID 集，跳过移动检测：%s", folder, exc)
            return 0

        known_uids = {
            record.uid
            for record in self._messages.list_all_for_extract(self._account)
            if record.folder == folder
            and record.uid_validity == uid_validity
            and not record.folder_moved
        }

        moved = 0
        for uid in sorted(known_uids - present):
            record = self._messages.find_by_uid(self._account, folder, uid_validity, uid)
            if record is None:
                continue
            if apply:
                self._messages.mark_folder_moved(record.id, None)
            moved += 1
        return moved


def _brief(exc: object) -> str:
    """给台账写一行短错误，不含正文。"""
    return sanitize_error(exc)
