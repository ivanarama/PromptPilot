"""Task-bound, costed Native source refresh inside an existing admitted session.

Only this command imports Native DB services. Project-only pipelinectl commands
remain DB-free. No provider, task, skill reservation or product grant is created.
"""
import argparse
from datetime import timezone
import hashlib
import json
import os
import sqlite3
import sys

from .task_identity import task_execution_fingerprint


def _profile_digest(profiles):
    return hashlib.sha256(json.dumps(profiles, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class _Continuation:
    def __init__(self, db, insights, profiles, profile_id, queue_id, task_id, marker,
                 expected_attempt):
        self.db, self.insights = db, insights
        self.profile_id, self.queue_id, self.task_id = profile_id, queue_id, task_id
        self.profiles_sha256 = _profile_digest(profiles)
        self.policy = insights._github_budget_policy(profiles[profile_id])
        if self.policy is None or self.policy.get("costs") is None:
            raise ValueError("source refresh requires a costed Native profile")
        self.policy = insights._with_shared_budget_floor(self.policy)
        self.marker = marker
        self.expected_attempt = expected_attempt
        self.guard = None
        self.published = None
        self.ledger()

    def ledger(self, policy=None):
        if _profile_digest(self.insights._profiles()) != self.profiles_sha256:
            raise ValueError("source refresh profile policy changed")
        result = self.db.pipeline_source_refresh_state(self.policy["lease_scope"],
            self.task_id, self.profile_id, self.queue_id, self.policy["costs"],
            starvation_timeout_seconds=(policy or self.policy).get("starvation_timeout_seconds", 0))
        observed = result.pop("guard")
        if any(observed[key] != value for key, value in self.expected_attempt.items()):
            raise ValueError("source refresh claimed attempt or execution inputs changed")
        if self.marker is not None and self.marker != observed["started_at"]:
            raise ValueError("worker start marker differs from the running attempt")
        if self.guard is None:
            self.guard = observed
        elif self.guard != observed:
            raise ValueError("source refresh Native attempt or admission changed")
        return result

    def check(self):
        try:
            self.ledger()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise self.insights._GitHubScanLeaseLost(
                "Native source refresh attempt fence lost") from exc


def refresh(profile_id, environ=None):
    env = os.environ if environ is None else environ
    raw_id = env.get("PP_TASK_ID", "")
    if (not isinstance(raw_id, str) or not raw_id.isascii() or not raw_id.isdecimal()
            or len(raw_id) > 19 or not 0 < int(raw_id) < 2**63):
        raise ValueError("source refresh requires worker-issued PP_TASK_ID")
    marker = env.get("PP_TASK_STARTED_AT")
    if any(env.get(k) for k in ("PP_PIPELINE_REPLICAS", "PP_PIPELINE_TARGET_TOKEN",
                                "PP_PROVIDER_OWNERSHIP_KIND")) and not marker:
        raise ValueError("replicated Native source refresh requires its start marker")
    # Ordinary workers before this tool exported only PP_TASK_ID. Resolve their
    # exact current start/session/admission from the existing Native ledger.
    from . import db, pipeline_insights as pi

    task = db.get_task(int(raw_id))
    if task is None or task.status.value != "running":
        raise ValueError("source refresh requires an already running Native task")
    matched = pi._matching_queue(task)
    if matched is None or matched[0] != profile_id:
        raise ValueError("source refresh profile does not own this Native task")
    profiles = pi._profiles()
    expected_attempt = {key: getattr(task, key) for key in (
        "session_id", "herdr_pane", "series_id", "priority")}
    expected_attempt["started_at"] = (task.started_at.astimezone(timezone.utc).isoformat()
                                      if task.started_at is not None else None)
    expected_attempt["execution_sha256"] = task_execution_fingerprint(task)
    continuation = _Continuation(db, pi, profiles, profile_id, str(matched[2]["id"]),
                                 task.id, marker, expected_attempt)
    previous = getattr(pi._scan_lease_context, "continuation", None)
    if previous is not None:
        raise ValueError("nested Native source refresh is forbidden")
    pi._scan_lease_context.continuation = continuation
    try:
        # INSIGHTS pays its own configured scan estimate against the normal
        # scan floor at the actual task priority. Its existing execution promise
        # is counted once by the read-only ledger; it is never reserved again.
        with pi._github_scan_admission(profiles[profile_id],
                f"Native source continuation {profile_id}/{matched[2]['id']}",
                profile_id=profile_id, budget_route="insights", task=task) as admission:
            if not admission.get("allowed"):
                return {"state": "wait", "source_refreshed": False,
                        "admitted_route": continuation.guard["route"],
                        "existing_execution_reservation_reused": True,
                        "budget": pi._public_budget_decision(admission),
                        "mutable_admission_granted": False}
            try:
                continuation.check()
                result = pi._analyze_without_budget(profile_id, db.list_series(),
                    use_cache=False, refresh_diagnostics=True)
                pi._ensure_github_scan_lease(renew=True)
            except pi._GitHubSearchBudgetDenied as exc:
                return {"state": "refused" if exc.decision.get("state") == "invalid_config" else "wait",
                        "source_refreshed": False, "reason": exc.decision["state"],
                        "budget": pi._public_budget_decision(exc.decision),
                        "mutable_admission_granted": False}
            except pi._GitHubRateLimitUnavailable as exc:
                denied = pi._replace_admission_with_rate_limit_failure(
                    admission, profiles[profile_id], exc)
                return {"state": "wait", "source_refreshed": False,
                        "reason": denied["state"], "budget": pi._public_budget_decision(denied),
                        "mutable_admission_granted": False}
            except pi._GitHubScanLeaseFailure:
                return {"state": "refused", "source_refreshed": False,
                        "reason": "Native attempt or scan publication fence lost",
                        "mutable_admission_granted": False}
        cache = result.get("cache") or {}
        diagnostics = result.get("diagnostics") or {}
        complete = (continuation.published is not None and cache.get("source") == "live"
                    and cache.get("complete") is True and cache.get("stale") is False
                    and not cache.get("invalidated") and not cache.get("refresh_blocked")
                    and all(q.get("membership_complete") is True for q in result.get("queues", []))
                    and (not profiles[profile_id].get("health_check")
                         or isinstance(result.get("diagnostics"), dict))
                    and not diagnostics.get("checker_failed"))
        checker_failed = bool(diagnostics.get("checker_failed"))
        return {"state": "refreshed" if complete else "refused" if checker_failed else "wait",
                "reason": (None if complete else "checker_failed" if checker_failed
                           else "complete source snapshot was not published"),
                "source_refreshed": complete,
                "task_id": task.id, "task_started_at": continuation.guard["started_at"],
                "native_session_bound": True, "existing_execution_reservation_reused": True,
                "admitted_route": continuation.guard["route"],
                "existing_skill_reservation_reused": continuation.guard["route"] == "skill",
                "profile_id": profile_id, "queue_id": matched[2]["id"],
                "budget": pi._public_budget_decision(admission), "cache": cache,
                "source_search_usage": result.get("source_search_usage"),
                "snapshot_token": continuation.published if complete else None,
                "scope": "Native-source-observation-only", "mutable_admission_granted": False,
                "product_mutation_authorized": False}
    finally:
        pi._scan_lease_context.continuation = previous


def run(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(prog="pipelinectl source-refresh", description=__doc__)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    try:
        result = refresh(args.profile)
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, RuntimeError) as exc:
        result = {"state": "refused", "source_refreshed": False, "reason": str(exc),
                  "mutable_admission_granted": False}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("source_refreshed") is True else 3 if result["state"] == "wait" else 2


if __name__ == "__main__":
    sys.exit(run())
