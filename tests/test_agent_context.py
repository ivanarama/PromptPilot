"""Actual Git context boundary, including staged and untracked private content."""
import hashlib
import os
from pathlib import Path
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


def test_context_does_not_inherit_another_git_directory(project, tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    subprocess.run(["git", "-C", str(foreign), "init", "-b", "main"], check=True, capture_output=True)
    (foreign / "README.md").write_text("Foreign private context", encoding="utf-8")
    subprocess.run(["git", "-C", str(foreign), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(foreign), "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
                    "commit", "--no-gpg-sign", "-m", "Foreign fixture"], check=True, capture_output=True)
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    assert context.read_file(project, "README.md")["text"] == "Committed context: пример\n"


def test_partial_clone_context_never_fetches_missing_objects(project, tmp_path):
    source = Path(project["repository_path"])
    subprocess.run(["git", "-C", str(source), "config", "uploadpack.allowFilter", "true"],
                   check=True, capture_output=True)
    clone = tmp_path / "partial.git"
    subprocess.run(["git", "clone", "--bare", "--filter=blob:none", source.as_uri(), str(clone)],
                   check=True, capture_output=True, timeout=20)
    blob = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD:README.md"]).decode().strip()
    offline = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
    def missing():
        return subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob],
                              env=offline, capture_output=True, timeout=10).returncode != 0
    if not missing():
        pytest.skip("This Git transport did not create a blob-filtered clone")
    with pytest.raises(context.ContextError):
        context.read_file({"repository_path": str(clone), "readable_paths": ["README.md"]}, "README.md")
    assert missing()
