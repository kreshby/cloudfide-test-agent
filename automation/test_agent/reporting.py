"""Bounded host reports. Model analysis never supplies execution facts.

All inputs are host objects from the trusted installation, not deserialized PR
artifacts. These types validate bindings; they do not authenticate a Python caller.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum
import math
import re
import string
import unicodedata

from .agent import (AgentResult, State, UNIT_TEST_AGENT, MAX_TRACE_BYTES,
                    MAX_TRACE_EVENTS, MAX_PROVIDER_CALLS)
from .context import ContextBundle, WorkflowIdentity
from .contracts import (EditOperation, ExecutionKind, ExecutionResult,
                        FinalHostStatus, HostResult, ModelAction, ModelProposal,
                        PolicyDecision, SkillUseRecord, SourceIdentity, Target)
from .github import CollectionError, integer, repository, sha
from .patches import PolicyAssessment, TrustedChanges
from .policy import (CREATE_FILE_MODE, MAX_EDITED_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES,
                     SUPPORTED_FILE_MODES, ValidatedEdit, normalized_path, test_path)
from .provider import (COMPLETION_TOKENS, MAX_REQUEST_BYTES, Stage, canonical,
                       digest, strict_json)
from .skills import REGISTRY
from .verification import CommandEvidence, Counts, VerificationReport
from automation.verifier.profiles import PROFILES


MARKER = "taskly-unit-test-agent-report:v1"
MAX_COMMENT_BYTES = 48 * 1024
MAX_ADVISORY_BYTES = 2 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MARKER = re.compile(
    r"<!-- " + MARKER + r" repository=([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)"
    r" pr=([1-9][0-9]{0,18}) head=([0-9a-f]{40}) policy=([0-9a-f]{40})"
    r" run=([1-9][0-9]{0,18}) attempt=([1-9][0-9]{0,18})"
    r" base=([0-9a-f]{40}) merge_base=([0-9a-f]{40}) repository_id=([1-9][0-9]{0,18}) -->"
)


class ReportingError(ValueError):
    """Fixed messages only, with no untrusted input or credential exception chain."""


@dataclass(frozen=True)
class PublicationIdentity:
    repository: str
    pr_number: int
    head_sha: str
    policy_sha: str
    workflow_run_id: int
    run_attempt: int
    base_sha: str
    merge_base_sha: str
    repository_id: int

    def __post_init__(self):
        try:
            repository(self.repository)
            for value in (self.pr_number, self.workflow_run_id, self.run_attempt, self.repository_id):
                integer(value, 1)
            for value in (self.head_sha, self.policy_sha, self.base_sha, self.merge_base_sha):
                sha(value)
        except CollectionError:
            raise ReportingError("invalid publication identity") from None

    @property
    def order(self):
        return self.workflow_run_id, self.run_attempt

    @property
    def run_url(self):
        return f"https://github.com/{self.repository}/actions/runs/{self.workflow_run_id}"

    def marker(self):
        self.__post_init__()
        return (f"<!-- {MARKER} repository={self.repository} pr={self.pr_number}"
                f" head={self.head_sha} policy={self.policy_sha} run={self.workflow_run_id}"
                f" attempt={self.run_attempt} base={self.base_sha} merge_base={self.merge_base_sha}"
                f" repository_id={self.repository_id} -->")


def parse_marker(body):
    """Only the exact first-line grammar is metadata. Similar text is not authority."""
    if type(body) is not str or body.count(MARKER) != 1:
        raise ReportingError("invalid sticky marker")
    match = _MARKER.fullmatch(body.split("\n", 1)[0])
    if match is None:
        raise ReportingError("invalid sticky marker")
    repo, pr, head, policy, run, attempt, base, merge, repo_id = match.groups()
    identity = PublicationIdentity(repo, int(pr), head, policy, int(run), int(attempt),
                                   base, merge, int(repo_id))
    if identity.marker() != body.split("\n", 1)[0]:
        raise ReportingError("noncanonical sticky marker")
    return identity


class DeliveryState(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True)
class PatchDelivery:
    """Host upload receipt only. No URL or retention is accepted from data."""
    state: DeliveryState
    artifact_id: int | None = None

    def __post_init__(self):
        if type(self.state) is not DeliveryState:
            raise ReportingError("invalid patch delivery state")
        if self.state == DeliveryState.AVAILABLE:
            try:
                integer(self.artifact_id, 1)
            except CollectionError:
                raise ReportingError("available patch requires trusted artifact identity") from None
        elif self.artifact_id is not None:
            raise ReportingError("unexpected artifact identity")


def inert_text(value, maximum=MAX_ADVISORY_BYTES):
    """Encode punctuation before Markdown parsing; fullwidth @ cannot ping.

    Entity-encoded punctuation remains visible text, including URL punctuation,
    fences and HTML delimiters. Reject controls rather than interpreting them.
    Clipping bounds the displayed UTF-8 bytes, including entity expansion.
    """
    if type(value) is not str or any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                                     for c in value):
        raise ReportingError("invalid display text")
    encoded = ["＠" if c == "@" else f"&#{ord(c)};" if c in string.punctuation else c for c in value]
    if sum(len(c.encode()) for c in encoded) <= maximum:
        return "".join(encoded)
    suffix = " [clipped]"
    remaining = maximum - len(suffix.encode())
    result = []
    for part in encoded:
        size = len(part.encode())
        if size > remaining:
            break
        result.append(part)
        remaining -= size
    return "".join(result) + suffix


def _hash(value):
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ReportingError("invalid evidence digest")


def loaded_skills(result, bundle):
    """Extract only canonical T04 request records, authenticated by fixed registry."""
    if (type(result.trace) is not tuple or not 1 <= len(result.trace) <= MAX_TRACE_EVENTS
            or any(type(e) is not str for e in result.trace)
            or sum(len(e.encode()) for e in result.trace) > MAX_TRACE_BYTES):
        raise ReportingError("invalid skill trace")
    found, requests = {}, set()
    fields = {"event", "stage", "target", "binding", "request_sha256", "request_bytes",
              "system_sha256", "completion_tokens", "skills"}
    for index, raw in enumerate(result.trace):
        try:
            event = strict_json(raw, MAX_TRACE_BYTES)
            if type(event) is not dict or canonical(event).decode() != raw:
                raise ValueError()
            if index == 0:
                if (set(event) != {"event", "source", "context_sha256", "definition_sha256"}
                        or event["event"] != "initial" or event["source"] != asdict(bundle.source)
                        or event["context_sha256"] != bundle.sha256
                        or event["definition_sha256"] != digest(UNIT_TEST_AGENT.encode())):
                    raise ValueError()
            elif event.get("event") == "initial":
                raise ValueError()
            if event.get("event") != "request":
                continue
            if set(event) != fields:
                raise ValueError()
            stage, target = Stage(event["stage"]), Target(event["target"])
            if ((stage, target) in requests or not any(
                f.path.startswith(str(target) + "/") for f in bundle.changed_files
            )):
                raise ValueError()
            requests.add((stage, target))
            for key in ("binding", "request_sha256", "system_sha256"):
                _hash(event[key])
            integer(event["request_bytes"], 1, MAX_REQUEST_BYTES)
            if type(event["completion_tokens"]) is not int or event["completion_tokens"] != COMPLETION_TOKENS[stage]:
                raise ValueError()
            expected = ({Stage.DECISION: ("analyze-change", f"{target}-unit-tests"),
                         Stage.EDITS: (f"{target}-unit-tests",),
                         Stage.VERIFICATION: ("verify-test-patch",)})[stage]
            if type(event["skills"]) is not list or len(event["skills"]) != len(expected):
                raise ValueError()
            for obj, identifier in zip(event["skills"], expected):
                if type(obj) is not dict or set(obj) != set(SkillUseRecord.__dataclass_fields__):
                    raise ValueError()
                record = SkillUseRecord(**{**obj, "target": Target(obj["target"])})
                entry = REGISTRY[identifier]
                if (record.skill_id != identifier or record.version != entry.version
                        or record.target != target or target not in entry.targets
                        or record.content_sha256 != entry.sha256
                        or record.loaded_content_sha256 != entry.sha256):
                    raise ValueError()
                key = (str(target), identifier)
                if key in found and found[key] != record:
                    raise ValueError()
                found[key] = record
        except Exception:
            raise ReportingError("invalid skill trace") from None
    if (type(result.provider_calls) is not int or result.provider_calls != len(requests)
            or not 0 <= result.provider_calls <= MAX_PROVIDER_CALLS):
        raise ReportingError("invalid skill trace")
    return tuple(found[k] for k in sorted(found))


@dataclass(frozen=True)
class BoundEvidence:
    identity: PublicationIdentity
    agent: AgentResult
    verification: VerificationReport | None
    skills: tuple[SkillUseRecord, ...]
    changes_sha256: str | None


def bind_evidence(bundle, result, verification=None):
    """Validate the complete context/proposal/policy/verifier identity chain."""
    try:
        if type(bundle) is not ContextBundle or type(result) is not AgentResult:
            raise ValueError()
        source, workflow = bundle.source, bundle.workflow
        if type(source) is not SourceIdentity or type(workflow) is not WorkflowIdentity:
            raise ValueError()
        source.__post_init__()
        integer(workflow.run_id, 1)
        integer(workflow.run_attempt, 1)
        integer(workflow.workflow_id, 1)
        if (workflow.head_sha != source.head_sha
                or (source.run_id is not None and str(workflow.run_id) != source.run_id)
                or workflow.event not in {"pull_request", "push"}
                or any(f.head_sha != source.head_sha for f in bundle.changed_files)):
            raise ValueError()
        identity = PublicationIdentity(source.repository, source.pr_number, source.head_sha,
                                       source.policy_sha, workflow.run_id, workflow.run_attempt,
                                       source.base_sha, source.merge_base_sha, workflow.repository_id)
        if result.context_sha256 != bundle.sha256 or type(result.state) is not State:
            raise ValueError()
        if (type(result.recommendations) is not tuple or len(result.recommendations) > 2
                or any(r not in {"accept", "reject", "unavailable"} for r in result.recommendations)):
            raise ValueError()
        skills = loaded_skills(result, bundle)
        if result.proposal is None:
            if (result.state != State.FAILED or verification is not None or result.host_result is not None
                    or result.policy is not None):
                raise ValueError()
            return BoundEvidence(identity, result, None, skills, None)
        proposal = result.proposal
        if type(proposal) is not ModelProposal:
            raise ValueError()
        ModelProposal.from_payload(canonical(asdict(proposal)).decode())
        if proposal.source != source:
            raise ValueError()
        policy = result.policy
        if (type(policy) is not PolicyAssessment or policy.decision is not PolicyDecision.ACCEPTED
                or type(policy.changes) is not TrustedChanges or policy.reason is not None):
            raise ValueError()
        changes = policy.changes
        if (changes.source != source or type(changes.edits) is not tuple
                or len(changes.edits) > MAX_EDITED_FILES
                or changes.changed_files != tuple(sorted(f.path for f in bundle.changed_files))
                or changes.permitted_test_paths != tuple(sorted(c.path for c in bundle.candidate_tests))
                or tuple(e.path for e in changes.edits) != tuple(sorted(e.path for e in changes.edits))
                or len({e.path for e in changes.edits}) != len(changes.edits)):
            raise ValueError()
        proposed = {e.path: e for e in proposal.edits}
        if set(proposed) != {e.path for e in changes.edits}:
            raise ValueError()
        for edit in changes.edits:
            if (type(edit) is not ValidatedEdit or type(edit.target) is not Target
                    or type(edit.operation) is not EditOperation or type(edit.content) is not bytes
                    or type(edit.mode) is not int or edit.mode not in SUPPORTED_FILE_MODES
                    or len(edit.content) > MAX_FILE_BYTES or edit.path not in changes.permitted_test_paths):
                raise ValueError()
            test_path(edit.path, edit.target)
            p = proposed[edit.path]
            if (p.target, p.operation, p.original_sha256, p.content.encode()) != (
                edit.target, edit.operation, edit.original_sha256, edit.content
            ):
                raise ValueError()
            if edit.operation == EditOperation.UPDATE:
                _hash(edit.original_sha256)
            elif edit.original_sha256 is not None or edit.mode != CREATE_FILE_MODE:
                raise ValueError()
        if sum(len(e.content) for e in changes.edits) > MAX_TOTAL_BYTES:
            raise ValueError()
        if {e.target for e in changes.edits} - {d.target for d in proposal.decisions}:
            raise ValueError()
        for path in changes.changed_files:
            normalized_path(path)
        if (len(set(changes.changed_files)) != len(changes.changed_files)
                or len(set(changes.permitted_test_paths)) != len(changes.permitted_test_paths)):
            raise ValueError()
        for decision in proposal.decisions:
            actual = {e.path for e in changes.edits if e.target == decision.target}
            if set(decision.plan.intended_test_files) != actual:
                raise ValueError()
            if decision.action in {ModelAction.NO_TESTS_NEEDED, ModelAction.UNSUPPORTED}:
                if actual:
                    raise ValueError()
            elif not actual or (decision.action == ModelAction.GENERATE and any(
                e.operation != EditOperation.CREATE for e in changes.edits if e.target == decision.target
            )) or (decision.action == ModelAction.UPDATE and not any(
                e.operation == EditOperation.UPDATE for e in changes.edits if e.target == decision.target
            )):
                raise ValueError()
        changes_hash = digest(changes.to_bytes())
        if changes.edits:
            if verification is None and result.host_result is not None:
                raise ValueError()
            if verification is not None:
                _verification(verification, source, changes_hash)
                if result.host_result is not None and result.host_result != verification.host_result:
                    raise ValueError()
        elif verification is not None or result.host_result is not None or result.state != State.FINISHED:
            raise ValueError()
        return BoundEvidence(identity, result, verification, skills, changes_hash)
    except Exception:
        raise ReportingError("report input binding rejected") from None


def _verification(report, source, changes_hash):
    if (type(report) is not VerificationReport or type(report.host_result) is not HostResult
            or report.host_result.source != source or report.changes_sha256 != changes_hash
            or type(report.commands) is not tuple or len(report.commands) > len(PROFILES)):
        raise ReportingError("invalid verification binding")
    host = report.host_result
    host.__post_init__()
    for record in host.verifier_results:
        record.__post_init__()
    seen, tests = set(), []
    for c in report.commands:
        if type(c) is not CommandEvidence or c.profile_id not in PROFILES or c.profile_id in seen:
            raise ReportingError("invalid command evidence")
        seen.add(c.profile_id)
        p = PROFILES[c.profile_id]
        if (c.command_id != p.command_id or type(c.kind) is not ExecutionKind or c.kind.value != p.kind
                or c.source_sha != source.head_sha or c.changes_sha256 != changes_hash
                or type(c.image_id) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", c.image_id) is None
                or type(c.timed_out) is not bool or type(c.passed) is not bool
                or (c.exit_code is not None and type(c.exit_code) is not int)
                or type(c.duration_seconds) not in {int, float} or not math.isfinite(c.duration_seconds)
                or c.duration_seconds < 0):
            raise ReportingError("invalid command binding")
        for number in (c.stdout_bytes, c.stderr_bytes):
            integer(number)
        for value in (c.stdout_sha256, c.stderr_sha256):
            _hash(value)
        if type(c.reports) is not tuple or len(c.reports) > 3:
            raise ReportingError("invalid report references")
        references = set()
        for ref, size, hashed in c.reports:
            if (type(ref) is not str or re.fullmatch(
                r"reports/[0-7]/(?:tests\.json|tests\.xml|collection\.json|coverage\.json|coverage/coverage-summary\.json)", ref
            ) is None or ref in references):
                raise ReportingError("invalid report reference")
            references.add(ref)
            integer(size, 0, 128 * 1024)
            _hash(hashed)
        if c.counts is not None:
            n = c.counts
            if type(n) is not Counts or type(n.files) is not tuple or len(n.files) > 256:
                raise ReportingError("invalid collected counts")
            for number in (n.collected, n.passed, n.failed, n.errors, n.skipped):
                integer(number, 0, 100000)
            if len(dict(n.files)) != len(n.files):
                raise ReportingError("duplicate count paths")
            for path, number in n.files:
                test_path(path, Target(p.stack))
                integer(number, 0, 100000)
            if sum(number for _, number in n.files) != n.collected:
                raise ReportingError("invalid collected inventory")
        if p.command_id == "tsc":
            if c.counts is not None:
                raise ReportingError("typecheck cannot collect tests")
        else:
            tests.append(c)
        if c.passed and (c.timed_out or c.exit_code != 0 or c.error is not None or (
            p.command_id != "tsc" and (c.counts is None or c.counts.collected < 1
                                     or c.counts.passed < 1 or c.counts.failed or c.counts.errors
                                     or any(number < 1 for _, number in c.counts.files))
        )):
            raise ReportingError("contradictory passing command")
    if host.status == FinalHostStatus.VERIFIED and (not report.commands or any(not c.passed for c in report.commands)):
        raise ReportingError("contradictory verified evidence")
    if len(tests) != len(host.verifier_results):
        raise ReportingError("verifier/command evidence mismatch")
    for c, r in zip(tests, host.verifier_results):
        expected = (ExecutionResult.PASSED if c.passed else ExecutionResult.TIMEOUT if c.timed_out
                    else ExecutionResult.FAILED if c.exit_code not in (0, None) else ExecutionResult.ERROR)
        if ((r.profile_id, r.command_id, r.kind, r.exit_code, r.collected_tests, r.result) != (
            c.profile_id, c.command_id, c.kind, c.exit_code,
            None if c.counts is None else c.counts.collected, expected)
                or type(r.report_reference) is not str
                or re.fullmatch(r"commands/[0-7]\.json", r.report_reference) is None):
            raise ReportingError("verifier/command evidence mismatch")


def verification_metadata(bound):
    """Small selected facts only; never raw logs, build output or host paths."""
    report = bound.verification
    return {"schema_version": 1, "source": asdict(bound.identity),
            "changes_sha256": bound.changes_sha256,
            "status": None if report is None else str(report.host_result.status),
            "commands": [] if report is None else [{
                "profile_id": c.profile_id, "command_id": c.command_id, "kind": str(c.kind),
                "exit_code": c.exit_code, "timed_out": c.timed_out, "passed": c.passed,
                "counts": None if c.counts is None else asdict(c.counts),
                "reports": c.reports, "source_sha": c.source_sha, "changes_sha256": c.changes_sha256,
                "stdout_bytes": c.stdout_bytes, "stderr_bytes": c.stderr_bytes,
                "stdout_sha256": c.stdout_sha256, "stderr_sha256": c.stderr_sha256,
            } for c in report.commands]}


@dataclass(frozen=True)
class PreparedReport:
    identity: PublicationIdentity
    body: str
    status: str
    delivery: PatchDelivery


_MEANINGS = {
    "VERIFIED": "Required baseline and patched checks passed in host evidence.",
    "VERIFICATION_FAILED": "Required patched checks or their evidence failed verification.",
    "BASELINE_FAILED": "A required baseline guard failed before patched verification completed.",
    "UNAVAILABLE": "Verification is unavailable; this does not mean tests failed.",
    "STALE": "Source or accepted changes were stale; these facts cannot verify the current PR.",
    "UNSUPPORTED": "The requested analysis or verification is unsupported.",
    "NO_TESTS_NEEDED": "The agent recommends no additional tests; no verification was run.",
    "AGENT_FAILED": "Agent analysis failed; no successful agent completion is claimed.",
}


def prepare_report(bundle, result, verification=None, *, delivery=None):
    bound = bind_evidence(bundle, result, verification)
    edited = result.proposal is not None and bool(result.proposal.edits)
    delivery = delivery if delivery is not None else PatchDelivery(
        DeliveryState.UNAVAILABLE if edited else DeliveryState.NOT_REQUIRED)
    if type(delivery) is not PatchDelivery:
        raise ReportingError("trusted patch delivery required")
    delivery.__post_init__()
    if (edited and delivery.state == DeliveryState.NOT_REQUIRED
            or not edited and delivery.state != DeliveryState.NOT_REQUIRED):
        raise ReportingError("delivery does not match required edits")
    if result.state == State.FAILED:
        status = "AGENT_FAILED"
    elif verification is not None:
        status = verification.host_result.status.value.upper().replace("-", "_")
    elif edited:
        status = "UNAVAILABLE"
    else:
        status = "UNSUPPORTED" if any(d.action == ModelAction.UNSUPPORTED for d in result.proposal.decisions) else "NO_TESTS_NEEDED"
    i = bound.identity
    lines = [i.marker(), "## Unit test agent", f"Final status: **{status}** — {_MEANINGS[status]}",
             f"PR head: `{i.head_sha}`", f"Policy: `{i.policy_sha}`",
             f"Workflow run: {i.workflow_run_id}; attempt: {i.run_attempt}",
             f"[Run]({i.run_url})", "", "### Agent analysis (advisory)"]
    if result.proposal is None:
        lines.append("No proposal was produced.")
    else:
        for d in sorted(result.proposal.decisions, key=lambda d: str(d.target)):
            lines.extend([f"- {d.target}: {d.action}",
                          "  Changed behavior: " + inert_text(d.plan.changed_behavior),
                          "  Rationale: " + inert_text(d.plan.rationale)])
        if result.recommendations:
            if type(result.recommendations) is not tuple or len(result.recommendations) > 2:
                raise ReportingError("invalid advisory recommendations")
            lines.append("Review recommendations (advisory): " + ", ".join(inert_text(r) for r in result.recommendations))
    lines.extend(["", "### Host evidence (authoritative)", "Actual loaded skills:", ""])
    if not bound.skills:
        lines.append("- No actual skill loads recorded.")
    for s in bound.skills:
        lines.append(f"- {inert_text(s.skill_id)} {inert_text(s.version)} ({s.target}); content SHA-256 `{s.content_sha256}`; loaded SHA-256 `{s.loaded_content_sha256}`")
    lines.extend(["", "Accepted test files:", ""])
    if edited:
        lines.extend(f"- {e.operation}: {inert_text(e.path)}" for e in result.policy.changes.edits)
    else:
        lines.append("- None.")
    if verification is None:
        lines.extend(["", "Host verification: no execution evidence supplied."])
    else:
        verdict = verification.host_result.status.value.upper().replace("-", "_")
        lines.extend(["", f"Host verification: **{verdict}** — {_MEANINGS[verdict]}"])
        if not verification.commands:
            lines.append("No commands executed in the supplied host evidence.")
        else:
            lines.extend(["", "| Profile | State | Outcome | Collected | Passed | Failed | Errors | Skipped | Exit | Timeout |",
                          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
            for c in verification.commands:
                counts = ["—"] * 5 if c.counts is None else [str(getattr(c.counts, k)) for k in ("collected", "passed", "failed", "errors", "skipped")]
                lines.append(f"| {c.profile_id} | {c.kind} | {'passed' if c.passed else 'not passed'} | " + " | ".join(counts) + f" | {c.exit_code if c.exit_code is not None else '—'} | {str(c.timed_out).lower()} |")
            lines.extend(["", "Report references:", ""])
            for c in verification.commands:
                for ref, size, hashed in c.reports:
                    lines.append(f"- {c.profile_id}: {inert_text(ref)}; {size} bytes; SHA-256 `{hashed}`.")
        lines.extend(["", f"Accepted changes SHA-256: `{bound.changes_sha256}`"])
    lines.extend(["", "### Patch delivery", f"Patch delivery: **{delivery.state}**"])
    if delivery.state == DeliveryState.AVAILABLE:
        lines.append(f"[Download patch artifact]({i.run_url}/artifacts/{delivery.artifact_id})")
    elif delivery.state == DeliveryState.UNAVAILABLE:
        lines.append("Patch unavailable. Delivery of required edits is unresolved; the host verification verdict is unchanged.")
    else:
        lines.append("No patch is required.")
    body = "\n".join(lines) + "\n"
    if len(body.encode()) > MAX_COMMENT_BYTES:
        raise ReportingError("report byte limit exceeded")
    return PreparedReport(i, body, status, delivery)
