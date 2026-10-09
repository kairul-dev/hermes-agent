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

3. ``_load_reasoning_config`` must honor a YAML boolean False
   (``reasoning_effort: false`` / ``off`` / ``no``) as thinking-disabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tui_gateway import server
from tui_gateway.server import _session_info


def _agent(reasoning_config, **overrides):
    return SimpleNamespace(**{
        "reasoning_config": reasoning_config,
        "service_tier": None,
        "model": "glm-5",
        "provider": "zai",
        "session_id": "sess-key",
        **overrides,
    })


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
        assert info["reasoning_effort_wire"] == ""

    def test_wire_level_is_what_the_route_actually_sends(self) -> None:
        """`ultra` is a Hermes-internal step (#61634): the route clamps it, and the Desktop must be able to
        say so ("ultra sends max on this route") instead of presenting Ultra as a distinct wire level."""
        info = _session_info(_agent({"enabled": True, "effort": "ultra"}))
        assert info["reasoning_effort"] == "ultra"
        assert info["reasoning_effort_wire"] == "max"
        # Verbatim levels report themselves, so clients only annotate a real clamp.
        assert _session_info(_agent({"enabled": True, "effort": "high"}))["reasoning_effort_wire"] == "high"
        assert _session_info(_agent({"enabled": False}))["reasoning_effort_wire"] == ""
        # On the Codex app-server ``ultra`` is codex's own harness mode, sent verbatim.
        app_server = _agent({"enabled": True, "effort": "ultra"}, provider="openai-codex",
                            model="gpt-5.6-sol", api_mode="codex_app_server")
        assert _session_info(app_server)["reasoning_effort_wire"] == "ultra"


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


class TestSessionNoneReachesDeepSeekWire:
    """Desktop ``config.set value=none`` must disable DeepSeek V4 thinking.

    ``{effort: "none"}`` without ``enabled: False`` is what ``_session_info``
    already reports as Off; the profile used to ignore it and send enabled.
    """

    def test_session_info_effort_none_without_enabled_reports_none(self) -> None:
        info = _session_info(_agent({"effort": "none"}))
        assert info["reasoning_effort"] == "none"

    def test_config_set_none_on_lazy_session_pins_disabled_override(self) -> None:
        session = {"session_key": "k-lazy", "agent": None}
        with patch.dict(server._sessions, {"s-lazy": session}, clear=False), \
                patch.object(server, "_write_config_key") as write_key:
            resp = server._methods["config.set"](
                "rid-1", {"key": "reasoning", "session_id": "s-lazy", "value": "none"}
            )
        assert resp["result"]["value"] == "none"
        assert session["create_reasoning_override"] == {"enabled": False}
        write_key.assert_not_called()
        kw = server._deferred_build_agent_kwargs(session, session_db=None)
        assert kw["reasoning_config_override"] == {"enabled": False}

    def test_effort_none_override_emits_thinking_disabled(self) -> None:
        import model_tools
        import providers
        from agent.transports.chat_completions import ChatCompletionsTransport

        profile = providers.get_provider_profile("deepseek")
        kwargs = ChatCompletionsTransport().build_kwargs(
            model="deepseek-v4.1-flash-expires-on-0910",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=profile,
            reasoning_config={"effort": "none"},
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in kwargs


class TestSessionInfoReasoningPin:
    """The session's create_reasoning_override pin outranks a lagging agent.

    A session-scoped change made while the deferred agent build is in flight
    lands on ``create_reasoning_override`` and is applied when the agent is
    built — but an agent that finished building BEFORE the pin exists keeps the
    PROFILE config until the next build re-applies it. ``_session_info`` used to
    read only the agent, so session.info / resume / activate reported the
    profile effort while ``config.get {session_id}`` reported the pin, and
    clients that trust info (desktop, Forge) snapped the user's pick back.
    """

    def test_live_pin_outranks_a_lagging_agent(self) -> None:
        info = _session_info(
            _agent({"enabled": True, "effort": "medium"}),
            {
                "session_key": "k-pinned",
                "create_reasoning_override": {"enabled": True, "effort": "low"},
            },
        )
        assert info["reasoning_effort"] == "low"

    def test_disabled_pin_reports_none_not_the_agent(self) -> None:
        info = _session_info(
            _agent({"enabled": True, "effort": "high"}),
            {"session_key": "k-pinned", "create_reasoning_override": {"enabled": False}},
        )
        assert info["reasoning_effort"] == "none"

    def test_agent_value_is_used_when_no_pin_exists(self) -> None:
        """Control: the pin is an override, not a replacement for the agent."""
        info = _session_info(
            _agent({"enabled": True, "effort": "high"}),
            {"session_key": "k-live"},
        )
        assert info["reasoning_effort"] == "high"

    def test_non_dict_pin_falls_back_to_the_agent(self) -> None:
        info = _session_info(
            _agent({"enabled": True, "effort": "high"}),
            {"session_key": "k-live", "create_reasoning_override": None},
        )
        assert info["reasoning_effort"] == "high"


class TestSessionScopeReasoningFailsClosed:
    """A session-targeted reasoning change must never rewrite the profile value.

    The config.set dispatcher already rejects a NAMED session that is no longer
    live (_sess_nowait -> 4001 before any handler runs). These tests pin that
    behavior AND the remaining edge: ``scope: "session"`` with no session_id
    must not fall through to the global write either. The durable artifact —
    the real config.yaml on disk — is asserted, not a mocked writer.
    """

    PROFILE_EFFORT = "medium"

    def _config_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        monkeypatch.setattr(server, "_cfg_cache", None)
        monkeypatch.setattr(server, "_cfg_sig", None)
        monkeypatch.setattr(server, "_cfg_path", None)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"agent:\n  reasoning_effort: {self.PROFILE_EFFORT}\n", encoding="utf-8"
        )
        return cfg_path

    def _profile_effort(self, cfg_path) -> str:
        import hermes_yaml as yaml
        return str(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["agent"]["reasoning_effort"])

    def _dispatch(self, params: dict) -> dict:
        handler = server._methods["config.set"]
        return handler("rid-stale", params)

    def test_scope_session_without_session_id_rejected(self, tmp_path, monkeypatch):
        cfg_path = self._config_home(tmp_path, monkeypatch)
        before = cfg_path.read_bytes()

        resp = self._dispatch({"key": "reasoning", "value": "high", "scope": "session"})

        assert "error" in resp, "a session-scoped request that names no session must be rejected"
        assert resp["error"]["code"] == 4001
        assert cfg_path.read_bytes() == before
        assert self._profile_effort(cfg_path) == self.PROFILE_EFFORT

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
            popped = server._sessions.pop("s-raced", None)
            assert popped is not None
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

    def test_live_session_change_still_session_scoped(self, tmp_path, monkeypatch):
        """The guard must not break the case it protects."""
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


