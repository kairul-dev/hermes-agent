"""Server→client JSON-RPC requests: the backend asks the renderer a question and waits for the
response frame carrying the same ``id``.

JSON-RPC is peer-to-peer; this is the backend's half. Every "ask the renderer" bridge (clarify,
approval, sudo, secret, vault prompts, desktop GUI reads, MCP setup consent, the tour) is one
:func:`send` (blocking) or :func:`send_async` (queue-backed approvals) and one response frame from
the client — no paired ``*.request`` notification / ``*.respond`` method, no per-kind ``*.expire``.

Ids are ``srq-<12 hex>``: strings never collide with client-minted integer ids, and the random
part keeps a compute-host child's requests distinct from the parent's when both reach one socket.
A request that times out or is cancelled (interrupt, session close, shutdown) emits ONE
``request.cancel {id, method, reason}`` notification so every renderer tears the card down the
same way. A response for an id that is no longer open is dropped — the wait already returned.

Reconnect: unanswered requests are returned as ``open_requests`` by ``session.resume`` /
``session.activate`` / ``session.events.since`` (:func:`open_requests`); the shared TypeScript
channel re-delivers them as if they had just arrived, so the notification replay ring never
has to carry "a question still waiting for an answer".

Batch clarify keeps per-question locks (``clarify.lock`` → :func:`lock_answer`): answers stay
editable until every question is locked, locked answers survive a timeout, and the last lock
resolves the request with the full answer set.

Window-owned bridges (``preview.read`` / ``preview.act`` / ``terminal.read`` / ``window.read`` /
``tour``) are answered only by the window showing the session; every other attached window declines
with :data:`NOT_SHOWN_CODE`. A decline does not settle the request — the owner may still answer — until
every answering client attached to the session has declined; then the request resolves with
:data:`NOT_SHOWN_MESSAGE` at once instead of the agent waiting out the deadline (#119333).

Capability: a client says once per connection that it answers server→client requests
(``client.capabilities {server_requests: true}`` → :func:`advertise`). A WebSocket client that never
did is a build older than this half of the protocol — it drops the frame silently and the agent
would wait the full deadline (clarify's 300s) for nothing — so :func:`send` / :func:`send_async`
return the same ``None`` an error response produces without writing the frame (#112548).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ServerRequest:
    __slots__ = (
        "answered",
        "created_at",
        "declined",
        "event",
        "id",
        "locked",
        "method",
        "on_result",
        "params",
        "qids",
        "result",
        "shared_identity",
        "shared_session",
        "sid",
    )

    def __init__(self, sid: str, method: str, params: dict, *, qids: list[str] | None = None,
                 on_result: Callable[[dict | None], None] | None = None) -> None:
        self.id = f"srq-{uuid.uuid4().hex[:12]}"
        self.sid = sid
        self.method = method
        self.params = dict(params)
        self.event = threading.Event()
        self.result: dict | None = None
        self.answered = False
        self.created_at = time.time()
        # Batch clarify: question ids still to lock, and the answers locked so far.
        self.qids = list(qids) if qids else None
        self.locked: dict[str, str | None] = {}
        self.on_result = on_result
        # Client transports that answered NOT_SHOWN_CODE (no window there shows this session).
        self.declined: set = set()
        # Captured from server-owned live metadata at registration; never reconstructed from response params.
        self.shared_identity: dict | None = None
        self.shared_session: Any = None

    def frame(self) -> dict:
        return {"jsonrpc": "2.0", "id": self.id, "method": self.method,
                "params": {"session_id": self.sid, **self.params}}

    def snapshot(self) -> dict:
        """``open_requests`` entry: the request as sent, plus the batch answers locked so far so a
        reconnecting client restores its ✓ state."""
        params = {"session_id": self.sid, **self.params}
        if self.locked:
            params["answers"] = dict(self.locked)
        return {"id": self.id, "method": self.method, "params": params}


_lock = threading.Lock()
_open: dict[str, ServerRequest] = {}

# Recent-settlement identities: request id → (session id, method, settled_at). Bounded, in-memory
# and secret-free (no params, no results) — it is only how a repeated shared answer is told apart
# from an id this process never served. NOT a request store: nothing here can settle anything.
_settled_recent: "OrderedDict[str, tuple[str, str, float]]" = OrderedDict()
_SETTLED_RECENT_MAX = 256
_SETTLED_RECENT_TTL_S = 900.0


def _remember_settled_locked(req: ServerRequest) -> None:
    """Record one settlement (caller holds ``_lock``, same critical section as the pop)."""
    now = time.time()
    _settled_recent[req.id] = (req.sid, req.method, now)
    while len(_settled_recent) > _SETTLED_RECENT_MAX:
        _settled_recent.popitem(last=False)
    for rid, row in list(_settled_recent.items()):
        if now - row[2] > _SETTLED_RECENT_TTL_S:
            _settled_recent.pop(rid, None)


def settled_recently(request_id: str) -> tuple[str, str] | None:
    """``(sid, method)`` of a request this process settled recently, or None."""
    with _lock:
        row = _settled_recent.get(request_id)
    return (row[0], row[1]) if row else None


# Frame sinks, bound by ``bind_sinks`` from server.py at import time (like the method_ctx split
# modules): importing server back from here would pick a different module object under the test
# fixtures that patch ``sys.modules`` around the server import.
_write: Callable[[dict], Any] = lambda frame: None
_emit: Callable[[str, str, dict], Any] = lambda event, sid, payload: None
# ``answerable(sid)``: False only when every client attached to the session is a build that never
# advertised handling server→client requests (session_transports.py::_session_client_answers_requests).
_answerable: Callable[[str], bool] = lambda sid: True
# ``clients(sid)``: the attached client transports that answer server→client requests — the set whose
# unanimous NOT_SHOWN_CODE decline settles a window-owned request (session_transports.py).
_clients: Callable[[str], list] = lambda sid: []
# ``authorize(sid, transport)``: strict-mode gate for RESPONSE settlement (raw response frames,
# request.answer, declines). Bound by server.py to the shared-runtime owner/membership policy;
# the default admits everything (ordinary mode), keeping the historical path byte-identical.
_authorize_response: Callable[[str, Any], bool] = lambda sid, transport: True  # noqa: E731
# ``shared_wire()``: the shared-runtime wire epoch — the exact advertised epoch while the shared
# runtime is enabled, None while it is off. Gates the NEW resolution projection so ordinary mode
# emits exactly its historical frames; the epoch rides the projection like every shared payload.
_shared_wire: Callable[[], str | None] = lambda: None  # noqa: E731
_capture_shared_identity: Callable[[str], tuple[dict | None, Any]] = lambda sid: (None, None)  # noqa: E731
_shared_identity_current: Callable[[str, dict, Any], bool] = lambda sid, identity, session: False  # noqa: E731

# Error code a client answers when none of its windows shows the request's session, and the refusal the
# tool reports once every attached client said so. Mirrored in apps/desktop server-requests.ts.
NOT_SHOWN_CODE = 4404
NOT_SHOWN_MESSAGE = ("No Hermes Desktop window is showing this chat, so its preview, terminal and tour are out "
                     "of reach. Ask the user to open this chat in the Desktop app, then retry.")

# Request-transition observers ``cb(sid, phase)``, invoked OUTSIDE ``_lock`` after a transition has
# committed: phase ∈ ``register | settled | expired | cancelled | locked``. server.py binds the
# attention reconcile; an observer failure is logged and can never affect the request machinery.
_observers: list[Callable[[str, str], None]] = []


def add_request_observer(cb: Callable[[str, str], None]) -> None:
    """Register one transition observer (idempotent). See the phase list above."""
    if cb not in _observers:
        _observers.append(cb)


def _notify(sid: str, phase: str) -> None:
    for cb in tuple(_observers):
        try:
            cb(sid, phase)
        except Exception:
            logger.warning("server request observer failed sid=%s phase=%s", sid, phase, exc_info=True)


# Client transports that sent ``client.capabilities {server_requests: true}`` (identity set: StdioTransport
# has __slots__ and cannot be weak-referenced; ws.py forgets a peer on disconnect).
_answering_clients: set = set()


def bind_sinks(write_json: Callable[[dict], Any], emit: Callable[[str, str, dict], Any],
               answerable: Callable[[str], bool], clients: Callable[[str], list] | None = None) -> None:
    global _write, _emit, _answerable, _clients
    _write, _emit, _answerable = write_json, emit, answerable
    if clients is not None:
        _clients = clients


def bind_response_authorizer(authorize: Callable[[str, Any], bool]) -> None:
    """Bind the strict-mode settlement gate ``authorize(sid, transport) -> bool`` (server.py).
    False drops a response frame/decline from an unauthorized transport; the request stays open."""
    global _authorize_response
    _authorize_response = authorize


def bind_shared_wire(enabled: Callable[[], str | None]) -> None:
    """Bind the shared-runtime wire gate ``enabled() -> epoch | None`` (server.py): the additive
    ``request.resolved`` projection is emitted only while the shared runtime is enabled, and it
    carries the exact advertised runtime epoch alongside the native session identity."""
    global _shared_wire
    _shared_wire = enabled


def bind_shared_identity(capture: Callable[[str], tuple[dict | None, Any]],
                         is_current: Callable[[str, dict, Any], bool]) -> None:
    """Bind trusted live-session identity capture/validation; client params never supply these fields."""
    global _capture_shared_identity, _shared_identity_current
    _capture_shared_identity, _shared_identity_current = capture, is_current


def _projection_identity(req: ServerRequest) -> dict | None:
    """Public identity tuple only when the exact registration-time live session is still present."""
    epoch = _shared_wire()
    identity, session = req.shared_identity, req.shared_session
    if not epoch or not isinstance(identity, dict) or identity.get("runtime_epoch") != epoch or session is None:
        return None
    try:
        if not _shared_identity_current(req.sid, identity, session):
            return None
    except Exception:
        return None
    return {key: identity[key] for key in ("runtime_epoch", "session_id", "stored_session_id")}


def _emit_cancel(req: ServerRequest, reason: str, *, identity: dict | None = None) -> None:
    wire = _shared_wire()
    if wire is None:
        # A request created in strict mode must not fall back to an ordinary/global route if its
        # session disappeared or policy changed before cancellation.
        if req.shared_identity is not None:
            return
        payload = {"id": req.id, "method": req.method, "reason": reason}
    else:
        identity = identity or _projection_identity(req)
        if identity is None:
            return
        payload = {"id": req.id, "method": req.method, "reason": reason, **identity}
    _emit("request.cancel", req.sid, payload)


def _emit_resolved(req: ServerRequest, outcome: str, *, identity: dict | None = None) -> bool:
    """Secret-free resolution projection, emitted only for a live exact shared-session identity."""
    if not _shared_wire():
        return False
    identity = identity or _projection_identity(req)
    if identity is None:
        return False
    _emit("request.resolved", req.sid,
          {"id": req.id, "method": req.method, "outcome": outcome, **identity})
    return True


def _emit_answered(req: ServerRequest) -> None:
    """Two wire projections for one already-committed semantic settlement; never calls cancel()."""
    identity = _projection_identity(req)
    if identity is None:
        return
    _emit_resolved(req, "answered", identity=identity)
    # Legacy Desktop clears special request cards only on request.cancel(reason="resolved").
    _emit_cancel(req, "resolved", identity=identity)


def _authorized_response(sid: str, transport: Any) -> bool:
    authorize = _authorize_response
    try:
        return bool(authorize(sid, transport))
    except Exception:
        # A broken policy must never settle anything: fail closed and log loudly.
        logger.warning("server request response authorization failed for %s; dropping", sid, exc_info=True)
        return False


def advertise(transport: Any, server_requests: bool) -> None:
    """Record whether *transport*'s client answers server→client requests (``client.capabilities``)."""
    with _lock:
        if server_requests:
            _answering_clients.add(transport)
        else:
            _answering_clients.discard(transport)


