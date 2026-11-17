"""Explicit pricing opt-in preserves independent legacy projects unchanged."""
import ast
import hashlib
import inspect
import copy
import json
import textwrap
from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as pi, pipeline_source_refresh as source
from tests.test_pipeline_graphql_search import REAL_SEARCH, page
from tests.test_pipeline_source_cost_meter import RUN_HEALTH, source_transport
from tests.test_pipeline_source_refresh import continuation as continuation, ledger


@pytest.mark.parametrize("method,expected", [
    ("__init__","76d51dbc8dc99e27257f86c85a647708cd828f2b6792a5a7f8969b782db9a448"),
    ("_allowance_denied","285fc8d0aa0981565f11dbe129fa212a8a11447bfbbb972a5838e28845ae4da5"),
    ("before_request","44f7dbde5005fd04670a894a8a77fc8f5fef45ec5073ed102c5e80904a020bc4")])
def test_legacy_counter_algorithm_is_exact_accepted_0930_ast(method,expected):
    # Normative migration pin: no new math or fallback may enter this old policy.
    tree=ast.parse(textwrap.dedent(inspect.getsource(getattr(pi._LegacyGraphQLSearchBudget,method)))).body[0]
    # Python3.12 adds empty PEP695 metadata absent from Python3.10's AST.
    # Nonempty type parameters remain part of the pinned semantic structure.
    for child in ast.walk(tree):
        if getattr(child,"type_params",None)==[]:
            del child.type_params
    assert hashlib.sha256(ast.dump(tree,include_attributes=False).encode()).hexdigest()==expected


def test_legacy_adapter_keeps_original_preview_data_and_final_quotes():
    value=object.__new__(pi._LegacyGraphQLSearchBudget)
    calls=[]
    value.before_request=calls.append
    value.before_preview();value.before_data(7);value.metadata()
    assert calls==[1,7,0]
    value.pending_cost=7
    with pytest.raises(ValueError,match="cost changed after preview"):
        value.after_data(8)


def legacy_checker(f,monkeypatch,command):
    f.profile["github_budget"].pop("source_cost_protocol")
    f.profile["health_check"]={"command":command}
    monkeypatch.setattr(pi,"_run_profile_health_check",RUN_HEALTH)
    transport=pi.subprocess.run
    calls=[]
    def run(argv,**kwargs):
        if argv==command:
            calls.append(argv)
            assert not any("source-graphql" in v for v in argv)
            return SimpleNamespace(returncode=0,stdout=json.dumps({"state":"green","findings":[]}),stderr="")
        return transport(argv,**kwargs)
    monkeypatch.setattr(pi.subprocess,"run",run)
    return calls


@pytest.mark.parametrize("command", [["/fake/go","run","./tools/health/main.go","-json"],
                                    ["/fake/pilot-checker","-technical-handoff","-json"]])
def test_absent_mode_runs_existing_unmetered_project_checker_without_new_proof(continuation,monkeypatch,command):
    f=continuation; monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    calls=source_transport(monkeypatch,[page([])])
    checks=legacy_checker(f,monkeypatch,command)
    before=ledger(f); result=source.refresh("example",f.env)
    assert result["source_refreshed"] and len(calls)==len(checks)==1
    usage=result["source_search_usage"]
    assert usage["scope"]=="legacy strict counter-window Source accounting"
    assert "successful_observation_cost_proved" not in usage and ledger(f)==before
    assert "source_cost_protocol" not in pi._github_budget_policy(f.profile)


def test_legacy_keeps_old_window_drift_refusal_instead_of_silently_selecting_modern(continuation,monkeypatch):
    f=continuation; monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    from tests.test_github_budget import _limits
    first=_limits()
    samples=[first,_limits(reset=first["graphql"]["reset"]+1)]
    def rates():return samples.pop(0)
    calls=source_transport(monkeypatch,[],rates)
    checks=legacy_checker(f,monkeypatch,["/fake/checker","-json"])
    result=source.refresh("example",f.env)
    assert result["state"]=="wait" and not result["source_refreshed"]
    assert "window changed" in result["budget"]["reason"]
    assert len(checks)==1 and not calls and not calls.previews
    assert f.db.list_pipeline_snapshots("example")==[]


