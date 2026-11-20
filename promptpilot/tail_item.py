"""The issue-creation transaction used by the opt-in canonical TAIL procedure.

The caller selects a proved issue item after checking current main and semantic
duplicates. This module owns the returned lease, ref and intent for one process.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import re
import time
from urllib.parse import quote
from uuid import uuid4

from .pipeline_errors import PipelineError
from .project_pipeline import _parse_github_timestamp, ensure_identity, post_comment
from .tail_admission import normalize_text


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_BODY_LIMIT = 65_536
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_RESERVED = re.compile(r"<!--\s*pp:|^[ \t]*(?:pp:|pp-tail-task-v1\b|Каноничная суть:|Источник:|"
                       r"Reviewed-SHA:|Outcome-Label:|PromptPilot service marker:)", re.M)


def read_issue_context(path):
    if path is None:
        return ""
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            context = file.read(_BODY_LIMIT * 2 + 1).replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        raise PipelineError(f"cannot read TAIL context file as UTF-8: {exc}") from exc
    if len(context) > _BODY_LIMIT:
        raise PipelineError("TAIL context exceeds the GitHub issue body limit")
    if _CONTROL.search(context) or _RESERVED.search(context):
        raise PipelineError("TAIL context contains a control character or reserved protocol line")
    return context.strip("\n") if context.strip() else ""


@dataclass(frozen=True)
class Item:
    title: str
    text: str
    key: str
    dedupe: str
    task_marker: str
    source_marker: str

    @classmethod
    def selected(cls, number, source, index):
        matches = [i for i in source["items"] if i["index"] == index and i["kind"] == "issue"]
        if len(matches) != 1:
            raise PipelineError("selected TAIL item is missing or is not an issue")
        selected = matches[0]
        title, text = normalize_text(selected["title"]), normalize_text(selected["text"])
        if not title or not text:
            raise PipelineError("TAIL item must have a title and task text")
        title_hash, task_hash = _sha(title), _sha(text)
        dedupe = _sha(f"pp-tail-task-v1\ntitle-sha256={title_hash}\ntask-sha256={task_hash}\n")
        key = (f"review-comment={source['review_id']} review-updated={source['review_updated_at']} "
               f"item={index} item-sha256={selected['item_sha256']} dedupe-sha256={dedupe}")
        return cls(title, text, key, dedupe,
                   f"<!-- pp:tail-task-v1 title-sha256={title_hash} task-sha256={task_hash} dedupe-sha256={dedupe} -->",
                   f"<!-- pp:tail-source pr={number} {key} -->")

    @property
    def dedupe_marker(self):
        return f"<!-- pp:tail-dedupe sha256={self.dedupe} -->"

    def issue_body(self, repo, number, source, context):
        title = source.get("title")
        if not isinstance(title, str) or not title.strip() or "\n" in title or _CONTROL.search(title):
            raise PipelineError("missing or malformed source PR title for the TAIL issue")
        link = f"https://github.com/{repo}/pull/{number}#issuecomment-{source['review_id']}"
        paragraphs = [
            f"Найдено при ревью PR #{number} ({escape(title, quote=False)}), в хвост попало как отдельная работа.",
            f"Каноничная суть: {self.text}",
        ]
        if context and context != self.text:
            paragraphs.append(context)
        paragraphs.append(f"Источник: {link}\n{self.source_marker}\n{self.task_marker}\n{self.dedupe_marker}")
        body = "\n\n".join(paragraphs)
        if len(body) > _BODY_LIMIT:
            raise PipelineError("TAIL issue body exceeds the GitHub issue body limit")
        return body


class ItemStop(Exception):
    def __init__(self, state, reason, issue=None, **evidence):
        self.outcome = {"state": state, "reason": reason, "issue": issue, **evidence}
        super().__init__(reason)


_OWNER = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TTL = timedelta(minutes=30)


def _check_item(source, item, index, trusted, *, for_recovery=False):
    """Read every control record for this exact item, including human withdrawals."""
    comments = sorted((c for c in source["comments"] if c["user"]["login"] == trusted),
                      key=lambda c: (_parse_github_timestamp(c["created_at"], field="created_at"), int(c["id"])))
    key = re.escape(item.key)
    drop = "pp:tail-drop " + item.key.split(" dedupe-sha256=")[0]
    roots, transitions, intents = [], [], []
    dropped, completions = False, {}
    for comment in comments:
        lines = comment["body"].replace("\r\n", "\n").split("\n")
        if any(re.fullmatch(r"<!-- pp:tail-done(?: review-comment=[0-9]+ review-updated=\S+)? -->", s)
               for s in lines):
            continue
        for line in lines:
            if line == drop:
                if not for_recovery:
                    raise ItemStop("waiting-policy", "the maintainer dropped this version of the item")
                dropped = True
            legacy = re.fullmatch(r"pp:tail-drop ([1-9][0-9]*(?:,[1-9][0-9]*)*)", line)
            if source["protocol"] == "legacy" and legacy and str(index) in legacy[1].split(","):
                if not for_recovery:
                    raise ItemStop("waiting-policy", "the maintainer dropped this legacy item")
                dropped = True
            done = re.fullmatch(rf"<!-- pp:tail-item-done {key} issue=(none|[1-9][0-9]*) -->", line)
            if done:
                issue = None if done[1] == "none" else int(done[1])
                if comment.get("tail_edited") or comment["updated_at"] != comment["created_at"]:
                    raise ItemStop("invalid-source", "edited TAIL item completion")
                proof_order = (_parse_github_timestamp(source["review_updated_at"], field="review_updated_at"),
                               int(source["review_id"]))
                comment_order = (_parse_github_timestamp(comment["created_at"], field="created_at"),
                                 int(comment["id"]))
                if comment_order <= proof_order or (source.get("review_edited") and comment_order[0] == proof_order[0]):
                    raise ItemStop("invalid-source", "TAIL item completion predates the selected review")
                completions.setdefault(issue, comment)
            root = re.fullmatch(rf"<!-- pp:tail-claim {key} owner=({_OWNER}) -->", line)
            transition = re.fullmatch(rf"<!-- pp:tail-lease {key} previous=([1-9][0-9]*) owner=({_OWNER}) -->", line)
            intent = re.fullmatch(rf"<!-- pp:tail-create-intent {key} lease=([1-9][0-9]*) owner=({_OWNER}) -->", line)
            if root or transition or intent:
                if comment.get("tail_edited") or comment["updated_at"] != comment["created_at"]:
                    raise ItemStop("invalid-source", "edited TAIL ownership record")
                stamp = _parse_github_timestamp(comment["created_at"], field="created_at")
                cid = int(comment["id"])
                if root:
                    roots.append((cid, root[1], stamp, comment["created_at"]))
                elif transition:
                    transitions.append((cid, transition[2], stamp, int(transition[1])))
                else:
                    intents.append((cid, intent[2], int(intent[1])))
    if len(completions) > 1:
        raise ItemStop("invalid-source", "the exact item has conflicting TAIL completions")
    if completions:
        issue, comment = next(iter(completions.items()))
        raise ItemStop("item-done", "the exact item already has a completion", issue,
                       completion_id=comment["id"], completed_at=comment["created_at"])
    lease = roots[0][:3] if roots else None
    for cid, owner, stamp, previous in transitions:
        if (lease and previous == lease[0] and (stamp, cid) > (lease[2], lease[0])
                and (owner == lease[1] or stamp >= lease[2] + _TTL)):
            lease = (cid, owner, stamp)
    return lease, (roots[0][3] if roots else None), intents, dropped


def _lookup(gh, config, item, since=None):
    path = f"repos/{config['repository']}/issues?state=all&per_page=100"
    if since is not None:
        path += "&since=" + quote(since, safe=":-TZ")
    raw = gh.run("api", "--paginate", path, "--jq", ".[]")
    return _match_issues(_issue_rows(raw), config, item, exact=since is not None)


def _issue_rows(raw):
    rows = []
    for line in raw.split("\n"):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise PipelineError("invalid GitHub issue listing") from exc
    return rows


def _match_issues(rows, config, item, *, exact=False):
    found = set()
    for row in rows:
        if not isinstance(row, dict) or "user" not in row:
            raise PipelineError("incomplete GitHub issue listing")
        author = row["user"]
        if "pull_request" in row or author is None:
            continue
        if not isinstance(author, dict) or not isinstance(author.get("login"), str):
            raise PipelineError("incomplete GitHub issue author")
        if author["login"] != config["trusted_account"]:
            continue
        if ("body" not in row or row["body"] is not None and not isinstance(row["body"], str)
                or not isinstance(row.get("title"), str)):
            raise PipelineError("incomplete GitHub issue payload during TAIL deduplication")
        lines = (row["body"] or "").replace("\r\n", "\n").split("\n")
        if item.source_marker not in lines and (exact or item.dedupe_marker not in lines):
            continue
        if (normalize_text(row["title"]) != item.title or lines.count(item.task_marker) != 1
                or lines.count(item.dedupe_marker) != 1
                or lines.count(item.source_marker) > 1
                or [s for s in lines if s.startswith("Каноничная суть: ")] != ["Каноничная суть: " + item.text]
                or type(row.get("number")) is not int or row["number"] <= 0):
            raise ItemStop("ambiguous-create", "an issue carrying this transaction marker has a damaged payload")
        found.add(row["number"])
    return found


def _existing(gh, config, item, root_stamp):
    matches = _lookup(gh, config, item, root_stamp) | _lookup(gh, config, item)
    if len(matches) > 1:
        raise ItemStop("ambiguous-create", "multiple issues carry the same canonical task identity")
    return next(iter(matches), None)


def _create_ref(gh, repo, sha, ref):
    if not re.fullmatch("[0-9a-f]{40}", sha):
        raise PipelineError("invalid base branch HEAD")
    try:
        raw = gh.run("api", "--include", f"repos/{repo}/git/refs", "--method", "POST", "--input", "-",
                     input_value={"ref": ref, "sha": sha})
        header, separator, payload = raw.replace("\r\n", "\n").partition("\n\n")
        if not separator or not re.match(r"^HTTP/[0-9.]+ 201(?: |$)", header.split("\n", 1)[0]):
            raise ValueError("missing 201 Created")
        record = json.loads(payload)
        if record.get("ref") != ref or (record.get("object") or {}).get("sha") != sha:
            raise ValueError("unexpected ref response")
    except (PipelineError, ValueError, AttributeError) as exc:
        raise ItemStop("ambiguous-create", f"no owned 201 receipt for {ref}; check for an existing issue") from exc


def apply_item(gh, config, row, source, index, recheck, context=""):
    number, repo, trusted = row["pr_number"], config["repository"], config["trusted_account"]
    item = Item.selected(number, source, index)
    owner = str(uuid4())
    own_id = own_intent = None
    key = item.key
    body = None

    def comment(body):
        response = post_comment(gh, config, number, body)
        cid = response.get("id")
        if type(cid) is not int or cid <= 0:
            raise PipelineError("no returned id for the TAIL comment")
        return cid

    def inspect():
        nonlocal body
        _, current = recheck()
        body = item.issue_body(repo, number, current, context)
        return _check_item(current, item, index, trusted)

    def owned():
        nonlocal own_id
        lease, root_stamp, intents, _ = inspect()
        now = datetime.now(timezone.utc)
        if (lease is None or lease[:2] != (own_id, owner) or lease[2] > now
                or now >= lease[2] + _TTL):
            raise ItemStop("active-elsewhere", "no current owned lease")
        expected = [(own_intent, owner, own_id)] if own_intent is not None else []
        if intents != expected:
            raise ItemStop("ambiguous-create", f"cannot prove owned create-intent for PR #{number}, item {index}")
        if lease[2] + _TTL - now <= timedelta(minutes=5):
            if intents:
                raise ItemStop("ambiguous-create", f"lease near expiry after intent {own_intent}")
            own_id = comment(f"<!-- pp:tail-lease {key} previous={own_id} owner={owner} -->")
            renewed, root_stamp, intents, _ = inspect()
            if (intents or renewed is None or renewed[:2] != (own_id, owner)
                    or not timedelta(minutes=5) < renewed[2] + _TTL - datetime.now(timezone.utc) <= _TTL):
                raise ItemStop("active-elsewhere", "could not verify the returned TAIL lease renewal")
        return root_stamp

    try:
        ensure_identity(gh, config)
        lease, stamp, intents, _ = inspect()
        if intents:
            existing = _existing(gh, config, item, stamp) if stamp else None
            if existing is not None:
                # The skill verifies recovery ownership and writes the matching
                # item-done. A foreign intent never gives this process create rights.
                return {"state": "existing-issue", "issue": existing, "reason": "recover item-done for existing intent"}
            raise ItemStop("ambiguous-create", f"existing create-intent {intents[0][0]}; reconcile before continuing")
        if lease is None:
            own_id = comment(f"<!-- pp:tail-claim {key} owner={owner} -->")
        elif datetime.now(timezone.utc) >= lease[2] + _TTL:
            own_id = comment(f"<!-- pp:tail-lease {key} previous={lease[0]} owner={owner} -->")
        else:
            raise ItemStop("active-elsewhere", "an unexpired item lease belongs to another process")
        stamp = owned()
        existing = _existing(gh, config, item, stamp)
        if existing is not None:
            owned()
            comment(f"<!-- pp:tail-item-done {key} issue={existing} -->")
            return {"state": "item-done", "issue": existing, "reason": "existing canonical issue"}
        base_ref = gh.json("api", f"repos/{repo}/git/ref/heads/{quote(config['base_branch'], safe='')}")
        sha = (base_ref.get("object") or {}).get("sha", "")
        owned()
        try:
            _create_ref(gh, repo, sha, "refs/heads/pp-tail-dedupe/" + item.dedupe)
        except ItemStop:
            # A concurrent source can publish the same task before or just
            # after our ref attempt. A lost response never grants create rights.
            for delay in (0, 1, 2):
                if delay:
                    time.sleep(delay)
                existing = _existing(gh, config, item, stamp)
                if existing is not None:
                    owned()
                    comment(f"<!-- pp:tail-item-done {key} issue={existing} -->")
                    return {"state": "item-done", "issue": existing, "reason": "concurrent canonical issue"}
            raise
        owned()
        own_intent = comment(f"<!-- pp:tail-create-intent {key} lease={own_id} owner={owner} -->")
        stamp = owned()
        existing = _existing(gh, config, item, stamp)
        owned()
        if existing is None:
            try:
                result = gh.json("api", f"repos/{repo}/issues", "--method", "POST", "--input", "-",
                                 input_value={"title": item.title, "body": body})
                existing = result.get("number")
                if (type(existing) is not int or existing <= 0 or result.get("body") != body
                        or result.get("title") != item.title or (result.get("user") or {}).get("login") != trusted):
                    raise PipelineError("issue POST did not return the exact created issue")
            except (PipelineError, OSError, ValueError) as exc:
                raise ItemStop("ambiguous-create", f"issue POST outcome unknown; retain intent {own_intent}") from exc
        # Once the exact issue exists, a late hold/proof change closes the
        # ordinary write path. Hand the number to the canonical recovery step,
        # which can prove and record only that already-observed issue.
        try:
            owned()
            comment(f"<!-- pp:tail-item-done {key} issue={existing} -->")
        except (PipelineError, ItemStop, OSError, ValueError) as exc:
            return {"state": "existing-issue", "issue": existing,
                    "reason": f"verify the exact issue before restoring item-done: {exc}"}
        return {"state": "item-done", "issue": existing}
    except ItemStop as stop:
        return stop.outcome
