"""The terminal handoff may publish one first-hop done and nothing else."""

import copy
import hashlib
from pathlib import Path

import pytest

from promptpilot import merge_finish_proof as finish
from promptpilot import project_pipeline as pp


REPO = "ivanarama/onebase"
ACCOUNT = "ivanarama"
N = 2711
OLD, BASE, HEAD, TREE = [char * 40 for char in "abcd"]
CHECKS = [
    "build",
    "lint",
    "postgres-integration",
    "vuln",
    "smoke",
    "e2e",
    "test-windows",
    "launcher-webview-build",
]
CONFIG = {"repository": REPO, "base_branch": "main", "trusted_account": ACCOUNT, "required_checks": CHECKS}
CHECKPOINT = {
    "repository": REPO,
    "number": N,
    "from": OLD,
    "base": BASE,
    "to": HEAD,
    "tree": TREE,
    "intent": 164,
    "next_step": "confirm-base-sync-done",
}
INTENT = (
    f"<!-- pp:base-sync-intent from={OLD} base={BASE} review-comment=101 "
    "claim=102 completion=103 ship-event=ship previous=none -->"
)
DONE = (
    f"<!-- pp:base-sync-done intent=164 from={OLD} to={HEAD} base={BASE} previous=none ship-event=ship -->"
)


