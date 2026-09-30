"""Issue #42: carry a trusted ship through one proven mechanical base-sync.

Everything runs through the public ``pipelinectl`` entry point
(``project_pipeline.run``) against a real git "origin" (the PR head lives at
``refs/pull/7/head``, exactly as GitHub exposes it) and a GitHub double that
serves the server-ordered GraphQL timeline, the REST state and the mutations.
The project health checker is replaced by the snapshot onebase#1776 agreed:
the updated owner is ``legacy-integration-review`` with a descriptive
``base_sync_candidate``.

The positive path: BEHIND owner → update-branch → the updated owner is merged
without a new REVIEW, the ship carried. Every negative case changes one fact
the owner's four conditions depend on, and must end with no carry, no merge
and nothing published.
"""

import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from promptpilot import base_sync_carry, project_pipeline as pp

NUMBER = 7
REPO = "owner/repo"
TRUSTED = "pp-bot"
CANDIDATE_CHECKS = ["base_ancestry", "merge_tree", "required_checks", "timeline_epoch"]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-09-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-09-01T00:00:00Z",
}


def git(cwd, *args) -> str:
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if result.returncode:
        raise AssertionError(f"git {args}: {result.stderr or result.stdout}")
    return result.stdout.strip()


def commit(repo: Path, message: str, **files) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class World:
    """A repository where main moved on after the PR was reviewed.

    Built once per module; every test works on its own copy (``clone_to``).
    """

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", str(self.origin))
        seed = self.seed = root / "seed"
        git(root, "clone", "-q", str(self.origin), str(seed))
        self.m0 = commit(seed, "M0", **{"a.txt": "a0\n", "b.txt": "b0\n"})
        git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(seed, "checkout", "-q", "-b", "feature")
        self.a = commit(seed, "A: the reviewed change", **{"a.txt": "a1\n"})   # from
        git(seed, "checkout", "-q", "main")
        self.m1 = commit(seed, "M1: main moves on", **{"b.txt": "b1\n"})       # base
        git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(seed, "checkout", "-q", "feature")
        git(seed, "merge", "-q", "--no-ff", "-m", "Merge branch 'main' into feature", "main")
        self.to = git(seed, "rev-parse", "HEAD")                               # to
        self.main_tip = self.m1
        self.publish(self.to, "refs/staging/to")
        self.set_pr_head(self.a)
        self.work = root / "work"  # the automation checkout pipelinectl runs in
        git(root, "clone", "-q", str(self.origin), str(self.work))
        self.variants = {"evil": self._evil_merge(), "conflict": self._conflicting_sync(),
                         "rewritten": self._rewritten_main()}

    def clone_to(self, root: Path) -> "World":
        shutil.copytree(self.root, root, dirs_exist_ok=True)
        copy_ = object.__new__(World)
        copy_.__dict__.update(self.__dict__)
        copy_.root, copy_.origin = root, root / "origin.git"
        copy_.seed, copy_.work = root / "seed", root / "work"
        for repo in (copy_.seed, copy_.work):
            git(repo, "remote", "set-url", "origin", str(copy_.origin))
        return copy_

    def evil_merge(self) -> str:
        return self.variants["evil"]

    def conflicting_sync(self) -> tuple[str, str]:
        return self.variants["conflict"]

    def rewritten_main(self) -> str:
        return self.variants["rewritten"]

    def publish(self, sha: str, ref: str):
        git(self.seed, "push", "-q", "-f", "origin", f"{sha}:{ref}")

    def set_pr_head(self, sha: str):
        git(self.origin, "update-ref", f"refs/pull/{NUMBER}/head", sha)

    def set_main(self, sha: str):
        git(self.origin, "update-ref", "refs/heads/main", sha)
        self.main_tip = sha

    def _evil_merge(self) -> str:
        """The shape of update-branch, plus an extra change hidden in the merge."""
        git(self.seed, "checkout", "-q", "-B", "evil", self.a)
        git(self.seed, "merge", "-q", "--no-ff", "--no-commit", self.m1)
        (self.seed / "c.txt").write_text("smuggled\n", encoding="utf-8", newline="\n")
        git(self.seed, "add", "c.txt")
        git(self.seed, "commit", "-q", "-m", "Merge branch 'main' into feature")
        sha = git(self.seed, "rev-parse", "HEAD")
        self.publish(sha, "refs/staging/evil")
        return sha

    def _conflicting_sync(self) -> tuple[str, str]:
        """main changed the same line; someone resolved the conflict by hand."""
        git(self.seed, "checkout", "-q", "-B", "mainc", self.m0)
        base = commit(self.seed, "MC: conflicting change", **{"a.txt": "ac\n"})
        self.publish(base, "refs/staging/mainc")
        git(self.seed, "checkout", "-q", "-B", "conf", self.a)
        subprocess.run(["git", "-c", "core.autocrlf=false", "merge", "-q", "--no-ff", base],
                       cwd=self.seed, capture_output=True, env={**os.environ, **GIT_ENV})
        (self.seed / "a.txt").write_text("resolved\n", encoding="utf-8", newline="\n")
        git(self.seed, "add", "a.txt")
        git(self.seed, "commit", "-q", "-m", "Merge branch 'main' into feature")
        sha = git(self.seed, "rev-parse", "HEAD")
        self.publish(sha, "refs/staging/conf")
        return sha, base

    def _rewritten_main(self) -> str:
        """main force-moved: the base the PR was synced with is gone from it."""
        git(self.seed, "checkout", "-q", "-B", "rewritten", self.m0)
        sha = commit(self.seed, "M2", **{"b.txt": "b2\n"})
        self.publish(sha, "refs/staging/rewritten")
        return sha

    def parents(self, sha: str) -> list[str]:
        return git(self.origin, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def epoch_of(head: str, anchor: str) -> str:
    return hashlib.sha256(
        f"pp-review-epoch-v1\nhead={head}\nanchor-node={anchor}\n".encode("ascii")).hexdigest()


class FakeGitHub:
    """One PR as gh shows it: GraphQL timeline, REST state and the mutations."""

    def __init__(self, world: World):
        self.world = world
        self.timeout_seconds = 120
        self.head = world.a
        self.labels = {"ship"}
        self.base_ref = "main"
        self.merge_state, self.mergeable = "BEHIND", "MERGEABLE"
        self.checks = [{"name": "build", "conclusion": "SUCCESS"},
                       {"name": "test", "conclusion": "SUCCESS"}]
        self.body = ""
        self.merged, self.merge_sha = False, None
        self.update_conflict = False
        self.merge_error = None
        self.remove_error = None
        self.comments = []   # REST view of issue comments
        self.edges = []      # GraphQL timeline
        self.mutations = []
        self.next_id = 500
        self._clock = 0
        hash_a = epoch_of(world.a, "PRC_A")
        self.edge("PullRequestCommit", id="PRC_A", commit={"oid": world.a})
        self.comment(101, f"**Ревью.**\nReviewed-SHA: {world.a}\nOutcome-Label: reviewed\n"
                          "Вердикт: годится к мержу.\n<!-- pp:review pp:tail=0 -->")
        self.comment(102, f"<!-- pp:review-claim {world.a} review-comment=101 epoch-sha256={hash_a} -->")
        self.edge("LabeledEvent", id="LBL_reviewed", actor={"login": TRUSTED},
                  label={"name": "reviewed"}, createdAt=self.now())
        self.comment(103, f"<!-- pp:head-reviewed {world.a} review-comment=101 claim=102 "
                          f"epoch-sha256={hash_a} -->")
        self.edge("LabeledEvent", id="SHIP_1", actor={"login": TRUSTED},
                  label={"name": "ship"}, createdAt=self.now())
        self.candidate = {
            "from": world.a, "base": world.m1, "to": world.to, "source": "head_parents",
            "from_review": {"state": "consistent", "sha": world.a, "review_comment": 101,
                            "claim": 102, "epoch_sha256": hash_a, "outcome_label": "reviewed"},
            "current_head_reviewed": False, "consumer_must_verify": list(CANDIDATE_CHECKS),
        }

    # --- timeline construction -----------------------------------------------------------
    def now(self) -> str:
        self._clock += 1
        return f"2026-09-01T00:{self._clock // 60:02d}:{self._clock % 60:02d}Z"

    def edge(self, kind: str, **node):
        self.edges.append({"cursor": f"c{len(self.edges) + 1}",
                           "node": {"__typename": kind, **node}})

    def comment(self, comment_id: int, body: str) -> dict:
        created = self.now()
        rest = {"id": comment_id, "body": body, "user": {"login": TRUSTED},
                "created_at": created, "updated_at": created,
                "issue_url": f"https://api.github.com/repos/{REPO}/issues/{NUMBER}"}
        self.comments.append(rest)
        self.edge("IssueComment", id=f"IC_{comment_id}", fullDatabaseId=str(comment_id),
                  createdAt=created, lastEditedAt=None, author={"login": TRUSTED}, body=body)
        return rest

    def synced(self):
        """The state right after update-branch (what the positive path produces)."""
        self.world.set_pr_head(self.world.to)
        self.head = self.world.to
        self.edge("PullRequestCommit", id="PRC_TO", commit={"oid": self.world.to})
        self.merge_state = "CLEAN"
        return self

    def find(self, node_id: str) -> dict:
        return next(edge["node"] for edge in self.edges if edge["node"].get("id") == node_id)

    # --- gh ---------------------------------------------------------------------------
    def published(self):
        return [call for call in self.mutations if not call[1].endswith("/update-branch")]

    def run(self, *args, input_value=None, allow=(0,), timeout_seconds=None):
        if args[:2] == ("api", "--paginate"):
            route = args[2]
            if route.startswith(f"repos/{REPO}/issues/comments"):
                return "\n".join(json.dumps(item) for item in self.comments)
            if route.startswith(f"repos/{REPO}/issues/{NUMBER}/comments"):
                return "\n".join(json.dumps(item) for item in self.comments)
        if args[:3] == ("api", "--method", "DELETE"):
            route = args[3]
            self.mutations.append(("DELETE", route, None))
            if self.remove_error:
                error, self.remove_error = self.remove_error, None
                raise pp.PipelineError(error)
            label = route.rsplit("/", 1)[1]
            if label in self.labels:
                self.labels.discard(label)
                self.edge("UnlabeledEvent", id=f"UNL_{len(self.edges)}", actor={"login": TRUSTED},
                          label={"name": label}, createdAt=self.now())
            return ""
        raise AssertionError(f"unexpected gh run: {args}")

    def json(self, *args, input_value=None):
        if args == ("api", "user"):
            return {"login": TRUSTED}
        if args[:2] == ("pr", "view"):
            return {"headRefOid": self.head, "mergeStateStatus": self.merge_state,
                    "mergeable": self.mergeable, "statusCheckRollup": copy.deepcopy(self.checks),
                    "body": self.body}
        route = args[1]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        if method != "GET":
            self.mutations.append((method, route, copy.deepcopy(input_value)))
        if route == f"repos/{REPO}/pulls/{NUMBER}" and method == "GET":
            return {"state": "closed" if self.merged else "open", "merged": self.merged,
                    "head": {"sha": self.head}, "base": {"ref": self.base_ref},
                    "merge_commit_sha": self.merge_sha, "body": self.body}
        if route == f"repos/{REPO}/pulls/{NUMBER}/update-branch":
            if self.update_conflict:
                raise pp.PipelineError("Merge conflict (HTTP 422)")
            assert input_value == {"expected_head_sha": self.head, "update_method": "merge"}
            self.synced()
            return {"message": "Updating pull request branch."}
        if route == f"repos/{REPO}/pulls/{NUMBER}/merge":
            assert input_value == {"merge_method": "merge", "sha": self.head}
            if self.merge_error:
                error, self.merge_error = self.merge_error, None
                raise pp.PipelineError(error)
            self.merged, self.merge_sha = True, "f" * 40
            self.edge("MergedEvent", id="MERGED", createdAt=self.now(), commit={"oid": self.merge_sha})
            return {"merged": True}
        if route == f"repos/{REPO}/issues/{NUMBER}/comments" and method == "POST":
            self.next_id += 1
            return self.comment(self.next_id, input_value["body"])
        if route == f"repos/{REPO}/issues/{NUMBER}" and method == "GET":
            return {"state": "closed" if self.merged else "open",
                    "labels": [{"name": label} for label in sorted(self.labels)]}
        if route.startswith(f"repos/{REPO}/commits/"):
            sha = route.rsplit("/", 1)[1]
            return {"sha": sha, "parents": [{"sha": value} for value in self.world.parents(sha)]}
        raise AssertionError(f"unexpected gh call: {args}")

    def graphql_page(self, owner, name, number, cursor):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": self.head, "baseRefOid": self.world.main_tip, "baseRefName": self.base_ref,
            "state": "MERGED" if self.merged else "OPEN", "isDraft": False,
            "labels": {"nodes": [{"name": label} for label in sorted(self.labels)],
                       "pageInfo": {"hasNextPage": False}},
            "timelineItems": {"updatedAt": "2026-09-01T01:00:00Z",
                              "edges": copy.deepcopy(self.edges),
                              "pageInfo": {"hasNextPage": False, "endCursor": None}},
        }}}}


