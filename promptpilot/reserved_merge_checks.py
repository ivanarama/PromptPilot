"""Prove the documented OneBase bench exception for an existing merge reservation."""

from .merge_finish_proof import REPOSITORY, REQUIRED, TRUSTED


QUERY = """
query($number:Int!, $head:String!) {
  repository(owner:"ivanarama", name:"onebase") {
    ref(qualifiedName:"refs/heads/main") {
      target { oid }
      compare(headRef:$head) { status behindBy }
      branchProtectionRule {
        requiresStatusChecks requiresStrictStatusChecks requiredStatusChecks { context app { databaseId } }
      }
    }
    pullRequest(number:$number) {
      state isDraft baseRefName baseRefOid headRefOid mergeStateStatus mergeable reviewDecision body
      commits(last:1) { nodes { commit {
        oid statusCheckRollup { contexts(first:100) {
          totalCount pageInfo { hasNextPage } nodes {
            __typename
            ... on CheckRun { name status conclusion isRequired(pullRequestNumber:$number) checkSuite { app { databaseId } } }
            ... on StatusContext { context state isRequired(pullRequestNumber:$number) }
          }
        } }
      } } }
    }
  }
}
"""


def _check_results(contexts):
    rows = contexts["nodes"]
    if contexts["pageInfo"]["hasNextPage"] is not False or len(rows) != contexts["totalCount"]:
        return False, "incomplete check rollup"
    confirmed = set()
    bench_failed = False
    for row in rows:
        if row["__typename"] == "StatusContext":
            if (row["isRequired"] is not False or row["context"] in REQUIRED | {"bench"}
                    or row["state"] != "SUCCESS"):
                return False, f"unexpected commit status: {row['context']}={row['state']}"
            continue
        if row["__typename"] != "CheckRun":
            return False, "unknown check type"
        name, conclusion = row["name"], row["conclusion"]
        if row["status"] != "COMPLETED":
            return False, f"{name}={row['status']}"
        if name in REQUIRED or name == "bench":
            if (row["isRequired"] is not (name in REQUIRED)
                    or row["checkSuite"]["app"]["databaseId"] != 15368):
                return False, f"unexpected requirement or producer for {name}"
            if name == "bench" and conclusion == "FAILURE" and not bench_failed:
                bench_failed = True
                continue
            if name not in REQUIRED or name in confirmed or conclusion != "SUCCESS":
                return False, f"unexpected or duplicate check: {name}={conclusion}"
            confirmed.add(name)
        elif row["isRequired"] is not False or conclusion not in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
            return False, f"additional check: {name}={conclusion}"
    if confirmed != REQUIRED:
        return False, "missing required checks: " + ", ".join(sorted(REQUIRED - confirmed))
    if not bench_failed:
        return False, "no isolated optional bench failure"
    return True, "only the optional bench check failed"


def verify_optional_bench(gh, config, intent, snapshot, body):
    if (config["repository"] != REPOSITORY or config.get("trusted_account") != TRUSTED
            or config.get("base_branch") != "main" or set(config.get("required_checks") or ()) != REQUIRED):
        return False, "no optional bench recovery policy for this configuration"
    head = intent["head"]
    response = gh.json("api", "graphql", "--input", "-", input_value={
        "query": QUERY, "variables": {"number": intent["number"], "head": head},
    })
    try:
        if response.get("errors"):
            return False, "GitHub returned incomplete recovery evidence"
        repo = response["data"]["repository"]
        pr, ref = repo["pullRequest"], repo["ref"]
        if (pr["state"] != "OPEN" or pr["isDraft"] is not False or pr["baseRefName"] != "main"
                or pr["headRefOid"] != head or snapshot["headRefOid"] != head or pr["body"] != body
                or pr["mergeStateStatus"] != "UNSTABLE" or pr["mergeable"] != "MERGEABLE"
                or pr["reviewDecision"] not in (None, "APPROVED")):
            return False, "PR head, body or merge eligibility changed"
        if (ref["target"]["oid"] != snapshot["baseRefOid"] or pr["baseRefOid"] != ref["target"]["oid"]
                or ref["compare"]["status"] not in {"AHEAD", "IDENTICAL"} or ref["compare"]["behindBy"] != 0):
            return False, "HEAD does not include the verified current main"
        rule = ref["branchProtectionRule"]
        requirements = rule["requiredStatusChecks"]
        if (rule["requiresStatusChecks"] is not True or rule["requiresStrictStatusChecks"] is not True
                or len(requirements) != len(REQUIRED)
                or {(r["context"], r["app"]["databaseId"]) for r in requirements}
                != {(name, 15368) for name in REQUIRED}):
            return False, "strict branch protection or required checks changed"
        commits = pr["commits"]["nodes"]
        if len(commits) != 1 or commits[0]["commit"]["oid"] != head:
            return False, "check rollup belongs to a different commit"
        return _check_results(commits[0]["commit"]["statusCheckRollup"]["contexts"])
    except (KeyError, TypeError, AttributeError, IndexError):
        return False, "incomplete GitHub recovery evidence"
