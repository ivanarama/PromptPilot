"""Validate the optional, artifact-bound successful checker cost receipt."""
import hashlib
import json
import re
from pathlib import Path

PROTOCOL = "source-graphql-cost-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:raise ValueError("duplicate Source checker JSON field")
        result[key]=value
    return result


def health_contract(profile):
    health = profile.get("health_check") or {}
    declaration = health.get("source_graphql_cost")
    if not isinstance(declaration,dict) or declaration.get("protocol") != PROTOCOL:
        raise ValueError("fresh health checker has no supported Source cost protocol")
    command = health.get("command")
    if not isinstance(command,list) or not command or not all(isinstance(v,str) and v for v in command):
        raise ValueError("invalid health checker command")
    executable = Path(command[0])
    pin = declaration.get("executable_sha256")
    if (not executable.is_absolute() or executable.resolve()!=executable
            or not isinstance(pin,str) or re.fullmatch("[0-9a-f]{64}",pin) is None
            or hashlib.sha256(executable.read_bytes()).hexdigest()!=pin):
        raise ValueError("immutable Source cost checker executable changed")
    account = (profile.get("priority_control") or {}).get("trusted_account")
    if not isinstance(account,str) or not account:
        raise ValueError("Source cost checker requires its explicit trusted account")
    contract = declaration.get("contract_sha256")
    if not isinstance(contract,str) or re.fullmatch("[0-9a-f]{64}",contract) is None:
        raise ValueError("Source cost checker requires an explicit pinned contract SHA")
    if any(arg.split("=",1)[0].lstrip("-") in ("source-graphql-budget","source-graphql-nonce") for arg in command[1:]):
        raise ValueError("Source cost checker flags must be supplied by this observation")
    contracts = [index for index,arg in enumerate(command) if arg in ("-contract","--contract")]
    if len(contracts)!=1 or contracts[0]+1>=len(command):
        raise ValueError("Source cost checker requires one explicit contract file")
    path=Path(command[contracts[0]+1])
    if not path.is_absolute():path=Path(health.get("working_dir") or Path.cwd())/path
    actual=hashlib.sha256(path.read_bytes()).hexdigest()
    if contract!=actual:raise ValueError("health contract changed")
    return {"protocol":PROTOCOL,"executable_sha256":pin,"contract_sha256":contract,
            "repo":profile["repository"],"trusted_account":account}


def _validate(receipt, expected, *, nonce, allowance, refusal):
    if not isinstance(receipt,dict):raise ValueError("Source checker cost receipt missing")
    required={"protocol","nonce","allowance","charged_upper_bound","observed_actual_cost",
        "physical_attempts","preview_attempts","data_attempts","unproved_attempts",
        "uncertain_cost","complete","repo","trusted_account","contract_sha256"}
    if refusal:required.add("reason")
    if set(receipt)!=required:
        raise ValueError("successful Source checker cost receipt has unexpected fields")
    for key in ("protocol","repo","trusted_account","contract_sha256"):
        if receipt.get(key)!=expected[key]:raise ValueError("Source checker cost receipt context differs")
    if (not isinstance(nonce,str) or re.fullmatch("[0-9a-f]{32}",nonce) is None
            or type(allowance)is not int or not 0<allowance<=2147483647
            or receipt.get("nonce")!=nonce or receipt.get("allowance")!=allowance
            or type(receipt.get("allowance"))is not int):
        raise ValueError("Source checker cost receipt attempt/cap differs")
    if receipt.get("complete")is not (not refusal) or receipt.get("uncertain_cost")is not False:
        raise ValueError("Source checker cost is unproved")
    if refusal and receipt.get("reason")!="allowance_exhausted":
        raise ValueError("Source checker refusal is not a clean allowance denial")
    names=("charged_upper_bound","observed_actual_cost","physical_attempts","preview_attempts","data_attempts","unproved_attempts")
    if any(type(receipt.get(k))is not int or receipt[k]<0 for k in names):
        raise ValueError("invalid Source checker cost receipt counter")
    if any(receipt[k]>10000 for k in ("physical_attempts","preview_attempts","data_attempts")):
        raise ValueError("Source checker physical attempt limit exceeded")
    charge=receipt["charged_upper_bound"]
    preview,data=receipt["preview_attempts"],receipt["data_attempts"]
    balanced = (preview in (data,data+1)) if refusal else (preview==data and preview>0)
    if (charge!=receipt["observed_actual_cost"] or charge>allowance or receipt["unproved_attempts"]!=0 or not balanced
            or receipt["physical_attempts"]<=0
            or receipt["physical_attempts"]!=receipt["preview_attempts"]+receipt["data_attempts"]
            or charge<receipt["physical_attempts"]):
        raise ValueError("Source checker cost receipt is incomplete or exceeds allowance")
    return charge


def validate_receipt(receipt, expected, *, nonce, allowance):
    return _validate(receipt,expected,nonce=nonce,allowance=allowance,refusal=False)


def validate_refusal(receipt, expected, *, nonce, allowance):
    return _validate(receipt,expected,nonce=nonce,allowance=allowance,refusal=True)


def checker_miss(profile, report, observed_at, ttl):
    diagnostics = report["diagnostics"]
    stamp = report["diagnostics_generated_at"]
    if type(stamp) not in (int, float) or not 0 <= stamp <= observed_at:
        return "checker_proof_invalid"
    if observed_at - stamp >= ttl:
        return "checker_expired"
    if type(diagnostics.get("exit_code")) is not int or diagnostics["exit_code"] != 0:
        return "checker_failed"
    usage = report["source_search_usage"]
    if usage["successful_observation_cost_proved"] is not True or usage["uncertain_cost"] is not False:
        return "checker_proof_invalid"
    contract = health_contract(profile)
    receipt = diagnostics["source_graphql_usage"]
    validate_receipt(receipt, contract, nonce=receipt["nonce"], allowance=receipt["allowance"])
    proof = {"executable_sha256": contract["executable_sha256"], "receipt_sha256": digest(receipt)}
    if (diagnostics["source_graphql_cost_proof"] != proof
            or any(usage["health_cost_provenance"][key] != value for key, value in proof.items())):
        return "checker_proof_invalid"
    return None