def health_of(github: FakeGitHub):
    """The project health snapshot, as agreed in onebase#1776."""
    def run_health(config, *, config_path=None, working_dir=None):
        barrier = {"code": "single_flight_barrier", "severity": "yellow", "pr": NUMBER}
        if github.merged:
            return {"state": "green", "findings": [], "review_candidates": [],
                    "content_review_candidates": [], "merge_executable": []}
        if github.head == github.world.a:
            owner = {"number": NUMBER, "head": github.head, "stage": "integration-merge-ready"}
            return {"state": "yellow", "integration_owner": owner, "review_candidates": [],
                    "content_review_candidates": [], "merge_executable": [owner],
                    "findings": [barrier]}
        owner = {"number": NUMBER, "head": github.head, "stage": "legacy-integration-review"}
        if github.candidate is not None:
            owner["base_sync_candidate"] = copy.deepcopy(github.candidate)
        return {"state": "yellow", "integration_owner": owner, "review_candidates": [owner],
                "content_review_candidates": [], "merge_executable": [],
                "findings": [barrier]}
    return run_health


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return World(tmp_path_factory.mktemp("carry-world"))


@pytest.fixture
def world(template, tmp_path):
    return template.clone_to(tmp_path / "world")


@pytest.fixture
def pipelinectl(tmp_path, monkeypatch, capsys, world):
    def write_config(**extra):
        path = tmp_path / "pipelinectl.json"
        config = {"repository": REPO, "trusted_account": TRUSTED,
                  "health_command": ["project-health", "-json"],
                  "base_sync_merge": True, "base_sync_carry": True,
                  "required_checks": ["build", "test"]}
        config.update(extra)
        config = {key: value for key, value in config.items() if value is not None}
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    state = {"config": write_config()}
    monkeypatch.chdir(world.work)
    monkeypatch.setenv("PP_PIPELINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PP_PIPELINE_REPLICAS", raising=False)
    monkeypatch.delenv("PP_PIPELINE_EXCLUDED_NUMBERS", raising=False)

    def invoke(github, *command):
        monkeypatch.setattr(pp, "GitHub", lambda: github)
        monkeypatch.setattr(pp, "run_health", health_of(github))
        code = pp.run(["--config", str(state["config"]), *command])
        captured = capsys.readouterr()
        return code, json.loads(captured.out), captured.err

    invoke.configure = lambda **extra: state.update(config=write_config(**extra))
    return invoke


