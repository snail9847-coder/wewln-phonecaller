import pytest

from wewln_phonecaller.errors import StorageError
from wewln_phonecaller.storage import MIGRATIONS, Database, Migration


def table_names(db: Database) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def applied_versions(db: Database) -> list[int]:
    with db.connect() as conn:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [row[0] for row in rows]


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "nested" / "wewln.db")
    assert db.migrate() == MIGRATIONS[-1].version
    assert db.migrate() == MIGRATIONS[-1].version  # nothing is applied twice
    assert applied_versions(db) == [m.version for m in MIGRATIONS]
    assert {"schema_migrations", "audit_log"} <= table_names(db)


def test_check_reports_ok_and_wal(tmp_path):
    db = Database(tmp_path / "wewln.db")
    db.migrate()
    result = db.check()
    assert result["status"] == "ok"
    assert result["schema_version"] == result["expected_schema_version"] == db.latest_version
    assert result["journal_mode"] == "wal"


def test_check_never_creates_the_database(tmp_path):
    path = tmp_path / "missing.db"
    assert Database(path).check() == {"status": "error", "error": "database_missing"}
    assert not path.exists()


def test_check_detects_an_outdated_schema(tmp_path):
    path = tmp_path / "wewln.db"
    Database(path).migrate()
    future = Migration(version=99, name="future", statements=("CREATE TABLE future (id INTEGER)",))
    result = Database(path, migrations=(*MIGRATIONS, future)).check()
    assert result["status"] == "error"
    assert result["error"] == "schema_out_of_date"
    assert result["expected_schema_version"] == 99


def test_failed_migration_is_rolled_back(tmp_path):
    path = tmp_path / "wewln.db"
    broken = Migration(
        version=MIGRATIONS[-1].version + 1,
        name="broken",
        statements=("CREATE TABLE half_done (id INTEGER)", "THIS IS NOT SQL"),
    )
    with pytest.raises(StorageError):
        Database(path, migrations=(*MIGRATIONS, broken)).migrate()
    db = Database(path)
    assert "half_done" not in table_names(db)
    assert applied_versions(db) == [m.version for m in MIGRATIONS]
    assert db.check()["status"] == "ok"


def test_unusable_path_raises_storage_error(tmp_path):
    folder = tmp_path / "folder.db"
    folder.mkdir()
    with pytest.raises(StorageError):
        Database(folder).migrate()
    assert Database(folder).check()["status"] == "error"


def test_audit_stores_phone_numbers_masked(tmp_path):
    db = Database(tmp_path / "wewln.db")
    db.migrate()
    db.audit("app.start")
    row_id = db.audit(
        "call.simulated",
        subject="contact +79161234567",
        details={"to": "+7 916 123-45-67", "history": ["8 916 123 45 67"], "attempt": 2},
        request_id="req-1",
    )
    assert row_id > 0
    latest, first = db.recent_audit()
    assert first["action"] == "app.start"
    assert latest["subject"] == "contact +79*******67"
    assert latest["details"] == {"attempt": 2, "history": ["89*******67"], "to": "+79*******67"}
    assert latest["request_id"] == "req-1"
    # Nothing unmasked reaches the files on disk (database and WAL journal).
    for file in tmp_path.glob("wewln.db*"):
        assert b"1234567" not in file.read_bytes()


def test_lock_is_exclusive_until_released_or_expired(tmp_path):
    db = Database(tmp_path / "wewln.db")
    db.migrate()
    assert db.acquire_lock("sync", "a", ttl_seconds=60)
    assert db.acquire_lock("sync", "a", ttl_seconds=60)  # renewal by the owner
    assert not db.acquire_lock("sync", "b", ttl_seconds=60)
    assert db.lock_holder("sync") == "a"
    db.release_lock("sync", "a")
    assert db.lock_holder("sync") is None
    assert db.acquire_lock("sync", "b", ttl_seconds=-1)  # already expired
    assert db.acquire_lock("sync", "a", ttl_seconds=60)


def test_transaction_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "wewln.db")
    db.migrate()
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("INSERT INTO locks (name, owner, expires_at) VALUES ('x', 'y', 0)")
        raise RuntimeError("boom")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 0
