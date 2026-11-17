"""Real Native Source publication, fake physical GitHub/checker boundaries."""
import copy
import hashlib
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as pi, pipeline_source_refresh as source
from promptpilot.source_graphql_cost import PROTOCOL
from tests.test_github_budget import _limits
from tests.test_pipeline_graphql_search import REAL_SEARCH, gh_transport, observed_rates, page, stable_rates
from tests.test_pipeline_source_refresh import continuation as continuation, ledger

RUN_HEALTH = pi._run_profile_health_check
REAL_RATES = pi._github_rate_limits


def source_transport(monkeypatch, responses, rates=None):
    """Real metadata parser and durable raw-rate write, only physical gh fake."""
    calls = gh_transport(monkeypatch, responses)
    transport = pi.subprocess.run
    current = None
    fixed_limits = _limits()
    calls.observations = []
    def run(command, **kwargs):
        nonlocal current
        if command == ["gh", "api", "rate_limit"]:
            current = rates() if rates else copy.deepcopy(fixed_limits)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"resources": current}), stderr="")
        payload = json.loads(kwargs.get("input") or "{}")
        if payload.get("query") == pi._GITHUB_RATE_LIMIT_QUERY:
            calls.observations.append(copy.deepcopy(current))
            raw = current["graphql"]
            return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"viewer": {"login": "owner"},
                "rateLimit": {"limit": raw["limit"], "used": raw["used"], "remaining": raw["remaining"],
                    "resetAt": raw["reset_at"], "cost": 1}}}), stderr="")
        return transport(command, **kwargs)
    monkeypatch.setattr(pi.subprocess, "run", run)
    monkeypatch.setattr(pi, "_github_rate_limits", REAL_RATES)
    return calls


