"""Exercise EXPLAIN on actual production projections over a 20,000-row ledger."""

import sqlite3
import time

from hermes_state import SessionDB


def test_production_usage_projection_uses_session_time_index(tmp_path, record_property):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        for sid in ("target", "other"):
            db.create_session(sid, "cli")
        timestamp = time.time()
        db._conn.execute("BEGIN IMMEDIATE")
        db._conn.executemany(
            "INSERT INTO session_usage_events "
            "(session_id, recorded_at, model, billing_provider, api_call_count, input_tokens) "
            "VALUES (?, ?, 'm', 'p', 1, 1)",
            [("target" if i < 100 else "other", timestamp) for i in range(20000)],
        )
        db._conn.executemany(
            "INSERT INTO session_model_usage "
            "(session_id, model, billing_provider, api_call_count, input_tokens) "
            "VALUES (?, 'm', 'p', ?, ?)",
            [("target", 100, 100), ("other", 19900, 19900)],
        )
        db._conn.commit()
    finally:
        db.close()
    db = SessionDB(db_path=path, read_only=True)
    try:
        statements = []
        db._conn.set_trace_callback(statements.append)
        result = db.get_session_usage_detail("target", start=timestamp)
        db._conn.set_trace_callback(None)
        assert result["coverage"]["status"] == "COMPLETE"
        assert result["totals"]["input_tokens"] == 100
        assert db._conn.execute("SELECT COUNT(*) FROM session_usage_events").fetchone()[0] == 20000
        projection = next(sql for sql in statements if "SELECT id AS event_id" in sql)
        plan = [row[3] for row in db._conn.execute("EXPLAIN QUERY PLAN " + projection)]
        record_property("sqlite_version", sqlite3.sqlite_version)
        record_property("event_rows", 20000)
        record_property("production_event_projection_plan", " | ".join(plan))
        assert any("SEARCH session_usage_events" in step
                   and "idx_session_usage_events_session_time" in step
                   and "recorded_at>?" in step and "recorded_at<?" in step for step in plan), plan
        assert not any("SCAN session_usage_events" in step for step in plan), plan
    finally:
        db.close()
