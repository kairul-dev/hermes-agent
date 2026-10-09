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
    au._panel_last_attempt.clear()
    yield
    au._panel_cache.clear()
    au._panel_last_attempt.clear()


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
        monkeypatch.setattr(au, "resolve_anthropic_cooled_down_oauth_token", lambda: None)
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "oauth-tok")
        monkeypatch.setattr(au, "_is_oauth_token", lambda token: token == "oauth-tok")
        assert au._panel_has_credentials("anthropic") is True

        # Switched from OAuth to a plain API key: cannot serve the usage API.
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-api-key")
        assert au._panel_has_credentials("anthropic") is False

        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "  ")
        assert au._panel_has_credentials("anthropic") is False

    def test_unknown_error_errs_on_keeping_the_account(self, monkeypatch):
        def weird(*_a, **_k):
            raise ValueError("unexpected")

        monkeypatch.setattr(au, "_read_codex_tokens", weird)
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


# ── Codex credentials in a quota cooldown (real auth store, no resolver mocks) ──
import json  # noqa: E402
import time  # noqa: E402

import hermes_cli.auth as auth_mod  # noqa: E402


def _write_auth(tmp_path, payload):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, **payload}))


def _pool_entry(**over):
    now = time.time()
    entry = {
        "id": "cred",
        "label": "acct",
        "auth_type": "oauth",
        "priority": 0,
        "source": "device_code",
        "access_token": "tok-quota",
        "last_status": "exhausted",
        "last_status_at": now,
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_message": "The usage limit has been reached",
        "last_error_reset_at": now + 3 * 24 * 3600,
    }
    entry.update(over)
    return {"credential_pool": {"openai-codex": [entry]}}


@pytest.fixture
def no_network_quota_probe(monkeypatch):
    # The resolver and the pool each ask the live usage endpoint whether the
    # quota reset early; the account is genuinely still at its limit here.
    monkeypatch.setattr(auth_mod, "_probe_codex_quota_restored", lambda *a, **k: False)


class TestCodexQuotaCooldown:
    def test_probe_a_cooled_down_pool_login_is_still_signed_in(self, tmp_path, no_network_quota_probe):
        _write_auth(tmp_path, _pool_entry())
        assert au._panel_has_credentials("openai-codex") is True

    def test_probe_a_stored_singleton_login_is_signed_in(self, tmp_path):
        _write_auth(
            tmp_path,
            {"providers": {"openai-codex": {"tokens": {"access_token": "at", "refresh_token": "rt"}, "auth_mode": "chatgpt"}}},
        )
        assert au._panel_has_credentials("openai-codex") is True

    def test_probe_no_login_at_all_is_signed_out(self, tmp_path):
        _write_auth(tmp_path, {})
        assert au._panel_has_credentials("openai-codex") is False

    def test_probe_a_dead_credential_is_signed_out(self, tmp_path, no_network_quota_probe):
        # Exhausted for a NON-quota reason (token invalidated): not a usable account.
        _write_auth(
            tmp_path,
            _pool_entry(last_error_code=401, last_error_reason="token_invalidated", last_error_message=""),
        )
        assert au._panel_has_credentials("openai-codex") is False

    def test_fetch_resolves_the_cooled_down_token_instead_of_giving_up(self, tmp_path, no_network_quota_probe):
        _write_auth(tmp_path, _pool_entry())

        token, _base_url, account_id = au._resolve_codex_usage_credentials(None, None)

        assert token == "tok-quota"
        assert account_id is None

    def test_first_load_shows_an_exhausted_account_at_100_percent(self, tmp_path, monkeypatch, no_network_quota_probe):
        """The state the panel matters most in: Codex is at its limit. Through the
        real builder + real credential resolution, only the HTTP edge is stubbed."""
        _write_auth(tmp_path, _pool_entry())
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "")
        reset_at = time.time() + 3600
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 100, "reset_at": reset_at},
                "secondary_window": {"used_percent": 80, "reset_at": reset_at + 86400},
            },
        }

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                assert headers["Authorization"] == "Bearer tok-quota"
                return _Resp()

        monkeypatch.setattr(au.httpx, "Client", _Client)

        (codex,) = au.build_account_usage_panel()

        assert codex["id"] == "openai-codex"
        assert [(w["kind"], w["used_percent"]) for w in codex["windows"]] == [("five_hour", 100.0), ("weekly", 80.0)]

    def test_a_cached_account_is_not_evicted_when_it_hits_its_limit(self, tmp_path, monkeypatch, no_network_quota_probe):
        """Cached while healthy; then the fetch starts failing and the pool entry
        goes into a 429 cooldown. Still signed in -> keep (stale), never evict."""
        table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 90.0)), "anthropic": None}
        _install(monkeypatch, table)
        au.build_account_usage_panel()

        table["openai-codex"] = None
        _write_auth(tmp_path, _pool_entry())

        (codex,) = au.build_account_usage_panel(refresh=True)

        assert codex["stale"] is True


