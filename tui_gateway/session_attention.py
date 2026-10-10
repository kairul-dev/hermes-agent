"""Canonical live session-attention state for the TUI / desktop gateway.

Additive wire contract (``schema_version`` 1) — nothing here changes the
historical ``status`` / ``running`` fields or any transport::

    session_attention = {
        "schema_version": 1,
        "status": "IDLE" | "WORKING" | "NEEDS_INPUT" | "COMPLETED" | "FAILED",
        "substatus": "",             # "starting" | "interrupted" | ...
        "request_types": [str],      # distinct pending request kinds
        "revision": int,             # monotonic per session; bumps on change
        "epoch": str,                # exact decimal process epoch on JSON wire
        "updated_at": float,
        "turn_started_at": float | None,
    }

Derivation precedence: pending request > live running/starting activity > last
terminal ``message.complete`` outcome > idle. ``message.start`` opens a turn and
CLEARS the retained outcome; ``message.complete`` is the only frame that
records one (an unrelated ``error`` event never does, so it can never mark
FAILED).

Because the terminal frame fires while the turn loop still holds
``running`` (its settle point releases the session a moment later), the
outcome transition is committed and announced by the runtime's settle
reconcile — not by the frame alone.  Until then the session still reads
WORKING, matching "live running beats a prior outcome"; once the loop
releases the session the last terminal outcome becomes the visible state.

Clean transitions (a change of status / substatus / request_types — or of the
EXACT pending identity, see below) bump ``revision`` and notify the configured
emitter exactly once per change, which is how future clients learn about
``session.attention``.  Reads (:func:`snapshot`) never emit and never bump the
revision, so polling cannot storm clients; reconciliation of pending-request
removal is driven by the runtime's own removal/transition paths (no connected
client required).

``revision`` is bound to the exact pending identity, not just its shape: two
same-kind waits collapsing to one (or one being replaced by another) keeps
``status``/``request_types`` constant, yet the client-visible pending set
changed.  Callers therefore pass ``pending_request_ids`` parallel to
``pending_types``; the record stores an order-independent, secret-free
``(type, request_id)`` fingerprint and a change in it commits a new revision
and emits, so a revision-deduplicating client can never keep the superseded
identity.  Callers that pass types alone keep the historical shape-only
comparison.

The gateway's live projections (the ``session.active_list`` item and the
``session.activate`` / ``session.resume`` payload) all source their
``session_attention`` dict from :func:`snapshot`, so every surface agrees on
one canonical state machine.
"""

from __future__ import annotations

import threading
import time

SCHEMA_VERSION = 1

IDLE = "IDLE"
WORKING = "WORKING"
NEEDS_INPUT = "NEEDS_INPUT"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

# Process identity captured once per process. Serialize it as an exact decimal
# string so JSON clients never round it. Revisions order one epoch; clients use
# updated_at for different epochs and must not assume a persistent clock/counter.
PROCESS_EPOCH = time.time_ns()

_lock = threading.RLock()
_records: dict[str, dict] = {}
_emitter = None


def configure_emitter(cb) -> None:
    """Install the transition sink (``cb(sid, payload)``).

    The gateway wires this once at import to emit ``session.attention`` event
    frames.  With no emitter configured transitions still update state and
    bump revisions; only the notification is skipped.
    """
    global _emitter
    _emitter = cb


def _new_record() -> dict:
    return {
        "revision": 0,
        "status": None,  # committed state; None until the first commit
        "substatus": "",
        "request_types": [],
        "pending_identity": (),  # exact (type, request_id) pending-set fingerprint
        "outcome": None,  # None | "completed" | "failed"
        "outcome_detail": "",  # e.g. "interrupted"
        "turn_active": False,
        "turn_started_at": None,
        "updated_at": 0.0,
    }


def _types(pending_types) -> list[str]:
    return list(dict.fromkeys(str(kind) for kind in (pending_types or ()) if kind))


def _pending_identity(pending_types, pending_request_ids) -> tuple[tuple[str, str], ...]:
    """Order-independent fingerprint of the EXACT pending set.

    ``(type, request_id)`` pairs only — never prompts, commands or answer
    material — so the committed ``revision`` can be bound to the exact pending
    identity without storing anything sensitive.  Callers that pass types
    alone (standalone transitions) keep the historical empty fingerprint.
    """
    return tuple(
        sorted(
            (str(kind), str(rid))
            for kind, rid in zip(pending_types or (), pending_request_ids or ())
        )
    )


def _derive(
    record: dict,
    running: bool,
    live_turn: bool,
    starting: bool,
    pending_types,
) -> tuple[str, str, list[str]]:
    kinds = _types(pending_types)
    if kinds:
        return NEEDS_INPUT, "", kinds
    if record["turn_active"] or running or live_turn:
        # A live turn — a started-and-unterminated one, the loop's running
        # flag, or a fresh in-flight turn record — beats every prior outcome.
        return WORKING, "", []
    if starting:
        return WORKING, "starting", []
    if record["outcome"] is not None:
        if record["outcome"] == "failed":
            return FAILED, "", []
        return COMPLETED, record["outcome_detail"], []
    return IDLE, "", []


def _payload(record: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": record["status"],
        "substatus": record["substatus"],
        "request_types": list(record["request_types"]),
        "revision": record["revision"],
        "epoch": str(PROCESS_EPOCH),
        "updated_at": record["updated_at"],
        "turn_started_at": record["turn_started_at"],
    }


