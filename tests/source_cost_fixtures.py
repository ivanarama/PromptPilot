"""Explicit server/checker cost receipts at mocked observation boundaries."""
import copy
import hashlib

from promptpilot import pipeline_insights as pi
from promptpilot.source_graphql_cost import PROTOCOL


def observed_rates(limits):
    lease = getattr(pi._scan_lease_context, "lease", None)
    if lease is not None:
        lease.last_graphql_metadata_cost = 1
    return copy.deepcopy(limits)


def declare_checker(profile, tmp_path):
    profile["github_budget"]["source_cost_protocol"] = PROTOCOL
    executable = tmp_path / "checker"
    executable.write_bytes(b"mocked checker observation boundary")
    contract = tmp_path / "contract.md"
    contract.write_bytes(b"test full checker contract")
    health = profile["health_check"]
    health["command"] = [str(executable.resolve()), "-contract", str(contract.resolve())]
    health["source_graphql_cost"] = {"protocol": PROTOCOL,
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest()}
    profile.setdefault("priority_control", {})["trusted_account"] = "owner"


def checker_receipt(payload):
    lease = getattr(pi._scan_lease_context, "lease", None)
    context = lease.search_budget.health_context
    return dict(payload, exit_code=0, source_graphql_usage={
        **{key: context[key] for key in ("protocol", "repo", "trusted_account", "contract_sha256", "nonce", "allowance")},
        "charged_upper_bound": 2, "observed_actual_cost": 2, "physical_attempts": 2,
        "preview_attempts": 1, "data_attempts": 1, "unproved_attempts": 0,
        "uncertain_cost": False, "complete": True})
