"""One ordinary BEHIND update, before the separately proven carry (#42).

The signed ticket is not a bypass: every live gate is repeated after receipt
writer admission. A durable one-attempt receipt precedes the PUT. Recovery
observes a mechanical transition; expiry or an absent response never retries it.
The store lives beside the signing key, not in the disposable comment cache.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .pipeline_errors import PipelineError

PROTOCOL = "ordinary-base-sync-v1"
TTL = 300
NEXT = "rerun next merge; carry revalidates proof, ship, tree and current CI"
FIELDS = set("version protocol purpose stage mode repository trusted_account number head base "
             "base_branch snapshot proof config_sha256 nonce issued_at expires_at".split())


def _pp():
    from . import project_pipeline
    return project_pipeline


def _enabled(config):
    return config.get("base_sync_merge") is True and config.get("base_sync_carry") is True


def _wire(value, config):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value["version"]) is not int or value["version"] != 1
            or value["protocol"] != PROTOCOL or value["purpose"] != PROTOCOL
            or value["stage"] != "merge" or value["mode"] != "ordinary-update"):
        raise PipelineError("unsupported ordinary base-sync ticket")
    for field in ("repository", "trusted_account"):
        if value[field] != config[field]:
            raise PipelineError("ordinary base-sync identity changed")
    if value["base_branch"] != config["base_branch"]:
        raise PipelineError("ordinary base-sync base branch changed")
    if type(value["number"]) is not int or value["number"] < 1:
        raise PipelineError("invalid ordinary base-sync target")
    for field, size in (("head", 40), ("base", 40), ("snapshot", 64),
                        ("config_sha256", 64), ("nonce", 32)):
        if not isinstance(value[field], str) or not re.fullmatch("[0-9a-f]{"+str(size)+"}", value[field]):
            raise PipelineError("invalid ordinary base-sync "+field)
    if (not isinstance(value["proof"], dict) or value["proof"].get("outcome") != "reviewed"
            or any(type(value[k]) is not int for k in ("issued_at", "expires_at"))
            or value["expires_at"]-value["issued_at"] != TTL):
        raise PipelineError("invalid ordinary base-sync proof/lifetime")


def _live_time(value):
    now = time.time()
    if value["issued_at"] > now+30 or value["expires_at"] <= now:
        raise PipelineError("ordinary base-sync ticket expired")


def _key(value):
    return _pp().digest({k: value[k] for k in ("repository", "number", "head")})


def _directory():
    root = Path(os.environ.get("PP_DATA_DIR", Path.home()/".promptpilot"))
    signing = Path(os.environ.get("PP_PIPELINE_LEASE_KEY_FILE", root/"pipeline-lease.key"))
    if not signing.is_absolute():
        raise PipelineError("ordinary base-sync requires an absolute private signing-key path")
    return signing.parent/"base-sync-updates-v1"


@contextmanager
def _store():
    """Never replace a missing established receipt file or a mismatched marker."""
    directory = _directory()
    directory.mkdir(parents=True, exist_ok=True)
    path, marker = directory/"receipts.sqlite3", directory/"initialized.json"
    if path.is_symlink() or marker.is_symlink() or marker.exists() and not path.is_file():
        raise PipelineError("ordinary base-sync receipt store is missing or unsafe")
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        if not marker.exists():
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "receipts" in tables and connection.execute("SELECT 1 FROM receipts LIMIT 1").fetchone():
                raise PipelineError("ordinary base-sync initialization marker was lost")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), uuid TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS receipts ("
                               "nonce TEXT PRIMARY KEY, target TEXT NOT NULL, ticket TEXT NOT NULL, "
                               "state TEXT NOT NULL CHECK(state IN ('ATTEMPTED','DONE','REFUSED')), result TEXT)")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_target ON receipts(target) "
                               "WHERE state IN ('ATTEMPTED','DONE')")
            connection.execute("INSERT OR IGNORE INTO metadata VALUES (1,?)", (secrets.token_hex(16),))
            identity = connection.execute("SELECT uuid FROM metadata WHERE id=1").fetchone()[0]
            connection.commit()
            try:
                with marker.open("x", encoding="utf-8") as output:
                    json.dump({"protocol": PROTOCOL, "uuid": identity}, output)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                pass
            connection.execute("BEGIN IMMEDIATE")
        expected = json.loads(marker.read_text(encoding="utf-8"))
        actual = connection.execute("SELECT uuid FROM metadata WHERE id=1").fetchone()
        if (actual is None or not isinstance(actual[0], str) or not re.fullmatch("[0-9a-f]{32}", actual[0])
                or expected != {"protocol": PROTOCOL, "uuid": actual[0]}):
            raise PipelineError("ordinary base-sync store identity changed")
        connection.rollback()
        yield connection
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise PipelineError("ordinary base-sync receipt store unavailable: "+str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def _active(connection, value):
    return connection.execute("SELECT * FROM receipts WHERE target=? AND state IN ('ATTEMPTED','DONE')",
                              (_key(value),)).fetchone()


def _fresh(gh, config, value, config_path):
    pp = _pp()
    from .fallback_handoff import validate_health
    _wire(value, config)
    _live_time(value)
    if not _enabled(config) or pp._configured_replica_count() != 1:
        raise PipelineError("ordinary base-sync requires both opt-ins and one MERGE lane")
    if pp.digest(config) != value["config_sha256"]:
        raise PipelineError("ordinary base-sync configuration changed")
    health = pp.run_health(config, config_path=config_path)
    validate_health(health)
    if (health.get("state") == "red" or health.get("integration_owner")
            or any(f.get("code") == "single_flight_barrier" for f in health.get("findings", []))):
        raise PipelineError("ordinary base-sync is blocked by current health/owner")
    candidates = health.get("merge_executable") or []
    if (not candidates or (candidates[0].get("number"), candidates[0].get("head"), candidates[0].get("stage"))
            != (value["number"], value["head"], "merge")
            or value["number"] in pp.admission_exclusions()):
        raise PipelineError("ordinary base-sync target left the first MERGE slot")
    status = gh.json("pr", "view", str(value["number"]), "--repo", config["repository"], "--json",
                     "headRefOid,mergeStateStatus,mergeable,statusCheckRollup,body")
    ready, reason = pp.checks_ready(config, status.get("statusCheckRollup") or [])
    if (status.get("headRefOid") != value["head"] or status.get("mergeStateStatus") != "BEHIND"
            or status.get("mergeable") != "MERGEABLE" or not ready):
        raise PipelineError("ordinary base-sync live HEAD/state/CI changed: "+reason)
    pp.ensure_identity(gh, config)
    snapshot = pp.stable_timeline(gh, config, value["number"])
    pp.validate_common(snapshot, config, value)
    info = pp.epoch(snapshot, config["trusted_account"])
    pp.validate_epoch_safety(info, config["trusted_account"])
    if (pp.digest(snapshot) != value["snapshot"] or snapshot["baseRefOid"] != value["base"]
            or pp.proof(info, value["head"], config["trusted_account"]) != value["proof"]
            or not pp.trusted_ship_authorized(info, config["trusted_account"])):
        raise PipelineError("ordinary base-sync proof/ship/epoch changed")
    if (pp.digest(config) != value["config_sha256"]
            or config_path is not None and pp.digest(pp.load_config(config_path)) != value["config_sha256"]):
        raise PipelineError("ordinary base-sync configuration changed during admission")
    _live_time(value)


def offer(gh, config, health, target, snapshot, proof, *, config_path=None):
    """Called only for an ordinary BEHIND target; OFF never enters this module."""
    pp = _pp()
    from . import base_sync_carry
    try:
        base_sync_carry.ensure_tree_proof_possible(config)
    except (PipelineError, base_sync_carry._Refused) as exc:
        return pp.fallback_target(config, health, "merge", target, str(exc))
    now = int(time.time())
    value = dict(version=1, protocol=PROTOCOL, purpose=PROTOCOL, stage="merge", mode="ordinary-update",
                 repository=config["repository"], trusted_account=config["trusted_account"],
                 number=target["number"], head=target["head"], base=snapshot["baseRefOid"],
                 base_branch=config["base_branch"], snapshot=pp.digest(snapshot), proof=proof,
                 config_sha256=pp.digest(config), nonce=secrets.token_hex(16), issued_at=now, expires_at=now+TTL)
    with _store() as connection:
        previous = _active(connection, value)
        if previous:
            return {"action": "wait", "number": value["number"],
                    "reason": "a branch update was already attempted; observe its HEAD or resolve the uncertainty"}
    _fresh(gh, config, value, config_path)
    return {"action": "merge", "target": target, "lease": pp.encode_signed_lease(value),
            "complete": "complete merge with this signed ordinary base-sync ticket"}


def _readback(gh, config, value):
    pp = _pp()
    from . import base_sync_carry
    number = value["number"]
    pr = gh.json("api", f"repos/{config['repository']}/pulls/{number}")
    head = (pr.get("head") or {}).get("sha")
    if head == value["head"]:
        return None
    if not isinstance(head, str) or not re.fullmatch("[0-9a-f]{40}", head):
        raise PipelineError("branch update readback has no exact HEAD")
    parents = gh.json("api", f"repos/{config['repository']}/commits/{head}").get("parents") or []
    parents = [x.get("sha") for x in parents]
    if (len(parents) != 2 or parents[0] != value["head"]
            or not all(isinstance(x, str) and re.fullmatch("[0-9a-f]{40}", x) for x in parents)):
        raise PipelineError("branch update readback is not the expected two-parent transition")
    snapshot = pp.stable_timeline(gh, config, number)
    if snapshot["headRefOid"] != head or snapshot["baseRefName"] != value["base_branch"]:
        raise PipelineError("branch update target changed during readback")
    try:
        base_sync_carry.git_facts(config, number, value["head"], parents[1], head, snapshot["baseRefOid"])
    except base_sync_carry._Refused as exc:
        raise PipelineError("branch update is not mechanically proven: "+str(exc)) from exc
    final = pp.stable_timeline(gh, config, number)
    if final["headRefOid"] != head or final["baseRefName"] != value["base_branch"]:
        raise PipelineError("branch update moved while proving its tree")
    return dict(action="updated", stage="merge", number=number, old_head=value["head"], new_head=head,
                next=NEXT)


def complete(gh, config, token, *, config_path=None):
    pp = _pp()
    value = pp.decode_signed_lease(token)
    _wire(value, config)
    pp.ensure_identity(gh, config)
    timeout = config.get("base_sync_update_timeout_seconds", 30)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise PipelineError("invalid base-sync update readback timeout")
    timeout = min(timeout, 30)
    send = False
    with _store() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _active(connection, value)
        refused = connection.execute("SELECT state FROM receipts WHERE nonce=?", (value["nonce"],)).fetchone()
        if row:
            if row["ticket"] != token:
                raise PipelineError("another ticket owns this branch update")
            if row["state"] == "DONE":
                receipt = pp.decode_signed_lease(row["result"])
                if (set(receipt) != {"version", "protocol", "nonce", "ticket_sha256", "result"}
                        or receipt["protocol"] != PROTOCOL+"-readback"
                        or receipt["nonce"] != value["nonce"] or receipt["ticket_sha256"] != pp.digest(value)):
                    raise PipelineError("ordinary base-sync readback receipt does not match its ticket")
                return receipt["result"]
            connection.rollback()
        else:
            if refused:
                raise PipelineError("this branch update ticket was refused; select a fresh ticket")
            # The remote/config/expiry checks happen after entering the writer.
            _fresh(gh, config, value, config_path)
            connection.execute("INSERT INTO receipts VALUES (?,?,?,'ATTEMPTED',NULL)",
                               (value["nonce"], _key(value), token))
            connection.commit()
            send = True
        if send:
            try:
                _fresh(gh, config, value, config_path)
            except BaseException:
                connection.execute("UPDATE receipts SET state='REFUSED' WHERE nonce=? AND state='ATTEMPTED'",
                                   (value["nonce"],))
                raise
            # There is exactly one mutator entry. Any exception or missing
            # response after this boundary retains ATTEMPTED, never permits retry.
            gh.json("api", f"repos/{config['repository']}/pulls/{value['number']}/update-branch",
                    "--method", "PUT", "--input", "-", input_value={"expected_head_sha": value["head"]})
        deadline = time.monotonic()+timeout
        while True:
            result = _readback(gh, config, value)
            if result is not None:
                receipt = dict(version=1, protocol=PROTOCOL+"-readback", nonce=value["nonce"],
                               ticket_sha256=pp.digest(value), result=result)
                connection.execute("UPDATE receipts SET state='DONE',result=? WHERE nonce=? AND state='ATTEMPTED'",
                                   (pp.encode_signed_lease(receipt), value["nonce"]))
                return result
            if time.monotonic() >= deadline:
                return {"action": "wait", "number": value["number"],
                        "reason": "branch update outcome unconfirmed; no second update is authorized"}
            time.sleep(1)