def metered_health(f, tmp_path, monkeypatch, *, fault=None):
    executable = tmp_path / "immutable-checker"
    executable.write_bytes(b"fake physical checker: never executed")
    contract = tmp_path / "procedure.md"
    contract.write_bytes(b"full accepted checker contract")
    declaration = {"protocol": PROTOCOL,
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest()}
    f.profile["priority_control"] = {"trusted_account": "owner"}
    f.profile["health_check"] = {"command": [str(executable.resolve()), "-contract", str(contract.resolve())],
        "source_graphql_cost": declaration, "cache_seconds": 1800}
    calls = []
    transport = pi.subprocess.run
    def run(command, **kwargs):
        if command[0] != str(executable.resolve()):
            return transport(command, **kwargs)
        calls.append(command)
        allowance = int(command[command.index("-source-graphql-budget") + 1])
        nonce = command[command.index("-source-graphql-nonce") + 1]
        usage = dict(protocol=PROTOCOL, nonce=nonce, allowance=allowance,
            charged_upper_bound=4, observed_actual_cost=4, physical_attempts=2,
            preview_attempts=1, data_attempts=1, unproved_attempts=0,
            uncertain_cost=False, complete=True, repo="owner/example", trusted_account="owner",
            contract_sha256=declaration["contract_sha256"])
        if fault == "missing": usage = None
        elif fault == "nonce": usage["nonce"] = "f" * 32
        elif fault == "partial": usage["complete"] = False
        elif fault == "uncertain": usage.update(uncertain_cost=True, unproved_attempts=1)
        elif fault == "cap": usage.update(charged_upper_bound=allowance+1, observed_actual_cost=allowance+1)
        elif fault == "zero": usage.update(charged_upper_bound=0, observed_actual_cost=0,
            physical_attempts=0, preview_attempts=0, data_attempts=0)
        elif fault == "artifact": executable.write_bytes(b"changed while executing")
        elif fault == "contract": contract.write_bytes(b"changed while executing")
        elif fault == "timeout": raise subprocess.TimeoutExpired(command, 1)
        if fault and fault.startswith("75"):
            usage.update(complete=False, reason="allowance_exhausted",
                charged_upper_bound=allowance, observed_actual_cost=allowance)
            if fault == "75-preview":
                usage.update(physical_attempts=1, data_attempts=0)
            elif fault == "75-nonce": usage["nonce"] = "f" * 32
            elif fault == "75-uncertain": usage.update(uncertain_cost=True, unproved_attempts=1)
            elif fault == "75-reason": usage["reason"] = "contract_changed"
            elif fault == "75-zero": usage.update(charged_upper_bound=0,observed_actual_cost=0,
                physical_attempts=0,preview_attempts=0,data_attempts=0)
            return SimpleNamespace(returncode=75,stdout=json.dumps({"source_graphql_usage":usage}),stderr="")
        if fault == "duplicate":
            return SimpleNamespace(returncode=0,stdout='{"state":"green","state":"red"}',stderr="")
        payload = {"state": "red" if fault == "red" else "green", "findings": [], "source_graphql_usage": usage}
        return SimpleNamespace(returncode=2 if fault == "exit" else 1 if fault == "red" else 0,
            stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(pi.subprocess, "run", run)
    monkeypatch.setattr(pi, "_run_profile_health_check", RUN_HEALTH)
    return calls


def test_reset_and_remaining_oscillation_does_not_replenish_or_poison_own_cost(continuation, monkeypatch):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    f.profile["queues"][0]["queries"] = ["is:pr", "is:issue"]
    observations = []; values = [4993, 4944, 4970, 4969, 4980, 4990]
    def rates():
        limits = _limits(graphql=values[len(observations)])
        limits = _limits(graphql=values[len(observations)], reset=limits["graphql"]["reset"] + (60 if len(observations) % 2 else 0))
        observations.append(copy.deepcopy(limits))
        return limits
    before = ledger(f); calls = source_transport(monkeypatch, [page([]), page([])], rates)
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is True and len(calls) == 2
    assert result["source_search_usage"]["conservative_graphql_spend"] == 10
    assert result["source_search_usage"]["successful_observation_cost_proved"] is True
    assert ledger(f) == before
    cached = pi.read_cached("example", f.db.list_series())
    assert cached["github_rate_limit"] == observations[-1]  # authenticated raw, not E


@pytest.mark.parametrize("fault", [None, "red"])
def test_full_checker_cost_is_in_same_whole_allowance_once(continuation, monkeypatch, tmp_path, fault):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    search_calls = source_transport(monkeypatch, [page([])])
    health_calls = metered_health(f, tmp_path, monkeypatch, fault=fault)
    before = ledger(f); result = source.refresh("example", f.env)
    assert result["source_refreshed"] is True and len(search_calls) == len(health_calls) == 1
    usage = result["source_search_usage"]
    assert usage["conservative_graphql_spend"] == 10 and usage["graphql_allowance"] == 100
    assert usage["health_cost_provenance"]["charged"] == 4
    assert usage["health_cost_provenance"]["reused"] is False and ledger(f) == before


@pytest.mark.parametrize("fault", ["missing", "nonce", "partial", "uncertain", "cap", "zero", "exit", "artifact", "contract", "timeout"])
def test_unproved_external_checker_never_publishes_or_searches(continuation, monkeypatch, tmp_path, fault):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    searches = source_transport(monkeypatch, [])
    calls = metered_health(f, tmp_path, monkeypatch, fault=fault)
    before = ledger(f); result = source.refresh("example", f.env)
    assert result["source_refreshed"] is False and len(calls) == 1 and not searches and not searches.previews
    assert f.db.list_pipeline_snapshots("example") == [] and ledger(f) == before


def test_unmetered_checker_is_not_a_zero_cost_proof(continuation, monkeypatch):
    f = continuation
    f.profile["health_check"] = {"command": ["legacy-checker"]}
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits()))
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [])
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is False and result["reason"] == "invalid_config"
    assert f.checks == []
    assert not calls and not calls.previews and f.db.list_pipeline_snapshots("example") == []


