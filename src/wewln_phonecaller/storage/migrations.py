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
)
