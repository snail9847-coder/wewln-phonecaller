"""Forward-only schema migrations. Never edit an applied migration; add a new one."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="foundation_audit_log",
        statements=(
            """
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                subject TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                request_id TEXT
            )
            """,
            "CREATE INDEX idx_audit_log_ts ON audit_log (ts)",
            "CREATE INDEX idx_audit_log_action ON audit_log (action)",
        ),
    ),
    Migration(
        version=2,
        name="campaigns_contacts_jobs_notion",
        statements=(
            """
            CREATE TABLE campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK (source IN ('local', 'notion')),
                notion_page_id TEXT UNIQUE,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'running', 'paused', 'stopped', 'completed')),
                notion_status TEXT,
                script TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru-RU',
                max_attempts INTEGER NOT NULL DEFAULT 1,
                problem TEXT NOT NULL DEFAULT '',
                stopped_at TEXT,
                stop_source TEXT,
                stop_reason TEXT,
                notion_push_status INTEGER NOT NULL DEFAULT 0,
                missing_in_notion INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK (source IN ('local', 'notion')),
                notion_page_id TEXT UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                phone_raw TEXT NOT NULL DEFAULT '',
                phone_e164 TEXT,
                phone_error TEXT,
                notion_dnc INTEGER NOT NULL DEFAULT 0,
                missing_in_notion INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX idx_contacts_local_phone ON contacts (phone_e164) "
            "WHERE source = 'local'",
            "CREATE INDEX idx_contacts_phone ON contacts (phone_e164)",
            """
            CREATE TABLE campaign_contacts (
                campaign_id INTEGER NOT NULL REFERENCES campaigns (id),
                contact_id INTEGER NOT NULL REFERENCES contacts (id),
                added_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, contact_id)
            )
            """,
            "CREATE INDEX idx_campaign_contacts_contact ON campaign_contacts (contact_id)",
            """
            CREATE TABLE suppression (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_e164 TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('manual', 'notion', 'opt_out')),
                source_ref TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (phone_e164, source, source_ref)
            )
            """,
            "CREATE INDEX idx_suppression_phone ON suppression (phone_e164)",
            """
            CREATE TABLE call_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL REFERENCES campaigns (id),
                contact_id INTEGER NOT NULL REFERENCES contacts (id),
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN
                    ('queued', 'blocked', 'in_progress', 'done', 'failed', 'cancelled')),
                reason TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                notion_page_id TEXT UNIQUE,
                notion_pushed TEXT,
                notion_dirty INTEGER NOT NULL DEFAULT 1,
                notion_uncertain_since TEXT,
                notion_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (campaign_id, contact_id)
            )
            """,
            "CREATE INDEX idx_call_jobs_status ON call_jobs (status)",
            """
            CREATE TABLE call_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES call_jobs (id),
                attempt INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL,
                simulated INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                duration_s REAL NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '',
                notion_page_id TEXT UNIQUE,
                notion_uncertain_since TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (job_id, attempt)
            )
            """,
            """
            CREATE TABLE notion_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                stats TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE locks (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """,
        ),
    ),
)
