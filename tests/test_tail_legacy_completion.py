"""The legacy completion must follow the exact selected review through the public CLI."""
import pytest

from tail_admission_fixture import add_comment, source_fixture
from test_tail_drain_cli import local_cli as local_cli


DONE = "<!-- pp:tail-done -->"
MARKED_AT = "2026-03-01T01:00:00Z"
LATER = "2026-03-01T02:00:00Z"


@pytest.fixture
def legacy(local_cli):
    run, peer = local_cli
    source = source_fixture("legacy")
    # Both the merged review and its later owner edits predate the protocol cutover.
    source["cutover"]["merged_at"] = "2026-03-03T00:00:00Z"
    peer.sources[42] = source
    peer.cutover = source["cutover"]
    return run, peer, source


@pytest.mark.parametrize("change", ["new-review", "later-edit", "same-second-edit"])
def test_stale_plain_done_reopens_the_changed_review(legacy, change):
    run, peer, source = legacy
    review = source["comments"][0]
    old_done = add_comment(source, DONE, created=MARKED_AT)
    old, = run("tail-next")["observations"]
    assert old["completion_kind"] == "legacy"

    new_body = review["body"].replace("Сохранение записи", "Повторное сохранение")
    if change == "new-review":
        # The new review follows the marker even though both have the same second.
        review = add_comment(source, new_body, created=MARKED_AT)
        # Editing the old completion does not renew the original acknowledgement.
        old_done["updated_at"] = LATER
        done_node = next(edge["node"] for edge in source["snapshot"]["edges"]
                         if edge["node"]["id"] == old_done["node_id"])
        done_node["lastEditedAt"] = LATER
    else:
        edited = LATER if change == "later-edit" else MARKED_AT
        review.update(body=new_body, updated_at=edited)
        node = next(edge["node"] for edge in source["snapshot"]["edges"]
                    if edge["node"]["id"] == review["node_id"])
        node.update(body=new_body, lastEditedAt=edited)

    run("tail-gate", "--number", "42", "--version", old["version"], code=2)
    ready, = run("tail-next")["ready"]
    assert ready["review_id"] == str(review["id"])
    assert ready["items"][0]["title"] == "Повторное сохранение"
    assert run("tail-backlog")["counts"] == {"ready": 1}

    # A same-second publication has a server-ordered edge; an edit needs a later second.
    done_at = MARKED_AT if change == "new-review" else "2026-03-01T03:00:00Z"
    add_comment(source, DONE, created=done_at)
    current = run("tail-gate", "--number", "42", "--version", ready["version"])
    assert current["completion_kind"] == "legacy"
    assert run("tail-backlog")["completions"] == {"legacy": 1}
    assert peer.writes == []
