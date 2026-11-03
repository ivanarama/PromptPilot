import asyncio
import base64
import json
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot, db, result_files, worker
from promptpilot.models import TaskCreate
from promptpilot.remote import POSIX, POWERSHELL, Remote, _ps_encode


def task_for(tmp_path, **kwargs):
    values = dict(id=7, working_dir=str(tmp_path), worktree_path=None, machine=None,
                  started_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
                  tg_chat_id=123, result="", prompt="Create a document", note=None)
    values.update(kwargs)
    return SimpleNamespace(**values)


def file_for(task, name="result.txt", data=b"result"):
    root = Path(result_files.output_root(task))
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(data)
    task.result = f"Finished\nPP-FILE: {path}"
    return result_files.specs(task)[0]


def test_only_explicit_deliverables_are_attached(tmp_path):
    task = task_for(tmp_path)
    spec = file_for(task)
    task.result += f"\nPP-FILE: {spec['path']}\n[source]({tmp_path / 'source.py'})"
    assert result_files.specs(task) == [spec]
    assert result_files.read_bytes(spec) == b"result"
    assert "PP-FILE:" not in result_files.visible_result(task.result)
    assert "[source]" in result_files.visible_result(task.result)


def test_example_markers_inside_code_blocks_are_not_deliverables(tmp_path):
    task = task_for(tmp_path, result="Example:\n```text\nPP-FILE: /etc/passwd\n```")
    assert result_files.specs(task) == []
    assert result_files.visible_result(task.result) == task.result


@pytest.mark.parametrize("name", [".env", ".env.prod", "credentials.json", "id_ed25519", "private.key"])
def test_credentials_are_not_automatic_result_files(tmp_path, name):
    task = task_for(tmp_path)
    root = Path(result_files.output_root(task))
    task.result = f"PP-FILE: {root / name}"
    with pytest.raises(result_files.RejectedFile, match="Credentials"):
        result_files.specs(task)


def test_output_directory_is_bound_to_task_attempt_and_worktree(tmp_path):
    task = task_for(tmp_path)
    original = result_files.output_root(task)
    task.started_at = datetime(2026, 11, 4, tzinfo=timezone.utc)
    assert result_files.output_root(task) != original
    task.worktree_path = str(tmp_path / 'worktree')
    assert str(tmp_path / 'worktree') in result_files.output_root(task)
    task.result = f"PP-FILE: {Path(original) / 'old.txt'}"
    with pytest.raises(result_files.RejectedFile, match="outside"):
        result_files.specs(task)


def test_windows_paths_do_not_depend_on_bot_operating_system():
    task = task_for(Path('.'), working_dir=r'D:\Projects\Demo')
    root = result_files.output_root(task)
    task.result = f"PP-FILE: {root}\\Готовый файл.ps1"
    assert result_files.specs(task)[0]['name'] == 'Готовый файл.ps1'
    task.result = f"PP-FILE: {root}\\..\\private.txt"
    with pytest.raises(result_files.RejectedFile):
        result_files.specs(task)


def test_non_telegram_tasks_keep_their_prompt(tmp_path):
    task = task_for(tmp_path, tg_chat_id=None)
    assert result_files.prompt_instruction(task) == ''
    task.tg_chat_id = 123
    task.note = 'Keep the existing files'
    prompt = worker.effective_prompt(task)
    assert result_files.output_root(task) in prompt
    assert prompt.index('</telegram-result-files>') < prompt.index('<приписка>')


def test_file_limit_and_missing_files_are_rejected(tmp_path):
    task = task_for(tmp_path)
    spec = file_for(task, data=b'large')
    with pytest.raises(result_files.RejectedFile, match='size limit'):
        result_files.read_bytes(spec, limit=2)
    Path(spec['path']).unlink()
    with pytest.raises(result_files.RejectedFile):
        result_files.read_bytes(spec)
    root = result_files.output_root(task)
    task.result = '\n'.join(f'PP-FILE: {root}/file{i}.txt' for i in range(6))
    with pytest.raises(result_files.RejectedFile, match='Too many'):
        result_files.specs(task)