def test_stale_fallback_is_not_republished_as_fresh_by_a_later_cache_hit(monkeypatch):
    """A forced refresh that fails inside the TTL returns a stale copy; the NEXT
    ordinary call (remount, profile round-trip, another client) is a cache hit
    that must keep saying stale, not quietly clear the degraded state."""
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 38.0)), "anthropic": None}
    calls = _install(monkeypatch, table)
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: True)
    au.build_account_usage_panel()

    table["openai-codex"] = None
    (forced,) = au.build_account_usage_panel(refresh=True)
    assert forced["stale"] is True
    fetches = calls["openai-codex"]

    (later,) = au.build_account_usage_panel()

    assert later["stale"] is True
    assert calls["openai-codex"] == fetches  # still served from cache, no extra fetch


def test_a_later_success_clears_the_stale_flag(monkeypatch):
    table = {"openai-codex": _snapshot("openai-codex", _win("five_hour", 38.0)), "anthropic": None}
    _install(monkeypatch, table)
    monkeypatch.setattr(au, "_panel_has_credentials", lambda provider: True)
    au.build_account_usage_panel()
    table["openai-codex"] = None
    au.build_account_usage_panel(refresh=True)

    table["openai-codex"] = _snapshot("openai-codex", _win("five_hour", 41.0))
    (fresh,) = au.build_account_usage_panel(refresh=True)

    assert fresh["stale"] is False
    assert fresh["windows"][0]["used_percent"] == 41.0


# ── Claude OAuth benched by a 429 cooldown (diagnostic resolver) ──────────────────────────
class TestClaudeQuotaCooldown:
    def _no_chat_token(self, monkeypatch, cooled=None):
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: None)
        monkeypatch.setattr(au, "resolve_anthropic_cooled_down_oauth_token", lambda: cooled)

    def test_probe_a_cooled_down_oauth_login_is_still_signed_in(self, monkeypatch):
        self._no_chat_token(monkeypatch, cooled="sk-ant-oat01-cooled")
        assert au._panel_has_credentials("anthropic") is True

    def test_probe_nothing_stored_is_signed_out(self, monkeypatch):
        self._no_chat_token(monkeypatch, cooled=None)
        assert au._panel_has_credentials("anthropic") is False

    def test_probe_an_active_api_key_wins_over_a_cooled_down_oauth_entry(self, monkeypatch):
        # The user explicitly configured a plain key: that is the account in use.
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-api03-key")
        monkeypatch.setattr(au, "resolve_anthropic_cooled_down_oauth_token", lambda: "sk-ant-oat01-cooled")
        assert au._panel_has_credentials("anthropic") is False

    def test_first_load_shows_an_exhausted_claude_account_at_100_percent(self, monkeypatch):
        """Through the real builder + fetch; only the HTTP edge is stubbed."""
        self._no_chat_token(monkeypatch, cooled="sk-ant-oat01-cooled")
        monkeypatch.setattr(au, "_fetch_codex_account_usage", lambda *a, **k: None)
        reset = "2026-10-09T15:00:00+00:00"
        payload = {
            "five_hour": {"utilization": 100, "resets_at": reset},
            "seven_day": {"utilization": 0.42, "resets_at": reset},
        }

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                assert headers["Authorization"] == "Bearer sk-ant-oat01-cooled"
                return _Resp()

        monkeypatch.setattr(au.httpx, "Client", _Client)

        (claude,) = au.build_account_usage_panel()

        assert claude["id"] == "anthropic"
        assert [(w["kind"], w["used_percent"]) for w in claude["windows"]] == [("five_hour", 100.0), ("weekly", 42.0)]

    def test_a_cached_claude_account_is_not_evicted_when_it_hits_its_limit(self, monkeypatch):
        table = {"openai-codex": None, "anthropic": _snapshot("anthropic", _win("five_hour", 90.0))}
        _install(monkeypatch, table)
        monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-oat01-live")
        au.build_account_usage_panel()

        table["anthropic"] = None  # fetch now fails: the pool entry just went into cooldown
        self._no_chat_token(monkeypatch, cooled="sk-ant-oat01-cooled")

        (claude,) = au.build_account_usage_panel(refresh=True)

        assert claude["stale"] is True


