"""Minimal safe current model projection for an owned runtime session."""
from typing import Any
from pydantic import StrictStr
from .base import Params, Result
from .registry import method


class SessionInfoGetParams(Params):
    session_id: StrictStr


class SessionInfoGetResult(Result):
    session_id: str
    stored_session_id: str
    info: dict[str, Any]


method("session.info.get", params=SessionInfoGetParams, result=SessionInfoGetResult)