class Replay:
    def __init__(self):
        self.edges, self.posts, self.calls = [], [], []
        self.epoch = hashlib.sha256(f"pp-review-epoch-v1\nhead={OLD}\nanchor-node=A\n".encode()).hexdigest()
        self.add("PullRequestCommit", "A", commit={"oid": OLD})
        self.comment(101, f"Reviewed-SHA: {OLD}\nOutcome-Label: reviewed\n<!-- pp:review pp:tail=0 -->")
        self.comment(102, f"<!-- pp:review-claim {OLD} review-comment=101 epoch-sha256={self.epoch} -->")
        self.comment(
            103, f"<!-- pp:head-reviewed {OLD} review-comment=101 claim=102 epoch-sha256={self.epoch} -->"
        )
        self.add(
            "LabeledEvent",
            "ship",
            label={"name": "ship"},
            actor={"login": ACCOUNT},
            createdAt="2026-11-19T14:00:00Z",
        )
        self.comment(164, INTENT)
        self.add("PullRequestCommit", "B", commit={"oid": HEAD})
        self.labels = ["reviewed", "ship"]
        self.main, self.parents = BASE, [OLD, BASE]
        self.ci_error = False
        self.drop_post = False
        self.past_send = False
        self.gates = []
        self.node_drift = False
        self.old_failed_run = False
        self.gate_head = HEAD
        self.stale_after_gate = False
        self.on_source = lambda: None
        self.source_calls = 0
        self.current_head = HEAD
        self.current_state = "OPEN"

    def add(self, kind, identifier, **extra):
        self.edges.append(
            {"cursor": f"c{len(self.edges)}", "node": {"__typename": kind, "id": identifier, **extra}}
        )

    def comment(self, number, body):
        self.add(
            "IssueComment",
            f"C{number}",
            fullDatabaseId=str(number),
            body=body,
            lastEditedAt=None,
            createdAt="2026-11-19T13:59:00Z",
            author={"login": ACCOUNT},
        )

    def graphql_page(self, owner, name, number, cursor):
        pr = {
            "headRefOid": self.current_head,
            "baseRefOid": BASE,
            "baseRefName": "main",
            "state": self.current_state,
            "isDraft": False,
            "labels": {"nodes": [{"name": v} for v in self.labels], "pageInfo": {"hasNextPage": False}},
            "timelineItems": {
                "edges": copy.deepcopy(self.edges),
                "updatedAt": "2026-11-19T14:00:00Z",
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
        return {"data": {"repository": {"pullRequest": pr}}}

    def json(self, verb, path, *args, input_value=None):
        assert verb == "api"
        self.calls.append(path)
        if path == "graphql":
            data = self.graphql_page("ivanarama", "onebase", N, None)
            by_id = {e["node"]["id"]: e["node"] for e in self.edges}
            data["data"]["nodes"] = [copy.deepcopy(by_id[i]) for i in input_value["variables"]["ids"]]
            if self.node_drift:
                data["data"]["nodes"][0] = None
            return data
        if path == "user":
            return {"login": ACCOUNT}
        root = f"repos/{REPO}"
        if path == root:
            return {"full_name": REPO, "permissions": {"push": True}}
        endpoint = path.removeprefix(root + "/")
        if endpoint == f"pulls/{N}":
            return {
                "number": N,
                "draft": False,
                "state": "open",
                "merged": False,
                "labels": [{"name": v} for v in self.labels],
                "maintainer_can_modify": False,
                "head": {"repo": {"full_name": REPO}, "sha": HEAD, "ref": "feature/queue"},
                "base": {"repo": {"full_name": REPO}, "sha": BASE, "ref": "main"},
            }
        if endpoint == "git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.main}}
        if endpoint == f"git/commits/{HEAD}":
            return {"sha": HEAD, "parents": [{"sha": s} for s in self.parents], "tree": {"sha": TREE}}
        if endpoint == "branches/main/protection":
            return {"required_status_checks": {"strict": True, "contexts": CHECKS}}
        if "/check-runs?" in endpoint:
            sha = endpoint.split("/")[1]
            rows = [
                {
                    "id": i,
                    "name": k,
                    "head_sha": sha,
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "github-actions", "id": 15368},
                }
                for i, k in enumerate(CHECKS, 1)
            ]
            if self.ci_error:
                rows[0]["conclusion"] = "failure"
            if self.old_failed_run:
                old = dict(rows[0], conclusion="failure")
                rows[0]["id"] = 48
                rows.append(old)
            return {"total_count": len(rows), "check_runs": rows}
        if endpoint.startswith("issues/comments/"):
            key = endpoint.split("/")[-1]
            n = next(e["node"] for e in self.edges if e["node"].get("fullDatabaseId") == key)
            return {
                "id": int(key),
                "node_id": n["id"],
                "body": n["body"],
                "user": {"login": ACCOUNT},
                "issue_url": f"https://api.github.com/repos/{REPO}/issues/{N}",
            }
        if endpoint == f"issues/{N}/comments":
            assert args == ("--method", "POST", "--input", "-")
            self.posts.append(input_value)
            self.comment(888, input_value["body"])
            if self.drop_post:
                raise pp.PipelineError("reply lost")
            return {"id": 888, "node_id": "C888", "body": input_value["body"], "user": {"login": ACCOUNT}}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def run(self):
        def gate():
            self.gates.append(True)
            return {
                "action": "validated",
                "stage": "merge",
                "repository": REPO,
                "target": {"number": N, "head": self.gate_head, "stage": "integration-merge-recovery"},
            }

        def claim(number, head, intent, digest):
            assert (number, head, intent, digest) == (
                N,
                HEAD,
                164,
                hashlib.sha256(DONE.encode()).hexdigest(),
            )
            if self.past_send:
                return False
            self.past_send = True
            return True

        def fence():
            if self.stale_after_gate and self.gates:
                raise pp.PipelineError("runtime fence changed")

        def source():
            assert self.calls, "Source must follow expensive preparation"
            self.source_calls += 1
            self.on_source()

        return finish.confirm(
            self,
            CONFIG,
            CHECKPOINT,
            working_dir=str(Path.cwd()),
            prepare_source=source,
            gate=gate,
            validate_attempt=fence,
            claim_send=claim,
        )


@pytest.fixture
def replay(monkeypatch):
    gh = Replay()
    monkeypatch.setattr(finish, "_tree_proof", lambda *args: None)
    monkeypatch.setattr(finish, "_verify_remote", lambda *args: None)
    return gh


def test_confirm_and_recover_existing_done(replay):
    replay.old_failed_run = True  # #2711 had a failed build followed by a successful rerun
    first = replay.run()
    second = replay.run()
    assert (first["done"], second["done"]) == (888, 888)
    assert first["published"] and not second["published"]
    assert first["body_sha256"] == second["body_sha256"] == hashlib.sha256(DONE.encode()).hexdigest()
    assert replay.posts == [{"body": DONE}] and len(replay.gates) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda g: g.labels.append("hold"),
        lambda g: setattr(g, "main", "e" * 40),
        lambda g: setattr(g, "parents", [BASE, OLD]),
        lambda g: setattr(g, "node_drift", True),
        lambda g: setattr(g, "ci_error", True),
        lambda g: g.comment(165, INTENT),
        lambda g: g.add("UnlabeledEvent", "stop", label={"name": "ship"}, actor={"login": ACCOUNT}),
        lambda g: g.add("PullRequestCommit", "duplicate", commit={"oid": OLD}),
        lambda g: g.edges[1]["node"].pop("lastEditedAt"),
        lambda g: g.comment(1010, DONE.replace("previous=none", "previous=404")),
    ],
)
def test_drift_refuses_before_gate_or_write(replay, change):
    change(replay)
    with pytest.raises(pp.PipelineError):
        replay.run()
    assert not replay.gates and not replay.posts and not replay.past_send


def test_lost_response_never_sends_again(replay):
    replay.drop_post = True
    with pytest.raises(pp.PipelineError):
        replay.run()
    assert replay.run()["published"] is False
    assert len(replay.posts) == 1
    replay.edges.pop()  # receipt has disappeared from GitHub too
    with pytest.raises(pp.PipelineError):
        replay.run()
    assert len(replay.posts) == 1