def test_health_cap_cannot_be_replenished_for_following_source(continuation, monkeypatch, tmp_path):
    f = continuation; stable_rates(monkeypatch)
    f.profile["github_budget"]["costs"]["insights"]["graphql"] = 8
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [])
    health = metered_health(f, tmp_path, monkeypatch)
    result = source.refresh("example", f.env)
    assert not result["source_refreshed"] and len(health) == 1
    assert not calls and len(calls.previews) == 1 and f.db.list_pipeline_snapshots("example") == []


def test_cached_health_keeps_old_nonce_provenance_without_recharging(continuation, monkeypatch, tmp_path):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [page([]), page([])])
    health = metered_health(f, tmp_path, monkeypatch)
    assert source.refresh("example", f.env)["source_refreshed"] is True
    old = pi.read_cached("example", f.db.list_series())["diagnostics"]
    # The independent sampler may reuse successful diagnostics after the task's
    # original execution reservation has ended; it obtains its own scan lease.
    assert f.db.release_pipeline_github_budget("github-default", token="existing-skill",
        task_id=f.task.id, task_started_at=f.task.started_at)
    result = pi.analyze("example", f.db.list_series(), use_cache=False, refresh_diagnostics=False)
    usage = result["source_search_usage"]
    assert len(health) == 1 and len(calls) == 2
    assert usage["conservative_graphql_spend"] == 6
    assert usage["health_cost_provenance"]["reused"] is True
    assert usage["health_cost_provenance"]["charged"] == 0
    assert result["diagnostics"]["source_graphql_usage"]["nonce"] == old["source_graphql_usage"]["nonce"]


def test_regional_increase_cannot_replenish_account_envelope_at_real_floor(continuation, monkeypatch):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    count = 0
    def rates():
        nonlocal count
        count += 1
        return _limits(graphql=1203 if count == 1 else 5000)
    calls = source_transport(monkeypatch, [page([])], rates)
    before = ledger(f); result = source.refresh("example", f.env)
    assert result["source_refreshed"] is False and len(calls) == 1
    assert f.db.get_pipeline_github_rate_snapshot("github-default")["limits"]["graphql"]["remaining"] == 5000
    assert pi.read_cached("example", f.db.list_series())["github_rate_limit"]["graphql"]["remaining"] == 5000
    assert result["budget"]["source_availability_envelope"]["graphql"] == 1199
    assert result["budget"]["reserved_other"]["graphql"] == 1000
    assert ledger(f) == before and f.db.list_pipeline_snapshots("example") == []


@pytest.mark.parametrize("cost", [None, 0, True, -1])
def test_initial_metadata_requires_actual_server_cost_not_a_one_point_guess(continuation, monkeypatch, cost):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [])
    transport = pi.subprocess.run
    def run(command, **kwargs):
        result = transport(command, **kwargs)
        payload = json.loads(kwargs.get("input") or "{}")
        if payload.get("query") == pi._GITHUB_RATE_LIMIT_QUERY:
            response = json.loads(result.stdout)
            response["data"]["rateLimit"]["cost"] = cost
            result.stdout = json.dumps(response)
        return result
    monkeypatch.setattr(pi.subprocess, "run", run)
    result = source.refresh("example", f.env)
    assert not result["source_refreshed"] and result["budget"]["source_cost_uncertain"]
    assert not calls and not calls.previews and f.db.list_pipeline_snapshots("example") == []


