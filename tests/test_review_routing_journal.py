"""Crash/timeout receipts do not permit a second non-idempotent POST."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import threading

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_journal import Journal
from promptpilot.review_routing_wire import render


PAYLOAD=json.loads((Path(__file__).parent/'fixtures/review-routing-wire-v1.json').read_text(encoding='utf-8'))['records'][0]['payload']
OWNER=PAYLOAD['owner']
BODY=render(PAYLOAD)
REQUEST={'owner':OWNER,'phase':'publication','repository':'owner/repo','number':42,'body':BODY}


def test_crash_after_prepare_or_invisible_timeout_is_not_permission_to_repeat_post(tmp_path):
    first=Journal(tmp_path)
    assert first.prepare(**REQUEST) is True
    recovered=Journal(tmp_path)
    assert recovered.get(owner=OWNER,phase='publication')['state']=='pending'
    with pytest.raises(PipelineError,match='ambiguous'):
        recovered.prepare(**REQUEST)
    recovered.ambiguous(owner=OWNER,phase='publication')
    with pytest.raises(PipelineError,match='ambiguous'):
        Journal(tmp_path).prepare(**REQUEST)
    # Later authenticated read-back of the exact request can recover the
    # returned ID. Mere lack of a visible result never clears the request.
    recovered.confirm(owner=OWNER,phase='publication',returned_id='9007199254740993',body=BODY)
    assert Journal(tmp_path).prepare(**REQUEST) is False
    assert recovered.get(owner=OWNER,phase='publication')['returned_id']=='9007199254740993'


def test_two_local_process_equivalents_can_attempt_exact_post_only_once(tmp_path):
    readers=[Journal(tmp_path),Journal(tmp_path)]
    barrier=threading.Barrier(2)
    def attempt(journal):
        barrier.wait()
        try:return journal.prepare(**REQUEST)
        except PipelineError as exc:
            assert 'ambiguous' in str(exc)
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,readers))
    assert sum(results)==1


def test_response_from_another_id_or_modified_body_cannot_rewrite_known_receipt(tmp_path):
    journal=Journal(tmp_path)
    journal.prepare(**REQUEST)
    with pytest.raises(PipelineError,match='prepared exact body'):
        journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY+' ')
    journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY)
    journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY)
    with pytest.raises(PipelineError,match='known returned ID'):
        journal.confirm(owner=OWNER,phase='publication',returned_id='11',body=BODY)
    with pytest.raises(PipelineError,match='already completed'):
        journal.ambiguous(owner=OWNER,phase='publication')


def test_request_key_is_bound_to_exact_body_target_and_owner(tmp_path):
    journal=Journal(tmp_path)
    journal.prepare(**REQUEST)
    changed=copy.deepcopy(PAYLOAD)
    changed['anchor']='OTHER_ANCHOR'
    for request in [dict(REQUEST,body=render(changed)),dict(REQUEST,number=43),dict(REQUEST,repository='owner/other')]:
        with pytest.raises(PipelineError,match='another body/target'):
            journal.prepare(**request)
    with pytest.raises(PipelineError,match='owner differs'):
        journal.prepare(**dict(REQUEST,owner='00000000-0000-4000-8000-000000000002'))


def test_unsupported_local_journal_version_fails_before_any_request(tmp_path):
    journal=Journal(tmp_path)
    import sqlite3
    with sqlite3.connect(journal.path) as connection:
        connection.execute('PRAGMA user_version=2')
    with pytest.raises(PipelineError,match='unsupported routing journal'):
        Journal(tmp_path)
