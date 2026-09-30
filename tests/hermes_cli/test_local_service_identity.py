"""Real persisted service auth, scope, rotation and dispatcher contracts."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.datastructures import Headers

from hermes_cli.dashboard_auth.local_service import (
    CAPABILITY_ROUTE, CONTRACT, ServiceStore, ServiceDenied,
    authorize_params, authorize_rpc, local_peer, watch_service,
)
from hermes_cli.dashboard_auth.service_files import read_private


@pytest.fixture
def provision(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    grants = {"http": [f"GET {CAPABILITY_ROUTE}", "GET /api/model/options"],
              "ws": ["/api/ws"], "rpc": ["gateway.capabilities", "gateway.ping", "config.get"],
              "responses": ["approval", "clarify"],
              "restrictions": {"profile_ids": ["current"], "file_root": "", "project_roots": [str(tmp_path / "project")],
              "config_read_keys": ["theme"], "config_write_keys": ["theme"], "plugin_actions": ["list"], "mcp_names": ["fixture"], "cron_fields": ["name", "schedule", "prompt", "deliver"]}}
    store = ServiceStore()
    output = tmp_path / "credential"
    meta = store.create("fixture-host", int(time.time()) + 3600, grants, output)
    credential = read_private(output).decode().strip()
    return store, credential, meta, grants


def test_private_persistence_and_overlapping_rotation(provision, tmp_path):
    store, credential, meta, grants = provision
    assert credential.encode() not in store.path.read_bytes()
    assert credential not in json.dumps(store.metadata())
    first = ServiceStore(store.path).authenticate(credential)
    second_path = tmp_path / "rotation"
    second = store.create(meta["principal_id"], int(time.time()) + 3600, grants, second_path)
    next_credential = read_private(second_path).decode().strip()
    assert store.authenticate(next_credential).principal_id == first.principal_id
    store.revoke(meta["credential_id"])
    with pytest.raises(ServiceDenied):
        first.active()
    with pytest.raises(ServiceDenied):
        ServiceStore(store.path).authenticate(credential)
    assert ServiceStore(store.path).authenticate(next_credential).credential_id == second["credential_id"]
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE credentials SET expires=0 WHERE id=?", (second["credential_id"],))
    with pytest.raises(ServiceDenied):
        ServiceStore(store.path).authenticate(next_credential)


@pytest.mark.parametrize("peer,headers,allowed", [
    ("127.0.0.1", {}, True), ("::1", {}, True), ("::ffff:127.0.0.1", {}, True),
    ("192.0.2.2", {}, False), ("testclient", {}, False),
    ("192.0.2.2", {"x-forwarded-for": "127.0.0.1"}, False),
    ("127.0.0.1", {"forwarded": "for=127.0.0.1"}, False),
])
def test_direct_peer_is_required(peer, headers, allowed):
    assert local_peer(SimpleNamespace(client=SimpleNamespace(host=peer), headers=Headers(headers))) is allowed


@pytest.mark.parametrize("operation,params", [
    ("config.get", {"key": "full"}), ("config.set", {"key": "terminal"}),
    ("plugins.manage", {"action": "install"}), ("mcp.servers.add", {"name": "other", "preset": "other"}),
    ("projects.add_folder", {"path": "C:/outside"}), ("config.get", {"key": "theme", "profile": "other"}),
    ("config.get", {"key": "theme", "principal_id": "admin"}),
    ("/api/cron/jobs/{job_id}", {"updates": {"script": "arbitrary code"}}),
    ("projects.create", {"folders": [{"path": "C:/outside"}]}),
    ("session.create", {"cwd": "C:/outside", "source": "tui"}),
    ("mcp.servers.add", {"name": "fixture", "preset": "fixture", "config": {"command": "arbitrary"}}),
    ("config.set", {"key": "theme", "value": "arbitrary"}),
])
def test_typed_resource_restrictions(provision, operation, params):
    store, credential, _, _ = provision
    with pytest.raises(ServiceDenied):
        authorize_params(store.authenticate(credential), operation, params)


def test_real_http_gate_fails_closed(provision, monkeypatch):
    from hermes_cli import web_server
    store, credential, meta, _ = provision
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    async def exercise():
        transport = httpx.ASGITransport(app=web_server.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get(CAPABILITY_ROUTE)).status_code == 403
            for wrong in ("hls1.bad", "hls1." + "0" * 32 + "." + "a" * 64, credential[:-1] + ("a" if credential[-1] != "a" else "b")):
                assert (await client.get(CAPABILITY_ROUTE, headers={"Authorization": "Bearer " + wrong})).status_code == 403
            headers = {"Authorization": "Bearer " + credential}
            reply = await client.get(CAPABILITY_ROUTE, headers=headers)
            assert reply.status_code == 200
            assert reply.json()["principal_id"] == meta["principal_id"]
            assert reply.json()["auth_contract"] == CONTRACT
            assert credential not in reply.text and "verifier" not in reply.text
            for method, route in (("POST", CAPABILITY_ROUTE), ("GET", "/api/config"), ("GET", "/api/not-a-route")):
                assert (await client.request(method, route, headers=headers)).status_code == 403
            assert (await client.get(CAPABILITY_ROUTE, headers=headers | {"x-forwarded-for": "127.0.0.1"})).status_code == 403
            remote = httpx.ASGITransport(app=web_server.app, client=("192.0.2.2", 50000))
            async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as outside:
                assert (await outside.get(CAPABILITY_ROUTE, headers=headers)).status_code == 403
            store.revoke(meta["credential_id"])
            assert (await client.get(CAPABILITY_ROUTE, headers=headers)).status_code == 403
    asyncio.run(exercise())


def test_ws_admission_origin_and_other_endpoints(provision, monkeypatch):
    from hermes_cli import web_server_chat
    _, credential, _, _ = provision
    def ws(extra=None, path="/api/ws", peer="127.0.0.1"):
        return SimpleNamespace(headers=Headers({"Authorization": "Bearer " + credential} | (extra or {})),
                               url=SimpleNamespace(path=path), query_params={}, client=SimpleNamespace(host=peer))
    assert web_server_chat._ws_auth_reason(ws()) == (None, "local-service")
    for rejected in (ws({"origin": "http://127.0.0.1"}), ws({"origin": "null"}), ws(path="/api/pty"), ws(peer="192.0.2.2")):
        assert web_server_chat._ws_auth_reason(rejected)[0] == "service_denied"


def test_dispatch_and_alternate_response_paths_deny_before_work(provision):
    from tui_gateway import server
    store, credential, meta, _ = provision
    transport = SimpleNamespace(service_identity=store.authenticate(credential))
    for request in ({"jsonrpc": "2.0", "id": 1, "method": "config.set", "params": {"key": "theme", "value": "dark"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "not.a.method", "params": {}},
                    {"jsonrpc": "2.0", "id": "srq-forged", "result": {"choice": "always"}},
                    {"jsonrpc": "2.0", "id": 3, "method": "prompt.submit", "params": {"text": "must not enqueue"}}):
        reply = server.dispatch(request, transport)
        assert reply["error"]["code"] == 4030
    assert server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "gateway.capabilities", "params": {}}, transport)["result"]
    store.revoke(meta["credential_id"])
    assert server.dispatch({"jsonrpc": "2.0", "id": 5, "method": "gateway.capabilities", "params": {}}, transport)["error"]["code"] == 4030


def test_active_idle_connection_is_closed_by_external_revocation(provision):
    store, credential, meta, _ = provision
    async def exercise():
        closed = asyncio.Event()
        class Peer:
            async def close(self, code, reason):
                assert code == 4401
                closed.set()
        class Transport:
            service_identity = store.authenticate(credential)
            def close(self):
                pass
        watch = asyncio.create_task(watch_service(Peer(), Transport()))
        ServiceStore(store.path).revoke(meta["credential_id"])
        await asyncio.wait_for(closed.wait(), 3)
        await watch
    asyncio.run(exercise())


def test_queued_worker_rechecks_revocation(provision, monkeypatch):
    from tui_gateway import server
    store, token, meta, _ = provision
    queued, written = [], []
    class Future:
        def add_done_callback(self, fn): self.done = fn
    class Pool:
        def submit(self, fn):
            queued.append(fn)
            return Future()
    transport = SimpleNamespace(service_identity=store.authenticate(token), write=written.append)
    monkeypatch.setattr(server, "_pool", Pool())
    monkeypatch.setattr(server, "_LONG_HANDLERS", {"config.get"})
    request = {"jsonrpc": "2.0", "id": 10, "method": "config.get", "params": {"key": "theme"}}
    assert server.dispatch(request, transport) is None
    store.revoke(meta["credential_id"])
    queued[0]()
    assert written[0]["error"]["code"] == 4030
    from hermes_cli.backend_retirement import retirement
    retirement.release()


def test_response_grant_requires_the_owning_transport(provision, monkeypatch):
    from tui_gateway import server, server_requests
    store, token, _, _ = provision
    owner = SimpleNamespace(service_identity=store.authenticate(token))
    other = SimpleNamespace(service_identity=store.authenticate(token))
    request = server_requests.ServerRequest("fixture-session", "approval", {})
    monkeypatch.setitem(server_requests._open, request.id, request)
    monkeypatch.setitem(server._sessions, "fixture-session", {"transport": owner})
    frame = {"jsonrpc": "2.0", "id": request.id, "result": {"choice": "once"}}
    authorize_rpc(owner, frame)
    with pytest.raises(ServiceDenied): authorize_rpc(other, frame)


def test_provisioning_refuses_credential_in_granted_root(provision, tmp_path):
    store, _, _, grants = provision
    output = Path(grants["restrictions"]["project_roots"][0]) / "credential"
    output.parent.mkdir()
    with pytest.raises(ValueError): store.create("unsafe", int(time.time()) + 60, grants, output)
    assert not output.exists()


def test_operator_cli_never_prints_secret(provision, tmp_path, capsys):
    from datetime import datetime, timezone
    from hermes_cli.dashboard_auth.service_cli import main
    store, _, _, grants = provision
    manifest, output = tmp_path / "grants.json", tmp_path / "operator.credential"
    manifest.write_text(json.dumps(grants))
    expires = datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat()
    assert main(["create", "--principal", "operator-fixture", "--expires", expires,
                 "--grants", str(manifest), "--output", str(output)]) == 0
    printed = capsys.readouterr().out
    token = read_private(output).decode().strip()
    assert token not in printed and "verifier" not in printed
    cid = json.loads(printed)["credential_id"]
    for command in (["list"], ["inspect", cid], ["revoke", cid]):
        assert main(command) == 0
        assert token not in capsys.readouterr().out
    assert store.metadata(cid)["revoked"] is True


def test_live_mutations_cannot_escape_profile_or_project(provision, tmp_path, monkeypatch):
    from tui_gateway import server
    store, _, _, grants = provision
    grants = deepcopy(grants); grants["rpc"].append("prompt.submit")
    output = tmp_path / "prompt.credential"
    store.create("prompt-fixture", int(time.time()) + 60, grants, output)
    transport = SimpleNamespace(service_identity=store.authenticate(read_private(output).decode().strip()))
    request = {"jsonrpc": "2.0", "id": 7, "method": "prompt.submit", "params": {"session_id": "foreign-live", "text": "must not run"}}
    for session in ({"profile_home": str(tmp_path / "other-profile"), "cwd": grants["restrictions"]["project_roots"][0]},
                    {"profile_home": str(store.path.parent.parent), "cwd": str(tmp_path / "outside-project")}):
        monkeypatch.setitem(server._sessions, "foreign-live", session)
        assert server.dispatch(request, transport)["error"]["code"] == 4030


def test_shared_board_and_task_visibility_uses_generic_context(provision, tmp_path):
    from hermes_cli import projects_db
    from hermes_cli.dashboard_auth.local_service import CURRENT_SERVICE
    from plugins.kanban.dashboard import service_scope
    from fastapi import HTTPException
    store, token, _, grants = provision
    root = Path(grants["restrictions"]["project_roots"][0]); root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    with projects_db.connect_closing() as db:
        own = projects_db.create_project(db, name="own", folders=[str(root)])
        foreign = projects_db.create_project(db, name="foreign", folders=[str(outside)])
    context = CURRENT_SERVICE.set(store.authenticate(token))
    try:
        assert service_scope.project_allowed(own)
        assert not service_scope.project_allowed(foreign)
        assert not service_scope.project_allowed(None)
        task = SimpleNamespace(project_id=own, assignee=service_scope.launch_assignee(), workspace_path=None)
        assert service_scope.task_allowed(task)
        service_scope.require_status_owner(task)
        task.assignee = "other-profile"
        assert not service_scope.task_allowed(task)
        with pytest.raises(HTTPException): service_scope.require_status_owner(task)
    finally:
        CURRENT_SERVICE.reset(context)
