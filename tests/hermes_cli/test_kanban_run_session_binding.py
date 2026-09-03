"""Durable, claim-fenced Kanban run -> Hermes session attribution."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_state import SessionDB
from run_agent import AIAgent, _gateway_origin_json


@pytest.fixture
def claimed_run(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path)
    task_id = kb.create_task(conn, title="bind me", assignee="default")
    claimed = kb.claim_task(conn, task_id, claimer="claim-one")
    assert claimed is not None
    run_id = kb.get_task(conn, task_id).current_run_id
    assert run_id is not None
    yield conn, db_path, home, task_id, run_id, "claim-one"
    conn.close()


def test_fresh_schema_and_legacy_migration_add_worker_session_id(tmp_path):
    fresh = tmp_path / "fresh.db"
    kb.init_db(db_path=fresh)
    conn = sqlite3.connect(fresh)
    try:
        fresh_columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
    finally:
        conn.close()
    assert "worker_session_id" in fresh_columns

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        """
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL, profile TEXT, step_key TEXT,
            status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,
            worker_pid INTEGER, max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER, started_at INTEGER NOT NULL,
            ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    kb.init_db(db_path=legacy)
    conn = sqlite3.connect(legacy)
    try:
        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
    finally:
        conn.close()
    assert "worker_session_id" in legacy_columns


def test_first_binding_is_idempotent_and_conflicts_fail_closed(claimed_run):
    conn, _db_path, _home, task_id, run_id, claim_lock = claimed_run
    first = kb.bind_worker_session(
        conn,
        task_id=task_id,
        run_id=run_id,
        claim_lock=claim_lock,
        session_id="session-one",
    )
    repeated = kb.bind_worker_session(
        conn,
        task_id=task_id,
        run_id=run_id,
        claim_lock=claim_lock,
        session_id="session-one",
    )
    assert first.worker_session_id == repeated.worker_session_id == "session-one"

    with pytest.raises(kb.RunSessionBindingConflictError):
        kb.bind_worker_session(
            conn,
            task_id=task_id,
            run_id=run_id,
            claim_lock=claim_lock,
            session_id="session-two",
        )
    assert kb.get_run(conn, run_id).worker_session_id == "session-one"


def test_worker_hook_binds_after_session_row_is_durable(claimed_run, monkeypatch):
    conn, _db_path, _home, task_id, run_id, claim_lock = claimed_run
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim_lock)
    agent = SimpleNamespace(
        platform=None,
        session_id="session-from-worker",
        _session_db_created=True,
        _kanban_worker_session_bound=False,
    )

    AIAgent._bind_kanban_worker_session(agent)

    assert agent._kanban_worker_session_bound is True
    assert kb.get_run(conn, run_id).worker_session_id == "session-from-worker"


def test_retry_attempts_bind_independently_and_completion_preserves_binding(claimed_run):
    conn, _db_path, _home, task_id, first_run_id, first_lock = claimed_run
    kb.bind_worker_session(
        conn,
        task_id=task_id,
        run_id=first_run_id,
        claim_lock=first_lock,
        session_id="session-first",
    )
    assert kb.reclaim_task(conn, task_id, signal_fn=lambda *_args: None)
    second = kb.claim_task(conn, task_id, claimer="claim-two")
    assert second is not None
    second_run_id = kb.get_task(conn, task_id).current_run_id
    assert second_run_id is not None and second_run_id != first_run_id
    kb.bind_worker_session(
        conn,
        task_id=task_id,
        run_id=second_run_id,
        claim_lock="claim-two",
        session_id="session-second",
    )
    kb.complete_task(conn, task_id, result="done", summary="complete")

    runs = {run.id: run for run in kb.list_runs(conn, task_id)}
    assert runs[first_run_id].worker_session_id == "session-first"
    assert runs[second_run_id].worker_session_id == "session-second"
    assert runs[second_run_id].ended_at is not None


def test_stale_reclaimed_worker_cannot_bind(claimed_run):
    conn, _db_path, _home, task_id, run_id, claim_lock = claimed_run
    assert kb.reclaim_task(conn, task_id, signal_fn=lambda *_args: None)

    with pytest.raises(kb.RunSessionBindingError):
        kb.bind_worker_session(
            conn,
            task_id=task_id,
            run_id=run_id,
            claim_lock=claim_lock,
            session_id="too-late",
        )
    assert kb.get_run(conn, run_id).worker_session_id is None


def test_concurrent_different_sessions_cannot_overwrite(claimed_run):
    conn, db_path, _home, task_id, run_id, claim_lock = claimed_run
    barrier = threading.Barrier(2)
    outcomes = []

    def bind(session_id):
        worker_conn = kb.connect(db_path)
        try:
            barrier.wait()
            try:
                row = kb.bind_worker_session(
                    worker_conn,
                    task_id=task_id,
                    run_id=run_id,
                    claim_lock=claim_lock,
                    session_id=session_id,
                )
                outcomes.append(("bound", row.worker_session_id))
            except kb.RunSessionBindingConflictError:
                outcomes.append(("conflict", session_id))
        finally:
            worker_conn.close()

    threads = [
        threading.Thread(target=bind, args=("session-a",)),
        threading.Thread(target=bind, args=("session-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kind for kind, _value in outcomes) == ["bound", "conflict"]
    assert kb.get_run(conn, run_id).worker_session_id in {"session-a", "session-b"}


def test_run_close_recovers_session_created_before_binding(claimed_run):
    conn, db_path, home, task_id, run_id, claim_lock = claimed_run
    marker = json.dumps(
        {
            "kanban_run": {
                "board": "default",
                "db_fingerprint": hashlib.sha256(
                    str(db_path.resolve()).encode("utf-8")
                ).hexdigest(),
                "task_id": task_id,
                "run_id": str(run_id),
                "claim_fingerprint": hashlib.sha256(
                    claim_lock.encode("utf-8")
                ).hexdigest(),
            }
        }
    )
    session_db = SessionDB(db_path=home / "state.db")
    session_db.create_session(
        session_id="session-in-crash-window",
        source="kanban",
        origin_json=marker,
    )
    session_db.close()

    assert kb.reclaim_task(conn, task_id, signal_fn=lambda *_args: None)
    assert kb.get_run(conn, run_id).worker_session_id == "session-in-crash-window"


def test_session_insert_marker_contains_exact_claim_identity(claimed_run, monkeypatch):
    _conn, db_path, _home, task_id, run_id, claim_lock = claimed_run
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim_lock)
    payload = json.loads(_gateway_origin_json(SimpleNamespace(platform=None)))

    assert payload["kanban_run"] == {
        "board": "default",
        "db_fingerprint": hashlib.sha256(
            str(db_path.resolve()).encode("utf-8")
        ).hexdigest(),
        "task_id": task_id,
        "run_id": str(run_id),
        "claim_fingerprint": hashlib.sha256(
            claim_lock.encode("utf-8")
        ).hexdigest(),
    }
