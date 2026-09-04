"""Trusted-cutover regressions for the Stage 4A S4A-06 remediation."""

import sqlite3
import time

from hermes_state import SessionDB
from hermes_state_common import (
    SESSION_USAGE_DETAIL_BASELINE_KEY,
    SESSION_USAGE_DETAIL_COVERAGE_KEY,
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
    db._conn.execute(
        "DELETE FROM state_meta WHERE key = ?",
        (SESSION_USAGE_DETAIL_BASELINE_KEY,),
    )
    db.close()

    upgraded = SessionDB(db_path=path)
    baseline = upgraded._conn.execute(
        "SELECT api_call_count, input_tokens "
        "FROM session_usage_activation_baseline WHERE session_id = 'reviewer'"
    ).fetchone()
    result = upgraded.get_session_usage_detail(
        "reviewer", start=activation, end=time.time() + 1
    )

    assert tuple(baseline) == (3, 105)
    assert result["coverage"]["status"] != "COMPLETE"
    upgraded.close()
