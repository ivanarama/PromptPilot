from promptpilot.pipeline_board import build_board


def test_board_keeps_unknown_classification_and_separates_closed_issues():
    delivery = {"exact": True, "merged_prs": [
        {"number": 1, "title": "feat: pipeline product docs"}, {"number": 2}],
        "closed_issues": [{"number": 3}]}
    board = build_board(delivery, {}, [], available=True, classifications={
        "2": {"category": "pipeline", "evidence": "operator audit: changed scheduler"}})
    groups = {row["id"]: row for row in board["delivery_groups"]}
    assert groups["product"]["count"] == 0
    assert groups["pipeline"]["count"] == 1
    assert groups["unclassified"]["items"][0]["number"] == 1
    assert not board["classification_complete"]
    assert delivery["merged_prs"][1] == {"number": 2}


def test_board_stale_diagnostics_cannot_claim_no_blockers_or_decisions():
    board = build_board({"exact": False}, {
        "findings": [{"severity": "red", "code": "error"}],
        "human_waiting": [{"number": 7}],
    }, [{"status": "running", "task_id": 8}], available=False)
    assert board["available"] is False
    assert board["working"][0]["task_id"] == 8
    assert board["blocked"] == board["human_waiting"] == []
    assert all(group["count"] is None for group in board["delivery_groups"])


def test_board_does_not_describe_owner_barrier_as_error():
    board = build_board({"exact": True}, {"findings": [
        {"severity": "yellow", "code": "single_flight_barrier", "pr": 1},
        {"severity": "red", "code": "claim_invalid", "pr": 2, "message": "broken proof"},
    ]}, [], available=True)
    assert [row["number"] for row in board["blocked"]] == [2]
    assert board["ci_available"] is False
