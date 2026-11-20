"""Full legacy post-push proof with live transport and object-store boundaries replaced separately."""

import hashlib
import os
from pathlib import Path

import pytest

from promptpilot import (
    merge_done_proof as full,
    merge_finish_proof as finish,
    merge_ship_authorization as lineage,
    project_pipeline as pp,
)
from tests.test_base_sync_carry import git, commit
from tests.test_merge_finish_proof import (
    Replay,
    ACCOUNT,
    BASE,
    CHECKPOINT,
    CONFIG,
    DONE,
    HEAD,
    INTENT,
    INTENT_PREAMBLE,
    N,
    OLD,
    TREE,
)


class FullReplay(Replay):
    def __init__(self):
        super().__init__()
        self.cp = dict(CHECKPOINT)
        self.fork = None
        self.allow_modify = False
        self.current_ci = "success"
        self.tamper_ci_identity = False
        self.calls_to_ref = []
        self.ci_missing = False

    def json(self, verb, path, *args, input_value=None):
        if path.endswith("/git/commits/" + self.cp["to"]) and self.cp["to"] != HEAD:
            return {
                "sha": self.cp["to"],
                "parents": [{"sha": HEAD}, {"sha": BASE}],
                "tree": {"sha": TREE},
            }
        result = super().json(verb, path, *args, input_value=input_value)
        if path.endswith(f"/pulls/{N}"):
            result["head"]["sha"] = self.cp["to"]
            if self.fork:
                result["head"]["repo"]["full_name"] = self.fork
                result["maintainer_can_modify"] = self.allow_modify
        if f"/commits/{self.cp['to']}/check-runs?" in path:
            result["check_runs"][0]["conclusion"] = self.current_ci
            result["check_runs"][0]["status"] = (
                "completed" if self.current_ci else "queued"
            )
            if self.ci_missing:
                result.update(check_runs=[], total_count=0)
            if self.tamper_ci_identity:
                result["check_runs"][0]["head_sha"] = OLD
        return result

    def add_hop(self):
        self.comment(888, DONE)
        epoch = hashlib.sha256(
            f"pp-review-epoch-v1\nhead={HEAD}\nanchor-node=B\n".encode()
        ).hexdigest()
        self.comment(
            201,
            f"Reviewed-SHA: {HEAD}\nOutcome-Label: reviewed\n<!-- pp:review pp:tail=0 -->",
        )
        self.comment(
            202,
            f"<!-- pp:review-claim {HEAD} review-comment=201 epoch-sha256={epoch} -->",
        )
        self.comment(
            203,
            f"<!-- pp:head-reviewed {HEAD} review-comment=201 claim=202 epoch-sha256={epoch} -->",
        )
        self.comment(
            300,
            f"<!-- pp:base-sync-intent from={HEAD} base={BASE} review-comment=201 "
            "claim=202 completion=203 ship-event=ship previous=888 -->",
        )
        self.current_head = "e" * 40
        self.add("PullRequestCommit", "E", commit={"oid": self.current_head})
        self.cp.update(intent=300, to=self.current_head, **{"from": HEAD})
        for edge in self.edges:
            if "createdAt" in edge["node"]:
                edge["node"]["createdAt"] = "2026-11-19T14:00:00Z"

    def prepare(self):
        return full.prepare(self, CONFIG, self.cp, working_dir=Path.cwd())


@pytest.fixture
def replay(monkeypatch):
    result = FullReplay()
    monkeypatch.setattr(full, "_tree", lambda *args: None)
    monkeypatch.setattr(
        full,
        "_ref",
        lambda config, directory, *args, **kw: result.calls_to_ref.append((args, kw)),
    )
    return result


def test_first_hop_and_post_readback(replay):
    body, done, before = replay.prepare()
    assert (body, done) == (DONE, None)
    assert replay.prepare()[2] == before
    replay.comment(888, body)
    body, done, after = replay.prepare()
    assert (body, done) == (DONE, 888) and after != before
    assert not replay.posts


def test_verbatim_2731_comment_and_multihop_preparation(replay):
    # ivanarama/onebase issuecomment-7049065664, including its explanatory first line.
    actual = (
        "PromptPilot service marker: MERGE base synchronization intent.\n"
        "<!-- pp:base-sync-intent from=32c32c993b26795014b93ca7a3546b3969307add "
        "base=ad337867f31bbdfb54bbe65ccc66d05c7aa1fc69 review-comment=7009432714 claim=7009433577 "
        "completion=7009435441 ship-event=LE_lADOSKrn-s8AAAABgsRv-c8AAAAIiJB2SA previous=none -->"
    )
    assert hashlib.sha256(actual.encode()).hexdigest() == "4c5c522e4213d3a4b05f51d061c8ac7cf326b2dcb4aa809db1344f5e6c48ea65"
    assert lineage.intent_line(actual)[0] == actual.split("\n")[1]
    intent = replay.edges[-2]["node"]
    body, _, fingerprint = replay.prepare()
    intent["body"] = INTENT_PREAMBLE + INTENT
    observed, receipt, with_preamble = replay.prepare()
    assert observed == body and receipt is None and with_preamble != fingerprint
    replay.add_hop()
    replay.edges[-2]["node"]["body"] = INTENT_PREAMBLE + replay.edges[-2]["node"]["body"]
    assert "previous=888" in replay.prepare()[0] and not replay.posts


