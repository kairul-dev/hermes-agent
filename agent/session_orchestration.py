"""Durable per-conversation delegation selection; never profile configuration."""
import inspect
import json


class SessionOrchestrationChangedError(RuntimeError):
    """The current durable session changed; callers must retry, not report storage failure."""


def default_policy():
    return dict(version=1, enabled=False, worker_provider="", worker_model="", worker_reasoning_effort="")


def validate_policy(value):
    from hermes_constants import parse_reasoning_effort

    if not isinstance(value, dict) or set(value) != set(default_policy()):
        raise ValueError("invalid session orchestration policy")
    if type(value["version"]) is not int or value["version"] != 1 or type(value["enabled"]) is not bool:
        raise ValueError("invalid session orchestration version or enabled flag")
    for key in ("worker_provider", "worker_model", "worker_reasoning_effort"):
        if not isinstance(value[key], str) or value[key] != value[key].strip():
            raise ValueError(f"invalid {key}")
    if value["enabled"] and not (value["worker_provider"] and value["worker_model"]):
        raise ValueError("enabled orchestration requires worker_provider and worker_model")
    effort = value["worker_reasoning_effort"]
    if effort and parse_reasoning_effort(effort) is None:
        raise ValueError("invalid worker_reasoning_effort")
    return dict(value)


def worker_config(config, policy):
    """Freeze the session route while preserving operator iteration/concurrency budgets."""
    from copy import deepcopy

    snapshot = deepcopy(config)
    for key in ("base_url", "api_key", "api_mode", "request_overrides", "fallback_model",
                "fallback_providers", "command", "args"):
        snapshot.pop(key, None)
    snapshot["_orchestration"] = dict(policy)
    snapshot.update(provider=policy["worker_provider"], model=policy["worker_model"],
                    reasoning_effort=policy["worker_reasoning_effort"], fallback_providers=[])
    return snapshot


def policy_for_spawn(parent):
    """Read at spawn, so resume and policy changes need no agent rebuild."""
    desktop = getattr(parent, "platform", None) in ("desktop", "gui")
    # A dynamic proxy must not invent a persistence capability (the same
    # explicit-capability rule as interrupt_compat). Native agents own this field.
    db = getattr(parent, "_session_db", None) if inspect.getattr_static(parent, "_session_db", None) is not None else None
    sid = getattr(parent, "session_id", None) if inspect.getattr_static(parent, "session_id", None) is not None else None
    if db is None or not sid:
        return default_policy() if desktop else None
    try:
        policy = read_policy(db, sid)
    except Exception as exc:
        # Never infer permission from a damaged or unavailable persisted selection.
        raise ValueError("Session orchestration policy is unavailable or invalid") from exc
    return default_policy() if policy is None and desktop else policy


def require_spawn_policy(parent):
    policy = policy_for_spawn(parent)
    if policy is not None and not policy["enabled"]:
        raise ValueError("Session orchestration is disabled. Enable it in this session before spawning workers.")
    return policy


def read_policy(db, session_id):
    """None means no saved choice. Malformed JSON is not an absent policy."""
    row = db.get_session(session_id)
    return policy_from_model_config(row.get("model_config") if row else None)


def policy_from_model_config(raw):
    """Validate policy from an already-read row, including inside a write transaction."""
    if raw is None or raw == "":
        return None
    try:
        config = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid session model_config") from exc
    if not isinstance(config, dict):
        raise ValueError("invalid session model_config")
    return validate_policy(config["_orchestration"]) if "_orchestration" in config else None
