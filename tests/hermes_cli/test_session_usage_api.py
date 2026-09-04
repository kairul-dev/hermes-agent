"""Authenticated, profile-safe dashboard session usage read API."""

import json

import pytest


@pytest.fixture
def profile_homes(tmp_path, monkeypatch, _isolate_hermes_home):
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    named_home = profiles_root / "worker"
    for home in (default_home, named_home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)

    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", default_home / "state.db")
    return {"default": default_home, "worker": named_home}


@pytest.fixture
def client(monkeypatch, profile_homes):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli import web_server

    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    test_client = TestClient(
        web_server.app, base_url="http://127.0.0.1:9119"
    )
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return test_client


def _seed_usage(home, session_id: str, input_tokens: int) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id, "cli", model="safe-model")
        db.append_message(
            session_id,
            "user",
            "private transcript content must never enter usage responses",
        )
        db.update_token_counts(
            session_id,
            input_tokens=input_tokens,
            output_tokens=2,
            cache_read_tokens=3,
            cache_write_tokens=4,
            reasoning_tokens=1,
            estimated_cost_usd=0.25,
            actual_cost_usd=0.20,
            model="safe-model",
            billing_provider="safe-provider",
            billing_base_url="https://secret-accounting.invalid/v1",
            billing_mode="metered",
            api_call_count=1,
        )
    finally:
        db.close()


def test_route_requires_dashboard_auth(profile_homes, monkeypatch):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli import web_server

    _seed_usage(profile_homes["default"], "auth", 5)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)

    response = TestClient(
        web_server.app, base_url="http://127.0.0.1:9119"
    ).get("/api/sessions/auth/usage")

    assert response.status_code == 401


def test_default_profile_projection_is_bounded_and_safe(
    client, profile_homes
):
    _seed_usage(profile_homes["default"], "default-session", 11)

    response = client.get("/api/sessions/default-session/usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability"] == "session.usage.detail.v1"
    assert payload["profile"] == "default"
    assert payload["scope"] == "physical"
    assert payload["coverage"]["status"] == "COMPLETE"
    assert payload["totals"]["input_tokens"] == 11
    assert payload["totals"]["actual_cost_usd"] == pytest.approx(0.20)
    assert payload["totals"]["estimated_cost_usd"] == pytest.approx(0.25)
    serialized = json.dumps(payload)
    assert "secret-accounting" not in serialized
    assert "billing_base_url" not in serialized
    assert "cost_source" not in serialized
    assert "private transcript content" not in serialized


def test_named_profile_isolation_with_duplicate_session_ids(
    client, profile_homes
):
    _seed_usage(profile_homes["default"], "duplicate", 10)
    _seed_usage(profile_homes["worker"], "duplicate", 90)

    default = client.get("/api/sessions/duplicate/usage").json()
    worker = client.get(
        "/api/sessions/duplicate/usage", params={"profile": "worker"}
    ).json()

    assert default["profile"] == "default"
    assert default["totals"]["input_tokens"] == 10
    assert worker["profile"] == "worker"
    assert worker["totals"]["input_tokens"] == 90
    assert worker["session_ids"] == ["duplicate"]


