"""Trusted-cutover regressions for the Stage 4A S4A-06 remediation."""

import sqlite3
import threading
import time

import pytest

import hermes_state_schema
from hermes_state import SessionDB
from hermes_state_common import (
    SESSION_USAGE_DETAIL_BASELINE_KEY,
    SESSION_USAGE_DETAIL_COVERAGE_KEY,
    SESSION_USAGE_RECONCILIATION_KEY,
    parse_session_usage_reconciliation_marker,
)


def _marker(db):
    return parse_session_usage_reconciliation_marker(
        db._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (SESSION_USAGE_RECONCILIATION_KEY,),
        ).fetchone()[0]
    )


def _force_new_cutover(db, *, drop_table=False):
    db._conn.execute(
        "DELETE FROM state_meta WHERE key = ?",
        (SESSION_USAGE_RECONCILIATION_KEY,),
    )
    if drop_table:
        db._conn.execute("DROP TABLE session_usage_reconciliation_baseline")
    else:
        db._conn.execute("DELETE FROM session_usage_reconciliation_baseline")


def _record(db, session_id, tokens, *, model="m", provider="p", timestamp=None):
    db.update_token_counts(
        session_id,
        model=model,
        billing_provider=provider,
        input_tokens=tokens,
        api_call_count=1,
        _usage_timestamp=time.time() if timestamp is None else timestamp,
    )


def test_upgrade_does_not_absorb_existing_aggregate_detail_gap(tmp_path):
    """The independent-review reproduction must never become COMPLETE."""
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("reviewer", "cli")
    activation = time.time() - 10
    db._conn.execute(
        "UPDATE state_meta SET value = ? WHERE key IN (?, ?)",
        (
            repr(activation),
            SESSION_USAGE_DETAIL_COVERAGE_KEY,
            SESSION_USAGE_DETAIL_BASELINE_KEY,
        ),
    )
    db._conn.execute(
        "INSERT INTO session_model_usage "
        "(session_id, model, billing_provider, billing_base_url, billing_mode, "
        "task, api_call_count, input_tokens) "
        "VALUES ('reviewer', 'm', 'p', '', '', '', 3, 105)"
    )
    db._conn.execute(
        "INSERT INTO session_usage_events "
        "(session_id, recorded_at, model, billing_provider, api_call_count, "
        "input_tokens) VALUES ('reviewer', ?, 'm', 'p', 1, 1)",
        (activation + 1,),
    )
    db._conn.execute("DROP TABLE session_usage_activation_baseline")
    db._conn.execute("DROP TABLE session_usage_reconciliation_baseline")
    db._conn.execute(
        "DELETE FROM state_meta WHERE key IN (?, ?)",
        (SESSION_USAGE_DETAIL_BASELINE_KEY, SESSION_USAGE_RECONCILIATION_KEY),
    )
    db.close()

    upgraded = SessionDB(db_path=path)
    baseline = upgraded._conn.execute(
        "SELECT api_call_count, input_tokens "
        "FROM session_usage_reconciliation_baseline "
        "WHERE session_id = 'reviewer'"
    ).fetchone()
    marker = parse_session_usage_reconciliation_marker(
        upgraded._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (SESSION_USAGE_RECONCILIATION_KEY,),
        ).fetchone()[0]
    )
    result = upgraded.get_session_usage_detail(
        "reviewer", start=activation, end=time.time() + 1
    )

    assert tuple(baseline) == (3, 105)
    assert result["coverage"]["status"] != "COMPLETE"
    assert marker["event_id_high_water"] == 1
    zero = upgraded.get_session_usage_detail(
        "reviewer", start=marker["cutover_at"]
    )
    assert zero["coverage"]["status"] == "COMPLETE"
    assert zero["totals"]["api_call_count"] == 0
    assert zero["totals"]["input_tokens"] == 0

    upgraded.update_token_counts(
        "reviewer",
        model="m",
        billing_provider="p",
        input_tokens=5,
        api_call_count=1,
        _usage_timestamp=marker["cutover_at"],
    )
    post_cutover = upgraded.get_session_usage_detail(
        "reviewer", start=marker["cutover_at"]
    )
    assert post_cutover["coverage"]["status"] == "COMPLETE"
    assert post_cutover["totals"]["api_call_count"] == 1
    assert post_cutover["totals"]["input_tokens"] == 5
    upgraded.close()