def test_source_rechecked_after_long_preparation(replay):
    replay.on_source = lambda: replay.comment(920, "A new observation appeared.")
    with pytest.raises(pp.PipelineError, match="changed across final Source"):
        replay.run()
    assert replay.source_calls == 1
    assert not replay.gates and not replay.past_send and not replay.posts


def test_ordinary_recovery_handoff_only_before_final_source(replay):
    replay.main = "e" * 40
    with pytest.raises(finish.OutsideScope):
        replay.run()
    assert replay.source_calls == 0
    replay.main = BASE
    replay.on_source = lambda: setattr(replay, "main", "e" * 40)
    with pytest.raises(pp.PipelineError, match="scope changed after final Source") as caught:
        replay.run()
    assert not isinstance(caught.value, finish.OutsideScope)
    assert replay.source_calls == 1 and not replay.gates and not replay.posts


@pytest.mark.parametrize("field,value", [("gate_head", "0" * 40), ("stale_after_gate", True)])
def test_final_gate_binding_is_checked_before_send(replay, field, value):
    setattr(replay, field, value)
    with pytest.raises(pp.PipelineError):
        replay.run()
    assert len(replay.gates) == 1 and not replay.posts and not replay.past_send


def test_real_git_reproduces_the_remote_tree_without_moving_any_ref(tmp_path, monkeypatch):
    from test_base_sync_carry import commit, git

    source, checker = tmp_path / "source", tmp_path / "checker"
    git(tmp_path, "init", str(source))
    commit(source, "common", common="shared")
    git(source, "checkout", "-b", "feature/queue")
    old = commit(source, "feature", feature="change")
    git(source, "checkout", "main")
    base = commit(source, "base advances", base="new")
    git(tmp_path, "clone", str(source), str(checker))
    git(source, "checkout", "feature/queue")
    git(source, "merge", "--no-ff", "-m", "update", base)
    head = git(source, "rev-parse", "HEAD")
    tree = git(source, "rev-parse", "HEAD^{tree}")
    checkpoint = dict(CHECKPOINT, **{"from": old, "base": base, "to": head, "tree": tree})
    monkeypatch.setattr(finish, "REMOTE", str(source))
    refs = git(checker, "show-ref")
    finish._verify_remote(CONFIG, checker, "feature/queue", head)
    finish._tree_proof(CONFIG, checker, "feature/queue", checkpoint)
    assert git(checker, "show-ref") == refs
    assert not (checker / ".git" / "FETCH_HEAD").exists()
    with pytest.raises(pp.PipelineError):
        finish._tree_proof(CONFIG, checker, "feature/queue", dict(checkpoint, tree="0" * 40))


@pytest.mark.parametrize("merged", [False, True])
def test_historical_receipt_survives_review_and_later_head(replay, merged):
    replay.drop_post = True
    with pytest.raises(pp.PipelineError):
        replay.run()
    replay.current_head = "e" * 40
    replay.labels = ["reviewed"]
    replay.comment(889, "Documentation quotes the marker: " + DONE)
    replay.comment(890, DONE)
    replay.edges[-1]["node"]["author"]["login"] = "untrusted"
    replay.add("UnlabeledEvent", "unship", label={"name": "ship"}, actor={"login": ACCOUNT})
    replay.add("PullRequestCommit", "new", commit={"oid": replay.current_head})
    if merged:
        replay.current_state = "MERGED"
        replay.add("MergedEvent", "merged", commit={"oid": "f" * 40})
    record = dict(CHECKPOINT, body_sha256=hashlib.sha256(DONE.encode()).hexdigest())
    assert finish.observe_receipt(replay, record, config=CONFIG) == 888
    assert len(replay.posts) == 1 and len(replay.gates) == 1


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda g: g.edges.pop(),
        lambda g: g.comment(889, DONE),
        lambda g: g.edges[-1]["node"].update(author={"login": "elsewhere"}),
        lambda g: g.edges[-1]["node"].update(lastEditedAt="2026-11-20T00:00:00Z"),
        lambda g: g.edges[-1]["node"].update(body=DONE.replace("intent=164", "intent=165")),
        lambda g: g.edges.reverse(),
    ],
)
def test_incomplete_historical_receipt_keeps_one_send(replay, corrupt):
    replay.comment(888, DONE)
    corrupt(replay)
    record = dict(CHECKPOINT, body_sha256=hashlib.sha256(DONE.encode()).hexdigest())
    assert finish.observe_receipt(replay, record, config=CONFIG) is None
    assert not replay.posts and not replay.gates
