"""Fast-mode (service tier) session scoping in the TUI gateway (desktop backend).

Sibling of test_reasoning_session_scope.py — the ``reasoning`` key was made
session-scoped when a session is targeted, but ``fast`` kept writing the
global ``agent.service_tier`` to config.yaml on every call. The desktop's
per-model presets call ``config.set key=fast`` on every model selection, so
toggling fast in ONE session silently flipped the tier for every other
session, profile, CLI, and gateway build ("switch one session, switches
everywhere").

Contract under test:

1. ``config.set key=fast`` with a session must NOT write config.yaml; it pins
   ``create_service_tier_override`` ("priority" / "" for explicit normal) so
   lazily-built sessions and rebuilds keep the choice.
2. Without a session it persists globally, unchanged.
3. ``config.get key=fast`` must read a pre-build session's pin.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import yaml

import tui_gateway.server as server

FAST_OVERRIDES = {"service_tier": "priority"}


def _agent(service_tier=None):
    return SimpleNamespace(
        reasoning_config=None,
        service_tier=service_tier,
        request_overrides={},
        model="gpt-6",
        provider="openai",
        session_id="sess-key",
    )


def _set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


def _get(params: dict) -> dict:
    return server._methods["config.get"]("rid-1", params)


class TestConfigSetFastSessionScope:
    """Session-targeted fast changes must never touch global config."""

    def test_session_scoped_fast_skips_global_write(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
                patch.object(server, "_write_config_key") as write_key, \
                patch.object(server, "_persist_live_session_runtime"), \
                patch.object(server, "_emit"), \
                patch(
                    "hermes_cli.models.resolve_fast_mode_overrides",
                    return_value=FAST_OVERRIDES,
                ):
            resp = _set({"key": "fast", "session_id": "s1", "value": "fast"})
        assert resp["result"]["value"] == "fast"
        assert agent.service_tier == "priority"
        assert session["create_service_tier_override"] == "priority"
        write_key.assert_not_called()


    def test_lazy_session_pins_create_override(self) -> None:
        """A pre-build (agent=None) session must keep the change for the
        deferred agent build instead of dropping it."""
        session = {
            "session_key": "k3",
            "agent": None,
            "model_override": {"model": "gpt-6", "provider": "openai"},
        }
        with patch.dict(server._sessions, {"s3": session}, clear=False), \
                patch.object(server, "_write_config_key") as write_key, \
                patch(
                    "hermes_cli.models.resolve_fast_mode_overrides",
                    return_value=FAST_OVERRIDES,
                ):
            resp = _set({"key": "fast", "session_id": "s3", "value": "fast"})
        assert resp["result"]["value"] == "fast"
        assert session["create_service_tier_override"] == "priority"
        write_key.assert_not_called()


    def test_toggle_flips_prebuild_pin(self) -> None:
        """An empty value toggles from the session's pin, not the global."""
        session = {
            "session_key": "k5",
            "agent": None,
            "create_service_tier_override": "priority",
        }
        with patch.dict(server._sessions, {"s5": session}, clear=False), \
                patch.object(server, "_write_config_key") as write_key:
            resp = _set({"key": "fast", "session_id": "s5", "value": ""})
        assert resp["result"]["value"] == "normal"
        assert session["create_service_tier_override"] == ""
        write_key.assert_not_called()

    def test_no_session_persists_globally(self) -> None:
        with patch.object(server, "_write_config_key") as write_key:
            resp = _set({"key": "fast", "value": "normal"})
        assert resp["result"]["value"] == "normal"
        write_key.assert_called_once_with("agent.service_tier", "normal")


class TestConfigGetFastSessionScope:
    def test_reads_prebuild_pin(self) -> None:
        session = {
            "session_key": "k6",
            "agent": None,
            "create_service_tier_override": "priority",
        }
        with patch.dict(server._sessions, {"s6": session}, clear=False):
            resp = _get({"key": "fast", "session_id": "s6"})
        assert resp["result"]["value"] == "fast"


    def test_falls_back_to_global(self) -> None:
        with patch.object(server, "_load_service_tier", return_value="priority"):
            resp = _get({"key": "fast"})
        assert resp["result"]["value"] == "fast"


