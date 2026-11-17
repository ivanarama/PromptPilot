"""Local dashboard visibility, separate from task state and source permissions."""
import json

from . import db, task_feed

PREFIX = "task-feed-hidden:"


def setting_key(source_id, kind, object_id):
    return PREFIX + json.dumps([source_id, kind, str(object_id)], separators=(",", ":"))


def set_hidden(source_id, kind, object_id, hidden):
    key = setting_key(source_id, kind, object_id)
    if hidden:
        db.set_setting(key, "1")
    else:
        db.delete_setting(key)


def apply(items, *, include_hidden=False):
    # Keep GET read-only, including older databases without a settings table.
    with task_feed._snapshot() as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        hidden = {row[0] for row in connection.execute(
            "SELECT key FROM settings WHERE substr(key,1,?)=? AND value='1'", (len(PREFIX), PREFIX)
        )} if exists else set()
    result = []
    for item in items:
        is_hidden = setting_key(item["source_id"], item["kind"], item["object_id"]) in hidden
        if include_hidden or not is_hidden:
            result.append({**item, "hidden": True} if is_hidden else item)
    return result
