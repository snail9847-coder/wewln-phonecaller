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
    Migration(
        version=3,
        name="telephony_attempts_events_permits",
        statements=(
            # A job waiting for its next attempt (retry after busy / no answer).
            "ALTER TABLE call_jobs ADD COLUMN next_attempt_at TEXT",
            # One row per dial attempt, written before the provider is asked to dial: after a
            # crash every call that may exist is known. `provider_call_id` is derived from the
            # idempotency key, so the same attempt can never become two calls.
            """
            CREATE TABLE call_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('job', 'test')),
                job_id INTEGER REFERENCES call_jobs (id),
                attempt INTEGER NOT NULL DEFAULT 1,
                idempotency_key TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                provider_call_id TEXT NOT NULL UNIQUE,
                simulated INTEGER NOT NULL,
                target TEXT NOT NULL DEFAULT 'phone' CHECK (target IN ('phone', 'softphone')),
                phone_masked TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK (state IN ('pending', 'dialing', 'ringing',
                    'answered', 'completed', 'busy', 'no_answer', 'rejected', 'unreachable',
                    'failed', 'cancelled')),
                end_reason TEXT,
                end_request TEXT,
                cancel_requested TEXT,
                hangup_requested_at TEXT,
                hangup_tries INTEGER NOT NULL DEFAULT 0,
                hangup_cause INTEGER,
                hangup_cause_txt TEXT,
                tech_cause INTEGER,
                error_code TEXT,
                created_at TEXT NOT NULL,
                dial_started_at TEXT,
                ringing_at TEXT,
                answered_at TEXT,
                ended_at TEXT,
                updated_at TEXT NOT NULL,
                CHECK ((kind = 'job') = (job_id IS NOT NULL))
            )
            """,
            "CREATE UNIQUE INDEX idx_call_attempts_job_attempt ON call_attempts (job_id, attempt) "
            "WHERE job_id IS NOT NULL",
            # At most one live attempt per job, enforced by SQLite itself.
            "CREATE UNIQUE INDEX idx_call_attempts_one_active ON call_attempts (job_id) "
            "WHERE job_id IS NOT NULL AND state IN ('pending', 'dialing', 'ringing', 'answered')",
            "CREATE INDEX idx_call_attempts_state ON call_attempts (state)",
            # Every provider event once: a repeated event (reconnect, webhook retry) is ignored.
            """
            CREATE TABLE call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                provider_call_id TEXT NOT NULL,
                attempt_id INTEGER REFERENCES call_attempts (id),
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_call_events_received ON call_events (received_at)",
            # The operator's explicit, short-lived permission for a real test call to one number.
            """
            CREATE TABLE real_call_permits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_e164 TEXT NOT NULL,
                max_calls INTEGER NOT NULL DEFAULT 1 CHECK (max_calls BETWEEN 1 AND 3),
                used INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """,
            "CREATE INDEX idx_real_call_permits_phone ON real_call_permits (phone_e164)",
            # Runtime switches shared by all processes (the emergency stop).
            """
            CREATE TABLE runtime_flags (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=4,
        name="voice_conversations",
        statements=(
            # How the conversation of an answered call ended (voice mode), whether the person
            # asked not to be called again, and how many times they spoke.
            "ALTER TABLE call_attempts ADD COLUMN voice_end TEXT",
            "ALTER TABLE call_attempts ADD COLUMN opt_out INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE call_attempts ADD COLUMN voice_turns INTEGER",
            # The transcript: one row per phrase, with the time it took to answer. Kept for
            # WEWLN_VOICE_TRANSCRIPT_DAYS, then deleted.
            """
            CREATE TABLE call_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES call_attempts (id),
                seq INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('assistant', 'caller')),
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                interrupted INTEGER NOT NULL DEFAULT 0,
                at_ms INTEGER NOT NULL,
                speech_ms INTEGER,
                stt_ms INTEGER,
                llm_ms INTEGER,
                tts_ms INTEGER,
                response_ms INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE (attempt_id, seq)
            )
            """,
            "CREATE INDEX idx_call_turns_created ON call_turns (created_at)",
        ),
    ),
)
