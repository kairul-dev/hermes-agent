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
    # Still signed in: only the fetch failed.
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: True)
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


def test_profile_home_reaches_the_provider_fetch_workers(monkeypatch, tmp_path):
    """The RPC's profile scope is a ContextVar; worker threads must inherit it,
    or the fetch reads the launch profile's credentials while the result is
    cached under the requested profile (wrong account shown)."""
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    seen = {}

    def fake(provider, **_kw):
        seen[provider] = str(get_hermes_home())
        return _snapshot(provider, _win("five_hour", 10.0))

    monkeypatch.setattr(au, "fetch_account_usage", fake)
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)

    token = set_hermes_home_override(profile_home)
    try:
        au.build_account_usage_panel()
    finally:
        reset_hermes_home_override(token)

    assert seen == {"openai-codex": str(profile_home), "anthropic": str(profile_home)}


def test_signed_out_provider_is_evicted_not_kept_stale(monkeypatch):
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 38.0)), "anthropic": None}
    _install(monkeypatch, table)
    assert [p["id"] for p in au.build_account_usage_panel()] == ["openai-codex"]

    # Credentials removed: fetch_account_usage now answers None, same as a failure.
    table["openai-codex"] = None
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: False)

    assert au.build_account_usage_panel(refresh=True) == []
    assert au._panel_cache == {}

    # And it stays gone even if the credential probe later flips back (no resurrection from cache).
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: True)
    assert au.build_account_usage_panel(refresh=True) == []


def test_transient_failure_with_credentials_still_serves_stale(monkeypatch):
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 38.0)), "anthropic": None}
    _install(monkeypatch, table)
    au.build_account_usage_panel()

    table["openai-codex"] = None
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: True)

    (codex,) = au.build_account_usage_panel(refresh=True)

    assert codex["stale"] is True


class TestCredentialProbe:
    def test_anthropic_requires_an_oauth_login_not_just_a_token(self, monkeypatch):
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "oauth-tok")
        monkeypatch.setattr(au, "_is_oauth_token", lambda token: token == "oauth-tok")
        assert au._panel_has_credentials("anthropic") is True

        # Switched from OAuth to a plain API key: cannot serve the usage API.
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-api-key")
        assert au._panel_has_credentials("anthropic") is False

        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "  ")
        assert au._panel_has_credentials("anthropic") is False

    def test_codex_without_credentials_is_absent(self, monkeypatch):
        def none(*_a, **_k):
            raise au.AuthError("not signed in")

        monkeypatch.setattr(au, "_resolve_codex_usage_credentials", none)
        assert au._panel_has_credentials("openai-codex") is False

        def pool_empty(*_a, **_k):
            raise RuntimeError("No available openai-codex credential in credential pool")

        monkeypatch.setattr(au, "_resolve_codex_usage_credentials", pool_empty)
        assert au._panel_has_credentials("openai-codex") is False

    def test_codex_with_credentials_is_present(self, monkeypatch):
        monkeypatch.setattr(au, "_resolve_codex_usage_credentials", lambda *_a, **_k: ("tok", "", None))
        assert au._panel_has_credentials("openai-codex") is True

    def test_unknown_error_errs_on_keeping_the_account(self, monkeypatch):
        def weird(*_a, **_k):
            raise ValueError("unexpected")

        monkeypatch.setattr(au, "_resolve_codex_usage_credentials", weird)
        assert au._panel_has_credentials("openai-codex") is True


def test_anthropic_api_key_switch_drops_the_cached_oauth_account(monkeypatch):
    """End to end through the builder: OAuth account cached, then the user is on
    an API key (fetch reports unavailable) -> the old numbers must not linger."""
    table = {"openai-codex": None, "anthropic": _snapshot("anthropic", _win("five_hour", 62.0))}
    _install(monkeypatch, table)
    monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-api-key")
    monkeypatch.setattr(au, "_is_oauth_token", lambda token: False)
    assert [p["id"] for p in au.build_account_usage_panel()] == ["anthropic"]

    table["anthropic"] = _snapshot("anthropic", unavailable_reason="OAuth only")

    assert au.build_account_usage_panel(refresh=True) == []
    assert au._panel_cache == {}


def test_stale_numbers_expire_even_when_credentials_look_fine(monkeypatch):
    """A revoked-but-well-formed token passes the probe; the age cap is the
    backstop that ends the stale display."""
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 38.0)), "anthropic": None}
    _install(monkeypatch, table)
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: True)
    au.build_account_usage_panel()

    table["openai-codex"] = None
    (stale,) = au.build_account_usage_panel(refresh=True)
    assert stale["stale"] is True

    key = next(iter(au._panel_cache))
    fetched_at, payload = au._panel_cache[key]
    au._panel_cache[key] = (fetched_at - au._PANEL_STALE_MAX_S - 1, payload)

    assert au.build_account_usage_panel(refresh=True) == []
    assert au._panel_cache == {}