def test_legacy_minimum_preview_quote_still_denies_before_preview(continuation,monkeypatch):
    f=continuation; monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    f.profile["github_budget"]["costs"]["insights"]["graphql"]=2
    calls=source_transport(monkeypatch,[])
    legacy_checker(f,monkeypatch,["/fake/checker","-json"])
    result=source.refresh("example",f.env)
    assert not result["source_refreshed"] and result["state"]=="wait"
    assert not calls and not calls.previews and len(calls.observations)==1


@pytest.mark.parametrize("mode", [None,False,"","unknown-v2",True])
def test_unrecognized_present_mode_refuses_before_every_rpc(continuation,monkeypatch,mode):
    f=continuation; f.profile["github_budget"]["source_cost_protocol"]=mode
    monkeypatch.setattr(pi,"_github_rate_limits",lambda:pytest.fail("invalid mode reached HTTP"))
    monkeypatch.setattr(pi,"_run_profile_health_check",lambda _:pytest.fail("invalid mode spawned checker"))
    with pytest.raises(ValueError,match="source_cost_protocol"):
        source.refresh("example",f.env)
    assert f.db.list_pipeline_snapshots("example")==[]


def test_modern_checker_declaration_without_pricing_optin_refuses_before_rpc(continuation,monkeypatch):
    f=continuation; f.profile["github_budget"].pop("source_cost_protocol")
    monkeypatch.setattr(pi,"_github_rate_limits",lambda:pytest.fail("undeclared pricing reached HTTP"))
    monkeypatch.setattr(pi,"_run_profile_health_check",lambda _:pytest.fail("undeclared pricing spawned checker"))
    with pytest.raises(ValueError,match="requires github_budget.source_cost_protocol"):
        source.refresh("example",f.env)


def test_modern_optin_cannot_reuse_legacy_no_health_full_cache(continuation,monkeypatch):
    f=continuation
    f.profile.pop("health_check")
    f.profile["github_budget"].pop("source_cost_protocol")
    legacy_projection=copy.deepcopy(f.profile);legacy_projection.pop("github_budget")
    old_hash=pi._profile_fingerprint(f.profile)
    assert old_hash==hashlib.sha256(json.dumps(legacy_projection,ensure_ascii=False,sort_keys=True,
        separators=(",",":"),default=str).encode()).hexdigest()
    monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    calls=source_transport(monkeypatch,[page([]),page([])])
    assert source.refresh("example",f.env)["source_refreshed"]
    old_key=pi._cache_key("example",old_hash);old_bytes=f.db.get_setting(old_key)
    f.profile["github_budget"]["source_cost_protocol"]="source-graphql-cost-v1"
    modern_hash=pi._profile_fingerprint(f.profile)
    assert modern_hash!=old_hash
    cached=pi.read_cached("example",f.db.list_series())
    assert pi._required_snapshot_state(cached,f.profile,"fix") is not None
    result=pi.analyze("example",f.db.list_series(),use_cache=False,reuse_fresh=True,snapshot_queue_id="fix")
    assert len(calls)==2 and result["source_search_usage"]["successful_observation_cost_proved"] is True
    assert result["source_search_usage"]["conservative_graphql_spend"]==6
    assert f.db.get_setting(old_key)==old_bytes and len(f.db.list_pipeline_snapshots("example"))==2
    f.profile["github_budget"]["minimum_remaining"]["core"]+=17
    f.profile["github_budget"]["costs"]["insights"]["graphql"]+=10
    assert pi._profile_fingerprint(f.profile)==modern_hash
