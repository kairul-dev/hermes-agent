"""YOLO (approval bypass) session scoping in the TUI gateway (desktop backend).

Sibling of test_reasoning_session_scope.py / test_fast_session_scope.py.
``config.set key=yolo`` defaults to ``scope: "session"`` and, when the session
lookup MISSES, falls through to the process-wide ``HERMES_YOLO_MODE`` env flag —
an approval bypass for everything this gateway process runs (children inherit
it) — while still answering ``scope: "session"``. A caller that NAMED a session
therefore had its stale id silently armed process-wide.

Intent, from the desktop callers (`apps/desktop/src/lib/yolo-session.ts`):
``setSessionYolo`` sends ``session_id`` and documents that it "does NOT touch
the global ... so CLI / TUI / cron behavior is unaffected"; the global
affordance is a separate ``scope: "global"`` call (`setGlobalYolo`), and the
TUI's ``/yolo`` and the Shift+Tab / zap input handler always send a live sid.

These tests are INERT: they never run a command, never enable a real bypass
(``tools.approval._YOLO_MODE_FROZEN`` is read at import time), and assert the
env flag plus that the session-scoped helpers are the ones called.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import tui_gateway.server as server

PROCESS_FLAG = "HERMES_YOLO_MODE"


def _set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


class TestStaleSessionYoloFailsClosed:
    """A session-targeted YOLO toggle must not mutate broader state."""

    def test_stale_session_id_is_rejected_and_process_flag_untouched(self, monkeypatch):
        monkeypatch.delenv(PROCESS_FLAG, raising=False)

        resp = _set({"session_id": "no-longer-live", "key": "yolo", "value": "1"})

        assert "error" in resp, "a stale session-targeted YOLO toggle must be rejected"
        assert resp["error"]["code"] == 4001
        assert "not live" in resp["error"]["message"]
        assert PROCESS_FLAG not in os.environ, "no process-wide bypass may be armed"

    def test_stale_session_id_off_is_rejected_too(self, monkeypatch):
        """Disabling globally from a dead session is equally a scope escape."""
        monkeypatch.setenv(PROCESS_FLAG, "1")

        resp = _set({"session_id": "no-longer-live", "key": "yolo", "value": "0"})

        assert resp["error"]["code"] == 4001
        assert os.environ.get(PROCESS_FLAG) == "1", "the inherited flag must survive"

    def test_deleted_live_session_rejected(self, monkeypatch):
        """The deletion/race case: the session existed, then went away."""
        monkeypatch.delenv(PROCESS_FLAG, raising=False)
        session = {"session_key": "k-raced", "agent": None}
        with patch.dict(server._sessions, {"s-raced": session}, clear=False):
            popped = server._pop_session_by_id("s-raced")
            assert popped is not None
            assert "s-raced" not in server._sessions

            resp = _set({"session_id": "s-raced", "key": "yolo", "value": "1"})

        assert resp["error"]["code"] == 4001
        assert PROCESS_FLAG not in os.environ

    def test_live_session_toggle_stays_session_scoped(self, monkeypatch):
        """The fix must not break the case it protects."""
        monkeypatch.delenv(PROCESS_FLAG, raising=False)
        session = {"session_key": "k-live", "agent": None}
        with patch.dict(server._sessions, {"s-live": session}, clear=False), patch(
            "tools.approval.is_session_yolo_enabled", return_value=False
        ), patch("tools.approval.enable_session_yolo") as enable, patch(
            "tools.approval.disable_session_yolo"
        ):
            resp = _set({"session_id": "s-live", "key": "yolo", "value": "1"})

        assert resp["result"]["value"] == "1"
        assert resp["result"]["scope"] == "session"
        enable.assert_called_once_with("k-live")
        assert PROCESS_FLAG not in os.environ, "a live session toggle must stay session-scoped"

    def test_no_session_process_flag_path_is_preserved(self, monkeypatch):
        """Control: the intentional sessionless process path still works.

        Without this, every "untouched" assertion above would be vacuous — a
        guard that refused every call would look identical.
        """
        monkeypatch.delenv(PROCESS_FLAG, raising=False)

        resp = _set({"key": "yolo", "value": "1"})

        assert resp["result"]["value"] == "1"
        assert os.environ.get(PROCESS_FLAG) == "1"
        monkeypatch.delenv(PROCESS_FLAG, raising=False)  # inert: leave no flag behind

    def test_global_scope_is_unaffected(self, monkeypatch):
        """`scope: "global"` is the documented global affordance, session or not."""
        monkeypatch.delenv(PROCESS_FLAG, raising=False)
        with patch.object(server, "_write_config_key") as write_key, patch.object(
            server, "_load_cfg", return_value={"approvals": {"mode": "manual"}}
        ):
            resp = _set({"session_id": "no-longer-live", "key": "yolo", "value": "1", "scope": "global"})

        assert resp["result"]["scope"] == "global"
        write_key.assert_called_once_with("approvals.mode", "off")
        assert PROCESS_FLAG not in os.environ
