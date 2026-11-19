"""Discover merged PRs without interpreting reviews or writing to GitHub."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from . import tail_github, tail_store
from .pipeline_errors import PipelineError


_BEGINNING = "1970-01-01T00:00:00Z"
_SECOND = timedelta(seconds=1)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _new_pass(state, pass_number=1):
    state.update(pass_number=pass_number, pass_token=uuid4().hex, cursor=None,
                 source_count=None, scanned=0, matched=0, verify_pending=False)
    state.pop("last_created_at", None)
    state.pop("same_second", None)
    if pass_number == 1:
        state["first_digest"] = None


def _generation(previous, repository, base, fingerprint, now):
    compatible = previous and previous["fingerprint"] == fingerprint
    through = previous["discovery_complete_through"] if compatible else None
    lower = max(_BEGINNING, _stamp(_date(through) - timedelta(hours=48))) if through else _BEGINNING
    upper = max(through or _BEGINNING, now)
    state = {
        "generation": previous["generation"] + 1 if previous else 1,
        "fingerprint": fingerprint, "repository": repository, "base": base,
        "lower": lower, "upper": upper, "intervals": [[lower, upper]],
        "mode": "search", "state": "incomplete", "last_error": None,
        "discovery_complete_through": through,
        "last_complete_at": previous["last_complete_at"] if compatible else None,
        "budget_remaining": previous.get("budget_remaining") if previous else None,
        "budget_reset_at": previous.get("budget_reset_at") if previous else None,
    }
    _new_pass(state)
    return state


def _split(state, items):
    """Keep the outstanding intervals on a stack, oldest first."""
    lower, upper = state["intervals"][-1]
    if lower == upper:
        state["mode"] = "enumerate"
    else:
        # A burst of mergers in one second must eventually be enumerated
        # outside Search's 1000-result cap. Isolate that second immediately.
        times = {item["merged_at"] for item in items}
        if len(times) == 1:
            pivot = times.pop()
            intervals = []
            if lower < pivot:
                intervals.append([lower, _stamp(_date(pivot) - _SECOND)])
            intervals.append([pivot, pivot])
            if pivot < upper:
                intervals.append([_stamp(_date(pivot) + _SECOND), upper])
        else:
            midpoint = _date(lower) + (_date(upper) - _date(lower)) // 2
            pivot = midpoint.replace(microsecond=0)
            intervals = [[lower, _stamp(pivot)], [_stamp(pivot + _SECOND), upper]]
        state["intervals"][-1:] = reversed(intervals)
    _new_pass(state)


def _accept_page(state, page):
    scanned = state["scanned"] + page.raw_count
    if state["source_count"] is not None and state["source_count"] != page.total_count:
        raise PipelineError("TAIL source count changed during pagination")
    if page.next_cursor is not None:
        if page.next_cursor == state["cursor"] or not page.raw_count or scanned >= page.total_count:
            raise PipelineError("TAIL source pagination did not advance consistently")
    elif scanned != page.total_count:
        raise PipelineError("TAIL source pagination ended before its reported count")
    if state["mode"] == "enumerate":
        # Validate the entire CREATED_AT-ordered connection, including PRs
        # outside this merge-time interval. Ties need no server-side ordering.
        last = state.get("last_created_at")
        same_second = set(state.get("same_second", ()))
        for created_at, number in page.enumerated:
            if last is not None and created_at < last:
                raise PipelineError("TAIL enumeration repeated or reordered a page")
            if created_at != last:
                same_second.clear()
                last = created_at
            if number in same_second:
                raise PipelineError("TAIL enumeration repeated a pull request")
            same_second.add(number)
        state.update(last_created_at=last, same_second=sorted(same_second))
    state.update(scanned=scanned, source_count=page.total_count,
                 matched=state["matched"] + len(page.items), cursor=page.next_cursor,
                 verify_pending=page.next_cursor is None)


def _finish_pass(repository, state):
    count, digest = tail_store.pass_snapshot(repository, state["pass_token"])
    if count != state["matched"]:
        raise PipelineError("TAIL source returned duplicate PRs during pagination")
    if state["pass_number"] == 1:
        state["first_digest"] = digest
        _new_pass(state, 2)
    elif state["first_digest"] != digest:
        # Search results can legitimately change between two complete passes.
        # Start another pair in the same frozen interval and page budget.
        _new_pass(state)
        state["last_error"] = "TAIL verification passes disagreed"
    else:
        state["intervals"].pop()
        if state["intervals"]:
            state["mode"] = "search"
            _new_pass(state)
        else:
            state.update(state="complete", discovery_complete_through=state["upper"],
                         last_complete_at=_stamp(datetime.now(timezone.utc)), verify_pending=False)
            state.pop("same_second", None)
            state.pop("last_created_at", None)


def discover(gh, config, max_pages):
    if not 1 <= max_pages <= 100:
        raise PipelineError("--max-pages must be between 1 and 100")
    repository, base = config["repository"].lower(), config["base_branch"]
    if not isinstance(base, str) or not base:
        raise PipelineError("TAIL discovery requires a base branch")
    # Bump the query version if pagination, ordering, or filters change.
    query = json.dumps(["merged-tail-v1", repository, base], ensure_ascii=True)
    fingerprint = hashlib.sha256(query.encode("ascii")).hexdigest()
    state = tail_store.load_discovery(repository)
    revision = state.pop("revision") if state else None
    if not state or state["fingerprint"] != fingerprint or state["state"] == "complete":
        state = _generation(state, repository, base, fingerprint, _stamp(datetime.now(timezone.utc)))
        revision = tail_store.save_discovery(repository, revision, state)

    pages = 0
    remaining = None
    while state["state"] != "complete":
        if state["verify_pending"]:
            try:
                _finish_pass(repository, state)
            except PipelineError as exc:
                _new_pass(state)
                state["last_error"] = str(exc)
                tail_store.save_discovery(repository, revision, state)
                raise
            revision = tail_store.save_discovery(repository, revision, state)
            continue
        if remaining == 0 or (state.get("budget_remaining") == 0 and
                              state["budget_reset_at"] > _stamp(datetime.now(timezone.utc))):
            state["last_error"] = "TAIL GraphQL budget exhausted"
            tail_store.save_discovery(repository, revision, state)
            raise PipelineError(state["last_error"])
        if pages >= max_pages:
            break
        lower, upper = state["intervals"][-1]
        pages += 1
        try:
            page = tail_github.read_page(gh, repository, state["mode"], lower, upper,
                                         state["cursor"], base=base)
        except tail_github.SearchUnsupported:
            state["mode"] = "enumerate"
            _new_pass(state)
            revision = tail_store.save_discovery(repository, revision, state)
            continue
        except PipelineError as exc:
            if isinstance(exc, tail_github.InvalidCursor):
                _new_pass(state)
            # Preserve the last committed cursor for transient transport errors.
            state["last_error"] = str(exc)
            tail_store.save_discovery(repository, revision, state)
            raise
        remaining = page.remaining
        state.update(budget_remaining=remaining, budget_reset_at=page.reset_at)
        state["last_error"] = None
        token = state["pass_token"]
        if state["mode"] == "search" and page.total_count >= 1000:
            _split(state, page.items)
        else:
            try:
                _accept_page(state, page)
            except PipelineError as exc:
                _new_pass(state)
                state["last_error"] = str(exc)
                tail_store.save_discovery(repository, revision, state)
                raise
        revision = tail_store.save_discovery(repository, revision, state, page.items, pass_token=token)
    return {"action": "tail-discover", "repository": repository, "state": state["state"],
            "generation": state["generation"], "pages": pages,
            "discovery_complete_through": state["discovery_complete_through"]}
