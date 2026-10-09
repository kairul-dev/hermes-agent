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
