import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "install_merge_resolution", Path(__file__).parents[1] / "tools" / "install_merge_resolution.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_install_requires_exact_owner_decision_and_preserves_existing_pointer(tmp_path):
    source, base, tree = (char * 40 for char in "abc")
    conflict = {key: "100644 blob " + val for key, val in
                (("source", source), ("main", base), ("resolved", tree))}
    raw = json.dumps(dict(repository="ivanarama/onebase", pull_request=97, conflicts={"go.sum": conflict},
                          source=source, main=base, tree=tree)).encode()
    digest = hashlib.sha256(raw).hexdigest()
    body = " ".join([source, base, tree, digest])
    pins = [(421, hashlib.sha256(body.encode()).hexdigest())]
    comment = dict(id=421, user={"login": "ivanarama"}, author_association="OWNER",
                   issue_url="https://api.github.com/repos/ivanarama/onebase/issues/97",
                   body=body, created_at="t1", updated_at="t1")
    state = tmp_path.resolve()
    with pytest.raises(ValueError, match="differs"):
        tool.install(state, raw, digest, pins, read=lambda _: dict(comment, updated_at="t2"))
    with pytest.raises(ValueError, match="timeline order"):
        tool.install(state, raw, digest, [*pins, (420, pins[0][1])],
                     read=lambda cid: dict(comment, id=cid))
    with pytest.raises(ValueError, match="1 MiB"):
        tool.install(state, b" " * (1048576 + 1), digest, pins, read=lambda _: comment)
    assert not (state / "merge-resolutions").exists()
    path = tool.install(state, raw, digest, pins, read=lambda _: comment)
    assert json.loads(path.read_bytes()) == {
        "version": 1, "approvals": [{"id": 421, "body_sha256": pins[0][1]}], "resolution_sha256": digest}
    assert (path.parent / (digest + ".json")).read_bytes() == raw
    assert tool.install(state, raw, digest, pins, read=lambda _: comment) == path
    path.write_bytes(b"prior pointer")
    with pytest.raises(ValueError, match="conflicting"):
        tool.install(state, raw, digest, pins, read=lambda _: comment)
    assert path.read_bytes() == b"prior pointer"
