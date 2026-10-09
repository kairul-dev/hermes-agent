"""Server-side gate, wire epoch and policy for the generic shared-runtime adapter.

The shared adapter is one additive wire surface (``session.shared.rpc`` / ``session.shared.answer``)
over the existing native handlers — never a second session engine, request store or execution owner.
It is enabled ONLY by the server-side config ``dashboard.shared_runtime.enabled`` (default false);
when it is off every native Desktop/TUI call keeps its historical behavior byte-for-byte.

When it is on (strict shared mode):
- ``gateway.ready`` advertises ``shared_runtime{schema_version, runtime_epoch}``; clients echo the
  epoch they actually observed on every adapter call and the runtime validates it exactly.
- ``session.shared.rpc`` forwards ONLY the explicit allowlist below, re-entering the native
  admission path with the same outer RPC id (the wrapped method's contract, profile scope and
  authorization still apply). Unknown or non-allowlisted methods fail closed.
- Owner/membership enforcement applies to native bypass paths too: a transport may only attach,
  read, control or answer a session whose server-verified owner is its own, and control/answer
  additionally require live transport membership. Missing or ambiguous ownership fails closed.

Ownership model: ``_transport_auth_user_id`` is the server-verified ``provider:user`` prefix (or
None for the legacy loopback token / stdio, i.e. the single local profile owner). Provider
principals are distinct from each other and from the local owner, and are never read from client
params. Sessions without a stamped owner belong to the local owner.

This module is a split sibling: ``server.py`` imports it last and :func:`register` rebinds every
definition onto the server namespace, so the usual bare names (``_load_cfg``, ``_sessions``, ...)
resolve against the live server module exactly like the other ``methods_*`` modules.
"""

from __future__ import annotations

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

SHARED_RUNTIME_SCHEMA_VERSION = 1

# Error codes: 4403 is the gateway's established fail-closed authorization denial
# (browser_control); 4401 marks a stale/missing runtime epoch; 4400 an identity that contradicts
# a known native fact (never a first-writer-slot consumption).
ERR_SHARED_IDENTITY = 4400
ERR_SHARED_FORBIDDEN = 4403
ERR_SHARED_EPOCH = 4401

# ── allowlist ───────────────────────────────────────────────────────────────────────────────────
# Explicitly enumerated: a name not listed here is never forwarded, even if the native handler
# exists. Credential/admin surfaces (model.save_key, MCP credential setters, profile/system/config
# admin writes, privileged connector setup, provider credentials) are intentionally absent, as are
# ordinary session chat surfaces nobody renders in shared mode.
_SHARED_RPC_ALLOWLIST = frozenset({
    # attach / lifecycle
    "session.create", "session.resume", "session.activate", "session.close",
    # credential-free reads needed to render ordinary sessions
    "session.list", "session.active_list", "session.history", "session.events.since",
    "session.info.get", "orchestration.get", "model.options",
    "commands.catalog", "tools.list", "session.context_breakdown", "session.usage", "session.status",
    # controls
    "prompt.submit", "session.interrupt", "session.steer",
    # native answers (the acknowledged session.shared.answer RPC is the shared path; these stay
    # available for clients that send a response frame through the bridge)
    "approval.pending", "approval.received", "approval.respond", "clarify.lock", "request.answer",
})

