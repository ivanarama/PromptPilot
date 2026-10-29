import copy
import hashlib
import json
from pathlib import Path

import pytest

from promptpilot import project_pipeline as pp
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing import MAX_REPORT_BYTES, inspect_file, inspect_report


FIXTURE = Path(__file__).parent / "fixtures/review-routing-v1.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
HEAD = "a" * 40


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_golden_reports_have_explicit_categories_and_exact_utf8_hash(case):
    result = inspect_report(case["report"], HEAD)
    assert result["classification"] == case["classification"]
    assert result["canonical_record"] == case["canonical_record"]
    assert result["report_sha256"] == case["sha256"]
    assert hashlib.sha256(result["canonical_record"].encode("utf-8")).hexdigest() == case["sha256"]
    assert result["authority"] == "unverified-input"
    assert result["mutation_authorized"] is result["handoff_enabled"] is False
    assert result["canonical_record"].endswith("\n")
    assert "\r" not in result["canonical_record"]


def test_public_offline_cli_never_reads_config_health_or_github(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("offline report validation must not access config, health or GitHub")

    monkeypatch.setattr(pp, "load_config", forbidden)
    monkeypatch.setattr(pp, "GitHub", forbidden)
    monkeypatch.setattr(pp, "run_health", forbidden)
    report = tmp_path / "report.json"
    for case in CASES:
        report.write_text(json.dumps(case["report"], ensure_ascii=False), encoding="utf-8")
        assert pp.run(["validate-routing-report", "--report", str(report), "--head", HEAD]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["report_sha256"] == case["sha256"]
        assert result["mutation_authorized"] is False


@pytest.mark.parametrize("path,value", [
    (("routing_version",), "review-routing-v2"),
    (("head",), "b" * 40),
    (("checks",), []),
    (("checks",), [" "]),
    (("change",), "invalid\ud800"),
    (("blocking", 0, "kind"), "unknown"),
    (("blocking", 0, "kind"), "tool_error"),
    (("blocking", 0, "kind"), ["technical"]),
    (("blocking", 0, "evidence", "entrypoint"), ""),
    (("blocking", 0, "evidence", "command"), None),
    (("blocking", 0, "evidence", "result"), "\0"),
    (("blocking", 0, "evidence", "invariant"), ""),
    (("blocking", 0, "id"), "summary inferred id"),
    (("blocking", 0, "id"), True),
    (("tail",), [{"kind": "issue", "text": "missing title"}]),
])
def test_incomplete_or_unknown_input_fails_closed(path, value):
    report = copy.deepcopy(CASES[0]["report"])
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(PipelineError):
        inspect_report(report, HEAD)


def test_categories_are_not_inferred_from_prose_and_mixed_never_becomes_technical():
    report = copy.deepcopy(CASES[1]["report"])
    report["blocking"][0]["summary"] = "purely technical bug, please fix automatically"
    assert inspect_report(report, HEAD)["classification"] == "product"
    report["blocking"].append(copy.deepcopy(CASES[0]["report"]["blocking"][0]))
    assert inspect_report(report, HEAD)["classification"] == "mixed_blockers"


@pytest.mark.parametrize("number", [True, 0, 2147483648, 1.5, "1"])
def test_product_choices_require_portable_unique_integer_numbers(number):
    report = copy.deepcopy(CASES[1]["report"])
    report["blocking"][0]["evidence"]["options"][0]["number"] = number
    with pytest.raises(PipelineError):
        inspect_report(report, HEAD)


def test_product_options_recommendation_and_blocker_ids_cannot_be_ambiguous():
    original = CASES[1]["report"]
    for field in ("options", "recommendation"):
        report = copy.deepcopy(original)
        evidence = report["blocking"][0]["evidence"]
        if field == "options":
            evidence[field][1]["number"] = evidence[field][0]["number"]
        else:
            evidence[field]["number"] = 9
        with pytest.raises(PipelineError):
            inspect_report(report, HEAD)
    report = copy.deepcopy(original)
    report["blocking"] *= 2
    with pytest.raises(PipelineError):
        inspect_report(report, HEAD)


def test_report_hash_binds_category_evidence_head_and_exact_unicode_without_normalization():
    original = CASES[0]["report"]
    digest = inspect_report(original, HEAD)["report_sha256"]
    altered = copy.deepcopy(original)
    altered["blocking"][0]["evidence"]["actual"] += " changed output"
    assert inspect_report(altered, HEAD)["report_sha256"] != digest
    altered = copy.deepcopy(original)
    altered["head"] = "b" * 40
    assert inspect_report(altered, "b" * 40)["report_sha256"] != digest
    altered["change"] = "é"
    composed = inspect_report(altered, "b" * 40)["report_sha256"]
    altered["change"] = "e\u0301"
    assert inspect_report(altered, "b" * 40)["report_sha256"] != composed


@pytest.mark.parametrize("data", [
    b'\xef\xbb\xbf{}', b'\xff', b'{"head":1,"head":2}',
    b'{"head":NaN}', b'{"head":Infinity}', b'[]', b'{}',
    b' ' * (MAX_REPORT_BYTES + 1),
    b'[' * 5000 + b'0' + b']' * 5000,
], ids=["bom", "invalid-utf8", "duplicate-key", "nan", "infinity", "array", "empty", "oversized",
        "excessive-nesting"])
def test_public_cli_rejects_ambiguous_invalid_and_oversized_json(tmp_path, capsys, data):
    report = tmp_path / "report.json"
    report.write_bytes(data)
    assert pp.run(["validate-routing-report", "--report", str(report), "--head", HEAD]) == 2
    assert json.loads(capsys.readouterr().out)["action"] == "error"


def test_unknown_fields_and_missing_category_are_not_silently_dropped(tmp_path):
    for mutate in (lambda report: report.update(extra="not committed separately"),
                   lambda report: report["blocking"][0].pop("kind")):
        report = copy.deepcopy(CASES[0]["report"])
        mutate(report)
        path = tmp_path / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(PipelineError):
            inspect_file(str(path), HEAD)


def test_versioned_report_cannot_enter_legacy_public_completion(tmp_path, monkeypatch, capsys):
    config = tmp_path / "pipelinectl.json"
    config.write_text(json.dumps({"repository": "owner/repo", "trusted_account": "owner",
                                  "health_command": ["must-not-run"]}), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(CASES[0]["report"]), encoding="utf-8")
    lease = pp.encode_lease({"version": 1, "stage": "review", "repository": "owner/repo",
                             "number": 42, "head": HEAD, "snapshot": "c" * 64,
                             "epoch": "d" * 64, "anchor": "anchor", "depth": 0,
                             "completion_gate": "health"})

    class NoRequests:
        timeout_seconds = 0

        def json(self, *args, **kwargs):
            pytest.fail("unsupported report must be rejected before GitHub requests")

    monkeypatch.setattr(pp, "GitHub", NoRequests)
    assert pp.run(["--config", str(config), "complete", "review", "--lease", lease,
                   "--report", str(report)]) == 2
    assert "future compatible completion" in json.loads(capsys.readouterr().out)["error"]


def test_legacy_report_keeps_its_existing_outcome():
    report = {"change": "legacy", "checks": ["pytest"], "blocking": [], "tail": []}
    lease = {"head": HEAD, "epoch": "b" * 64, "depth": 0}
    _, outcome = pp.format_review(lease, report)
    assert outcome == "reviewed"
