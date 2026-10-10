"""Authorized orchestration options for an exact live conversation."""
import contextlib

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@contextlib.contextmanager
def _orchestration_db(session):
    db = getattr(session.get("agent"), "_session_db", None)
    if db is not None:
        yield db
    else:
        with _session_db(session) as db:
            if db is None:
                raise RuntimeError("session database is unavailable")
            yield db


def _orchestration_stored_id(session):
    return getattr(session.get("agent"), "session_id", None) or session.get("session_key") or ""


def _orchestration_read(db, stored_id):
    from agent.session_orchestration import default_policy, read_policy
    policy = read_policy(db, stored_id)
    return default_policy() if policy is None else policy


def _orchestration_result(sid, stored_id, policy):
    return {**policy, "supported": True, "scope": "session", "session_id": sid, "stored_session_id": stored_id}


@method("orchestration.get")
@_profile_scoped
def _orchestration_get(rid, params):
    from tui_gateway.contracts.orchestration import OrchestrationGetParams
    try:
        params = OrchestrationGetParams.model_validate(params).model_dump()
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    sid = params["session_id"]
    _, session = _current_session_steer_authority(sid)
    if session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    stored_id = _orchestration_stored_id(session)
    if not stored_id:
        return _err(rid, 4001, "session has no durable identity")
    try:
        with _orchestration_db(session) as db:
            policy = _orchestration_read(db, stored_id)
        return _ok(rid, _orchestration_result(sid, stored_id, policy))
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    except Exception as exc:
        return _err(rid, 5033, f"Session orchestration storage unavailable: {exc}")


@method("orchestration.set")
@_profile_scoped
def _orchestration_set(rid, params):
    from agent.session_orchestration import SessionOrchestrationChangedError
    from tui_gateway.contracts.orchestration import OrchestrationSetParams
    try:
        params = OrchestrationSetParams.model_validate(params).model_dump()
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    sid = params["session_id"]
    _, session = _current_session_steer_authority(sid)
    if session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    stored_id = _orchestration_stored_id(session)
    if not stored_id:
        return _err(rid, 4001, "session has no durable identity")
    try:
        with _orchestration_db(session) as db:
            policy = _orchestration_read(db, stored_id)
            policy = {**policy, "enabled": params["enabled"]}
            complete_selection = bool(params["worker_provider"] and params["worker_model"])
            for key in ("worker_provider", "worker_model", "worker_reasoning_effort"):
                if params["enabled"] or params[key] or (key == "worker_reasoning_effort" and complete_selection):
                    policy[key] = params[key]
            from agent.session_orchestration import validate_policy
            policy = validate_policy(policy)
            # An explicit choice is real activity, unlike opening an untouched draft.
            if db.get_session(stored_id) is None:
                agent = session.get("agent")
                if agent is None:
                    # Same planner projection as first prompt materialization;
                    # orchestration is activity, not a new default-model draft.
                    row_model, model_config = _workdir_row_model_config(session)
                else:
                    row_model = getattr(agent, "model", None)
                    model_config = getattr(agent, "_session_init_model_config", None)
                db.create_session(stored_id, source=_session_source(session),
                                  model=row_model, model_config=model_config,
                                  parent_session_id=session.get("parent_session_id"),
                                  cwd=_persisted_session_cwd(session), user_id=_session_auth_user_id(session),
                                  profile_name=profile_name_for_home(session.get("profile_home")) or _current_profile_name())
            transport = current_transport()
            def current_identity():
                # Lock-free registry reference fence inside SQLite: native info paths
                # also read the DB while holding _sessions_lock, so acquiring that
                # lock here would invert the order. Recheck full authority after commit.
                return (_sessions.get(sid) is session
                        and _session_transport_contains(session, transport)
                        and _orchestration_stored_id(session) == stored_id)
            db.patch_session_model_config(stored_id, {"_orchestration": policy}, identity_guard=current_identity)
            saved = db.get_session_model_config_value(stored_id, "_orchestration")
            _, current = _current_session_steer_authority(sid)
            if current is not session or not current_identity():
                raise SessionOrchestrationChangedError("Current session changed; retry with the current session")
            if saved != policy:
                raise RuntimeError("session orchestration readback failed")
        return _ok(rid, _orchestration_result(sid, stored_id, saved))
    except SessionOrchestrationChangedError as exc:
        return _err(rid, 4001, str(exc))
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    except Exception as exc:
        return _err(rid, 5033, f"Session orchestration storage unavailable: {exc}")


def register(server):
    bind_module(globals(), server)
