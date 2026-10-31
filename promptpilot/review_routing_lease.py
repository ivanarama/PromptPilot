"""Pure ownership reducer for review-routing-v1, not mutation authority.

The adapter must first authenticate immutable records, bind their complete
context and prove a complete history/deletion baseline. This module selects a
30-minute lease within ONE such operation; it neither dispatches an executor
nor treats expiry as evidence that a previous executor stopped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID

from .pipeline_errors import PipelineError


TTL = timedelta(minutes=30)
RENEW_WINDOW = timedelta(minutes=5)
PURPOSES = {"review-publication", "technical-attempt"}


def decimal_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,127}", value)):
        raise PipelineError("routing lease ID must be a bounded positive decimal string")
    return value


def owner_id(value):
    if not isinstance(value, str):
        raise PipelineError("routing lease owner must be UUID4 text")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise PipelineError("routing lease owner must be UUID4 text") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise PipelineError("routing lease owner must be canonical UUID4 text")
    return value


def timestamp(value):
    if (not isinstance(value, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value)):
        raise PipelineError("routing lease requires an exact UTC server timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PipelineError("invalid routing lease server timestamp") from exc


def _now(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PipelineError("routing lease clock must be timezone aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class LeaseNode:
    id: str
    owner: str
    created_at: str
    context_sha256: str
    purpose: str
    root: str | None = None
    previous: str | None = None

    def __post_init__(self):
        decimal_id(self.id)
        owner_id(self.owner)
        timestamp(self.created_at)
        if (not isinstance(self.context_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.context_sha256)):
            raise PipelineError("routing lease context hash is invalid")
        if not isinstance(self.purpose, str) or self.purpose not in PURPOSES:
            raise PipelineError("routing lease purpose is unsupported")
        if (self.root is None) != (self.previous is None):
            raise PipelineError("routing lease child requires root and previous")
        if self.root is not None:
            decimal_id(self.root)
            decimal_id(self.previous)

    @property
    def time(self):
        return timestamp(self.created_at)

    @property
    def order(self):
        # IDs must never pass through float/JavaScript Number.
        return self.time, len(self.id), self.id


@dataclass(frozen=True)
class LeaseView:
    root: LeaseNode
    active: LeaseNode
    chain: tuple[LeaseNode, ...]
    ignored_ids: tuple[str, ...]

    @property
    def expires_at(self):
        return self.active.time + TTL

    def require_owner(self, *, returned_id, owner, now):
        decimal_id(returned_id)
        owner_id(owner)
        current = _now(now)
        if returned_id != self.active.id or owner != self.active.owner:
            raise PipelineError("routing lease is owned by another returned ID/UUID")
        if not self.active.time <= current < self.expires_at:
            raise PipelineError("routing lease is not currently active")

    def to_dict(self):
        return {"root_id": self.root.id, "active_id": self.active.id,
                "owner": self.active.owner, "expires_at": self.expires_at.isoformat(),
                "chain": [node.id for node in self.chain],
                "ignored_ids": list(self.ignored_ids), "mutation_authorized": False}


def _eligible(child, parent):
    if child.order <= parent.order:
        return False
    expiry = parent.time + TTL
    if child.time < expiry:
        return child.owner == parent.owner and expiry - child.time < RENEW_WINDOW
    return child.owner != parent.owner


def resolve(nodes, *, context_sha256, purpose, now, deleted_at=()):
    """Select the canonical root/active chain without issuing any permission.

    Equivalent roots must already share the same record/context hash excluding
    owner UUID. Conflicting contexts cannot be converted into harmless losers.
    The lifetime technical-attempt ledger is a separate, mandatory gate.
    """
    current = _now(now)
    if not isinstance(nodes, (list, tuple)) or not 1 <= len(nodes) <= 4096:
        raise PipelineError("routing lease requires a complete bounded node list")
    if any(not isinstance(node, LeaseNode) for node in nodes):
        raise PipelineError("routing lease requires validated nodes")
    if any(node.context_sha256 != context_sha256 or node.purpose != purpose for node in nodes):
        raise PipelineError("conflicting routing lease context or purpose")
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise PipelineError("duplicate routing lease server ID")
    roots = sorted((node for node in nodes if node.root is None), key=lambda node: node.order)
    if not roots:
        raise PipelineError("routing lease root is missing")
    root = roots[0]
    # The caller supplies deletion events relevant to the proved operation
    # baseline. Even a deletion before the earliest *surviving* root can have
    # removed its true predecessor/winner. Never resurrect a surviving loser.
    deletions = tuple(timestamp(value) for value in deleted_at)
    if deletions:
        raise PipelineError("comment deletion invalidates routing lease history")
    root_ids = {node.id for node in roots}
    children = {}
    for node in nodes:
        if node.root is None:
            continue
        if node.root not in root_ids or node.previous not in by_id:
            raise PipelineError("routing lease references missing history")
        parent = by_id[node.previous]
        if ((parent.root is None and parent.id != node.root)
                or (parent.root is not None and parent.root != node.root)):
            raise PipelineError("routing lease crosses root ownership")
        children.setdefault(node.previous, []).append(node)
    # Reject cycles even on a losing branch: missing/invalid history is not an
    # empty budget or permission to revive another branch.
    finished = set(root_ids)
    for node in nodes:
        seen = set()
        cursor = node
        while cursor.id not in finished:
            if cursor.id in seen:
                raise PipelineError("routing lease history contains a cycle")
            seen.add(cursor.id)
            cursor = by_id[cursor.previous]
        finished.update(seen)
    chain = [root]
    while True:
        eligible = sorted((node for node in children.get(chain[-1].id, [])
                           if node.root == root.id and _eligible(node, chain[-1])),
                          key=lambda node: node.order)
        if not eligible:
            break
        chain.append(eligible[0])
    if chain[-1].time > current:
        raise PipelineError("routing lease server time is in the future")
    selected = {node.id for node in chain}
    ignored = tuple(node.id for node in sorted(nodes, key=lambda node: node.order)
                    if node.id not in selected)
    return LeaseView(root, chain[-1], tuple(chain), ignored)
