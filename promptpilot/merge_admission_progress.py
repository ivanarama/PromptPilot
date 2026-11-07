"""Read-only, bounded evidence for ONE same-HEAD recovery admission.

This is not a mutation lease. Version 1 deliberately recognizes only a current
positive legacy review with sticky ship, and its first pending base-sync intent
while HEAD still equals ``from``. Carry, cleanup and ambiguous sends stay on
the existing fail-closed path. Every normal election and mutation gate remains.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from pathlib import Path

from . import project_pipeline as pp
from .merge_ship_authorization import _INTENT, _protocol_body, _full_edges, _time

PROTOCOL = "promptpilot-merge-admission-progress-v1"
TTL = 300
MAX_READS = 32
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
NODE = re.compile(r"[A-Za-z0-9_=-]+\Z")
FACT_FIELDS = {"protocol", "repository", "config_sha256", "config_path", "checkout",
               "number", "head", "source", "phase", "intent"}
SOURCE_FIELDS = {"epoch", "anchor", "review_id", "claim_id", "completion_id",
                 "review_node", "claim_node", "completion_node", "ship_event"}
INTENT_FIELDS = {"id", "node", "body_sha256", "base"}


def _need(value, message):
    if not value:
        raise pp.PipelineError("merge admission progress: " + message)


def _source_identity(value):
    _need(isinstance(value, dict) and set(value) == SOURCE_FIELDS, "unknown source schema")
    _need(isinstance(value["epoch"], str) and HEX.fullmatch(value["epoch"]), "invalid epoch")
    for name in ("review_id", "claim_id", "completion_id"):
        _need(type(value[name]) is int and 0 < value[name] <= 2**63 - 1, "invalid source ID")
    for name in ("anchor", "review_node", "claim_node", "completion_node", "ship_event"):
        _need(isinstance(value[name], str) and NODE.fullmatch(value[name]), "invalid source node")


def validate_facts(value):
    """Validate a persisted normalized fact, never accept a stage-only hint."""
    _need(isinstance(value, dict) and set(value) == FACT_FIELDS, "unknown fact schema")
    _need(value["protocol"] == PROTOCOL, "unknown fact protocol")
    _need(isinstance(value["repository"], str)
          and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]), "invalid repository")
    _need(isinstance(value["config_sha256"], str) and HEX.fullmatch(value["config_sha256"]),
          "invalid config digest")
    _need(all(isinstance(value[k], str) and Path(value[k]).is_absolute()
              for k in ("config_path", "checkout")), "unbound execution paths")
    _need(type(value["number"]) is int and value["number"] > 0
          and isinstance(value["head"], str) and SHA.fullmatch(value["head"]), "invalid target")
    _source_identity(value["source"])
    if value["phase"] == "reviewed-source":
        _need(value["intent"] is None, "source baseline has an intent")
    else:
        item = value["intent"]
        _need(value["phase"] == "pending-base-sync" and isinstance(item, dict)
              and set(item) == INTENT_FIELDS, "unknown progress phase")
        _need(type(item["id"]) is int and 0 < item["id"] <= 2**63 - 1
              and isinstance(item["node"], str) and NODE.fullmatch(item["node"])
              and isinstance(item["body_sha256"], str) and HEX.fullmatch(item["body_sha256"])
              and isinstance(item["base"], str) and SHA.fullmatch(item["base"]), "invalid intent identity")
    return value


def advanced(before, after):
    """The only v1 transition. A second attempt of the same intent is not progress."""
    try:
        validate_facts(before)
        validate_facts(after)
        immutable = FACT_FIELDS - {"phase", "intent"}
        return (before["phase"] == "reviewed-source" and after["phase"] == "pending-base-sync"
                and all(before[k] == after[k] for k in immutable))
    except (pp.PipelineError, TypeError, ValueError):
        return False


class VerifiedProgress:
    """Only a signed, fresh producer result can enter the local hold machinery."""
    def __init__(self, token, *, config, config_path, checkout, number, head):
        value = pp.decode_signed_lease(token)
        _need(set(value) == {"version", "purpose", "facts", "issued_at", "expires_at",
                             "mutation_authorized"}, "unknown attestation fields")
        _need(type(value["version"]) is int and value["version"] == 1 and value["purpose"] == PROTOCOL
              and value["mutation_authorized"] is False, "not an admission-only attestation")
        _need(type(value["issued_at"]) is int and type(value["expires_at"]) is int
              and value["expires_at"] - value["issued_at"] == TTL
              and value["issued_at"] <= time.time() <= value["expires_at"], "expired attestation")
        facts = validate_facts(value["facts"])
        expected = {"repository": config["repository"], "config_sha256": pp.digest(config),
                    "config_path": str(Path(config_path).resolve()),
                    "checkout": str(Path(checkout).resolve()), "number": number, "head": head}
        _need(all(facts[k] == v for k, v in expected.items()), "attestation execution identity differs")
        self._encoded = pp.canonical(facts)
        self._expires_at = value["expires_at"]

    @property
    def facts(self):
        import json
        _need(time.time() <= self._expires_at, "expired attestation")
        return json.loads(self._encoded)


class _ReadOnly:
    def __init__(self, gh):
        self.gh, self.reads = gh, 0

    def _count(self):
        self.reads += 1
        _need(self.reads <= MAX_READS, "read bound exceeded")

    def json(self, *args, input_value=None):
        _need(input_value is None and args and args[0] == "api"
              and len(args) == 2 and not args[1].startswith("-"), "non-read request refused")
        self._count()
        return self.gh.json(*args)

    def graphql_page(self, *args):
        self._count()
        return self.gh.graphql_page(*args)


def observe(gh, config, config_path, number, head):
    try:
        return _observe(gh, config, config_path, number, head)
    except pp.PipelineError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        raise pp.PipelineError("merge admission progress: incomplete canonical facts") from exc


def _observe(gh, config, config_path, number, head):
    """Prove a bounded target using full stable server reads; write nothing."""
    _need(type(number) is int and number > 0 and isinstance(head, str) and SHA.fullmatch(head),
          "invalid requested target")
    _need(config["base_branch"] == "main" and config.get("required_checks"),
          "v1 requires main and explicit required checks")
    key = pp.pipeline_lease_key(create=False)  # Inspection must never bootstrap a signing key.
    reader = _ReadOnly(gh)
    _need(reader.json("api", "user").get("login") == config["trusted_account"], "wrong GitHub actor")
    snapshot = pp.stable_timeline(reader, config, number)
    pp.validate_common(snapshot, config, {"head": head})
    _full_edges(snapshot)
    _need(len(snapshot["edges"]) <= 1000, "history bound exceeded")
    labels = set(snapshot["labels"])
    _need("ship" in labels and not labels & {"hold", "manual", "needs-decision", "changes-requested"},
          "stopped label route")
    info = pp.epoch(snapshot, config["trusted_account"])
    pp.validate_epoch_safety(info, config["trusted_account"])
    from .review_routing_policy import require_legacy
    require_legacy(reader, config, number, snapshot=snapshot)
    proof = pp.proof(info, head, config["trusted_account"])
    _need(proof and proof["outcome"] == "reviewed"
          and pp.trusted_ship_authorized(info, config["trusted_account"]), "unproven current source/ship")
    by_id = {pp.comment_id(e["node"]): (i, e["node"]) for i, e in enumerate(info["edges"])
             if e["node"]["__typename"] == "IssueComment"}
    ordered = [by_id[proof[k]] for k in ("review_id", "claim_id", "completion_id")]
    _need(ordered[0][0] < ordered[1][0] < ordered[2][0]
          and _time(ordered[0][1]["createdAt"]) <= _time(ordered[1][1]["createdAt"])
          <= _time(ordered[2][1]["createdAt"]), "source server order differs")
    ships = [e["node"] for e in info["edges"] if e["node"]["__typename"] in
             {"LabeledEvent", "UnlabeledEvent"} and e["node"].get("label", {}).get("name") == "ship"]
    ship = ships[-1]["id"]
    ship_index = next(i for i, edge in enumerate(snapshot["edges"]) if edge["node"]["id"] == ship)
    source = {k: proof[k] for k in ("review_id", "claim_id", "completion_id", "review_node",
                                   "claim_node", "completion_node")}
    source.update(epoch=info["hash"], anchor=info["anchor_id"], ship_event=ship)
    # Unsafe or unknown protocol markers cannot be misread as "no intent".
    intents = []
    for full_index, edge in enumerate(snapshot["edges"]):
        node = edge["node"]
        if node["__typename"] != "IssueComment":
            continue
        body = _protocol_body(node.get("body") or "")
        if "<!-- pp:merge-cleanup-" in body or "<!-- pp:base-sync-done" in body:
            raise pp.PipelineError("merge admission progress: cleanup/completed sync is outside v1")
        if "<!-- pp:base-sync-" not in body:
            continue
        matches = list(_INTENT.finditer(body))
        _need(len(matches) == 1 and body.count("<!-- pp:base-sync-") == 1
              and node.get("author", {}).get("login") == config["trusted_account"]
              and "lastEditedAt" in node and node["lastEditedAt"] is None,
              "untrusted/edited/unknown base-sync intent")
        match = matches[0]
        _need(match[1] == head and match[7] == "none" and match[6] == ship
              and [int(match[k]) for k in (3, 4, 5)] ==
              [proof[k] for k in ("review_id", "claim_id", "completion_id")]
              and full_index - info["anchor_index"] - 1 > proof["completion_index"]
              and full_index > ship_index,
              "intent source/ship/proof differs")
        _need(_time(node["createdAt"]) >= max(_time(ordered[2][1]["createdAt"]),
              _time(ships[-1]["createdAt"])), "intent predates source proof/ship")
        intents.append({"id": pp.comment_id(node), "node": node["id"],
                        "body_sha256": hashlib.sha256(node["body"].encode("utf8")).hexdigest(),
                        "base": match[2]})
    _need(len(intents) <= 1, "duplicate intents require full recovery")
    repo = config["repository"]
    pr = reader.json("api", f"repos/{repo}/pulls/{number}")
    _need(type(pr.get("number")) is int and pr.get("number") == number
          and pr.get("state") == "open" and pr.get("merged") is False
          and pr.get("draft") is False and pr.get("head", {}).get("sha") == head
          and pr.get("head", {}).get("repo", {}).get("full_name") == repo
          and pr.get("base", {}).get("repo", {}).get("full_name") == repo
          and pr.get("base", {}).get("ref") == "main"
          and pr.get("base", {}).get("sha") == snapshot["baseRefOid"]
          and {x["name"] for x in pr["labels"]} == labels, "REST source differs")
    commit = reader.json("api", f"repos/{repo}/git/commits/{head}")
    _need(commit.get("sha") == head and isinstance(commit.get("parents"), list)
          and commit["parents"] and all(SHA.fullmatch(p.get("sha", "")) for p in commit["parents"])
          and SHA.fullmatch(commit.get("tree", {}).get("sha", "")), "invalid source graph")
    main = reader.json("api", f"repos/{repo}/git/ref/heads/main")
    main_head = main.get("object", {}).get("sha")
    _need(main.get("ref") == "refs/heads/main" and main.get("object", {}).get("type") == "commit"
          and isinstance(main_head, str) and SHA.fullmatch(main_head), "invalid protected main")
    if intents and intents[0]["base"] != main_head:
        base = intents[0]["base"]
        compare = reader.json("api", f"repos/{repo}/compare/{base}...{main_head}")
        _need(compare.get("status") == "ahead" and compare.get("behind_by") == 0
              and compare.get("base_commit", {}).get("sha") == base
              and compare.get("merge_base_commit", {}).get("sha") == base
              and compare.get("commits") and compare["commits"][-1].get("sha") == main_head,
              "intent base is not an ancestor of current main")
    checks = reader.json("api", f"repos/{repo}/commits/{head}/check-runs?per_page=100")
    _need(type(checks.get("total_count")) is int and checks["total_count"] <= 100
          and len(checks.get("check_runs", [])) == checks["total_count"], "incomplete CI")
    for name in config["required_checks"]:
        rows = [r for r in checks["check_runs"] if r.get("name") == name and r.get("head_sha") == head
                and r.get("app", {}).get("id") == 15368]
        _need(rows and all(type(r.get("id")) is int and r["id"] > 0 for r in rows), "missing/invalid required CI")
        latest = max(rows, key=lambda r: r["id"])
        _need(latest.get("status") == "completed" and latest.get("conclusion") == "success", "CI not successful")
    _need(pp.digest(pp.stable_timeline(reader, config, number)) == pp.digest(snapshot)
          and reader.json("api", f"repos/{repo}/git/ref/heads/main") == main,
          "source/main changed during proof")
    facts = {"protocol": PROTOCOL, "repository": repo, "config_sha256": pp.digest(config),
             "config_path": str(Path(config_path).resolve()), "checkout": str(Path.cwd().resolve()),
             "number": number, "head": head, "source": source,
             "phase": "pending-base-sync" if intents else "reviewed-source",
             "intent": intents[0] if intents else None}
    validate_facts(facts)
    issued = int(time.time())
    value = {"version": 1, "purpose": PROTOCOL, "facts": facts, "issued_at": issued,
             "expires_at": issued + TTL, "mutation_authorized": False}
    payload = pp.encode_lease(value)
    signature = base64.urlsafe_b64encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
    return {"action": "observed", "protocol": PROTOCOL, "number": number, "head": head,
            "mutation_authorized": False, "attestation": payload + "." + signature.decode("ascii").rstrip("=")}