def lease_file(tmp_path, result) -> str:
    path = tmp_path / f"lease-{abs(hash(result['lease']))}.txt"
    path.write_text(result["lease"], encoding="ascii")
    return str(path.resolve())


def merge_calls(github):
    return [call for call in github.mutations if call[1].endswith("/merge")]


# --- the whole path ----------------------------------------------------------------------------

def test_behind_owner_is_updated_and_merged_without_a_new_review(pipelinectl, world, tmp_path):
    github = FakeGitHub(world)

    code, update, _ = pipelinectl(github, "next", "merge")
    assert code == 0 and pp.decode_lease(update["lease"])["mode"] == "update-branch"
    code, updated, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                   lease_file(tmp_path, update))
    assert code == 0 and updated["new_head"] == world.to

    code, election, _ = pipelinectl(github, "next", "merge")
    assert code == 0, election
    assert election["action"] == "merge" and election["carry"] == "proven"
    lease = pp.decode_lease(election["lease"])
    assert lease["mode"] == "carry" and lease["head"] == world.to
    evidence = lease["carry"]
    assert (evidence["from"], evidence["base"], evidence["to"]) == (world.a, world.m1, world.to)
    assert evidence["ship_event"] == "SHIP_1" and evidence["from_proof"]["review_id"] == 101
    assert github.published() == []  # deciding publishes nothing

    code, merged, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, election))
    assert code == 0, merged
    assert merged["action"] == "completed" and merged["head"] == world.to
    assert merge_calls(github) == [("PUT", f"repos/{REPO}/pulls/{NUMBER}/merge",
                                    {"merge_method": "merge", "sha": world.to})]
    posted = [call[2]["body"] for call in github.mutations if call[0] == "POST"]
    intent = pp.MERGE_CLEANUP_INTENT.fullmatch(posted[0].strip())
    assert intent and intent.group(1) == world.to
    assert intent.group(2) == pp.digest(evidence)  # the intent binds this exact carry
    assert posted[1].strip().endswith(f"head={world.to} merge={'f' * 40} -->")
    assert len(posted) == 2
    assert not any("pp:review" in body or "pp:base-sync" in body for body in posted)
    assert "ship" not in github.labels