def test_symlinked_output_directory_cannot_escape_project(tmp_path):
    task = task_for(tmp_path / 'project')
    root = Path(result_files.output_root(task))
    root.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'result.txt').write_bytes(b'private')
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation requires Windows developer mode or elevation')
    task.result = f'PP-FILE: {root / "result.txt"}'
    spec = result_files.specs(task)[0]
    with pytest.raises(result_files.RejectedFile):
        result_files.read_bytes(spec)


@pytest.mark.parametrize('data', [b'', b'\x00\xffscript\r\n'])
def test_posix_export_handles_binary_data_and_shell_metacharacters(tmp_path, data):
    task = task_for(tmp_path)
    spec = file_for(task, name="a';echo INJECTED;'.txt", data=data)
    command = shlex.split(result_files.export_script(spec, POSIX))
    process = subprocess.run([sys.executable, '-O', *command[1:]], capture_output=True, check=True)
    assert base64.b64decode(process.stdout.strip(), validate=True) == data


def test_remote_export_scope_check_survives_python_optimization(tmp_path):
    task = task_for(tmp_path)
    spec = file_for(task)
    outside = tmp_path / 'private.txt'
    outside.write_bytes(b'private')
    spec['path'] = str(outside)
    command = shlex.split(result_files.export_script(spec, POSIX))
    process = subprocess.run([sys.executable, '-O', *command[1:]], capture_output=True)
    assert process.returncode != 0
    assert process.stdout == b''


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows PowerShell exporter')
@pytest.mark.parametrize('data', [b'', b'\x00\xffscript\r\n'])
def test_powershell_export_handles_unicode_quotes_and_binary_data(tmp_path, data):
    task = task_for(tmp_path)
    spec = file_for(task, name="Файл';Write-Output INJECTED;'.ps1", data=data)
    script = result_files.export_script(spec, POWERSHELL)
    process = subprocess.run(
        [shutil.which('powershell.exe'), '-NoProfile', '-NonInteractive', '-EncodedCommand', _ps_encode(script)],
        capture_output=True, timeout=20, check=True,
    )
    assert base64.b64decode(process.stdout.strip(), validate=True) == data


def completed_task(isolated_db, tmp_path):
    task = db.create_task(TaskCreate(prompt='Create a document', working_dir=str(tmp_path), tg_chat_id=123))
    started = datetime(2026, 11, 3, tzinfo=timezone.utc)
    with db._connect() as conn:
        conn.execute("UPDATE tasks SET status='completed', started_at=? WHERE id=?", (started.isoformat(), task.id))
    task = db.get_task(task.id)
    file_for(task)
    with db._connect() as conn:
        conn.execute('UPDATE tasks SET result=? WHERE id=?', (task.result, task.id))
    return db.get_task(task.id)


def test_file_outbox_and_completion_are_atomic_and_idempotent(isolated_db, tmp_path):
    task = completed_task(isolated_db, tmp_path)
    notes = bot._result_file_notifications(task)
    assert db.mark_notified_with_files(task, notes)
    assert not db.mark_notified_with_files(task, notes)
    assert len(db.get_unsent_notifications()) == 1
    assert db.get_task(task.id).notified_at is not None


def test_file_outbox_failure_does_not_lose_completion(isolated_db, tmp_path, monkeypatch):
    task = completed_task(isolated_db, tmp_path)
    notes = bot._result_file_notifications(task)
    calls = []
    def fail_insert_clock():
        calls.append(True)
        if len(calls) == 2:
            raise OSError('outbox failure')
        return '2026-11-03T00:00:00+00:00'
    monkeypatch.setattr(db, '_now', fail_insert_clock)
    with pytest.raises(OSError):
        db.mark_notified_with_files(task, notes)
    assert db.get_task(task.id).notified_at is None
    assert db.get_unsent_notifications() == []


