"""Usage deadlines return while a blocked portal request is still running."""
import threading

import pytest

from agent.account_usage import build_credits_view, nous_credits_lines
from agent.billing_usage import build_usage_model


@pytest.mark.parametrize("fetch", [nous_credits_lines, build_credits_view, build_usage_model])
def test_portal_deadline_does_not_wait_for_worker_shutdown(monkeypatch, fetch):
    import hermes_cli.auth
    import hermes_cli.nous_account

    monkeypatch.delenv("HERMES_DEV_CREDITS_FIXTURE", raising=False)
    monkeypatch.setattr(hermes_cli.auth, "get_provider_auth_state", lambda _: {"access_token": "test"})
    started, release, returned, finished = (threading.Event() for _ in range(4))
    result = []

    def portal(**kwargs):
        started.set()
        release.wait(10)
        finished.set()
        return None

    monkeypatch.setattr(hermes_cli.nous_account, "get_nous_portal_account_info", portal)

    def call():
        result.append(fetch(timeout=0.05))
        returned.set()

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    try:
        assert started.wait(2)
        assert returned.wait(2), "caller waited for the blocked request after its deadline"
        assert not finished.is_set()
        if fetch is nous_credits_lines:
            assert result == [[]]
        elif fetch is build_credits_view:
            assert not result[0].logged_in
        else:
            assert not result[0].available
    finally:
        release.set()
        caller.join(2)
        assert finished.wait(2)
