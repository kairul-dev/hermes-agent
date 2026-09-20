"""Sanitized, structured errors for required session persistence.

The normal Forge/TUI turn path must fail before provider or tool execution when
the durable session cannot be proven.  This module deliberately keeps the
diagnostic surface small: it records operation/stage, a safe session identity,
exception type, and SQLite's classified error code when present, but never
includes raw exception text, SQL, credentials, or transcript content.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Optional


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def safe_session_identity(value: Any) -> str:
    """Return a bounded, non-secret session identifier for diagnostics."""

    raw = str(value or "").strip()
    if not raw:
        return "<missing>"
    if _SAFE_ID_RE.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    return f"<redacted:{digest}>"


def _sqlite_details(exc: BaseException | None) -> tuple[Optional[int], Optional[str]]:
    if exc is None:
        return None, None
    code = getattr(exc, "sqlite_errorcode", None)
    name = getattr(exc, "sqlite_errorname", None)
    if isinstance(code, bool) or not isinstance(code, int):
        code = None
    if not isinstance(name, str) or not name or len(name) > 64:
        name = None
    return code, name


def _classify_cause(exc: BaseException | None) -> str:
    """Classify SQLite integrity errors before Hermes' broad classifier."""

    if isinstance(exc, sqlite3.IntegrityError):
        foreign_key_code = getattr(sqlite3, "SQLITE_CONSTRAINT_FOREIGNKEY", 787)
        if (
            getattr(exc, "sqlite_errorcode", None) == foreign_key_code
            or "foreign key" in str(exc).lower()
        ):
            return "foreign_key"
        return "integrity"
    try:
        from hermes_state import classify_persistence_error

        return str(classify_persistence_error(exc) or "unknown")
    except Exception:
        return "unknown"


_USER_GUIDANCE = {
    "missing_key": "the durable session key is missing",
    "unavailable": "the session store is unavailable",
    "no_op": "the session row was not created or could not be verified",
    "foreign_key": "the session lineage was rejected; its parent is not valid",
    "integrity": "the session row was rejected by the database",
    "locked": "the session store is busy",
    "disk": "the session store is not writable",
    "corrupt": "the session store reported structural damage",
    "replaced": "the session store changed while it was open",
    "compression": "the session is being compressed by another writer",
    "compression_closed": "the session was rotated by compression",
    "turn_lease": "the session turn lease is no longer valid",
    "write_failed": "the required write did not complete",
    "unknown": "the session store rejected the operation",
}


class SessionPersistenceError(RuntimeError):
    """A safe, actionable failure of a required durable-session operation."""

    def __init__(
        self,
        *,
        operation: str,
        stage: str,
        session_id: Any,
        kind: str = "unknown",
        cause: BaseException | None = None,
    ) -> None:
        self.operation = str(operation or "session persistence")[:80]
        self.stage = str(stage or "unknown")[:80]
        self.session_identity = safe_session_identity(session_id)
        self.kind = str(kind or "unknown")[:40]
        self.exception_type = type(cause).__name__ if cause is not None else "None"
        self.sqlite_error_code, self.sqlite_error_name = _sqlite_details(cause)
        self.cause = cause
        guidance = _USER_GUIDANCE.get(self.kind, _USER_GUIDANCE["unknown"])
        super().__init__(
            f"Session persistence failed during {self.operation}/{self.stage} "
            f"for session {self.session_identity}: {guidance}. "
            "The turn cannot continue. Check storage and any prior tool effects before retrying."
        )

    @property
    def user_message(self) -> str:
        return str(self)

    def diagnostic(self) -> dict[str, Any]:
        """Return the bounded diagnostic safe for logs and RPC metadata."""

        data: dict[str, Any] = {
            "operation": self.operation,
            "stage": self.stage,
            "session_id": self.session_identity,
            "kind": self.kind,
            "exception_type": self.exception_type,
        }
        if self.sqlite_error_code is not None:
            data["sqlite_error_code"] = self.sqlite_error_code
        if self.sqlite_error_name is not None:
            data["sqlite_error_name"] = self.sqlite_error_name
        return data


def persistence_error_from_exception(
    *,
    operation: str,
    stage: str,
    session_id: Any,
    exc: BaseException,
    kind: str | None = None,
) -> SessionPersistenceError:
    """Wrap an exception without exposing its unrestricted text."""

    return SessionPersistenceError(
        operation=operation,
        stage=stage,
        session_id=session_id,
        kind=kind or _classify_cause(exc),
        cause=exc,
    )


def persistence_error(
    *,
    operation: str,
    stage: str,
    session_id: Any,
    kind: str,
) -> SessionPersistenceError:
    """Construct a safe error for a missing/unverified condition."""

    return SessionPersistenceError(
        operation=operation,
        stage=stage,
        session_id=session_id,
        kind=kind,
    )