class TestStaleSessionFastFailsClosed:
    """A session-targeted fast change must never rewrite the global tier.

    Sibling of the reasoning guard. ``config.set key=fast`` treated an
    unresolvable session exactly like an absent one, so the desktop's per-model
    preset (apps/desktop/src/store/model-presets.ts — the same id it holds for
    the conversation) and the TUI's ``/fast`` (ui-tui slash commands) rewrote
    ``agent.service_tier`` for every other session, profile, CLI and gateway
    build when the live id was stale. These tests read the real config.yaml on
    disk, so they assert the durable artifact rather than a mocked writer.
    """

    PROFILE_TIER = "priority"

    def _config_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "model:\n"
            "  default: gpt-6\n"
            "  provider: openai\n"
            "agent:\n"
            f"  service_tier: {self.PROFILE_TIER}\n",
            encoding="utf-8",
        )
        return cfg_path

    @staticmethod
    def _profile_tier(cfg_path) -> str:
        return str(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["agent"]["service_tier"])

    def test_stale_session_id_rejected_and_profile_tier_untouched(self, tmp_path, monkeypatch):
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = _set({"session_id": "no-longer-live", "key": "fast", "value": "normal"})

        assert "error" in resp, "a stale session-targeted fast change must be rejected"
        assert resp["error"]["code"] == 4001
        assert "not live" in resp["error"]["message"]
        assert cfg_path.read_bytes() == before, "agent.service_tier must not be rewritten"
        assert self._profile_tier(cfg_path) == self.PROFILE_TIER

    def test_stale_session_id_rejected_even_when_the_level_resolves(
        self, tmp_path, monkeypatch
    ):
        """The reject must not depend on the model's fast support."""
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()
        with patch(
            "hermes_cli.models.resolve_fast_mode_overrides", return_value=FAST_OVERRIDES
        ):
            resp = _set({"session_id": "no-longer-live", "key": "fast", "value": "fast"})

        assert resp["error"]["code"] == 4001
        assert cfg_path.read_bytes() == before

    def test_deleted_live_session_rejected(self, tmp_path, monkeypatch):
        """The deletion/race case: the session existed, then went away."""
        cfg_path = self._config_home(tmp_path, monkeypatch)
        session = {"session_key": "k-raced", "agent": _agent(), "create_service_tier_override": "priority"}
        with patch.dict(server._sessions, {"s-raced": session}, clear=False):
            popped = server._pop_session_by_id("s-raced")
            assert popped is not None
            before = cfg_path.read_bytes()

            resp = _set({"session_id": "s-raced", "key": "fast", "value": "normal"})

        assert resp["error"]["code"] == 4001
        assert cfg_path.read_bytes() == before
        assert self._profile_tier(cfg_path) == self.PROFILE_TIER

    def test_live_session_change_still_session_scoped(self, tmp_path, monkeypatch):
        """The fix must not break the case it protects."""
        cfg_path = self._config_home(tmp_path, monkeypatch)
        session = {"session_key": "k-live", "agent": _agent()}
        with patch.dict(server._sessions, {"s-live": session}, clear=False), patch.object(
            server, "_emit"
        ):
            before = cfg_path.read_bytes()
            resp = _set({"session_id": "s-live", "key": "fast", "value": "normal"})

        assert resp["result"]["value"] == "normal"
        assert session["create_service_tier_override"] == ""
        assert cfg_path.read_bytes() == before, "a live session change stays session-scoped"

    def test_no_session_still_persists_globally(self, tmp_path, monkeypatch):
        """Control: the intentional profile path is preserved AND observable.

        Without this, every "unchanged" assertion above would be vacuous — a
        guard that simply refused all writes would look identical.
        """
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = _set({"key": "fast", "value": "normal"})

        assert resp["result"]["value"] == "normal"
        assert cfg_path.read_bytes() != before, "the intentional profile write must land"
        assert self._profile_tier(cfg_path) == "normal"