def test_old_attempt_cannot_queue_files_after_retry(isolated_db, tmp_path):
    task = completed_task(isolated_db, tmp_path)
    with db._connect() as conn:
        conn.execute("UPDATE tasks SET started_at='2026-11-04T00:00:00+00:00' WHERE id=?", (task.id,))
    assert not db.mark_notified_with_files(task, bot._result_file_notifications(task))
    assert db.get_unsent_notifications() == []


def test_delivery_uses_recorded_chat_and_preserves_file_bytes(isolated_db, tmp_path):
    task = completed_task(isolated_db, tmp_path)
    db.mark_notified_with_files(task, bot._result_file_notifications(task))
    note = db.get_unsent_notifications()[0]
    captured = []
    async def send(**kwargs):
        captured.append((kwargs['chat_id'], kwargs['filename'], kwargs['document'].read()))
    client = SimpleNamespace(send_document=send)
    asyncio.run(bot._send_result_file(client, note))
    assert captured == [(123, 'result.txt', b'result')]


def test_changed_machine_cannot_fall_back_to_local_file(tmp_path, monkeypatch):
    task = task_for(tmp_path, machine='windows')
    file_for(task)
    monkeypatch.setattr(bot, 'load_machines', lambda: {'windows': {'host': 'first'}})
    monkeypatch.setattr(bot, '_machine_remote', lambda name: Remote('first', POWERSHELL))
    payload = bot._result_file_notifications(task)[0]['message']
    monkeypatch.setattr(bot, '_machine_remote', lambda name: Remote('second', POWERSHELL))
    client = SimpleNamespace(send_document=AsyncMock())
    note = {'message': payload, 'machine': 'windows', 'task_id': task.id, 'tg_chat_id': 123}
    with pytest.raises(result_files.RejectedFile, match='endpoint'):
        asyncio.run(bot._send_result_file(client, note))
    client.send_document.assert_not_called()
    monkeypatch.setattr(bot, 'load_machines', lambda: {})
    with pytest.raises(result_files.RejectedFile, match='registered'):
        asyncio.run(bot._send_result_file(client, note))


def test_expired_files_do_not_trigger_remote_reads(tmp_path):
    client = SimpleNamespace(send_document=AsyncMock())
    note = {'message': json.dumps({'expires_at': 0}), 'machine': None}
    with pytest.raises(result_files.RejectedFile, match='expired'):
        asyncio.run(bot._send_result_file(client, note))
    client.send_document.assert_not_called()


def notify_ticks(monkeypatch, client, count):
    ticks = []
    async def sleep(_):
        if len(ticks) == count:
            raise asyncio.CancelledError()
        ticks.append(True)
    monkeypatch.setattr(bot.asyncio, 'sleep', sleep)
    monkeypatch.setattr(bot, '_epf_maybe_deliver', AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._notify_loop(client))


def test_notify_loop_automatically_attaches_and_does_not_replay(isolated_db, tmp_path, monkeypatch):
    completed_task(isolated_db, tmp_path)
    sent = []
    async def document(**kwargs):
        sent.append((kwargs['chat_id'], kwargs['filename'], kwargs['document'].read()))
    client = SimpleNamespace(send_message=AsyncMock(), send_document=document)
    notify_ticks(monkeypatch, client, 3)
    assert sent == [(123, 'result.txt', b'result')]
    assert db.get_unsent_notifications() == []
    assert client.send_message.await_count == 1
    assert 'PP-FILE:' not in client.send_message.call_args.kwargs['text']
    notify_ticks(monkeypatch, client, 1)  # Simulate a restarted bot process.
    assert len(sent) == 1


def test_transient_read_failure_remains_in_durable_outbox(isolated_db, tmp_path, monkeypatch):
    task = completed_task(isolated_db, tmp_path)
    db.mark_notified_with_files(task, bot._result_file_notifications(task))
    real_read = result_files.read_bytes
    def unavailable(*args):
        raise OSError('offline')
    monkeypatch.setattr(result_files, 'read_bytes', unavailable)
    client = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    notify_ticks(monkeypatch, client, 1)
    assert len(db.get_unsent_notifications()) == 1
    client.send_document.assert_not_called()
    monkeypatch.setattr(result_files, 'read_bytes', real_read)
    notify_ticks(monkeypatch, client, 1)
    client.send_document.assert_awaited_once()
    assert db.get_unsent_notifications() == []


