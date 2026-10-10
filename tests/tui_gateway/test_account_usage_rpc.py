"""``account.usage`` JSON-RPC: structured subscription limits for the desktop
sidebar panel.

Contract: account-level (no session needed), fail-open to an empty provider
list so a broken fetch hides the panel instead of erroring the surface, and
``refresh`` is the only way to bypass the backend cache.
"""

from __future__ import annotations

import pytest

import tui_gateway.server as srv


def _call(params: dict | None = None) -> dict:
    envelope = srv._methods["account.usage"](1, params or {})
    return envelope["result"]


@pytest.fixture
def panel(monkeypatch):
    """Replace the panel builder; returns the list of ``refresh`` flags it saw."""
    import agent.account_usage as au

    seen: list[bool] = []
    payload = [
        {
            "id": "anthropic",
            "label": "Claude",
            "plan": None,
            "fetched_at": "2026-10-09T12:00:00+00:00",
            "stale": False,
            "windows": [{"kind": "five_hour", "used_percent": 62.0, "reset_at": None}],
        }
    ]

    def fake(*, refresh=False):
        seen.append(refresh)
        return payload

    monkeypatch.setattr(au, "build_account_usage_panel", fake)
    return seen


def test_returns_the_panel_providers_without_needing_a_session(panel):
    result = _call()

    assert [p["id"] for p in result["providers"]] == ["anthropic"]
    assert result["providers"][0]["windows"][0]["kind"] == "five_hour"


def test_refresh_flag_passes_through_and_defaults_off(panel):
    _call()
    _call({"refresh": True})

    assert panel == [False, True]


def test_failure_is_an_empty_list_not_an_error(monkeypatch):
    import agent.account_usage as au

    def boom(*, refresh=False):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(au, "build_account_usage_panel", boom)

    envelope = srv._methods["account.usage"](1, {})

    assert "error" not in envelope
    assert envelope["result"] == {"providers": []}


def test_runs_off_the_stdin_loop(panel):
    # Two blocking provider HTTP fetches: must not stall approval.respond /
    # session.interrupt on the dispatcher thread.
    assert "account.usage" in srv._LONG_HANDLERS


# ── profile selection: HERMES_HOME *and* the profile's .env secret scope ──────────────


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """A launch profile plus an ``ops`` profile whose token lives in its own .env."""
    launch = tmp_path / "launch"
    ops = tmp_path / "profiles" / "ops"
    launch.mkdir(parents=True)
    ops.mkdir(parents=True)
    (ops / ".env").write_text("ANTHROPIC_TOKEN=ops-profile-token" + chr(10), encoding="utf-8")

    monkeypatch.setenv("ANTHROPIC_TOKEN", "launch-profile-token")
    monkeypatch.setattr(srv, "_hermes_home", launch)

    def profile_home(name):
        # Mirrors the real resolver: the launch profile is None, a named one is its home, an unknown one raises.
        name = (name or "").strip()
        if name in {"", "default"}:
            return None
        if name == "ops":
            return ops
        raise srv.ProfileUnavailableError(f"Profile '{name}' does not exist.")

    monkeypatch.setattr(srv, "_profile_home", profile_home)
    return launch, ops


def _spy_builder(monkeypatch):
    """Record what the builder can see from inside the RPC's scope."""
    import agent.account_usage as au
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    seen = []

    def fake(*, refresh=False):
        seen.append({"token": get_secret("ANTHROPIC_TOKEN"), "home": str(get_hermes_home())})
        return []

    monkeypatch.setattr(au, "build_account_usage_panel", fake)
    return seen


def test_a_secondary_profile_reads_its_own_env_not_the_launch_profiles(profiles, monkeypatch):
    _, ops = profiles
    seen = _spy_builder(monkeypatch)

    _call({"profile": "ops"})

    assert seen == [{"token": "ops-profile-token", "home": str(ops)}]


def test_the_secret_scope_does_not_outlive_the_call(profiles, monkeypatch):
    from agent.secret_scope import get_secret

    _spy_builder(monkeypatch)

    _call({"profile": "ops"})

    assert get_secret("ANTHROPIC_TOKEN") == "launch-profile-token"


def test_the_launch_profile_keeps_reading_the_process_environment(profiles, monkeypatch):
    seen = _spy_builder(monkeypatch)

    _call({})
    _call({"profile": "default"})

    assert [s["token"] for s in seen] == ["launch-profile-token", "launch-profile-token"]


def test_an_unknown_profile_is_empty_never_the_launch_profiles_account(profiles, monkeypatch):
    seen = _spy_builder(monkeypatch)

    assert _call({"profile": "ghost"}) == {"providers": []}
    assert seen == []  # the builder never ran, so no launch-profile account could leak