def test_missing_post_cutover_detail_fails_closed(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    db._conn.execute(
        "INSERT INTO session_model_usage "
        "(session_id, model, billing_provider, billing_base_url, billing_mode, "
        "task, api_call_count, input_tokens) "
        "VALUES ('s', 'm', 'p', '', '', '', 3, 105)"
    )
    _force_new_cutover(db)
    db.close()

    db = SessionDB(db_path=path)
    cutover = _marker(db)["cutover_at"]
    _record(db, "s", 5, timestamp=cutover)
    db._conn.execute(
        "UPDATE session_model_usage SET api_call_count = api_call_count + 1, "
        "input_tokens = input_tokens + 5 WHERE session_id = 's'"
    )
    result = db.get_session_usage_detail("s", start=cutover)
    assert result["coverage"]["status"] == "PARTIAL"
    assert result["coverage"]["reason"] == "aggregate_detail_mismatch"
    assert result["totals"]["api_call_count"] == 1
    assert result["totals"]["input_tokens"] == 5
    db.close()


def test_missing_trusted_marker_never_rebaselines_live_accounting_gap(tmp_path):
    """Marker loss must not move the immutable trusted cutover forward."""
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    db._conn.execute(
        "INSERT INTO session_model_usage "
        "(session_id, model, billing_provider, billing_base_url, billing_mode, "
        "task, api_call_count, input_tokens) "
        "VALUES ('s', 'm', 'p', '', '', '', 3, 105)"
    )
    _force_new_cutover(db)
    db.close()

    db = SessionDB(db_path=path)
    original_marker = _marker(db)
    original_baseline = tuple(
        db._conn.execute(
            "SELECT api_call_count, input_tokens "
            "FROM session_usage_reconciliation_baseline "
            "WHERE session_id = 's'"
        ).fetchone()
    )
    _record(db, "s", 5, timestamp=original_marker["cutover_at"])
    db._conn.execute(
        "UPDATE session_model_usage SET api_call_count = api_call_count + 1, "
        "input_tokens = input_tokens + 5 WHERE session_id = 's'"
    )
    before_loss = db.get_session_usage_detail(
        "s", start=original_marker["cutover_at"]
    )
    assert original_baseline == (3, 105)
    assert original_marker["event_id_high_water"] == 0
    assert before_loss["coverage"]["status"] == "PARTIAL"
    assert before_loss["totals"]["api_call_count"] == 1
    assert before_loss["totals"]["input_tokens"] == 5

    db._conn.execute(
        "DELETE FROM state_meta WHERE key = ?",
        (SESSION_USAGE_RECONCILIATION_KEY,),
    )
    db.close()

    with pytest.raises(RuntimeError, match="trusted session usage cutover"):
        SessionDB(db_path=path)

    raw = sqlite3.connect(path)
    baseline_after = raw.execute(
        "SELECT api_call_count, input_tokens "
        "FROM session_usage_reconciliation_baseline WHERE session_id = 's'"
    ).fetchone()
    aggregate_after = raw.execute(
        "SELECT api_call_count, input_tokens FROM session_model_usage "
        "WHERE session_id = 's'"
    ).fetchone()
    detail_after = raw.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0) "
        "FROM session_usage_events WHERE session_id = 's'"
    ).fetchone()
    raw.close()

    assert baseline_after == (3, 105)
    assert aggregate_after == (5, 115)
    assert detail_after == (1, 5)


