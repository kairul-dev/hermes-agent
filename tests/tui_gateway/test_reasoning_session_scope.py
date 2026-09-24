"""Reasoning-effort session scoping in the TUI gateway (desktop backend).

Covers the "desktop reverts thinking to medium after one turn" report:

1. ``_session_info`` must report ``reasoning_effort: "none"`` when reasoning
   is disabled — reporting ``""`` (indistinguishable from "unset") made the
   desktop adopt the empty value after the first turn, wiping its sticky
   "thinking off" pick so every later chat reverted to the default effort.

2. ``config.set key=reasoning`` with a live session must be session-scoped:
   it must NOT rewrite the global ``agent.reasoning_effort`` in config.yaml
   (the desktop model menu applies a per-model preset on every selection,
   which was silently clobbering the user's configured value), and it must
   land on ``create_reasoning_override`` so lazily-built sessions (agent not
   constructed until the first prompt) don't drop the change.

3. A session-targeted change must FAIL CLOSED when the named session is not
   live — deleted, idle-reaped, LRU-evicted, or a stale id held by a client
   whose gateway restarted. ``session is None`` used to fall through to the
   global write, so a pick made in a conversation that had just gone away
   silently changed ``agent.reasoning_effort`` for every other session,
   profile, CLI and gateway build. The intentional profile path (no
   session_id, or an explicit ``scope: "global"``) is preserved.

4. ``_load_reasoning_config`` must honor a YAML boolean False
   (``reasoning_effort: false`` / ``off`` / ``no``) as thinking-disabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import yaml

import tui_gateway.server as server
from tui_gateway.server import _session_info


def _agent(reasoning_config):
    return SimpleNamespace(
        reasoning_config=reasoning_config,
        service_tier=None,
        model="glm-5",
        provider="zai",
        session_id="sess-key",
    )


class TestSessionInfoReasoningEffort:
    """Disabled reasoning must be reported as 'none', never ''."""

    def test_disabled_reports_none(self) -> None:
        info = _session_info(_agent({"enabled": False}))
        assert info["reasoning_effort"] == "none"

    def test_enabled_reports_effort(self) -> None:
        info = _session_info(_agent({"enabled": True, "effort": "high"}))
        assert info["reasoning_effort"] == "high"

    def test_unset_reports_empty(self) -> None:
        info = _session_info(_agent(None))
        assert info["reasoning_effort"] == ""


class TestConfigSetReasoningSessionScope:
    """Session-targeted reasoning changes must not touch global config."""

    def _dispatch(self, params: dict) -> dict:
        handler = server._methods["config.set"]
        return handler("rid-1", params)

    def test_session_scoped_set_skips_global_write(self) -> None:
        agent = _agent(None)
        session = {"session_key": "k1", "agent": agent}
        with patch.dict(server._sessions, {"s1": session}, clear=False), \
                patch.object(server, "_write_config_key") as write_key, \
                patch.object(server, "_persist_live_session_runtime"), \
                patch.object(server, "_emit"):
            resp = self._dispatch(
                {"key": "reasoning", "session_id": "s1", "value": "none"}
            )
        assert resp["result"]["value"] == "none"
        assert agent.reasoning_config == {"enabled": False}
        write_key.assert_not_called()


    def test_no_session_persists_globally(self) -> None:
        with patch.object(server, "_write_config_key") as write_key:
            resp = self._dispatch({"key": "reasoning", "value": "low"})
        assert resp["result"]["value"] == "low"
        write_key.assert_called_once_with("agent.reasoning_effort", "low")

    def test_unknown_value_rejected(self) -> None:
        resp = self._dispatch({"key": "reasoning", "value": "bogus"})
        assert "error" in resp


class TestStaleSessionReasoningFailsClosed:
    """A session-targeted effort change must never rewrite the profile value.

    Regression for the deletion/reap/stale-id race: the conversation the pick
    belongs to is already gone when the request arrives. Before the fix,
    ``session is None`` fell through to
    ``_write_config_key("agent.reasoning_effort", <level>)`` — so changing
    reasoning in a conversation that had just been closed or evicted silently
    changed the profile default for every OTHER session, profile, CLI and
    gateway build. These tests read the real config.yaml on disk, so they
    assert the durable artifact rather than a mocked writer.
    """

    PROFILE_EFFORT = "medium"

    def _config_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"agent:\n  reasoning_effort: {self.PROFILE_EFFORT}\n", encoding="utf-8"
        )
        return cfg_path

    @staticmethod
    def _profile_effort(cfg_path) -> str:
        return str(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["agent"]["reasoning_effort"])

    @staticmethod
    def _dispatch(params: dict) -> dict:
        handler = server._methods["config.set"]
        return handler("rid-stale", params)

    def test_stale_session_id_rejected_and_profile_untouched(self, tmp_path, monkeypatch):
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = self._dispatch(
            {
                "session_id": "no-longer-live",
                "key": "reasoning",
                "value": "ultra",
                "scope": "session",
            }
        )

        assert "error" in resp, "a stale session-targeted change must be rejected"
        assert resp["error"]["code"] == 4001
        assert "not live" in resp["error"]["message"]
        assert cfg_path.read_bytes() == before, "the profile default must not be rewritten"
        assert self._profile_effort(cfg_path) == self.PROFILE_EFFORT

    def test_deleted_live_session_rejected(self, tmp_path, monkeypatch):
        """The deletion/race case: the session existed, then went away."""
        cfg_path = self._config_home(tmp_path, monkeypatch)
        session = {
            "session_key": "k-raced",
            "agent": _agent(None),
            "create_reasoning_override": {"enabled": True, "effort": "low"},
        }
        with patch.dict(server._sessions, {"s-raced": session}, clear=False):
            # Exactly how the gateway removes a live session before teardown
            # (idle reaper, LRU eviction, WS close): the pop is the ownership
            # claim, so the record is gone while the client still holds the id.
            popped = server._pop_session_by_id("s-raced")
            assert popped is not None
            assert "s-raced" not in server._sessions
            before = cfg_path.read_bytes()

            resp = self._dispatch(
                {
                    "session_id": "s-raced",
                    "key": "reasoning",
                    "value": "high",
                    "scope": "session",
                }
            )

        assert "error" in resp, "a deleted session must not fall back to the profile value"
        assert resp["error"]["code"] == 4001
        assert cfg_path.read_bytes() == before
        assert self._profile_effort(cfg_path) == self.PROFILE_EFFORT

    def test_session_scope_without_session_id_rejected(self, tmp_path, monkeypatch):
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = self._dispatch({"key": "reasoning", "value": "high", "scope": "session"})

        assert "error" in resp, "a session-scoped request that names no session must be rejected"
        assert resp["error"]["code"] == 4001
        assert cfg_path.read_bytes() == before

    def test_live_session_change_still_session_scoped(self, tmp_path, monkeypatch):
        """The fix must not break the case it protects."""
        cfg_path = self._config_home(tmp_path, monkeypatch)
        agent = _agent(None)
        session = {"session_key": "k-live", "agent": agent}
        with patch.dict(server._sessions, {"s-live": session}, clear=False), patch.object(
            server, "_persist_live_session_runtime"
        ), patch.object(server, "_emit"):
            before = cfg_path.read_bytes()
            resp = self._dispatch(
                {
                    "session_id": "s-live",
                    "key": "reasoning",
                    "value": "high",
                    "scope": "session",
                }
            )

        assert resp["result"]["value"] == "high"
        assert session["create_reasoning_override"] == {"enabled": True, "effort": "high"}
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
        assert cfg_path.read_bytes() == before, "a live session change stays session-scoped"

    def test_profile_write_without_session_still_works(self, tmp_path, monkeypatch):
        """Control: the profile path is preserved AND the file is observable.

        Without this, every "unchanged" assertion above would be vacuous — a
        guard that simply refused all writes would look identical.
        """
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = self._dispatch({"key": "reasoning", "value": "low"})

        assert resp["result"]["value"] == "low"
        assert cfg_path.read_bytes() != before, "the intentional profile write must land"
        assert self._profile_effort(cfg_path) == "low"

    def test_explicit_global_scope_with_stale_session_id_writes_profile(
        self, tmp_path, monkeypatch
    ):
        """An explicit ``scope: "global"`` is not a session-scoped request."""
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = self._dispatch(
            {"session_id": "no-longer-live", "key": "reasoning", "value": "xhigh", "scope": "global"}
        )

        assert resp["result"]["value"] == "xhigh"
        assert cfg_path.read_bytes() != before
        assert self._profile_effort(cfg_path) == "xhigh"


class TestLoadReasoningConfigYamlBoolean:
    """YAML `reasoning_effort: false` means disabled, not default."""

    def test_boolean_false_disables(self) -> None:
        with patch.object(
            server, "_load_cfg", return_value={"agent": {"reasoning_effort": False}}
        ):
            assert server._load_reasoning_config() == {"enabled": False}

    def test_string_false_disables(self) -> None:
        with patch.object(
            server, "_load_cfg", return_value={"agent": {"reasoning_effort": "false"}}
        ):
            assert server._load_reasoning_config() == {"enabled": False}

