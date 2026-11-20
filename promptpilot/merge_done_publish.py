"""Publish one fully verified ordinary base-sync completion under Native's signed gate."""

import hashlib

from . import merge_done_proof, merge_finish_proof, project_pipeline as pp


def confirm(gh, config, checkpoint, *, working_dir, prepare_source, gate, claim_send, validate_attempt):
    number, head, intent = (checkpoint[key] for key in ("number", "to", "intent"))
    validate_attempt()
    prepared = merge_done_proof.prepare(gh, config, checkpoint, working_dir=working_dir)
    prepare_source()
    validate_attempt()
    final = merge_done_proof.prepare(gh, config, checkpoint, working_dir=working_dir)
    if final != prepared:
        raise pp.PipelineError("ordinary base-sync proof changed across final Source admission")
    body, done, _ = final
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    published = done is None
    if published:
        validate_attempt()
        result = gate()
        if (result.get("action") != "validated" or result.get("repository") != config["repository"]
                or result.get("stage") != "merge" or result.get("mutation_authorized") is not False
                or result.get("target") != {"stage": "integration-merge-recovery", "number": number, "head": head}):
            raise pp.PipelineError("ordinary base-sync final scheduling gate refused")
        validate_attempt()
        if not claim_send(number, head, intent, digest):
            raise pp.PipelineError("ordinary base-sync comment requires receipt readback")
        reply = pp.post_comment(gh, config, number, body)
        validate_attempt()
        record = {key: checkpoint[key] for key in ("repository", "number", "from", "base", "to", "tree", "intent")}
        record.update(body_sha256=digest, proof_kind="legacy-general-done-v1")
        if not isinstance(reply.get("node_id"), str) or not reply["node_id"]:
            raise pp.PipelineError("ordinary base-sync response omitted its comment node ID")
        done = merge_finish_proof.observe_receipt(gh, record, config=config, expected_node_id=reply["node_id"])
        if type(done) is not int or done <= 0 or type(reply.get("id")) is not int or done != reply["id"]:
            raise pp.PipelineError("ordinary base-sync canonical receipt did not confirm the posted comment")
    validate_attempt()
    return {"action": "completed", "stage": "merge-confirmation", "number": number, "head": head,
            "intent": intent, "done": done, "body_sha256": digest, "published": published}