@pytest.mark.parametrize("body", [
    "> " + INTENT, "    " + INTENT, "`" + INTENT + "`", "~~~md\n" + INTENT + "\n~~~",
    "````\n```\n" + INTENT + "\n````", "<blockquote>\n" + INTENT + "\n</blockquote>",
    INTENT + "\n" + INTENT, INTENT + "\n<!-- pp:base-sync-intent malformed -->",
    INTENT + "\n" + DONE, INTENT + "\npp:review-again", INTENT.replace("claim=102", "claim=0102"),
    INTENT.replace("from=" + OLD, "from=" + HEAD),
])
def test_one_standalone_intent_required_before_recovery(replay, body):
    replay.edges[-2]["node"]["body"] = body
    with pytest.raises(pp.PipelineError):
        replay.prepare()
    assert not replay.posts


def test_full_preparation_requires_identical_explanatory_text_in_rest(replay, monkeypatch):
    replay.edges[-2]["node"]["body"] = INTENT_PREAMBLE + INTENT
    get = replay.json

    def drift(verb, path, *args, **kwargs):
        value = get(verb, path, *args, **kwargs)
        if path.endswith("/issues/comments/164"):
            return dict(value, body="Different explanation.\n" + INTENT)
        return value

    monkeypatch.setattr(replay, "json", drift)
    with pytest.raises(pp.PipelineError, match="exact UTF-8 readback"):
        replay.prepare()
    assert not replay.posts


@pytest.mark.parametrize("state", ["failure", None, "absent"])
def test_new_ci_is_observed_even_before_it_passes(replay, state):
    green = replay.prepare()[2]
    replay.current_ci = state
    replay.ci_missing = state == "absent"
    assert replay.prepare()[2] != green
    replay.ci_error = True
    with pytest.raises(pp.PipelineError, match="source CI8"):
        replay.prepare()


def test_mismatched_new_check_sha(replay):
    replay.tamper_ci_identity = True
    with pytest.raises(pp.PipelineError, match="unbound new-head"):
        replay.prepare()


def test_only_exact_writable_fork(replay):
    replay.fork = "other/onebase"
    with pytest.raises(pp.PipelineError):
        replay.prepare()
    replay.allow_modify = True
    assert replay.prepare()[:2] == (DONE, None)
    assert [args[0] for args, kw in replay.calls_to_ref if args[1] != "main"] == [
        "https://github.com/other/onebase.git"
    ] * 2


def test_advanced_intent_and_main_require_both_ancestry_proofs(replay, monkeypatch):
    earlier, tip = "1" * 40, "2" * 40
    replay.main = tip
    replay.edges[-2]["node"]["body"] = replay.edges[-2]["node"]["body"].replace(
        "base=" + BASE, "base=" + earlier
    )
    observed = []
    monkeypatch.setattr(
        finish, "_git", lambda config, directory, *args: observed.append(args)
    )
    assert replay.prepare()[:2] == (DONE, None)
    assert observed == [
        ("merge-base", "--is-ancestor", earlier, BASE),
        ("merge-base", "--is-ancestor", BASE, tip),
    ]

    def reject(*args):
        raise pp.PipelineError("not ancestor")

    monkeypatch.setattr(finish, "_git", reject)
    with pytest.raises(pp.PipelineError, match="not ancestor"):
        replay.prepare()


def test_multi_hop_uses_old_review_and_original_ship(replay):
    replay.add_hop()
    body, receipt, _ = replay.prepare()
    assert (
        receipt is None
        and f"from={HEAD}" in body
        and "previous=888 ship-event=ship" in body
    )
    replay.comment(900, body)
    assert replay.prepare()[:2] == (body, 900)
    report = dict(
        replay.cp,
        proof_kind="legacy-general-done-v1",
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    assert finish.observe_receipt(replay, report, config=CONFIG) == 900
    report.pop("proof_kind")
    assert finish.observe_receipt(replay, report, config=CONFIG) is None


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-previous",
        "revocation",
        "foreign-intent",
        "old-proof",
        "duplicate",
        "node-drift",
    ],
)
def test_faulty_lineage_cannot_arm(replay, fault):
    replay.add_hop()
    intent = replay.edges[-2]["node"]
    if fault == "wrong-previous":
        intent["body"] = intent["body"].replace("previous=888", "previous=887")
    elif fault == "revocation":
        replay.add(
            "UnlabeledEvent",
            "unship",
            label={"name": "ship"},
            actor={"login": ACCOUNT},
            createdAt="2026-11-19T15:00:00Z",
        )
    elif fault == "foreign-intent":
        intent["author"]["login"] = "someone-else"
    elif fault == "old-proof":
        replay.edges[1]["node"]["body"] = "Edited"
    elif fault == "duplicate":
        replay.comment(600, intent["body"].replace("claim=202", "claim=999"))
    else:
        replay.node_drift = True
    with pytest.raises((ValueError, pp.PipelineError)):
        replay.prepare()
    assert not replay.posts


