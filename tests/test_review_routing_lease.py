"""Ownership races/recovery boundaries; no GitHub or executor side effects."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_lease import LeaseNode, resolve


A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
C = "00000000-0000-4000-8000-000000000003"
CONTEXT = "a" * 64
PURPOSE = "technical-attempt"


def clock(value):
    return datetime.fromisoformat("2026-10-29T" + value + "+00:00")


def node(ident, owner=A, at="00:00:00", *, root=None, previous=None):
    return LeaseNode(ident, owner, "2026-10-29T" + at + "Z", CONTEXT, PURPOSE, root, previous)


def view(nodes, at="00:29:00", **kwargs):
    return resolve(nodes, context_sha256=CONTEXT, purpose=PURPOSE, now=clock(at), **kwargs)


def test_same_second_roots_use_exact_large_decimal_order_and_returned_id():
    early = node("9007199254740992")
    later = node("9007199254740993", B)
    result = view([later, early])
    assert result.root.id == early.id
    result.require_owner(returned_id=early.id, owner=A, now=clock("00:29:00"))
    with pytest.raises(PipelineError, match="returned ID/UUID"):
        result.require_owner(returned_id=later.id, owner=A, now=clock("00:29:00"))
    assert result.ignored_ids == (later.id,)
    assert result.to_dict()["mutation_authorized"] is False


def test_renew_then_takeover_never_revives_losing_sibling():
    root = node("10")
    renew = node("20", at="00:26:00", root="10", previous="10")
    loser = node("21", at="00:27:00", root="10", previous="10")
    loser_child = node("40", B, "00:58:00", root="10", previous="21")
    takeover = node("30", B, "00:56:00", root="10", previous="20")
    result = view([loser_child, root, loser, takeover, renew], at="00:58:01")
    assert [item.id for item in result.chain] == ["10", "20", "30"]
    result.require_owner(returned_id="30", owner=B, now=clock("00:58:01"))
    with pytest.raises(PipelineError, match="returned ID/UUID"):
        result.require_owner(returned_id="20", owner=A, now=clock("00:58:01"))


@pytest.mark.parametrize("owner,at,chosen", [
    (A, "00:25:00", "10"),  # Strictly less than five minutes remain.
    (A, "00:25:01", "20"),
    (B, "00:29:59", "10"),
    (A, "00:30:00", "10"),  # Expiry requires a fresh UUID, not a renewal.
    (B, "00:30:00", "20"),
])
def test_renew_and_takeover_boundaries(owner, at, chosen):
    result = view([node("10"), node("20", owner, at, root="10", previous="10")], at=at)
    assert result.active.id == chosen


def test_expiry_is_not_permission_to_keep_mutating_or_start_an_executor():
    result = view([node("10")], at="00:30:00")
    with pytest.raises(PipelineError, match="not currently active"):
        result.require_owner(returned_id="10", owner=A, now=clock("00:30:00"))
    assert result.to_dict()["mutation_authorized"] is False


def test_deleted_winner_cannot_resurrect_loser():
    with pytest.raises(PipelineError, match="deletion"):
        view([node("11", B)], deleted_at=["2026-10-29T00:00:00Z"])


def test_winner_deleted_before_surviving_loser_was_created_also_refuses():
    with pytest.raises(PipelineError, match="deletion"):
        view([node("11", B, at="00:10:00")],
             deleted_at=["2026-10-29T00:05:00Z"])


def test_context_and_purpose_conflicts_are_not_equivalent_losers():
    with pytest.raises(PipelineError, match="context or purpose"):
        view([node("10"), replace(node("11", B), context_sha256="b" * 64)])
    with pytest.raises(PipelineError, match="context or purpose"):
        view([node("10"), replace(node("11", B), purpose="review-publication")])


def test_losing_root_cannot_supply_the_active_lease():
    result = view([node("10"), node("11", B),
                   node("20", B, "00:26:00", root="11", previous="11")])
    assert result.active.id == "10"
    with pytest.raises(PipelineError, match="returned ID/UUID"):
        result.require_owner(returned_id="20", owner=B, now=clock("00:29:00"))


@pytest.mark.parametrize("nodes", [
    [node("10"), node("20", at="00:26:00", root="10", previous="15")],
    [node("10"), node("20", at="00:26:00", root="9", previous="10")],
    [node("10"), node("11", B), node("20", at="00:26:00", root="10", previous="11")],
    [node("10"), node("20", at="00:26:00", root="10", previous="21"),
     node("21", at="00:27:00", root="10", previous="20")],
    [node("10"), node("10", B)],
])
def test_partial_cross_root_cyclic_and_duplicate_history_refuses(nodes):
    with pytest.raises(PipelineError):
        view(nodes)


@pytest.mark.parametrize("ident", [0, 1, True, "0", "01", "1.0", "1e2", "-1", "1\n", "1" * 129])
def test_ids_are_canonical_decimal_strings(ident):
    with pytest.raises(PipelineError, match="decimal string"):
        node(ident)


def test_future_active_record_and_naive_clock_refuse():
    with pytest.raises(PipelineError, match="future"):
        view([node("10", at="00:30:00")])
    with pytest.raises(PipelineError, match="timezone aware"):
        resolve([node("10")], context_sha256=CONTEXT, purpose=PURPOSE,
                now=datetime(2026, 10, 29))
    # A normal aware clock remains a read-only view.
    result = resolve([node("10")], context_sha256=CONTEXT, purpose=PURPOSE,
                     now=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert result.active.owner == A