def forget(transport: Any) -> None:
    """Drop a disconnected transport's advertisement."""
    with _lock:
        _answering_clients.discard(transport)


def answers_requests(transport: Any) -> bool:
    with _lock:
        return transport in _answering_clients


def _unanswerable(method: str, sid: str) -> bool:
    if _answerable(sid):
        return False
    logger.info("server request %s for %s not sent: the attached client predates server→client requests "
                "(update the Hermes app)", method, sid)
    return True


def _register(req: ServerRequest) -> None:
    from tui_gateway.contracts import registry as contracts

    contract = contracts.SERVER_REQUESTS.get(req.method)
    if contract is None:
        raise RuntimeError(f"server request {req.method!r} has no contract in tui_gateway/contracts")
    _, problem = contracts.validate_params(contract, {"session_id": req.sid, **req.params})
    if problem is not None:
        raise ValueError(problem)  # a key the renderer's typed handler would never read: our bug
    if _shared_wire() is not None:
        try:
            req.shared_identity, req.shared_session = _capture_shared_identity(req.sid)
        except Exception:
            # A failed projection identity never changes native request authority; it only suppresses
            # the additive shared projections. Do not log exception text from a live session record.
            logger.warning("server request %s (%s): shared projection identity unavailable", req.id, req.method)
    with _lock:
        _open[req.id] = req
    _write(req.frame())
    _notify(req.sid, "register")


