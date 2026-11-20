"""Only a collision during the elected MERGE's own live scan lease can unblock its cached proof."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import merge_checkpoint, merge_confirmation, pipeline_insights as pi
from tests.test_bounded_merge_finish import native_recovery as native_recovery
from tests.test_merge_checkpoint import dispatch, HEAD
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import (
    continuation as continuation,
    targeted_merge as targeted_merge,
)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign",
        "expired-lease",
        "replaced-lease",
        "invalidated",
        "old-publication",
        "expired-source",
        "wrong-route",
        "checker-failed",
        "incomplete",
        "wrong-candidate",
    ],
)
def test_signed_recovery_checks_collision_and_actual_publication(
    native_recovery, monkeypatch, fault
):
    f, snapshot, _ = native_recovery
    profile = f.profile
    scope = pi._GITHUB_SCAN_LEASE_SCOPE
    profile_id = "example"
    fingerprint = pi._profile_fingerprint(profile)
    stamp = time.time() - (
        pi._CACHE_TTL_SECONDS + 1 if fault == "expired-source" else 5
    )
    token = {
        "epoch": 0,
        "revision": 1,
        "generated_at": stamp,
        "profile_hash": fingerprint,
    }
    snapshot["cache"].update(
        source="durable", token=token, generated_at=stamp, invalidated=False
    )
    f.db.set_setting(pi._published_revision_key(profile_id, fingerprint), "1")
    f.db.set_setting(pi._cache_key(profile_id, fingerprint), json.dumps(token))
    f.db.set_setting(pi._CACHE_EPOCH_KEY, "0")

    def report_collision():
        denied = f.db.acquire_pipeline_scan_lease(scope, "competing-sampler", 120)
        assert not denied["acquired"] and denied["state"] == "busy"
        until = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        decision = {
            "allowed": False,
            "state": "scan_in_progress",
            "lease_scope": scope,
            "reason": "sampler found the account scan lease occupied",
            "defer_until": until,
            "budget_route": "unclassified" if fault == "wrong-route" else "insights",
        }
        assert f.db.publish_pipeline_refresh_status(
            profile_id,
            profile["repository"],
            pi._refresh_blocked_status(decision),
            revision=denied["status_revision"],
        )

    if fault == "foreign":
        assert f.db.acquire_pipeline_scan_lease(scope, "old-owner", 120)["acquired"]
        report_collision()
        assert f.db.release_pipeline_scan_lease(scope, "old-owner")

    signed_next = pi._tool_preflight

    def next_merge(*args):
        result = signed_next(*args)
        if fault != "foreign":
            report_collision()
        if fault in ("expired-lease", "replaced-lease"):
            key = f.db._pipeline_scan_lease_key(scope)
            value = json.loads(f.db.get_setting(key))
            if fault == "replaced-lease":
                value["token"] = "other-owner"
            else:
                value["expires_at"] = time.time() - 1
            f.db.set_setting(key, json.dumps(value))
        elif fault == "invalidated":
            f.db.set_setting(pi._CACHE_EPOCH_KEY, "1")
        elif fault == "old-publication":
            f.db.set_setting(pi._published_revision_key(profile_id, fingerprint), "2")
        elif fault == "checker-failed":
            key = f.db._pipeline_refresh_status_key(profile_id)
            event = json.loads(f.db.get_setting(key))
            event["source_checker_failure_revisions"] = {fingerprint: event["revision"]}
            f.db.set_setting(key, json.dumps(event))
        elif fault == "incomplete":
            snapshot["queues"][0]["membership_complete"] = False
        elif fault == "wrong-candidate":
            snapshot["diagnostics"]["integration_owner"]["base_sync_candidate"][
                "from"
            ] = "f" * 40
        return result

    monkeypatch.setattr(pi, "_tool_preflight", next_merge)
    monkeypatch.setattr(
        pi,
        "read_cached",
        lambda *_: pi._with_persisted_refresh_status(snapshot, profile_id, profile),
    )
    reached = []

    def native(*args, **kw):
        reached.append(args[-1]["to"])
        return {"action": "complete_empty"}

    monkeypatch.setattr(merge_confirmation, "execute", native)
    result = dispatch(f)
    assert reached == ([] if fault else [HEAD])
    assert not merge_checkpoint.blocks_fallback(
        profile["repository"], {"number": 42, "head": HEAD}
    )
    assert result["action"] == ("defer" if fault else "complete_empty")
    assert "refresh_blocked" not in snapshot["cache"]