# ── concurrent fetches for one profile/provider are serialized ─────────────────────────────
import threading  # noqa: E402


class _Gate:
    """A fake Codex fetch whose first call blocks until released; later calls answer at once."""

    def __init__(self, first_pct, later_pct):
        self.first_pct, self.later_pct = first_pct, later_pct
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, provider, **_kw):
        if provider != "openai-codex":
            return None
        with self._lock:
            self.calls += 1
            n = self.calls
        if n == 1:
            self.entered.set()
            assert self.release.wait(10)
            return _snapshot(provider, _win("five_hour", self.first_pct))
        return _snapshot(provider, _win("five_hour", self.later_pct))


def _in_thread(results, name, **kw):
    t = threading.Thread(target=lambda: results.__setitem__(name, au.build_account_usage_panel(**kw)))
    t.start()
    return t


def _codex_pct(panel):
    (codex,) = panel
    return codex["windows"][0]["used_percent"]


def test_an_older_in_flight_fetch_cannot_overwrite_a_newer_result(monkeypatch):
    """A (automatic) starts and blocks; B (forced) is asked afterwards. B's answer
    must be the one left in cache, however the two are scheduled."""
    gate = _Gate(first_pct=10.0, later_pct=99.0)
    monkeypatch.setattr(au, "fetch_account_usage", gate)
    results = {}

    a = _in_thread(results, "a")
    assert gate.entered.wait(10)
    b = _in_thread(results, "b", refresh=True)
    threading.Event().wait(0.3)  # let B reach the key lock
    gate.release.set()
    a.join(10)
    b.join(10)

    assert _codex_pct(results["b"]) == 99.0
    assert _codex_pct(au.build_account_usage_panel()) == 99.0  # cache holds the newer answer


def test_a_plain_call_shares_the_outcome_of_a_fetch_already_in_flight(monkeypatch):
    gate = _Gate(first_pct=10.0, later_pct=99.0)
    monkeypatch.setattr(au, "fetch_account_usage", gate)
    results = {}

    a = _in_thread(results, "a")
    assert gate.entered.wait(10)
    b = _in_thread(results, "b")  # plain: no refresh
    threading.Event().wait(0.3)
    gate.release.set()
    a.join(10)
    b.join(10)

    assert gate.calls == 1  # one upstream fetch served both callers
    assert _codex_pct(results["a"]) == _codex_pct(results["b"]) == 10.0


def test_providers_are_not_serialized_behind_each_other(monkeypatch):
    """The lock is per (home, provider): a slow Codex must not hold up Claude."""
    from hermes_constants import get_hermes_home

    gate = _Gate(first_pct=10.0, later_pct=10.0)

    def fake(provider, **kw):
        if provider == "anthropic":
            return _snapshot("anthropic", _win("five_hour", 62.0))
        return gate(provider, **kw)

    monkeypatch.setattr(au, "fetch_account_usage", fake)
    results = {}

    a = _in_thread(results, "a")
    assert gate.entered.wait(10)
    # Codex is blocked; an independent Claude fetch for the same home still completes.
    claude = au._fetch_panel_entry("anthropic", "Claude", str(get_hermes_home()), False)
    assert claude["windows"][0]["used_percent"] == 62.0
    gate.release.set()
    a.join(10)
