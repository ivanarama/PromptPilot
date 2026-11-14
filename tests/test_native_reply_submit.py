"""A successful paste is not proof that the terminal submitted the user's request."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from promptpilot import bot


def terminal(**changes):
    return dict(pane_id='w1:p1',terminal_id='terminal-1',agent='codex',agent_status='idle',
                state_change_seq=10,completion_seq=4,**changes)


def reply(agent):return {'result':{'agent':agent}}


def stalled():return {'error':{'code':'agent_prompt_stalled'}}


async def allowed():return True


def run(monkeypatch, responses, before=None, access=allowed):
    calls=[]
    async def rpc(*args, **kwargs):
        calls.append((args,kwargs));assert responses,args
        return responses.pop(0)
    monkeypatch.setattr(bot,'_herdr_json',rpc)
    monkeypatch.setattr(bot.asyncio,'sleep',AsyncMock())
    result=asyncio.run(bot._submit_native_reply('w1:p1','User request',before or terminal(),None,access))
    return result,calls


def test_waits_for_started_turn_not_plain_rpc_success(monkeypatch):
    after=terminal();after.update(agent_status='working',state_change_seq=11)
    result,calls=run(monkeypatch,[reply(after)])
    assert result=='started'
    assert calls[0][0]==('agent','prompt','w1:p1','User request','--wait','--until','working','--timeout','7000')
    assert calls[0][1]['allow_error'] is True


def test_recovers_paste_that_swallowed_enter_without_reinserting_text(monkeypatch):
    after=terminal();after.update(agent_status='working',state_change_seq=11)
    result,calls=run(monkeypatch,[stalled(),reply(terminal()),{'result':True},reply(after)])
    assert result=='started'
    assert sum(args[:2]==('agent','prompt') for args,_ in calls)==1
    assert sum(args==('agent','send-keys','w1:p1','right','enter') for args,_ in calls)==1


@pytest.mark.parametrize('change',[{'agent_status':'blocked'},{'agent_status':'working'},
                                 {'agent_status':'unknown'},{'terminal_id':'replacement'},
                                 {'state_change_seq':11},{'pane_id':'another'}])
def test_never_adds_enter_to_a_changed_terminal_or_dialog(monkeypatch,change):
    current=terminal();current.update(change)
    result,calls=run(monkeypatch,[stalled(),reply(current)])
    assert result==('started' if change.get('agent_status')=='working' else 'unconfirmed')
    assert all(args[:2]!=('agent','send-keys') for args,_ in calls)


def test_transport_error_does_not_submit_an_unknown_existing_draft(monkeypatch):
    result,calls=run(monkeypatch,[None])
    assert result=='unconfirmed' and len(calls)==1


def test_enter_without_a_started_turn_does_not_claim_success(monkeypatch):
    result,calls=run(monkeypatch,[stalled(),reply(terminal()),{'result':True}]+[reply(terminal()) for _ in range(8)])
    assert result=='unconfirmed'
    assert sum(args[:2]==('agent','send-keys') for args,_ in calls)==1


def test_revoked_authorization_prevents_enter_recovery(monkeypatch):
    checks=iter([True,False])
    async def access():return next(checks)
    result,calls=run(monkeypatch,[stalled(),reply(terminal())],access=access)
    assert result=='denied' and all(args[:2]!=('agent','send-keys') for args,_ in calls)


def test_busy_session_is_reported_as_busy_not_as_a_new_started_turn(monkeypatch):
    before=terminal();before['agent_status']='working'
    result,_=run(monkeypatch,[reply(deepcopy(before))],before)
    assert result=='busy'


def test_fast_completed_turn_is_not_submitted_again(monkeypatch):
    current=terminal();current.update(agent_status='done',state_change_seq=12,completion_seq=5)
    result,calls=run(monkeypatch,[stalled(),reply(current)])
    assert result=='started' and all(args[:2]!=('agent','send-keys') for args,_ in calls)


def test_cli_error_json_survives_only_when_requested(monkeypatch):
    process=SimpleNamespace(returncode=1,communicate=AsyncMock(return_value=(b'{"error":{"code":"agent_prompt_stalled"}}',b'')))
    monkeypatch.setattr(bot.asyncio,'create_subprocess_exec',AsyncMock(return_value=process))
    assert asyncio.run(bot._herdr_json('agent','prompt','pane','text')) is None
    assert asyncio.run(bot._herdr_json('agent','prompt','pane','text',allow_error=True))==stalled()


def test_a_preexisting_permission_dialog_never_receives_the_prompt(monkeypatch):
    before=terminal();before['agent_status']='blocked'
    result,calls=run(monkeypatch,[],before)
    assert result=='blocked' and calls==[]


def test_codex_boundary_is_never_used_for_another_agent(monkeypatch):
    before=terminal();before['agent']='claude'
    result,calls=run(monkeypatch,[stalled(),reply(before)],before)
    assert result=='unconfirmed' and all(args[:2]!=('agent','send-keys') for args,_ in calls)
