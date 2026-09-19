"""The pass over open levels, and what one failing level costs.

Robinhood's public RPC rate-limits, so a level failing mid-pass is ordinary.
What was not ordinary is what it used to cost: the rollback that cleaned up
after it expired every row the session held, and reading the next one then
tried to reload itself from inside plain attribute access -- IO that async
SQLAlchemy refuses to do. One 429 took the whole pass down, every pass.
"""

import pytest

from app.dex.chain import ChainError
from app.workers.dex import DexWorker


class FakeRow:
    """A row that punishes being read while expired, the way a real one does."""

    _FIELDS = (
        "id", "status", "created_at", "submitted_at",
        "expires_at", "blocked_until", "tx_hash", "raw_tx",
    )

    def __init__(self, intent_id, status="WAITING"):
        object.__setattr__(self, "pk", intent_id)
        object.__setattr__(self, "expired", False)
        object.__setattr__(
            self,
            "_data",
            {"id": intent_id, "status": status}
            | {name: None for name in self._FIELDS[2:]},
        )

    def __getattr__(self, name):
        data = object.__getattribute__(self, "_data")
        if name not in data:
            raise AttributeError(name)
        if object.__getattribute__(self, "expired"):
            raise RuntimeError(
                f"read {name} off an expired row: "
                "greenlet_spawn has not been called"
            )
        return data[name]


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1
        for row in self.rows:
            object.__setattr__(row, "expired", True)


class FakeRepository:
    """Only the two reads the pass makes before it touches a level."""

    def __init__(self, session):
        self.session = session
        self.reloaded = []

    async def open_intents(self):
        return list(self.session.rows)

    async def reload(self, intent_id):
        self.reloaded.append(intent_id)
        for row in self.session.rows:
            if row.pk == intent_id:
                object.__setattr__(row, "expired", False)
                return row
        return None


class RecordingWorker(DexWorker):
    """A worker with no network clients: only the loop is under test."""

    def __init__(self, *, fail_on=(), gone=()):
        self.applied = []
        self.fail_on = set(fail_on)
        self.gone = set(gone)

    async def _apply(self, session, repository, intent, action):
        self.applied.append(intent.id)
        if intent.id in self.fail_on:
            raise ChainError("429 Too Many Requests")


@pytest.fixture
def fake_repository(monkeypatch):
    holder = {}

    def factory(session):
        holder["repository"] = FakeRepository(session)
        return holder["repository"]

    monkeypatch.setattr("app.workers.dex.DexIntentRepository", factory)
    return holder


async def test_one_failing_level_costs_only_that_level(fake_repository):
    session = FakeSession([FakeRow(1), FakeRow(2), FakeRow(3)])
    worker = RecordingWorker(fail_on={1})

    await worker.tick(session)

    assert worker.applied == [1, 2, 3]
    assert session.rollbacks == 1


async def test_every_level_is_read_back_rather_than_remembered(fake_repository):
    """Rows are re-read by id, not carried across the rollback that expires them."""
    session = FakeSession([FakeRow(1), FakeRow(2)])
    worker = RecordingWorker()

    await worker.tick(session)

    assert fake_repository["repository"].reloaded == [1, 2]


async def test_a_level_cancelled_mid_pass_is_skipped_not_an_error(monkeypatch):
    session = FakeSession([FakeRow(1), FakeRow(2)])
    worker = RecordingWorker()

    class Vanishing(FakeRepository):
        async def reload(self, intent_id):
            if intent_id == 1:
                self.session.rows = [r for r in self.session.rows if r.pk != 1]
                return None
            return await super().reload(intent_id)

    monkeypatch.setattr("app.workers.dex.DexIntentRepository", Vanishing)
    await worker.tick(session)

    assert worker.applied == [2]
    assert session.rollbacks == 0