def test_missing_file_reports_problem_once_without_leaking_path(isolated_db, tmp_path, monkeypatch):
    task = completed_task(isolated_db, tmp_path)
    db.mark_notified_with_files(task, bot._result_file_notifications(task))
    Path(result_files.specs(task)[0]['path']).unlink()
    client = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    notify_ticks(monkeypatch, client, 2)
    client.send_message.assert_awaited_once()
    assert str(tmp_path) not in client.send_message.call_args.kwargs['text']
    assert db.get_unsent_notifications() == []
    client.send_document.assert_not_called()


def native_for(tmp_path, **changes):
    agent = dict(pane_id='w1:p1', terminal_id='t1', agent_session={'value': 's1'},
                 completion_seq=0, agent_status='working', cwd=str(tmp_path))
    agent.update(changes)
    return agent, result_files.native_context(agent, '', None, 123)


def native_manifest(context, name='report.txt', data=b'finished'):
    root = Path(result_files.output_root(SimpleNamespace(**context)))
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    (root / '.pp-files.json').write_text(json.dumps({'files': [
        {'name': name, 'sha256': hashlib.sha256(data).hexdigest()}]}), encoding='utf-8')
    return root


def test_native_context_requires_completion_cursor_and_instance(tmp_path):
    _, first = native_for(tmp_path)
    _, second = native_for(tmp_path)
    assert first['id'] != second['id']
    assert 'manifest' in result_files.native_instruction(first)
    assert native_for(tmp_path, completion_seq=None)[1] is None
    assert native_for(tmp_path, terminal_id=None, agent_session=None)[1] is None


def test_native_manifest_missing_is_normal_and_hashes_are_recorded(tmp_path):
    _, context = native_for(tmp_path)
    assert result_files.manifest_specs(context, None) == []
    native_manifest(context)
    spec = result_files.manifest_specs(context, None)[0]
    assert spec['name'] == 'report.txt'
    assert spec['expected_sha256'] == hashlib.sha256(b'finished').hexdigest()


@pytest.mark.parametrize('name', ['../secret.txt', '/secret.txt', '.env', 'private.key'])
def test_native_manifest_rejects_unsafe_deliverables(tmp_path, name):
    _, context = native_for(tmp_path)
    root = native_manifest(context)
    (root / '.pp-files.json').write_text(json.dumps({'files': [
        {'name': name, 'sha256': '0' * 64}]}), encoding='utf-8')
    with pytest.raises(result_files.RejectedFile):
        result_files.manifest_specs(context, None)


def test_native_completion_is_scoped_to_reply_instance_and_survives_restart(isolated_db, tmp_path):
    agent, context = native_for(tmp_path)
    db.save_native_file_context('source', context)
    assert db.queue_native_file_manifests('source', [agent]) == 0
    agent.update(agent_status='done', completion_seq=1)
    assert db.queue_native_file_manifests('other-source', [agent]) == 0
    assert db.queue_native_file_manifests('source', [{**agent, 'terminal_id': 'reused'}]) == 0
    assert db.queue_native_file_manifests('source', [agent]) == 1
    assert db.queue_native_file_manifests('source', [agent]) == 0
    notes = db.get_unsent_notifications()
    assert len(notes) == 1 and notes[0]['tg_chat_id'] == 123
    assert json.loads(notes[0]['message'])['id'] == context['id']


