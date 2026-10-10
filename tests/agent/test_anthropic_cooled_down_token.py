"""resolve_anthropic_cooled_down_oauth_token: diagnostic read of a pool OAuth entry
benched only by a quota (429) cooldown.

The chat resolver skips such entries on purpose. A usage reader needs the
opposite: the account is still valid and "at its limit" is what it should show.
Only STATUS_EXHAUSTED OAuth entries qualify; the read never mutates the pool.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.anthropic_credentials as ac
import agent.credential_pool as cp


def _entry(**over):
    base = dict(
        id="e1",
        auth_type=cp.AUTH_TYPE_OAUTH,
        last_status=cp.STATUS_EXHAUSTED,
        access_token="sk-ant-oat01-cooled",
        refresh_token="rt",
        source="hermes_pkce",
    )
    base.update(over)
    return SimpleNamespace(**base)


class _ReadOnlyPool:
    """Exposes ONLY entries(): any mutating/rotating call would AttributeError."""

    def __init__(self, entries):
        self._entries = entries

    def entries(self):
        return list(self._entries)


@pytest.fixture
def pool(monkeypatch):
    def install(*entries):
        monkeypatch.setattr(cp, "load_pool", lambda provider: _ReadOnlyPool(entries))
        monkeypatch.setattr(ac, "is_rotation_consumed_uncommitted", lambda *a, **k: False)

    return install


def test_returns_the_token_of_an_exhausted_oauth_entry(pool):
    pool(_entry())
    assert ac.resolve_anthropic_cooled_down_oauth_token() == "sk-ant-oat01-cooled"


@pytest.mark.parametrize(
    "over",
    [
        {"last_status": cp.STATUS_OK},  # healthy: the normal resolver owns it
        {"last_status": cp.STATUS_DEAD},  # revoked / invalidated: not a usable account
        {"auth_type": cp.AUTH_TYPE_API_KEY},  # a plain key cannot serve the usage API
        {"access_token": None},  # partially written row must not crash
        {"access_token": "   "},
    ],
)
def test_other_entries_do_not_qualify(pool, over):
    pool(_entry(**over))
    assert ac.resolve_anthropic_cooled_down_oauth_token() is None


def test_skips_a_spent_rotation_and_falls_through_to_the_next_entry(pool, monkeypatch):
    pool(_entry(id="spent", access_token="spent-tok"), _entry(id="good", access_token="good-tok"))
    monkeypatch.setattr(ac, "is_rotation_consumed_uncommitted", lambda tok, **k: tok == "spent-tok")
    assert ac.resolve_anthropic_cooled_down_oauth_token() == "good-tok"


def test_a_pool_that_cannot_be_read_is_quietly_none(monkeypatch):
    def boom(provider):
        raise RuntimeError("auth store unreadable")

    monkeypatch.setattr(cp, "load_pool", boom)
    assert ac.resolve_anthropic_cooled_down_oauth_token() is None


def test_the_normal_resolver_still_skips_cooled_down_entries(monkeypatch):
    """The two resolvers must stay opposites: chat never routes via a benched entry."""

    class _Pool:
        def _available_entries(self, **_kw):
            return [], []

        def entries(self):  # pragma: no cover - must not be consulted
            raise AssertionError("the chat resolver must not read benched entries")

    monkeypatch.setattr(cp, "load_pool", lambda provider: _Pool())
    assert ac._resolve_anthropic_pool_token() is None
