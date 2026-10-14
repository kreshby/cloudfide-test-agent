"""Current-run transport for selected host facts, never rendered comments or code.

A digest binds bytes to trusted producer job outputs. It is not a signature or
an attestation of a hostile producer. The publisher rederives every identity
from GitHub and the exact trusted checkout before accepting these bytes.
"""

from dataclasses import asdict
from pathlib import Path
import re

from .contracts import (ContractError, ExecutionKind, ExecutionResult, FinalHostStatus, HostResult,
                        SourceIdentity, Target, VerifierResult, _text as contract_text)
from .github import integer, sha
from .policy import test_path
from .provider import canonical, digest, strict_json
from .reporting import (DeliveryState, MAX_ADVISORY_BYTES, MAX_COMMENT_BYTES, PatchDelivery, PreparedReport,
                        PublicationIdentity, ReportingError, _MEANINGS, _verification, bind_evidence,
                        inert_text, relevance_warning_lines, report_status)
from .skills import REGISTRY
from .diagnostics import EXPLANATIONS, safe_diagnostic
from .verification import CommandEvidence, Counts, VerificationReport


VERSION = 1
MAX_HANDOFF_BYTES = 192 * 1024
FILE_NAME = "handoff.json"
REASONS = {
    "fork-disabled": "Automatic agent generation is disabled for fork PRs.",
    "docs-only": "No supported frontend or backend behavior target is represented.",
    "source-ci-unsuccessful": "Source Ordinary CI did not succeed; generation was skipped.",
    "repository-unavailable": "The exact public repository requirement no longer holds.",
    "context-unavailable": "Exact source context could not be collected.",
    "checkout-unavailable": "The exact source checkout could not be prepared.",
    "runtime-unavailable": "The selected local model runtime was unavailable.",
    "agent-contract-failed": "The agent did not produce an accepted terminal result.",
    "policy-rejected": "The proposed edits were rejected by trusted policy.",
    "verifier-unavailable": "The verifier could not supply a valid bound result.",
    "artifact-preparation-unavailable": "Required patch preparation was unavailable.",
    "report-unavailable": "A bound final report could not be prepared.",
}
DIAGNOSTIC_REASONS = EXPLANATIONS
FACT_FIELDS = {"status", "reason", "context_sha256", "changes_sha256", "required_edits",
               "patch_prepared", "decisions", "recommendations", "skills", "edits", "verification"}
TOP_FIELDS = {"schema_version", "identity", "source", "source_ci", "trusted_workflow_sha", "facts"}
CI_FIELDS = {"run_id", "run_attempt", "workflow_id", "repository_id", "head_sha", "name", "path",
             "event", "status", "conclusion"}
COMMAND_FIELDS = set(CommandEvidence.__dataclass_fields__) - {"error"}


class HandoffError(ValueError):
    """Fixed errors only; neither remote data nor filesystem paths are echoed."""


