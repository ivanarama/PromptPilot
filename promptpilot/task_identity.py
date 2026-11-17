"""Non-secret scheduler markers inherited by task tools; never an admission grant."""
from datetime import datetime, timezone
import hashlib
import json

_EXECUTION_FIELDS = (
    "prompt", "working_dir", "provider", "model", "effort", "rights",
    "skip_permissions", "machine", "detached", "herdr_target", "worktree",
    "worktree_path", "worktree_branch", "task_timeout",
)


def task_execution_fingerprint(task):
    """Bind the claimed task's inputs, independently of its future series."""
    values = {key: task[key] if hasattr(task, "keys") else getattr(task, key, None)
              for key in _EXECUTION_FIELDS}
    for key in ("skip_permissions", "detached", "worktree"):
        if values[key] is not None:
            values[key] = int(values[key])
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def apply_task_markers(environment, task):
    """Replace provider/inherited markers with the actual claimed task values."""
    environment.pop("PP_TASK_ID", None)
    environment.pop("PP_TASK_STARTED_AT", None)
    environment["PP_TASK_ID"] = str(task.id)
    started = getattr(task, "started_at", None)
    if isinstance(started, datetime) and started.tzinfo is not None:
        environment["PP_TASK_STARTED_AT"] = started.astimezone(timezone.utc).isoformat()
    return environment
