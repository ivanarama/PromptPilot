"""Finite GitHub transport for the deterministic MERGE confirmation only."""

import json
from pathlib import Path
import secrets
import hashlib

from . import project_pipeline as pp
from .github_observation_budget import ObservationCost
from .source_graphql_cost import health_contract, unique_object, validate_receipt


# Separate allowances keep preparation and queue discovery from spending the
# last POST, direct GET and two full post-publication timeline passes.
PHASES = {"proof": (64, 128), "gate": (20, 64), "publication": (32, 128)}


def reservation_cost(policy):
    return {
        "core": sum(v[0] for v in PHASES.values()),
        "search": 0,
        "graphql": (
            sum(v[1] for v in PHASES.values()) + policy["costs"]["insights"]["graphql"] + 4
        ),
    }  # two live account observations at the final gate


class Transport:
    def __init__(self, profile, gh, fence):
        self.profile = profile
        self.backend = gh
        self.fence = fence
        self.phase = "proof"
        self.rest = {phase: size[0] for phase, size in PHASES.items()}
        self.graphql = {phase: ObservationCost(size[1]) for phase, size in PHASES.items()}
        self.health_allowance = profile["github_budget"]["costs"]["insights"]["graphql"]
        self.health_nonce = None
        self.sent = False
        self.authorized_body = None
        self.actor = health_contract(profile)["trusted_account"]

    def health_command(self, command, working_dir):
        """Match the signed gate's effective executable, arguments and cwd."""
        expected = self.profile["health_check"]
        target = expected["command"]
        health_contract(self.profile)
        directory = Path(expected["working_dir"]).resolve(strict=True)
        if command[1:] != target[1:] or not self.health_allowance:
            raise pp.PipelineError("gate and metered Source checker arguments differ")
        if command[0] == target[0]:
            if Path(working_dir).resolve(strict=True) != directory:
                raise pp.PipelineError("gate and metered checker working directories differ")
        else:
            # The operator's signed gate may use this exact transparent shim.
            # Do not invoke shell code from the wrapper after validating it.
            wrapper = Path(command[0])
            literal = lambda s: "'" + str(s).replace("'", "'\"'\"'") + "'"
            expected_bytes = (
                f'#!/bin/sh\nset -eu\ncd {literal(directory)}\nexec {literal(target[0])} "$@"\n'
            ).encode()
            if (
                not wrapper.is_absolute()
                or wrapper.resolve() != wrapper
                or wrapper.stat().st_size != len(expected_bytes)
                or wrapper.read_bytes() != expected_bytes
            ):
                raise pp.PipelineError(
                    "gate checker wrapper differs from the pinned Source checker"
                )
            # pp.run_health uses the signed health_checkout as cwd. The shim's
            # cwd can differ, so preserve its exact semantics in that case.
        if (
            "-transport" not in target
            or target[target.index("-transport") + 1] != "graphql"
            or "-json" not in target
        ):
            raise pp.PipelineError("confirmation needs the live GraphQL checker contract")
        self.health_nonce = secrets.token_hex(16)
        return [
            *target,
            "-source-graphql-budget",
            str(self.health_allowance),
            "-source-graphql-nonce",
            self.health_nonce,
        ], str(directory)

    def health_result(self, stdout):
        value = json.loads(stdout, object_pairs_hook=unique_object)
        validate_receipt(
            value.get("source_graphql_usage"),
            health_contract(self.profile),
            nonce=self.health_nonce,
            allowance=self.health_allowance,
        )
        return value

    def _rest(self, *args, input_value=None):
        self.fence()
        if self.rest[self.phase] <= 0:
            raise pp.PipelineError("confirmation REST page allowance exhausted")
        self.rest[self.phase] -= 1
        return self.backend.json(
            "api",
            *args,
            **({"input_value": input_value} if input_value is not None else {}),
        )

    def _query(self, payload):
        query = payload["query"]
        end = query.rfind("}")
        if (
            not query.strip().startswith("query")
            or end < 0
            or "ppFinish" in query
            or set(payload) != {"query", "variables"}
        ):
            raise pp.PipelineError("confirmation query shape is unsupported")
        cost = self.graphql[self.phase]
        price = None
        for preview in (True, False):
            self.fence()
            cost.reserve(price)
            suffix = "(dryRun: true)" if preview else ""
            fields = (
                "\nppFinishRate: rateLimit" + suffix + " {cost}\nppFinishActor: viewer {login}\n"
            )
            request = dict(payload, query=query[:end] + fields + query[end:])
            try:
                raw = self.backend.run("api", "graphql", "--input", "-", input_value=request)
                result = json.loads(raw, object_pairs_hook=unique_object)
                data = result["data"]
                actor = data.pop("ppFinishActor", None)
                if result.get("errors", []) != [] or not preview and actor != {"login": self.actor}:
                    raise pp.PipelineError("confirmation GraphQL identity/receipt failed")
                price = data.pop("ppFinishRate")["cost"]
                cost.settle(price)
            except BaseException:
                cost.poison()
                raise
        return result

    def graphql_page(self, owner, name, number, cursor):
        variables = {"owner": owner, "name": name, "number": number, "cursor": cursor}
        return self._query({"query": pp.TIMELINE_QUERY, "variables": variables})

    def json(self, *args, input_value=None):
        if args == ("api", "graphql", "--input", "-") and isinstance(input_value, dict):
            return self._query(input_value)
        if args[:1] != ("api",) or len(args) < 2:
            raise pp.PipelineError("unsupported confirmation transport operation")
        path = args[1]
        if (
            len(args) == 2
            and input_value is None
            and (
                path == "user"
                or path == "repos/" + self.profile["repository"]
                or path.startswith("repos/" + self.profile["repository"] + "/")
            )
        ):
            return self._rest(path)
        if (
            self.phase == "publication"
            and not self.sent
            and self.authorized_body is not None
            and args[2:] == ("--method", "POST", "--input", "-")
            and path == f"repos/{self.profile['repository']}/issues/{self.number}/comments"
            and isinstance(input_value, dict)
            and set(input_value) == {"body"}
            and hashlib.sha256(input_value["body"].encode("utf-8")).hexdigest()
            == self.authorized_body
        ):
            self.sent = True
            return self._rest(*args[1:], input_value=input_value)
        raise pp.PipelineError("unsupported confirmation write or REST options")

    def run(self, *args, **kwargs):
        # Ordinary pending_merge_intents uses gh api --paginate. Walk numbered
        # pages explicitly. A short/empty page proves completion. A cap hit
        # throws before the canonical index advances its watermark.
        prefix = f"repos/{self.profile['repository']}/issues/comments?per_page=100&sort="
        if (
            self.phase != "gate"
            or len(args) != 5
            or args[:2] != ("api", "--paginate")
            or not args[2].startswith(prefix)
            or args[3:] != ("--jq", ".[]")
        ):
            raise pp.PipelineError("unsupported confirmation paginated request")
        rows = []
        page = 1
        while True:
            block = self._rest(args[2] + f"&page={page}")
            if not isinstance(block, list) or len(block) > 100:
                raise pp.PipelineError("incomplete repository comments page")
            rows.extend(json.dumps(row) for row in block)
            if len(block) < 100:
                return "\n".join(rows)
            page += 1
