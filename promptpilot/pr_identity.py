"""Plan 196 slice A: immutable PR identity and a read-only inventory.

This module grants no lease, review proof, checkout, push, or merge authority.
The existing v1 mutation protocol deliberately does not consume this view.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re

from .pipeline_errors import PipelineError


IDENTITY_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){id nameWithOwner
  pullRequest(number:$number){id number state isDraft
   baseRepository{id nameWithOwner} baseRefName baseRefOid
   headRepository{id nameWithOwner} headRefName headRefOid isCrossRepository
  }
 }
}
"""

QUEUE_FIELDS = (
    "review_candidates", "parallel_review_candidates", "content_review_candidates",
    "merge_executable", "merge_candidates", "reviewed_waiting_ship",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _text(value, field: str) -> str:
    if (not isinstance(value, str) or not value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PipelineError(f"invalid PR identity field: {field}")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PipelineError(f"invalid UTF-8 PR identity field: {field}") from exc
    return value


@dataclass(frozen=True)
class PRIdentityV1:
    base_repository_id: str
    base_repository: str
    base_ref: str
    pr_id: str
    pr_number: int
    head_repository_id: str
    head_repository: str
    head_ref: str
    head_sha: str
    cross_repository: bool

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key not in {"pr_number", "cross_repository"}:
                _text(value, key)
        if (type(self.pr_number) is not int or not 1 <= self.pr_number <= 2147483647
                or type(self.cross_repository) is not bool):
            raise PipelineError("invalid PR identity number or cross-repository flag")
        if self.base_ref != "main":
            raise PipelineError("PR identity requires base-ref main")
        if not SHA.fullmatch(self.head_sha):
            raise PipelineError("invalid PR identity head SHA")
        if not all(REPOSITORY.fullmatch(value) for value in (
                self.base_repository, self.head_repository)):
            raise PipelineError("invalid PR identity repository name")
        same_id = self.base_repository_id == self.head_repository_id
        same_name = self.base_repository == self.head_repository
        if same_id != same_name or self.cross_repository != (not same_id):
            raise PipelineError("inconsistent PR identity repository metadata")

    def canonical_bytes(self) -> bytes:
        fields = asdict(self)
        fields["cross_repository"] = "true" if self.cross_repository else "false"
        lines = ["pp-pr-identity-v1"]
        lines.extend(f"{key.replace('_', '-')}={value}" for key, value in fields.items())
        return ("\n".join(lines) + "\n").encode("utf-8", errors="strict")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


def _expected(identity: PRIdentityV1, repository: str, repository_id: str) -> PRIdentityV1:
    if (identity.base_repository != repository
            or identity.base_repository_id != _text(repository_id, "expected repository ID")):
        raise PipelineError("unexpected PR identity base repository")
    return identity


def identity_from_graphql(pr: dict, *, repository: str, repository_id: str) -> PRIdentityV1:
    try:
        base, head = pr["baseRepository"], pr["headRepository"]
        identity = PRIdentityV1(
            base["id"], base["nameWithOwner"], pr["baseRefName"], pr["id"], pr["number"],
            head["id"], head["nameWithOwner"], pr["headRefName"], pr["headRefOid"],
            pr["isCrossRepository"],
        )
    except (KeyError, TypeError) as exc:
        raise PipelineError("missing PR identity GraphQL metadata (possibly deleted fork)") from exc
    return _expected(identity, repository, repository_id)


def identity_from_rest(pr: dict, *, repository: str, repository_id: str) -> PRIdentityV1:
    """REST node_id is the GraphQL global ID; never substitute numeric database IDs."""
    try:
        base, head = pr["base"], pr["head"]
        base_repo, head_repo = base["repo"], head["repo"]
        base_id, head_id = base_repo["node_id"], head_repo["node_id"]
        identity = PRIdentityV1(
            base_id, base_repo["full_name"], base["ref"], pr["node_id"], pr["number"],
            head_id, head_repo["full_name"], head["ref"], head["sha"], base_id != head_id,
        )
    except (KeyError, TypeError) as exc:
        raise PipelineError("missing PR identity REST metadata (possibly deleted fork)") from exc
    return _expected(identity, repository, repository_id)


def inventory(gh, config: dict, health: dict) -> dict:
    """Enrich a health snapshot without sorting it or calling any mutation API."""
    if not isinstance(health, dict):
        raise PipelineError("identity inventory requires a health JSON object")
    repository = config["repository"]
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise PipelineError("invalid inventory repository")
    if config.get("base_branch", "main") != "main":
        raise PipelineError("identity inventory requires configured base main")
    root = gh.json("api", f"repos/{repository}")
    if not isinstance(root, dict) or root.get("full_name") != repository:
        raise PipelineError("unexpected inventory repository metadata")
    repository_id = _text(root.get("node_id"), "repository ID")
    if config.get("base_repository_id", repository_id) != repository_id:
        raise PipelineError("inventory repository ID differs from configured identity")
    owner, name = repository.split("/")
    cache, findings = {}, []

    def enrich(item):
        if not isinstance(item, dict):
            raise PipelineError("invalid identity inventory candidate")
        number = item.get("number")
        if type(number) is not int or not 1 <= number <= 2147483647:
            raise PipelineError("invalid identity inventory candidate number")
        if number not in cache:
            try:
                payload = gh.json("api", "graphql", "-f", f"query={IDENTITY_QUERY}",
                                  "-F", f"owner={owner}", "-F", f"name={name}",
                                  "-F", f"number={number}")
                if not isinstance(payload, dict) or payload.get("errors"):
                    raise PipelineError("identity GraphQL query returned errors")
                repo = payload["data"]["repository"]
                if repo["id"] != repository_id or repo["nameWithOwner"] != repository:
                    raise PipelineError("identity GraphQL repository changed")
                pr = repo["pullRequest"]
                identity = identity_from_graphql(pr, repository=repository,
                                                repository_id=repository_id)
                if identity.pr_number != number:
                    raise PipelineError("identity GraphQL PR number changed")
                base_oid = pr["baseRefOid"]
                if not isinstance(base_oid, str) or not SHA.fullmatch(base_oid):
                    raise PipelineError("invalid identity integration base SHA")
                if pr.get("state") != "OPEN" or pr.get("isDraft") is not False:
                    raise PipelineError("identity candidate is no longer an open ready PR")
                cache[number] = (identity, base_oid, None)
            except (PipelineError, KeyError, TypeError, UnicodeError) as exc:
                cache[number] = (None, None, str(exc))
        identity, base_oid, error = cache[number]
        if not error and item.get("head") != identity.head_sha:
            error = "identity HEAD differs from health candidate"
        if error:
            finding = {"code": "invalid_pr_identity", "number": number, "detail": error}
            if finding not in findings:
                findings.append(finding)
            return None
        return dict(item, pr_identity=identity.to_dict(), identity_sha256=identity.sha256,
                    base_ref_oid=base_oid)

    result = dict(health)
    for field in QUEUE_FIELDS:
        if field not in health:
            continue
        if not isinstance(health[field], list):
            raise PipelineError(f"invalid identity inventory queue: {field}")
        result[field] = [entry for item in health[field] if (entry := enrich(item)) is not None]
    if health.get("integration_owner") is not None:
        result["integration_owner"] = enrich(health["integration_owner"])
    existing = health.get("findings", [])
    if not isinstance(existing, list):
        raise PipelineError("invalid identity inventory findings")
    result["findings"] = [*existing, *findings]
    result.update(identity_inventory_version=1, identity_authority="read-only",
                  identity_v2_enabled=False)
    return result