def send(method: str, sid: str, params: dict, *, timeout: float | None,
         qids: list[str] | None = None) -> dict | None:
    """Send one request and block for the response ``result`` (a dict).

    Returns ``None`` when the renderer never answered (timeout, cancel, or an error response — e.g.
    a client without a handler for ``method``). ``timeout`` semantics: None → wait until answered or
    cancelled, 0 → return immediately, > 0 → bounded wait. A batch (``qids``) that settled returns
    ``{"answers": <locked so far>, "outcome"}`` (``submitted`` / ``cancelled`` / ``timed_out``).
    """
    if _unanswerable(method, sid):
        return None
    req = ServerRequest(sid, method, params, qids=qids)
    _register(req)
    try:
        req.event.wait(timeout)
    except BaseException:
        # The wait itself died (KeyboardInterrupt, SystemExit, injected error): withdraw the request
        # or it stays in _open forever — replayed to every reconnecting client and reported by
        # pending_kind() as a human still being waited on.
        with _lock:
            still_open = _open.pop(req.id, None) is req
            if still_open:
                _remember_settled_locked(req)
        if still_open:
            _emit_cancel(req, "interrupted")
            _notify(req.sid, "cancelled")
        raise
    with _lock:
        # The verdict is the state committed under the lock, never wait()'s return value: a
        # response frame can land after the deadline expires and before this removal, and
        # settlement (resolve_response / lock_answer / cancel) already popped it (#112548).
        timed_out = _open.pop(req.id, None) is req
        if timed_out:
            _remember_settled_locked(req)
        answered, result, locked = req.answered, req.result, dict(req.locked)
    if answered:
        return result
    if timed_out:
        _emit_cancel(req, "timeout")
        _notify(req.sid, "expired")
        if req.qids is not None:
            return {"answers": locked, "outcome": "timed_out"}
    return None