# Native methods whose direct (bypassing the adapter) invocation must still be authorized in
# strict shared mode. Attach methods only need the owner check — attaching is how a verified
# owner becomes a member; every control/read/answer additionally requires live membership.
_SHARED_NATIVE_ATTACH_METHODS = frozenset({"session.resume", "session.activate"})
_SHARED_NATIVE_MEMBER_METHODS = frozenset({
    "prompt.submit", "session.interrupt", "session.steer", "session.close",
    "session.usage", "session.context_breakdown", "session.status",
    "session.history", "session.events.since", "session.info.get", "orchestration.get", "orchestration.set",
    "session.start_chat",
    "approval.pending", "approval.received",
    "approval.respond", "clarify.lock", "request.answer",
})
_SHARED_NATIVE_COLLECTION_METHODS = frozenset({"session.list", "session.active_list", "session.most_recent"})
# Credential-free, session-aware reads exposed by the shared adapter. Without session_id these read
# the launch-profile view; with one they require the same live owner membership as other session reads.
_SHARED_NATIVE_SESSION_READ_METHODS = frozenset({"commands.catalog", "model.options", "tools.list"})
# This endpoint validates its exact session/stored/request identity and owner before settlement itself.
_SHARED_NATIVE_SELF_GUARDED_METHODS = frozenset({"session.shared.answer"})
_SHARED_NATIVE_SESSION_SCOPE_FIELDS = frozenset({
    "session_id", "parent_session_id", "session_key", "stored_session_id", "current_session_id",
})
# Answer RPCs carry their target request id instead of a session id.
_SHARED_REQUEST_KEYED_METHODS = {"clarify.lock": "request_id", "request.answer": "id"}
# A member read of a session that is not live here still answers natively (its replay ring is
# simply empty/truncated), so the guard only enforces identity when the session exists.
_SHARED_MISSING_SESSION_PASSTHROUGH = frozenset({"session.events.since"})


