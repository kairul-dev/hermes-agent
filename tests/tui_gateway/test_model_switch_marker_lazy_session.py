"""A model switch on an agent-ready fresh draft must land its durable marker, not trip the messages FK.

Drafts are lazy: ``session.create`` mints a key but no ``sessions`` row until real activity (first submit).
Once the agent is built it carries a ``_session_db`` HANDLE, which ``_append_model_switch_marker`` took as proof
the row existed and skipped the lazy row creation, so the marker INSERT failed with
``FOREIGN KEY constraint failed`` and the switch lost its durable notice. An explicit model switch is activity.
"""
import logging
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server


def _draft(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 96, "source": "desktop"}})
    assert "result" in resp, resp
    sid, key = resp["result"]["session_id"], resp["result"]["stored_session_id"]
    assert db.get_session(key) is None  # opening a draft never materializes it
    return sid, key, server._sessions[sid]


def _markers(db, key):
    prefix = server._MODEL_SWITCH_MARKER_PREFIX
    return [r for r in db.get_messages(key) if prefix in str(r.get("content") or "")]


@pytest.mark.parametrize("shape", ["no_agent", "agent_handle_no_row", "agent_handle_row_present", "rotated_live"])
def test_marker_is_durable_in_the_live_identity(monkeypatch, tmp_path, caplog, shape):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, session = _draft(monkeypatch, db)
    try:
        expected = key
        if shape != "no_agent":
            session["agent"] = SimpleNamespace(session_id=key, _session_db=db)
        if shape in ("agent_handle_row_present", "rotated_live"):
            assert server._ensure_session_db_row(session) is not False
        if shape == "rotated_live":
            from hermes_state_ids import new_session_id

            expected = new_session_id()
            db.publish_compression_child(
                parent_session_id=key, child_session_id=expected, source="desktop", model="test-model",
                messages=[{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}],
                compression_lock_holder=None, require_compression_lease=False)
            session["agent"].session_id = expected
            db.reopen_session(key)  # the stale parent stays writable, as after a resume

        with caplog.at_level(logging.WARNING, logger="tui_gateway.server"):
            server._append_model_switch_marker(session, model="model-b", provider="test-provider")

        assert "failed to persist model switch marker" not in caplog.text
        assert len(_markers(db, expected)) == 1, f"no durable marker under {expected}"
        if shape == "rotated_live":
            assert not _markers(db, key), "marker filed under the rotated-away parent"
        else:
            assert db.get_session(key) is not None
    finally:
        server._sessions.pop(sid, None)
        db.close()