def test_update_conflict_publishes_nothing_and_carries_nothing(pipelinectl, world, tmp_path):
    github = FakeGitHub(world)
    github.update_conflict = True
    _code, update, _ = pipelinectl(github, "next", "merge")

    code, failed, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, update))
    code_after, after, _ = pipelinectl(github, "next", "merge")

    assert code == 2 and "422" in failed["error"]
    assert github.head == world.a and github.labels == {"ship"}
    assert github.published() == []  # no done, no intent, no merge after a 422
    assert code_after == 0 and after["action"] == "merge"
    assert pp.decode_lease(after["lease"])["mode"] == "update-branch"  # still only an update


def test_merge_dated_before_the_review_is_carried_by_the_graph(pipelinectl, world, tmp_path):
    """A merge made locally before the review and pushed after the ship.

    GitHub lists its commit before the review, the claim and the ship
    (onebase#1561). The graph still proves the one transition, with the same
    evidence as for a commit listed last.
    """
    def evidence(github):
        facts = base_sync_carry.timeline_facts({"edges": github.edges}, TRUSTED,
                                               world.a, world.to, world.m1)
        return base_sync_carry.evidence_of(world.a, world.m1, world.to, facts)

    github = FakeGitHub(world).synced()
    listed_last = evidence(github)
    move_edge(github, "PRC_TO", 1)
    assert evidence(github) == listed_last

    code, election, _ = pipelinectl(github, "next", "merge")
    assert code == 0 and election["carry"] == "proven", election
    assert pp.decode_lease(election["lease"])["carry"] == listed_last
    code, merged, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, election))

    assert code == 0 and merged["action"] == "completed", merged
    assert merge_calls(github) == [("PUT", f"repos/{REPO}/pulls/{NUMBER}/merge",
                                    {"merge_method": "merge", "sha": world.to})]
    assert "ship" not in github.labels


