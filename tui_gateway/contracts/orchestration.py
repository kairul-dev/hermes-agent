"""Session-scoped planner/worker selection shared by desktop and TUI clients."""
from typing import Literal

from pydantic import StrictBool, StrictStr

from .base import Params, Result
from .registry import method


class OrchestrationGetParams(Params):
    session_id: StrictStr


class OrchestrationSetParams(OrchestrationGetParams):
    enabled: StrictBool
    worker_provider: StrictStr
    worker_model: StrictStr
    worker_reasoning_effort: StrictStr = ""


class OrchestrationResult(Result):
    version: Literal[1]
    supported: Literal[True]
    scope: Literal["session"]
    session_id: str
    stored_session_id: str
    enabled: bool
    worker_provider: str
    worker_model: str
    worker_reasoning_effort: str


method("orchestration.get", params=OrchestrationGetParams, result=OrchestrationResult)
method("orchestration.set", params=OrchestrationSetParams, result=OrchestrationResult)
