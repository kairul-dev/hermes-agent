"""Adversarial regressions for the Stage 4A independent-review findings."""

from contextlib import contextmanager
import json
import math
import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB
from hermes_state_common import (
    SESSION_USAGE_DETAIL_BASELINE_KEY,
    SESSION_USAGE_DETAIL_COVERAGE_KEY,
    SESSION_USAGE_RECONCILIATION_KEY,
    parse_session_usage_reconciliation_marker,
)


def _event(tokens=1, *, model="m", timestamp=None):
    return (
        "s",
        {
            "model": model,
            "billing_provider": "p",
            "input_tokens": tokens,
            "api_call_count": 1,
            "_usage_timestamp": time.time() if timestamp is None else timestamp,
        },
    )


def _append_without_writer(db, *events):
    with db._token_queue_cond:
        db._token_queue.extend(events)


def _raw_counts(db):
    aggregate = db._conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), "
        "COALESCE(SUM(api_call_count), 0) FROM session_model_usage "
        "WHERE session_id = 's'"
    ).fetchone()
    detail = db._conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0) "
        "FROM session_usage_events WHERE session_id = 's'"
    ).fetchone()
    return tuple(aggregate), tuple(detail)


def test_failed_queue_batch_is_preserved_and_retry_is_exact(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "test")
    original = db.update_token_counts
    _append_without_writer(db, _event(3), _event(4))
    monkeypatch.setattr(
        db, "update_token_counts", lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("forced failure")
        )
    )

    assert db.flush_token_counts() is False
    assert db.flush_token_counts() is False
    assert len(db._token_queue) == 1  # coalesced group retains two detail events
    assert _raw_counts(db) == ((0, 0), (0, 0))

    monkeypatch.setattr(db, "update_token_counts", original)
    assert db.flush_token_counts() is True
    assert _raw_counts(db) == ((7, 2), (2, 7))
    assert db.flush_token_counts() is True
    assert _raw_counts(db) == ((7, 2), (2, 7))
    db.close()


def test_partial_batch_retry_does_not_duplicate_committed_prefix(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "test")
    original = db.update_token_counts
    calls = 0

    def fail_second(session_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("second group failed")
        return original(session_id, **kwargs)

    _append_without_writer(db, _event(2, model="a"), _event(5, model="b"))
    monkeypatch.setattr(db, "update_token_counts", fail_second)
    assert db.flush_token_counts() is False
    assert _raw_counts(db) == ((2, 1), (1, 2))

    monkeypatch.setattr(db, "update_token_counts", original)
    assert db.flush_token_counts() is True
    assert _raw_counts(db) == ((7, 2), (2, 7))
    db.close()


def test_failed_flush_fails_usage_read_closed_and_close_is_observable(
    tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "test")
    original = db.update_token_counts
    _append_without_writer(db, _event(9))
    monkeypatch.setattr(
        db, "update_token_counts", lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("still unavailable")
        )
    )

    result = db.get_session_usage_detail("s")
    assert result["coverage"]["status"] == "UNAVAILABLE"
    assert result["coverage"]["reason"] == "accounting_flush_failed"
    assert result["totals"] is None
    with pytest.raises(RuntimeError, match="shutdown failed"):
        db.close()

    monkeypatch.setattr(db, "update_token_counts", original)
    assert db.flush_token_counts()
    assert _raw_counts(db) == ((9, 1), (1, 9))
    db.close()


def test_concurrent_producer_stays_after_failed_claimed_batch(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "test")
    original = db.update_token_counts
    entered = threading.Event()
    release = threading.Event()

    def fail_claimed(session_id, **kwargs):
        entered.set()
        assert release.wait(5)
        raise sqlite3.OperationalError("claimed batch failed")

    _append_without_writer(db, _event(1))
    monkeypatch.setattr(db, "update_token_counts", fail_claimed)
    result = []
    worker = threading.Thread(target=lambda: result.append(db.flush_token_counts()))
    worker.start()
    assert entered.wait(5)
    _append_without_writer(db, _event(2))
    release.set()
    worker.join(5)
    assert result == [False]
    assert [item[1]["input_tokens"] for item in db._token_queue] == [1, 2]

    monkeypatch.setattr(db, "update_token_counts", original)
    assert db.flush_token_counts()
    assert _raw_counts(db) == ((3, 2), (2, 3))
    db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_tokens", -1),
        ("output_tokens", -1),
        ("cache_read_tokens", -1),
        ("cache_write_tokens", -1),
        ("reasoning_tokens", -1),
        ("api_call_count", -1),
        ("api_call_count", 1.5),
        ("estimated_cost_usd", math.nan),
        ("estimated_cost_usd", math.inf),
        ("estimated_cost_usd", -math.inf),
    ],
)
def test_invalid_usage_values_are_rejected_before_any_write(tmp_path, field, value):
    db = SessionDB(db_path=tmp_path / "state.db")
    with pytest.raises(ValueError):
        db.update_token_counts("s", **{field: value})
    assert db.get_session("s") is None
    db.close()