# --- every condition, broken one at a time ----------------------------------------------------

def edited_review(github, world):
    github.find("IC_101")["lastEditedAt"] = "2026-09-02T00:00:00Z"


def review_changes_requested(github, world):
    node = github.find("IC_101")
    node["body"] = node["body"].replace("Outcome-Label: reviewed", "Outcome-Label: changes-requested")


def completion_missing(github, world):
    github.edges = [edge for edge in github.edges if edge["node"].get("id") != "IC_103"]


def ship_removed(github, world):
    github.labels.discard("ship")
    github.edge("UnlabeledEvent", id="UNSHIP", actor={"login": TRUSTED},
                label={"name": "ship"}, createdAt=github.now())


def ship_by_stranger(github, world):
    github.edge("LabeledEvent", id="SHIP_2", actor={"login": "someone"},
                label={"name": "ship"}, createdAt=github.now())


def ship_before_review(github, world):
    ship = next(edge for edge in github.edges if edge["node"].get("id") == "SHIP_1")
    github.edges.remove(ship)
    github.edges.insert(1, ship)


def hold_label(github, world):
    github.labels.add("hold")


def review_again_override(github, world):
    github.comment(700, "pp:review-again")


def force_push_after_sync(github, world):
    github.edge("HeadRefForcePushedEvent", id="FP", createdAt=github.now(),
                afterCommit={"oid": world.to})


