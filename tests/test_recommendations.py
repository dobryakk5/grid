from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.trading.recommendations import accept_recommendation, claim_recommendation


class Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class Session:
    def __init__(self, row):
        self.row = row
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)
        return Result(self.row)

    def add(self, item):
        self.added.append(item)


@pytest.mark.asyncio
async def test_claim_uses_a_single_conditional_update_then_accepts():
    recommendation = SimpleNamespace(
        id=1, profile_id=2, type="RETURN_TO_GRID", status="PROCESSING",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), resolved_at=None,
    )
    session = Session(recommendation)

    claimed = await claim_recommendation(session, 1)
    await accept_recommendation(session, claimed)

    assert claimed.status == "ACCEPTED"
    assert "status" in str(session.statements[0]).lower()