def test_malformed_persisted_ledger_row_fails_closed(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "test")
    db._conn.execute("PRAGMA ignore_check_constraints = ON")
    db._conn.execute(
        "INSERT INTO session_usage_events "
        "(session_id, recorded_at, input_tokens) VALUES ('s', 1, -5)"
    )
    db._conn.execute("PRAGMA ignore_check_constraints = OFF")

    result = db.get_session_usage_detail("s")
    assert result["coverage"]["status"] == "UNAVAILABLE"
    assert result["coverage"]["reason"] == "usage_storage_integrity_failure"
    assert result["totals"] is None
    db.close()


def _record(db, session_id, tokens=1, *, timestamp=None, model="m"):
    db.update_token_counts(
        session_id,
        model=model,
        billing_provider="p",
        billing_base_url="https://private.invalid/v1",
        input_tokens=tokens,
        api_call_count=1,
        _usage_timestamp=time.time() if timestamp is None else timestamp,
    )


def _compress(db, parent, child, *, model_config=None, source="cli"):
    db.end_session(parent, "compression")
    db.create_session(
        child,
        source,
        parent_session_id=parent,
        model_config=model_config,
    )


def test_branch_root_includes_only_its_compression_successors(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    db.create_session(
        "branch",
        "cli",
        parent_session_id="parent",
        model_config={"_branched_from": "parent"},
    )
    _record(db, "branch", 2)
    marker = {"_branched_from": "parent"}
    _compress(db, "branch", "branch-2", model_config=marker)
    _record(db, "branch-2", 4)
    _compress(db, "branch-2", "branch-3", model_config=marker)
    _record(db, "branch-3", 8)
    db.create_session(
        "sibling",
        "cli",
        parent_session_id="parent",
        model_config={"_branched_from": "parent"},
    )
    _record(db, "sibling", 100)

    for selected in ("branch", "branch-2", "branch-3"):
        result = db.get_session_usage_detail(
            selected, scope="compression_lineage"
        )
        assert result["coverage"]["status"] == "COMPLETE"
        assert result["session_ids"] == ["branch", "branch-2", "branch-3"]
        assert result["totals"]["input_tokens"] == 14
    db.close()


@pytest.mark.parametrize("model_config", ["{", "[]", '{"_branched_from":"x"}'])
def test_malformed_or_conflicting_lineage_fails_closed(tmp_path, model_config):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    db.end_session("parent", "compression")
    db.create_session("child", "cli", parent_session_id="parent")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'child'",
        (model_config,),
    )
    result = db.get_session_usage_detail("parent", scope="compression_lineage")
    assert result["coverage"]["status"] == "UNAVAILABLE"
    assert result["totals"] is None
    db.close()


def test_conflicting_successors_cycle_self_cycle_and_dangling_fail_closed(tmp_path):
    cases = []

    def conflicting(db):
        db.create_session("root", "cli")
        db.end_session("root", "compression")
        db.create_session("a", "cli", parent_session_id="root")
        db.create_session("b", "cli", parent_session_id="root")

    cases.append(conflicting)

    def cycle(db):
        db.create_session("root", "cli")
        db.create_session("other", "cli", parent_session_id="root")
        db._conn.execute(
            "UPDATE sessions SET parent_session_id='other', "
            "end_reason='compression' WHERE id='root'"
        )
        db._conn.execute(
            "UPDATE sessions SET end_reason='compression' WHERE id='other'"
        )

    cases.append(cycle)

    def self_cycle(db):
        db.create_session("root", "cli")
        db._conn.execute(
            "UPDATE sessions SET parent_session_id='root', "
            "end_reason='compression' WHERE id='root'"
        )

    cases.append(self_cycle)

    def dangling(db):
        db._conn.execute("PRAGMA foreign_keys=OFF")
        db.create_session("root", "cli")
        db._conn.execute(
            "UPDATE sessions SET parent_session_id='missing' WHERE id='root'"
        )

    cases.append(dangling)

    for index, setup in enumerate(cases):
        db = SessionDB(db_path=tmp_path / f"case-{index}.db")
        setup(db)
        result = db.get_session_usage_detail("root", scope="compression_lineage")
        assert result["coverage"]["status"] == "UNAVAILABLE"
        assert result["totals"] is None
        db.close()


