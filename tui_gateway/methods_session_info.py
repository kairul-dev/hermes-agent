"""Transport-owned read-only model projection; no activation or agent construction."""
import contextlib

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@method("session.info.get")
def _session_info_get(rid, params):
    from .contracts.session_info import SessionInfoGetParams
    try:
        params = SessionInfoGetParams.model_validate(params).model_dump()
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    sid = params["session_id"]
    _, session = _current_session_steer_authority(sid)
    if session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    stored_id = str(_orchestration_stored_id(session))
    host_key = session.get("session_key")
    if not stored_id:
        return _err(rid, 4001, "session has no durable identity")
    try:
        info = _session_planner_model_info(session)
    except Exception as exc:
        _, current = _current_session_steer_authority(sid)
        if current is not session or str(_orchestration_stored_id(session)) != stored_id:
            return _err(rid, 4001, "session changed while reading its projection")
        return _err(rid, 5033, f"Session model projection unavailable: {exc}")
    _, current = _current_session_steer_authority(sid)
    if (current is not session or str(_orchestration_stored_id(session)) != stored_id
            or session.get("session_key") != host_key):
        return _err(rid, 4001, "session changed while reading its projection")
    return _ok(rid, {"session_id": sid, "stored_session_id": stored_id, "info": info})


def _session_planner_model_info(session: dict, agent=None):
    """Exact six-field strict projection shared by the getter and native info producer."""
    import json
    import sqlite3
    from types import SimpleNamespace
    stored_id = str(_orchestration_stored_id(session))
    agent = session.get("agent") if "agent" in session else agent
    override = session.get("model_override")
    if agent is None and isinstance(override, dict) and override.get("model"):
        agent = SimpleNamespace(**{k: override[k] for k in ("model", "provider", "base_url", "reasoning_config") if k in override})
    if agent is None and session.get("profile_home") and stored_id:
        # Never open SessionDB here: construction can migrate/create operator state.
        path = Path(session["profile_home"]) / "state.db"
        if path.is_file():
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT model, model_config FROM sessions WHERE id = ?", (stored_id,)).fetchone()
            if row:
                config = json.loads(row[1] or "{}")
                if not isinstance(config, dict):
                    raise ValueError("invalid session model_config")
                agent = SimpleNamespace(**{k: config[k] for k in ("provider", "base_url", "reasoning_config") if k in config},
                                        model=row[0] or config.get("model") or "")
    info = _session_model_info(agent, session, strict_provider=True)
    if str(_orchestration_stored_id(session)) != stored_id:
        raise RuntimeError("session changed while reading its projection")
    return {**info, "model_available": bool(info["model"]), "provider_available": bool(info["provider"])}


def _session_projection_scope(session):
    """Config/home only: never read .env, hydrate sources, or create config backups."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.runtime_provider import provider_config_scope
    from utils import fast_safe_load
    home = Path(session.get("profile_home") or _hermes_home)
    catalog = {}
    path = home / "config.yaml"
    if path.is_file():
        with path.open(encoding="utf-8-sig") as stream:
            raw = fast_safe_load(stream) or {}
        # Native endpoint identity lookup needs only names and literal endpoints.
        # Do not expand ${SECRET} or retain credential-bearing config values.
        def endpoint(entry):
            return {k: entry[k] for k in ("name", "provider_key", "base_url", "enabled")
                    if k in entry and "${" not in str(entry[k])}
        if isinstance(raw, dict):
            if isinstance(raw.get("providers"), dict):
                catalog["providers"] = {name: endpoint(entry) for name, entry in raw["providers"].items() if isinstance(entry, dict)}
            if isinstance(raw.get("custom_providers"), list):
                catalog["custom_providers"] = [endpoint(entry) for entry in raw["custom_providers"] if isinstance(entry, dict)]
    with contextlib.ExitStack() as scope:
        scope.callback(reset_hermes_home_override, set_hermes_home_override(str(home)))
        scope.enter_context(provider_config_scope(catalog))
        yield


_session_projection_scope = contextlib.contextmanager(_session_projection_scope)


def register(server):
    bind_module(globals(), server)
