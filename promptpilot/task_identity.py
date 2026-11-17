"""Non-secret scheduler markers inherited by task tools; never an admission grant."""
from datetime import datetime, timezone


def apply_task_markers(environment, task):
    """Replace provider/inherited markers with the actual claimed task values."""
    environment.pop("PP_TASK_ID", None)
    environment.pop("PP_TASK_STARTED_AT", None)
    environment["PP_TASK_ID"] = str(task.id)
    started = getattr(task, "started_at", None)
    if isinstance(started, datetime) and started.tzinfo is not None:
        environment["PP_TASK_STARTED_AT"] = started.astimezone(timezone.utc).isoformat()
    return environment