def test_detail_events_are_capped_stable_and_redacted(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("events", "cli")
    timestamp = time.time()
    for _ in range(150):
        _record(db, "events", timestamp=timestamp)

    first = db.get_session_usage_detail("events")
    second = db.get_session_usage_detail("events")
    assert len(first["events"]) == 100
    assert first["events_truncated"] is True
    assert first["event_limit"] == 100
    assert first["totals"]["input_tokens"] == 150
    assert first["events"] == second["events"]
    ids = [event["event_id"] for event in first["events"]]
    assert ids == sorted(ids)
    serialized = json.dumps(first)
    for private in ("billing_base_url", "cost_source", "private.invalid"):
        assert private not in serialized
    db.close()


def test_upgrade_baseline_allows_exact_post_activation_window(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("historical", "cli")
    db._conn.execute(
        "INSERT INTO session_model_usage "
        "(session_id, model, billing_provider, billing_base_url, billing_mode, "
        "task, api_call_count, input_tokens) "
        "VALUES ('historical', 'old', 'p', '', '', '', 1, 99)"
    )
    db._conn.execute(
        "DELETE FROM state_meta WHERE key IN (?, ?, ?)",
        (
            SESSION_USAGE_DETAIL_COVERAGE_KEY,
            SESSION_USAGE_DETAIL_BASELINE_KEY,
            SESSION_USAGE_RECONCILIATION_KEY,
        ),
    )
    db.close()

    db = SessionDB(db_path=path)
    activation = parse_session_usage_reconciliation_marker(db._conn.execute(
        "SELECT value FROM state_meta WHERE key = ?",
        (SESSION_USAGE_RECONCILIATION_KEY,),
    ).fetchone()[0])["cutover_at"]
    _record(db, "historical", 1, timestamp=activation)

    after = db.get_session_usage_detail(
        "historical", start=activation
    )
    crossing = db.get_session_usage_detail(
        "historical", start=activation - 1
    )
    assert after["coverage"]["status"] == "COMPLETE"
    assert after["totals"]["input_tokens"] == 1
    assert crossing["coverage"]["status"] == "PARTIAL"
    assert crossing["totals"]["input_tokens"] == 1
    db.close()


def test_usage_read_uses_one_sqlite_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    reader = SessionDB(db_path=path)
    reader.create_session("snap", "cli")
    _record(reader, "snap", 1)
    writer = SessionDB(db_path=path)
    reader._conn.execute("PRAGMA journal_mode=WAL")
    writer._conn.execute("PRAGMA journal_mode=WAL")
    reader._wal_active = True
    writer._wal_active = True
    original = reader._usage_read_snapshot

    @contextmanager
    def interleaved_snapshot():
        with original() as read_at:
            _record(writer, "snap", 2, timestamp=read_at - 0.001)
            yield read_at

    monkeypatch.setattr(reader, "_usage_read_snapshot", interleaved_snapshot)
    during = reader.get_session_usage_detail("snap")
    monkeypatch.setattr(reader, "_usage_read_snapshot", original)
    settled = reader.get_session_usage_detail("snap")
    assert during["coverage"]["status"] == "COMPLETE"
    assert during["totals"]["input_tokens"] == 1
    assert len(during["events"]) == 1
    assert settled["totals"]["input_tokens"] == 3
    assert len(settled["events"]) == 2
    writer.close()
    reader.close()


def test_partial_detail_table_repairs_before_index_creation(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE session_usage_events")
    conn.execute(
        "CREATE TABLE session_usage_events "
        "(id INTEGER PRIMARY KEY, session_id TEXT)"
    )
    conn.execute(
        "DELETE FROM state_meta WHERE key IN (?, ?)",
        (SESSION_USAGE_DETAIL_COVERAGE_KEY, SESSION_USAGE_DETAIL_BASELINE_KEY),
    )
    conn.commit()
    conn.close()

    repaired = SessionDB(db_path=path)
    columns = {
        row[1] for row in repaired._conn.execute(
            'PRAGMA table_info("session_usage_events")'
        )
    }
    assert {"recorded_at", "input_tokens", "actual_cost_usd"} <= columns
    assert repaired._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_session_usage_events_session_time'"
    ).fetchone()
    repaired.close()


def test_partial_activation_metadata_fails_before_writable_use(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE state_meta SET value = '123' WHERE key = ?",
        (SESSION_USAGE_RECONCILIATION_KEY,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="trusted session usage cutover"):
        SessionDB(db_path=path)


def test_partial_activation_baseline_table_repairs_idempotently(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE session_usage_activation_baseline")
    conn.execute(
        "CREATE TABLE session_usage_activation_baseline "
        "(session_id TEXT PRIMARY KEY)"
    )
    conn.commit()
    conn.close()

    first = SessionDB(db_path=path)
    first.close()
    second = SessionDB(db_path=path)
    columns = {
        row[1] for row in second._conn.execute(
            'PRAGMA table_info("session_usage_activation_baseline")'
        )
    }
    assert {"input_tokens", "actual_cost_usd"} <= columns
    second.close()


def test_future_start_with_omitted_end_is_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "cli")
    with pytest.raises(ValueError, match="synchronized effective end"):
        db.get_session_usage_detail("s", start=9e15)
    db.close()