def _shared_runtime_policy_state() -> bool | None:
    """Read the launch process's policy, never the caller's current profile scope.

    ``None`` means the policy could not be established (including malformed config); security
    decisions must fail closed rather than treating that state as ordinary mode.
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective

        cfg = load_user_config_effective(Path(_hermes_home) / "config.yaml", fail_closed=True)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    dashboard = cfg.get("dashboard", {})
    if not isinstance(dashboard, dict):
        return None
    shared = dashboard.get("shared_runtime", {})
    if not isinstance(shared, dict):
        return None
    try:
        return is_truthy_value(shared.get("enabled"), default=False)
    except Exception:
        return None


def shared_runtime_enabled() -> bool:
    """``dashboard.shared_runtime.enabled`` from trusted process config (default false)."""
    return _shared_runtime_policy_state() is True


def shared_runtime_epoch() -> str:
    """The exact epoch token advertised in ``gateway.ready`` and required back on adapter calls."""
    from tui_gateway.event_replay import replay_epoch

    return replay_epoch()


def _shared_gateway_ready_extra() -> dict:
    """Extra ``gateway.ready`` payload keys: the advertisement, or nothing when disabled."""
    if not shared_runtime_enabled():
        return {}
    return {"shared_runtime": {"schema_version": SHARED_RUNTIME_SCHEMA_VERSION,
                               "runtime_epoch": shared_runtime_epoch()}}


# ── identity policy ─────────────────────────────────────────────────────────────────────────────


def _shared_owner_key(transport) -> tuple[bool, str | None]:
    """``(verifiable, owner key)`` for a calling transport.

    ``None`` is the verified legacy-local owner (loopback token / stdio); a provider principal is
    ``<provider>:<user id>``. A present-but-malformed ``auth_identity`` is ambiguous ownership and
    reports unverifiable so every caller fails closed.
    """
    if transport is None:
        return False, None
    identity = getattr(transport, "auth_identity", None)
    if identity is None:
        return True, None
    if _is_authenticated_identity(identity):
        return True, _transport_auth_user_id(transport)
    return False, None


def _shared_record_owner_matches(transport, record: dict, owner_field: str) -> bool:
    """True only for an exact server-stored owner match; absent/malformed metadata is ambiguous."""
    verifiable, caller = _shared_owner_key(transport)
    if not verifiable or not isinstance(record, dict) or owner_field not in record:
        return False
    owner = record[owner_field]
    if owner is None:
        return caller is None  # an explicitly stored NULL is the local-process owner
    if not isinstance(owner, str) or not owner or owner.strip() != owner:
        return False
    return caller == owner


def _shared_session_denial(transport, session: dict, *, require_membership: bool) -> str | None:
    """Why this transport may not act on this session, or None when it may."""
    verifiable, caller = _shared_owner_key(transport)
    if not verifiable:
        return "the calling transport has no verifiable server identity"
    owner = _session_auth_user_id(session)
    if caller != owner:
        return "the session belongs to a different owner"
    if require_membership and not _session_transport_contains(session, transport):
        return "the calling transport is not attached to this session"
    return None


def _shared_safe_session_config_keys() -> frozenset:
    """The session-scoped ``config.set`` keys shared-mode clients may write through the adapter."""
    from tui_gateway import methods_config_set

    return methods_config_set._SESSION_SCOPED_KEYS


def _shared_native_contract_has_session_scope(method: str) -> bool:
    """Whether this registered RPC accepts an identifier that targets stored/live session state."""
    contract = _contracts.METHODS.get(method)
    params = getattr(contract, "params", None)
    fields = getattr(params, "model_fields", {})
    return bool(_SHARED_NATIVE_SESSION_SCOPE_FIELDS.intersection(fields))


def _shared_reasoning_display_alias(value) -> bool:
    """Whether config.set reasoning selects a profile-wide display alias, not session effort."""
    from tui_gateway import methods_config_set

    word = str(value or "").strip().lower()
    return any(word in words for words, _reported, _fields, _thinking, _show
               in methods_config_set._REASONING_DISPLAY_WORDS)


def _shared_find_session_by_approval(request_id: str) -> dict | None:
    """The live session holding the approval-queue entry *request_id* (its queue id, not an srq id)."""
    if not request_id:
        return None
    try:
        from tools.approval import list_gateway_approvals

        with _sessions_lock:
            live = list(_sessions.items())
        for _sid, session in live:
            key = str(session.get("session_key") or "")
            if key and any(str(pending.get("request_id") or "") == request_id
                           for pending in list_gateway_approvals(key)):
                return session
    except Exception:
        logger.debug("shared runtime approval target lookup failed", exc_info=True)
    return None


def _shared_guard_session(method: str, params: dict) -> dict | None:
    """The live session a guarded native method targets, or None when it cannot be resolved."""
    sid = str(params.get("session_id") or "")
    if sid:
        return _sessions.get(sid)
    if (key := _SHARED_REQUEST_KEYED_METHODS.get(method)) is not None:
        from tui_gateway import server_requests

        meta = server_requests.open_request_identity(str(params.get(key) or ""))
        return _sessions.get(meta[0]) if meta else None
    if method == "approval.respond":
        return _shared_find_session_by_approval(str(params.get("request_id") or ""))
    return None


def _shared_stored_session_owner_matches(transport, params: dict, stored_id: str) -> bool:
    """Read only the durable owner row before a native method can copy its transcript."""
    if not stored_id or stored_id.strip() != stored_id:
        return False
    try:
        with _profile_db(params) as db:
            row = db.get_session(stored_id) if db is not None else None
    except Exception:
        return False
    return _shared_record_owner_matches(transport, row, "user_id")


def _shared_response_authorized(sid: str, transport) -> bool:
    """Strict shared mode: only a verified owner that is a live member of *sid* may settle its open
    native requests (raw response frames, ``request.answer``, ``NOT_SHOWN`` declines). Ordinary mode
    admits every transport — the historical, transport-agnostic Desktop behavior."""
    policy = _shared_runtime_policy_state()
    if policy is False:
        return True
    if policy is None:
        return False
    session = _sessions.get(sid)
    if session is None:
        return False  # nothing verifiable owns this wait: fail closed
    return _shared_session_denial(transport, session, require_membership=True) is None


def _shared_native_admission_error(rid, method: str, params: dict) -> dict | None:
    """Strict shared-mode authorization for one native RPC.

    Returns the JSON-RPC error to answer with instead of running the handler, or None to admit.
    Disabled (ordinary mode): always None — zero behavior change for Desktop/TUI.
    """
    policy = _shared_runtime_policy_state()
    if policy is False:
        return None
    if policy is None:
        guarded = (_SHARED_NATIVE_ATTACH_METHODS | _SHARED_NATIVE_MEMBER_METHODS
                   | _SHARED_NATIVE_COLLECTION_METHODS | _SHARED_NATIVE_SESSION_READ_METHODS
                   | {"config.set", "session.branch_stored", "session.create"})
        if method in guarded or _shared_native_contract_has_session_scope(method):
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime policy is unavailable; this request was denied")
        return None
    if method in _SHARED_NATIVE_COLLECTION_METHODS:
        verifiable, _owner = _shared_owner_key(current_transport())
        if not verifiable:
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: a server-verified transport is required")
        return None  # collection rows are filtered by their persisted/live owner before serialization
    if method in _SHARED_NATIVE_SESSION_READ_METHODS:
        verifiable, _owner = _shared_owner_key(current_transport())
        if not verifiable:
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: a server-verified transport is required")
        if not str(params.get("session_id") or ""):
            return None  # an explicit credential-free launch-profile read
        session = _shared_guard_session(method, params)
        if session is None:
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: session not found or not owned by this transport")
        denial = _shared_session_denial(current_transport(), session, require_membership=True)
        if denial:
            return _err(rid, ERR_SHARED_FORBIDDEN, f"shared runtime: {denial}")
        return None
    if method == "session.branch_stored":
        parent_id = str(params.get("parent_session_id") or "")
        if not _shared_stored_session_owner_matches(current_transport(), params, parent_id):
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: stored parent is not owned by this transport")
        return None
    if method in _SHARED_NATIVE_ATTACH_METHODS:
        membership = False
    elif method in _SHARED_NATIVE_MEMBER_METHODS:
        membership = True
    elif method == "config.set":
        # Session-scoped writes only reach a resolved live session, and only for the safe
        # session-scoped keys; global display/admin writes stay native-owned.
        if not str(params.get("session_id") or "") or str(params.get("scope") or "").strip().lower() == "global":
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: process-scoped config writes require a trusted global owner")
        if (str(params.get("key") or "") == "reasoning"
                and _shared_reasoning_display_alias(params.get("value"))):
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: reasoning display aliases write profile-wide configuration")
        if str(params.get("key") or "") not in _shared_safe_session_config_keys():
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: this config key is not available as a session-scoped write")
        membership = True
    elif method in _SHARED_NATIVE_SELF_GUARDED_METHODS:
        return None  # the handler validates full request identity and owner before state settlement
    elif method == "session.create":
        verifiable, _owner = _shared_owner_key(current_transport())
        if not verifiable:
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: a server-verified transport is required")
        return None
    else:
        if _shared_native_contract_has_session_scope(method):
            return _err(rid, ERR_SHARED_FORBIDDEN,
                        "shared runtime: this session-scoped native method is not classified")
        return None
    session = _shared_guard_session(method, params)
    if session is None:
        if method in _SHARED_NATIVE_ATTACH_METHODS or method in _SHARED_MISSING_SESSION_PASSTHROUGH:
            return None
        return _err(rid, ERR_SHARED_FORBIDDEN,
                    "shared runtime: session not found or not owned by this transport")
    denial = _shared_session_denial(current_transport(), session, require_membership=membership)
    if denial:
        return _err(rid, ERR_SHARED_FORBIDDEN, f"shared runtime: {denial}")
    return None


# ── session.shared.rpc ──────────────────────────────────────────────────────────────────────────


def _shared_rpc_allowed(inner_method: str, inner_params: dict) -> bool:
    if inner_method in _SHARED_RPC_ALLOWLIST:
        return True
    if inner_method == "config.set":
        # "safe session config.set": a live session id and one of the session-scoped keys, never
        # a global write (the native handler applies the session-scope semantics itself).
        return (bool(str(inner_params.get("session_id") or ""))
                and str(inner_params.get("key") or "") in _shared_safe_session_config_keys()
                and str(inner_params.get("scope") or "").strip().lower() != "global")
    return False


@method("session.shared.rpc")
def _shared_rpc(rid, params: dict) -> dict:
    """Invoke one allowlisted native handler under the exact advertised runtime epoch."""
    if not shared_runtime_enabled():
        return _err(rid, ERR_SHARED_FORBIDDEN, "shared runtime is not enabled")
    epoch = params.get("runtime_epoch")
    if not isinstance(epoch, str) or not epoch or epoch != shared_runtime_epoch():
        return _err(rid, ERR_SHARED_EPOCH,
                    "shared runtime: stale or missing runtime epoch — refresh the client")
    inner_method = str(params.get("method") or "")
    inner_params = params.get("params")
    if not inner_method or not isinstance(inner_params, dict):
        return _err(rid, 4000, "shared runtime: method and an object params are required")
    if not _shared_rpc_allowed(inner_method, inner_params):
        return _err(rid, ERR_SHARED_FORBIDDEN,
                    f"shared runtime: {inner_method} is not available through the shared adapter")
    if current_transport() is None:
        return _err(rid, ERR_SHARED_FORBIDDEN, "shared runtime: a server-verified transport is required")
    # Same outer RPC id: the native admission path re-validates the wrapped method's contract,
    # applies its profile scope and shared-mode authorization, and returns result/error unchanged.
    return _handle_admitted_request({"jsonrpc": "2.0", "id": rid, "method": inner_method,
                                     "params": inner_params})


# ── session.shared.answer ───────────────────────────────────────────────────────────────────────


@method("session.shared.answer")
def _shared_answer(rid, params: dict) -> dict:
    """Answer one open native request by its exact identity; validate everything BEFORE settling."""
    if not shared_runtime_enabled():
        return _err(rid, ERR_SHARED_FORBIDDEN, "shared runtime is not enabled")
    epoch = params.get("runtime_epoch")
    if not isinstance(epoch, str) or not epoch or epoch != shared_runtime_epoch():
        return _err(rid, ERR_SHARED_EPOCH,
                    "shared runtime: stale or missing runtime epoch — refresh the client")
    sid = str(params.get("session_id") or "")
    session = _sessions.get(sid)
    if session is None:
        return _err(rid, 4001, "shared runtime: session not found")
    stored = str(params.get("stored_session_id") or "")
    durable = str(_session_lookup_key(session, fallback=sid))
    if not stored or stored != durable or str(_orchestration_stored_id(session) or "") != durable:
        return _err(rid, ERR_SHARED_IDENTITY,
                    "shared runtime: stored_session_id does not match the live session")
    denial = _shared_session_denial(current_transport(), session, require_membership=True)
    if denial:
        return _err(rid, ERR_SHARED_FORBIDDEN, f"shared runtime: {denial}")
    request_id = str(params.get("request_id") or "")
    request_type = str(params.get("request_type") or "")
    result = params.get("result")
    if not request_id or not request_type or not isinstance(result, dict):
        return _err(rid, 4000, "shared runtime: request_id, request_type and an object result are required")
    from tui_gateway import server_requests

    try:
        status = server_requests.answer_from_shared(
            session_id=sid, request_id=request_id, request_type=request_type, result=dict(result))
    except ValueError as exc:
        return _err(rid, ERR_SHARED_IDENTITY, f"shared runtime: {exc}")
    return _ok(rid, {"status": status})


# ── session attention (ported state machine: tui_gateway/session_attention.py) ─────────────────
# Native facts derive from ``server_requests._open`` ONLY (register/settle/expiry/cancel/final-lock
# observers below + the terminal message frames); the approval queue is never scanned again, so an
# approval wait is one pending identity (its srq frame), never two.


def _session_attention_facts(sid: str, session: dict | None) -> dict:
    """Live facts the attention state machine derives from."""
    from tui_gateway import server_requests

    pending_types, pending_ids = server_requests.open_request_facts(sid)
    if session is None:
        return {"running": False, "live_turn": False, "starting": False,
                "pending_types": pending_types, "pending_request_ids": pending_ids}
    inflight = session.get("inflight_turn")
    ready = session.get("agent_ready")
    return {
        "running": bool(session.get("running")),
        # An in-flight turn record without a terminal error status is a live turn.
        "live_turn": isinstance(inflight, dict) and inflight.get("status") != "error",
        "starting": bool(ready is not None and not ready.is_set() and session.get("agent_build_started")),
        "pending_types": pending_types,
        "pending_request_ids": pending_ids,
    }


def _session_attention_snapshot(sid: str, session: dict | None) -> dict:
    """The canonical attention projection for one session (never emits, never bumps a revision)."""
    from tui_gateway import session_attention as _sa

    return _sa.snapshot(sid, **_session_attention_facts(sid, session))


def _session_attention_pending_rows(sid: str) -> list[dict]:
    """Safe ``{request_id, type}`` identity for every unresolved native wait (metadata only)."""
    from tui_gateway import server_requests

    pending_types, pending_ids = server_requests.open_request_facts(sid)
    return [{"request_id": rid, "type": kind} for kind, rid in zip(pending_types, pending_ids)]


def _session_attention_touch(sid: str) -> None:
    """Reconcile one session from the live registries; emits once when the state actually moved."""
    from tui_gateway import session_attention as _sa

    try:
        session = _sessions.get(sid)
        _sa.reconcile(sid, **_session_attention_facts(sid, session))
    except Exception:
        logger.debug("session attention reconcile failed for %s", sid, exc_info=True)


def _session_request_observer(sid: str, phase: str) -> None:
    """Request-transition observer (server_requests seam): reconcile attention exactly once per
    committed transition. Runs OUTSIDE the native request lock; never raises into the request path."""
    _session_attention_touch(sid)


def _session_attention_note_event(event: str, sid: str, payload) -> None:
    """Drive the state machine from terminal turn frames (only ``message.start``/``message.complete``
    reach here, wired inside ``_emit``); an unrelated error event can never mark FAILED."""
    from tui_gateway import session_attention as _sa

    session = _sessions.get(sid)
    facts = _session_attention_facts(sid, session)
    if event == "message.start":
        _sa.note_message_start(sid, **facts)
    else:
        raw = payload.get("status") if isinstance(payload, dict) else None
        _sa.note_message_complete(sid, raw, **facts)


def _shared_session_projection_identity(sid: str) -> dict | None:
    """Trusted session-scoped identity tuple for a live record; no event is projected after it is gone."""
    if not shared_runtime_enabled() or not isinstance(sid, str) or not sid:
        return None
    with _sessions_lock:
        session = _sessions.get(sid)
    if not isinstance(session, dict) or session.get("_finalized") or "auth_user_id" not in session:
        return None
    owner = session.get("auth_user_id")
    if owner is not None and (not isinstance(owner, str) or not owner or owner.strip() != owner):
        return None
    stored_id = str(session.get("session_key") or getattr(session.get("agent"), "session_id", None) or "")
    epoch = shared_runtime_epoch()
    if not stored_id or stored_id.strip() != stored_id or not epoch:
        return None
    with _sessions_lock:
        if _sessions.get(sid) is not session:
            return None
    return {"runtime_epoch": epoch, "session_id": sid, "stored_session_id": stored_id}


def _session_attention_forward(sid: str, payload: dict) -> None:
    """Transition sink for ``session_attention``: announce the canonical projection once per change.

    The attention WIRE surface belongs to the shared-runtime contract: it is enabled exactly when
    ``dashboard.shared_runtime.enabled`` is (the state machine itself always runs, so toggling the
    gate on shows current state immediately). Ordinary Desktop/TUI mode emits no new frames.
    """
    identity = _shared_session_projection_identity(sid)
    if identity is None:
        return
    _emit("session.attention", sid, {**payload, **identity})


def _shared_backend_owned_work_or_wait(sid: str, session: dict | None) -> bool:
    """Opt-in shared-mode exemption input: does this clientless session still hold backend-owned
    work (a live turn) or a pending native wait?

    The WS-orphan reaper defers while true and never interrupts it — losing every browser transport
    must not kill valid backend-owned work. Boundedness is inherent: a turn settles on its own (or
    is explicitly stopped), a wait has its own deadline, and an idle session reads False and keeps
    the ordinary bounded cleanup.
    """
    if not shared_runtime_enabled() or not session:
        return False
    if session.get("running"):
        return True
    from tui_gateway import server_requests

    pending_types, _pending_ids = server_requests.open_request_facts(sid)
    return bool(pending_types)


def register(server) -> None:
    """Publish this module's helpers/handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
    # Configure the state machine's transition sink with the SERVER-namespaced emitter (the rebound
    # copy, so ``_emit`` resolves through server.py like every other split module).
    from tui_gateway import session_attention as _sa

    _sa.configure_emitter(server._session_attention_forward)
