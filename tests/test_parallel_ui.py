from pathlib import Path

from tools.addons.parallel_orchestrator.ui_server import UiRunner


def test_ui_runner_validates_visual_plan():
    runner = UiRunner()
    result = runner.validate(
        {
            "name": "visual",
            "max_parallel": 2,
            "nodes": [
                {"id": "api", "prompt": "inspect api", "working_dir": "C:/repo", "mode": "worktree"},
                {"id": "ui", "prompt": "inspect ui", "working_dir": "C:/repo", "mode": "worktree"},
                {
                    "id": "review",
                    "kind": "review",
                    "review": True,
                    "prompt": "review",
                    "working_dir": "C:/repo",
                    "mode": "worktree",
                    "depends_on": ["api", "ui"],
                },
            ],
        }
    )
    assert result["node_count"] == 3
    assert result["waves"] == [["api", "ui"], ["review"]]


def test_visual_page_contains_drag_drop_and_run_controls():
    page = Path(__file__).parents[1] / "tools" / "addons" / "parallel_orchestrator" / "ui" / "index.html"
    html = page.read_text(encoding="utf-8")
    for marker in ("Перетащите блоки", "id=\"canvas\"", "id=\"runBtn\"", "id=\"fileInput\""):
        assert marker in html


def test_existing_promptpilot_app_exposes_same_origin_parallel_panel():
    from promptpilot.api import app

    paths = set(app.openapi()["paths"])
    assert "/parallel" in paths
    assert "/api/parallel/config" in paths
    assert "/api/parallel/run" in paths

    index = Path(__file__).parents[1] / "promptpilot" / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert 'onclick="openParallel()"' in source
    assert 'src="/parallel"' in source
