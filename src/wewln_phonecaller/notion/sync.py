"""Two-way synchronisation between Notion and the local SQLite database.

Polling, because Notion webhooks need a public HTTPS endpoint that a local backend does not have.
One cycle:
1. find the four databases and check their structure (a broken structure stops the cycle);
2. read every campaign and contact (full scans; deleted pages are detected only when a scan is
   complete);
3. plan the jobs locally (do-not-call, phone problems, stop and pause rules: see the repository);
4. write back: statuses set in wewln (stop), do-not-call marks set locally, phone problems, job
   pages (created once per idempotency key, then updated), results and campaign summaries.

Duplicates are prevented by the idempotency key («Ключ») of every job and result: before a page
is created the campaign's pages are searched for the key, and a create that Notion did not
confirm is never repeated blindly. Only one cycle runs at a time (a lease in SQLite), and logs
contain counts, codes and IDs only, never names, phone numbers or tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from ..config import Settings
from ..domain import (
    FINAL_JOB_STATUSES,
    JOB_REASON_BY_LABEL,
    JOB_STATUS_BY_LABEL,
    JOB_STATUS_LABELS,
    OPEN_JOB_STATUSES,
    OUTCOME_LABELS,
    JobReason,
    JobStatus,
    Outcome,
    reason_label,
)
from ..errors import WewlnError
from ..phones import PHONE_ERROR_TEXT_RU, normalize_phone
from ..safety import evaluate_real_calls
from ..storage.repository import (
    NotionCampaignRecord,
    NotionContactRecord,
    Repository,
    campaign_status_label,
)
from . import props
from .client import (
    NotionAccessError,
    NotionAuthError,
    NotionClient,
    NotionError,
    NotionRateLimitedError,
    NotionUnavailableError,
    NotionUncertainWriteError,
)
from .ids import compact_id
from .schema import (
    CAMPAIGN_ERROR,
    CAMPAIGN_LANGUAGE,
    CAMPAIGN_MAX_ATTEMPTS,
    CAMPAIGN_SCRIPT,
    CAMPAIGN_STATUS,
    CAMPAIGN_SUMMARY,
    CONTACT_CAMPAIGNS,
    CONTACT_DNC,
    CONTACT_DNC_REASON,
    CONTACT_ERROR,
    CONTACT_PHONE,
    JOB_ATTEMPTS,
    JOB_CAMPAIGN,
    JOB_CONTACT,
    JOB_KEY,
    JOB_REASON,
    JOB_SIMULATED,
    JOB_STATUS,
    JOB_UPDATED,
    RESULT_CAMPAIGN,
    RESULT_CONTACT,
    RESULT_DURATION,
    RESULT_JOB,
    RESULT_KEY,
    RESULT_OUTCOME,
    RESULT_SIMULATED,
    RESULT_STARTED,
    RESULT_SUMMARY,
)
from .setup import NotionSetupError, Workspace, resolve_workspace

log = logging.getLogger("wewln.notion.sync")

T = TypeVar("T")

LOCK_NAME = "notion_sync"
LOCK_TTL_SECONDS = 300.0
LOCK_RENEW_SECONDS = 60.0
# A page whose creation Notion did not confirm is searched for this long before it is re-created
# (a new page can take a moment to appear in query results).
UNCERTAIN_GRACE_SECONDS = 300.0
MAX_PROBLEMS = 50
DNC_REASON_FROM_WEWLN = "Добавлено в wewln"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class SyncResult:
    status: str  # ok | partial | failed | skipped
    started_at: str
    finished_at: str
    stats: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    retry_after: float | None = None
    run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Cycle:
    client: NotionClient
    workspace: Workspace | None = None
    stats: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    campaign_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    contact_pages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def count(self, name: str, n: int = 1) -> None:
        self.stats[name] = self.stats.get(name, 0) + n

    def problem(self, text: str) -> None:
        self.count("problems")
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(text)

    @property
    def ws(self) -> Workspace:
        if self.workspace is None:
            raise RuntimeError("workspace not resolved")
        return self.workspace


def campaign_summary(counts: dict[str, int], *, simulated: bool) -> str:
    total = sum(counts.values())
    if total:
        parts = [f"{JOB_STATUS_LABELS[s]}: {counts[s.value]}" for s in JobStatus if counts.get(s)]
        text = f"Заданий: {total} (" + ", ".join(parts) + ")"
    else:
        text = "Заданий пока нет"
    if simulated:
        text += ". Режим симуляции: реальные звонки не выполняются"
    return text


def _is_fatal(exc: NotionError) -> bool:
    """Errors that would repeat for every request end the cycle (token, access, limits, outage)."""
    if isinstance(exc, NotionUncertainWriteError):
        return False
    if isinstance(exc, NotionAccessError):
        return exc.status == 403
    return isinstance(exc, NotionAuthError | NotionRateLimitedError | NotionUnavailableError)


def _by_creation(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(pages, key=lambda p: (str(p.get("created_time") or ""), str(p.get("id"))))


_UNKNOWN: dict[str, Any] = {}


class NotionSync:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        *,
        client_factory: Callable[[], NotionClient] | None = None,
        owner: str | None = None,
        uncertain_grace: float = UNCERTAIN_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self._client_factory = client_factory or (lambda: NotionClient.from_settings(settings))
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._grace = uncertain_grace
        self._clock = clock
        self._running = asyncio.Lock()
        self._lock_renewed = 0.0
        # Campaign calls are simulated unless real calls are allowed (never in this version).
        self._simulated = not evaluate_real_calls(settings).allowed
        self.last_result: SyncResult | None = None
        self.last_success_at: str | None = None
        self.consecutive_failures = 0

    @property
    def running(self) -> bool:
        return self._running.locked()

    async def run_once(self) -> SyncResult:
        async with self._running:
            result = await self._run()
        self.last_result = result
        if result.status in ("ok", "partial"):
            self.last_success_at = result.finished_at
            self.consecutive_failures = 0
        elif result.status == "failed":
            self.consecutive_failures += 1
        return result

    @staticmethod
    async def _db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    # ------------------------------------------------------------------ run

    async def _run(self) -> SyncResult:
        started = _iso_now()
        db = self.repo.db
        try:
            acquired = await self._db(db.acquire_lock, LOCK_NAME, self.owner, LOCK_TTL_SECONDS)
        except (WewlnError, sqlite3.Error) as exc:
            return SyncResult(
                "failed",
                started,
                _iso_now(),
                error_code="storage_error",
                error_message=f"the local database is not usable ({type(exc).__name__})",
            )
        if not acquired:
            return SyncResult(
                "skipped",
                started,
                _iso_now(),
                error_code="sync_already_running",
                error_message="another Notion sync is running (another wewln process)",
            )
        self._lock_renewed = time.monotonic()
        status, error_code, error_message, retry_after = "failed", None, None, None
        run_id: int | None = None
        cycle: _Cycle | None = None
        try:
            run_id = await self._db(self.repo.start_sync_run)
            client = self._client_factory()
            cycle = _Cycle(client)
            try:
                await self._cycle(cycle)
            finally:
                cycle.stats["requests"] = client.requests_sent
                await client.close()
            status = "partial" if cycle.problems else "ok"
        except NotionError as exc:
            error_code, error_message, retry_after = exc.code, exc.message, exc.retry_after
        except WewlnError as exc:
            error_code, error_message = exc.code, exc.message
        except Exception as exc:  # a bug must not stop the server's sync loop
            log.exception("notion_sync_crashed")
            error_code, error_message = "internal_error", f"unexpected error ({type(exc).__name__})"
        finally:
            with contextlib.suppress(WewlnError, sqlite3.Error):
                await self._db(db.release_lock, LOCK_NAME, self.owner)
        stats = dict(cycle.stats) if cycle else {}
        problems = list(cycle.problems) if cycle else []
        result = SyncResult(
            status,
            started,
            _iso_now(),
            stats=stats,
            problems=problems,
            error_code=error_code,
            error_message=error_message,
            retry_after=retry_after,
            run_id=run_id,
        )
        if run_id is not None:
            summary = error_message or ("; ".join(problems[:5]) if problems else None)
            with contextlib.suppress(WewlnError, sqlite3.Error):
                await self._db(
                    self.repo.finish_sync_run,
                    run_id,
                    status=status,
                    stats=stats,
                    error_code=error_code,
                    error_message=summary,
                )
        level = logging.WARNING if status in ("failed", "partial") else logging.INFO
        log.log(
            level,
            "notion_sync_finished",
            extra={"status": status, "error_code": error_code, "stats": stats},
        )
        for problem in problems:
            log.warning("notion_sync_problem", extra={"problem": problem})
        return result

    async def _renew_lock(self) -> None:
        if time.monotonic() - self._lock_renewed < LOCK_RENEW_SECONDS:
            return
        if not await self._db(self.repo.db.acquire_lock, LOCK_NAME, self.owner, LOCK_TTL_SECONDS):
            raise WewlnError("another process took over the sync lock", code="sync_lock_lost")
        self._lock_renewed = time.monotonic()

    async def _cycle(self, c: _Cycle) -> None:
        workspace = await resolve_workspace(c.client, self.settings.notion_databases)
        blocking = workspace.blocking_problems()
        if blocking:
            raise NotionSetupError(
                "the Notion databases do not have the expected structure: "
                + "; ".join(blocking)
                + ". Run: wewln notion check --repair"
            )
        c.workspace = workspace
        await self._read_campaigns(c)
        await self._read_contacts(c)
        await self._db(self.repo.plan)
        await self._push_campaign_statuses(c)
        await self._push_contacts(c)
        for campaign in await self._db(self.repo.notion_campaigns):
            if campaign["missing_in_notion"]:
                continue
            await self._renew_lock()
            if await self._db(self.repo.campaign_needs_job_sync, campaign):
                await self._sync_jobs(c, campaign)
            await self._sync_results(c, campaign)
            await self._push_summary(c, campaign)

    async def _write(self, c: _Cycle, what: str, call: Awaitable[dict[str, Any]]) -> bool:
        try:
            await call
        except NotionError as exc:
            if _is_fatal(exc):
                raise
            c.problem(f"{what}: {exc.message}")
            return False
        return True

    def _waiting(self, since: str | None) -> bool:
        if not since:
            return False
        return self._clock() - datetime.fromisoformat(since).timestamp() < self._grace

    # ------------------------------------------------------------------ campaigns and contacts

    async def _read_campaigns(self, c: _Cycle) -> None:
        result = await c.client.query_all(c.ws["campaigns"].data_source_id)
        records = []
        for page in result.pages:
            c.campaign_pages[page["id"]] = page
            records.append(
                NotionCampaignRecord(
                    page_id=page["id"],
                    name=props.read_title(page),
                    status_label=props.read_select(page, CAMPAIGN_STATUS),
                    script=props.read_text(page, CAMPAIGN_SCRIPT),
                    language=props.read_select(page, CAMPAIGN_LANGUAGE),
                    max_attempts=props.read_number(page, CAMPAIGN_MAX_ATTEMPTS),
                )
            )
        for name, n in (
            await self._db(self.repo.apply_notion_campaigns, records, complete=result.complete)
        ).items():
            c.count(name, n)
        if not result.complete:
            c.problem(
                "campaigns: Notion returned an incomplete list; deleted campaigns are not "
                "detected in this cycle"
            )

    async def _read_contacts(self, c: _Cycle) -> None:
        result = await c.client.query_all(c.ws["contacts"].data_source_id)
        records = []
        for page in result.pages:
            c.contact_pages[page["id"]] = page
            ids, more = props.read_relation(page, CONTACT_CAMPAIGNS)
            membership: tuple[str, ...] | None = tuple(ids)
            if more:
                membership = await self._full_relation(c, page, CONTACT_CAMPAIGNS)
            records.append(
                NotionContactRecord(
                    page_id=page["id"],
                    name=props.read_title(page),
                    phone=props.read_phone(page, CONTACT_PHONE),
                    dnc=props.read_checkbox(page, CONTACT_DNC),
                    dnc_reason=props.read_text(page, CONTACT_DNC_REASON),
                    campaign_page_ids=membership,
                )
            )
        for name, n in (
            await self._db(self.repo.apply_notion_contacts, records, complete=result.complete)
        ).items():
            c.count(name, n)
        if not result.complete:
            c.problem(
                "contacts: Notion returned an incomplete list; deleted contacts are not "
                "detected in this cycle"
            )

    async def _full_relation(
        self, c: _Cycle, page: dict[str, Any], name: str
    ) -> tuple[str, ...] | None:
        """A page object lists at most 25 related pages; the rest is read page by page."""
        prop_id = props.property_id(page, name)
        if prop_id is None:
            return None
        try:
            items = await c.client.property_items(page["id"], prop_id)
        except NotionError as exc:
            if _is_fatal(exc):
                raise
            c.problem(
                f"contact {compact_id(page['id'])}: the list of its campaigns could not be read "
                f"({exc.code}); its membership is left unchanged"
            )
            return None
        c.count("relations_paginated")
        refs = [item.get("relation") for item in items]
        return tuple(str(r["id"]) for r in refs if isinstance(r, dict) and r.get("id"))

    async def _push_campaign_statuses(self, c: _Cycle) -> None:
        """A stop made in wewln, or a reverted restart of a stopped campaign, goes to Notion."""
        for campaign in await self._db(self.repo.campaigns_to_push):
            if campaign["missing_in_notion"] or campaign["notion_page_id"] not in c.campaign_pages:
                continue
            label = campaign_status_label(campaign["status"])
            body = {"properties": {CAMPAIGN_STATUS: props.select(label)}}
            what = f"campaign {campaign['id']}"
            if await self._write(c, what, c.client.update_page(campaign["notion_page_id"], body)):
                await self._db(self.repo.mark_campaign_status_pushed, campaign["id"], label)
                c.count("campaign_statuses_pushed")

    async def _push_contacts(self, c: _Cycle) -> None:
        # Numbers blocked in wewln get the «Не звонить» mark in Notion too.
        for row in await self._db(self.repo.notion_contacts_with_local_dnc):
            page = c.contact_pages.get(row["notion_page_id"])
            if page is None:
                continue
            values = {CONTACT_DNC: props.checkbox(True)}
            if not props.read_text(page, CONTACT_DNC_REASON).strip():
                values[CONTACT_DNC_REASON] = props.rich_text(row["reason"] or DNC_REASON_FROM_WEWLN)
            what = f"contact {compact_id(page['id'])}"
            if await self._write(c, what, c.client.update_page(page["id"], {"properties": values})):
                c.count("contacts_dnc_pushed")
        # Phone number problems are shown on the contact page («Ошибка»).
        for page_id, page in c.contact_pages.items():
            error = normalize_phone(props.read_phone(page, CONTACT_PHONE), self.repo.default_region)
            desired = PHONE_ERROR_TEXT_RU.get(error.error or "", "")
            if props.read_text(page, CONTACT_ERROR) == desired:
                continue
            body = {"properties": {CONTACT_ERROR: props.rich_text(desired)}}
            what = f"contact {compact_id(page_id)}"
            if await self._write(c, what, c.client.update_page(page_id, body)):
                c.count("contact_errors_pushed")

    async def _push_summary(self, c: _Cycle, campaign: Any) -> None:
        page = c.campaign_pages.get(campaign["notion_page_id"])
        if page is None:
            return
        counts = await self._db(self.repo.job_counts, campaign["id"])
        summary = campaign_summary(counts, simulated=self._simulated)
        problem = campaign["problem"] or ""
        values: dict[str, Any] = {}
        if props.read_text(page, CAMPAIGN_SUMMARY) != summary:
            values[CAMPAIGN_SUMMARY] = props.rich_text(summary)
        if props.read_text(page, CAMPAIGN_ERROR) != problem:
            values[CAMPAIGN_ERROR] = props.rich_text(problem)
        if not values:
            return
        what = f"campaign {campaign['id']}"
        if await self._write(c, what, c.client.update_page(page["id"], {"properties": values})):
            c.count("campaign_summaries_pushed")

    # ------------------------------------------------------------------ jobs

    def _job_values(self, job: Any) -> dict[str, Any]:
        return {
            JOB_STATUS: JOB_STATUS_LABELS[JobStatus(job["status"])],
            JOB_REASON: reason_label(job["reason"]),
            JOB_ATTEMPTS: float(job["attempts"]),
            JOB_UPDATED: job["updated_at"],
        }

    @staticmethod
    def _job_properties(values: dict[str, Any]) -> dict[str, Any]:
        return {
            JOB_STATUS: props.select(values[JOB_STATUS]),
            JOB_REASON: props.rich_text(values[JOB_REASON]),
            JOB_ATTEMPTS: props.number(int(values[JOB_ATTEMPTS])),
            JOB_UPDATED: props.date(values[JOB_UPDATED]),
        }

    async def _sync_jobs(self, c: _Cycle, campaign: Any) -> None:
        jobs = await self._db(self.repo.jobs_for_sync, campaign["id"])
        if not jobs:
            return
        scan = await c.client.query_all(
            c.ws["jobs"].data_source_id,
            filter={"property": JOB_CAMPAIGN, "relation": {"contains": campaign["notion_page_id"]}},
        )
        by_id = {page["id"]: page for page in scan.pages}
        by_key: dict[str, list[dict[str, Any]]] = {}
        for page in _by_creation(scan.pages):
            key = props.read_text(page, JOB_KEY).strip()
            if key:
                by_key.setdefault(key, []).append(page)
        extra = sum(len(pages) - 1 for pages in by_key.values())
        if extra:
            c.problem(
                f"campaign {campaign['id']}: {extra} job page(s) repeat an existing «Ключ» "
                "(copies?); wewln uses the oldest page of each key and ignores the others"
            )
        claimed = {job["notion_page_id"] for job in jobs if job["notion_page_id"]}
        for job in jobs:
            await self._renew_lock()
            await self._sync_job(c, job, by_id, by_key, claimed, complete=scan.complete)

    async def _sync_job(
        self,
        c: _Cycle,
        job: Any,
        by_id: dict[str, dict[str, Any]],
        by_key: dict[str, list[dict[str, Any]]],
        claimed: set[str],
        *,
        complete: bool,
    ) -> None:
        if job["notion_deleted"]:
            return
        if job["notion_page_id"]:
            page = by_id.get(job["notion_page_id"])
            if page is None:
                # Not among the campaign's pages: deleted, archived or moved. Ask Notion directly.
                page = await self._fetch_page(c, job)
                if page is _UNKNOWN:
                    return
                if page is None or page.get("in_trash") or page.get("is_archived"):
                    cancelled = await self._db(
                        self.repo.cancel_job_from_notion,
                        job["id"],
                        JobReason.PAGE_DELETED_IN_NOTION,
                        deleted=True,
                    )
                    c.count("jobs_cancelled_page_deleted" if cancelled else "job_pages_deleted")
                    return
        else:
            candidates = [
                p for p in by_key.get(job["idempotency_key"], []) if p["id"] not in claimed
            ]
            if candidates:
                page = candidates[0]
                claimed.add(page["id"])
                job = await self._adopt(c, job, page)
            elif self._waiting(job["notion_uncertain_since"]):
                c.count("jobs_waiting_for_confirmation")
                return
            elif job["status"] == JobStatus.CANCELLED:
                await self._db(
                    self.repo.mark_job_without_page,
                    job["id"],
                    expected_updated_at=job["updated_at"],
                )
                return
            elif not complete:
                c.problem(
                    f"job {job['id']}: not created because the list of job pages is incomplete"
                )
                return
            else:
                await self._create_job(c, job)
                return
        await self._update_job(c, job, page)

    async def _fetch_page(self, c: _Cycle, job: Any) -> dict[str, Any] | None:
        try:
            return await c.client.retrieve_page(job["notion_page_id"])
        except NotionAccessError as exc:
            if exc.status == 404:
                return None
            raise
        except NotionError as exc:
            if _is_fatal(exc):
                raise
            c.problem(f"job {job['id']}: its page could not be read ({exc.code})")
            return _UNKNOWN

    async def _adopt(self, c: _Cycle, job: Any, page: dict[str, Any]) -> Any:
        """A page with this job's key exists although the job did not know it: the local
        database was restored or replaced, or Notion did not confirm the create."""
        await self._db(self.repo.record_job_page, job["id"], page["id"], None)
        c.count("jobs_adopted")
        page_status = JOB_STATUS_BY_LABEL.get(props.read_select(page, JOB_STATUS) or "")
        if JobStatus(job["status"]) in OPEN_JOB_STATUSES and (
            page_status in FINAL_JOB_STATUSES or page_status == JobStatus.IN_PROGRESS
        ):
            # The page knows more than the local database: never call again what was done,
            # cancelled or possibly in progress.
            if page_status == JobStatus.IN_PROGRESS:
                status, reason = JobStatus.FAILED, JobReason.STATE_LOST
            else:
                status = page_status
                reason = JOB_REASON_BY_LABEL.get(props.read_text(page, JOB_REASON).strip())
                if status == JobStatus.CANCELLED and reason is None:
                    reason = JobReason.CANCELLED_IN_NOTION
            attempts = int(props.read_number(page, JOB_ATTEMPTS) or 0)
            await self._db(self.repo.adopt_job_state, job["id"], status, reason, attempts)
            c.count("jobs_state_adopted")
        return await self._db(self.repo.job_for_sync, job["id"])

    async def _update_job(self, c: _Cycle, job: Any, page: dict[str, Any]) -> None:
        pushed = json.loads(job["notion_pushed"]) if job["notion_pushed"] else {}
        page_label = props.read_select(page, JOB_STATUS)
        cancelled = JOB_STATUS_LABELS[JobStatus.CANCELLED]
        if (
            page_label == cancelled
            and JobStatus(job["status"]) not in FINAL_JOB_STATUSES
            and pushed.get(JOB_STATUS) != cancelled
        ):
            # «Отменено» set by a person in Notion is the one status accepted from there.
            if await self._db(
                self.repo.cancel_job_from_notion,
                job["id"],
                JobReason.CANCELLED_IN_NOTION,
                deleted=False,
            ):
                c.count("jobs_cancelled_in_notion")
            job = await self._db(self.repo.job_for_sync, job["id"])
        desired = self._job_values(job)
        current = {
            JOB_STATUS: page_label,
            JOB_REASON: props.read_text(page, JOB_REASON),
            JOB_ATTEMPTS: props.read_number(page, JOB_ATTEMPTS),
        }
        if all(current[name] == desired[name] for name in current):
            if job["notion_dirty"]:
                await self._db(
                    self.repo.record_job_page,
                    job["id"],
                    page["id"],
                    desired,
                    expected_updated_at=job["updated_at"],
                )
            return
        body = {"properties": self._job_properties(desired)}
        if not await self._write(c, f"job {job['id']}", c.client.update_page(page["id"], body)):
            return
        await self._db(
            self.repo.record_job_page,
            job["id"],
            page["id"],
            desired,
            expected_updated_at=job["updated_at"],
        )
        c.count("jobs_updated")

    async def _create_job(self, c: _Cycle, job: Any) -> None:
        if not job["campaign_page_id"] or not job["contact_page_id"]:
            c.problem(f"job {job['id']}: its campaign or contact has no Notion page")
            return
        jobs_db = c.ws["jobs"]
        desired = self._job_values(job)
        title = f"{job['contact_name'] or 'Контакт'} — {job['campaign_name']}"
        properties = {
            jobs_db.title_property: props.title(title[:200]),
            JOB_CAMPAIGN: props.relation([job["campaign_page_id"]]),
            JOB_CONTACT: props.relation([job["contact_page_id"]]),
            JOB_KEY: props.rich_text(job["idempotency_key"]),
            JOB_SIMULATED: props.checkbox(self._simulated),
            **self._job_properties(desired),
        }
        body = {"parent": {"data_source_id": jobs_db.data_source_id}, "properties": properties}
        page_id = await self._create(c, f"job {job['id']}", body)
        if page_id is None:
            await self._db(self.repo.mark_job_uncertain, job["id"])
            return
        if page_id:
            await self._db(
                self.repo.record_job_page,
                job["id"],
                page_id,
                desired,
                expected_updated_at=job["updated_at"],
            )
            c.count("jobs_created")

    async def _create(self, c: _Cycle, what: str, body: dict[str, Any]) -> str | None:
        """Create a page. Returns its ID, "" after an error, None when the result is unknown."""
        try:
            page = await c.client.create_page(body)
        except NotionUncertainWriteError as exc:
            if exc.committed_resource_id:
                return exc.committed_resource_id
            c.count("creates_unconfirmed")
            c.problem(
                f"{what}: Notion did not confirm the new page; the next cycles look for it "
                "(by «Ключ») before creating it again"
            )
            return None
        except NotionError as exc:
            if _is_fatal(exc):
                raise
            c.problem(f"{what}: {exc.message}")
            return ""
        return str(page["id"])

    # ------------------------------------------------------------------ results

    async def _sync_results(self, c: _Cycle, campaign: Any) -> None:
        results = [
            r for r in await self._db(self.repo.results_to_push, campaign["id"]) if r["job_page_id"]
        ]
        if not results:
            return
        scan = await c.client.query_all(
            c.ws["results"].data_source_id,
            filter={
                "property": RESULT_CAMPAIGN,
                "relation": {"contains": campaign["notion_page_id"]},
            },
        )
        by_key: dict[str, dict[str, Any]] = {}
        for page in _by_creation(scan.pages):
            by_key.setdefault(props.read_text(page, RESULT_KEY).strip(), page)
        for result in results:
            await self._renew_lock()
            existing = by_key.get(result["idempotency_key"])
            if existing is not None:
                await self._db(self.repo.record_result_page, result["id"], existing["id"])
                c.count("results_adopted")
            elif self._waiting(result["notion_uncertain_since"]):
                c.count("results_waiting_for_confirmation")
            elif not scan.complete:
                c.problem(f"result {result['id']}: not created because the list is incomplete")
            else:
                await self._create_result(c, result)

    async def _create_result(self, c: _Cycle, result: Any) -> None:
        results_db = c.ws["results"]
        outcome = OUTCOME_LABELS[Outcome(result["outcome"])]
        title = f"{outcome} — {result['contact_name'] or 'Контакт'}"
        properties = {
            results_db.title_property: props.title(title[:200]),
            RESULT_JOB: props.relation([result["job_page_id"]]),
            RESULT_CAMPAIGN: props.relation([result["campaign_page_id"]]),
            RESULT_CONTACT: props.relation([result["contact_page_id"]]),
            RESULT_OUTCOME: props.select(outcome),
            RESULT_STARTED: props.date(result["started_at"]),
            RESULT_DURATION: props.number(result["duration_s"]),
            RESULT_SUMMARY: props.rich_text(result["summary"]),
            RESULT_KEY: props.rich_text(result["idempotency_key"]),
            RESULT_SIMULATED: props.checkbox(bool(result["simulated"])),
        }
        body = {"parent": {"data_source_id": results_db.data_source_id}, "properties": properties}
        page_id = await self._create(c, f"result {result['id']}", body)
        if page_id is None:
            await self._db(self.repo.mark_result_uncertain, result["id"])
        elif page_id:
            await self._db(self.repo.record_result_page, result["id"], page_id)
            c.count("results_created")


class SyncService:
    """Runs the sync periodically inside `wewln serve`: interval, backoff, wake-up on demand."""

    def __init__(self, sync: NotionSync, *, interval: float, max_backoff: float = 900.0) -> None:
        self.sync = sync
        self.interval = interval
        self.max_backoff = max(max_backoff, interval)
        self.next_run_at: str | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="wewln-notion-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        """Run the next cycle now (for example after a stop, so that Notion learns it soon)."""
        if self._wake is not None:
            self._wake.set()

    def delay_after(self, result: SyncResult) -> float:
        if result.status != "failed":
            return float(self.interval)
        failures = max(self.sync.consecutive_failures, 1)
        delay = min(self.max_backoff, self.interval * 2 ** min(failures - 1, 10))
        if result.retry_after:
            delay = max(delay, min(result.retry_after, self.max_backoff))
        return float(delay)

    async def _loop(self) -> None:
        while True:
            try:
                result = await self.sync.run_once()
            except Exception:  # never let the loop die; the next cycle tries again
                log.exception("notion_sync_loop_error")
                now = _iso_now()
                result = SyncResult("failed", now, now, error_code="internal_error")
            delay = self.delay_after(result)
            self.next_run_at = datetime.fromtimestamp(time.time() + delay, UTC).isoformat(
                timespec="seconds"
            )
            if self._wake is None:
                self._wake = asyncio.Event()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    def status(self) -> dict[str, Any]:
        sync = self.sync
        last = sync.last_result
        if last is None or (last.status == "skipped" and sync.last_success_at is None):
            state = "starting"
        elif last.status == "ok" or (last.status == "skipped" and sync.consecutive_failures == 0):
            state = "ok"
        else:
            state = "degraded"
        info: dict[str, Any] = {
            "status": state,
            "running": sync.running,
            "last_success_at": sync.last_success_at,
            "consecutive_failures": sync.consecutive_failures,
            "next_run_at": self.next_run_at,
        }
        if last is not None:
            info["last_run"] = {
                "status": last.status,
                "finished_at": last.finished_at,
                "error_code": last.error_code,
                "problems": last.stats.get("problems", 0),
            }
        return info
