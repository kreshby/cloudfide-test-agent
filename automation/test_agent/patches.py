"""Construct deterministic host changes from structured edits, without writes.

This representation is data, not a git patch to apply or a script to execute.
Future writing/verification/publication needs separate trusted implementations.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

from .contracts import ContractError, ModelProposal, PolicyDecision, SourceIdentity
from .policy import PolicyError, TrustedContext, ValidatedEdit, validate_proposal


@dataclass(frozen=True)
class TrustedChanges:
    source: SourceIdentity
    changed_files: tuple[str, ...]
    permitted_test_paths: tuple[str, ...]
    edits: tuple[ValidatedEdit, ...]

    def to_bytes(self) -> bytes:
        """Canonical UTF-8 JSON, sorted by path and keys, independent of input order."""
        changes = []
        for edit in self.edits:
            item = asdict(edit)
            item["content"] = edit.content.decode("utf-8")
            item["byte_size"] = len(edit.content)
            item["new_sha256"] = hashlib.sha256(edit.content).hexdigest()
            changes.append(item)
        return json.dumps({
            "schema_version": self.source.schema_version,
            "source": asdict(self.source),
            "changed_files": self.changed_files,
            "permitted_test_paths": self.permitted_test_paths,
            "edits": changes,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class PolicyAssessment:
    decision: PolicyDecision
    changes: TrustedChanges | None
    reason: str | None


def prepare_changes(payload: Any, context: TrustedContext) -> TrustedChanges:
    """Parse untrusted data, validate against trusted input, construct changes.

    ContractError/PolicyError indicate rejection. Accepts no preconstructed model
    object, raw git patch, executable code, host states, or policy overrides.
    """
    proposal = ModelProposal.from_payload(payload)
    edits = validate_proposal(proposal, context)
    return TrustedChanges(context.source, tuple(sorted(context.changed_files)),
                          tuple(sorted(context.permitted_test_paths)), edits)


def assess_proposal(payload: Any, context: TrustedContext) -> PolicyAssessment:
    """Policy acceptance is separate from verification and final host success."""
    try:
        return PolicyAssessment(PolicyDecision.ACCEPTED, prepare_changes(payload, context), None)
    except (ContractError, PolicyError) as error:
        # Errors describe rules, never echo arbitrary model content or credentials.
        return PolicyAssessment(PolicyDecision.REJECTED, None, str(error))