def test_commit_date_can_place_the_only_new_commit_before_intent(replay):
    replay.edges.insert(1, replay.edges.pop())
    assert replay.prepare()[:2] == (DONE, None)


def test_later_identical_intent_does_not_replace_earliest(replay):
    body = replay.edges[-2]["node"]["body"]
    replay.comment(176, body)
    assert replay.prepare()[:2] == (DONE, None)
    replay.cp["intent"] = 176
    with pytest.raises(full.UnsupportedProof, match="earliest"):
        replay.prepare()


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("fault", ["force-restore", "abort", "extra-commit", "regrant"])
def test_projected_history_preserves_all_other_lifecycle_events(replay, multi, fault):
    if multi:
        replay.add_hop()
    if fault == "force-restore":
        replay.add("HeadRefForcePushedEvent", "force1", afterCommit={"oid": "f" * 40})
        replay.add(
            "HeadRefForcePushedEvent", "force2", afterCommit={"oid": replay.cp["to"]}
        )
    elif fault == "abort":
        replay.comment(
            1000,
            f"<!-- pp:base-sync-v1-aborted intent={replay.cp['intent']} "
            f"head={replay.cp['to']} reason=commit-before-intent -->",
        )
    elif fault == "extra-commit":
        replay.add("PullRequestCommit", "extra", commit={"oid": "f" * 40})
    else:
        replay.add(
            "UnlabeledEvent", "unship", label={"name": "ship"}, actor={"login": ACCOUNT}
        )
        replay.add(
            "LabeledEvent", "again", label={"name": "ship"}, actor={"login": ACCOUNT}
        )
    with pytest.raises((ValueError, pp.PipelineError)):
        replay.prepare()
    assert not replay.posts


def test_historical_receipt_requires_exact_body_even_if_branch_moved(replay):
    historical = "f" * 40
    replay.edges[-2]["node"]["body"] = replay.edges[-2]["node"]["body"].replace(
        "base=" + BASE, "base=" + historical
    )
    replay.comment(888, DONE)
    replay.current_head = "1" * 40
    record = dict(
        CHECKPOINT,
        proof_kind="legacy-general-done-v1",
        body_sha256=hashlib.sha256(DONE.encode()).hexdigest(),
    )
    assert finish.observe_receipt(replay, record, config=CONFIG) == 888
    replay.edges[-1]["node"]["body"] += "\n"
    assert finish.observe_receipt(replay, record, config=CONFIG) is None


def test_real_git_rejects_changes_outside_conflict(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "core.autocrlf", "false")
    commit(tmp_path, "initial", **{"CHANGELOG.md": "tail\n", "code": "unchanged\n"})
    git(tmp_path, "checkout", "-b", "feature")
    old = commit(tmp_path, "ours", **{"CHANGELOG.md": "ours\ntail\n"})
    git(tmp_path, "checkout", "main")
    base = commit(tmp_path, "theirs", **{"CHANGELOG.md": "theirs\ntail\n"})
    git(tmp_path, "checkout", "feature")
    (tmp_path / "CHANGELOG.md").write_bytes(b"ours\ntheirs\ntail\n")
    git(tmp_path, "add", "CHANGELOG.md")

    def snapshot():
        tree = git(tmp_path, "write-tree")
        head = git(tmp_path, "commit-tree", tree, "-p", old, "-p", base, "-m", "merge")
        return head, tree

    calls = []
    monkeypatch.setattr(full, "_check_document", lambda *args: calls.append(args[2:]))
    full._tree({}, tmp_path, old, base, *snapshot())
    assert calls[0][0] == b"CHANGELOG.md" and len(calls[0][1]) == 3
    (tmp_path / "code").write_bytes(b"modified\n")
    git(tmp_path, "add", "code")
    with pytest.raises(full.UnsupportedProof, match="outside the exact conflict"):
        full._tree({}, tmp_path, old, base, *snapshot())
    assert len(calls) == 1


def test_pinned_canonical_checker_with_historical_2712():
    checkout = os.environ.get("PP_TEST_ONEBASE_GIT_OBJECTS")
    if not checkout:
        pytest.skip(
            "set PP_TEST_ONEBASE_GIT_OBJECTS to a checkout containing the frozen 2712 merge objects"
        )
    source = "0ca97df7e77cca9a531b3d95c6036db292504b59"
    base = "39fd4947d965617027673e21bb2d77aaebfb47d3"
    head = "a87aff42bb2cd3122610d42ef596a029d7674ce3"
    tree = "75cb6f1728a77ff5ab4c4b075c87b37e5668c778"
    full._tree({}, checkout, source, base, head, tree)
    inputs = [
        "09b4a444fbd0bc4fc7f1c3fb345970ebab224442",
        "afd8495e8474924123953cf2fab9c60020876ca7",
        "47929b1f432083924a2556b5b79c0b5353727257",
    ]
    with pytest.raises(full.UnsupportedProof):
        full._check_document({}, checkout, b"CHANGELOG.md", inputs, inputs[0])
