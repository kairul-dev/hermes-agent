"""Persisted-owner admission precedes listing windows and canonical-title absence."""

import io
import threading

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import StdioTransport


class _LoginTransport(StdioTransport):
    auth_identity = None


def _transport(identity=None):
    transport = _LoginTransport(lambda: io.StringIO(), threading.Lock())
    transport.auth_identity = identity
    return transport


def _call(transport, method, **params):
    token = server.bind_transport(transport)
    try:
        return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    finally:
        server.reset_transport(token)


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("dashboard:\n  shared_runtime:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(server, "_hermes_home", home)
    db = SessionDB(home / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    yield db, home
    db.close()


def _seed(db, key, owner, when, *, title=None, hidden=False):
    db.create_session(key, source="desktop", user_id=owner)
    db.append_message(key, "user", f"synthetic {key}", timestamp=when)
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (when, key)))
    if title:
        db.set_session_title(key, title)
    if hidden:
        db.set_session_hidden(key, True)


@pytest.mark.parametrize("limit,foreign_count", [(20, 201), (200, 401)])
@pytest.mark.parametrize("owner,identity", [
    ("basic:alice", {"provider": "basic", "user_id": "alice"}),
    (None, None),
])
def test_owned_sessions_survive_foreign_listing_windows(store, limit, foreign_count, owner, identity):
    db, _home = store
    _seed(db, "owned-old", owner, 1000)
    for index in range(foreign_count):
        _seed(db, f"foreign-{index}", "oidc:alice", 2000 + index)
    caller = _transport(identity)

    response = _call(caller, "session.list", limit=limit)
    assert [row["id"] for row in response["result"]["sessions"]] == ["owned-old"]
    assert _call(caller, "session.most_recent")["result"]["session_id"] == "owned-old"


@pytest.mark.parametrize("owner", ["basic:unknown", "", " basic:alice "])
def test_listing_excludes_foreign_or_malformed_persisted_owners(store, owner):
    db, _home = store
    _seed(db, "ineligible", owner, 2000)
    _seed(db, "owned", "basic:alice", 1000)
    caller = _transport({"provider": "basic", "user_id": "alice"})
    assert [row["id"] for row in _call(caller, "session.list")["result"]["sessions"]] == ["owned"]


@pytest.mark.parametrize("identity", [{}, {"provider": "basic", "user_id": ""}])
def test_unverifiable_callers_are_denied_before_querying(store, monkeypatch, identity):
    db, _home = store
    monkeypatch.setattr(db, "list_sessions_rich", lambda **kwargs: pytest.fail("unverifiable caller queried rows"))
    for method in ("session.list", "session.most_recent"):
        assert _call(_transport(identity), method)["error"]["code"] == 4403


def test_unavailable_policy_stays_fail_closed(store, monkeypatch):
    db, home = store
    (home / "config.yaml").write_text("dashboard: []\n", encoding="utf-8")
    monkeypatch.setattr(db, "list_sessions_rich", lambda **kwargs: pytest.fail("unavailable policy queried rows"))
    assert _call(_transport(), "session.list")["error"]["code"] == 4403


def test_rpc_keeps_final_owner_check_after_query(store, monkeypatch):
    db, _home = store

    def foreign_result(**kwargs):
        assert kwargs["owner_user_id"] == "basic:alice"
        return [{"id": "foreign", "user_id": "basic:bob", "source": "desktop"}, {"id": "missing-owner"}]

    monkeypatch.setattr(db, "list_sessions_rich", foreign_result)
    caller = _transport({"provider": "basic", "user_id": "alice"})
    assert _call(caller, "session.list")["result"]["sessions"] == []


@pytest.mark.parametrize("owner", ["basic:bob", None, "", " basic:alice "])
def test_foreign_or_malformed_title_is_denied_without_side_effects(store, monkeypatch, owner):
    db, _home = store
    _seed(db, "canonical", owner, 1000, title="Bot Chat", hidden=True)
    before = db.get_session("canonical")
    monkeypatch.setattr(server, "_unarchive_recoverable", lambda *args: pytest.fail("foreign title was revived"))
    caller = _transport({"provider": "basic", "user_id": "alice"})

    response = _call(caller, "session.list", title="Bot Chat", include_hidden=True)
    assert response["error"]["code"] == 4403
    assert "canonical" not in response["error"]["message"]
    assert db.get_session("canonical") == before
    assert [row["id"] for row in db.list_sessions_rich(include_hidden=True)] == ["canonical"]


def test_same_owner_can_resolve_canonical_title_and_confirm_absence(store):
    db, _home = store
    _seed(db, "canonical", "basic:alice", 1000, title="Bot Chat", hidden=True)
    caller = _transport({"provider": "basic", "user_id": "alice"})
    response = _call(caller, "session.list", title="Bot Chat", include_hidden=True)
    assert response["result"]["sessions"][0]["id"] == "canonical"
    assert _call(caller, "session.list", title="Absent")["result"]["sessions"] == []


def test_ordinary_listing_and_title_lookup_keep_existing_visibility(store):
    db, home = store
    (home / "config.yaml").write_text("dashboard:\n  shared_runtime:\n    enabled: false\n", encoding="utf-8")
    _seed(db, "foreign", "basic:bob", 2000, title="Bot Chat")
    _seed(db, "owned", "basic:alice", 1000)
    caller = _transport({"provider": "basic", "user_id": "alice"})
    assert {row["id"] for row in _call(caller, "session.list")["result"]["sessions"]} == {"owned", "foreign"}
    assert _call(caller, "session.list", title="Bot Chat")["result"]["sessions"][0]["id"] == "foreign"
    assert _call(caller, "session.most_recent")["result"]["session_id"] == "owned"
