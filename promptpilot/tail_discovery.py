"""Discover merged PRs without interpreting reviews or writing to GitHub."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from . import tail_github, tail_store
from .pipeline_errors import PipelineError


_BEGINNING = "1970-01-01T00:00:00Z"
_SECOND = timedelta(seconds=1)
_MAX_PAGES = 100


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
    source_count = state["source_count"] if state["source_count"] is not None else page.total_count
    # Enumeration walks towards the oldest PR from a fixed first page. New PRs
    # can increase the live total, but they are behind the saved cursor.
    if page.total_count < source_count or state["mode"] == "search" and page.total_count != source_count:
        raise PipelineError("TAIL source count changed during pagination")
    if page.next_cursor is not None:
        if page.next_cursor == state["cursor"] or not page.raw_count or scanned >= source_count:
            raise PipelineError("TAIL source pagination did not advance consistently")
    elif scanned != source_count:
        raise PipelineError("TAIL source pagination ended before its reported count")
    if state["mode"] == "enumerate":
        # GitHub returns each backwards page in ascending creation order.
        # Visit it newest first so adjacent pages share the same ordering.
        last = state.get("last_created_at")
        same_second = set(state.get("same_second", ()))
        for created_at, number in reversed(page.enumerated):
            if last is not None and created_at > last:
                raise PipelineError("TAIL enumeration repeated or reordered a page")
            if created_at != last:
                same_second.clear()
                last = created_at
            if number in same_second:
                raise PipelineError("TAIL enumeration repeated a pull request")
            same_second.add(number)
        state.update(last_created_at=last, same_second=sorted(same_second))
    state.update(scanned=scanned, source_count=source_count,
                 matched=state["matched"] + len(page.items), cursor=page.next_cursor,
                 verify_pending=page.next_cursor is None)


def _finish_pass(repository, state, revision):
    count, digest = tail_store.pass_snapshot(repository, state["pass_token"], revision)
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
    if not 1 <= max_pages <= _MAX_PAGES:
        raise PipelineError("--max-pages must be between 1 and 100")
    repository, base = config["repository"].lower(), config["base_branch"]
    if not isinstance(base, str) or not base:
        raise PipelineError("TAIL discovery requires a base branch")
    fingerprint = tail_store.query_fingerprint(repository, base)
    state = tail_store.load_discovery(repository)
    revision = state.pop("revision") if state else None
    remaining = reset_at = None

    def join_checkpoint(receipt=None):
        nonlocal state, revision
        budget = (remaining, reset_at) if remaining is not None else None
        current = tail_store.load_discovery(repository, budget=budget)
        if (current is None or current["fingerprint"] != fingerprint
                or current["generation"] != state["generation"]
                or (receipt is not None and receipt not in current.get("page_receipts", []))):
            raise tail_store.TailDiscoveryConflict("TAIL discovery changed during this request")
        if current["last_error"]:
            raise PipelineError(current["last_error"])
        revision = current.pop("revision")
        state = current

    def advance(observations=(), *, token=None, evidence=None):
        nonlocal revision
        receipt = None
        if evidence is not None:
            # Bind the actual page to the source revision, including the set,
            # ordering and next cursor. Rate-limit receipts vary per request.
            payload = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
            receipt = [revision, hashlib.sha256(payload).hexdigest()]
            # Keep up to one full CLI page budget. A worker delayed beyond
            # that bounded history must retry from the durable checkpoint.
            state["page_receipts"] = [*state.get("page_receipts", []), receipt][-_MAX_PAGES:]
        try:
            revision = tail_store.save_discovery(repository, revision, state, observations, pass_token=token)
        except tail_store.TailDiscoveryConflict:
            # Local steps (start/verify) have no outstanding remote page.
            # Never write our stale cursor or report our uncommitted progress.
            join_checkpoint(receipt)

    def record_failure():
        try:
            tail_store.save_discovery(repository, revision, state)
        except tail_store.TailDiscoveryConflict:
            # Keep the winner's cursor. The caller still reports the actual
            # API/verification failure from this invocation.
            if remaining is not None:
                tail_store.load_discovery(repository, budget=(remaining, reset_at))

    if not state or state["fingerprint"] != fingerprint or state["state"] == "complete":
        state = _generation(state, repository, base, fingerprint, _stamp(datetime.now(timezone.utc)))
        advance()

    pages = 0
    while state["state"] != "complete":
        if state["verify_pending"]:
            try:
                _finish_pass(repository, state, revision)
            except tail_store.TailDiscoveryConflict:
                join_checkpoint()
                continue
            except PipelineError as exc:
                _new_pass(state)
                state["last_error"] = str(exc)
                record_failure()
                raise
            advance()
            continue
        if remaining == 0 or (state.get("budget_remaining") == 0 and
                              state["budget_reset_at"] > _stamp(datetime.now(timezone.utc))):
            state["last_error"] = "TAIL GraphQL budget exhausted"
            record_failure()
            raise PipelineError(state["last_error"])
        if pages >= max_pages:
            break
        lower, upper = state["intervals"][-1]
        pages += 1
        try:
            page = tail_github.read_page(gh, repository, state["mode"], lower, upper,
                                         state["cursor"], base=base)
        except PipelineError as exc:
            if isinstance(exc, tail_github.SourceError) and exc.receipt is not None:
                remaining, reset_at = exc.receipt
                state.update(budget_remaining=remaining, budget_reset_at=reset_at)
            if isinstance(exc, tail_github.SearchUnsupported):
                if not exc.requested:
                    pages -= 1
                state["mode"] = "enumerate"
                _new_pass(state)
                advance(evidence=["enumerate", str(exc)])
                continue
            if isinstance(exc, tail_github.InvalidCursor):
                _new_pass(state)
            # Preserve the last committed cursor for transient transport errors.
            state["last_error"] = str(exc)
            record_failure()
            raise
        remaining, reset_at = page.remaining, page.reset_at
        state.update(budget_remaining=remaining, budget_reset_at=reset_at)
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
                record_failure()
                raise
        advance(page.items, token=token, evidence=[page.total_count, page.raw_count, page.next_cursor,
                                                   page.items, page.enumerated])
    return {"action": "tail-discover", "repository": repository, "state": state["state"],
            "generation": state["generation"], "pages": pages,
            "discovery_complete_through": state["discovery_complete_through"]}