def base_branch_changed(github, world):
    github.edge("BaseRefChangedEvent", id="BRC", createdAt=github.now(),
                previousRefName="release", currentRefName="main")


def main_rewritten(github, world):
    world.set_main(world.rewritten_main())


def candidate_names_another_base(github, world):
    github.candidate["base"] = world.m0


def evil_merge(github, world):
    sha = world.evil_merge()
    world.set_pr_head(sha)
    github.head = sha
    next(edge for edge in github.edges if edge["node"].get("id") == "PRC_TO")["node"]["commit"]["oid"] = sha
    github.candidate["to"] = sha


def conflict_resolved_by_hand(github, world):
    sha, base = world.conflicting_sync()
    world.set_pr_head(sha)
    world.set_main(base)
    github.head = sha
    next(edge for edge in github.edges if edge["node"].get("id") == "PRC_TO")["node"]["commit"]["oid"] = sha
    github.candidate.update(to=sha, base=base)


def ci_red(github, world):
    github.checks[1] = {"name": "test", "conclusion": "FAILURE"}


def ci_incomplete(github, world):
    github.checks = github.checks[:1]


def main_moved_again(github, world):
    github.merge_state = "BEHIND"


def current_head_already_reviewed(github, world):
    github.candidate["current_head_reviewed"] = True


def snapshot_disagrees_with_timeline(github, world):
    github.candidate["from_review"]["claim"] = 999


def snapshot_pair_not_consistent(github, world):
    github.candidate["from_review"] = {"state": "claim_missing", "sha": world.a}


def no_candidate(github, world):
    github.candidate = None


def unknown_consumer_check(github, world):
    github.candidate["consumer_must_verify"].append("signed_tree")


def contradicting_done_marker(github, world):
    github.comment(701, f"<!-- pp:base-sync-done intent=1 from={world.a} to={world.to} "
                        f"base={world.m0} previous=none ship-event=SHIP_1 -->")


def move_edge(github, node_id: str, index: int):
    """GitHub places a PullRequestCommit by the commit date, not the push."""
    edge = next(edge for edge in github.edges if edge["node"].get("id") == node_id)
    github.edges.remove(edge)
    github.edges.insert(index if index >= 0 else len(github.edges) + index + 1, edge)


def sync_commit_listed_twice(github, world):
    github.edge("PullRequestCommit", id="PRC_TO_AGAIN", commit={"oid": world.to})


def extra_commit_dated_before_the_sync(github, world):
    github.edge("PullRequestCommit", id="PRC_X", commit={"oid": "e" * 40})
    move_edge(github, "PRC_X", 1)


def head_branch_restored(github, world):
    github.edge("HeadRefDeletedEvent", id="HRD", createdAt=github.now())
    github.edge("HeadRefRestoredEvent", id="HRR", createdAt=github.now())


def sync_commit_reviewed_before_its_edge(github, world):
    github.comment(703, f"**Ревью.**\nReviewed-SHA: {world.to}\nOutcome-Label: reviewed\n"
                        "<!-- pp:review pp:tail=0 -->")
    move_edge(github, "PRC_TO", -1)


