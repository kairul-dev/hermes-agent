"""Sidebar usage panel payload (``build_account_usage_panel``).

Contract: one structured entry per provider that can report subscription
limits, with the 5-hour and weekly windows addressed by a stable ``kind`` (never
by provider label copy); cached per profile home; a transient failure keeps the
last good numbers marked stale instead of blanking the panel.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import account_usage as au
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

RESET = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _snapshot(provider, *windows, plan=None, unavailable_reason=None):
    return AccountUsageSnapshot(
        provider=provider,
        source="test",
        fetched_at=RESET,
        plan=plan,
        windows=tuple(windows),
        unavailable_reason=unavailable_reason,
    )


def _win(kind, used, label="x", reset=RESET):
    return AccountUsageWindow(label=label, used_percent=used, reset_at=reset, kind=kind)


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    au._panel_cache.clear()
    yield
    au._panel_cache.clear()


def _install(monkeypatch, table):
    """``table``: provider -> snapshot | None | Exception; records call counts."""
    calls = {}

    def fake(provider, **_kw):
        calls[provider] = calls.get(provider, 0) + 1
        value = table[provider]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(au, "fetch_account_usage", fake)
    return calls


def test_maps_windows_by_kind_not_label(monkeypatch):
    _install(
        monkeypatch,
        {
            "openai-codex": _snapshot(
                "openai-codex",
                _win("weekly", 54.0, label="Odd Weekly Copy"),
                _win("five_hour", 38.0, label="Odd Session Copy"),
                plan="Plus",
            ),
            "anthropic": _snapshot(
                "anthropic",
                _win("five_hour", 62.0),
                _win(None, 90.0, label="Opus week"),
                _win("weekly", 29.0),
            ),
        },
    )

    panel = au.build_account_usage_panel()

    assert [p["id"] for p in panel] == ["openai-codex", "anthropic"]
    codex, claude = panel
    assert codex["plan"] == "Plus"
    # Fixed order (five_hour, weekly) regardless of upstream order; the
    # kind-less Opus window has no panel slot.
    assert [w["kind"] for w in codex["windows"]] == ["five_hour", "weekly"]
    assert [w["kind"] for w in claude["windows"]] == ["five_hour", "weekly"]
    assert [w["used_percent"] for w in claude["windows"]] == [62.0, 29.0]
    assert claude["windows"][0]["reset_at"] == RESET.isoformat()
    assert not codex["stale"]


def test_percent_is_clamped_and_missing_windows_skipped(monkeypatch):
    _install(
        monkeypatch,
        {
            "openai-codex": _snapshot("openai-codex", _win("five_hour", 140.0), _win("weekly", None)),
            "anthropic": None,
        },
    )

    (codex,) = au.build_account_usage_panel()

    assert [(w["kind"], w["used_percent"]) for w in codex["windows"]] == [("five_hour", 100.0)]


def test_providers_without_data_are_omitted(monkeypatch):
    _install(
        monkeypatch,
        {
            "openai-codex": None,  # no credentials -> fetch_account_usage fails open
            "anthropic": _snapshot("anthropic", unavailable_reason="api key, not OAuth"),
        },
    )

    assert au.build_account_usage_panel() == []


def test_cached_within_ttl_and_refresh_bypasses(monkeypatch):
    calls = _install(
        monkeypatch,
        {"openai-codex": _snapshot("openai-codex", _win("five_hour", 10.0)), "anthropic": None},
    )

    au.build_account_usage_panel()
    au.build_account_usage_panel()
    assert calls["openai-codex"] == 1

    au.build_account_usage_panel(refresh=True)
    assert calls["openai-codex"] == 2


def test_failure_after_success_serves_stale_last_good(monkeypatch):
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 38.0)), "anthropic": None}
    _install(monkeypatch, table)
    au.build_account_usage_panel()

    table["openai-codex"] = RuntimeError("network down")
    (codex,) = au.build_account_usage_panel(refresh=True)

    assert codex["stale"] is True
    assert codex["windows"][0]["used_percent"] == 38.0


def test_failure_with_no_prior_success_is_absent_not_error(monkeypatch):
    _install(monkeypatch, {"openai-codex": RuntimeError("boom"), "anthropic": None})

    assert au.build_account_usage_panel() == []


def test_cache_is_scoped_per_profile_home(monkeypatch, tmp_path):
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 10.0)), "anthropic": None}
    calls = _install(monkeypatch, table)

    au.build_account_usage_panel()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
    au.build_account_usage_panel()

    # A second profile must not be answered from the first profile's account.
    assert calls["openai-codex"] == 2
