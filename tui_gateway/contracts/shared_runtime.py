"""Generic shared-runtime adapter: two additive RPCs over the existing native handler table.

``session.shared.rpc`` re-enters the native admission path with the SAME outer RPC id, so the
wrapped method's own contract, profile scope and shared-mode authorization all apply and its
result/error is returned unchanged. ``session.shared.answer`` settles one native server→client
request after validating the exact identities and the method's native result contract.

Both are advertised only when ``dashboard.shared_runtime.enabled``; disabled means fail closed.
"""

from typing import Literal

from pydantic import Field, StrictStr

from .base import JsonValue, Params, Payload, Result
from .registry import event, method


# ── canonical session attention (companion contract) ────────────────────────────────────────────


class SessionAttention(Payload):
    """``tui_gateway/session_attention.py`` — one canonical projection per live session. Also ships
    as a field of the live projections (active list / activate / resume)."""

    schema_version: int
    status: str  # IDLE | WORKING | NEEDS_INPUT | COMPLETED | FAILED
    substatus: str = ""
    request_types: list[str] = Field(default_factory=list)
    revision: int
    epoch: str  # exact decimal process epoch (JSON string so clients never round it)
    updated_at: float
    turn_started_at: float | None = None


class PendingRequest(Payload):
    """One unresolved native wait's identity: opaque ``request_id`` + method. Never prompt text."""

    request_id: str
    type: str


class SessionAttentionEventPayload(SessionAttention):
    """Session-attention event identity is additive; native active-list/resume snapshots stay unchanged."""

    runtime_epoch: str
    session_id: str
    stored_session_id: str


event("session.attention", SessionAttentionEventPayload,
      doc="A session's canonical attention state moved, fenced by runtime/live/durable identity.")


class SharedRpcParams(Params):
    runtime_epoch: StrictStr
    method: StrictStr
    params: dict[str, JsonValue]


class SharedRpcResult(Result):
    """The wrapped native handler's result, verbatim — its closed shape is that method's contract."""

    model_config = Result.model_config | {"extra": "allow"}


method("session.shared.rpc", params=SharedRpcParams, result=SharedRpcResult,
       doc="Invoke one allowlisted native handler under the exact advertised runtime epoch.")


class SharedAnswerParams(Params):
    runtime_epoch: StrictStr
    session_id: StrictStr
    stored_session_id: StrictStr
    request_id: StrictStr
    request_type: StrictStr
    result: dict[str, JsonValue]


class SharedAnswerResult(Result):
    status: Literal["accepted", "already_resolved", "unavailable"]


method("session.shared.answer", params=SharedAnswerParams, result=SharedAnswerResult,
       doc="Answer one open native server→client request by its exact identity, after validation.")
