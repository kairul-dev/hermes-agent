"""Quota-cooled accounts in ``/usage`` and the stable window ``kind``.

``resolve_anthropic_cooled_down_oauth_token`` and the Codex tier-4 credential read are diagnostic
only: the chat resolvers keep refusing benched entries (asserted here too), while a usage reader
still sees the login that just hit its limit. Everything runs over a real ``auth.json`` in the
isolated HERMES_HOME; only HTTP is replaced.
"""

import json
import time

import pytest

import agent.anthropic_credentials as ac
from agent import account_usage


def _write_store(pool):
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": pool}), encoding="utf-8"
    )


def _anthropic_row(**over):
    row = {
        "id": "a1", "label": "claude", "auth_type": "oauth", "source": "manual:test", "priority": 0,
        "access_token": "sk-ant-oat01-cooled", "refresh_token": "rt", "last_status": "exhausted",
    }
    row.update(over)
    return row


def _codex_row(**over):
    row = {
        "id": "c1", "label": "codex", "auth_type": "oauth", "source": "manual:test", "priority": 0,
        "access_token": "codex-cooled", "refresh_token": "rt", "last_status": "exhausted",
        "last_error_code": 429, "last_error_reason": "rate_limit",
        "last_error_message": "usage_limit_reached", "last_error_reset_at": time.time() + 3600,
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    for name in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: None)


# ── Anthropic: cooled-down resolver ─────────────────────────────────────────────────────────────

def test_returns_the_token_of_an_exhausted_oauth_entry():
    _write_store({"anthropic": [_anthropic_row()]})
    assert ac.resolve_anthropic_cooled_down_oauth_token() == "sk-ant-oat01-cooled"


@pytest.mark.parametrize(
    "over",
    [
        {"last_status": "ok"},  # healthy: the normal resolver owns it
        {"last_status": "dead"},  # revoked / invalidated: not a usable account
        {"auth_type": "api_key", "access_token": "sk-ant-api03-plain-key"},  # a Console key cannot serve it
        {"access_token": None},  # partially written row must not crash
        {"access_token": "   "},
    ],
)
def test_other_entries_do_not_qualify(over):
    _write_store({"anthropic": [_anthropic_row(**over)]})
    assert ac.resolve_anthropic_cooled_down_oauth_token() is None


def test_skips_a_spent_rotation_and_falls_through_to_the_next_entry(monkeypatch):
    _write_store({"anthropic": [
        _anthropic_row(id="spent", access_token="spent-tok"),
        _anthropic_row(id="good", access_token="good-tok"),
    ]})
    monkeypatch.setattr(ac, "is_rotation_consumed_uncommitted", lambda tok, **kw: tok == "spent-tok")
    assert ac.resolve_anthropic_cooled_down_oauth_token() == "good-tok"


def test_an_unreadable_pool_is_quietly_none(monkeypatch):
    import hermes_cli.auth as auth

    def boom(*args, **kwargs):
        raise RuntimeError("auth store unreadable")

    monkeypatch.setattr(auth, "read_credential_pool", boom)
    assert ac.resolve_anthropic_cooled_down_oauth_token() is None


def test_the_cooled_down_read_never_writes_or_loads_the_pool(monkeypatch):
    _write_store({"anthropic": [_anthropic_row()]})
    from hermes_constants import get_hermes_home

    store = get_hermes_home() / "auth.json"
    before = store.read_bytes()

    import agent.credential_pool as cp

    monkeypatch.setattr(cp, "load_pool", lambda *a, **k: (_ for _ in ()).throw(AssertionError("load_pool")))
    assert ac.resolve_anthropic_cooled_down_oauth_token() == "sk-ant-oat01-cooled"
    assert store.read_bytes() == before


def test_the_chat_resolver_still_skips_cooled_down_entries():
    """The two resolvers stay opposites: chat never routes via a benched entry."""
    _write_store({"anthropic": [_anthropic_row(last_status="exhausted", last_error_reset_at=time.time() + 600)]})
    assert ac._resolve_anthropic_pool_token() is None


# ── Anthropic: /usage fetcher ───────────────────────────────────────────────────────────────────

def _usage_http(monkeypatch):
    seen = []

    def fake(url, headers, *, timeout):
        seen.append(headers["Authorization"])
        return {
            "five_hour": {"utilization": 1.0, "resets_at": "2026-10-10T12:00:00Z"},
            "seven_day": {"utilization": 0.4, "resets_at": "2026-10-14T12:00:00Z"},
            "seven_day_sonnet": {"utilization": 20.0, "resets_at": "2026-10-14T12:00:00Z"},
        }

    monkeypatch.setattr(account_usage, "_get_json", fake)
    return seen


def test_usage_fetch_uses_the_cooled_down_login_and_reads_percentages_with_kinds(monkeypatch):
    _write_store({"anthropic": [_anthropic_row()]})
    seen = _usage_http(monkeypatch)

    snapshot = account_usage.fetch_account_usage("anthropic")

    assert seen == ["Bearer sk-ant-oat01-cooled"]
    by_label = {w.label: w for w in snapshot.windows}
    # 1.0 and 0.4 are 1% and 0.4% as reported — not "100%" and "40%".
    assert by_label["Current session"].used_percent == 1.0
    assert by_label["Current week"].used_percent == 0.4
    assert {w.label: w.kind for w in snapshot.windows} == {
        "Current session": "five_hour", "Current week": "weekly", "Sonnet week": None,
    }


def test_an_explicit_api_key_is_never_replaced_by_a_cooled_down_login(monkeypatch):
    _write_store({"anthropic": [_anthropic_row()]})
    seen = _usage_http(monkeypatch)
    snapshot = account_usage.fetch_account_usage("anthropic", api_key="sk-ant-api03-explicit")
    assert seen == []  # API keys are not OAuth: reported unavailable, never swapped for another account
    assert snapshot is not None and snapshot.unavailable_reason


# ── Codex: tier-4 cooled-down credentials in /usage ─────────────────────────────────────────────

def test_codex_usage_credentials_fall_back_to_a_quota_cooled_pool_login():
    _write_store({"openai-codex": [_codex_row()]})
    token, base_url, account_id = account_usage._resolve_codex_usage_credentials(None, None)
    assert token == "codex-cooled" and account_id is None
    assert base_url.startswith("https://")


def test_codex_usage_credentials_do_not_adopt_an_auth_failed_pool_login():
    """Only a 429/quota stop is a "valid login at its limit"; an auth failure is not an account to report."""
    _write_store({"openai-codex": [
        _codex_row(last_error_code=401, last_error_reason="auth", last_error_message="invalid token"),
    ]})
    with pytest.raises(Exception):
        account_usage._resolve_codex_usage_credentials(None, None)


def test_codex_window_kinds_follow_the_duration_not_the_position(monkeypatch):
    """A weekly-only response occupies ``primary_window``: it must still be the weekly slot (#65387)."""
    monkeypatch.setattr(
        account_usage, "_get_json",
        lambda url, headers, *, timeout: {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 56, "limit_window_seconds": 604800, "reset_at": 1791590446},
                "secondary_window": {},
            },
        },
    )
    snapshot = account_usage._fetch_codex_account_usage(api_key="live", base_url="https://chatgpt.com/backend-api/codex")
    assert [(w.label, w.kind) for w in snapshot.windows] == [("Weekly", "weekly")]
