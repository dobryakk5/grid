"""The schema fingerprint that lets a restart skip init_db's DDL."""

from sqlalchemy import create_mock_engine

from app.db import init
from app.db.models import Base


def test_the_fingerprint_survives_create_all():
    """The trap this guards. ``create_all`` resolves the foreign-key cycle
    between grid_profiles and grid_ranges by marking those keys ``use_alter``
    -- on the shared metadata. A hash of compiled CREATE TABLE text changed
    the moment the first migration ran, so the next check in the same
    process said "stale" and ran the DDL again: forty ACCESS EXCLUSIVE locks
    beside live trading, which deadlocked on deploy."""
    before = init.schema_fingerprint()

    engine = create_mock_engine("postgresql://", lambda *_a, **_k: None)
    Base.metadata.create_all(engine, checkfirst=False)

    assert init.schema_fingerprint() == before


def test_a_new_migration_changes_the_fingerprint(monkeypatch):
    before = init.schema_fingerprint()
    monkeypatch.setattr(init, "_MIGRATIONS", init._MIGRATIONS + ("SELECT 1",))
    assert init.schema_fingerprint() != before
