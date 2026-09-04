"""The established corruption matrix must have one nonmutating HTTP contract."""

import json
import sqlite3

import pytest

from tests.hermes_cli.test_session_usage_api import (
    client as client,
    profile_homes as profile_homes,
)
from tests.state.test_stage4a_s4a06_cutover import (
    _create_cutover_with_live_gap,
    _damage_cutover,
    _damage_epoch,
)


@pytest.mark.parametrize("damage", [
    "marker_only", "baseline_only", "high_water_only", "integrity_binding_only",
    "one_baseline_route", "multiple_baseline_routes", "marker_and_high_water",
    "marker_and_integrity_binding", "baseline_and_marker", "altered_baseline_value",
    "altered_high_water", "altered_integrity_hash", "baseline_table_recreated_empty",
    "epoch_row_missing", "epoch_table_missing", "altered_generation_number",
    "active_schema_shell_missing_state", "inconsistent_generation_integrity",
    "all_epoch_and_cutover_artifacts_removed", "schema_generation_mismatch",
    "complete_ordinary_erasure", "malformed_marker", "malformed_baseline",
    "malformed_epoch_schema", "missing_detail_table", "inconsistent_cutover",
])
def test_damaged_active_transport_is_503_and_immutable(client, profile_homes, damage):
    from hermes_state_common import SESSION_USAGE_RECONCILIATION_KEY

    path = profile_homes["default"] / "state.db"
    marker = _create_cutover_with_live_gap(path)
    assert client.get("/api/sessions/s/usage").status_code == 200
    with sqlite3.connect(path) as raw:
        _damage_cutover(raw, damage)
        _damage_epoch(raw, damage)
        if damage == "complete_ordinary_erasure":
            _damage_cutover(raw, "marker_and_integrity_binding")
            raw.execute("DROP TABLE session_usage_reconciliation_baseline")
        elif damage == "malformed_marker":
            raw.execute("UPDATE state_meta SET value = '{broken' WHERE key = ?",
                        (SESSION_USAGE_RECONCILIATION_KEY,))
        elif damage == "malformed_baseline":
            raw.execute("UPDATE session_usage_reconciliation_baseline SET input_tokens = 'broken'")
        elif damage == "malformed_epoch_schema":
            raw.execute("ALTER TABLE session_usage_trusted_epoch RENAME COLUMN epoch_id TO broken")
        elif damage == "missing_detail_table":
            raw.execute("DROP TABLE session_usage_events")
        elif damage == "inconsistent_cutover":
            value = dict(marker, cutover_at=marker["cutover_at"] + 1)
            raw.execute("UPDATE state_meta SET value = ? WHERE key = ?",
                        (json.dumps(value), SESSION_USAGE_RECONCILIATION_KEY))
    raw = sqlite3.connect(path)
    before = list(raw.iterdump())
    try:
        for _ in range(2):
            response = client.get("/api/sessions/s/usage")
            assert response.status_code == 503, response.text
            payload = response.json()
            assert payload["coverage"]["status"] == "UNAVAILABLE"
            assert payload["coverage"]["reason_code"] == "TRUSTED_USAGE_STATE_DAMAGED"
            assert payload["totals"] is None
            assert "trusted accounting state is damaged" in payload["coverage"]["message"]
            for private in (str(path), "sqlite", "Traceback", "{broken", marker["generation"]):
                assert private.lower() not in response.text.lower()
            assert list(raw.iterdump()) == before
    finally:
        raw.close()


def test_accounting_sync_failure_has_distinct_reason(client, profile_homes, monkeypatch):
    from hermes_state import SessionDB

    _create_cutover_with_live_gap(profile_homes["default"] / "state.db")
    monkeypatch.setattr(SessionDB, "_synchronize_usage_accounting",
                        lambda self, session_id: "accounting_flush_failed")
    response = client.get("/api/sessions/s/usage")
    assert response.status_code == 503
    assert response.json()["coverage"]["reason_code"] == "ACCOUNTING_SYNCHRONIZATION_FAILED"
    assert response.json()["totals"] is None


@pytest.mark.parametrize("incomplete", [False, True])
def test_first_initialization_remains_recoverable(client, profile_homes, monkeypatch, incomplete):
    from hermes_state import SessionDB
    from hermes_cli import web_server

    path = profile_homes["default"] / "state.db"
    if incomplete:
        original = SessionDB._usage_epoch_init_checkpoint
        def interrupt(self, point):
            if point == "after_schema_tables":
                raise RuntimeError("injected bootstrap interruption")
        with monkeypatch.context() as patch:
            patch.setattr(SessionDB, "_usage_epoch_init_checkpoint", interrupt)
            with pytest.raises(RuntimeError, match="injected"):
                SessionDB(db_path=path)
        assert SessionDB._usage_epoch_init_checkpoint is original
    # Bootstrap through the public route, then create a real session.
    assert client.get("/api/sessions/absent/usage").status_code == 404
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT generation FROM session_usage_trusted_epoch").fetchone() == (1,)
    db = web_server._open_session_db_at_path(path, read_only=False)
    db.create_session("new", "cli")
    db.close()
    response = client.get("/api/sessions/new/usage")
    assert response.status_code == 200
    assert response.json()["coverage"]["status"] == "COMPLETE"
    assert response.json()["totals"] is not None