def snapshot(
    sid: str,
    *,
    running: bool = False,
    live_turn: bool = False,
    starting: bool = False,
    pending_types=(),
    pending_request_ids=(),
) -> dict:
    """Pure read of one session's attention state.

    Derives the status from the live facts plus the committed turn/outcome
    record so a snapshot is always canonical for "right now".  Never emits,
    never bumps the revision: calling this any number of times is
    side-effect-stable.  A session that was never seen before gets a committed
    IDLE (or derived) first revision so later transitions stay monotonic.
    """
    with _lock:
        record = _records.get(sid)
        if record is None:
            record = _new_record()
            status, substatus, kinds = _derive(
                record, running, live_turn, starting, pending_types
            )
            record["status"] = status
            record["substatus"] = substatus
            record["request_types"] = kinds
            # First sight: remember the exact identity that this first
            # committed revision describes, so later writer comparisons are
            # grounded in a revision a client actually saw.  An ESTABLISHED
            # record is never mutated by a read — a read must not mask a
            # pending-set transition a writer has not committed yet.
            record["pending_identity"] = _pending_identity(
                pending_types, pending_request_ids
            )
            record["revision"] = 1
            record["updated_at"] = time.time()
            _records[sid] = record
        status, substatus, kinds = _derive(
            record, running, live_turn, starting, pending_types
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "substatus": substatus,
            "request_types": kinds,
            "revision": record["revision"],
            "epoch": str(PROCESS_EPOCH),
            "updated_at": record["updated_at"],
            "turn_started_at": record["turn_started_at"],
        }


def _apply(
    sid: str,
    mutate,
    running,
    live_turn,
    starting,
    pending_types,
    pending_request_ids=(),
) -> dict | None:
    """Mutate ``sid``'s record, commit the derived state, notify on change.

    Returns the committed payload when the reportable state
    (status/substatus/request_types or the exact pending identity) changed,
    else ``None``.  The emitter is called after the lock is released;
    payloads carry the revision so a client can order frames regardless of
    delivery interleaving.
    """
    with _lock:
        record = _records.get(sid)
        if record is None:
            record = _new_record()
        mutate(record)
        status, substatus, kinds = _derive(
            record, running, live_turn, starting, pending_types
        )
        identity = _pending_identity(pending_types, pending_request_ids)
        first_commit = record["status"] is None
        unchanged = (
            not first_commit
            and status == record["status"]
            and substatus == record["substatus"]
            and kinds == record["request_types"]
            and identity == record.get("pending_identity", ())
        )
        if unchanged:
            _records.setdefault(sid, record)
            return None
        record["status"] = status
        record["substatus"] = substatus
        record["request_types"] = kinds
        record["pending_identity"] = identity
        record["revision"] = 1 if first_commit else record["revision"] + 1
        record["updated_at"] = time.time()
        _records[sid] = record
        payload = _payload(record)
    emit = _emitter
    if emit is not None:
        try:
            emit(sid, payload)
        except Exception:
            # A failing transport must never corrupt the state machine.
            pass
    return payload


def reconcile(
    sid: str,
    *,
    running: bool = False,
    live_turn: bool = False,
    starting: bool = False,
    pending_types=(),
    pending_request_ids=(),
) -> dict | None:
    """Re-derive and commit one session from live facts.

    Called by the runtime's own request add/remove paths so a resolved or
    expired wait returns the session to WORKING / its outcome without any
    client read.  Emits once when the state actually moved; a no-op otherwise
    (no storms).
    """
    return _apply(
        sid,
        lambda _rec: None,
        running,
        live_turn,
        starting,
        pending_types,
        pending_request_ids,
    )


def note_message_start(
    sid: str,
    *,
    running: bool = False,
    live_turn: bool = False,
    starting: bool = False,
    pending_types=(),
    pending_request_ids=(),
) -> dict | None:
    """Open a turn: clear any retained terminal outcome and stamp the start."""

    def mutate(record: dict) -> None:
        record["turn_active"] = True
        record["turn_started_at"] = time.time()
        record["outcome"] = None
        record["outcome_detail"] = ""

    return _apply(
        sid, mutate, running, live_turn, starting, pending_types, pending_request_ids
    )


def note_message_complete(
    sid: str,
    raw_status,
    *,
    running: bool = False,
    live_turn: bool = False,
    starting: bool = False,
    pending_types=(),
    pending_request_ids=(),
) -> dict | None:
    """Record the last terminal ``message.complete`` status for a session.

    Only an explicit error status marks FAILED; ``"interrupted"`` is a
    completed turn with the ``interrupted`` substatus; a missing status (the
    subagent mirror frame) counts as completed.  An unrelated ``error`` event
    never reaches this function.
    """
    normalized = str(raw_status or "").strip().lower()

    def mutate(record: dict) -> None:
        record["turn_active"] = False
        if normalized in ("error", "failed", "failure"):
            record["outcome"] = "failed"
            record["outcome_detail"] = ""
        elif normalized == "interrupted":
            record["outcome"] = "completed"
            record["outcome_detail"] = "interrupted"
        else:
            record["outcome"] = "completed"
            record["outcome_detail"] = ""

    return _apply(
        sid, mutate, running, live_turn, starting, pending_types, pending_request_ids
    )


def forget(sid: str) -> None:
    """Drop a session's attention record (session teardown)."""
    with _lock:
        _records.pop(sid, None)
