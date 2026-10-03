"""Issue #42: the public pipelinectl path of the experimental base_sync_merge.

Agents drive the base-sync owner only through the CLI: ``next merge`` →
``complete merge --lease-file`` → ``next merge`` / ``next review``. These tests
run that exact sequence through ``project_pipeline.run`` (argument parsing,
config file, lease file, JSON output, exit codes) with GitHub and the
project's health checker replaced at the process boundary.

They pin what the opt-in does today, so that finishing it later is a
deliberate change:

* BEHIND → update-branch touches only the branch: ``ship`` is kept (carry is
  not dropped) and no marker — in particular no ``pp:base-sync-done`` — is
  published;
* a 422 conflict fails the command with nothing published;
* after the update the owner is a two-parent HEAD without a done marker, which
  health routes to ``legacy-integration-review``: MERGE waits and REVIEW falls
  back to the full skill. BEHIND → update → autonomous REVIEW → merge is NOT
  complete — see BASE_SYNC_EXPERIMENTAL_NOTE.
"""

import json

import pytest

from promptpilot import project_pipeline as pp

NUMBER = 1232
HEAD = "3e7be634fba4504b0a768662122d3cc12d5572d9"
UPDATED_HEAD = "b" * 40  # the two-parent commit GitHub creates on update-branch
BARRIER = {"code": "single_flight_barrier", "severity": "yellow", "pr": NUMBER}


class FakeGitHub:
    """GitHub as the gh CLI shows it, for one PR: the single-flight owner."""

    def __init__(self, *, conflict=False):
        self.head = HEAD
        self.labels = ["ship"]
        self.merge_state = "BEHIND"
        self.conflict = conflict
        self.mutations = []
        self.timeout_seconds = 120

    # gh api --paginate ... --jq '.[]'
    def run(self, *args, input_value=None, allow=(0,), timeout_seconds=None):
        route = next(arg for arg in args if arg.startswith("repos/"))
        if "/issues/comments" in route:
            return ""  # no protocol markers anywhere in the repository
        raise AssertionError(f"unexpected gh call: {args}")

    def json(self, *args, input_value=None):
        if args[:2] == ("pr", "view"):
            return {"mergeStateStatus": self.merge_state, "mergeable": "MERGEABLE",
                    "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
                    "body": ""}
        route = args[1]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        if method != "GET":
            self.mutations.append((method, route, input_value))
        if method == "GET" and route == f"repos/owner/repo/pulls/{NUMBER}":
            return {"state": "open", "head": {"sha": self.head}}
        if method == "PUT" and route == f"repos/owner/repo/pulls/{NUMBER}/update-branch":
            if self.conflict:
                raise pp.PipelineError("Merge conflict (HTTP 422)")
            # compare-and-swap on the exact HEAD; the method comes from the
            # config default (merge_method: merge)
            assert input_value == {"expected_head_sha": self.head, "update_method": "merge"}
            self.head = UPDATED_HEAD
            self.merge_state = "CLEAN"
            return {"message": "Updating pull request branch."}
        raise AssertionError(f"unexpected gh call: {args}")

    def graphql_page(self, owner, name, number, cursor):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": self.head, "baseRefOid": "c" * 40, "baseRefName": "main",
            "state": "OPEN", "isDraft": False,
            "labels": {"nodes": [{"name": label} for label in self.labels],
                       "pageInfo": {"hasNextPage": False}},
            "timelineItems": {
                "edges": [{"cursor": "c1", "node": {
                    "__typename": "PullRequestCommit", "id": "PRC_1",
                    "commit": {"oid": self.head}}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}}

    def published(self):
        """Anything visible in the PR other than the branch update itself."""
        return [call for call in self.mutations if not call[1].endswith("/update-branch")]


def health_of(github):
    """The project's health checker, as it routes the owner (issue #42 comment)."""
    def run_health(config, *, config_path=None, working_dir=None):
        if github.head == HEAD:
            owner = {"number": NUMBER, "head": HEAD, "stage": "integration-merge-ready"}
            return {"state": "yellow", "integration_owner": owner,
                    "review_candidates": [], "content_review_candidates": [],
                    "merge_executable": [owner], "findings": [dict(BARRIER)]}
        # A two-parent HEAD without pp:base-sync-done: the committed review
        # does not cover it, so it is a legacy integration REVIEW.
        owner = {"number": NUMBER, "head": github.head, "stage": "legacy-integration-review"}
        return {"state": "yellow", "integration_owner": owner,
                "review_candidates": [owner], "content_review_candidates": [],
                "merge_executable": [], "findings": [dict(BARRIER)]}
    return run_health


@pytest.fixture
def pipelinectl(tmp_path, monkeypatch, capsys):
    config = tmp_path / "pipelinectl.json"
    config.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "pp-bot",
        "health_command": ["project-health", "-json"], "base_sync_merge": True,
    }), encoding="utf-8")
    monkeypatch.setenv("PP_PIPELINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PP_PIPELINE_REPLICAS", raising=False)
    monkeypatch.delenv("PP_PIPELINE_EXCLUDED_NUMBERS", raising=False)

    def invoke(github, *command):
        monkeypatch.setattr(pp, "GitHub", lambda: github)
        monkeypatch.setattr(pp, "run_health", health_of(github))
        code = pp.run(["--config", str(config), *command])
        captured = capsys.readouterr()
        return code, json.loads(captured.out), captured.err

    return invoke