def fields(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise HandoffError("handoff schema rejected")
    return value


def hashed(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise HandoffError("handoff digest rejected")
    return value


def artifact_name(run_id, attempt):
    return f"test-agent-handoff-{integer(run_id, 1)}-{integer(attempt, 1)}"


def advisory_text(value):
    """Reuse TestPlan's text contract and T06's stricter display character rules."""
    try:
        contract_text(value, "advisory text")
        inert_text(value, MAX_ADVISORY_BYTES)
    except (ContractError, ReportingError):
        raise HandoffError("advisory text rejected") from None
    return value


def terminal_facts(status, reason):
    expected_status = ("UNSUPPORTED" if reason in {"fork-disabled", "docs-only"} else
                       "AGENT_FAILED" if reason in {"agent-contract-failed", "policy-rejected"} else "UNAVAILABLE")
    if status != expected_status or reason not in REASONS:
        raise HandoffError("terminal outcome rejected")
    return {"status": status, "reason": reason, "context_sha256": None, "changes_sha256": None,
            "required_edits": False, "patch_prepared": False, "decisions": [],
            "recommendations": [], "skills": [], "edits": [], "verification": None}


def execution_facts(bundle, result, verification, publication, *, patch_prepared):
    bound = bind_evidence(bundle, result, verification, publication=publication)
    # Only selected validated plan fields cross as advisory text. Raw provider
    # responses/prompts, trace, tests, process logs and output handles do not.
    verification_data = None
    if verification is not None:
        verification_data = {
            "host_result": asdict(verification.host_result),
            "commands": [{k: v for k, v in asdict(c).items() if k != "error"}
                         | {"has_error": c.error is not None} for c in verification.commands],
        }
    return {"status": report_status(bound),
            "reason": str(safe_diagnostic(result.diagnostic)) if result.state == "failed" else None,
            "context_sha256": bundle.sha256,
            "changes_sha256": bound.changes_sha256,
            "required_edits": bool(result.proposal and result.proposal.edits),
            "patch_prepared": patch_prepared,
            "decisions": [] if result.proposal is None else [
                {"target": str(d.target), "action": str(d.action),
                 "changed_behavior": advisory_text(d.plan.changed_behavior),
                 "rationale": advisory_text(d.plan.rationale)} for d in result.proposal.decisions],
            "recommendations": list(result.recommendations), "skills": [asdict(s) for s in bound.skills],
            "edits": [] if not result.policy else [
                {"target": str(e.target), "path": e.path, "operation": str(e.operation)}
                for e in result.policy.changes.edits], "verification": verification_data}


def _verification_data(value, source, changes_hash):
    fields(value, {"host_result", "commands"})
    host = fields(value["host_result"], set(HostResult.__dataclass_fields__))
    if host["source"] != asdict(source) or type(host["verifier_results"]) is not list or len(host["verifier_results"]) > 8:
        raise HandoffError("verification source rejected")
    records = []
    for obj in host["verifier_results"]:
        fields(obj, set(VerifierResult.__dataclass_fields__))
        records.append(VerifierResult(**{**obj, "kind": ExecutionKind(obj["kind"]),
                                        "result": ExecutionResult(obj["result"])}))
    host_result = HostResult(source, FinalHostStatus(host["status"]), tuple(records), host["schema_version"])
    if type(value["commands"]) is not list or len(value["commands"]) > 9:
        raise HandoffError("command budget rejected")
    commands = []
    for raw in value["commands"]:
        # Version-1 archived handoffs predate structural facts.
        raw = {"structural": [], **raw}
        fields(raw, COMMAND_FIELDS | {"has_error"})
        if type(raw["has_error"]) is not bool or type(raw["reports"]) is not list:
            raise HandoffError("command schema rejected")
        obj = {k: v for k, v in raw.items() if k != "has_error"}
        obj["error"] = "command evidence unavailable" if raw["has_error"] else None
        obj["kind"] = ExecutionKind(obj["kind"])
        if type(obj["structural"]) is not list:
            raise HandoffError("structural facts rejected")
        obj["structural"] = tuple(obj["structural"])
        if obj["counts"] is not None:
            counts = fields(obj["counts"], set(Counts.__dataclass_fields__))
            if type(counts["files"]) is not list or any(type(p) is not list or len(p) != 2 for p in counts["files"]):
                raise HandoffError("count inventory rejected")
            obj["counts"] = Counts(**{**counts, "files": tuple(tuple(p) for p in counts["files"])})
        if any(type(p) is not list or len(p) != 3 for p in obj["reports"]):
            raise HandoffError("report inventory rejected")
        obj["reports"] = tuple(tuple(p) for p in obj["reports"])
        commands.append(CommandEvidence(**obj))
    report = VerificationReport(host_result, changes_hash, None, tuple(commands), None, Path("/nonexistent"))
    _verification(report, source, changes_hash)
    return report


def validate(value):
    fields(value, TOP_FIELDS)
    if type(value["schema_version"]) is not int or value["schema_version"] != VERSION:
        raise HandoffError("handoff version rejected")
    identity_fields = fields(value["identity"], set(PublicationIdentity.__dataclass_fields__))
    if any(v is None for v in identity_fields.values()):
        raise HandoffError("explicit publication and source identity required")
    i = PublicationIdentity(**identity_fields)
    source = SourceIdentity.from_mapping(value["source"])
    ci = fields(value["source_ci"], CI_FIELDS)
    sha(value["trusted_workflow_sha"])
    if (source.repository, source.pr_number, source.base_sha, source.merge_base_sha,
        source.head_sha, source.policy_sha, source.run_id) != (
        i.repository, i.pr_number, i.base_sha, i.merge_base_sha, i.head_sha, i.policy_sha, str(i.source_ci_run_id)):
        raise HandoffError("handoff source identity rejected")
    for key in ("run_id", "run_attempt", "workflow_id", "repository_id"):
        integer(ci[key], 1)
    if ((ci["run_id"], ci["run_attempt"], ci["repository_id"], ci["head_sha"]) !=
        (i.source_ci_run_id, i.source_ci_run_attempt, i.repository_id, i.head_sha)
            or ci["name"] != "Ordinary CI" or ci["path"] != ".github/workflows/ci.yml"
            or ci["event"] != "pull_request" or ci["status"] != "completed"
            or ci["conclusion"] not in {"success", "failure", "cancelled", "timed_out", "neutral",
                                         "skipped", "action_required", "startup_failure", "stale"}):
        raise HandoffError("handoff source CI rejected")
    facts = fields(value["facts"], FACT_FIELDS)
    status, reason = facts["status"], facts["reason"]
    if status not in _MEANINGS or (reason is not None and (
            type(reason) is not str or reason not in REASONS.keys() | DIAGNOSTIC_REASONS.keys())):
        raise HandoffError("handoff status rejected")
    for key in ("required_edits", "patch_prepared"):
        if type(facts[key]) is not bool:
            raise HandoffError("handoff edit flags rejected")
    for key in ("context_sha256", "changes_sha256"):
        if facts[key] is not None:
            hashed(facts[key])
    for key, maximum in (("decisions", 2), ("recommendations", 2), ("skills", 6), ("edits", 8)):
        if type(facts[key]) is not list or len(facts[key]) > maximum:
            raise HandoffError("handoff fact budget rejected")
    targets = set()
    for d in facts["decisions"]:
        fields(d, {"target", "action", "changed_behavior", "rationale"})
        if d["target"] not in {"frontend", "backend"} or d["target"] in targets or d["action"] not in {
            "generate", "update", "no-tests-needed", "unsupported"}:
            raise HandoffError("advisory decision rejected")
        targets.add(d["target"])
        advisory_text(d["changed_behavior"])
        advisory_text(d["rationale"])
    if any(r not in {"accept", "reject", "unavailable"} for r in facts["recommendations"]):
        raise HandoffError("advisory recommendation rejected")
    seen_skills = set()
    for s in facts["skills"]:
        fields(s, {"skill_id", "version", "content_sha256", "loaded_content_sha256", "target", "schema_version"})
        entry = REGISTRY[s["skill_id"]]
        if (s["target"], s["skill_id"]) in seen_skills or (s["version"], s["content_sha256"], s["loaded_content_sha256"]) != (
                entry.version, entry.sha256, entry.sha256) or s["target"] not in entry.targets or type(s["schema_version"]) is not int or s["schema_version"] != 1:
            raise HandoffError("skill facts rejected")
        seen_skills.add((s["target"], s["skill_id"]))
    paths = set()
    for e in facts["edits"]:
        fields(e, {"target", "path", "operation"})
        test_path(e["path"], Target(e["target"]))
        if e["path"] in paths or e["operation"] not in {"create", "update"}:
            raise HandoffError("edit facts rejected")
        paths.add(e["path"])
    for d in facts["decisions"]:
        edits = [e for e in facts["edits"] if e["target"] == d["target"]]
        if ((d["action"] in {"generate", "update"}) != bool(edits)
                or d["action"] == "generate" and any(e["operation"] != "create" for e in edits)
                or d["action"] == "update" and not any(e["operation"] == "update" for e in edits)):
            raise HandoffError("decision and accepted edit facts conflict")
    if any(e["target"] not in targets for e in facts["edits"]):
        raise HandoffError("accepted edits lack advisory decisions")
    if (facts["required_edits"] != bool(paths) or facts["patch_prepared"] and not paths
            or paths and facts["changes_sha256"] is None):
        raise HandoffError("edit flags conflict")
    if reason is not None and facts["context_sha256"] is None:
        if facts != terminal_facts(status, reason):
            raise HandoffError("terminal report contains execution claims")
    elif facts["context_sha256"] is None:
        raise HandoffError("execution facts lack context binding")
    elif reason is not None and (status != "AGENT_FAILED" or reason not in DIAGNOSTIC_REASONS):
        raise HandoffError("execution diagnostic rejected")
    verification = None
    if facts["verification"] is not None:
        if not paths or facts["changes_sha256"] is None:
            raise HandoffError("unexpected verification facts")
        verification = _verification_data(facts["verification"], source, facts["changes_sha256"])
        observed = verification.host_result.status.value.upper().replace("-", "_")
        if status not in {observed, "AGENT_FAILED"}:
            raise HandoffError("status differs from host observation")
    elif status in {"VERIFIED", "BASELINE_FAILED", "VERIFICATION_FAILED", "STALE"}:
        raise HandoffError("status requires verification facts")
    if ci["conclusion"] != "success" and reason not in {"source-ci-unsuccessful", "fork-disabled"}:
        raise HandoffError("unsuccessful CI cannot authorize execution")
    fork = i.head_repository_id != i.repository_id or i.head_repository != i.repository
    if fork != (reason == "fork-disabled"):
        raise HandoffError("fork classification differs from exact identity")
    if status == "NO_TESTS_NEEDED" and (paths or not facts["decisions"] or any(d["action"] != "no-tests-needed" for d in facts["decisions"])):
        raise HandoffError("no-edit terminal facts rejected")
    return i, verification


def encode(identity, source, source_ci, trusted_workflow_sha, facts):
    value = {"schema_version": VERSION, "identity": asdict(identity), "source": asdict(source),
             "source_ci": source_ci, "trusted_workflow_sha": trusted_workflow_sha, "facts": facts}
    raw = canonical(value)
    decode(raw, digest(raw), expected_identity=identity, expected_source_ci=source_ci,
           expected_trusted_sha=trusted_workflow_sha)
    return raw


def decode(raw, expected_digest, *, expected_identity, expected_source_ci, expected_trusted_sha):
    try:
        hashed(expected_digest)
        if type(raw) is not bytes or len(raw) > MAX_HANDOFF_BYTES or digest(raw) != expected_digest:
            raise HandoffError("handoff byte integrity rejected")
        value = strict_json(raw.decode("utf-8"), MAX_HANDOFF_BYTES)
        if canonical(value) != raw:
            raise HandoffError("noncanonical handoff rejected")
        identity, _ = validate(value)
        if identity != expected_identity or value["source_ci"] != expected_source_ci or value["trusted_workflow_sha"] != expected_trusted_sha:
            raise HandoffError("handoff expected identity rejected")
        return value
    except Exception:
        raise HandoffError("handoff validation rejected") from None


def report(value, *, artifact_id=None):
    """Render validated facts, never use a producer's rendered comment as authority."""
    i, verification = validate(value)
    f = value["facts"]
    # artifact_id is supplied exclusively from the trusted upload step output.
    receipt = PatchDelivery(DeliveryState.AVAILABLE, artifact_id) if artifact_id is not None else None
    if receipt is not None and not f["patch_prepared"]:
        raise HandoffError("artifact receipt without prepared patch")
    delivery = receipt or PatchDelivery(DeliveryState.UNAVAILABLE if f["required_edits"] else DeliveryState.NOT_REQUIRED)
    lines = [i.marker(), "## Unit test agent", f"Final status: **{f['status']}** — {_MEANINGS[f['status']]}",
             f"PR head: `{i.head_sha}`", f"Policy: `{i.policy_sha}`",
             f"Source CI run: {i.source_ci_run_id}; attempt: {i.source_ci_run_attempt}",
             f"Agent workflow run: {i.workflow_run_id}; attempt: {i.run_attempt}",
             f"Trusted workflow code: `{value['trusted_workflow_sha']}`", f"[Agent run]({i.run_url})",
             f"[Source CI](https://github.com/{i.repository}/actions/runs/{i.source_ci_run_id})", "",
             "### Agent analysis (advisory)"]
    for d in f["decisions"]:
        lines.extend([f"- {d['target']}: {d['action']}",
                      "  Changed behavior: " + inert_text(d["changed_behavior"], MAX_ADVISORY_BYTES),
                      "  Rationale: " + inert_text(d["rationale"], MAX_ADVISORY_BYTES)])
    if not f["decisions"]:
        lines.append("No accepted advisory decision is supplied.")
    if f["recommendations"]:
        lines.append("Review recommendations (advisory): " + ", ".join(
            inert_text(r, MAX_ADVISORY_BYTES) for r in f["recommendations"]))
    lines.extend(["", "### Host evidence (authoritative)"])
    if f["reason"]:
        lines.append((f"Agent diagnostic: `{f['reason']}` — " + DIAGNOSTIC_REASONS[f["reason"]])
                     if f["context_sha256"] is not None else REASONS[f["reason"]])
    lines.append(f"Source CI conclusion: {value['source_ci']['conclusion']}.")
    if verification is None:
        lines.append("Host verification: no execution evidence supplied.")
    else:
        observed = verification.host_result.status.value.upper().replace("-", "_")
        lines.append(f"Host verification: **{observed}** — {_MEANINGS[observed]}")
        for c in verification.commands:
            counts = "no test count" if c.counts is None else f"collected {c.counts.collected}, passed {c.counts.passed}, failed {c.counts.failed}, errors {c.counts.errors}, skipped {c.counts.skipped}"
            lines.append(f"- {c.profile_id}: {c.kind}; {'passed' if c.passed else 'not passed'}; {counts}; exit {c.exit_code}; timeout {str(c.timed_out).lower()}.")
            lines.extend(relevance_warning_lines(c))
            for ref, size, hashed in c.reports:
                lines.append(f"  Report: {inert_text(ref)}; {size} bytes; SHA-256 `{hashed}`.")
    for s in f["skills"]:
        lines.append(f"- Loaded skill: {inert_text(s['skill_id'])} {inert_text(s['version'])} ({s['target']}); content SHA-256 `{s['content_sha256']}`; loaded SHA-256 `{s['loaded_content_sha256']}`.")
    for e in f["edits"]:
        lines.append(f"- Accepted {e['operation']}: {inert_text(e['path'])}.")
    lines.extend(["", "### Patch delivery", f"Patch delivery: **{delivery.state}**"])
    if delivery.state == DeliveryState.AVAILABLE:
        lines.append(f"[Download patch artifact]({i.run_url}/artifacts/{delivery.artifact_id})")
    elif delivery.state == DeliveryState.UNAVAILABLE:
        lines.append("Delivery of required edits is unresolved; the host verification verdict is unchanged.")
    else:
        lines.append("No patch is required.")
    body = "\n".join(lines) + "\n"
    if len(body.encode()) > MAX_COMMENT_BYTES:
        raise HandoffError("report byte budget exceeded")
    return PreparedReport(i, body, f["status"], delivery)


def successful(prepared):
    return (prepared.status in {"NO_TESTS_NEEDED", "UNSUPPORTED"} and
            prepared.delivery.state == DeliveryState.NOT_REQUIRED) or (
        prepared.status == "VERIFIED" and prepared.delivery.state == DeliveryState.AVAILABLE)
