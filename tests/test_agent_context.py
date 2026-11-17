"""Actual Git context boundary, including staged and untracked private content."""
import hashlib
import subprocess

import pytest

from promptpilot import agent_context as context


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs-private").mkdir()
    for name, raw in {"README.md": "Committed context: пример\n".encode(),
                      "docs/guide.md": b"A guide\n",
                      "docs-private/secret.md": b"Not granted\n",
                      "binary.bin": b"\0secret",
                      "large.md": b"x" * (context.MAX_FILE_BYTES + 1)}.items():
        (root / name).write_bytes(raw)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)
    git("init", "-b", "main")
    git("add", ".")
    git("-c", "user.name=Context fixture", "-c", "user.email=test@example.invalid",
        "commit", "--no-gpg-sign", "-m", "Context fixture")
    (root / "README.md").write_text("Private uncommitted replacement", encoding="utf-8")
    (root / "untracked.md").write_text("Private untracked file", encoding="utf-8")
    return {"repository_path": str(root), "readable_paths": ["README.md", "docs/"]}


def test_context_reads_committed_bytes_and_paginates(project):
    first = context.read_file(project, "README.md", 0, 10)
    second = context.read_file(project, "README.md", first["next_offset"])
    raw = "Committed context: пример\n".encode()
    assert first["text"] + second["text"] == raw.decode()
    assert second["eof"] and first["sha256"] == hashlib.sha256(raw).hexdigest()
    assert first["head"] == second["head"]
    assert context.read_file(project, "docs/guide.md")["text"] == "A guide\n"


@pytest.mark.parametrize("name", ["../README.md", "/etc/passwd", "C:/secret", "docs\\guide.md",
                                  "docs/../README.md", "docs//guide.md", "README.md\0", "README.md\n"])
def test_context_rejects_path_traversal(project, name):
    with pytest.raises(context.ContextError):
        context.read_file(project, name)


@pytest.mark.parametrize("name", ["docs-private/secret.md", "untracked.md", ".git/config"])
def test_context_respects_policy_and_does_not_read_untracked(project, name):
    if name == "untracked.md":
        project["readable_paths"].append(name)
    with pytest.raises(context.ContextError):
        context.read_file(project, name)


@pytest.mark.parametrize("name", ["binary.bin", "large.md"])
def test_context_bounds_and_text_policy(project, name):
    project["readable_paths"].append(name)
    with pytest.raises(context.ContextError):
        context.read_file(project, name)


@pytest.mark.parametrize("offset,length", [(True, 1), (0, True), (-1, 1), (0, 0), (10000, 1), (0, 16385)])
def test_context_validates_slices(project, offset, length):
    with pytest.raises(context.ContextError):
        context.read_file(project, "README.md", offset, length)
