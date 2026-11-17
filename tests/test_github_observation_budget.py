import copy

import pytest

from promptpilot.github_observation_budget import AvailabilityEnvelope,CostUnproved,ObservationCost
from tests.test_github_budget import _limits


def test_unknown_pricing_reserves_all_and_only_clean_receipt_settles():
    s=ObservationCost(100);s.bootstrap(2);s.reserve()
    assert s.escrow==98 and s.remaining==0
    s.settle(7)
    assert s.charged==s.observed_actual==9 and s.remaining==91
    s.reserve(12);s.settle(8)
    assert s.charged==17 and s.remaining==83


@pytest.mark.parametrize('actual',[None,True,0,-1,101])
def test_bad_receipt_poison_retains_escrow_and_cannot_resume(actual):
    s=ObservationCost(100);s.reserve()
    with pytest.raises(CostUnproved):s.settle(actual)
    assert s.uncertain and s.charged==100 and s.remaining==0
    with pytest.raises(CostUnproved):s.reserve()


def test_transport_timeout_retains_pending_quote_without_promising_server_charge():
    s=ObservationCost(100);s.bootstrap(1);s.reserve(5);s.poison()
    assert s.charged==6 and s.observed_actual==1 and s.uncertain


def test_cap_is_one_whole_observation_not_per_page():
    s=ObservationCost(10);s.bootstrap(1)
    for _ in range(2):s.reserve();s.settle(2);s.reserve(2);s.settle(2)
    assert s.charged==9
    s.reserve();s.settle(1)
    with pytest.raises(CostUnproved):s.reserve()


def test_bootstrap_raw_already_includes_metadata_cost_no_double_charge():
    raw=_limits(graphql=4998);e=AvailabilityEnvelope(raw)
    assert e.projected_limits(raw)['graphql']['remaining']==4998


def test_actual_alternating_windows_increases_never_replenish_envelope():
    a=_limits(graphql=4993,reset=1794920802);b=_limits(graphql=4944,reset=1794920745)
    original=copy.deepcopy(a);e=AvailabilityEnvelope(a)
    e.charge_graphql(2);e.observe(b)
    assert e.remaining['graphql']==4944
    e.charge_graphql(7);e.observe(_limits(graphql=4970,reset=1794920802))
    assert e.remaining['graphql']==4937
    # The next observed decrease already covers our same interval: no double
    # debit beyond min(raw, priorEnvelope - intervalOwn).
    e.charge_graphql(3);e.observe(_limits(graphql=4900,reset=1794920745))
    assert e.remaining['graphql']==4900 and a==original


def test_projection_cannot_exceed_fresh_raw_even_before_observe():
    e=AvailabilityEnvelope(_limits(graphql=4900));raw=_limits(graphql=10)
    raw['graphql']['limit']=20;raw['graphql']['used']=10
    projected=e.projected_limits(raw)
    assert projected['graphql']['remaining']==10 and projected['graphql']['used']==10


def test_all_resources_preserve_lower_available_window():
    e=AvailabilityEnvelope(_limits(core=1000,search=22,graphql=4000))
    e.charge_graphql(5);e.observe(_limits(core=5000,search=30,graphql=5000))
    assert e.remaining=={'core':1000,'search':22,'graphql':3995}
