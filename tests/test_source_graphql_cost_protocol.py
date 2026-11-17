import copy

import pytest

from promptpilot.source_graphql_cost import PROTOCOL,validate_receipt


def receipt():
    return {'protocol':PROTOCOL,'nonce':'a'*32,'allowance':99,'charged_upper_bound':42,
        'observed_actual_cost':42,'physical_attempts':12,'preview_attempts':6,'data_attempts':6,
        'unproved_attempts':0,'uncertain_cost':False,'complete':True,'repo':'owner/repo',
        'trusted_account':'owner','contract_sha256':'b'*64}


def check(value):
    expected={k:receipt()[k] for k in ('protocol','repo','trusted_account','contract_sha256')}
    return validate_receipt(value,expected,nonce='a'*32,allowance=99)


def test_full_successful_receipt_is_the_only_health_cost_proof():
    assert check(receipt())==42


@pytest.mark.parametrize('key,value',[
    ('nonce','c'*32),('allowance',100),('allowance',True),('protocol','old'),
    ('repo','foreign/repo'),('trusted_account','foreign'),('contract_sha256','c'*64),
    ('complete',False),('uncertain_cost',True),('unproved_attempts',1),
    ('charged_upper_bound',100),('observed_actual_cost',41),('physical_attempts',13),
    ('preview_attempts',True),('data_attempts',-1),('reason','unexpected'),
    ('physical_attempts',10001),('extra',1)])
def test_mixed_partial_foreign_overcap_or_uncertain_receipt_refuses(key,value):
    candidate=copy.deepcopy(receipt());candidate[key]=value
    with pytest.raises(ValueError):check(candidate)


def test_old_unmetered_checker_is_not_zero_cost():
    with pytest.raises(ValueError):check(None)


def test_zero_live_receipt_is_not_cached_reuse():
    value=receipt()
    value.update(charged_upper_bound=0,observed_actual_cost=0,physical_attempts=0,preview_attempts=0,data_attempts=0)
    with pytest.raises(ValueError):check(value)
