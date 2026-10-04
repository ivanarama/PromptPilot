# -*- coding: utf-8 -*-
"""F5: политика план-пилота — gate-канон миссии неприкосновенен."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "addons"))
from plan_pilot import validate_amendment, DEFAULT_GATE_CANON


def test_gate_canon_missing_check_blocks():
    ok, why = validate_amendment(
        "prm-full-service-v1",
        old_codes=["S01", "S02", "S27", "S30"],
        new_codes=["S01", "S02", "S03"],
        new_gates_text="uv run ... verify_stage.py S03",
    )
    assert not ok and "S27" in why


def test_gate_canon_present_allows():
    ok, why = validate_amendment(
        "prm-full-service-v1",
        old_codes=["S01", "S27", "S28", "S30"],
        new_codes=["S01", "S27", "S28", "S29A", "S30"],
        new_gates_text=("... verify_stage.py S27 ... verify_stage.py S28 "
                        "... verify_stage.py S30"),
    )
    assert ok, why


def test_unknown_slug_has_no_canon():
    ok, _ = validate_amendment("other-mission", [], [], "")
    assert ok
