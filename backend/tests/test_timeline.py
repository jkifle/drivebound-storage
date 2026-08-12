import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1.assets import timeline_statement
from app.services.timeline import TimelineCursor, decode_cursor, encode_cursor


def test_cursor_round_trip_normalizes_to_utc():
    cursor = TimelineCursor(
        timeline_at=datetime(2025, 6, 1, 12, 30, tzinfo=timezone(timedelta(hours=-4))),
        asset_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    )
    decoded = decode_cursor(encode_cursor(cursor))

    assert decoded.timeline_at == datetime(2025, 6, 1, 16, 30, tzinfo=timezone.utc)
    assert decoded.asset_id == cursor.asset_id


@pytest.mark.parametrize("value", ["not-a-cursor", "e30", ""])
def test_invalid_cursor_is_a_bad_request(value):
    with pytest.raises(HTTPException) as error:
        decode_cursor(value)
    assert error.value.status_code == 400


def test_timeline_query_is_keyset_paginated():
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    cursor = TimelineCursor(
        timeline_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        asset_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    )
    sql = str(
        timeline_statement(user_id, 25, cursor).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "assets.user_id =" in sql
    assert "coalesce(assets.taken_at, assets.created_at) <" in sql
    assert "assets.id <" in sql
    assert "ORDER BY coalesce(assets.taken_at, assets.created_at) DESC, assets.id DESC" in sql
    assert "LIMIT 26" in sql
