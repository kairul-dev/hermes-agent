"""Cookie mutations require provenance before verification, refresh or revoke."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE, SESSION_RT_COOKIE
from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
from hermes_cli.dashboard_auth.routes import router
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

ORIGIN = "https://agent.example.test"


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url", lambda: "")
    clear_providers()
    provider = StubAuthProvider()
    register_provider(provider)
    calls = []
    app = FastAPI()
    app.state.auth_required = True
    app.middleware("http")(gated_auth_middleware)
    app.include_router(router)

    @app.post("/api/mutate")
    async def mutate():
        calls.append("mutation")
        return {"ok": True}

    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        start = client.get("/auth/login", params={"provider": "stub"})
        callback = client.get(start.headers["location"])
        assert callback.status_code == 302
        yield client, provider, calls
    clear_providers()


@pytest.mark.parametrize("headers", [
    {}, {"Origin": ORIGIN + ":0"}, {"Origin": ORIGIN + ":"}, {"Origin": "null"}, {"Origin": "https://evil.example.test"},
    {"Origin": "http://agent.example.test"}, {"Origin": ORIGIN + ":444"},
    {"Origin": "https://evil.example.test\\@agent.example.test"},
    {"Origin": "https://user@agent.example.test"},
])
@pytest.mark.parametrize("refresh_only", [False, True])
def test_untrusted_cookie_mutation_never_verifies_or_refreshes(harness, monkeypatch, headers, refresh_only):
    client, provider, calls = harness
    if refresh_only:
        client.cookies.clear()
        # Mint a legitimate refresh cookie through the real OAuth route again.
        start = client.get("/auth/login", params={"provider": "stub"})
        client.get(start.headers["location"])
        for name in list(client.cookies.keys()):
            if not name.endswith(SESSION_RT_COOKIE):
                del client.cookies[name]
    monkeypatch.setattr(provider, "verify_session", lambda **kw: pytest.fail("verified hostile request"))
    monkeypatch.setattr(provider, "refresh_session", lambda **kw: pytest.fail("refreshed hostile request"))
    response = client.post("/api/mutate", headers=headers)
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("headers", [{"Origin": ORIGIN}, {"Referer": ORIGIN + "/chat"}])
def test_same_origin_cookie_mutation_works(harness, headers):
    client, _, calls = harness
    assert client.post("/api/mutate", headers=headers).status_code == 200
    assert calls == ["mutation"]


def test_same_origin_refresh_only_mutation_works(harness):
    client, _, calls = harness
    for name in list(client.cookies.keys()):
        if not name.endswith(SESSION_RT_COOKIE):
            del client.cookies[name]
    assert client.post("/api/mutate", headers={"Origin": ORIGIN}).status_code == 200
    assert calls == ["mutation"]


def test_explicit_bearer_does_not_require_browser_origin(harness):
    client, _, calls = harness
    access = next(value for name, value in client.cookies.items() if name.endswith(SESSION_AT_COOKIE))
    client.cookies.clear()
    assert client.post("/api/mutate", headers={"Authorization": f"Bearer {access}"}).status_code == 200
    assert calls == ["mutation"]


def test_logout_rejects_sibling_origin_before_revoke(harness, monkeypatch):
    client, provider, _ = harness
    revoked = []
    monkeypatch.setattr(provider, "revoke_session", lambda **kw: revoked.append(kw))
    assert client.post("/auth/logout", headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert revoked == []
    assert client.post("/auth/logout", headers={"Origin": ORIGIN}).status_code == 302
    assert len(revoked) == 1


def test_cookieless_logout_still_requires_trusted_origin(harness):
    # A cross-site form POST can omit SameSite cookies, yet the response's
    # Set-Cookie deletions would still be applied by the browser.
    client, _, _ = harness
    client.cookies.clear()
    assert client.post("/auth/logout", headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers={"Origin": ORIGIN}).status_code == 302


def test_configured_public_origin_handles_reverse_proxy(harness, monkeypatch):
    client, _, calls = harness
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url", lambda: "https://public.example.test/hermes")
    assert client.post("/api/mutate", headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/mutate", headers={"Origin": "https://public.example.test"}).status_code == 200
    assert calls == ["mutation"]