def test_new_reply_supersedes_previous_context(isolated_db, tmp_path):
    agent, old = native_for(tmp_path)
    _, new = native_for(tmp_path, completion_seq=1)
    db.save_native_file_context('source', old)
    db.save_native_file_context('source', new)
    assert db.queue_native_file_manifests('source', [{**agent, 'agent_status': 'done', 'completion_seq': 1}]) == 0
    assert db.queue_native_file_manifests('source', [{**agent, 'agent_status': 'done', 'completion_seq': 2}]) == 1
    assert json.loads(db.get_unsent_notifications()[0]['message'])['id'] == new['id']


def test_native_reply_adds_file_instructions_to_authorized_request(isolated_db, tmp_path, monkeypatch):
    agent, _ = native_for(tmp_path)
    rpc = AsyncMock(side_effect=[{'result': {'agent': agent}}, {'result': True}])
    monkeypatch.setattr(bot, '_herdr_json', rpc)
    monkeypatch.setattr(bot, 'is_authorized', lambda uid: uid == 123)
    monkeypatch.setattr(bot, 'LOG_PROMPTS', False)
    event = SimpleNamespace(effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type='private'),
        message=SimpleNamespace(text='Send the report', reply_text=AsyncMock()))
    context = SimpleNamespace(user_data={'herdr_reply_pane': 'w1:p1'})
    asyncio.run(bot.herdr_reply_got_text(event, context))
    args = rpc.call_args_list[1].args
    assert args[:3] == ('agent', 'prompt', 'w1:p1')
    assert args[3].endswith('Send the report') and '.pp-files.json' in args[3]
    assert db.queue_native_file_manifests(bot._herdr_watch_source('', None), [
        {**agent, 'agent_status': 'done', 'completion_seq': 1}]) == 1


def test_native_manifest_delivers_to_original_chat_once(isolated_db, tmp_path, monkeypatch):
    agent, context = native_for(tmp_path)
    native_manifest(context)
    db.save_native_file_context('source', context)
    db.queue_native_file_manifests('source', [{**agent, 'agent_status': 'done', 'completion_seq': 1}])
    client = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    notify_ticks(monkeypatch, client, 3)
    client.send_document.assert_awaited_once()
    assert client.send_document.call_args.kwargs['chat_id'] == 123
    assert client.send_document.call_args.kwargs['filename'] == 'report.txt'
    client.send_message.assert_not_called()
    assert db.get_unsent_notifications() == []
    notify_ticks(monkeypatch, client, 1)
    assert client.send_document.await_count == 1


def test_native_changed_file_is_not_uploaded(isolated_db, tmp_path):
    _, context = native_for(tmp_path)
    root = native_manifest(context)
    spec = result_files.manifest_specs(context, None)[0]
    (root / 'report.txt').write_bytes(b'changed')
    client = SimpleNamespace(send_document=AsyncMock())
    note = dict(message=json.dumps({**spec, 'endpoint': None, 'expires_at': context['expires_at']}),
                tg_chat_id=123, machine='', task_id=None)
    with pytest.raises(result_files.RejectedFile, match='changed'):
        asyncio.run(bot._send_result_file(client, note))
    client.send_document.assert_not_called()


def test_reply_file_watch_runs_without_general_notifications(isolated_db, tmp_path, monkeypatch):
    agent, context = native_for(tmp_path)
    source = bot._herdr_watch_source('', None)
    db.save_native_file_context(source, context)
    monkeypatch.setattr(bot, '_herdr_watch_targets', lambda: [('', None)])
    monkeypatch.setattr(bot, '_herdr_json', AsyncMock(return_value={'result': {'agents': [
        {**agent, 'agent_status': 'done', 'completion_seq': 1} ]}}))
    ticks = []
    async def sleep(_):
        if ticks:
            raise asyncio.CancelledError()
        ticks.append(True)
    monkeypatch.setattr(bot.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._native_file_watch_loop())
    assert len(db.get_unsent_notifications()) == 1
    assert db.get_unsent_notifications()[0]['tg_chat_id'] == 123


def test_machine_host_check_works_without_posix_uname(monkeypatch):
    monkeypatch.delattr(os, 'uname', raising=False)
    assert bot._is_local_host('user@localhost')
    assert not bot._is_local_host('different-result-host.example')