def send_async(method: str, sid: str, params: dict, on_result: Callable[[dict | None], None]) -> Callable[[str], None]:
    """Send one request whose wait is owned elsewhere (the approval queue's own timeout). ``on_result``
    runs on the dispatching thread when the response lands. Returns ``settle(reason)``: call it when
    the underlying wait ends; if the request is still open it is withdrawn with ``request.cancel``."""
    if _unanswerable(method, sid):
        on_result(None)
        return lambda reason: None
    req = ServerRequest(sid, method, params, on_result=on_result)
    _register(req)

    def settle(reason: str) -> None:
        with _lock:
            still_open = _open.pop(req.id, None) is not None
            if still_open:
                _remember_settled_locked(req)
        if still_open:
            _emit_cancel(req, reason)
            _notify(req.sid, "cancelled")

    return settle


def _valid_json_value(value: Any) -> bool:
    """Whether an error.data value is representable as standards-compliant JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_valid_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _valid_json_value(item) for key, item in value.items())
    return False


def _valid_error_object(error: Any) -> bool:
    if not isinstance(error, dict) or set(error) - {"code", "message", "data"}:
        return False
    if "code" not in error or type(error["code"]) is not int or not isinstance(error.get("message"), str):
        return False
    return "data" not in error or _valid_json_value(error["data"])


def _valid_shared_response_frame(frame: Any, req: ServerRequest) -> bool:
    """Validate the complete response envelope before it can compete for native settlement."""
    if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0" or frame.get("id") != req.id:
        return False
    if set(frame) - {"jsonrpc", "id", "result", "error"}:
        return False
    has_result, has_error = "result" in frame, "error" in frame
    if has_result == has_error:
        return False
    if has_error:
        return _valid_error_object(frame["error"])
    from tui_gateway.contracts import registry as contracts
    contract = contracts.SERVER_REQUESTS.get(req.method)
    if contract is None:
        return False
    try:
        contract.result.model_validate(frame["result"])
    except Exception:
        # ValidationError can include submitted values; never log or propagate its text.
        return False
    return True


def _is_not_shown(frame: dict) -> bool:
    error = frame.get("error")
    return isinstance(error, dict) and error.get("code") == NOT_SHOWN_CODE


def _decline(rid: str, transport: Any) -> bool:
    """One client's "no window here shows this session". Settles only once every answering client
    attached to the session declined: a bystander window must not beat the owner (#113348), and with
    no owner at all the agent gets the refusal now rather than at the deadline (#119333). A decline
    from an unknown transport (relayed, proxied) is recorded nowhere and the wait goes on."""
    with _lock:
        req = _open.get(rid)
    if req is None:
        logger.debug("server request %s: decline dropped, request no longer open", rid)
        return False
    if not _authorized_response(req.sid, transport):
        logger.warning("server request %s (%s): decline dropped, transport not authorized", rid, req.method)
        return False
    clients = set(_clients(req.sid)) if transport is not None else set()
    with _lock:
        if _open.get(rid) is not req:
            return True  # settled meanwhile
        if transport is not None:
            req.declined.add(transport)
        if not clients or not clients <= req.declined:
            return True
        _open.pop(rid, None)
        _remember_settled_locked(req)
        req.result = {"value": json.dumps({"success": False, "error": NOT_SHOWN_MESSAGE})}
        req.answered = True
    _emit_resolved(req, "not_shown")
    if req.on_result is not None:
        req.on_result(req.result)
    req.event.set()
    _notify(req.sid, "settled")
    return True


def _commit_response(req: ServerRequest, result: Any) -> None:
    """Commit one response result into *req* (caller holds ``_lock``). Batch clarify: answers locked
    early via ``clarify.lock`` belong to the final set even when the closing response only carries
    the tail the user answered last; a response without ``answers`` is the closed card's cancel-all."""
    req.result = result if isinstance(result, dict) else {}
    if req.qids and "answers" in req.result:
        answers = req.result.get("answers")
        merged = dict(req.locked)
        if isinstance(answers, dict):
            merged.update(answers)
        req.result = {**req.result, "answers": merged, "outcome": "submitted"}
    elif req.qids:
        req.result = {"answers": dict(req.locked), "outcome": "cancelled"}
    req.answered = True


def resolve_response(frame: dict, transport: Any = None) -> bool:
    """Route one client response frame to its open native request; invalid shared replies never settle it."""
    if not isinstance(frame, dict):
        return False
    rid = frame.get("id")
    if not isinstance(rid, str):
        return False
    shared = _shared_wire() is not None
    # Preserve the ordinary-mode decline path exactly. Shared mode validates the complete error
    # envelope before a NOT_SHOWN response can enter the unanimous-decline state machine.
    if not shared and _is_not_shown(frame):
        return _decline(rid, transport)
    with _lock:
        req = _open.get(rid)
    if req is None:
        logger.debug("server request %s: response dropped, request no longer open", rid)
        return False
    if not _authorized_response(req.sid, transport):
        logger.warning("server request %s (%s): response dropped, transport not authorized", rid, req.method)
        return False
    if shared:
        if not _valid_shared_response_frame(frame, req):
            error = frame.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            safe_code = code if type(code) is int else "invalid"
            logger.warning("server request %s (%s): invalid response dropped (error_code=%s)",
                           rid, req.method, safe_code)
            return False
        if _is_not_shown(frame):
            return _decline(rid, transport)
    with _lock:
        if _open.get(rid) is not req:
            logger.debug("server request %s: response dropped, request no longer open", rid)
            return False
        # Validation occurs before this same first-valid critical section as cancel()/lock_answer().
        _open.pop(rid, None)
        _remember_settled_locked(req)
        if "error" in frame:
            # Only the protocol code is safe to log; message/data can carry submitted passwords/secrets.
            logger.debug("server request %s (%s) answered with error code=%s",
                         rid, req.method, frame["error"]["code"])
            req.result, req.answered = None, False
        else:
            _commit_response(req, frame["result"] if shared else frame.get("result"))
        answered = req.answered
    if answered:
        if shared:
            _emit_answered(req)
        else:
            _emit_resolved(req, "answered")
    elif shared and "error" in frame:
        # A valid native error releases the wait but is not an answer. The legacy Desktop still
        # needs the existing clear-card signal; it must not be reported as request.resolved(answered).
        _emit_cancel(req, "resolved")
    if req.on_result is not None:
        req.on_result(req.result)
    req.event.set()
    _notify(req.sid, "settled")
    return True


def answer_from_shared(*, session_id: str, request_id: str, request_type: str, result: Any) -> str:
    """Settle one open native request for the shared adapter, after the caller validated the runtime
    epoch and the live/durable session identity, owner and membership.

    The native result contract for *request_type* is validated BEFORE any settlement, the frame id's
    exact identity (session + method) is re-checked under the same lock that ``resolve_response`` /
    ``cancel`` / ``lock_answer`` settle under (first writer wins), and the answer commits through the
    same ``_commit_response`` path as a raw response frame.

    Returns ``"accepted"`` (this call settled it), ``"already_resolved"`` (a known request that is no
    longer open), or ``"unavailable"`` (this process never served that request id). Raises
    ``ValueError`` for an identity mismatch or a result violating the native contract — and never
    consumes the first-writer slot in that case.
    """
    from tui_gateway.contracts import registry as contracts

    contract = contracts.SERVER_REQUESTS.get(request_type)
    if contract is None:
        raise ValueError(f"unknown native request type {request_type!r}")
    try:
        contract.result.model_validate(result)
    except ValueError:
        # Pydantic diagnostics include input_value, including secret/password
        # fragments. Expose only the known contract type, never those values.
        raise ValueError(f"result does not match the {request_type} contract") from None
    with _lock:
        req = _open.get(request_id)
        if req is None:
            recent = _settled_recent.get(request_id)
            if recent is not None and recent[0] == session_id and recent[1] == request_type:
                return "already_resolved"
            return "unavailable"
        if req.sid != session_id:
            raise ValueError("request_id does not belong to session_id")
        if req.method != request_type:
            raise ValueError("request_type does not match the native request")
        _open.pop(request_id, None)
        _remember_settled_locked(req)
        _commit_response(req, dict(result) if isinstance(result, dict) else result)
    _emit_answered(req)
    if req.on_result is not None:
        req.on_result(req.result)
    req.event.set()
    _notify(req.sid, "settled")
    return "accepted"


def lock_answer(request_id: str, question_id: str, answer: str | None) -> list[str] | None:
    """Lock one batch-clarify answer (update-in-place; ``None`` = skipped). Returns the question ids
    still unanswered; the last lock resolves the request with the full ``{"answers"}`` set. ``None``
    when no open batch has that id (expired or foreign); ``ValueError`` for an unknown question id."""
    with _lock:
        req = _open.get(request_id)
        if req is None or req.qids is None:
            return None
        if question_id not in req.qids:
            raise ValueError(f"unknown question_id {question_id!r}")
        req.locked[question_id] = answer
        remaining = [qid for qid in req.qids if qid not in req.locked]
        if not remaining:
            req.result, req.answered = {"answers": dict(req.locked), "outcome": "submitted"}, True
            _open.pop(request_id, None)
            _remember_settled_locked(req)
    if not remaining:
        _emit_answered(req)
        req.event.set()
        _notify(req.sid, "locked")
    return remaining


def cancel(sid: str | None = None, reason: str = "interrupted") -> int:
    """Withdraw open requests — only *sid*'s (session.interrupt must not touch other sessions'), or
    every one when *sid* is None (shutdown). Blocked waits return None (a batch returns its locked
    answers with ``outcome: cancelled``); queue-backed requests run ``on_result(None)`` so their
    owner can settle. Returns the number withdrawn."""
    with _lock:
        targets = [req for req in _open.values() if sid is None or req.sid == sid]
        for req in targets:
            _open.pop(req.id, None)
            _remember_settled_locked(req)
            if req.qids is not None:
                req.result, req.answered = {"answers": dict(req.locked), "outcome": "cancelled"}, True
            else:
                req.result, req.answered = None, False
    for req in targets:
        if req.on_result is not None:
            req.on_result(None)
        req.event.set()
        _emit_cancel(req, reason)
        _notify(req.sid, "cancelled")
    return len(targets)


def open_requests(sid: str) -> list[dict]:
    """Unanswered requests for *sid*, oldest first."""
    with _lock:
        reqs = sorted((req for req in _open.values() if req.sid == sid), key=lambda r: r.created_at)
    return [req.snapshot() for req in reqs]


def open_request_identity(request_id: str) -> tuple[str, str] | None:
    """``(sid, method)`` of one open request, or None — the authorization seam for answer RPCs
    that carry a request id instead of a session id. Never exposes params."""
    with _lock:
        req = _open.get(request_id)
        return (req.sid, req.method) if req is not None else None


def open_request_facts(sid: str) -> tuple[list[str], list[str]]:
    """``(methods, request ids)`` oldest first for one session's open requests — the ONLY source
    the attention derivation reads (never the approval queue, so an approval can never be counted
    twice: once from its native request frame and once from its queue entry)."""
    with _lock:
        reqs = sorted((req for req in _open.values() if req.sid == sid), key=lambda r: r.created_at)
        return [req.method for req in reqs], [req.id for req in reqs]


def open_request_count() -> int:
    """Unanswered server→client requests across every session: the process is waiting on a
    human (clarify, approval, sudo, secret, ...) and must not be treated as idle."""
    with _lock:
        return len(_open)


def pending_kind(sid: str) -> str:
    """Method of the oldest open request for *sid* ("" when none) — the session is waiting on a human."""
    with _lock:
        reqs = [req for req in _open.values() if req.sid == sid]
    return min(reqs, key=lambda r: r.created_at).method if reqs else ""


def is_response_frame(obj: Any) -> bool:
    """A client response: has an ``id`` and a ``result``/``error`` member but no ``method``."""
    return isinstance(obj, dict) and "method" not in obj and "id" in obj and ("result" in obj or "error" in obj)


def reset_for_tests() -> None:
    with _lock:
        _open.clear()
        _answering_clients.clear()
        _settled_recent.clear()
