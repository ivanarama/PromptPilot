"""Re-observe an already-pushed legacy base sync. No method here publishes."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import (
    base_sync_carry,
    merge_finish_proof as narrow,
    merge_ship_authorization as lineage,
)
from . import project_pipeline as pp
from .review_routing_policy import _file_bytes, require_legacy


_need = narrow._need
_SHA = re.compile(r"[0-9a-f]{40}")
_ROOT = "repos/" + narrow.REPOSITORY
_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
_DOCS = {b"CHANGELOG.md", b"docs/features.md"}
_SHA256 = re.compile(r"[0-9a-f]{64}")


class UnsupportedProof(pp.PipelineError):
    """Authenticated immutable facts cannot support this automatic recovery."""


def _proven(condition, message):
    if not condition:
        raise UnsupportedProof(message)


def _git_bytes(config, working_dir, *args, accepted=(0,)):
    try:
        result = subprocess.run(
            [
                base_sync_carry.git_bin(config),
                "--no-replace-objects",
                "-C",
                str(working_dir),
                "-c",
                "maintenance.auto=false",
                *args,
            ],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise pp.PipelineError("git could not verify the pushed tree") from exc
    _need(
        result.returncode in accepted, f"git {args[0]} could not verify the pushed tree"
    )
    return result.returncode, result.stdout


# Audited OneBase mergecheck sources from the protected base, independent of
# the candidate tree. Only these exact bytes may be compiled and executed.
_CHECKER = {
    "main.go": (
        "fed95a88d55d1dac72833aa93b0a0f4be632c491",
        "f224f3646baad4609f837a19e313eb1fe088d5fead20418dd1f65097495493e5",
    ),
    "main_additions.go": (
        "648624b557d94454a95a97e24d8cb16e08e09d52",
        "81b8cea106da2b104f57d10755d484075d2d953e45a23b189eafc2ad1b3f36c6",
    ),
}


def _check_document(config, working_dir, path, stages, result_oid):
    go = shutil.which("go")
    git = shutil.which(base_sync_carry.git_bin(config))
    _need(go and git, "the trusted mergecheck requires Go and Git")
    env = dict(
        os.environ,
        GOENV="off",
        GO111MODULE="off",
        GOWORK="off",
        GOTOOLCHAIN="local",
        GOFLAGS="",
        CGO_ENABLED="0",
        GOPROXY="off",
        GOSUMDB="off",
        PATH=str(Path(git).parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        for name, (oid, expected) in _CHECKER.items():
            data = _git_bytes(config, working_dir, "cat-file", "blob", oid)[1]
            _need(
                hashlib.sha256(data).hexdigest() == expected,
                "trusted mergecheck source differs",
            )
            (directory / name).write_bytes(data)
        inputs = []
        for name, oid in zip(
            ("base", "ours", "theirs", "result"), (*stages, result_oid)
        ):
            data = _git_bytes(config, working_dir, "cat-file", "blob", oid)[1]
            file = directory / name
            file.write_bytes(data)
            inputs.extend(("-" + name, str(file)))
        modes = (
            ("entries", "main-additions") if path == b"CHANGELOG.md" else ("entries",)
        )
        executable = directory / ("mergecheck.exe" if os.name == "nt" else "mergecheck")
        try:
            build = subprocess.run(
                [go, "build", "-o", str(executable), *_CHECKER],
                cwd=temp,
                env=env,
                capture_output=True,
                timeout=120,
            )
            _need(build.returncode == 0, "trusted mergecheck could not be built")
            for mode in modes:
                check = subprocess.run(
                    [str(executable), "-kind", mode, *inputs],
                    cwd=temp,
                    env=env,
                    capture_output=True,
                    timeout=60,
                )
                if check.returncode == 0:
                    return
                _need(
                    check.returncode == 1
                    and check.stderr.startswith(b"mergecheck:")
                    and not check.stderr.startswith(b"mergecheck: git merge-file:"),
                    "trusted mergecheck could not verify the conflicted document",
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise pp.PipelineError("trusted mergecheck unavailable") from exc
        raise UnsupportedProof("trusted mergecheck rejected the conflict resolution")


def _enrolled_resolution(config, working_dir, cp):
    """Load the operator's exact decision pins and original owner-referenced manifest.

    Enroll outside the checkout, under the pipeline state directory, in
    merge-resolutions/<owner>/<repo>/<pr>/<tree>.json. This version-1 record
    lists resolution_sha256 and ordered approvals (id, body_sha256). Store
    the original manifest bytes alongside it as <resolution_sha256>.json.
    The operator checks the meaning of the decisions before enrollment.
    A PR body, a checkpoint, and a path supplied by a comment cannot enroll it.
    """
    directory = (
        pp._merge_comment_index_path(config).parent
        / "merge-resolutions"
        / cp["repository"]
        / str(cp["number"])
    )
    index = directory / (cp["tree"] + ".json")
    try:
        if not index.exists():
            return None
        checkout = Path(working_dir).resolve()

        def read(path):
            _proven(
                not path.resolve().is_relative_to(checkout),
                "resolution enrollment must be an operator file outside the checkout",
            )
            try:
                return _file_bytes(path)
            except pp.PipelineError as exc:
                raise UnsupportedProof(
                    "enrolled resolution evidence is not a bounded regular file"
                ) from exc

        entry = json.loads(read(index))
        _proven(
            isinstance(entry, dict)
            and set(entry) == {"version", "resolution_sha256", "approvals"}
            and type(entry["version"]) is int
            and entry["version"] == 1
            and isinstance(entry["resolution_sha256"], str)
            and _SHA256.fullmatch(entry["resolution_sha256"]),
            "invalid resolution enrollment",
        )
        approvals = entry["approvals"]
        _proven(
            isinstance(approvals, list)
            and 0 < len(approvals) <= 16
            and all(
                isinstance(a, dict)
                and set(a) == {"id", "body_sha256"}
                and type(a["id"]) is int
                and 0 < a["id"] < 2**63
                and isinstance(a["body_sha256"], str)
                and _SHA256.fullmatch(a["body_sha256"])
                for a in approvals
            )
            and len({a["id"] for a in approvals}) == len(approvals),
            "invalid resolution decision pins",
        )
        raw = read(directory / (entry["resolution_sha256"] + ".json"))
        _proven(
            hashlib.sha256(raw).hexdigest() == entry["resolution_sha256"],
            "enrolled resolution manifest bytes differ",
        )
        manifest = json.loads(raw)
        _proven(
            isinstance(manifest, dict)
            and manifest.get("repository") == cp["repository"]
            and type(manifest.get("pull_request")) is int
            and manifest["pull_request"] == cp["number"]
            and manifest.get("source") == cp["from"]
            and manifest.get("main") == cp["base"]
            and manifest.get("tree") == cp["tree"]
            and isinstance(manifest.get("conflicts"), dict)
            and manifest["conflicts"],
            "enrolled resolution manifest identifies a different merge",
        )
        return dict(entry, conflicts=manifest["conflicts"])
    except (ValueError, OSError) as exc:
        raise UnsupportedProof("cannot read enrolled resolution evidence") from exc


def _resolution_approvals(gh, cp, resolution, nodes, order, after, before, protocol):
    comments = {pp.comment_id(n): n for n in nodes if n["__typename"] == "IssueComment"}
    ids = []
    for approval in resolution["approvals"]:
        node = comments.get(approval["id"])
        _proven(
            node
            and after < order[node["id"]] < before
            and node["author"]["login"] == narrow.TRUSTED
            and node["lastEditedAt"] is None
            and hashlib.sha256(node["body"].encode()).hexdigest()
            == approval["body_sha256"],
            "resolution decision absent, edited or outside the reviewed pre-intent history",
        )
        pins = {cp["from"], cp["base"], cp["tree"]}
        if not ids:
            pins.add(resolution["resolution_sha256"])
        hashes = re.findall(r"(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])", node["body"])
        _proven(
            pins.issubset(hashes),
            "owner decision does not identify the exact resolution",
        )
        value = narrow._comment_readback(gh, cp["number"], node, node["body"])
        _proven(
            value.get("author_association") == "OWNER"
            and node["createdAt"] == value.get("created_at") == value.get("updated_at"),
            "owner resolution decision readback differs",
        )
        ids.append(node["id"])
        after = order[node["id"]]
    # An owner can stop or supersede an approval in ordinary prose. Only
    # the canonical transaction's separately checked markers are expected.
    for node in nodes[after + 1 :]:
        kind = node["__typename"]
        if kind == "IssueComment" and node["id"] not in protocol:
            actor = (node.get("author") or {}).get("login")
            _proven(
                actor and actor != narrow.TRUSTED,
                "new owner or unattributed comment after the enrolled resolution decision",
            )
        if kind in ("LabeledEvent", "UnlabeledEvent") and node["label"]["name"] in {
            "hold",
            "manual",
            "needs-decision",
            "changes-requested",
        }:
            _proven(
                kind == "UnlabeledEvent"
                and (node.get("actor") or {}).get("login") == narrow.TRUSTED,
                "new stop or untrusted removal after the enrolled resolution decision",
            )
    return ids


def _tree(config, working_dir, old, base, head, tree, resolution=None):
    git = lambda *args: narrow._git(config, working_dir, *args)
    _need(
        git("rev-list", "-n", "1", "--parents", head).split() == [head, old, base]
        and git("rev-parse", head + "^{tree}") == tree,
        "local parents/tree differ from checkpoint",
    )
    _tree_contents(config, working_dir, old, base, tree, resolution)


def _tree_contents(config, working_dir, old, base, tree, resolution):
    rc, data = _git_bytes(
        config,
        working_dir,
        "merge-tree",
        "--write-tree",
        "--no-messages",
        "-z",
        old,
        base,
        accepted=(0, 1),
    )
    items = data.rstrip(b"\0").split(b"\0")
    automatic = items.pop(0).decode("ascii")
    _need(_SHA.fullmatch(automatic), "invalid automatic tree")
    if rc == 0:
        _proven(
            not items and automatic == tree and resolution is None,
            "pushed tree differs from conflict-free merge",
        )
        return
    conflicts = {}
    for row in items:
        match = re.fullmatch(rb"100644 ([a-f0-9]{40}) ([123])\t(.+)", row)
        _proven(
            match and (match[3] in _DOCS or resolution is not None),
            "unsupported conflict or file mode",
        )
        group = conflicts.setdefault(match[3], {})
        stage = int(match[2])
        _proven(stage not in group, "duplicate conflict stage")
        group[stage] = match[1].decode("ascii")
    _, delta = _git_bytes(
        config,
        working_dir,
        "diff-tree",
        "-r",
        "--name-only",
        "--no-renames",
        "-z",
        automatic,
        tree,
    )
    _proven(
        conflicts and set(delta.rstrip(b"\0").split(b"\0")) == set(conflicts),
        "pushed tree changes paths outside the exact conflict set",
    )
    observed = {}
    for path, group in conflicts.items():
        _proven(set(group) == {1, 2, 3}, "incomplete conflict stages")
        try:
            name = path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedProof("unsupported conflict filename encoding") from exc

        def blob_at(revision):
            _, entry = _git_bytes(
                config,
                working_dir,
                "ls-tree",
                "-z",
                revision,
                "--",
                ":(literal)" + name,
            )
            return re.fullmatch(
                rb"100644 blob ([a-f0-9]{40})\t" + re.escape(path) + rb"\x00", entry
            )

        match = blob_at(tree)
        _proven(match, "changed file mode or missing conflict result")
        result_oid = match[1].decode("ascii")
        if resolution is not None:
            for parent, stage in ((old, 2), (base, 3)):
                entry = blob_at(parent)
                _proven(
                    entry and entry[1].decode("ascii") == group[stage],
                    "conflict stage differs from regular file at parent path",
                )
            observed[name] = {
                key: "100644 blob " + oid
                for key, oid in (
                    ("source", group[2]),
                    ("main", group[3]),
                    ("resolved", result_oid),
                )
            }
        if path in _DOCS:
            _check_document(
                config, working_dir, path, [group[i] for i in (1, 2, 3)], result_oid
            )
    if resolution is not None:
        _proven(
            observed == resolution["conflicts"],
            "actual conflict set or blobs differ from owner resolution",
        )


def _identity(pr, cp):
    repo, branch = pr["head"]["repo"]["full_name"], pr["head"]["ref"]
    labels = sorted(label["name"] for label in pr["labels"])
    _need(
        pr["number"] == cp["number"]
        and pr["state"] == "open"
        and pr["merged"] is False
        and pr["draft"] is False
        and pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"] == narrow.REPOSITORY
        and _SHA.fullmatch(pr["base"]["sha"])
        and pr["head"]["sha"] == cp["to"]
        and _REMOTE_NAME.fullmatch(repo)
        and repo.split("/")[1] not in (".", "..")
        and type(pr["maintainer_can_modify"]) is bool
        and (repo == narrow.REPOSITORY or pr["maintainer_can_modify"])
        and isinstance(branch, str)
        and branch
        and (repo != narrow.REPOSITORY or branch != "main")
        and labels.count("ship") == 1
        and not set(labels) & {"hold", "manual", "needs-decision", "changes-requested"},
        "PR head/base, permission or routing labels changed",
    )
    return (
        repo,
        branch,
        cp["to"],
        pr["base"]["sha"],
        pr["maintainer_can_modify"],
        labels,
    )


def _ref(config, working_dir, remote, branch, sha, *, fetch):
    git = lambda *args: narrow._git(config, working_dir, *args)
    ref = "refs/heads/" + branch
    git("check-ref-format", ref)
    _need(
        git("ls-remote", "--refs", remote, ref) == f"{sha}\t{ref}",
        "remote branch differs",
    )
    if fetch:
        git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            remote,
            ref,
        )


def _previous(gh, config, source, pr, main, previous, ship):
    candidate = lineage.current_lineage_candidate(source, narrow.TRUSTED)
    _need(
        candidate and str(candidate["hops"][-1]["done_id"]) == previous,
        "previous marker is not the source's exact final receipt",
    )
    commits = {
        hop["head"]: gh.json("api", f"{_ROOT}/git/commits/{hop['head']}")
        for hop in candidate["hops"]
    }
    tip = main["object"]["sha"]
    ancestry = {
        base: gh.json("api", f"{_ROOT}/compare/{base}...{tip}")
        for base in {hop["base"] for hop in candidate["hops"]}
        if base != tip
    }
    # Only the already-proven following merge and its markers are projected out.
    # Its first parent is the exact historical source; all other live REST facts stand.
    historical = dict(pr, head=dict(pr["head"], sha=source["headRefOid"]))
    chain = lineage.resolve_current_ship_facts(
        pp,
        config,
        source,
        historical,
        commits[source["headRefOid"]],
        main,
        commits=commits,
        ancestry=ancestry,
    )
    _need(chain["original_ship_event"] == ship, "original ship changed")
    return chain


def _new_checks(gh, head):
    # A done confirms the push. REVIEW and MERGE own the outcome of the NEW
    # head's checks. Include every observed required run (also pending/failed)
    # in the fence; never present absence or red as green.
    response = gh.json(
        "api", f"{_ROOT}/commits/{head}/check-runs?per_page=100&page=1&filter=latest"
    )
    rows = response["check_runs"]
    _need(
        isinstance(rows, list)
        and type(response["total_count"]) is int
        and len(rows) == response["total_count"] < 100,
        "incomplete new-head check inventory",
    )
    found = {}
    for run in rows:
        if run["name"] not in narrow.REQUIRED:
            continue
        rid = run["id"]
        _need(
            type(rid) is int
            and rid > 0
            and rid not in found
            and run["head_sha"] == head
            and run["app"]["slug"] == "github-actions"
            and run["app"]["id"] == 15368
            and isinstance(run["status"], str)
            and run["status"]
            and (run["conclusion"] is None or isinstance(run["conclusion"], str)),
            "unbound new-head required check",
        )
        found[rid] = (rid, run["name"], run["status"], run["conclusion"])
    return sorted(found.values())


def _history(gh, config, cp, pr, main, resolution):
    old, base, head, iid, number = (
        cp[k] for k in ("from", "base", "to", "intent", "number")
    )
    snap = pp.stable_timeline(gh, config, number)
    pp.validate_common(snap, config, {"head": head})
    _need(
        snap["labels"] == sorted(n["name"] for n in pr["labels"])
        and snap["baseRefOid"] == pr["base"]["sha"],
        "REST and GraphQL PR identity differ",
    )
    require_legacy(gh, config, number, snapshot=snap)
    edges = lineage._full_edges(snap)
    nodes = [edge["node"] for edge in edges]
    comments = [n for n in nodes if n["__typename"] == "IssueComment"]
    order = {n["id"]: i for i, n in enumerate(nodes)}
    _need(
        all(isinstance(n["body"], str) and "lastEditedAt" in n for n in comments),
        "incomplete comment history",
    )
    intents = [n for n in comments if pp.comment_id(n) == iid]
    _need(len(intents) == 1, "missing checkpoint intent")
    saved = intents[0]
    match = lineage.intent_line(saved["body"])
    _proven(match and match[1] == old, "invalid checkpoint intent")
    intent_base, review, claim, completion, ship, previous = match.groups()[1:]
    fields = f"from={old} to={head} base={base} previous={previous} ship-event={ship}"
    body = f"<!-- pp:base-sync-done intent={iid} {fields} -->"
    dones, later_intents = [], []
    for n in comments:
        protocol = lineage._protocol_body(n["body"])
        for m in lineage._INTENT.finditer(protocol):
            if m[1] == old:
                if n["id"] != saved["id"]:
                    _proven(
                        order[n["id"]] > order[saved["id"]]
                        and n["body"] == saved["body"]
                        and n["author"]["login"] == narrow.TRUSTED
                        and n["lastEditedAt"] is None,
                        "checkpoint does not name the earliest canonical intent or has a competing intent",
                    )
                    later_intents.append(n)
        for m in lineage._DONE.finditer(protocol):
            if m[1] == str(iid) or m[2] == old or m[3] == head:
                _proven(n["body"] == body, "competing confirmation")
                dones.append(n)
    _proven(len(dones) <= 1, "duplicate confirmation")
    done = dones[0] if dones else None
    for n in (saved, *dones):
        _need(
            n["lastEditedAt"] is None and n["author"]["login"] == narrow.TRUSTED,
            "edited or foreign marker",
        )
    commits = {
        sha: [
            n
            for n in nodes
            if n["__typename"] == "PullRequestCommit" and n["commit"]["oid"] == sha
        ]
        for sha in (old, head)
    }
    _need(
        all(len(group) == 1 for group in commits.values()),
        "source or pushed commit anchor is ambiguous",
    )
    exclude = {
        saved["id"],
        commits[head][0]["id"],
        *(n["id"] for n in dones + later_intents),
    }
    source = dict(
        snap,
        headRefOid=old,
        edges=[edge for edge in edges if edge["node"]["id"] not in exclude],
    )
    info = pp.epoch(source, narrow.TRUSTED)
    pp.validate_epoch_safety(info, narrow.TRUSTED)
    _need(
        info["anchor_id"] == commits[old][0]["id"],
        "source epoch was restarted or restored",
    )
    proof = pp.proof(info, old, narrow.TRUSTED)
    _need(
        proof
        and proof["outcome"] == "reviewed"
        and tuple(str(proof[k]) for k in ("review_id", "claim_id", "completion_id"))
        == (review, claim, completion),
        "source review transaction differs from intent",
    )
    ids = [
        info["anchor_id"],
        proof["review_node"],
        proof["claim_node"],
        proof["completion_node"],
        ship,
        saved["id"],
        commits[head][0]["id"],
    ]
    _need(
        ship in order
        and order[saved["id"]] > max(order[ship], order[proof["completion_node"]])
        and (done is None or order[done["id"]] > order[saved["id"]]),
        "intent or receipt outside approved epoch",
    )
    if resolution is not None:
        ids.extend(
            _resolution_approvals(
                gh,
                cp,
                resolution,
                nodes,
                order,
                max(order[ship], order[proof["completion_node"]]),
                order[saved["id"]],
                exclude,
            )
        )
    for n in nodes[order[info["anchor_id"]] + 1 :]:
        if n["__typename"] == "IssueComment" and n["id"] not in exclude:
            protocol = lineage._protocol_body(n["body"])
            markers = (
                pp.REVIEW.search(protocol),
                pp.CLAIM.fullmatch(protocol),
                pp.COMPLETE.fullmatch(protocol),
            )
            _need(
                not any(
                    m and m[1] == head
                    for m in markers
                    if n["author"]["login"] == narrow.TRUSTED
                ),
                "pushed head already entered its review transaction",
            )
            if re.search(r"(?m)^<!-- pp:base-sync-", protocol):
                # Historical markers for previous hops are verified by the full lineage resolver below.
                _need(
                    previous != "none"
                    and (
                        lineage._INTENT.fullmatch(protocol)
                        or lineage.intent_line(n["body"])
                        or lineage._DONE.fullmatch(protocol)
                    ),
                    "unknown protocol marker in source epoch",
                )
    if previous == "none":
        transitions = [
            e["node"]
            for e in info["edges"]
            if e["node"]["__typename"] in ("LabeledEvent", "UnlabeledEvent")
            and e["node"].get("label", {}).get("name") == "ship"
        ]
        _need(
            pp.trusted_ship_authorized(info, narrow.TRUSTED)
            and transitions[-1]["id"] == ship,
            "trusted source ship revoked or replaced",
        )
        chain = None
    else:
        chain = _previous(gh, config, source, pr, main, previous, ship)
    narrow._comment_readback(gh, number, saved, saved["body"])
    if done:
        narrow._comment_readback(gh, number, done, body)
        ids.append(done["id"])
    narrow._check_addressed(gh, config, number, snap, ids)
    return snap, chain, intent_base, body, pp.comment_id(done) if done else None


def _prepared_index(config, working_dir, source, base, tree):
    git = lambda *args: narrow._git(config, working_dir, *args)
    merge_head_path = Path(
        git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD")
    )
    _need(
        merge_head_path.is_absolute() and merge_head_path.is_file(),
        "prepared merge has no MERGE_HEAD",
    )
    _need(
        git("rev-parse", "HEAD") == source
        and _file_bytes(merge_head_path).splitlines() == [base.encode()]
        and git("write-tree") == tree,
        "prepared merge HEAD, single MERGE_HEAD or index tree differs",
    )


def prepare_before_intent(gh, config, *, number, source, base, tree, working_dir):
    """Check the resolved Git index using the same rules as later receipt recovery."""
    _need(
        config.get("repository") == narrow.REPOSITORY
        and config.get("trusted_account") == narrow.TRUSTED
        and config.get("base_branch") == "main"
        and set(config.get("required_checks", ())) == narrow.REQUIRED
        and type(number) is int
        and 0 < number < 2**63
        and Path(working_dir).is_absolute()
        and all(
            isinstance(oid, str) and _SHA.fullmatch(oid) for oid in (source, base, tree)
        )
        and source != base,
        "invalid pre-intent resolution route",
    )
    cp = {
        "repository": narrow.REPOSITORY,
        "number": number,
        "from": source,
        "to": source,
        "base": base,
        "tree": tree,
    }
    _prepared_index(config, working_dir, source, base, tree)
    resolution = _enrolled_resolution(config, working_dir, cp)
    _tree_contents(config, working_dir, source, base, tree, resolution)
    if resolution is not None:
        pull = f"{_ROOT}/pulls/{number}"
        ref = f"{_ROOT}/git/ref/heads/main"
        _need(
            gh.json("api", "user")["login"] == narrow.TRUSTED,
            "unexpected authenticated user",
        )
        pr = gh.json("api", pull)
        identity = _identity(pr, cp)
        main = gh.json("api", ref)
        _need(
            main["ref"] == "refs/heads/main"
            and main["object"]["type"] == "commit"
            and main["object"]["sha"] == base,
            "protected main changed before intent",
        )
        snap = pp.stable_timeline(gh, config, number)
        pp.validate_common(snap, config, {"head": source})
        _need(
            snap["baseRefOid"] == identity[3] and snap["labels"] == identity[5],
            "REST and GraphQL PR identity differ",
        )
        require_legacy(gh, config, number, snapshot=snap)
        nodes = [edge["node"] for edge in lineage._full_edges(snap)]
        order = {n["id"]: i for i, n in enumerate(nodes)}
        intents = [
            n
            for n in nodes
            if n["__typename"] == "IssueComment"
            and any(
                m[1] == source
                for m in lineage._INTENT.finditer(lineage._protocol_body(n["body"]))
            )
        ]
        _proven(len(intents) <= 1, "competing pre-push intents exist for this source")
        saved = intents[0] if intents else None
        protocol = {n["id"] for n in intents}
        reviewed = dict(
            snap, edges=[e for e in snap["edges"] if e["node"]["id"] not in protocol]
        )
        info = pp.epoch(reviewed, narrow.TRUSTED)
        pp.validate_epoch_safety(info, narrow.TRUSTED)
        anchors = [
            n["id"]
            for n in nodes
            if n["__typename"] == "PullRequestCommit" and n["commit"]["oid"] == source
        ]
        proof = pp.proof(info, source, narrow.TRUSTED)
        _need(
            anchors == [info["anchor_id"]] and proof and proof["outcome"] == "reviewed",
            "enrolled resolution needs the current reviewed source",
        )
        ship = next(
            (
                n["id"]
                for n in reversed(nodes)
                if n["__typename"] == "LabeledEvent" and n["label"]["name"] == "ship"
            ),
            None,
        )
        _proven(ship is not None, "enrolled resolution has no source ship event")
        if saved:
            marker = lineage.intent_line(saved["body"])
            expected = (
                source,
                base,
                *(str(proof[k]) for k in ("review_id", "claim_id", "completion_id")),
                ship,
            )
            _proven(
                marker
                and marker.groups()[:6] == expected
                and (saved.get("author") or {}).get("login") == narrow.TRUSTED
                and saved["lastEditedAt"] is None,
                "existing intent differs from exact source, base, review or ship",
            )
            if marker[7] == "none":
                _proven(
                    pp.trusted_ship_authorized(info, narrow.TRUSTED),
                    "existing intent has no trusted direct ship",
                )
            else:
                previous = lineage.current_lineage_candidate(reviewed, narrow.TRUSTED)
                _proven(
                    previous and str(previous["hops"][-1]["done_id"]) == marker[7],
                    "existing intent has an unproven previous receipt",
                )
                _previous(gh, config, reviewed, pr, main, marker[7], ship)
            narrow._comment_readback(gh, number, saved, saved["body"])
        else:
            _proven(
                pp.current_ship_authorized(gh, config, number, snap, info=info),
                "enrolled resolution needs current trusted ship",
            )
        ids = [
            anchors[0],
            proof["review_node"],
            proof["claim_node"],
            proof["completion_node"],
            ship,
        ]
        ids.extend(
            _resolution_approvals(
                gh,
                cp,
                resolution,
                nodes,
                order,
                max(order[ship], order[proof["completion_node"]]),
                order[saved["id"]] if saved else len(nodes),
                protocol,
            )
        )
        ids.extend(protocol)
        narrow._check_addressed(gh, config, number, snap, ids)
        _need(
            _identity(gh.json("api", pull), cp) == identity
            and gh.json("api", ref) == main,
            "remote identity changed before intent",
        )
    _prepared_index(config, working_dir, source, base, tree)
    return {
        "repository": narrow.REPOSITORY,
        "number": number,
        "source": source,
        "base": base,
        "tree": tree,
        "resolution_sha256": resolution["resolution_sha256"] if resolution else None,
    }


def prepare(gh, config, checkpoint, *, working_dir):
    """Return exact body, observed receipt ID, and digest of the live pre-send evidence."""
    cp = checkpoint
    _need(
        config.get("repository") == cp.get("repository") == narrow.REPOSITORY
        and config.get("trusted_account") == narrow.TRUSTED
        and config.get("base_branch") == "main"
        and set(config.get("required_checks", ())) == narrow.REQUIRED
        and cp.get("next_step") == "confirm-base-sync-done"
        and Path(working_dir).is_absolute()
        and all(
            type(cp.get(k)) is int and 0 < cp[k] < 2**63 for k in ("number", "intent")
        )
        and all(
            isinstance(cp.get(k), str) and _SHA.fullmatch(cp[k])
            for k in ("from", "base", "to", "tree")
        )
        and len({cp["from"], cp["base"], cp["to"]}) == 3,
        "invalid full confirmation route",
    )
    old, base, head, tree = (cp[k] for k in ("from", "base", "to", "tree"))
    _need(
        gh.json("api", "user")["login"] == narrow.TRUSTED,
        "unexpected authenticated user",
    )
    repo = gh.json("api", _ROOT)
    _need(
        repo["full_name"] == narrow.REPOSITORY and repo["permissions"]["push"] is True,
        "repository permission changed",
    )
    pull = f"{_ROOT}/pulls/{cp['number']}"
    pr = gh.json("api", pull)
    identity = _identity(pr, cp)
    remote = f"https://github.com/{identity[0]}.git"
    _ref(config, working_dir, remote, identity[1], head, fetch=True)
    main_path = f"{_ROOT}/git/ref/heads/main"
    main = gh.json("api", main_path)
    tip = main["object"]["sha"]
    _need(
        main["ref"] == "refs/heads/main"
        and main["object"]["type"] == "commit"
        and _SHA.fullmatch(tip),
        "invalid protected main reference",
    )
    _ref(config, working_dir, narrow.REMOTE, "main", tip, fetch=True)
    commit = gh.json("api", f"{_ROOT}/git/commits/{head}")
    lineage._commit_identity(commit, head, old, base)
    _need(commit["tree"]["sha"] == tree, "REST tree differs from checkpoint")
    resolution = _enrolled_resolution(config, working_dir, cp)
    _tree(config, working_dir, old, base, head, tree, resolution)
    protection = gh.json("api", f"{_ROOT}/branches/main/protection")[
        "required_status_checks"
    ]
    _need(
        protection["strict"] is True and set(protection["contexts"]) == narrow.REQUIRED,
        "strict branch protection differs",
    )
    try:
        source_checks = narrow._ci(gh, _ROOT, old)
    except narrow.OutsideScope as exc:
        raise pp.PipelineError(
            "reviewed source CI8 is incomplete or has not passed"
        ) from exc
    checks = [source_checks, _new_checks(gh, head)]
    snap, chain, intent_base, body, done = _history(
        gh, config, cp, pr, main, resolution
    )
    for earlier, later in ((intent_base, base), (base, tip)):
        if earlier != later:
            narrow._git(
                config, working_dir, "merge-base", "--is-ancestor", earlier, later
            )
    _need(
        _identity(gh.json("api", pull), cp) == identity
        and gh.json("api", main_path) == main,
        "remote identity changed during proof",
    )
    _ref(config, working_dir, remote, identity[1], head, fetch=False)
    return (
        body,
        done,
        pp.digest(
            [
                cp,
                identity,
                main,
                commit,
                protection,
                checks,
                snap,
                chain,
                intent_base,
                body,
                *([resolution] if resolution is not None else []),
            ]
        ),
    )
