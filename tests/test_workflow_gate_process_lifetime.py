"""A gate's wrapper exit must not leave descendants alive or pipes unbounded."""
import base64
import os
import shlex
import subprocess
import sys
import time

import pytest

from promptpilot import workflows
from promptpilot.models import GateVerdict, WorkflowCreate
from test_process_tree import _stop_unrelated, _wait_not_running


def gate_command(source):
    encoded = base64.b64encode(source.encode('utf-8')).decode('ascii')
    argv = [sys.executable, '-c', f"exec(__import__('base64').b64decode('{encoded}'))"]
    if os.name == 'nt':
        return '& ' + ' '.join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)


@pytest.mark.parametrize('inherit_output', [True, False])
def test_real_gate_owns_descendants_after_wrapper_exit(isolated_db, tmp_path, inherit_output):
    pid_file = tmp_path/'gate-child.pid'
    output = '' if inherit_output else ', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL'
    source = (
        'import subprocess, sys; from pathlib import Path; '
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']"
        + output + '); '
        + f'Path({str(pid_file)!r}).write_text(str(child.pid), encoding="ascii")'
    )
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug='gate-process-lifetime', objective='Bound the real gate process tree',
        repository_path=str(tmp_path), candidate_branch='feature/gate-lifetime',
        config={'gate': {'enabled': True, 'timeout_seconds': 2,
                         'commands': [gate_command(source)]}}))
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    descendant = None
    try:
        started = time.monotonic()
        decision = workflows._run_gate_commands(workflow)
        elapsed = time.monotonic() - started
        assert pid_file.exists(), 'gate wrapper did not start the fixture child'
        descendant = int(pid_file.read_text(encoding='ascii'))
        assert elapsed < 7, f'gate timeout exceeded its process-tree bound: {elapsed}'
        assert decision.verdict == (GateVerdict.FAIL if inherit_output else GateVerdict.PASS)
        assert _wait_not_running(descendant, timeout=3), 'gate descendant survived wrapper completion'
        assert unrelated.poll() is None, 'gate cleanup selected an unrelated process'
    finally:
        # Even the deliberately failing pre-fix run owns only a finite sleeper.
        # Wait for that exact fixture child rather than killing a reused PID.
        try:
            if descendant is None and pid_file.exists():
                descendant = int(pid_file.read_text(encoding='ascii'))
            if descendant is not None:
                assert _wait_not_running(descendant, timeout=12)
        finally:
            _stop_unrelated(unrelated)