@pytest.mark.parametrize("failure", ["timeout", "technical-exit"])
def test_latest_real_checker_failure_remains_durable_over_last_good_cache(continuation, monkeypatch, tmp_path, failure):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [page([])])
    health = metered_health(f, tmp_path, monkeypatch)
    assert source.refresh("example", f.env)["source_refreshed"] is True
    before = f.db.list_pipeline_snapshots("example")
    transport = pi.subprocess.run
    executable = f.profile["health_check"]["command"][0]
    def timeout(command, **kwargs):
        if command[0] == executable:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            result = transport(command, **kwargs)
            result.returncode = 2
            return result
        return transport(command, **kwargs)
    monkeypatch.setattr(pi.subprocess, "run", timeout)
    result = source.refresh("example", f.env)
    assert result["state"] == "refused" and result["reason"] == "checker_failed"
    assert len(calls) == 1 and len(health) == (1 if failure == "timeout" else 2)
    assert f.db.list_pipeline_snapshots("example") == before
    cached = pi.read_cached("example", f.db.list_series())
    assert cached["cache"]["refresh_blocked"] == "checker_failed"
    assert pi._required_snapshot_state(cached, f.profile, "fix") is not None


@pytest.mark.parametrize("age", [-60, 1801, float("nan")])
def test_future_expired_or_nonfinite_health_age_runs_fresh_checker(continuation, monkeypatch, tmp_path, age):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [page([]), page([])])
    health = metered_health(f, tmp_path, monkeypatch)
    assert source.refresh("example", f.env)["source_refreshed"] is True
    pi._cache["example"][1]["diagnostics_generated_at"] = time.time() - age
    assert f.db.release_pipeline_github_budget("github-default", token="existing-skill",
        task_id=f.task.id, task_started_at=f.task.started_at)
    result = pi.analyze("example", f.db.list_series(), use_cache=False, refresh_diagnostics=False)
    assert len(health) == len(calls) == 2
    assert result["source_search_usage"]["health_cost_provenance"]["reused"] is False
    assert result["source_search_usage"]["conservative_graphql_spend"] == 10


@pytest.mark.parametrize("errors", [None, {}, False, 0, ""])
@pytest.mark.parametrize("at", [1, 2])
def test_only_clean_initial_or_later_metadata_can_prove_cost(continuation, monkeypatch, errors, at):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [])
    transport = pi.subprocess.run
    count = 0
    def run(command, **kwargs):
        nonlocal count
        result = transport(command, **kwargs)
        payload = json.loads(kwargs.get("input") or "{}")
        if payload.get("query") == pi._GITHUB_RATE_LIMIT_QUERY:
            count += 1
            if count == at:
                response = json.loads(result.stdout)
                response["errors"] = errors
                result.stdout = json.dumps(response)
        return result
    monkeypatch.setattr(pi.subprocess, "run", run)
    before = ledger(f); result = source.refresh("example", f.env)
    assert not result["source_refreshed"] and result["budget"]["source_cost_uncertain"]
    assert not calls and not calls.previews and ledger(f) == before
    assert f.db.list_pipeline_snapshots("example") == []


def test_explicit_empty_metadata_errors_is_a_clean_cost_receipt(continuation, monkeypatch):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [page([])])
    transport = pi.subprocess.run
    def run(command, **kwargs):
        result = transport(command, **kwargs)
        payload = json.loads(kwargs.get("input") or "{}")
        if payload.get("query") == pi._GITHUB_RATE_LIMIT_QUERY:
            response = json.loads(result.stdout)
            response["errors"] = []
            result.stdout = json.dumps(response)
        return result
    monkeypatch.setattr(pi.subprocess, "run", run)
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] and len(calls) == 1
    assert result["source_search_usage"]["conservative_graphql_spend"] == 6


@pytest.mark.parametrize("fault", ["75-pair", "75-preview"])
def test_only_authentic_clean_exit75_is_a_typed_budget_wait(continuation, monkeypatch, tmp_path, fault):
    f = continuation; stable_rates(monkeypatch)
    if fault == "75-preview":
        f.profile["github_budget"]["costs"]["insights"]["graphql"] = 2
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    calls = source_transport(monkeypatch, [])
    health = metered_health(f, tmp_path, monkeypatch, fault=fault)
    before = ledger(f); result = source.refresh("example", f.env)
    assert result["state"] == "wait" and result["reason"] == "budget_in_flight"
    assert result["budget"]["source_cost_uncertain"] is False
    assert not result["source_refreshed"] and len(health) == 1 and not calls and not calls.previews
    assert f.db.list_pipeline_snapshots("example") == [] and ledger(f) == before
    assert pi.read_cached("example",f.db.list_series())["cache"]["refresh_blocked"] == "budget_in_flight"


