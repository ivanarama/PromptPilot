"""Historical routing proof/attempt reducer; no network or execution authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .pipeline_errors import PipelineError
from .review_routing_lease import LeaseNode, resolve, timestamp
from .review_routing_wire import Record, context, sha


def _lease_nodes(records, root, *, purpose):
    context_hash = context(root.payload)
    nodes = [LeaseNode(root.id, root.payload["owner"], root.created_at, context_hash, purpose)]
    for record in records:
        data = record.payload
        if data["kind"] != "lease" or data["root"] != root.id:
            continue
        if data["context_sha256"] != context_hash or data["purpose"] != purpose:
            raise PipelineError("routing lease does not bind its immutable root")
        nodes.append(LeaseNode(record.id, data["owner"], record.created_at,
                               context_hash, purpose, data["root"], data["previous"]))
    return nodes


def owner_at(records, root, record, *, purpose):
    prior = [item for item in records if item.order < record.order]
    nodes = _lease_nodes(prior, root, purpose=purpose)
    view = resolve(nodes, context_sha256=context(root.payload), purpose=purpose,
                   now=timestamp(record.created_at))
    view.require_owner(returned_id=record.payload["lease_id"], owner=record.payload["owner"],
                       now=timestamp(record.created_at))
    return view


@dataclass(frozen=True)
class ReviewProof:
    publication: Record
    claim: Record
    completion: Record

    @property
    def source(self):
        from .pr_identity import PRIdentityV1

        data = self.publication.payload
        identity = PRIdentityV1(**data["identity"])
        return {"publication_id": self.publication.id, "claim_id": self.claim.id,
                "completion_id": self.completion.id, "head": identity.head_sha,
                "identity_sha256": identity.sha256, "epoch": data["epoch"],
                "report_sha256": data["report_sha256"]}

    @property
    def outcome(self):
        return self.publication.payload["outcome"]


@dataclass(frozen=True)
class Ledger:
    proofs: tuple[ReviewProof, ...]
    attempts: tuple[Record, ...]
    phases: tuple[Record, ...]
    resumes: tuple[Record, ...]
    ignored_ids: tuple[str, ...]
    parking_id: str | None = None
    resume_id: str | None = None
    resuming_publication_id: str | None = None
    settlements: tuple[Record, ...] = ()

    @property
    def budget_used(self):
        return len(self.attempts)

    def source_proof(self, source):
        return next((proof for proof in self.proofs if proof.source == source), None)

    def attempt_phases(self, attempt):
        return {record.payload["phase"]: record for record in self.phases
                if record.payload["attempt_id"] == attempt.id}

    def attempt_settlement(self, attempt):
        return next((record for record in self.settlements if record.payload['attempt_id'] == attempt.id), None)

    def next_attempt_admission(self, source, *, max_attempts):
        """Current read-only eligibility, separate from a historical outcome.

        A caller must fresh-check effective configuration and all project /
        identity / ownership gates again before reserving anything. An old
        technical label never establishes a currently available budget slot.
        """
        if type(max_attempts) is not int or not 1 <= max_attempts <= 16:
            raise PipelineError('invalid explicit current routing attempt limit')
        proof=self.source_proof(source)
        reason=('engineering parking is active'
                if self.parking_id is not None
                else 'source is not the latest canonical completed REVIEW'
                if proof is None or proof is not self.proofs[-1]
                else 'historical outcome does not authorize a technical attempt'
                if proof.outcome!='technical-handoff'
                else 'same source already reserved its lifetime slot'
                if any(item.payload['source']==source for item in self.attempts)
                else 'current lifetime budget is exhausted'
                if self.budget_used>=max_attempts else None)
        return {'eligible':reason is None,'reason':reason,'budget_used':self.budget_used,
                'current_limit':max_attempts,'historical_outcome':proof.outcome if proof else None,
                'mutation_authorized':False}

    def to_dict(self):
        result = {"budget_used": self.budget_used,
                "series": self.attempts[0].payload["series"] if self.attempts else None,
                "attempt_ids": [item.id for item in self.attempts],
                "proofs": [{"source": proof.source, "outcome": proof.outcome} for proof in self.proofs],
                "phases": [{"id": record.id, **record.payload} for record in self.phases],
                "resume_ids": [item.id for item in self.resumes],
                "active_parking_id": self.parking_id, "active_resume_id": self.resume_id,
                "resuming_publication_id": self.resuming_publication_id,
                "ignored_ids": list(self.ignored_ids), "mutation_authorized": False}
        if self.settlements:
            result['settlements'] = [{'id': record.id, **record.payload} for record in self.settlements]
        return result


def _proofs(records):
    publications, ignored = {}, set()
    for record in records:
        data = record.payload
        if data["kind"] != "publication":
            continue
        key = (data["identity"]["head_sha"], data["epoch"])
        winner = publications.get(key)
        if winner is None:
            publications[key] = record
        elif context(data) != context(winner.payload):
            raise PipelineError("conflicting routing publications for the same HEAD/epoch")
        else:
            ignored.add(record.id)
    proofs = []
    for publication in publications.values():
        context_hash = context(publication.payload)
        claims = []
        completions = []
        for record in records:
            data = record.payload
            if data["kind"] not in {"claim", "completion"}:
                continue
            if data["publication_id"] in ignored:
                ignored.add(record.id)
                continue
            if data["publication_id"] != publication.id:
                continue
            if data["context_sha256"] != context_hash or record.order <= publication.order:
                raise PipelineError("routing proof does not bind its publication/order")
            owner_at(records, publication, record, purpose="review-publication")
            (claims if data["kind"] == "claim" else completions).append(record)
        claim = min(claims, key=lambda record: record.order) if claims else None
        if claim:
            ignored.update(item.id for item in claims if item.id != claim.id)
        for completion in completions:
            if claim is None or completion.payload["claim_id"] != claim.id or completion.order <= claim.order:
                raise PipelineError("routing completion does not bind canonical earliest claim")
        if completions:
            completed = min(completions, key=lambda record: record.order)
            ignored.update(item.id for item in completions if item.id != completed.id)
            proofs.append(ReviewProof(publication, claim, completed))
    known = {item.id for item in records if item.payload["kind"] == "publication"}
    if any(item.payload["kind"] in {"claim", "completion"}
           and item.payload["publication_id"] not in known for item in records):
        raise PipelineError("routing proof publication history is missing")
    return tuple(sorted(proofs, key=lambda proof: proof.completion.order)), ignored


def _phases(records, attempts):
    by_id = {item.id: item for item in attempts}
    result, seen = [], {}
    for record in records:
        data = record.payload
        if data["kind"] != "phase":
            continue
        root = by_id.get(data["attempt_id"])
        if root is None:
            raise PipelineError("routing phase canonical attempt history is missing")
        if data["context_sha256"] != context(root.payload) or record.order <= root.order:
            raise PipelineError("routing phase does not bind its attempt/order")
        owner_at(records, root, record, purpose="technical-attempt")
        key = root.id, data["phase"]
        if key in seen:
            if sha(data) != sha(seen[key].payload):
                raise PipelineError("conflicting routing phase receipts")
            continue
        done = {item.payload["phase"] for item in result if item.payload["attempt_id"] == root.id}
        required = {"handoff-done": "assigned", "started": "handoff-done", "fix-done": "started"}
        if root.payload['executor']['kind']=='author':
            required['fix-done']='handoff-done'
        if data["phase"] in required and required[data["phase"]] not in done:
            raise PipelineError("routing phase prerequisite is missing")
        if "barrier" in done:
            raise PipelineError("routing attempt is parked behind an ambiguity barrier")
        if data["phase"] == "fix-done":
            if data["details"]["source_trailer"] != sha(root.payload["source"]):
                raise PipelineError("routing FIX source trailer differs")
            if data["details"]["new_head"] == root.payload["source"]["head"]:
                raise PipelineError("routing FIX completion did not advance HEAD")
        result.append(record)
        seen[key] = record
    return tuple(result)


def _settlements(records, attempts, phases):
    """History only: the trusted issuer must prove the executor really ended.

    Neither an expired lease nor an absent PID/exit code is such evidence.
    A settlement parks a spent slot; it never fabricates a successful FIX.
    """
    roots = {root.id: root for root in attempts}
    settled = {}
    for record in records:
        data = record.payload
        if data['kind'] != 'settlement':
            continue
        root = roots.get(data['attempt_id'])
        if (root is None or record.order <= root.order or data['source'] != root.payload['source']
                or data['context_sha256'] != context(root.payload)):
            raise PipelineError('settlement lacks its exact canonical attempt/context/source')
        previous = settled.get(root.id)
        if previous is not None:
            if sha(previous.payload) != sha(data):
                raise PipelineError('conflicting attempt settlements')
            continue
        state = {item.payload['phase']: item for item in phases if item.payload['attempt_id'] == root.id}
        if 'fix-done' in state or any(item.order >= record.order for item in state.values()):
            raise PipelineError('settlement cannot replace success or precede a later attempt phase')
        for phase, name in [('barrier', 'barrier_id'), ('started', 'started_id')]:
            if data[name] != (state[phase].id if phase in state else None):
                raise PipelineError('settlement expected barrier/started receipt differs')
        kind = root.payload['executor']['kind']
        expected = 'author-unassigned' if kind == 'author' else 'pipeline-stopped' if 'started' in state else 'not-started'
        if data['executor_outcome'] != expected:
            raise PipelineError('settlement outcome does not confirm this exact old executor')
        owner_at(records, root, record, purpose='technical-attempt')
        settled[root.id] = record
    return tuple(settled.values())


def reduce(records, *, deleted_at=(), max_attempts=2):
    """Rebuild lifetime counters from immutable history, never a local cache.

    A deletion anywhere in the supplied proven history invalidates an empty
    ledger too: the deleted record might have been its only budget reservation.
    The adapter must supply the complete PR history and its deletion fence.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 16:
        raise PipelineError("invalid explicit routing attempt limit")
    if not isinstance(records, (list, tuple)) or len(records) > 4096:
        raise PipelineError("routing lifetime history is incomplete or oversized")
    if any(not isinstance(item, Record) for item in records):
        raise PipelineError("routing history requires validated server records")
    if tuple(deleted_at):
        raise PipelineError("comment deletion makes routing lifetime budget unknown")
    ordered = sorted(records, key=lambda item: item.order)
    if len({item.id for item in ordered}) != len(ordered):
        raise PipelineError("duplicate routing server record ID")
    if len({sha(item.payload["pr"]) for item in ordered}) > 1:
        raise PipelineError("routing history crosses lifetime PR identities")
    proofs, ignored = _proofs(ordered)
    proof_map = {proof.completion.id: proof for proof in proofs}
    attempts, by_previous = [], {}
    for record in ordered:
        data = record.payload
        if data["kind"] != "attempt":
            continue
        proof = proof_map.get(data["source"]["completion_id"])
        if (proof is None or proof.source != data["source"]
                or data["context_sha256"] != context(proof.publication.payload)
                or proof.outcome != "technical-handoff" or record.order <= proof.completion.order):
            raise PipelineError("technical attempt lacks its exact canonical completed proof")
        previous = data["previous"]
        existing = by_previous.get(previous)
        if existing:
            if context(data) != context(existing.payload):
                raise PipelineError("conflicting technical attempt roots/executors")
            ignored.add(record.id)
            continue
        latest=max((item for item in proofs if item.completion.order<record.order),
                   key=lambda item:item.completion.order,default=None)
        if latest is not proof:
            raise PipelineError('technical attempt source is not the latest completed REVIEW')
        if (len(attempts) >= max_attempts or data["attempt"] != len(attempts) + 1
                or previous != (attempts[-1].id if attempts else None)
                or data["series"] != (attempts[0].payload["series"] if attempts else proof.completion.id)):
            raise PipelineError("technical attempt lifetime budget/continuity differs")
        if proof.publication.payload["budget_used"] != len(attempts):
            raise PipelineError("technical attempt source used a stale lifetime budget")
        if (data["executor"]["kind"] == "author"
                and data["executor"]["login"] != proof.publication.payload["author"]["login"]):
            raise PipelineError("author handoff differs from the bound PR author")
        if any(item.payload["source"] == data["source"] for item in attempts):
            raise PipelineError("same routing source cannot reserve another technical attempt")
        attempts.append(record)
        by_previous[previous] = record
    # Every root/renewal must be well formed even on an inactive branch.
    roots = {item.id: item for item in ordered if item.payload["kind"] in {"publication", "attempt"}}
    for item in ordered:
        if item.payload["kind"] == "lease" and item.payload["root"] not in roots:
            raise PipelineError("routing lease root history is missing")
    for root in roots.values():
        purpose = "review-publication" if root.payload["kind"] == "publication" else "technical-attempt"
        resolve(_lease_nodes(ordered, root, purpose=purpose), context_sha256=context(root.payload),
                purpose=purpose, now=datetime.max.replace(tzinfo=timezone.utc))
    phases = _phases([item for item in ordered if item.id not in ignored], attempts)
    settlements = _settlements([item for item in ordered if item.id not in ignored], attempts, phases)
    settled = {item.payload['attempt_id']: item for item in settlements}
    for previous, current in zip(attempts, attempts[1:]):
        completed = next((phase for phase in phases if phase.payload["attempt_id"] == previous.id
                          and phase.payload["phase"] == "fix-done"), None)
        proof = proof_map[current.payload["source"]["completion_id"]]
        settlement = settled.get(previous.id)
        resumed_failure = (settlement is not None and settlement.order < proof.publication.order
                           and proof.publication.payload['resume_id'] is not None)
        if not resumed_failure and (completed is None or completed.order >= proof.publication.order
                or (completed.payload["details"]["new_head"] != current.payload["source"]["head"]
                    and proof.publication.payload['resume_id'] is None)):
            raise PipelineError("next technical attempt lacks completed post-push provenance")
    # A completed REVIEW is itself a side effect, even if it routes no new
    # attempt. Fence *every* later canonical publication, not just the next FIX.
    publications=[item for item in ordered if item.payload['kind']=='publication' and item.id not in ignored]
    for publication in publications:
        preceding=[item for item in attempts if item.order < publication.order]
        if not preceding:
            continue
        previous=preceding[-1]
        completed=next((phase for phase in phases if phase.payload['attempt_id']==previous.id
                        and phase.payload['phase']=='fix-done'),None)
        settlement = settled.get(previous.id)
        resumed_failure = (settlement is not None and settlement.order < publication.order
                           and publication.payload['resume_id'] is not None)
        if not resumed_failure and (completed is None or completed.order >= publication.order
                or (completed.payload['details']['new_head']!=publication.payload['identity']['head_sha']
                    and publication.payload['resume_id'] is None)):
            raise PipelineError("new routing REVIEW lacks completed post-push provenance")
    for proof in proofs:
        historical = sum(item.order < proof.publication.order for item in attempts)
        if proof.publication.payload["budget_used"] != historical:
            raise PipelineError("routing publication budget differs from lifetime reservations")
    resumes = tuple(item for item in ordered if item.payload["kind"] == "resume")
    # Parking is a chronological state, not max(all historical parking). A
    # completed resumed REVIEW consumes/clears the old park; only a new
    # engineering completion can establish another park and another grant.
    publication_ids={item.id for item in publications}
    completion_map={proof.completion.id:proof for proof in proofs}
    active_park=None
    active_resume=None
    resuming_publication=None
    for item in ordered:
        data=item.payload
        if data['kind']=='resume':
            preceding=[root for root in attempts if root.order<item.order]
            previous=preceding[-1].id if preceding else None
            if (active_park is None or data['parking_id']!=active_park
                    or data['previous']!=previous or resuming_publication is not None):
                raise PipelineError('engineering resume lacks current parking/lifetime state')
            settlement = next((entry for entry in settlements if entry.id == active_park), None)
            if settlement is not None and data['head'] == settlement.payload['source']['head']:
                raise PipelineError('settled attempt resume requires a new HEAD; same-HEAD rebaseline is unsupported')
            # Before consumption, a later explicit trusted action can change
            # the chosen exact HEAD. It does not mint another resume slot.
            active_resume=item
        elif data['kind']=='settlement' and item in settlements:
            if active_park is not None or resuming_publication is not None:
                raise PipelineError('settlement cannot replace an existing engineering parking')
            active_park = item.id
            active_resume = None
        elif data['kind']=='publication' and item.id in publication_ids:
            if active_park is None:
                if data['resume_id'] is not None:
                    raise PipelineError('unparked routing REVIEW cannot consume engineering resume')
                continue
            if (active_resume is None or resuming_publication is not None
                    or data['resume_id']!=active_resume.id
                    or active_resume.payload['head']!=data['identity']['head_sha']):
                raise PipelineError('engineering parking requires one exact-HEAD resume before REVIEW')
            resuming_publication=item.id
        elif data['kind']=='completion' and item.id in completion_map:
            proof=completion_map[item.id]
            if active_park is not None:
                if resuming_publication!=proof.publication.id:
                    raise PipelineError('engineering parking appeared before REVIEW completion')
                active_park=None
                active_resume=None
                resuming_publication=None
            elif proof.publication.payload['resume_id'] is not None:
                raise PipelineError('engineering resume was already consumed by another REVIEW')
            if proof.outcome=='engineering-handoff':
                active_park=proof.completion.id
    return Ledger(proofs, tuple(attempts), phases, resumes, tuple(sorted(ignored)),
                  active_park, active_resume.id if active_resume else None,resuming_publication,settlements)