@pytest.mark.parametrize("old_calls,old_tokens", [(0, 0), (1, 1), (3, 105)])
def test_untrusted_old_detail_never_reduces_full_baseline(
    tmp_path, old_calls, old_tokens
):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    db._conn.execute(
        "INSERT INTO session_model_usage "
        "(session_id, model, billing_provider, billing_base_url, billing_mode, "
        "task, api_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, estimated_cost_usd, "
        "actual_cost_usd) VALUES "
        "('s', 'm', 'p', 'u', 'metered', '', 3, 105, 7, 6, 5, 4, 1.25, 0.75)"
    )
    if old_calls:
        db._conn.execute(
            "INSERT INTO session_usage_events "
            "(session_id, recorded_at, model, billing_provider, "
            "billing_base_url, billing_mode, api_call_count, input_tokens) "
            "VALUES ('s', ?, 'm', 'p', 'u', 'metered', ?, ?)",
            (time.time() - 1, old_calls, old_tokens),
        )
    _force_new_cutover(db)
    db.close()

    db = SessionDB(db_path=path)
    baseline = db._conn.execute(
        "SELECT api_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, estimated_cost_usd, "
        "actual_cost_usd FROM session_usage_reconciliation_baseline "
        "WHERE session_id = 's'"
    ).fetchone()
    assert tuple(baseline) == (3, 105, 7, 6, 5, 4, 1.25, 0.75)
    marker = _marker(db)
    assert marker["event_id_high_water"] == (1 if old_calls else 0)
    result = db.get_session_usage_detail("s", start=marker["cutover_at"])
    assert result["coverage"]["status"] == "COMPLETE"
    assert result["totals"]["api_call_count"] == 0
    assert result["totals"]["input_tokens"] == 0
    db.close()


