"""Fail-closed durable-session gates for normal Forge/TUI turns."""

from __future__ import annotations

import os
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.persistence import (
    SessionPersistenceError,
    persistence_error_from_exception,
)
from hermes_state import SessionDB


def _response(text: str, *, finish_reason: str = "stop", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        model="test-model",
    )


def _tool_call(call_id: str = "call-1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="web_search", arguments="{}"),
    )


def _agent(tmp_path, db, session_id: str):
    from run_agent import AIAgent

    isolated_home = tmp_path / "hermes-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    with (
        patch.dict(os.environ, {"HERMES_HOME": str(isolated_home)}, clear=False),
        patch("agent.agent_init.get_hermes_home", return_value=isolated_home),
        patch(
            "run_agent.get_tool_definitions",
            return_value=[
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "test tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="test-model",
            platform="tui",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=session_id,
        )
    agent.client = MagicMock()
    agent._session_json_enabled = False
    agent.logs_dir = isolated_home / "sessions"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._persistence_required = True
    return agent


class _RaisingDB:
    def __init__(self, error):
        self.error = error
        self.create_calls = 0

    def create_session(self, *_args, **_kwargs):
        self.create_calls += 1
        raise self.error


class _NoOpDB:
    def __init__(self):
        self.create_calls = 0

    def create_session(self, *_args, **_kwargs):
        self.create_calls += 1

    def get_session(self, _session_id):
        return None


def test_create_session_failure_rejects_before_provider_or_tool(tmp_path):
    db = _RaisingDB(sqlite3.OperationalError("database is locked; secret=hidden"))
    agent = _agent(tmp_path, db, "gate-create-error")
    agent._execute_tool_calls = MagicMock()

    result = agent.run_conversation("do not send this to a provider")

    assert result["failed"] is True
    assert result["failure_reason"] == "session_persistence_failed"
    assert agent.client.chat.completions.create.call_count == 0
    assert agent._execute_tool_calls.call_count == 0
    assert db.create_calls == 1
    assert "secret" not in result["error"]
    assert "database is locked" not in result["error"]


def test_unavailable_store_rejects_before_provider(tmp_path):
    agent = _agent(tmp_path, None, "gate-no-store")

    result = agent.run_conversation("the store is required")

    assert result["failure_reason"] == "session_persistence_failed"
    assert agent.client.chat.completions.create.call_count == 0
    assert "session store is unavailable" in result["error"]


def test_noop_creation_without_target_row_is_rejected(tmp_path):
    db = _NoOpDB()
    agent = _agent(tmp_path, db, "gate-noop-create")

    result = agent.run_conversation("the row must exist first")

    assert result["failure_reason"] == "session_persistence_failed"
    assert agent.client.chat.completions.create.call_count == 0
    assert "row was not created" in result["error"]


def test_successful_first_turn_resume_and_reopen_do_not_duplicate(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    agent = _agent(tmp_path, db, "gate-success")
    agent.client.chat.completions.create.return_value = _response("saved answer")

    first = agent.run_conversation("save this first")

    assert first["final_response"] == "saved answer"
    assert agent.client.chat.completions.create.call_count == 1
    assert db.get_session("gate-success")["id"] == "gate-success"
    assert [m["role"] for m in db.get_messages_as_conversation("gate-success")] == [
        "user",
        "assistant",
    ]
    db.close()

    reopened = SessionDB(db_path=db_path)
    try:
        old_history = reopened.get_messages_as_conversation("gate-success")
        resumed = _agent(tmp_path, reopened, "gate-success")
        resumed.client.chat.completions.create.return_value = _response("second answer")
        second = resumed.run_conversation("save this second", conversation_history=old_history)

        assert second["final_response"] == "second answer"
        durable = reopened.get_messages_as_conversation("gate-success")
        assert [(m["role"], m["content"]) for m in durable] == [
            ("user", "save this first"),
            ("assistant", "saved answer"),
            ("user", "save this second"),
            ("assistant", "second answer"),
        ]
    finally:
        reopened.close()


def test_initial_user_row_is_durable_before_preflight_compression(tmp_path):
    db = SessionDB(db_path=tmp_path / "preflight-order.db")
    try:
        agent = _agent(tmp_path, db, "gate-preflight-order")
        agent.compression_enabled = True
        agent.context_compressor.should_compress = MagicMock(return_value=True)
        agent.max_compression_attempts = 1

        def observe_compression(messages, system_message, **_kwargs):
            durable = db.get_messages_as_conversation("gate-preflight-order")
            assert [(row["role"], row["content"]) for row in durable] == [
                ("user", "persist before compression")
            ]
            return messages, system_message

        agent._compress_context = observe_compression
        agent.client.chat.completions.create.return_value = _response("compressed safely")

        with (
            patch("agent.turn_context._should_run_preflight_estimate", return_value=True),
            patch("agent.turn_context._preflight_request_tokens", return_value=999),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("persist before compression")

        assert result["final_response"] == "compressed safely"
    finally:
        db.close()


def test_incremental_failure_blocks_subsequent_tool_execution(tmp_path):
    db = SessionDB(db_path=tmp_path / "incremental.db")
    try:
        agent = _agent(tmp_path, db, "gate-incremental")
        agent.client.chat.completions.create.return_value = _response(
            "I will search",
            finish_reason="tool_calls",
            tool_calls=[_tool_call()],
        )
        executed = MagicMock()
        agent._execute_tool_calls = executed
        original_flush = agent._flush_messages_to_session_db
        calls = {"count": 0}

        def fail_after_initial_flush(messages, conversation_history=None):
            calls["count"] += 1
            if calls["count"] >= 3:
                return False
            return original_flush(messages, conversation_history)

        agent._flush_messages_to_session_db = fail_after_initial_flush
        result = agent.run_conversation("search, but only after the write")

        assert calls["count"] >= 3
        assert agent.client.chat.completions.create.call_count == 1
        executed.assert_not_called()
        assert result["failed"] is True
        assert result["turn_exit_reason"] == "session_persistence_failed"
        assert result["failure_reason"].startswith("session_persistence_failed:")
        assert [m["role"] for m in db.get_messages_as_conversation("gate-incremental")] == [
            "user",
        ]
    finally:
        db.close()


def test_noop_incremental_append_is_rejected_before_provider(tmp_path):
    real_db = SessionDB(db_path=tmp_path / "noop-append.db")

    class _NoOpAppendDB:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def append_messages_batch(self, **_kwargs):
            return 0

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    try:
        agent = _agent(
            tmp_path,
            _NoOpAppendDB(real_db),
            "gate-noop-append",
        )
        result = agent.run_conversation("the append must be proven")

        assert result["failure_reason"] == "session_persistence_failed"
        assert agent.client.chat.completions.create.call_count == 0
        assert real_db.get_messages_as_conversation("gate-noop-append") == []
    finally:
        real_db.close()


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (sqlite3.OperationalError("database is locked; secret=hidden"), "locked"),
        (sqlite3.OperationalError("attempt to write a readonly database"), "disk"),
        (sqlite3.IntegrityError("FOREIGN KEY constraint failed"), "foreign_key"),
        (sqlite3.IntegrityError("UNIQUE constraint failed: sessions.id"), "integrity"),
    ],
)
def test_persistence_diagnostics_are_sanitized_and_classified(exc, kind):
    error = persistence_error_from_exception(
        operation="session row",
        stage="create",
        session_id="diagnostic-session",
        exc=exc,
    )

    assert error.kind == kind
    diagnostic = error.diagnostic()
    assert diagnostic["operation"] == "session row"
    assert diagnostic["stage"] == "create"
    assert diagnostic["session_id"] == "diagnostic-session"
    assert diagnostic["exception_type"] == type(exc).__name__
    assert str(exc) not in error.user_message


def test_real_disposable_sqlite_faults_are_classified(tmp_path):
    db_path = tmp_path / "sqlite-faults.db"
    db = SessionDB(db_path=db_path)
    db.create_session("fault-session", source="tui", model="test-model")
    db.close()

    holder = sqlite3.connect(db_path, timeout=0)
    writer = sqlite3.connect(db_path, timeout=0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as locked:
            writer.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                ("locked", "fault-session"),
            )
            writer.commit()
        locked_error = persistence_error_from_exception(
            operation="transcript flush",
            stage="append",
            session_id="fault-session",
            exc=locked.value,
        )
        assert locked_error.kind == "locked"
        assert locked_error.sqlite_error_code is not None
    finally:
        writer.close()
        holder.rollback()
        holder.close()

    readonly = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro",
        uri=True,
    )
    try:
        with pytest.raises(sqlite3.OperationalError) as readonly_exc:
            readonly.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                ("readonly", "fault-session"),
            )
            readonly.commit()
        readonly_error = persistence_error_from_exception(
            operation="session row",
            stage="create",
            session_id="fault-session",
            exc=readonly_exc.value,
        )
        assert readonly_error.kind == "disk"
        assert readonly_error.sqlite_error_code is not None
    finally:
        readonly.close()

    integrity = sqlite3.connect(db_path)
    try:
        integrity.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError) as foreign_key:
            integrity.execute(
                "INSERT INTO sessions (id, source, parent_session_id, started_at) "
                "VALUES (?, ?, ?, ?)",
                ("invalid-child", "tui", "missing-parent", 0.0),
            )
            integrity.commit()
        foreign_key_error = persistence_error_from_exception(
            operation="session row",
            stage="create",
            session_id="invalid-child",
            exc=foreign_key.value,
        )
        assert foreign_key_error.kind == "foreign_key"
        assert foreign_key_error.sqlite_error_code is not None

        with pytest.raises(sqlite3.IntegrityError) as unique:
            integrity.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                ("fault-session", "tui", 0.0),
            )
            integrity.commit()
        unique_error = persistence_error_from_exception(
            operation="session row",
            stage="create",
            session_id="fault-session",
            exc=unique.value,
        )
        assert unique_error.kind == "integrity"
        assert unique_error.sqlite_error_code is not None
    finally:
        integrity.close()