def lease_file(tmp_path, result):
    path = tmp_path / "lease.txt"
    path.write_text(result["lease"], encoding="ascii")
    return str(path.resolve())


def test_behind_owner_update_keeps_ship_and_publishes_no_marker(pipelinectl, tmp_path):
    github = FakeGitHub()

    code, election, warning = pipelinectl(github, "next", "merge")
    assert code == 0 and election["action"] == "merge"
    assert pp.decode_lease(election["lease"])["mode"] == "update-branch"
    assert "EXPERIMENTAL" in warning and "#42" in warning

    code, completed, _ = pipelinectl(
        github, "complete", "merge", "--lease-file", lease_file(tmp_path, election))
    assert code == 0
    assert completed["action"] == "updated"
    assert (completed["old_head"], completed["new_head"]) == (HEAD, UPDATED_HEAD)

    assert github.labels == ["ship"]      # carry is not dropped
    assert github.published() == []       # no intent, no done, no labels, no merge


def test_updated_owner_is_not_merged_autonomously(pipelinectl, tmp_path):
    github = FakeGitHub()
    _code, election, _ = pipelinectl(github, "next", "merge")
    pipelinectl(github, "complete", "merge", "--lease-file", lease_file(tmp_path, election))

    code, merge, _ = pipelinectl(github, "next", "merge")
    assert code == 0 and merge["action"] == "wait"
    assert "waiting for integration REVIEW" in merge["reason"]

    code, review, _ = pipelinectl(github, "next", "review")
    assert code == 0 and review == {
        "action": "fallback",
        "reason": "integration/base-sync state requires the full skill"}
    assert github.published() == []


def test_update_conflict_fails_with_nothing_published(pipelinectl, tmp_path):
    github = FakeGitHub(conflict=True)
    _code, election, _ = pipelinectl(github, "next", "merge")

    code, failed, error = pipelinectl(
        github, "complete", "merge", "--lease-file", lease_file(tmp_path, election))

    assert code == 2
    assert failed["action"] == "error" and "422" in failed["error"]
    assert "422" in error
    assert github.head == HEAD and github.labels == ["ship"]
    assert github.published() == []       # the #42 incident: no done after 422


def test_capabilities_say_the_opt_in_is_experimental(pipelinectl):
    code, capabilities, _ = pipelinectl(FakeGitHub(), "capabilities")

    assert code == 0 and capabilities["base_sync_merge"] == "experimental"
