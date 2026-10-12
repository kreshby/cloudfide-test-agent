"""Version 1 contracts. Only ModelProposal.from_payload accepts model output.

Unknown fields are rejected at every level. Verifier/final states and skill-use
records are separate host-owned types, never fields in a model proposal.
"""

from dataclasses import dataclass
from enum import StrEnum
import json
import re
from typing import Any


SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024


class ContractError(ValueError):
    """An untrusted payload does not satisfy the versioned contract."""


class Target(StrEnum):
    FRONTEND = "frontend"
    BACKEND = "backend"


class ModelAction(StrEnum):
    GENERATE = "generate"
    UPDATE = "update"
    NO_TESTS_NEEDED = "no-tests-needed"
    UNSUPPORTED = "unsupported"


class EditOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"


class PolicyDecision(StrEnum):
    ACCEPTED = "proposal-accepted"
    REJECTED = "proposal-rejected"


class ExecutionResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    NOT_RUN = "not-run"


class ExecutionKind(StrEnum):
    BASELINE = "baseline"
    PATCHED = "patched"


class FinalHostStatus(StrEnum):
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification-failed"
    BASELINE_FAILED = "baseline-failed"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


def _object(value: Any, fields: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ContractError(f"{label}: expected exactly {sorted(fields)}")
    return value


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ContractError(f"{label}: expected nonempty bounded string")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ContractError(f"{label}: invalid control or Unicode character")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{label}: expected integer >= {minimum}")
    return value


def _hash(value: Any, length: int, label: str) -> str:
    if type(value) is not str or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ContractError(f"{label}: expected lowercase {length}-digit hex hash")
    return value


def _enum(kind: type[StrEnum], value: Any, label: str):
    if type(value) is not str:
        raise ContractError(f"{label}: expected string")
    try:
        return kind(value)
    except ValueError as error:
        raise ContractError(f"{label}: unsupported value") from error


def _texts(value: Any, label: str, maximum: int = 32) -> tuple[str, ...]:
    if type(value) is not list or len(value) > maximum:
        raise ContractError(f"{label}: expected bounded array")
    result = tuple(_text(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ContractError(f"{label}: duplicate entries")
    return result


def _version(value: Any) -> None:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ContractError("unsupported schema_version")


@dataclass(frozen=True)
class SourceIdentity:
    repository: str
    pr_number: int | None
    base_sha: str
    merge_base_sha: str
    head_sha: str
    policy_sha: str
    run_id: str | None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version(self.schema_version)
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", _text(self.repository, "repository", 200)) is None:
            raise ContractError("repository: expected owner/name")
        if self.pr_number is not None:
            _integer(self.pr_number, "pr_number", 1)
        for name in ("base_sha", "merge_base_sha", "head_sha", "policy_sha"):
            _hash(getattr(self, name), 40, name)
        if self.run_id is not None:
            _text(self.run_id, "run_id", 200)

    @classmethod
    def from_mapping(cls, value: Any):
        return cls(**_object(value, set(cls.__dataclass_fields__), "source"))


@dataclass(frozen=True)
class TestPlan:
    changed_behavior: str
    existing_test_evidence: tuple[str, ...]
    rationale: str
    intended_test_files: tuple[str, ...]
    skill_ids: tuple[str, ...]

    def __post_init__(self):
        _text(self.changed_behavior, "changed_behavior")
        _text(self.rationale, "rationale")
        for name in ("existing_test_evidence", "intended_test_files", "skill_ids"):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise ContractError(f"{name}: expected immutable tuple")
            _texts(list(value), name)

    @classmethod
    def from_mapping(cls, value: Any):
        obj = _object(value, set(cls.__dataclass_fields__), "plan")
        return cls(
            _text(obj["changed_behavior"], "changed_behavior"),
            _texts(obj["existing_test_evidence"], "existing_test_evidence"),
            _text(obj["rationale"], "rationale"),
            _texts(obj["intended_test_files"], "intended_test_files"),
            _texts(obj["skill_ids"], "skill_ids"),
        )


@dataclass(frozen=True)
class TargetDecision:
    target: Target
    action: ModelAction
    plan: TestPlan

    def __post_init__(self):
        if (type(self.target) is not Target or type(self.action) is not ModelAction
                or type(self.plan) is not TestPlan):
            raise ContractError("decision requires target/action enums and a test plan")

    @classmethod
    def from_mapping(cls, value: Any):
        obj = _object(value, set(cls.__dataclass_fields__), "decision")
        return cls(_enum(Target, obj["target"], "target"),
                   _enum(ModelAction, obj["action"], "action"),
                   TestPlan.from_mapping(obj["plan"]))


@dataclass(frozen=True)
class ProposedEdit:
    target: Target
    path: str
    operation: EditOperation
    original_sha256: str | None
    content: str

    def __post_init__(self):
        if type(self.target) is not Target or type(self.operation) is not EditOperation:
            raise ContractError("edit requires target/operation enums")
        _text(self.path, "path", 240)
        if self.operation == EditOperation.UPDATE:
            _hash(self.original_sha256, 64, "original_sha256")
        elif self.original_sha256 is not None:
            raise ContractError("create must have null original_sha256")
        if type(self.content) is not str:
            raise ContractError("content: expected UTF-8 string")
        try:
            self.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContractError("content: invalid UTF-8") from error

    @property
    def byte_size(self) -> int:
        # Computed by the host; no model-supplied size is trusted.
        return len(self.content.encode("utf-8"))

    @classmethod
    def from_mapping(cls, value: Any):
        obj = _object(value, set(cls.__dataclass_fields__), "edit")
        return cls(_enum(Target, obj["target"], "target"),
                   obj["path"], _enum(EditOperation, obj["operation"], "operation"),
                   obj["original_sha256"], obj["content"])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON field")
        result[key] = value
    return result


@dataclass(frozen=True)
class ModelProposal:
    schema_version: int
    source: SourceIdentity
    decisions: tuple[TargetDecision, ...]
    edits: tuple[ProposedEdit, ...]

    def __post_init__(self):
        _version(self.schema_version)
        if type(self.source) is not SourceIdentity:
            raise ContractError("proposal requires source identity")
        if (type(self.decisions) is not tuple or not 1 <= len(self.decisions) <= 2
                or any(type(item) is not TargetDecision for item in self.decisions)):
            raise ContractError("decisions: expected one or two typed decisions")
        if len({item.target for item in self.decisions}) != len(self.decisions):
            raise ContractError("duplicate target decision")
        if (type(self.edits) is not tuple or len(self.edits) > 32
                or any(type(item) is not ProposedEdit for item in self.edits)):
            raise ContractError("edits: expected bounded typed edits")

    @classmethod
    def from_payload(cls, payload: Any):
        """Accept a JSON string or a plain mapping, never host-result fields."""
        if type(payload) is str:
            try:
                if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                    raise ContractError("model payload too large")
                payload = json.loads(payload, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ContractError("invalid model JSON") from error
        obj = _object(payload, set(cls.__dataclass_fields__), "proposal")
        _version(obj["schema_version"])
        decisions, edits = obj["decisions"], obj["edits"]
        if type(decisions) is not list or not 1 <= len(decisions) <= 2:
            raise ContractError("decisions: expected one or two targets")
        # Envelope bound before parsing; the stricter 8-file limit is policy.
        if type(edits) is not list or len(edits) > 32:
            raise ContractError("edits: expected bounded array")
        parsed = tuple(TargetDecision.from_mapping(item) for item in decisions)
        return cls(SCHEMA_VERSION, SourceIdentity.from_mapping(obj["source"]), parsed,
                   tuple(ProposedEdit.from_mapping(item) for item in edits))


@dataclass(frozen=True)
class SkillUseRecord:
    """Future host record of actual loading, distinct from plan skill ID claims."""

    skill_id: str
    version: str
    content_sha256: str
    loaded_content_sha256: str
    target: Target
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version(self.schema_version)
        _text(self.skill_id, "skill_id", 200)
        _text(self.version, "version", 200)
        _hash(self.content_sha256, 64, "content_sha256")
        _hash(self.loaded_content_sha256, 64, "loaded_content_sha256")
        if type(self.target) is not Target:
            raise ContractError("skill record requires a target")


@dataclass(frozen=True)
class VerifierResult:
    """Host-only future output, with identifiers rather than executable commands."""

    profile_id: str
    command_id: str
    kind: ExecutionKind
    result: ExecutionResult
    collected_tests: int | None
    exit_code: int | None
    report_reference: str | None
    output_bytes: int
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version(self.schema_version)
        _text(self.profile_id, "profile_id", 200)
        _text(self.command_id, "command_id", 200)
        if type(self.kind) is not ExecutionKind or type(self.result) is not ExecutionResult:
            raise ContractError("verifier requires host execution enums")
        if self.collected_tests is not None:
            _integer(self.collected_tests, "collected_tests")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ContractError("exit_code: expected integer or null")
        _integer(self.output_bytes, "output_bytes")
        if self.output_bytes > 64 * 1024:
            raise ContractError("verifier output exceeds 64 KiB")
        if self.report_reference is not None:
            _text(self.report_reference, "report_reference", 240)
        if self.output_bytes and self.report_reference is None:
            raise ContractError("output requires a host report reference")
        if self.result == ExecutionResult.PASSED and (
            self.exit_code != 0 or self.collected_tests is None or self.collected_tests < 1
        ):
            raise ContractError("passed execution requires exit 0 and collected tests")
        if self.result == ExecutionResult.FAILED and self.exit_code in (None, 0):
            raise ContractError("failed execution requires nonzero exit")
        if self.result == ExecutionResult.NOT_RUN and (
            self.exit_code is not None or self.collected_tests is not None or self.output_bytes
        ):
            raise ContractError("not-run cannot contain execution evidence")


@dataclass(frozen=True)
class HostResult:
    """A future trusted aggregator owns this; T02 implements no verifier runner."""

    source: SourceIdentity
    status: FinalHostStatus
    verifier_results: tuple[VerifierResult, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version(self.schema_version)
        if type(self.source) is not SourceIdentity or type(self.status) is not FinalHostStatus:
            raise ContractError("host result requires trusted source and status")
        if type(self.verifier_results) is not tuple or any(
            type(item) is not VerifierResult for item in self.verifier_results
        ):
            raise ContractError("invalid verifier results")
        if self.status == FinalHostStatus.VERIFIED and (
            not {ExecutionKind.BASELINE, ExecutionKind.PATCHED}.issubset(
                {item.kind for item in self.verifier_results}
            ) or any(item.result != ExecutionResult.PASSED for item in self.verifier_results)
        ):
            raise ContractError("verified requires successful baseline and patched evidence")
