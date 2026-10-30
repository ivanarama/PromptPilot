"""Finite subprocess fixture: real ASGI /advance with selected crash windows."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from promptpilot import gate_execution, workflows
from promptpilot.api import app


def crash(*_args, **_kwargs):
    os._exit(17)


phase, workflow_id = sys.argv[1:3]
if phase == "before_commit":
    original_claim = gate_execution._claim_gate

    def claim_then_crash(*args):
        original_claim(*args)
        crash()

    gate_execution._claim_gate = claim_then_crash
elif phase == "started":
    workflows._run_gate_commands = crash
elif phase == "effects":
    gate_execution._settle_gate = crash
elif phase == "live_effects":
    from promptpilot import process_tree
    original_start = process_tree.OwnedProcess.start

    def start_then_crash(*args, **kwargs):
        tree = original_start(*args, **kwargs)
        deadline = time.monotonic() + 10
        while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
            time.sleep(.05)
        # Deliberately lose the coordinator with its owned command still live.
        # The command is finite even on POSIX, where coordinator death is not
        # proof that the process group has terminated.
        crash(tree)

    process_tree.OwnedProcess.start = start_then_crash
elif phase == "settled":
    gate_execution._project_settled_gate = crash
elif phase == "applied":
    original_projection = gate_execution._project_settled_gate

    def apply_then_crash(*args):
        original_projection(*args)
        crash()

    gate_execution._project_settled_gate = apply_then_crash


async def advance():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8420") as client:
        response = await client.post(f"/api/workflows/{workflow_id}/advance")
        print(json.dumps({"code": response.status_code, "body": response.json()}), flush=True)


asyncio.run(advance())
