import sys
from unittest.mock import MagicMock

# The tray dependencies are intentionally optional in the lean test install.
# Process supervision itself does not need a desktop, so provide import-only
# stand-ins instead of skipping these tests on headless CI runners.
sys.modules.setdefault("pystray", MagicMock())
sys.modules.setdefault("PIL", MagicMock())

from promptpilot import tray


class FakeProcess:
    def __init__(self, running=True):
        self.running = running
        self.terminated = False

    def poll(self):
        return None if self.running else 1

    def terminate(self):
        self.running = False
        self.terminated = True


def test_supervisor_restarts_unexpectedly_exited_service(monkeypatch):
    old_procs, old_desired = tray._procs, tray._desired
    tray._procs, tray._desired = {"worker": FakeProcess(False)}, {"worker"}
    started = []
    monkeypatch.setattr(
        tray.subprocess, "Popen",
        lambda command, **_kwargs: started.append(command) or FakeProcess(True),
    )
    try:
        assert tray._supervise_once()
        assert started == [tray._cmd("worker")]
        assert tray._is_running("worker")
    finally:
        tray._procs, tray._desired = old_procs, old_desired


def test_manually_stopped_service_is_not_restarted(monkeypatch):
    old_procs, old_desired = tray._procs, tray._desired
    process = FakeProcess(True)
    tray._procs, tray._desired = {"worker": process}, {"worker"}
    monkeypatch.setattr(
        tray.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not restart")),
    )
    try:
        tray._stop("worker")
        assert process.terminated
        assert not tray._supervise_once()
    finally:
        tray._procs, tray._desired = old_procs, old_desired