@pytest.mark.parametrize(("breaks", "reason"), [
    (edited_review, "trusted comment was edited"),
    (review_changes_requested, "is changes-requested"),
    (completion_missing, "no canonical committed review proof"),
    (ship_removed, "ship label is absent"),
    (ship_by_stranger, "not a trusted ship"),
    (ship_before_review, "set before the review"),
    (hold_label, "routing label hold"),
    (review_again_override, "pp:review-again restarted the review epoch"),
    (force_push_after_sync, "HeadRefForcePushedEvent"),
    (head_branch_restored, "restored after the reviewed version"),
    (sync_commit_listed_twice, "listed in the timeline more than once"),
    (extra_commit_dated_before_the_sync, "unsupported epoch event: PullRequestCommit"),
    (sync_commit_reviewed_before_its_edge, "has its own review transaction"),
    (base_branch_changed, "BaseRefChangedEvent"),
    (main_rewritten, "not an ancestor of the current base branch"),
    (candidate_names_another_base, "not exactly the two-parent merge"),
    (evil_merge, "differs from the reproduced merge"),
    (conflict_resolved_by_hand, "has conflicts"),
    (ci_red, "checks not green: test=FAILURE"),
    (ci_incomplete, "required checks missing: test"),
    (main_moved_again, "only one mechanical hop"),
    (current_head_already_reviewed, "has its own review"),
    (snapshot_disagrees_with_timeline, "disagree"),
    (snapshot_pair_not_consistent, "claim_missing"),
    (no_candidate, "no base_sync_candidate"),
    (unknown_consumer_check, "unknown consumer checks"),
    (contradicting_done_marker, "contradicts the commit graph"),
])
def test_unproven_condition_means_no_carry_and_nothing_published(pipelinectl, world, breaks, reason):
    github = FakeGitHub(world).synced()
    breaks(github, world)

    code, merge, _ = pipelinectl(github, "next", "merge")
    code_review, review, _ = pipelinectl(github, "next", "review")

    assert code == 0 and merge["action"] == "wait", merge
    assert "waiting for integration REVIEW" in merge["reason"]
    assert merge["carry"].startswith("refused: ") and reason in merge["carry"], merge["carry"]
    assert code_review == 0 and review["action"] == "fallback"  # the ordinary REVIEW route
    assert "carry refused" in review["reason"] and reason in review["reason"]
    assert github.mutations == []


def test_running_ci_waits_and_review_does_not_start(pipelinectl, world):
    github = FakeGitHub(world).synced()
    github.checks[1] = {"name": "test", "status": "IN_PROGRESS"}

    _code, merge, _ = pipelinectl(github, "next", "merge")
    _code, review, _ = pipelinectl(github, "next", "review")

    assert merge == {"action": "wait", "number": NUMBER,
                     "reason": "base-sync carry pending: required CI checks are still running on HEAD"}
    assert review["action"] == "wait" and review["verdict"] == "ПУСТО"
    assert github.mutations == []


def test_review_leaves_a_proven_carry_to_merge(pipelinectl, world):
    github = FakeGitHub(world).synced()

    code, review, _ = pipelinectl(github, "next", "review")

    assert code == 0 and review["action"] == "wait" and review["verdict"] == "ПУСТО"
    assert "without a new REVIEW (proven" in review["reason"]


def test_without_the_opt_in_the_old_route_stays(pipelinectl, world, monkeypatch):
    pipelinectl.configure(base_sync_carry=None)
    monkeypatch.setattr(base_sync_carry, "_git", lambda *a, **k: pytest.fail("no git without the opt-in"))
    github = FakeGitHub(world).synced()

    _code, merge, _ = pipelinectl(github, "next", "merge")
    _code, review, _ = pipelinectl(github, "next", "review")

    assert merge == {"action": "wait", "number": NUMBER,
                     "reason": "single-flight owner is waiting for integration REVIEW"}
    assert review == {"action": "fallback",
                      "reason": "integration/base-sync state requires the full skill"}


