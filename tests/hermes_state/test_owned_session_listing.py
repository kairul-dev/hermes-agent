"""Optional owner filtering applies to SQL windows and pinned backfill, including NULL."""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def _seed(db, key, owner, when):
    db.create_session(key, source="desktop", user_id=owner)
    db.append_message(key, "user", "synthetic", timestamp=when)
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (when, key)))


@pytest.mark.parametrize("order_by_last_active", [False, True])
def test_owner_filter_distinguishes_omission_local_null_and_provider(db, order_by_last_active):
    _seed(db, "local", None, 1000)
    _seed(db, "basic", "basic:alice", 2000)
    _seed(db, "oidc", "oidc:alice", 3000)
    options = {"limit": 1, "order_by_last_active": order_by_last_active}
    assert db.list_sessions_rich(**options)[0]["id"] == "oidc"
    assert db.list_sessions_rich(**options, owner_user_id=None)[0]["id"] == "local"
    assert db.list_sessions_rich(**options, owner_user_id="basic:alice")[0]["id"] == "basic"


def test_foreign_pins_cannot_bypass_owner_filter(db):
    _seed(db, "owned", "basic:alice", 2000)
    _seed(db, "owned-pin", "basic:alice", 1000)
    _seed(db, "foreign-pin", "basic:bob", 3000)
    db.set_session_pinned("owned-pin", True)
    db.set_session_pinned("foreign-pin", True)
    rows = db.list_sessions_rich(limit=1, owner_user_id="basic:alice", include_pinned=True)
    assert {row["id"] for row in rows} == {"owned", "owned-pin"}
