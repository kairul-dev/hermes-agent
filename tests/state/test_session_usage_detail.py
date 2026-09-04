"""Forward-only detailed session usage service contracts."""

import sqlite3
import time

import pytest

from hermes_state import SessionDB
from hermes_state_common import (
    SESSION_USAGE_DETAIL_BASELINE_KEY,
    SESSION_USAGE_DETAIL_COVERAGE_KEY,
)


def _set_coverage_start(db: SessionDB, value: float) -> None:
    db._conn.execute(
        "UPDATE state_meta SET value = ? WHERE key = ?",
        (repr(value), SESSION_USAGE_DETAIL_COVERAGE_KEY),
    )
    db._conn.execute(
        "UPDATE state_meta SET value = ? WHERE key = ?",
        (repr(value), SESSION_USAGE_DETAIL_BASELINE_KEY),
    )
    db._conn.execute("DELETE FROM session_usage_activation_baseline")
    db._conn.execute(
        """INSERT INTO session_usage_activation_baseline
               (session_id, api_call_count, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, reasoning_tokens,
                estimated_cost_usd, actual_cost_usd)
           SELECT session_id, SUM(api_call_count), SUM(input_tokens),
                  SUM(output_tokens), SUM(cache_read_tokens),
                  SUM(cache_write_tokens), SUM(reasoning_tokens),
                  SUM(estimated_cost_usd), SUM(actual_cost_usd)
           FROM session_model_usage GROUP BY session_id"""
    )


def _record(
    db: SessionDB,
    session_id: str,
    *,
    timestamp: float,
    model: str = "model-a",
    provider: str = "provider-a",
    **usage,
) -> None:
    db.update_token_counts(
        session_id,
        model=model,
        billing_provider=provider,
        billing_base_url="https://private-billing.invalid/v1",
        billing_mode="metered",
        api_call_count=usage.pop("api_call_count", 1),
        _usage_timestamp=timestamp,
        **usage,
    )


def test_fresh_schema_has_forward_only_ledger_and_marker(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='session_usage_events'"
        ).fetchone()
        marker = db._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (SESSION_USAGE_DETAIL_COVERAGE_KEY,),
        ).fetchone()
        assert float(marker[0]) > 0
    finally:
        db.close()


def test_legacy_store_self_heals_without_backfill(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("legacy", "cli")
    db.close()

    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE session_usage_events")
    conn.execute(
        "DELETE FROM state_meta WHERE key = ?",
        (SESSION_USAGE_DETAIL_COVERAGE_KEY,),
    )
    conn.commit()
    conn.close()

    healed = SessionDB(db_path=path)
    try:
        assert healed._conn.execute(
            "SELECT COUNT(*) FROM session_usage_events"
        ).fetchone()[0] == 0
        assert healed._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (SESSION_USAGE_DETAIL_COVERAGE_KEY,),
        ).fetchone()
    finally:
        healed.close()


