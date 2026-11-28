import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tools import onebase_fix_lint_source as lint
from tools import onebase_fix_prepush as pre


def test_scoped_install_upgrade_and_rollback_leave_neighbouring_grants(
    tmp_path, monkeypatch
):
    def file(name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    head = "a" * 40
    entries = {
        "scope": file(
            "scope.json",
            json.dumps(
                {"pr": 2277, "source_head": head, "owner_comment": {"id": 21}}
            ).encode(),
        )
    }
    for key, (name, _) in lint.ABI.items():
        entries[key] = file(name, key.encode())
        monkeypatch.setitem(lint.ABI, key, (name, entries[key][1]))

    def binding(key):
        path, sha = entries[key]
        return f"{path} SHA256 {sha}"

    original = (
        f"#2277 at {head} uses OWNER 21, exact scope {binding('scope')}, "
        f"ordinary FIX guard {binding('claim')}, and offline readiness probe. "
        f"Use a single standard passive wait through {binding('window')} with pinned Python. "
        f"For this PR use the accepted canonical pre-push reader {binding('reader')}."
    )
    other = f"#2279 uses the same observer {binding('window')}."
    prompt = "\n".join([other, original, other])
    enrollment = lint.discover_scope(prompt)
    conf = tmp_path / "config.json"

    def install(text, number):
        path, sha = file(
            conf.name,
            json.dumps(
                {
                    "version": number,
                    "python": "python3",
                    "lint_source_scope": enrollment,
                }
            ).encode(),
        )
        old, new = lint.scoped_bindings(text, path, sha)[0]
        return text.replace(old, new), old, new

    installed, a, b = install(prompt, 2)
    assert a == original and installed.splitlines()[::2] == [other, other]
    assert (
        "recover" in b
        and "onebase_fix_cache_observe.py" in b
        and "strict publication" in b
    )
    assert lint.discover_scope(installed) == enrollment
    assert lint.scoped_bindings(installed, conf, lint.digest(conf.read_bytes())) == []
    upgraded, c, d = install(installed, 3)
    assert upgraded.replace(d, c).replace(b, a) == prompt
    assert lint.discover_scope(other) is None
    with pytest.raises(ValueError):
        lint.discover_scope(prompt + "\n" + original)
    entries["claim"][0].write_bytes(b"changed")
    with pytest.raises(ValueError, match="binding changed"):
        lint.discover_scope(prompt)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(lint.time, "time", lambda: 1000)
    task, started, native = ident = (17, "1970-01-01T00:15:00+00:00", "native")
    head = "a" * 40
    profile = "d" * 64
    token = dict(profile_hash=profile, generated_at=750, epoch=5, revision=7)
    cache = dict(
        profile_hash=profile,
        generated_at=750,
        token=token,
        ttl_seconds=300,
        source="live",
        available=True,
        complete=True,
        stale=False,
        invalidated=False,
    )
    original = dict(
        state="refreshed",
        source_refreshed=True,
        task_id=task,
        task_started_at=started,
        profile_id="onebase",
        queue_id="fix",
        native_session_bound=True,
        existing_skill_reservation_reused=True,
        admitted_route="skill",
        product_mutation_authorized=False,
        scope="Native-source-observation-only",
        snapshot_token=token,
        cache=cache,
    )
    budget = dict(budget_route="insights", state="priority_waiter", allowed=False)
    live = dict(
        profile_id="onebase",
        repository="ivanarama/onebase",
        cache={
            **cache,
            "source": "memory",
            "refresh_blocked": "priority_waiter",
            "refresh_budget": budget,
        },
        diagnostics={
            "exit_code": 0,
            "fix_candidates": [dict(stage="review", number=2277, head=head)],
        },
        queues=[
            dict(
                id="fix",
                membership_complete=True,
                admission_items=[
                    dict(
                        kind="pr",
                        number=2277,
                        key="pr:2277",
                        url="https://github.com/ivanarama/onebase/pull/2277",
                        labels=["changes-requested"],
                    )
                ],
            )
        ],
    )
    selected = dict(
        number=2277,
        head=head,
        decision_id="21",
        decision_sha256="abc",
        epoch="test",
        proof={"decision": "ok"},
        task_id=task,
        task_started_at=started,
        snapshot_token=token,
    )
    root = tmp_path / "fix-runs"
    root.mkdir()
    source = root / "source.json"
    source.write_bytes(pre.encode(original))
    election = root / "selection.json"
    election.write_bytes(pre.encode(selected))
    args = NS(
        pr=2277,
        source=head,
        selection=election,
        source_receipt=source,
        passive_source=False,
    )
    install = NS(
        root=root,
        prepush=pre,
        config={"lint_source_scope": {"owner_id": "21"}},
        identity=Mock(return_value=ident),
        verify_worker=Mock(),
        check_target=Mock(),
        current_report=Mock(return_value=live),
        priority_read=Mock(
            side_effect=lambda report, orig: (
                pre.priority_scan_deferred(report["cache"])
                and orig == report["cache"]["token"]
            )
        ),
    )
    claim = NS(
        HEAD=head,
        BODY="abc",
        EPOCH="test",
        PROOF=selected["proof"],
        BASE=tmp_path,
        SOURCE_WINDOW_SHA=lint.ABI["window"][1],
        load=Mock(),
    )
    window = NS(budget=Mock(), active_target=Mock())
    reader = NS(
        selected=lambda e, n, h: e, same_or_new=Mock(), passive_source_token=Mock()
    )
    modules = dict(claim=claim, window=window, reader=reader)
    checkpoint = lint.LintCheckpoint(install, modules, args)
    return NS(
        check=checkpoint,
        install=install,
        modules=modules,
        args=args,
        live=live,
        token=token,
        selection=selected,
    )


def local_read(c):
    return c.check.check(
        c.check.reader.__dict__,
        c.args.source_receipt,
        c.selection,
        2277,
        c.args.source,
        *c.check.identity[:2],
    )


def test_scope_checks_local_copy_without_changing_receipt(case):
    c = case
    before = copy.deepcopy(c.live)
    assert local_read(c) == c.token
    c.install.priority_read.assert_called_once_with(c.live, c.token)
    c.modules["window"].budget.assert_called_once()
    c.modules["window"].active_target.assert_called_once()
    c.install.check_target.assert_called_once()
    assert c.check.used_deferred and c.live == before


@pytest.mark.parametrize(
    "bad", ["fairness", "expired", "new-head", "changed-publication", "worker-changed"]
)
def test_unrelated_denials_stay_closed(case, bad):
    c = case
    if bad == "fairness":
        c.live["cache"]["refresh_blocked"] = "fairness_waiter"
    elif bad == "expired":
        c.live["cache"]["stale"] = True
    elif bad == "new-head":
        c.live["diagnostics"]["fix_candidates"][0]["head"] = "b" * 40
    elif bad == "changed-publication":
        c.live["cache"]["token"] = {**c.token, "revision": 8}
    else:
        c.install.identity.return_value = (99, *c.check.identity[1:])
    with pytest.raises(ValueError):
        local_read(c)
    c.modules["window"].budget.assert_not_called()


def test_sealed_run_uses_the_same_checked_window_inside_its_lock(case):
    c = case
    claim = c.modules["claim"]
    saved_load = claim.load
    attempts = []

    def invoke(args):
        def checked():
            view = claim.load(claim.BASE / "source_window.py", claim.SOURCE_WINDOW_SHA)
            return view["check"](
                c.check.reader.__dict__,
                args.source_receipt,
                c.selection,
                2277,
                args.source,
                *c.check.identity[:2],
            )

        checked()
        claim.load(claim.BASE / "independent-runner.py", "pinned")
        c.live["cache"]["stale"] = True
        checked()
        attempts.append(True)

    claim.invoke = invoke
    with pytest.raises(ValueError, match="unavailable or stale"):
        c.check.run(c.args)
    assert attempts == []
    saved_load.assert_called_once_with(claim.BASE / "independent-runner.py", "pinned")


def test_priority_read_cannot_issue_prepush_ticket(case):
    c = case
    reader = c.modules["reader"]
    issued = reader.same_or_new

    def read(*_):
        reader.current_fix_token(2277, c.args.source)
        return reader.same_or_new(Path("ticket"), {})

    reader.read = read
    with pytest.raises(ValueError, match="ticket for publication"):
        c.check.recovery(c.args)
    issued.assert_not_called()


def test_issuance_deferral_memory_prevents_ticket_even_after_live_status_clears(case):
    c = case
    receipt = dict(
        c.check.prior,
        state="reused",
        source_reused=True,
        source_refreshed=False,
        cache={**c.check.prior["cache"], "source": "durable"},
        observed_at=950,
        existing_execution_reservation_reused=True,
        mutable_admission_granted=False,
        attempt_binding_sha256="b" * 64,
        fix_pr_heads={"2277": c.args.source},
        deferred_refresh={
            "refresh_blocked": "priority_waiter",
            "refresh_budget": {
                "state": "priority_waiter",
                "budget_route": "insights",
                "allowed": False,
            },
        },
    )
    raw = pre.encode(receipt)
    c.args.source_receipt.write_bytes(raw)
    c.install.checked_origin = Mock(return_value={"source_sha256": lint.digest(raw)})
    c.live["cache"].pop("refresh_blocked")
    c.live["cache"].pop("refresh_budget")
    checkpoint = lint.LintCheckpoint(c.install, c.modules, c.args)
    assert checkpoint.used_deferred
    assert checkpoint.current_fix_token(2277, c.args.source) == c.token
    c.install.priority_read.assert_not_called()
    c.install.checked_origin.assert_called_once_with(receipt, checkpoint.identity)
    reader = c.modules["reader"]
    issued = reader.same_or_new
    reader.read = lambda *_: reader.same_or_new(Path("ticket"), {})
    with pytest.raises(ValueError, match="ticket for publication"):
        checkpoint.recovery(c.args)
    issued.assert_not_called()