@pytest.mark.parametrize("fault", ["75-nonce", "75-uncertain", "75-reason", "75-zero", "duplicate"])
def test_untrusted_exit75_or_duplicate_cost_json_is_real_checker_failure(continuation, monkeypatch, tmp_path, fault):
    f = continuation; stable_rates(monkeypatch)
    monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    calls = source_transport(monkeypatch,[])
    metered_health(f,tmp_path,monkeypatch,fault=fault)
    result=source.refresh("example",f.env)
    assert result["state"]=="refused" and result["reason"]=="checker_failed"
    assert not calls and not calls.previews and f.db.list_pipeline_snapshots("example")==[]
    assert pi.read_cached("example",f.db.list_series())["cache"]["refresh_blocked"]=="checker_failed"


def test_modern_empty_queue_publication_still_meters_bootstrap_and_final_metadata(continuation,monkeypatch):
    f=continuation;stable_rates(monkeypatch)
    f.profile["queues"]=[]
    calls=source_transport(monkeypatch,[])
    assert f.db.release_pipeline_github_budget("github-default",token="existing-skill",
        task_id=f.task.id,task_started_at=f.task.started_at)
    result=pi.analyze("example",f.db.list_series(),use_cache=False)
    assert result["cache"]["complete"] is True and result["queues"]==[]
    assert len(calls.observations)==2 and not calls and not calls.previews
    assert result["source_search_usage"]["conservative_graphql_spend"]==2
    assert result["source_search_usage"]["successful_observation_cost_proved"] is True


@pytest.mark.parametrize("stage",["initial-metadata","later-metadata","preview","data"])
@pytest.mark.parametrize("escaped",[False,True])
def test_modern_raw_duplicate_cost_keys_never_become_a_clean_receipt(continuation,monkeypatch,capsys,stage,escaped):
    f=continuation;stable_rates(monkeypatch)
    monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    calls=source_transport(monkeypatch,[page([])])
    transport=pi.subprocess.run
    metadata=0;altered=[]
    def run(command,**kwargs):
        nonlocal metadata
        result=transport(command,**kwargs)
        query=json.loads(kwargs.get("input") or "{}").get("query","")
        target=False
        if query==pi._GITHUB_RATE_LIMIT_QUERY:
            metadata+=1
            target=(stage=="initial-metadata" and metadata==1 or stage=="later-metadata" and metadata==2)
        elif "PipelineSource" in query:
            target=(stage=="preview" and "dryRun: true" in query or stage=="data" and "dryRun: true" not in query)
        if target:
            replacement=r'"cost": 100, "co\u0073t": 1' if escaped else '"cost": 100, "cost": 1'
            assert '"cost": 1' in result.stdout
            result.stdout=result.stdout.replace('"cost": 1',replacement,1)
            altered.append(True)
        return result
    monkeypatch.setattr(pi.subprocess,"run",run)
    monkeypatch.setenv("PP_TASK_ID",f.env["PP_TASK_ID"])
    before=ledger(f)
    assert source.run(["--profile","example"])==(3 if "metadata" in stage else 2)
    result=json.loads(capsys.readouterr().out)
    assert result["source_refreshed"] is False and altered==[True]
    assert f.db.list_pipeline_snapshots("example")==[] and ledger(f)==before
    assert len(calls)==(1 if stage=="data" else 0)
    assert len(calls.previews)==(1 if stage in ("preview","data") else 0)