def test_exact_routes_preserve_all_dimensions_and_costs(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", "cli", model="model-a")
        now = time.time()
        _record(
            db,
            "s1",
            timestamp=now,
            input_tokens=101,
            output_tokens=23,
            cache_read_tokens=11,
            cache_write_tokens=7,
            reasoning_tokens=5,
            estimated_cost_usd=0.12,
            actual_cost_usd=0.10,
        )
        db.record_auxiliary_usage(
            "s1",
            "vision",
            model="vision-model",
            billing_provider="vision-provider",
            billing_base_url="https://private-vision.invalid/v1",
            input_tokens=9,
            output_tokens=3,
            cache_read_tokens=2,
            cache_write_tokens=1,
            reasoning_tokens=1,
            estimated_cost_usd=0.02,
        )

        result = db.get_session_usage_detail("s1")

        assert result["coverage"]["status"] == "COMPLETE"
        assert result["totals"] == {
            "api_call_count": 2,
            "input_tokens": 110,
            "output_tokens": 26,
            "cache_read_tokens": 13,
            "cache_write_tokens": 8,
            "reasoning_tokens": 6,
            "estimated_cost_usd": pytest.approx(0.14),
            "actual_cost_usd": pytest.approx(0.10),
        }
        assert [(row["task"], row["model"]) for row in result["routes"]] == [
            ("", "model-a"),
            ("vision", "vision-model"),
        ]
        assert result["routes"][0]["first_seen"] == pytest.approx(now)
        assert "billing_base_url" not in str(result)
        # Reasoning is its own dimension and is not added onto output.
        assert result["totals"]["output_tokens"] == 26
    finally:
        db.close()


def test_model_and_provider_switch_produces_multiple_route_rows(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("switch", "cli")
        now = time.time()
        _record(db, "switch", timestamp=now, input_tokens=10)
        _record(
            db,
            "switch",
            timestamp=now + 0.001,
            model="model-b",
            provider="provider-b",
            input_tokens=20,
        )

        result = db.get_session_usage_detail("switch")

        assert [
            (row["model"], row["billing_provider"])
            for row in result["routes"]
        ] == [("model-a", "provider-a"), ("model-b", "provider-b")]
        assert result["totals"]["input_tokens"] == 30
    finally:
        db.close()


def test_route_output_is_capped_without_truncating_totals(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("bounded", "cli")
        now = time.time()
        for index in range(101):
            _record(
                db,
                "bounded",
                timestamp=now + index / 1000,
                model=f"model-{index:03d}",
                input_tokens=1,
            )

        result = db.get_session_usage_detail("bounded")

        assert len(result["routes"]) == 100
        assert result["routes_truncated"] is True
        assert result["totals"]["input_tokens"] == 101
    finally:
        db.close()


def test_window_is_start_inclusive_end_exclusive(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        base = time.time() - 100
        _set_coverage_start(db, base - 10)
        db.create_session("window", "cli")
        _record(db, "window", timestamp=base, input_tokens=1)
        _record(db, "window", timestamp=base + 10, input_tokens=2)
        _record(db, "window", timestamp=base + 20, input_tokens=4)

        result = db.get_session_usage_detail(
            "window", start=base + 10, end=base + 20
        )

        assert result["coverage"]["status"] == "COMPLETE"
        assert result["totals"]["input_tokens"] == 2
        assert result["routes"][0]["first_seen"] == pytest.approx(base + 10)
        assert result["routes"][0]["last_seen"] == pytest.approx(base + 10)
        assert result["window"]["start_inclusive"] is True
        assert result["window"]["end_exclusive"] is True
    finally:
        db.close()


def test_valid_zero_usage_is_complete_not_unavailable(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("zero", "cli")
        result = db.get_session_usage_detail("zero")

        assert result["coverage"] == {
            **result["coverage"],
            "status": "COMPLETE",
            "reason": "exact_detail_available_no_usage",
        }
        assert result["routes"] == []
        assert result["totals"] is not None
        assert all(value == 0 for value in result["totals"].values())
    finally:
        db.close()


def test_missing_session_returns_none(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db.get_session_usage_detail("missing") is None
    finally:
        db.close()


def test_historical_aggregate_is_partial_and_never_mixed_into_exact_totals(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("historical", "cli", model="old-model")
        db._conn.execute(
            "UPDATE sessions SET started_at = 10 WHERE id = 'historical'"
        )
        db._conn.execute(
            """INSERT INTO session_model_usage (
                   session_id, model, billing_provider, billing_base_url,
                   billing_mode, task, api_call_count, input_tokens
               ) VALUES ('historical', 'old-model', 'provider', '', '', '', 1, 99)"""
        )
        _set_coverage_start(db, 100)

        result = db.get_session_usage_detail(
            "historical", start=10, end=200
        )

        assert result["coverage"]["status"] == "PARTIAL"
        assert result["coverage"]["reason"] == "historical_aggregate_only"
        assert result["coverage"]["exact_start"] == 100
        assert result["totals"]["input_tokens"] == 0
        assert result["routes"] == []
    finally:
        db.close()


def test_unavailable_detail_table_does_not_fabricate_zero(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("legacy", "cli")
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE session_usage_events")
    conn.commit()
    conn.close()

    readonly = SessionDB(db_path=path, read_only=True)
    try:
        result = readonly.get_session_usage_detail("legacy")
        assert result["coverage"]["status"] == "UNAVAILABLE"
        assert result["coverage"]["reason"] == "detail_table_unavailable"
        assert result["totals"] is None
    finally:
        readonly.close()


def test_incompatible_detail_schema_fails_closed(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("malformed", "cli")
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE session_usage_events")
    conn.execute(
        "CREATE TABLE session_usage_events (id INTEGER PRIMARY KEY, session_id TEXT)"
    )
    conn.commit()
    conn.close()

    readonly = SessionDB(db_path=path, read_only=True)
    try:
        result = readonly.get_session_usage_detail("malformed")
        assert result["coverage"]["status"] == "UNAVAILABLE"
        assert result["coverage"]["reason"] == "detail_schema_incompatible"
        assert result["totals"] is None
    finally:
        readonly.close()


def test_async_accounting_is_visible_after_service_sync_point(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("queued", "cli")
        db.queue_token_counts(
            "queued",
            model="model-a",
            billing_provider="provider-a",
            api_call_count=1,
            input_tokens=3,
        )
        db.queue_token_counts(
            "queued",
            model="model-a",
            billing_provider="provider-a",
            api_call_count=1,
            input_tokens=7,
        )

        result = db.get_session_usage_detail("queued")

        assert result["totals"]["input_tokens"] == 10
        assert result["totals"]["api_call_count"] == 2
        rows = db._conn.execute(
            "SELECT COUNT(*) FROM session_usage_events WHERE session_id='queued'"
        ).fetchone()[0]
        # Aggregate coalescing remains allowed, but exact event timestamps are
        # not collapsed into one synthetic detail row.
        assert rows == 2
    finally:
        db.close()


def test_physical_and_compression_lineage_scopes(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", "cli")
        _record(db, "parent", timestamp=time.time(), input_tokens=4)
        db.end_session("parent", "compression")
        db.create_session("child", "cli", parent_session_id="parent")
        _record(db, "child", timestamp=time.time(), input_tokens=6)

        physical = db.get_session_usage_detail("child", scope="physical")
        lineage = db.get_session_usage_detail(
            "child", scope="compression_lineage"
        )

        assert physical["session_ids"] == ["child"]
        assert physical["totals"]["input_tokens"] == 6
        assert lineage["session_ids"] == ["parent", "child"]
        assert lineage["totals"]["input_tokens"] == 10
    finally:
        db.close()


def test_ambiguous_compression_lineage_fails_closed(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", "cli")
        db.end_session("parent", "compression")
        db.create_session("child-a", "cli", parent_session_id="parent")
        db.create_session("child-b", "cli", parent_session_id="parent")

        result = db.get_session_usage_detail(
            "parent", scope="compression_lineage"
        )

        assert result["coverage"]["status"] == "UNAVAILABLE"
        assert result["coverage"]["reason"] == "ambiguous_compression_lineage"
        assert result["totals"] is None
    finally:
        db.close()


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"scope": "forks"}, "scope must be one of"),
        ({"start": float("nan")}, "finite non-negative"),
        ({"start": 20, "end": 10}, "end must be greater"),
    ],
)
def test_malformed_service_request_is_rejected(tmp_path, kwargs, message):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "cli")
        with pytest.raises(ValueError, match=message):
            db.get_session_usage_detail("s", **kwargs)
    finally:
        db.close()
