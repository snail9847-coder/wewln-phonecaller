"""Campaigns, contacts, the do-not-call list, call jobs and results in SQLite.

Everything here works without Notion; the Notion sync uses the same functions, so one set of
rules holds everywhere:
- a stopped or completed campaign is final and never runs again;
- do-not-call always wins, whatever its source (Notion checkbox, manual list, opt-out);
- there is at most one job per campaign and contact (unique key), so re-planning is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import SUPPORTED_LANGUAGES
from ..domain import (
    CAMPAIGN_STATUS_BY_LABEL,
    CAMPAIGN_STATUS_LABELS,
    FINAL_CAMPAIGN_STATUSES,
    OPEN_JOB_STATUSES,
    PHONE_ERROR_REASONS,
    REACTIVATABLE_REASONS,
    CampaignStatus,
    JobReason,
    JobStatus,
    Outcome,
    SuppressionSource,
)
from ..errors import WewlnError
from ..notion.ids import compact_id
from ..phones import normalize_phone
from .db import Database

MAX_NAME_LENGTH = 200
MAX_ATTEMPTS_LIMIT = 5
SIMULATED_SUMMARY = "Симуляция: реальный звонок не выполнялся"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NotFoundError(WewlnError):
    code = "not_found"
    http_status = 404


class ConflictError(WewlnError):
    code = "conflict"
    http_status = 409


class InvalidInputError(WewlnError):
    code = "invalid_input"
    http_status = 400


@dataclass(frozen=True)
class ContactInput:
    line: int
    name: str
    phone: str
    dnc: bool = False


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    added_to_campaign: int = 0
    marked_dnc: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class NotionCampaignRecord:
    page_id: str
    name: str
    status_label: str | None
    script: str = ""
    language: str | None = None
    max_attempts: float | None = None


@dataclass(frozen=True)
class NotionContactRecord:
    page_id: str
    name: str
    phone: str | None
    dnc: bool = False
    dnc_reason: str = ""
    # None means the relation could not be read completely: membership is left unchanged.
    campaign_page_ids: tuple[str, ...] | None = ()


@dataclass
class PlanReport:
    created: int = 0
    changed: int = 0

    def add(self, other: PlanReport) -> None:
        self.created += other.created
        self.changed += other.changed


@dataclass
class SimulationReport:
    completed: int = 0
    skipped: int = 0
    job_ids: list[int] = field(default_factory=list)


def job_key(campaign: sqlite3.Row, contact: sqlite3.Row) -> str:
    """Deterministic idempotency key; Notion-based keys survive the loss of the local DB."""
    if campaign["notion_page_id"] and contact["notion_page_id"]:
        return (
            f"wewln:job:{compact_id(campaign['notion_page_id'])}:"
            f"{compact_id(contact['notion_page_id'])}"
        )
    return f"wewln:job:local:{campaign['id']}:{contact['id']}"


def result_key(job_idempotency_key: str, attempt: int) -> str:
    return job_idempotency_key.replace("wewln:job:", "wewln:result:", 1) + f":{attempt}"


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" * len(values))


class Repository:
    def __init__(
        self,
        db: Database,
        *,
        default_language: str = "ru-RU",
        default_region: str | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.db = db
        self.default_language = default_language
        self.default_region = default_region
        self._now = clock

    # ------------------------------------------------------------------ campaigns

    def create_local_campaign(self, name: str, *, script: str = "", language: str | None = None):
        name = name.strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise InvalidInputError(f"campaign name must be 1-{MAX_NAME_LENGTH} characters")
        language = language or self.default_language
        if language not in SUPPORTED_LANGUAGES:
            raise InvalidInputError("unsupported language: " + language)
        now = self._now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO campaigns (source, name, status, script, language, created_at, "
                "updated_at) VALUES ('local', ?, 'draft', ?, ?, ?, ?)",
                (name, script, language, now, now),
            )
            campaign_id = int(cursor.lastrowid or 0)
            self.db.audit("campaign.create", subject=f"campaign:{campaign_id}", conn=conn)
        return campaign_id

    def get_campaign(self, campaign_id: int) -> sqlite3.Row:
        with self.db.connect(must_exist=True) as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"campaign {campaign_id} not found")
        return row

    def list_campaigns(self) -> list[dict[str, Any]]:
        with self.db.connect(must_exist=True) as conn:
            campaigns = [dict(r) for r in conn.execute("SELECT * FROM campaigns ORDER BY id")]
            counts: dict[int, dict[str, int]] = {}
            for row in conn.execute(
                "SELECT campaign_id, status, COUNT(*) AS n FROM call_jobs "
                "GROUP BY campaign_id, status"
            ):
                counts.setdefault(row["campaign_id"], {})[row["status"]] = row["n"]
            members = dict(
                conn.execute(
                    "SELECT campaign_id, COUNT(*) FROM campaign_contacts GROUP BY campaign_id"
                ).fetchall()
            )
        for campaign in campaigns:
            campaign["jobs"] = counts.get(campaign["id"], {})
            campaign["members"] = members.get(campaign["id"], 0)
        return campaigns

    def set_local_campaign_status(self, campaign_id: int, status: CampaignStatus) -> PlanReport:
        if status not in (CampaignStatus.RUNNING, CampaignStatus.PAUSED):
            raise InvalidInputError("only running or paused can be set here; use stop to stop")
        with self.db.transaction() as conn:
            campaign = self._campaign(conn, campaign_id)
            if campaign["source"] != "local":
                raise ConflictError(
                    "this campaign is controlled from Notion: change its «Статус» there "
                    "(stop works everywhere: wewln campaign stop)"
                )
            if campaign["status"] in FINAL_CAMPAIGN_STATUSES:
                raise ConflictError("a stopped campaign cannot be restarted; create a new one")
            conn.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, self._now(), campaign_id),
            )
            self.db.audit(
                "campaign.status",
                subject=f"campaign:{campaign_id}",
                details={"status": status.value},
                conn=conn,
            )
            return self._plan(conn, campaign_ids=[campaign_id])

    def stop_campaign(
        self, campaign_id: int, *, reason: str = "", source: str = "local"
    ) -> PlanReport:
        """Stop is final, works offline and wins over Notion (the next sync pushes it there)."""
        now = self._now()
        with self.db.transaction() as conn:
            campaign = self._campaign(conn, campaign_id)
            if campaign["status"] not in FINAL_CAMPAIGN_STATUSES:
                conn.execute(
                    "UPDATE campaigns SET status = 'stopped', stopped_at = ?, stop_source = ?, "
                    "stop_reason = ?, notion_push_status = ?, updated_at = ? WHERE id = ?",
                    (
                        now,
                        source,
                        reason.strip()[:500],
                        1 if campaign["source"] == "notion" else 0,
                        now,
                        campaign_id,
                    ),
                )
                self.db.audit(
                    "campaign.stop",
                    subject=f"campaign:{campaign_id}",
                    details={"source": source, "reason": reason[:500]},
                    conn=conn,
                )
            return self._plan(conn, campaign_ids=[campaign_id])

    @staticmethod
    def _campaign(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"campaign {campaign_id} not found")
        return row

    def apply_notion_campaigns(
        self, records: Iterable[NotionCampaignRecord], *, complete: bool
    ) -> dict[str, int]:
        """Upsert campaigns read from Notion. `complete`: the scan saw every page, so campaigns
        that are absent were deleted or moved and are paused (never started again by absence).
        """
        now = self._now()
        stats = {"campaigns_seen": 0, "campaigns_new": 0, "campaigns_missing": 0}
        seen: set[str] = set()
        with self.db.transaction() as conn:
            for rec in records:
                stats["campaigns_seen"] += 1
                seen.add(rec.page_id)
                if self._apply_notion_campaign(conn, rec, now):
                    stats["campaigns_new"] += 1
            if complete:
                rows = conn.execute(
                    "SELECT id, notion_page_id, status FROM campaigns "
                    "WHERE source = 'notion' AND missing_in_notion = 0"
                ).fetchall()
                for row in rows:
                    if row["notion_page_id"] in seen:
                        continue
                    stats["campaigns_missing"] += 1
                    status = row["status"]
                    if status not in FINAL_CAMPAIGN_STATUSES:
                        status = CampaignStatus.PAUSED.value
                    conn.execute(
                        "UPDATE campaigns SET missing_in_notion = 1, status = ?, problem = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            status,
                            "Страница кампании не найдена в Notion (удалена или перенесена)",
                            now,
                            row["id"],
                        ),
                    )
        return stats

    def _apply_notion_campaign(
        self, conn: sqlite3.Connection, rec: NotionCampaignRecord, now: str
    ) -> bool:
        problems: list[str] = []
        label = (rec.status_label or "").strip()
        if not label:
            status = CampaignStatus.DRAFT
        elif label in CAMPAIGN_STATUS_BY_LABEL:
            status = CAMPAIGN_STATUS_BY_LABEL[label]
        else:
            status = CampaignStatus.DRAFT
            problems.append(f"Неизвестный статус «{label}»: кампания не запускается")
        language = (rec.language or "").strip() or self.default_language
        if language not in SUPPORTED_LANGUAGES:
            problems.append(
                f"Язык «{language}» не поддерживается (доступно: {', '.join(SUPPORTED_LANGUAGES)})"
            )
        attempts = 1
        if rec.max_attempts is not None:
            value = rec.max_attempts
            if value != int(value) or not 1 <= value <= MAX_ATTEMPTS_LIMIT:
                problems.append(f"«Макс. попыток» — целое число от 1 до {MAX_ATTEMPTS_LIMIT}")
                attempts = min(max(int(value), 1), MAX_ATTEMPTS_LIMIT)
            else:
                attempts = int(value)
        name = (rec.name or "").strip()[:MAX_NAME_LENGTH] or "Без названия"
        existing = conn.execute(
            "SELECT * FROM campaigns WHERE notion_page_id = ?", (rec.page_id,)
        ).fetchone()
        stop_values: tuple[Any, Any, Any] = (None, None, None)
        if status in FINAL_CAMPAIGN_STATUSES:
            stop_values = (now, "notion", f"Статус в Notion: {label}")
        if existing is None:
            conn.execute(
                "INSERT INTO campaigns (source, notion_page_id, name, status, notion_status, "
                "script, language, max_attempts, problem, stopped_at, stop_source, stop_reason, "
                "created_at, updated_at) VALUES ('notion', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.page_id,
                    name,
                    status.value,
                    label or None,
                    rec.script,
                    language,
                    attempts,
                    "; ".join(problems),
                    *stop_values,
                    now,
                    now,
                ),
            )
            return True
        push_status = existing["notion_push_status"]
        if existing["stopped_at"]:
            # Final: Notion may switch between "stopped" and "completed", nothing else.
            if status in FINAL_CAMPAIGN_STATUSES:
                new_status = status.value
            else:
                new_status = existing["status"]
                push_status = 1
                problems.append(
                    "Кампания остановлена — повторный запуск невозможен. Создайте новую кампанию."
                )
            if existing["stop_source"] == "local":
                reason = existing["stop_reason"] or "без причины"
                problems.insert(0, f"Остановлена в wewln: {reason}")
            conn.execute(
                "UPDATE campaigns SET name = ?, status = ?, notion_status = ?, script = ?, "
                "language = ?, max_attempts = ?, problem = ?, notion_push_status = ?, "
                "missing_in_notion = 0, updated_at = ? WHERE id = ?",
                (
                    name,
                    new_status,
                    label or None,
                    rec.script,
                    language,
                    attempts,
                    "; ".join(problems),
                    push_status,
                    now,
                    existing["id"],
                ),
            )
            return False
        conn.execute(
            "UPDATE campaigns SET name = ?, status = ?, notion_status = ?, script = ?, "
            "language = ?, max_attempts = ?, problem = ?, missing_in_notion = 0, "
            "stopped_at = ?, stop_source = ?, stop_reason = ?, updated_at = ? WHERE id = ?",
            (
                name,
                status.value,
                label or None,
                rec.script,
                language,
                attempts,
                "; ".join(problems),
                *stop_values,
                now,
                existing["id"],
            ),
        )
        return False

    def campaigns_to_push(self) -> list[sqlite3.Row]:
        with self.db.connect(must_exist=True) as conn:
            return conn.execute(
                "SELECT * FROM campaigns WHERE source = 'notion' AND notion_push_status = 1"
            ).fetchall()

    def mark_campaign_status_pushed(self, campaign_id: int, label: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE campaigns SET notion_push_status = 0, notion_status = ? WHERE id = ?",
                (label, campaign_id),
            )

    def notion_campaigns(self) -> list[sqlite3.Row]:
        with self.db.connect(must_exist=True) as conn:
            return conn.execute(
                "SELECT * FROM campaigns WHERE source = 'notion' ORDER BY id"
            ).fetchall()

    def job_counts(self, campaign_id: int) -> dict[str, int]:
        with self.db.connect(must_exist=True) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM call_jobs WHERE campaign_id = ? GROUP BY status",
                (campaign_id,),
            ).fetchall()
        return {status: n for status, n in rows}

    # ------------------------------------------------------------------ contacts

    def import_contacts(
        self, rows: Iterable[ContactInput], *, campaign_id: int | None = None
    ) -> ImportReport:
        """Upsert local contacts by phone number (E.164). Invalid rows are reported, not stored."""
        report = ImportReport()
        now = self._now()
        with self.db.transaction() as conn:
            if campaign_id is not None:
                campaign = self._campaign(conn, campaign_id)
                if campaign["source"] != "local":
                    raise ConflictError("contacts of a Notion campaign are managed in Notion")
                if campaign["status"] in FINAL_CAMPAIGN_STATUSES:
                    raise ConflictError("the campaign is stopped")
            for row in rows:
                phone = normalize_phone(row.phone, self.default_region)
                if phone.e164 is None:
                    report.rejected.append((row.line, phone.error or "invalid"))
                    continue
                name = row.name.strip()[:MAX_NAME_LENGTH]
                existing = conn.execute(
                    "SELECT id, name FROM contacts WHERE source = 'local' AND phone_e164 = ?",
                    (phone.e164,),
                ).fetchone()
                if existing is None:
                    cursor = conn.execute(
                        "INSERT INTO contacts (source, name, phone_raw, phone_e164, created_at, "
                        "updated_at) VALUES ('local', ?, ?, ?, ?, ?)",
                        (name, row.phone.strip()[:200], phone.e164, now, now),
                    )
                    contact_id = int(cursor.lastrowid or 0)
                    report.created += 1
                else:
                    contact_id = existing["id"]
                    if name and name != existing["name"]:
                        conn.execute(
                            "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
                            (name, now, contact_id),
                        )
                        report.updated += 1
                if row.dnc:
                    added = conn.execute(
                        "INSERT OR IGNORE INTO suppression (phone_e164, source, source_ref, "
                        "reason, created_at) VALUES (?, 'manual', '', ?, ?)",
                        (phone.e164, "Импорт CSV", now),
                    ).rowcount
                    report.marked_dnc += added
                if campaign_id is not None:
                    report.added_to_campaign += conn.execute(
                        "INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, "
                        "added_at) VALUES (?, ?, ?)",
                        (campaign_id, contact_id, now),
                    ).rowcount
            self.db.audit(
                "contacts.import",
                details={
                    "created": report.created,
                    "updated": report.updated,
                    "rejected": len(report.rejected),
                    "campaign_id": campaign_id,
                },
                conn=conn,
            )
            self._plan(conn, campaign_ids=None)
        return report

    def apply_notion_contacts(
        self, records: Iterable[NotionContactRecord], *, complete: bool
    ) -> dict[str, int]:
        now = self._now()
        stats = {"contacts_seen": 0, "contacts_new": 0, "contacts_missing": 0}
        seen: set[str] = set()
        with self.db.transaction() as conn:
            campaign_ids = {
                row["notion_page_id"]: row["id"]
                for row in conn.execute(
                    "SELECT id, notion_page_id FROM campaigns WHERE source = 'notion'"
                )
            }
            for rec in records:
                stats["contacts_seen"] += 1
                seen.add(rec.page_id)
                if self._apply_notion_contact(conn, rec, campaign_ids, now):
                    stats["contacts_new"] += 1
            if complete:
                for row in conn.execute(
                    "SELECT id, notion_page_id FROM contacts "
                    "WHERE source = 'notion' AND missing_in_notion = 0"
                ).fetchall():
                    if row["notion_page_id"] not in seen:
                        stats["contacts_missing"] += 1
                        conn.execute(
                            "UPDATE contacts SET missing_in_notion = 1, updated_at = ? "
                            "WHERE id = ?",
                            (now, row["id"]),
                        )
        return stats

    def _apply_notion_contact(
        self,
        conn: sqlite3.Connection,
        rec: NotionContactRecord,
        campaign_ids: dict[str, int],
        now: str,
    ) -> bool:
        phone = normalize_phone(rec.phone, self.default_region)
        name = (rec.name or "").strip()[:MAX_NAME_LENGTH]
        existing = conn.execute(
            "SELECT id FROM contacts WHERE notion_page_id = ?", (rec.page_id,)
        ).fetchone()
        values = (name, (rec.phone or "").strip()[:200], phone.e164, phone.error, int(rec.dnc))
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO contacts (source, notion_page_id, name, phone_raw, phone_e164, "
                "phone_error, notion_dnc, created_at, updated_at) "
                "VALUES ('notion', ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.page_id, *values, now, now),
            )
            contact_id = int(cursor.lastrowid or 0)
        else:
            contact_id = existing["id"]
            conn.execute(
                "UPDATE contacts SET name = ?, phone_raw = ?, phone_e164 = ?, phone_error = ?, "
                "notion_dnc = ?, missing_in_notion = 0, updated_at = ? WHERE id = ?",
                (*values, now, contact_id),
            )
        conn.execute(
            "DELETE FROM suppression WHERE source = 'notion' AND source_ref = ?", (rec.page_id,)
        )
        if rec.dnc and phone.e164:
            conn.execute(
                "INSERT OR IGNORE INTO suppression (phone_e164, source, source_ref, reason, "
                "created_at) VALUES (?, 'notion', ?, ?, ?)",
                (phone.e164, rec.page_id, rec.dnc_reason[:500] or "Отмечено в Notion", now),
            )
        if rec.campaign_page_ids is not None:
            target = {campaign_ids[p] for p in rec.campaign_page_ids if p in campaign_ids}
            current = {
                row[0]
                for row in conn.execute(
                    "SELECT cc.campaign_id FROM campaign_contacts cc JOIN campaigns c "
                    "ON c.id = cc.campaign_id WHERE cc.contact_id = ? AND c.source = 'notion'",
                    (contact_id,),
                )
            }
            for campaign_id in current - target:
                conn.execute(
                    "DELETE FROM campaign_contacts WHERE campaign_id = ? AND contact_id = ?",
                    (campaign_id, contact_id),
                )
            for campaign_id in target - current:
                conn.execute(
                    "INSERT INTO campaign_contacts (campaign_id, contact_id, added_at) "
                    "VALUES (?, ?, ?)",
                    (campaign_id, contact_id, now),
                )
        return existing is None

    def list_contacts(self, *, campaign_id: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.*, EXISTS (SELECT 1 FROM suppression s WHERE s.phone_e164 = c.phone_e164) "
            "OR c.notion_dnc AS do_not_call, (SELECT COUNT(*) FROM campaign_contacts cc "
            "WHERE cc.contact_id = c.id) AS campaigns FROM contacts c"
        )
        params: tuple[Any, ...] = ()
        if campaign_id is not None:
            sql += " WHERE c.id IN (SELECT contact_id FROM campaign_contacts WHERE campaign_id = ?)"
            params = (campaign_id,)
        with self.db.connect(must_exist=True) as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY c.id", params)]

    def notion_contacts_with_local_dnc(self) -> list[sqlite3.Row]:
        """Notion contacts whose number is blocked locally but whose checkbox is not set."""
        with self.db.connect(must_exist=True) as conn:
            return conn.execute(
                "SELECT c.id, c.notion_page_id, MIN(s.reason) AS reason FROM contacts c "
                "JOIN suppression s ON s.phone_e164 = c.phone_e164 "
                "WHERE c.source = 'notion' AND c.notion_dnc = 0 AND c.missing_in_notion = 0 "
                "AND s.source IN ('manual', 'opt_out') GROUP BY c.id"
            ).fetchall()

    # ------------------------------------------------------------------ do-not-call

    def add_do_not_call(
        self, phone: str, *, reason: str = "", source: SuppressionSource = SuppressionSource.MANUAL
    ) -> bool:
        normalized = normalize_phone(phone, self.default_region)
        if normalized.e164 is None:
            raise InvalidInputError(f"phone number rejected ({normalized.error})")
        with self.db.transaction() as conn:
            added = conn.execute(
                "INSERT OR IGNORE INTO suppression (phone_e164, source, source_ref, reason, "
                "created_at) VALUES (?, ?, '', ?, ?)",
                (normalized.e164, source.value, reason.strip()[:500], self._now()),
            ).rowcount
            self.db.audit(
                "dnc.add", subject=normalized.e164, details={"source": source.value}, conn=conn
            )
            self._plan(conn, campaign_ids=None)
        return bool(added)

    def remove_do_not_call(self, phone: str, *, all_sources: bool = False) -> tuple[int, list[str]]:
        """Remove manual entries (or every entry). Returns (removed, sources still blocking)."""
        normalized = normalize_phone(phone, self.default_region)
        if normalized.e164 is None:
            raise InvalidInputError(f"phone number rejected ({normalized.error})")
        with self.db.transaction() as conn:
            if all_sources:
                removed = conn.execute(
                    "DELETE FROM suppression WHERE phone_e164 = ?", (normalized.e164,)
                ).rowcount
            else:
                removed = conn.execute(
                    "DELETE FROM suppression WHERE phone_e164 = ? AND source = 'manual'",
                    (normalized.e164,),
                ).rowcount
            remaining = sorted(
                {
                    row[0]
                    for row in conn.execute(
                        "SELECT source FROM suppression WHERE phone_e164 = ?", (normalized.e164,)
                    )
                }
            )
            if conn.execute(
                "SELECT 1 FROM contacts WHERE phone_e164 = ? AND notion_dnc = 1",
                (normalized.e164,),
            ).fetchone():
                remaining = sorted({*remaining, "notion"})
            self.db.audit(
                "dnc.remove",
                subject=normalized.e164,
                details={"removed": removed, "all_sources": all_sources},
                conn=conn,
            )
            self._plan(conn, campaign_ids=None)
        return removed, remaining

    def list_do_not_call(self) -> list[dict[str, Any]]:
        with self.db.connect(must_exist=True) as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT phone_e164, source, reason, created_at FROM suppression "
                    "ORDER BY phone_e164, source"
                )
            ]

    # ------------------------------------------------------------------ planning

    def plan(self, *, campaign_ids: Sequence[int] | None = None) -> PlanReport:
        with self.db.transaction() as conn:
            return self._plan(conn, campaign_ids=campaign_ids)

    def _plan(self, conn: sqlite3.Connection, *, campaign_ids: Sequence[int] | None) -> PlanReport:
        suppressed = {row[0] for row in conn.execute("SELECT DISTINCT phone_e164 FROM suppression")}
        if campaign_ids is None:
            campaigns = conn.execute("SELECT * FROM campaigns").fetchall()
        else:
            campaigns = conn.execute(
                # Only "?" placeholders are formatted in; the values are bound parameters.
                f"SELECT * FROM campaigns WHERE id IN ({_placeholders(campaign_ids)})",  # noqa: S608
                tuple(campaign_ids),
            ).fetchall()
        report = PlanReport()
        for campaign in campaigns:
            report.add(self._plan_campaign(conn, campaign, suppressed))
        return report

    @staticmethod
    def contact_gate(
        contact: sqlite3.Row, suppressed: set[str]
    ) -> tuple[JobStatus, JobReason | None]:
        """Desired job status for a contact that belongs to the campaign."""
        if contact["missing_in_notion"]:
            return JobStatus.CANCELLED, JobReason.CONTACT_DELETED
        if contact["notion_dnc"] or (contact["phone_e164"] and contact["phone_e164"] in suppressed):
            return JobStatus.BLOCKED, JobReason.DNC
        if not contact["phone_e164"]:
            return JobStatus.BLOCKED, PHONE_ERROR_REASONS.get(
                contact["phone_error"] or "invalid", JobReason.PHONE_INVALID
            )
        return JobStatus.QUEUED, None

    @staticmethod
    def campaign_runnable(campaign: sqlite3.Row) -> bool:
        return (
            campaign["status"] == CampaignStatus.RUNNING
            and not campaign["stopped_at"]
            and not campaign["missing_in_notion"]
            and campaign["language"] in SUPPORTED_LANGUAGES
        )

    def _plan_campaign(
        self, conn: sqlite3.Connection, campaign: sqlite3.Row, suppressed: set[str]
    ) -> PlanReport:
        report = PlanReport()
        now = self._now()
        cid = campaign["id"]
        jobs = conn.execute("SELECT * FROM call_jobs WHERE campaign_id = ?", (cid,)).fetchall()
        members = {
            row[0]
            for row in conn.execute(
                "SELECT contact_id FROM campaign_contacts WHERE campaign_id = ?", (cid,)
            )
        }
        contacts = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM contacts WHERE id IN (SELECT contact_id FROM campaign_contacts "
                "WHERE campaign_id = ?) OR id IN (SELECT contact_id FROM call_jobs "
                "WHERE campaign_id = ?)",
                (cid, cid),
            )
        }
        final = campaign["status"] in FINAL_CAMPAIGN_STATUSES
        runnable = self.campaign_runnable(campaign)

        def update(job: sqlite3.Row, status: JobStatus, reason: JobReason | None) -> None:
            conn.execute(
                "UPDATE call_jobs SET status = ?, reason = ?, notion_dirty = 1, updated_at = ? "
                "WHERE id = ?",
                (status.value, reason.value if reason else None, now, job["id"]),
            )
            report.changed += 1

        for job in jobs:
            status = JobStatus(job["status"])
            if status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.IN_PROGRESS):
                continue
            if final:
                if status in OPEN_JOB_STATUSES:
                    reason = (
                        JobReason.CAMPAIGN_COMPLETED
                        if campaign["status"] == CampaignStatus.COMPLETED
                        else JobReason.CAMPAIGN_STOPPED
                    )
                    update(job, JobStatus.CANCELLED, reason)
                continue
            if job["contact_id"] not in members:
                target: tuple[JobStatus, JobReason | None] = (
                    JobStatus.CANCELLED,
                    JobReason.CONTACT_REMOVED,
                )
            else:
                target = self.contact_gate(contacts[job["contact_id"]], suppressed)
            current = (status, JobReason(job["reason"]) if job["reason"] else None)
            if status in OPEN_JOB_STATUSES:
                if target != current:
                    update(job, *target)
            elif (
                status == JobStatus.CANCELLED
                and current[1] in REACTIVATABLE_REASONS
                and target[0] in OPEN_JOB_STATUSES
                and runnable
            ):
                update(job, *target)
        if runnable:
            existing = {job["contact_id"] for job in jobs}
            for contact_id in sorted(members - existing):
                contact = contacts[contact_id]
                status, reason = self.contact_gate(contact, suppressed)
                if status == JobStatus.CANCELLED:
                    continue
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO call_jobs (campaign_id, contact_id, idempotency_key, "
                    "status, reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        contact_id,
                        job_key(campaign, contact),
                        status.value,
                        reason.value if reason else None,
                        now,
                        now,
                    ),
                ).rowcount
                report.created += inserted
        return report

    # ------------------------------------------------------------------ jobs and results

    def list_jobs(
        self, *, campaign_id: int | None = None, status: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT j.*, c.name AS campaign_name, k.name AS contact_name, "
            "k.phone_e164 AS phone FROM call_jobs j JOIN campaigns c ON c.id = j.campaign_id "
            "JOIN contacts k ON k.id = j.contact_id WHERE 1 = 1"
        )
        params: list[Any] = []
        if campaign_id is not None:
            sql += " AND j.campaign_id = ?"
            params.append(campaign_id)
        if status is not None:
            sql += " AND j.status = ?"
            params.append(status)
        sql += " ORDER BY j.id LIMIT ?"
        params.append(limit)
        with self.db.connect(must_exist=True) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def jobs_for_sync(self, campaign_id: int) -> list[sqlite3.Row]:
        with self.db.connect(must_exist=True) as conn:
            return conn.execute(
                "SELECT j.*, c.name AS campaign_name, c.notion_page_id AS campaign_page_id, "
                "k.name AS contact_name, k.notion_page_id AS contact_page_id "
                "FROM call_jobs j JOIN campaigns c ON c.id = j.campaign_id "
                "JOIN contacts k ON k.id = j.contact_id WHERE j.campaign_id = ? ORDER BY j.id",
                (campaign_id,),
            ).fetchall()

    def job_for_sync(self, job_id: int) -> sqlite3.Row:
        with self.db.connect(must_exist=True) as conn:
            row = conn.execute(
                "SELECT j.*, c.name AS campaign_name, c.notion_page_id AS campaign_page_id, "
                "k.name AS contact_name, k.notion_page_id AS contact_page_id "
                "FROM call_jobs j JOIN campaigns c ON c.id = j.campaign_id "
                "JOIN contacts k ON k.id = j.contact_id WHERE j.id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return row

    def campaign_needs_job_sync(self, campaign: sqlite3.Row) -> bool:
        """Active campaigns are reconciled every cycle; final ones only while work is pending.

        A cancelled job needs no Notion page, unless a page may exist already (an unconfirmed
        create): that page is looked up and marked as cancelled.
        """
        if campaign["status"] not in FINAL_CAMPAIGN_STATUSES:
            return True
        with self.db.connect(must_exist=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM call_jobs WHERE campaign_id = ? AND notion_deleted = 0 AND "
                "(notion_dirty = 1 OR (notion_page_id IS NULL AND (status != 'cancelled' OR "
                "notion_uncertain_since IS NOT NULL))) LIMIT 1",
                (campaign["id"],),
            ).fetchone()
        return row is not None

    def mark_job_without_page(self, job_id: int, *, expected_updated_at: str) -> None:
        """A cancelled job that never got a Notion page does not need one."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_jobs SET notion_uncertain_since = NULL, notion_dirty = CASE WHEN "
                "updated_at = ? THEN 0 ELSE notion_dirty END WHERE id = ? AND "
                "notion_page_id IS NULL",
                (expected_updated_at, job_id),
            )

    def record_job_page(
        self,
        job_id: int,
        page_id: str,
        pushed: dict[str, Any] | None,
        *,
        expected_updated_at: str | None = None,
    ) -> None:
        """Remember the Notion page of a job and what was written to it.

        `expected_updated_at`: when the job changed locally after the values were read (planner
        in another process), the job stays dirty so that the next cycle writes the new values.
        """
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_jobs SET notion_page_id = ?, notion_pushed = ?, "
                "notion_uncertain_since = NULL, notion_dirty = CASE WHEN ? IS NULL OR "
                "updated_at = ? THEN 0 ELSE 1 END WHERE id = ?",
                (
                    page_id,
                    json.dumps(pushed, ensure_ascii=False) if pushed is not None else None,
                    expected_updated_at,
                    expected_updated_at,
                    job_id,
                ),
            )

    def mark_job_uncertain(self, job_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_jobs SET notion_uncertain_since = COALESCE(notion_uncertain_since, ?) "
                "WHERE id = ?",
                (self._now(), job_id),
            )

    def adopt_job_state(
        self, job_id: int, status: JobStatus, reason: JobReason | None, attempts: int
    ) -> None:
        """After the local DB was lost: take over a final state found on the job's Notion page."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_jobs SET status = ?, reason = ?, attempts = MAX(attempts, ?), "
                "notion_dirty = 1, updated_at = ? WHERE id = ? AND status IN ('queued', 'blocked')",
                (status.value, reason.value if reason else None, attempts, self._now(), job_id),
            )

    def cancel_job_from_notion(self, job_id: int, reason: JobReason, *, deleted: bool) -> bool:
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE call_jobs SET status = 'cancelled', reason = ?, notion_dirty = 1, "
                "notion_deleted = MAX(notion_deleted, ?), updated_at = ? "
                "WHERE id = ? AND status IN ('queued', 'blocked', 'in_progress')",
                (reason.value, int(deleted), self._now(), job_id),
            ).rowcount
            if deleted:
                conn.execute(
                    "UPDATE call_jobs SET notion_deleted = 1, notion_dirty = 0 WHERE id = ?",
                    (job_id,),
                )
            if changed:
                self.db.audit(
                    "job.cancel",
                    subject=f"job:{job_id}",
                    details={"reason": reason.value},
                    conn=conn,
                )
        return bool(changed)

    def results_to_push(self, campaign_id: int) -> list[sqlite3.Row]:
        with self.db.connect(must_exist=True) as conn:
            return conn.execute(
                "SELECT r.*, j.notion_page_id AS job_page_id, j.idempotency_key AS job_key, "
                "c.notion_page_id AS campaign_page_id, c.name AS campaign_name, "
                "k.notion_page_id AS contact_page_id, k.name AS contact_name "
                "FROM call_results r JOIN call_jobs j ON j.id = r.job_id "
                "JOIN campaigns c ON c.id = j.campaign_id JOIN contacts k ON k.id = j.contact_id "
                "WHERE j.campaign_id = ? AND r.notion_page_id IS NULL ORDER BY r.id",
                (campaign_id,),
            ).fetchall()

    def record_result_page(self, result_id: int, page_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_results SET notion_page_id = ?, notion_uncertain_since = NULL "
                "WHERE id = ?",
                (page_id, result_id),
            )

    def mark_result_uncertain(self, result_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_results SET notion_uncertain_since = "
                "COALESCE(notion_uncertain_since, ?) WHERE id = ?",
                (self._now(), result_id),
            )

    def simulate(self, *, campaign_id: int | None = None, limit: int = 10) -> SimulationReport:
        """Complete queued jobs with a simulated result. No telephony is touched at all.

        Every condition is checked again at "dial time" inside the same transaction: the
        campaign must be running, the contact must still belong to it, the number must be valid
        and not on the do-not-call list, and a Notion job must already be reconciled with Notion.
        """
        report = SimulationReport()
        now = self._now()
        with self.db.transaction() as conn:
            self._plan(conn, campaign_ids=[campaign_id] if campaign_id is not None else None)
            suppressed = {r[0] for r in conn.execute("SELECT DISTINCT phone_e164 FROM suppression")}
            sql = "SELECT * FROM call_jobs WHERE status = 'queued'"
            params: tuple[Any, ...] = ()
            if campaign_id is not None:
                sql += " AND campaign_id = ?"
                params = (campaign_id,)
            for job in conn.execute(sql + " ORDER BY id", params).fetchall():
                if report.completed >= limit:
                    break
                campaign = self._campaign(conn, job["campaign_id"])
                contact = conn.execute(
                    "SELECT * FROM contacts WHERE id = ?", (job["contact_id"],)
                ).fetchone()
                member = conn.execute(
                    "SELECT 1 FROM campaign_contacts WHERE campaign_id = ? AND contact_id = ?",
                    (job["campaign_id"], job["contact_id"]),
                ).fetchone()
                callable_now = (
                    self.campaign_runnable(campaign)
                    and member is not None
                    and self.contact_gate(contact, suppressed) == (JobStatus.QUEUED, None)
                    and (campaign["source"] != "notion" or job["notion_page_id"] is not None)
                )
                if not callable_now:
                    report.skipped += 1
                    continue
                attempt = job["attempts"] + 1
                conn.execute(
                    "INSERT INTO call_results (job_id, attempt, idempotency_key, outcome, "
                    "simulated, started_at, duration_s, summary, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, 0, ?, ?)",
                    (
                        job["id"],
                        attempt,
                        result_key(job["idempotency_key"], attempt),
                        Outcome.SIMULATED.value,
                        now,
                        SIMULATED_SUMMARY,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE call_jobs SET status = 'done', reason = ?, attempts = ?, "
                    "notion_dirty = 1, updated_at = ? WHERE id = ?",
                    (JobReason.SIMULATED.value, attempt, now, job["id"]),
                )
                report.completed += 1
                report.job_ids.append(job["id"])
        return report

    # ------------------------------------------------------------------ sync runs

    def start_sync_run(self) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO notion_sync_runs (started_at, status) VALUES (?, 'running')",
                (self._now(),),
            )
            return int(cursor.lastrowid or 0)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        stats: dict[str, Any],
        error_code: str | None = None,
        error_message: str | None = None,
        keep: int = 200,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE notion_sync_runs SET finished_at = ?, status = ?, error_code = ?, "
                "error_message = ?, stats = ? WHERE id = ?",
                (
                    self._now(),
                    status,
                    error_code,
                    error_message,
                    json.dumps(stats, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
            conn.execute(
                "DELETE FROM notion_sync_runs WHERE id <= (SELECT MAX(id) FROM notion_sync_runs)"
                " - ?",
                (keep,),
            )

    def recent_sync_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.connect(must_exist=True) as conn:
            rows = conn.execute(
                "SELECT * FROM notion_sync_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(r), "stats": json.loads(r["stats"] or "{}")} for r in rows]


def campaign_status_label(status: str) -> str:
    return CAMPAIGN_STATUS_LABELS[CampaignStatus(status)]