# --- re-proof under the lease -------------------------------------------------------------------

def test_ship_lost_after_the_decision_stops_the_merge(pipelinectl, world, tmp_path):
    github = FakeGitHub(world).synced()
    _code, election, _ = pipelinectl(github, "next", "merge")
    ship_removed(github, world)

    code, failed, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, election))

    assert code == 2 and "ship label is absent" in failed["error"]
    assert github.mutations == []


def test_timeline_changed_after_the_decision_makes_the_lease_stale(pipelinectl, world, tmp_path):
    github = FakeGitHub(world).synced()
    _code, election, _ = pipelinectl(github, "next", "merge")
    github.comment(702, "обычный комментарий")

    code, failed, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, election))

    assert code == 2 and "stale" in failed["error"]
    assert github.mutations == []


def test_ci_turning_red_after_the_intent_stops_the_merge(pipelinectl, world, tmp_path, monkeypatch):
    github = FakeGitHub(world).synced()
    _code, election, _ = pipelinectl(github, "next", "merge")
    real_reserve = pp.reserve_merge_intent

    def reserve_then_break(*args, **kwargs):
        result = real_reserve(*args, **kwargs)
        ci_red(github, world)
        return result

    monkeypatch.setattr(pp, "reserve_merge_intent", reserve_then_break)

    code, failed, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, election))

    assert code == 2 and "test=FAILURE" in failed["error"]
    assert merge_calls(github) == []


# --- crashes ------------------------------------------------------------------------------------------

def test_crash_between_intent_and_merge_resumes_the_same_carry(pipelinectl, world, tmp_path):
    github = FakeGitHub(world).synced()
    _code, election, _ = pipelinectl(github, "next", "merge")
    github.merge_error = "GitHub is unavailable"
    code, _failed, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                   lease_file(tmp_path, election))
    assert code == 2 and not github.merged

    code, resumed, _ = pipelinectl(github, "next", "merge")
    assert code == 0 and resumed["action"] == "merge" and resumed["carry"] == "proven"
    assert pp.decode_lease(resumed["lease"])["intent"]["head"] == world.to
    code, merged, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                  lease_file(tmp_path, resumed))

    assert code == 0 and merged["action"] == "completed"
    intents = [call for call in github.mutations
               if call[0] == "POST" and "merge-cleanup-intent" in call[2]["body"]]
    assert len(intents) == 1  # the same transaction, not a second intent


def test_crash_after_merge_finishes_cleanup_even_with_the_opt_in_off(pipelinectl, world, tmp_path):
    github = FakeGitHub(world).synced()
    _code, election, _ = pipelinectl(github, "next", "merge")
    github.remove_error = "network error"
    code, _failed, _ = pipelinectl(github, "complete", "merge", "--lease-file",
                                   lease_file(tmp_path, election))
    assert code == 2 and github.merged and "ship" in github.labels

    pipelinectl.configure(base_sync_carry=None)
    code, cleanup, _ = pipelinectl(github, "next", "merge")
    assert code == 0 and cleanup["action"] == "cleanup"
    code, done, _ = pipelinectl(github, "complete", "merge-cleanup", "--lease-file",
                                lease_file(tmp_path, cleanup))

    assert code == 0 and done["action"] == "completed" and done["head"] == world.to
    assert "ship" not in github.labels


# --- configuration ---------------------------------------------------------------------------------

def test_carry_requires_an_explicit_required_check_list(tmp_path):
    path = tmp_path / "pipelinectl.json"
    path.write_text(json.dumps({"repository": REPO, "trusted_account": TRUSTED,
                                "health_command": ["h"], "base_sync_carry": True}),
                    encoding="utf-8")

    with pytest.raises(pp.PipelineError, match="required_checks"):
        pp.load_config(str(path))


def test_capabilities_name_the_carry_experimental(pipelinectl, world):
    code, value, _ = pipelinectl(FakeGitHub(world), "capabilities")

    assert code == 0 and value["base_sync_carry"] == "experimental"
