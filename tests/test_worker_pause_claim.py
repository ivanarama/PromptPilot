"""Public pause/resume and the real scheduler claim transaction."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import httpx
import pytest

from promptpilot.api import app
from promptpilot.models import TaskCreate


def action(name):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8420') as client:
            return await client.post('/api/worker/'+name)
    response=asyncio.run(request())
    assert response.status_code==200,response.text
    return response.json()


@pytest.mark.parametrize('ranked',[False,True])
def test_pause_committed_after_worker_precheck_prevents_actual_claim(isolated_db,ranked):
    task=isolated_db.create_task(TaskCreate(prompt='must remain pending'))
    assert isolated_db.is_paused() is False  # Worker's earlier, now stale precheck.
    assert action('pause')['paused'] is True
    ranks=[]
    def rank(candidate):
        ranks.append(candidate.id)
        return (0,)
    options={'order_key_fn':rank} if ranked else {}
    assert isolated_db.get_next_runnable(**options) is None
    retained=isolated_db.get_task(task.id)
    assert retained.status.value=='pending' and retained.started_at is None
    assert ranks==[]
    assert action('resume')['paused'] is False
    assert isolated_db.get_next_runnable(**options).id==task.id
    assert ranks==([task.id] if ranked else [])


def test_pause_does_not_interrupt_already_claimed_task(isolated_db):
    first=isolated_db.create_task(TaskCreate(prompt='already running'))
    second=isolated_db.create_task(TaskCreate(prompt='next'))
    claimed=isolated_db.get_next_runnable()
    assert claimed.id==first.id
    action('pause')
    assert isolated_db.get_next_runnable() is None
    assert isolated_db.get_task(first.id).status.value=='running'
    assert isolated_db.get_task(first.id).started_at==claimed.started_at
    isolated_db.mark_completed(first.id,'finished normally',exit_code=0)
    assert isolated_db.get_task(first.id).status.value=='completed'
    assert isolated_db.get_task(second.id).status.value=='pending'


def test_claim_waiting_for_pause_writer_observes_committed_pause(isolated_db,monkeypatch):
    task=isolated_db.create_task(TaskCreate(prompt='writer race'))
    original=isolated_db._connect
    entering=threading.Event()
    @contextmanager
    def observe(*args,**kwargs):
        if kwargs.get('immediate'):entering.set()
        with original(*args,**kwargs) as connection:yield connection
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(immediate=True) as writer:
            writer.execute("INSERT INTO settings(key,value) VALUES('worker_paused','1')")
            monkeypatch.setattr(isolated_db,'_connect',observe)
            future=pool.submit(isolated_db.get_next_runnable)
            assert entering.wait(3)
        assert future.result(timeout=5) is None
    assert isolated_db.get_task(task.id).status.value=='pending'