def test_missing_gateway_session_key_is_contract_error(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: pytest.fail("store lookup must not run"))
    with pytest.raises(SessionPersistenceError) as raised:
        server._ensure_session_db_row({}, required=True)

    assert raised.value.kind == "missing_key"
    assert "durable session key is missing" in raised.value.user_message


def test_invalid_branch_parent_fails_without_placeholder(monkeypatch, tmp_path):
    from tui_gateway import server

    db = SessionDB(db_path=tmp_path / "branch.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    try:
        with pytest.raises(SessionPersistenceError) as raised:
            server._ensure_session_db_row(
                {
                    "session_key": "branch-child",
                    "parent_session_id": "missing-parent",
                },
                required=True,
            )
        assert raised.value.kind == "foreign_key"
        assert db.get_session("branch-child") is None
    finally:
        db.close()


def test_browser_request_surfaces_persistence_error_without_provider(monkeypatch):
    from tui_gateway import server

    class _ProviderAgent:
        def __init__(self):
            self.run_conversation = MagicMock()

    fake_agent = _ProviderAgent()
    session = {
        "agent": fake_agent,
        "session_key": "gateway-noop",
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
    }
    monkeypatch.setattr(server, "_get_db", lambda: _NoOpDB())
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_a, **_k: False)
    server._sessions["gateway-noop"] = session
    try:
        response = server.handle_request(
            {
                "id": "rpc-1",
                "method": "prompt.submit",
                "params": {"session_id": "gateway-noop", "text": "hello"},
            }
        )
    finally:
        server._sessions.pop("gateway-noop", None)

    assert response["id"] == "rpc-1"
    assert response["error"]["code"] == 5071
    assert "row was not created" in response["error"]["message"]
    assert response["error"]["data"]["persistence"]["kind"] == "no_op"
    fake_agent.run_conversation.assert_not_called()