def test_named_profile_never_falls_back_to_default(client, profile_homes):
    _seed_usage(profile_homes["default"], "default-only", 10)

    response = client.get(
        "/api/sessions/default-only/usage", params={"profile": "worker"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


@pytest.mark.parametrize(
    "profile, expected_status",
    [
        ("missing", 404),
        ("../worker", 400),
        ("worker/../../default", 400),
    ],
)
def test_unavailable_or_malformed_profile_is_rejected(
    client, profile, expected_status
):
    response = client.get(
        "/api/sessions/anything/usage", params={"profile": profile}
    )

    assert response.status_code == expected_status


def test_missing_session_and_malformed_requests(client, profile_homes):
    _seed_usage(profile_homes["default"], "valid", 5)

    assert client.get("/api/sessions/missing/usage").status_code == 404
    assert client.get(
        "/api/sessions/valid/usage", params={"scope": "children"}
    ).status_code == 400
    assert client.get(
        "/api/sessions/valid/usage", params={"start": "not-a-time"}
    ).status_code == 422
    assert client.get(
        "/api/sessions/valid/usage", params={"start": 20, "end": 10}
    ).status_code == 400


def test_route_window_excludes_rows_before_and_after(client, profile_homes):
    import json
    import time

    from hermes_state import SessionDB
    from hermes_state_common import (
        SESSION_USAGE_DETAIL_BASELINE_KEY,
        SESSION_USAGE_DETAIL_COVERAGE_KEY,
        SESSION_USAGE_RECONCILIATION_EPOCH_KEY,
        SESSION_USAGE_RECONCILIATION_KEY,
        SESSION_USAGE_TRUSTED_EPOCH_TABLE,
    )

    db = SessionDB(db_path=profile_homes["default"] / "state.db")
    try:
        base = time.time() - 100
        db._conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (repr(base - 10), SESSION_USAGE_DETAIL_COVERAGE_KEY),
        )
        db._conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (repr(base - 10), SESSION_USAGE_DETAIL_BASELINE_KEY),
        )
        marker = json.loads(db._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (SESSION_USAGE_RECONCILIATION_KEY,),
        ).fetchone()[0])
        marker["cutover_at"] = base - 10
        marker_value = json.dumps(
            marker, separators=(",", ":"), sort_keys=True
        )
        db._conn.executemany(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (
                (marker_value, SESSION_USAGE_RECONCILIATION_KEY),
                (marker_value, SESSION_USAGE_RECONCILIATION_EPOCH_KEY),
            ),
        )
        db._conn.execute(
            f"UPDATE {SESSION_USAGE_TRUSTED_EPOCH_TABLE} SET activated_at = ?",
            (base - 10,),
        )
        db.create_session("window-api", "cli", model="m")
        for timestamp, tokens in (
            (base, 1),
            (base + 10, 2),
            (base + 20, 4),
        ):
            db.update_token_counts(
                "window-api",
                input_tokens=tokens,
                model="m",
                billing_provider="p",
                api_call_count=1,
                _usage_timestamp=timestamp,
            )
    finally:
        db.close()

    response = client.get(
        "/api/sessions/window-api/usage",
        params={"start": base + 10, "end": base + 20},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["input_tokens"] == 2
    assert payload["window"]["start_inclusive"] is True
    assert payload["window"]["end_exclusive"] is True


def test_sqlite_busy_failure_is_retryable_not_zero(
    client, profile_homes, monkeypatch
):
    import sqlite3

    from hermes_cli import web_server

    class BusyDB:
        def resolve_session_id(self, session_id):
            return session_id

        def get_session_usage_detail(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda profile, read_only: BusyDB(),
    )

    response = client.get("/api/sessions/busy/usage")

    assert response.status_code == 503
    assert "retry" in response.json()["detail"].lower()
    assert "totals" not in response.json()


def test_complete_cutover_erasure_returns_503_not_false_exactness(
    client, profile_homes
):
    from hermes_state import SessionDB
    from hermes_state_common import (
        SESSION_USAGE_RECONCILIATION_EPOCH_KEY,
        SESSION_USAGE_RECONCILIATION_KEY,
        SESSION_USAGE_TRUSTED_EPOCH_TABLE,
    )

    home = profile_homes["default"]
    _seed_usage(home, "damaged-epoch", 5)
    db = SessionDB(db_path=home / "state.db")
    try:
        epoch_before = tuple(db._conn.execute(
            f"SELECT generation, epoch_id, activated_at "
            f"FROM {SESSION_USAGE_TRUSTED_EPOCH_TABLE} WHERE singleton = 1"
        ).fetchone())
        db._conn.execute(
            "DELETE FROM state_meta WHERE key IN (?, ?)",
            (
                SESSION_USAGE_RECONCILIATION_KEY,
                SESSION_USAGE_RECONCILIATION_EPOCH_KEY,
            ),
        )
        db._conn.execute("DROP TABLE session_usage_reconciliation_baseline")
    finally:
        db.close()

    response = client.get("/api/sessions/damaged-epoch/usage")

    assert response.status_code == 503
    assert "trusted accounting state" in response.json()["detail"]
    assert "totals" not in response.json()
    raw = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        assert tuple(raw._conn.execute(
            f"SELECT generation, epoch_id, activated_at "
            f"FROM {SESSION_USAGE_TRUSTED_EPOCH_TABLE} WHERE singleton = 1"
        ).fetchone()) == epoch_before
        assert raw._conn.execute(
            "SELECT 1 FROM state_meta WHERE key IN (?, ?)",
            (
                SESSION_USAGE_RECONCILIATION_KEY,
                SESSION_USAGE_RECONCILIATION_EPOCH_KEY,
            ),
        ).fetchone() is None
        assert raw._conn.execute(
            "SELECT COUNT(*) FROM session_usage_reconciliation_baseline"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_public_api_returns_capped_detail_events(client, profile_homes):
    import time

    from hermes_state import SessionDB

    db = SessionDB(db_path=profile_homes["default"] / "state.db")
    try:
        db.create_session("many-events", "cli")
        timestamp = time.time()
        for _ in range(150):
            db.update_token_counts(
                "many-events",
                model="safe-model",
                billing_provider="safe-provider",
                billing_base_url="https://private.invalid/v1",
                input_tokens=1,
                api_call_count=1,
                _usage_timestamp=timestamp,
            )
    finally:
        db.close()

    payload = client.get("/api/sessions/many-events/usage").json()
    assert len(payload["events"]) == 100
    assert payload["events_truncated"] is True
    assert payload["totals"]["input_tokens"] == 150
    assert [row["event_id"] for row in payload["events"]] == sorted(
        row["event_id"] for row in payload["events"]
    )
    serialized = json.dumps(payload)
    assert "billing_base_url" not in serialized
    assert "private.invalid" not in serialized


def test_future_start_without_end_is_rejected(client, profile_homes):
    _seed_usage(profile_homes["default"], "future-window", 1)

    response = client.get(
        "/api/sessions/future-window/usage", params={"start": 9e15}
    )

    assert response.status_code == 400
    assert "synchronized effective end" in response.json()["detail"]
