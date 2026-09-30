"""Generic, loopback-only service principals on the noninteractive token seam.

Each immutable credential carries its own grants. The durable store contains
only SHA-256 verifiers of random 384-bit secrets; the secret never enters a
principal, transport, metadata response, exception, or audit event.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any

from .base import TokenPrincipal
from .service_files import create_private, private_permissions

CONTRACT = "hermes-local-service-v1"
CAPABILITY_ROUTE = "/api/auth/service-capabilities"
TOKEN = re.compile(r"^hls1\.([a-f0-9]{32})\.([A-Za-z0-9_-]{64})$")
ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
CURRENT_SERVICE = ContextVar("hermes_local_service", default=None)


class ServiceDenied(ValueError):
    def __init__(self, reason: str = "service authorization denied"):
        super().__init__(reason)


def store_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home().resolve() / "local-services" / "identities.db"


def validate_grants(grants: dict) -> dict:
    if not isinstance(grants, dict) or set(grants) != {"http", "ws", "rpc", "responses", "restrictions"}:
        raise ValueError("Explicit HTTP, WS, RPC, response grants and restrictions required")
    for name in ("http", "ws", "rpc", "responses"):
        rows = grants[name]
        if not isinstance(rows, list) or len(rows) > 150 or any(not isinstance(x, str) or not x or "*" in x or len(x) > 250 for x in rows):
            raise ValueError("Invalid explicit grants")
        if len(set(rows)) != len(rows):
            raise ValueError("Duplicate grants")
    for row in grants["http"]:
        parts = row.split(" ")
        if len(parts) != 2 or parts[0] not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not parts[1].startswith("/api/") or "?" in row:
            raise ValueError("Invalid method and route-template grant")
    if any(x != "/api/ws" for x in grants["ws"]):
        raise ValueError("Unsupported service WebSocket route")
    if any(x not in {"approval", "clarify"} for x in grants["responses"]):
        raise ValueError("Unsupported service response grant")
    r = grants["restrictions"]
    fields = {"profile_ids", "file_root", "project_roots", "config_read_keys", "config_write_keys", "plugin_actions", "mcp_names", "cron_fields"}
    if not isinstance(r, dict) or set(r) != fields or r["profile_ids"] != ["current"]:
        raise ValueError("This contract supports only the pinned launch profile")
    for key in fields - {"file_root"}:
        if not isinstance(r[key], list) or any(not isinstance(x, str) or not x or "*" in x for x in r[key]):
            raise ValueError("Invalid typed resource restriction")
    if not isinstance(r["file_root"], str) or (r["file_root"] and not Path(r["file_root"]).is_absolute()):
        raise ValueError("Managed file root must be absolute")
    if any(not Path(x).is_absolute() for x in r["project_roots"]):
        raise ValueError("Project roots must be absolute")
    if set(r["plugin_actions"]) - {"list", "onboarding"}:
        raise ValueError("Executable plugin mutations are not supported by this contract")
    return json.loads(json.dumps(grants))


class ServiceStore:
    def __init__(self, path: Path | None = None):
        self.path = (path or store_path()).absolute()

    def initialize(self) -> None:
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(mode=0o700)
            private_permissions(directory, secure=True)
        private_permissions(directory)
        if not self.path.exists():
            create_private(self.path, b"")
        private_permissions(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, principal TEXT NOT NULL, verifier TEXT NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, grants TEXT NOT NULL)")

    def _connect(self):
        private_permissions(self.path.parent)
        private_permissions(self.path)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, principal: str, expires: int, grants: dict, output: Path) -> dict:
        if not isinstance(principal, str) or not ID.fullmatch(principal) or type(expires) is not int or expires <= time.time():
            raise ValueError("Valid principal and future expiry required")
        grants = validate_grants(grants)
        exposed_roots = [*grants["restrictions"]["project_roots"]]
        if grants["restrictions"]["file_root"]:
            exposed_roots.append(grants["restrictions"]["file_root"])
        resolved_output = output.resolve()
        service_home = self.path.parent.parent.resolve()
        if any(Path(root).resolve() == target or Path(root).resolve() in target.parents
               for root in exposed_roots for target in (resolved_output, service_home)):
            raise ValueError("Service storage and credential output must be outside granted roots")
        if grants["restrictions"]["file_root"]:
            root = Path(grants["restrictions"]["file_root"]).resolve()
            service_home = self.path.parent.parent.resolve()
            if root == service_home or root in service_home.parents:
                raise ValueError("Managed root must not contain the service identity store")
        self.initialize()
        cid, secret = secrets.token_hex(16), secrets.token_urlsafe(48)
        credential = f"hls1.{cid}.{secret}"
        # Writing happens only after secure permissions have been checked, and
        # exclusive create prevents replacing an existing staged credential.
        create_private(output, credential.encode("ascii") + b"\n")
        try:
            with self._connect() as db:
                db.execute("INSERT INTO credentials (id,principal,verifier,expires,grants) VALUES (?,?,?,?,?)",
                           (cid, principal, hashlib.sha256(credential.encode()).hexdigest(), expires, json.dumps(grants)))
        except BaseException:
            output.unlink()
            raise
        return self.metadata(cid)

    def metadata(self, cid: str | None = None) -> dict | list:
        with self._connect() as db:
            rows = db.execute("SELECT id,principal,expires,revoked,grants FROM credentials" + (" WHERE id=?" if cid else ""), (cid,) if cid else ()).fetchall()
        result = [{"credential_id": row["id"], "principal_id": row["principal"], "expires_at": row["expires"], "revoked": bool(row["revoked"]), "grants": json.loads(row["grants"]), "auth_contract": CONTRACT} for row in rows]
        if cid:
            if not result:
                raise ServiceDenied()
            return result[0]
        return result

    def authenticate(self, bearer: str) -> ServiceIdentity:
        match = TOKEN.fullmatch(bearer)
        if not match:
            raise ServiceDenied("service credential malformed")
        try:
            with self._connect() as db:
                row = db.execute("SELECT * FROM credentials WHERE id=?", (match[1],)).fetchone()
        except (OSError, sqlite3.Error, ValueError):
            raise ServiceDenied("service identity store unavailable") from None
        if row is None or not hmac.compare_digest(row["verifier"], hashlib.sha256(bearer.encode()).hexdigest()):
            raise ServiceDenied("service credential mismatch")
        if row["revoked"]:
            raise ServiceDenied("service credential revoked")
        if row["expires"] <= time.time():
            raise ServiceDenied("service credential expired")
        return ServiceIdentity(self, row["id"], row["principal"], row["expires"], validate_grants(json.loads(row["grants"])))

    def revoke(self, cid: str) -> None:
        with self._connect() as db:
            if db.execute("UPDATE credentials SET revoked=1 WHERE id=?", (cid,)).rowcount != 1:
                raise ServiceDenied()


@dataclass(frozen=True)
class ServiceIdentity:
    store: ServiceStore = field(repr=False)
    credential_id: str
    principal_id: str
    expires_at: int
    grants: dict = field(repr=False)

    def active(self) -> None:
        try:
            row = self.store.metadata(self.credential_id)
        except (OSError, sqlite3.Error, ValueError):
            raise ServiceDenied("service identity store unavailable") from None
        if row["revoked"] or row["expires_at"] <= time.time():
            raise ServiceDenied("service credential revoked or expired")

    def principal(self) -> TokenPrincipal:
        return TokenPrincipal(self.principal_id, "local-service", tuple(self.grants["http"]))

    def capability(self) -> dict:
        self.active()
        return self.store.metadata(self.credential_id)

    def require(self, kind: str, name: str) -> None:
        self.active()
        if name not in self.grants[kind]:
            raise ServiceDenied("service operation ungranted")


def local_peer(connection) -> bool:
    # Never use request_utils.client_ip: it trusts proxy headers for audit.
    # Reject forwarded service traffic even when a local proxy is the peer.
    if any(key.lower() == "forwarded" or key.lower().startswith("x-forwarded-") for key in connection.headers):
        return False
    try:
        ip = ipaddress.ip_address(connection.client.host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except (ValueError, AttributeError):
        return False


def service_bearer(connection) -> str | None:
    from starlette.datastructures import Headers
    headers = Headers(connection.headers).getlist("authorization") if isinstance(connection.headers, dict) else connection.headers.getlist("authorization")
    if len(headers) != 1:
        if headers:
            raise ServiceDenied("ambiguous service authorization")
        return None
    parts = headers[0].split(" ")
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].startswith("hls1."):
        return parts[1]
    return None


def admit_service(connection) -> ServiceIdentity | None:
    bearer = service_bearer(connection)
    if bearer is None:
        return None
    if not local_peer(connection):
        raise ServiceDenied("service authentication requires a direct loopback peer")
    return ServiceStore().authenticate(bearer)


def path_inside(raw: str, roots: list[str], *, base: str | None = None) -> None:
    if not isinstance(raw, str) or not raw or "\0" in raw or raw.lower().startswith("file:"):
        raise ServiceDenied("service path scope denied")
    target = Path(raw)
    if not target.is_absolute():
        if base is None:
            raise ServiceDenied("service path scope denied")
        target = Path(base) / target
    target = target.resolve()
    if not any(target == Path(root).resolve() or Path(root).resolve() in target.parents for root in roots):
        raise ServiceDenied("service path scope denied")


def authorize_params(identity: ServiceIdentity, operation: str, params: dict) -> None:
    r = identity.grants["restrictions"]
    if not isinstance(params, dict) or any(k in params for k in ("principal", "principal_id", "credential_id", "grants", "profile_home")):
        raise ServiceDenied("service parameter scope denied")
    if params.get("profile", "current") not in {None, "", "current"}:
        raise ServiceDenied("service profile scope denied")
    if operation in {"config.get", "config.set"}:
        if set(params) - ({"key", "session_id"} if operation == "config.get" else {"key", "value", "scope", "session_id", "confirm_expensive_model"}):
            raise ServiceDenied("service configuration parameter denied")
        if "confirm_expensive_model" in params and (params.get("key") != "model" or type(params["confirm_expensive_model"]) is not bool):
            raise ServiceDenied("service model confirmation parameter denied")
        field = "config_read_keys" if operation == "config.get" else "config_write_keys"
        if params.get("key") not in r[field]:
            raise ServiceDenied("service configuration key ungranted")
        if params.get("scope", "session") not in {"session", "global"}:
            raise ServiceDenied("service configuration scope denied")
        if params.get("key") in {"model", "reasoning"} and operation == "config.set" and not params.get("session_id"):
            raise ServiceDenied("service model changes require a live session")
        if operation == "config.set":
            choices = {"theme": {"auto", "light", "dark"}, "thinking_mode": {"collapsed", "truncated", "full"}, "busy": {"queue", "steer", "interrupt"}, "density": {"on", "off"}, "reasoning": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}}
            key, value = params.get("key"), params.get("value")
            if key in choices and (not isinstance(value, str) or value not in choices[key]):
                raise ServiceDenied("service configuration value denied")
            if key == "model" and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160} --provider [A-Za-z0-9._:-]{1,100} --session", value)):
                raise ServiceDenied("service model value denied")
    if operation == "plugins.manage" and params.get("action") not in r["plugin_actions"]:
        raise ServiceDenied("service plugin action ungranted")
    if operation == "plugins.manage" and set(params) != {"action"}:
        raise ServiceDenied("service plugin parameters denied")
    if operation.startswith("mcp.servers.") and operation not in {"mcp.servers.list", "mcp.servers.status"}:
        if params.get("name") not in r["mcp_names"] or ("preset" in params and params["preset"] != params["name"]):
            raise ServiceDenied("service MCP resource ungranted")
        allowed = {"name", "preset"} if operation == "mcp.servers.add" else ({"name", "value"} if operation == "mcp.servers.set_api_key" else {"name"})
        if set(params) != allowed:
            raise ServiceDenied("service MCP parameters denied")
    if operation == "session.create":
        if set(params) - {"source", "cols", "cwd", "cwd_explicit", "close_on_disconnect"} or params.get("source", "tui") not in {"tui", "agent_gateway"}:
            raise ServiceDenied("service session creation parameters denied")
        if not params.get("cwd"):
            if not r["project_roots"]:
                raise ServiceDenied("service session working directory required")
            params["cwd"] = r["project_roots"][0]
    if operation.startswith("projects."):
        for folder in params.get("folders", []):
            path_inside(folder.get("path") if isinstance(folder, dict) else folder, r["project_roots"])
        if params.get("use"):
            raise ServiceDenied("service global project activation denied")
        project_id = params.get("id") or params.get("project_id")
        if project_id:
            from hermes_cli import projects_db
            with projects_db.connect_closing() as db:
                project = projects_db.get_project(db, str(project_id))
                if project is None:
                    raise ServiceDenied("service project unavailable in launch profile")
                for folder in project.to_dict().get("folders", []):
                    path_inside(folder["path"], r["project_roots"])
    if operation == "session.resume" and set(params) - {"session_id", "source"}:
        raise ServiceDenied("service session resume parameters denied")
    for key in ("cwd", "path", "folder_path", "primary_path"):
        if key in params and params[key] and (operation.startswith("projects.") or operation == "session.create"):
            path_inside(params[key], r["project_roots"])
    if operation.startswith("/api/files"):
        from hermes_cli.web_server_files import _managed_files_policy
        policy = _managed_files_policy(None, create_root=False)
        if not r["file_root"] or policy.locked_root is None or policy.locked_root.resolve() != Path(r["file_root"]).resolve():
            raise ServiceDenied("service managed-file root mismatch")
        if params.get("path"):
            path_inside(params["path"], [r["file_root"]], base=r["file_root"])
    if operation.startswith("/api/cron/jobs"):
        changes = params.get("updates", {key: value for key, value in params.items() if key not in {"profile", "limit"}})
        if not isinstance(changes, dict):
            raise ServiceDenied("service cron parameter denied")
        if set(changes) - set(r["cron_fields"]):
            raise ServiceDenied("service cron parameter ungranted")
        if changes.get("deliver", "local") != "local":
            raise ServiceDenied("service cron delivery must stay local")


async def service_http(request, call_next):
    from starlette.routing import Match
    from fastapi.responses import JSONResponse
    try:
        identity = admit_service(request)
        if identity is None:
            if request.url.path == CAPABILITY_ROUTE:
                raise ServiceDenied("service credential required")
            return None
        matched = next((route for route in request.app.routes if route.matches(request.scope)[0] == Match.FULL), None)
        if matched is None or not hasattr(matched, "path"):
            raise ServiceDenied("service route unknown or method denied")
        identity.require("http", f"{request.method} {matched.path}")
        params = dict(request.query_params)
        if len(params) != len(request.query_params.multi_items()):
            raise ServiceDenied("ambiguous service parameters")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and matched.path != "/api/files/upload-stream":
            raw = await request.body()
            if len(raw) > 512 * 1024:
                raise ServiceDenied("service request exceeds limit")
            if raw:
                if request.headers.get("content-type", "").startswith("multipart/form-data"):
                    form = await request.form()
                    params.update({k: v for k, v in form.items() if isinstance(v, str)})
                else:
                    body = json.loads(raw)
                    if not isinstance(body, dict) or set(body).intersection(params):
                        raise ServiceDenied("ambiguous service parameters")
                    params.update(body)
        authorize_params(identity, matched.path, params)
        sid = params.get("session_id")
        if sid:
            from tui_gateway import server
            from hermes_constants import get_process_hermes_home
            if not isinstance(sid, str):
                raise ServiceDenied("service session parameter denied")
            session = server._sessions.get(sid)
            if session and Path(session.get("profile_home") or get_process_hermes_home()).resolve() != identity.store.path.parent.parent.resolve():
                raise ServiceDenied("service session profile denied")
        write_fields = {
            "POST /api/plugins/kanban/boards": {"slug", "name", "project_id"},
            "POST /api/plugins/kanban/tasks": {"board", "title", "body", "project_id"},
            "PATCH /api/plugins/kanban/tasks/{task_id}": {"board", "title", "body", "status"},
            "PATCH /api/sessions/{session_id}": {"title", "archived"},
        }
        permitted = write_fields.get(f"{request.method} {matched.path}")
        if permitted is not None and set(params) - permitted:
            raise ServiceDenied("service mutation parameters denied")
        if params.get("project_id"):
            authorize_params(identity, "projects.get", {"id": params["project_id"]})
        if matched.path.startswith("/api/plugins/kanban/") and matched.path != "/api/plugins/kanban/boards":
            from hermes_cli import kanban_db
            board = params.get("board")
            if not isinstance(board, str) or not board:
                raise ServiceDenied("service board selection required")
            pid = kanban_db.read_board_metadata(board).get("project_id")
            if not pid:
                raise ServiceDenied("service board project required")
            authorize_params(identity, "projects.get", {"id": pid})
        request.state.service_identity = identity
        request.state.token_principal = identity.principal()
        request.state.token_authenticated = True
        token = CURRENT_SERVICE.set(identity)
        try:
            return await call_next(request)
        finally:
            CURRENT_SERVICE.reset(token)
    except ServiceDenied as exc:
        states = {"service credential required": "missing", "service credential malformed": "malformed",
                  "service credential mismatch": "mismatch", "service credential expired": "expired",
                  "service credential revoked": "revoked", "service identity store unavailable": "unavailable"}
        return JSONResponse({"error": "service_authorization_denied", "state": states.get(str(exc), "scope_denied"),
                             "detail": "Service authorization denied"}, status_code=403)
    except (ValueError, OSError, sqlite3.Error):
        # Never echo client data or a store error into a response.
        return JSONResponse({"error": "service_authorization_denied", "detail": "Service authorization denied"}, status_code=403)


def authorize_rpc(transport, req: dict) -> None:
    identity = getattr(transport, "service_identity", None)
    if identity is None:
        return
    identity.active()
    if not isinstance(req, dict):
        raise ServiceDenied()
    method, params = req.get("method"), req.get("params", {})
    if not isinstance(method, str):
        from tui_gateway import server_requests, server
        context = server_requests.service_response_context(req)
        if context is None:
            raise ServiceDenied("service response unknown")
        response_method, sid = context
        identity.require("responses", response_method)
        session = server._sessions.get(sid)
        if session is None or session.get("transport") is not transport:
            raise ServiceDenied("service response belongs to another connection")
        return
    identity.require("rpc", method)
    authorize_params(identity, method, params)
    sid = params.get("session_id")
    if sid:
        from tui_gateway import server
        from hermes_constants import get_hermes_home
        session = server._sessions.get(sid)
        if session and Path(session.get("profile_home") or get_hermes_home()).resolve() != identity.store.path.parent.parent.resolve():
            raise ServiceDenied("service session profile denied")
        if session and method in {"prompt.submit", "session.steer", "config.set", "image.attach_bytes", "pdf.attach", "file.attach"}:
            path_inside(session.get("cwd") or "", identity.grants["restrictions"]["project_roots"])


async def watch_service(ws, transport):
    """Poll durable state so operator revocation in another process closes idle sockets.

RPC dispatch also rereads state, including in pool workers. The quarter-second
watch bounds idle-socket revocation; it does not grant an RPC grace period.
"""
    while True:
        await asyncio.sleep(0.25)
        try:
            await asyncio.to_thread(transport.service_identity.active)
        except ServiceDenied:
            from tui_gateway import server
            # Cancel active authority before disconnect's normal detach path.
            for sid, session in list(server._sessions.items()):
                if session.get("transport") is transport:
                    await asyncio.to_thread(server.handle_request, {"jsonrpc": "2.0", "id": None, "method": "session.interrupt", "params": {"session_id": sid}})
            transport.close()
            await ws.close(code=4401, reason="service credential revoked or expired")
            return
