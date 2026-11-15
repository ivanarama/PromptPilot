"""Background herdr probes must not open Windows console windows."""
import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot, herdr_exec


@pytest.mark.parametrize('platform,flags', [('nt', 0x08000000), ('posix', 0)])
@pytest.mark.parametrize('host', [None, 'remote'])
def test_bot_hides_local_cli_and_ssh_consoles(monkeypatch, platform, flags, host):
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{}', b'')))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(bot.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(herdr_exec, 'herdr_argv', lambda args, host: ['cli', *args])
    with monkeypatch.context() as patch:
        patch.setattr(bot.os, 'name', platform)
        patch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
        result = asyncio.run(bot._herdr_cli('agent', 'list', host=host))
    assert result == '{}'
    assert spawn.call_args.kwargs['creationflags'] == flags
    assert spawn.call_args.kwargs['stdout'] == asyncio.subprocess.PIPE


@pytest.mark.parametrize('platform,flags', [('nt', 0x08000000), ('posix', 0)])
@pytest.mark.parametrize('host', [None, 'remote'])
def test_worker_hides_local_cli_and_ssh_consoles(monkeypatch, platform, flags, host):
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout='{}', stderr='')

    monkeypatch.setattr(herdr_exec, 'run_owned', run)
    monkeypatch.setattr(herdr_exec, 'herdr_argv', lambda args, host: ['cli', *args])
    with monkeypatch.context() as patch:
        patch.setattr(herdr_exec.os, 'name', platform)
        patch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
        result = herdr_exec._run(['agent', 'list'], host=host)
    assert result == (0, {}, '{}')
    assert calls[0]['creationflags'] == flags
    assert calls[0]['capture_output'] is True
