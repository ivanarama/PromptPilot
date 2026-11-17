"""A failed checker can recover only through fresh, fully published evidence."""
import copy
import json
import subprocess

import pytest

from promptpilot import pipeline_insights as pi, pipeline_source_refresh as source
from tests.test_pipeline_graphql_search import REAL_SEARCH, page, stable_rates
from tests.test_pipeline_source_cost_meter import metered_health, source_transport
from tests.test_pipeline_source_refresh import continuation as continuation


@pytest.fixture
def recovery(continuation,monkeypatch,tmp_path,request):
    f=continuation;stable_rates(monkeypatch)
    monkeypatch.setattr(pi,"_github_search",REAL_SEARCH)
    calls=source_transport(monkeypatch,[page([]),page([]),page([])])
    health=metered_health(f,tmp_path,monkeypatch)
    assert source.refresh("example",f.env)["source_refreshed"]
    fingerprint=pi._profile_fingerprint(f.profile)
    cache_key=pi._cache_key("example",fingerprint)
    oldcache=f.db.get_setting(cache_key)
    oldsnapshots=f.db.list_pipeline_snapshots("example")
    transport=pi.subprocess.run
    executable=f.profile["health_check"]["command"][0]
    state={"timeout":True,"attempts":0,"failure":getattr(request,"param","timeout")}
    def run(command,**kwargs):
        if command[0]==executable:
            state["attempts"]+=1
            if state["timeout"] and state["failure"]=="timeout":raise subprocess.TimeoutExpired(command,1)
            result=transport(command,**kwargs)
            if state["timeout"]:
                payload=json.loads(result.stdout)
                usage=payload["source_graphql_usage"]
                if state["failure"]=="missing":payload.pop("source_graphql_usage")
                elif state["failure"]=="nonce":usage["nonce"]="f"*32
                elif state["failure"]=="cap":usage.update(charged_upper_bound=usage["allowance"]+1,observed_actual_cost=usage["allowance"]+1)
                elif state["failure"]=="partial":usage["complete"]=False
                elif state["failure"]=="uncertain":usage.update(uncertain_cost=True,unproved_attempts=1)
                elif state["failure"]=="zero":usage.update(charged_upper_bound=0,observed_actual_cost=0,
                    physical_attempts=0,preview_attempts=0,data_attempts=0)
                result.stdout=json.dumps(payload)
            return result
        return transport(command,**kwargs)
    monkeypatch.setattr(pi.subprocess,"run",run)
    failed=source.refresh("example",f.env)
    assert failed["reason"]=="checker_failed" and not failed["source_refreshed"]
    assert f.db.get_setting(cache_key)==oldcache
    assert f.db.list_pipeline_snapshots("example")==oldsnapshots
    event=f.db.get_pipeline_refresh_status("example")
    revision=event["source_checker_failure_revisions"][fingerprint]
    assert revision==event["revision"]
    assert f.db.release_pipeline_github_budget("github-default",token="existing-skill",
        task_id=f.task.id,task_started_at=f.task.started_at)
    return f,state,calls,health,cache_key,oldcache,oldsnapshots,revision


@pytest.mark.parametrize("intermediate",["none","expired-defer","availability","status-none"])
@pytest.mark.parametrize("healthy",[False,True])
def test_normal_refresh_must_reexecute_checker_after_failure(recovery,intermediate,healthy):
    f,state,calls,health,key,oldcache,oldsnapshots,revision=recovery
    if intermediate!="none":
        event=f.db.get_pipeline_refresh_status("example")
        status=copy.deepcopy(event["status"])
        if intermediate=="expired-defer":status["refresh_deferred_until"]="2000-01-01T00:00:00+00:00"
        elif intermediate=="availability":
            status={"refresh_blocked":"low","refresh_deferred_until":"2999-01-01T00:00:00+00:00",
                "github_budget":{"state":"low","budget_route":"insights"}}
        else:status=None
        assert f.db.publish_pipeline_refresh_status("example","owner/example",status)
    cached=pi.read_cached("example",f.db.list_series())
    assert cached["cache"]["refresh_blocked"]=="checker_failed"
    assert pi._required_snapshot_state(cached,f.profile,"fix") is not None
    assert f.db.get_pipeline_refresh_status("example")["source_checker_failure_revisions"][pi._profile_fingerprint(f.profile)]==revision
    state["timeout"]=not healthy
    result=pi.analyze("example",f.db.list_series(),use_cache=False,refresh_diagnostics=False)
    assert state["attempts"]==2  # forced failure + actual new normal invocation
    if healthy:
        assert len(health)==2 and len(calls)==2
        assert result["source_checker_verified_failure_revision"]==revision
        assert result["source_search_usage"]["health_cost_provenance"]["reused"] is False
        assert len(f.db.list_pipeline_snapshots("example"))==len(oldsnapshots)+1
        assert pi._required_snapshot_state(pi.read_cached("example",f.db.list_series()),f.profile,"fix") is None
    else:
        assert len(health)==len(calls)==1
        assert result["cache"]["refresh_blocked"]=="checker_failed"
        assert f.db.get_setting(key)==oldcache and f.db.list_pipeline_snapshots("example")==oldsnapshots
        assert pi._required_snapshot_state(pi.read_cached("example",f.db.list_series()),f.profile,"fix") is not None


