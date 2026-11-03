"""SQLite storage: one file, WAL journal, transactional forward-only migrations (stdlib only)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import StorageError
from ..redact import redact, redact_value
from .migrations import MIGRATIONS, Migration


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path, *, migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
        self.path = Path(path)
        self._migrations = tuple(sorted(migrations, key=lambda m: m.version))

    @property
    def latest_version(self) -> int:
        return self._migrations[-1].version if self._migrations else 0

    @contextmanager
    def connect(self, *, must_exist: bool = False) -> Iterator[sqlite3.Connection]:
        """A short-lived connection in autocommit mode; transactions are explicit."""
        if must_exist:
            # mode=rw fails on a missing file instead of silently creating an empty database.
            target, uri = self.path.resolve().as_uri() + "?mode=rw", True
        else:
            target, uri = str(self.path), False
        try:
            conn = sqlite3.connect(target, timeout=5.0, isolation_level=None, uri=uri)
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open database ({type(exc).__name__})") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0])

    def migrate(self) -> int:
        """Apply pending migrations, each in its own transaction. Returns the schema version."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                for migration in self._migrations:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        done = conn.execute(
                            "SELECT 1 FROM schema_migrations WHERE version = ?",
                            (migration.version,),
                        ).fetchone()
                        if not done:
                            for statement in migration.statements:
                                conn.execute(statement)
                            conn.execute(
                                "INSERT INTO schema_migrations (version, name, applied_at) "
                                "VALUES (?, ?, ?)",
                                (migration.version, migration.name, _now()),
                            )
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
                return self._version(conn)
        except sqlite3.Error as exc:
            raise StorageError(f"database migration failed ({type(exc).__name__}: {exc})") from exc

    def check(self) -> dict[str, Any]:
        """Readiness probe. Never creates the database file."""
        if not self.path.is_file():
            return {"status": "error", "error": "database_missing"}
        try:
            with self.connect(must_exist=True) as conn:
                conn.execute("SELECT 1").fetchone()
                version = self._version(conn)
                journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except (sqlite3.Error, StorageError) as exc:
            return {"status": "error", "error": type(exc).__name__}
        result: dict[str, Any] = {
            "status": "ok" if version == self.latest_version else "error",
            "schema_version": version,
            "expected_schema_version": self.latest_version,
            "journal_mode": journal,
        }
        if version != self.latest_version:
            result["error"] = "schema_out_of_date"
        return result

    def audit(
        self,
        action: str,
        *,
        actor: str = "system",
        subject: str | None = None,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> int:
        """Append to the audit trail. Phone numbers in subject and details are stored masked."""
        payload = json.dumps(redact_value(details or {}), ensure_ascii=False, sort_keys=True)
        masked_subject = redact(subject) if subject else None
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO audit_log (ts, actor, action, subject, details, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (_now(), actor, action, masked_subject, payload, request_id),
                )
                return int(cursor.lastrowid or 0)
        except sqlite3.Error as exc:
            raise StorageError(f"audit write failed ({type(exc).__name__})") from exc

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, action, subject, details, request_id FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]