def test_pre_and_crossing_cutover_windows_never_claim_complete(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    _record(db, "s", 9, timestamp=time.time() - 10)
    _force_new_cutover(db)
    db.close()

    db = SessionDB(db_path=path)
    cutover = _marker(db)["cutover_at"]
    pre = db.get_session_usage_detail(
        "s", start=cutover - 2, end=cutover
    )
    crossing = db.get_session_usage_detail(
        "s", start=cutover - 1, end=cutover + 1
    )
    assert pre["coverage"]["status"] == "UNAVAILABLE"
    assert crossing["coverage"]["status"] == "PARTIAL"
    assert crossing["totals"]["input_tokens"] == 0
    db.close()


def test_route_level_reconciliation_prevents_cross_route_cancellation(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    for model, provider, task, calls, tokens in (
        ("m1", "p1", "", 3, 30),
        ("m2", "p2", "aux", 4, 40),
    ):
        db._conn.execute(
            "INSERT INTO session_model_usage "
            "(session_id, model, billing_provider, billing_base_url, "
            "billing_mode, task, api_call_count, input_tokens) "
            "VALUES ('s', ?, ?, '', '', ?, ?, ?)",
            (model, provider, task, calls, tokens),
        )
    _force_new_cutover(db)
    db.close()

    db = SessionDB(db_path=path)
    cutover = _marker(db)["cutover_at"]
    baseline = db._conn.execute(
        "SELECT model, billing_provider, task, api_call_count, input_tokens "
        "FROM session_usage_reconciliation_baseline ORDER BY model"
    ).fetchall()
    assert [tuple(row) for row in baseline] == [
        ("m1", "p1", "", 3, 30),
        ("m2", "p2", "aux", 4, 40),
    ]
    db._conn.execute(
        "UPDATE session_model_usage SET api_call_count = api_call_count + 1, "
        "input_tokens = input_tokens + 5 WHERE session_id = 's' AND model = 'm1'"
    )
    db._conn.execute(
        "INSERT INTO session_usage_events "
        "(session_id, recorded_at, model, billing_provider, task, "
        "api_call_count, input_tokens) VALUES ('s', ?, 'm2', 'p2', 'aux', 1, 5)",
        (cutover,),
    )
    result = db.get_session_usage_detail("s", start=cutover)
    assert result["totals"]["input_tokens"] == 5
    assert result["coverage"]["status"] == "PARTIAL"
    assert result["coverage"]["reason"] == "aggregate_detail_mismatch"
    db.close()


def test_cutover_is_idempotent_and_new_sessions_need_no_baseline(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("existing", "cli")
    _record(db, "existing", 50)
    _force_new_cutover(db)
    db.close()

    first = SessionDB(db_path=path)
    first_marker = _marker(first)
    first_rows = [tuple(row) for row in first._conn.execute(
        "SELECT * FROM session_usage_reconciliation_baseline"
    ).fetchall()]
    first.close()
    second = SessionDB(db_path=path)
    assert _marker(second) == first_marker
    assert [tuple(row) for row in second._conn.execute(
        "SELECT * FROM session_usage_reconciliation_baseline"
    ).fetchall()] == first_rows
    second.create_session("new", "cli")
    _record(second, "new", 7, timestamp=first_marker["cutover_at"])
    assert second._conn.execute(
        "SELECT 1 FROM session_usage_reconciliation_baseline "
        "WHERE session_id = 'new'"
    ).fetchone() is None
    result = second.get_session_usage_detail(
        "new", start=first_marker["cutover_at"]
    )
    assert result["coverage"]["status"] == "COMPLETE"
    assert result["totals"]["input_tokens"] == 7
    second.close()


def test_multiple_sessions_and_mixed_era_compression_lineage(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    for session_id, tokens in (("parent", 20), ("other", 30)):
        db.create_session(session_id, "cli")
        _record(db, session_id, tokens)
    _force_new_cutover(db)
    db.close()

    db = SessionDB(db_path=path)
    cutover = _marker(db)["cutover_at"]
    _record(db, "parent", 2, timestamp=cutover)
    db.end_session("parent", "compression")
    db.create_session("child", "cli", parent_session_id="parent")
    _record(db, "child", 3, timestamp=cutover)
    _record(db, "other", 4, timestamp=cutover)
    lineage = db.get_session_usage_detail(
        "parent", scope="compression_lineage", start=cutover
    )
    other = db.get_session_usage_detail("other", start=cutover)
    assert lineage["coverage"]["status"] == "COMPLETE"
    assert lineage["session_ids"] == ["parent", "child"]
    assert lineage["totals"]["input_tokens"] == 5
    assert other["coverage"]["status"] == "COMPLETE"
    assert other["totals"]["input_tokens"] == 4
    db.close()


def test_cutover_transaction_rolls_back_baseline_and_marker(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    _record(db, "s", 12)
    _force_new_cutover(db)
    db._conn.execute(
        "CREATE TRIGGER fail_trusted_cutover BEFORE INSERT ON state_meta "
        f"WHEN NEW.key = '{SESSION_USAGE_RECONCILIATION_KEY}' "
        "BEGIN SELECT RAISE(ABORT, 'forced cutover rollback'); END"
    )
    db.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced cutover rollback"):
        SessionDB(db_path=path)
    raw = sqlite3.connect(path)
    assert raw.execute(
        "SELECT 1 FROM state_meta WHERE key = ?",
        (SESSION_USAGE_RECONCILIATION_KEY,),
    ).fetchone() is None
    assert raw.execute(
        "SELECT COUNT(*) FROM session_usage_reconciliation_baseline"
    ).fetchone()[0] == 0
    raw.execute("DROP TRIGGER fail_trusted_cutover")
    raw.commit()
    raw.close()
    repaired = SessionDB(db_path=path)
    assert repaired._conn.execute(
        "SELECT input_tokens FROM session_usage_reconciliation_baseline "
        "WHERE session_id = 's'"
    ).fetchone()[0] == 12
    repaired.close()


def test_cutover_write_lock_excludes_concurrent_usage_writer(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    _record(db, "s", 10)
    _force_new_cutover(db)
    db.close()

    snapshot_captured = threading.Event()
    release_cutover = threading.Event()
    original_digest = hermes_state_schema.session_usage_reconciliation_baseline_digest

    def blocking_digest(rows):
        snapshot_captured.set()
        assert release_cutover.wait(5)
        return original_digest(rows)

    monkeypatch.setattr(
        hermes_state_schema,
        "session_usage_reconciliation_baseline_digest",
        blocking_digest,
    )
    opened = []
    errors = []

    def open_for_cutover():
        try:
            opened.append(SessionDB(db_path=path))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=open_for_cutover)
    thread.start()
    assert snapshot_captured.wait(5)
    contender = sqlite3.connect(path, timeout=0, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        contender.execute("BEGIN IMMEDIATE")
    contender.close()
    release_cutover.set()
    thread.join(5)
    assert errors == []
    assert len(opened) == 1
    cutover_db = opened[0]
    marker = _marker(cutover_db)
    _record(cutover_db, "s", 5, timestamp=marker["cutover_at"])
    result = cutover_db.get_session_usage_detail(
        "s", start=marker["cutover_at"]
    )
    assert result["coverage"]["status"] == "COMPLETE"
    assert result["totals"]["input_tokens"] == 5
    cutover_db.close()


def test_incomplete_marker_fails_closed_and_baseline_without_marker_recuts(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    _record(db, "s", 8)
    _force_new_cutover(db)
    db.close()
    db = SessionDB(db_path=path)
    db._conn.execute(
        "DELETE FROM session_usage_reconciliation_baseline WHERE session_id = 's'"
    )
    db.close()
    with pytest.raises(RuntimeError, match="incomplete trusted"):
        SessionDB(db_path=path)

    raw = sqlite3.connect(path)
    raw.execute(
        "DELETE FROM state_meta WHERE key = ?",
        (SESSION_USAGE_RECONCILIATION_KEY,),
    )
    raw.commit()
    raw.close()
    repaired = SessionDB(db_path=path)
    assert repaired._conn.execute(
        "SELECT input_tokens FROM session_usage_reconciliation_baseline "
        "WHERE session_id = 's'"
    ).fetchone()[0] == 8
    repaired.close()


def test_partial_trusted_baseline_schema_repairs_before_cutover(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    _record(db, "s", 6)
    _force_new_cutover(db, drop_table=True)
    db._conn.execute(
        "CREATE TABLE session_usage_reconciliation_baseline "
        "(session_id TEXT PRIMARY KEY)"
    )
    db.close()

    first = SessionDB(db_path=path)
    columns = {
        row[1] for row in first._conn.execute(
            'PRAGMA table_info("session_usage_reconciliation_baseline")'
        )
    }
    assert {"model", "task", "input_tokens", "actual_cost_usd"} <= columns
    assert first._conn.execute(
        "SELECT input_tokens FROM session_usage_reconciliation_baseline "
        "WHERE session_id = 's'"
    ).fetchone()[0] == 6
    first_marker = _marker(first)
    first.close()
    second = SessionDB(db_path=path)
    assert _marker(second) == first_marker
    second.close()


def test_failed_queue_flush_after_cutover_remains_retryable(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", "cli")
    cutover = _marker(db)["cutover_at"]
    original = db.update_token_counts
    with db._token_queue_cond:
        db._token_queue.append(("s", {
            "model": "m",
            "billing_provider": "p",
            "input_tokens": 5,
            "api_call_count": 1,
            "_usage_timestamp": cutover,
        }))
    monkeypatch.setattr(
        db,
        "update_token_counts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("forced queue failure")
        ),
    )
    failed = db.get_session_usage_detail("s", start=cutover)
    assert failed["coverage"]["status"] == "UNAVAILABLE"
    assert failed["coverage"]["reason"] == "accounting_flush_failed"
    monkeypatch.setattr(db, "update_token_counts", original)
    assert db.flush_token_counts()
    recovered = db.get_session_usage_detail("s", start=cutover)
    assert recovered["coverage"]["status"] == "COMPLETE"
    assert recovered["totals"]["input_tokens"] == 5
    db.close()