def test_fresh_checker_without_winning_source_cas_cannot_ack_failure(recovery,monkeypatch):
    f,state,_calls,_health,key,oldcache,oldsnapshots,revision=recovery
    state["timeout"]=False
    original=f.db.set_setting_if_newer_revision
    first=True
    def invalidate(*args,**kwargs):
        nonlocal first
        if first:
            first=False
            return False  # lost publication fence; old cache/epoch stay fresh
        return original(*args,**kwargs)
    monkeypatch.setattr(f.db,"set_setting_if_newer_revision",invalidate)
    result=pi.analyze("example",f.db.list_series(),use_cache=False,refresh_diagnostics=False)
    assert state["attempts"]==2 and result["cache"]["refresh_blocked"]=="checker_failed"
    assert f.db.get_setting(key)==oldcache and f.db.list_pipeline_snapshots("example")==oldsnapshots
    assert f.db.get_pipeline_refresh_status("example")["source_checker_failure_revisions"][pi._profile_fingerprint(f.profile)]==revision
    assert pi._required_snapshot_state(pi.read_cached("example",f.db.list_series()),f.profile,"fix") is not None
    third=pi.analyze("example",f.db.list_series(),use_cache=False,refresh_diagnostics=False)
    assert state["attempts"]==3
    assert third["source_search_usage"]["health_cost_provenance"]["reused"] is False
    assert third["source_checker_verified_failure_revision"]==revision
    assert len(f.db.list_pipeline_snapshots("example"))==len(oldsnapshots)+1


def test_reused_health_never_advances_recovery_revision_or_timestamp(recovery):
    f,state,_calls,_health,_key,_oldcache,_oldsnaps,revision=recovery
    state["timeout"]=False
    fresh=pi.analyze("example",f.db.list_series(),use_cache=False)
    reused=pi.analyze("example",f.db.list_series(),use_cache=False)
    assert state["attempts"]==2
    assert reused["source_checker_verified_failure_revision"]==fresh["source_checker_verified_failure_revision"]==revision
    assert reused["diagnostics_generated_at"]==fresh["diagnostics_generated_at"]
    assert reused["diagnostics"]["source_graphql_usage"]["nonce"]==fresh["diagnostics"]["source_graphql_usage"]["nonce"]
    assert reused["source_search_usage"]["health_cost_provenance"]["charged"]==0


@pytest.mark.parametrize("recovery",["missing","nonce","cap","partial","uncertain","zero"],indirect=True)
def test_invalid_fresh_exit0_receipt_also_requires_new_checker_before_recovery(recovery):
    f,state,_calls,_health,_key,_cache,snapshots,revision=recovery
    assert pi.read_cached("example",f.db.list_series())["cache"]["refresh_blocked"]=="checker_failed"
    state["timeout"]=False
    result=pi.analyze("example",f.db.list_series(),use_cache=False,refresh_diagnostics=False)
    assert state["attempts"]==2
    assert result["source_search_usage"]["health_cost_provenance"]["reused"] is False
    assert result["source_checker_verified_failure_revision"]==revision
    assert len(f.db.list_pipeline_snapshots("example"))==len(snapshots)+1


@pytest.mark.parametrize("event_missing",[False,True])
def test_future_cache_acknowledgement_cannot_mask_failure(recovery,monkeypatch,event_missing):
    f,_state,_calls,_health,_key,_cache,_snapshots,_revision=recovery
    event=f.db.get_pipeline_refresh_status("example")
    pi._cache["example"][1]["source_checker_verified_failure_revision"]=event["revision"]+1
    if event_missing:monkeypatch.setattr(f.db,"get_pipeline_refresh_status",lambda *a,**k:None)
    cached=pi.read_cached("example",f.db.list_series())
    assert cached["cache"]["refresh_blocked"]=="checker_state_unavailable"
    assert pi._required_snapshot_state(cached,f.profile,"fix") is not None


@pytest.mark.parametrize("fault",["missing","foreign-token","foreign-scope","expired"])
def test_failure_watermark_requires_exact_live_source_lease(isolated_db,fault):
    db=isolated_db
    assert db.acquire_pipeline_scan_lease("scope","owner",30)["acquired"]
    guard={"scope":"scope","token":"owner"}
    if fault=="missing":guard=None
    elif fault=="foreign-token":guard["token"]="other"
    elif fault=="foreign-scope":guard["scope"]="other"
    else:
        with db._connect() as conn:
            key=db._pipeline_scan_lease_key("scope")
            value=json.loads(conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()[0])
            value["expires_at"]=0
            conn.execute("UPDATE settings SET value=? WHERE key=?",(json.dumps(value),key))
    status={"profile_id":"example","repository":"owner/example","status":{"refresh_blocked":"checker_failed",
        "github_budget":{"source_checker_failure_profile_hash":"a"*64}}}
    assert db.release_pipeline_scan_lease("scope","owner",refresh_status=status,checker_failure_guard=guard)
    assert "source_checker_failure_revisions" not in db.get_pipeline_refresh_status("example")


def test_failure_watermarks_are_per_profile_fingerprint(isolated_db):
    db=isolated_db
    for pin in ("a"*64,"b"*64):
        assert db.acquire_pipeline_scan_lease("scope","owner",30)["acquired"]
        assert db.release_pipeline_scan_lease("scope","owner",checker_failure_guard={"scope":"scope","token":"owner"},
            refresh_status={"profile_id":"example","repository":"owner/example","status":{
                "refresh_blocked":"checker_failed","github_budget":{"source_checker_failure_profile_hash":pin}}})
    markers=db.get_pipeline_refresh_status("example")["source_checker_failure_revisions"]
    assert set(markers)=={"a"*64,"b"*64} and markers["a"*64]<markers["b"*64]
    assert db.publish_pipeline_refresh_status("example","owner/example",None)
    assert db.get_pipeline_refresh_status("example")["source_checker_failure_revisions"]==markers
