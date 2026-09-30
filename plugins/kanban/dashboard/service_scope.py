"""Consume the generic authenticated service context for shared Kanban resources."""
from fastapi import HTTPException
from hermes_cli.dashboard_auth.local_service import CURRENT_SERVICE, ServiceDenied, authorize_params, path_inside


def launch_assignee():
    identity = CURRENT_SERVICE.get()
    if identity is None:
        return None
    from hermes_constants import profile_name_for_home
    return profile_name_for_home(identity.store.path.parent.parent)


def project_allowed(pid):
    identity = CURRENT_SERVICE.get()
    if identity is None:
        return True
    if not pid:
        return False
    try:
        authorize_params(identity, "projects.get", {"id": pid})
        return True
    except (ServiceDenied, OSError, ValueError):
        return False


def task_allowed(task):
    identity = CURRENT_SERVICE.get()
    if identity is None:
        return True
    if not project_allowed(task.project_id) or task.assignee not in {None, launch_assignee()}:
        return False
    if task.workspace_path:
        try:
            path_inside(task.workspace_path, identity.grants["restrictions"]["project_roots"])
        except ServiceDenied:
            return False
    return True


def require_task(task):
    if not task_allowed(task):
        raise HTTPException(status_code=403, detail="Service task scope denied")


def require_status_owner(task):
    if CURRENT_SERVICE.get() is not None and task.assignee != launch_assignee():
        raise HTTPException(status_code=403, detail="Service task status requires launch-profile ownership")
