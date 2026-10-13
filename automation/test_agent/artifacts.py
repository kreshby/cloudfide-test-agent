"""Deterministic allowlisted bytes and future upload specs; no upload or execution."""

from dataclasses import asdict, dataclass
import difflib
from enum import StrEnum
import hashlib
from types import MappingProxyType

from .contracts import EditOperation
from .provider import canonical, digest
from .reporting import (ReportingError, bind_evidence, report_status, DeliveryState,
                        verification_metadata)
from .verification import Stale, _root, admitted, safe_read, safely_missing


MAX_BUNDLE_BYTES = 10 * 1024 * 1024
ENTRY_LIMITS = MappingProxyType({
    "manifest.json": 16 * 1024,
    "verification.json": 128 * 1024,
    "generated-tests.patch": 1024 * 1024,
    "report-metadata.json": 16 * 1024,
})
CLASSIFICATIONS = MappingProxyType({
    "manifest.json": "manifest",
    "verification.json": "host-verification",
    "generated-tests.patch": "untrusted-test-patch-data",
    "report-metadata.json": "host-report-metadata",
})


class ArtifactError(ValueError):
    """Fixed host errors only; no repository bytes or absolute paths."""


class PatchUnavailable(ArtifactError):
    pass


class PatchPreparationState(StrEnum):
    PREPARED = "PREPARED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True)
class ArtifactEntry:
    name: str
    data: bytes
    classification: str

    def __post_init__(self):
        if (type(self.name) is not str or self.name not in ENTRY_LIMITS
                or self.classification != CLASSIFICATIONS[self.name] or type(self.data) is not bytes):
            raise ArtifactError("artifact entry outside fixed allowlist")
        if len(self.data) > ENTRY_LIMITS[self.name]:
            raise ArtifactError("artifact entry byte limit exceeded")
        try:
            self.data.decode("utf-8")
        except UnicodeError:
            raise ArtifactError("artifact entry must be UTF-8") from None


def check_total(data):
    """Include the manifest in the hard per-run limit, before any future upload."""
    if type(data) is not tuple or any(type(value) is not bytes for value in data):
        raise ArtifactError("bounded artifact bytes required")
    if sum(len(value) for value in data) > MAX_BUNDLE_BYTES:
        raise ArtifactError("artifact total byte limit exceeded")


@dataclass(frozen=True)
class UploadSpec:
    """Trusted fixed names/paths/retention; never a recursive directory or glob."""
    classification: str
    logical_name: str
    paths: tuple[str, ...]
    retention_days: int

    def __post_init__(self):
        if (self.classification not in {"intermediate", "final"}
                or self.logical_name != "test-agent-" + self.classification
                or type(self.retention_days) is not int
                or self.retention_days != {"intermediate": 1, "final": 7}[self.classification]
                or type(self.paths) is not tuple or not self.paths or len(self.paths) > len(ENTRY_LIMITS)
                or any(type(path) is not str or path not in ENTRY_LIMITS for path in self.paths)
                or self.paths != tuple(sorted(set(self.paths)))):
            raise ArtifactError("invalid fixed upload specification")


def upload_spec(entries, *, classification="final"):
    validate_entries(entries)
    if classification not in {"intermediate", "final"}:
        raise ArtifactError("invalid upload classification")
    return UploadSpec(classification, "test-agent-" + classification,
                      tuple(e.name for e in entries), 7 if classification == "final" else 1)


def validate_entries(entries):
    if (type(entries) is not tuple or not 1 <= len(entries) <= len(ENTRY_LIMITS)
            or any(type(e) is not ArtifactEntry for e in entries)):
        raise ArtifactError("invalid artifact entries")
    check_total(tuple(e.data for e in entries))
    for entry in entries:
        entry.__post_init__()
    if tuple(e.name for e in entries) != tuple(sorted({e.name for e in entries})):
        raise ArtifactError("artifact names must be sorted and unique")


@dataclass(frozen=True)
class ArtifactBundle:
    entries: tuple[ArtifactEntry, ...]
    upload: UploadSpec
    patch_preparation: PatchPreparationState


def _diff(old, new, path, create):
    """Standard unified diff, including conventional missing-newline records."""
    before = "/dev/null" if create else "a/" + path
    after = "b/" + path
    parts = []
    def lines(text):
        parts = text.split("\n")
        return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

    for line in difflib.unified_diff(lines(old), lines(new),
                                     fromfile=before, tofile=after, lineterm="\n"):
        parts.append(line)
        if not line.endswith("\n"):
            parts.append("\n\\ No newline at end of file\n")
    return "".join(parts).encode("utf-8")


def build_patch(bundle, result, repository_root, verification=None, *, publication=None):
    """No shell/Git, no raw model patch, no apply. Recheck exact T03 head bytes.

    T03's authenticated Git blob identifies the original independently of local
    Git metadata. No-follow reads must match that content, SHA-256 and T02 mode.
    """
    try:
        bind_evidence(bundle, result, verification, publication=publication)
    except ReportingError:
        raise ArtifactError("artifact input binding rejected") from None
    if result.proposal is None or not result.proposal.edits:
        return None
    try:
        _root(repository_root)
        candidates = {c.path: c for c in bundle.candidate_tests}
        evidence = {e.path: e for e in bundle.existing_tests}
        parts = []
        for edit in result.policy.changes.edits:
            if not admitted(edit.path):
                raise PatchUnavailable("sensitive or bulk test path excluded")
            if edit.operation == EditOperation.CREATE:
                if candidates[edit.path].exists is not False or not safely_missing(repository_root, edit.path):
                    raise PatchUnavailable("create target is unavailable")
                old = b""
            else:
                item = evidence.get(edit.path)
                if (candidates[edit.path].exists is not True or item is None
                        or item.state != "available" or item.source_sha != bundle.source.head_sha
                        or type(item.content) is not str):
                    raise PatchUnavailable("exact-head original evidence unavailable")
                original = item.content.encode("utf-8")
                blob = hashlib.sha1(b"blob " + str(len(original)).encode("ascii") + b"\0" + original).hexdigest()
                old, mode = safe_read(repository_root, edit.path, maximum=64 * 1024)
                if (old != original or mode != edit.mode or digest(old) != edit.original_sha256
                        or item.observed_bytes != len(old) or item.blob_sha != blob):
                    raise PatchUnavailable("exact-head original revalidation failed")
            new_text, old_text = edit.content.decode("utf-8"), old.decode("utf-8")
            if "\x00" in new_text or "\x00" in old_text:
                raise PatchUnavailable("binary patch data unsupported")
            # LF is the diff record separator; splitlines must not treat other
            # control characters as record separators.
            if any(ord(c) < 32 and c not in "\n\r\t" for c in new_text + old_text):
                raise PatchUnavailable("unsupported patch control character")
            parts.append(_diff(old_text, new_text, edit.path, edit.operation == EditOperation.CREATE))
            if sum(len(p) for p in parts) > ENTRY_LIMITS["generated-tests.patch"]:
                raise PatchUnavailable("patch byte limit exceeded")
        return b"".join(parts)
    except (Stale, OSError, UnicodeError, KeyError):
        raise PatchUnavailable("exact-head original unavailable") from None


def _manifest(identity, entries):
    # Manifest inventories every other entry; self-hashing would be circular.
    return canonical({"schema_version": 1, "artifact_logical_name": "test-agent-final",
                      "source_head_sha": identity.head_sha, "policy_sha": identity.policy_sha,
                      "run_id": identity.workflow_run_id, "run_attempt": identity.run_attempt,
                      "source_ci_run_id": identity.source_ci_run_id,
                      "source_ci_run_attempt": identity.source_ci_run_attempt,
                      "entries": [{"relative_path": e.name, "byte_size": len(e.data),
                                   "sha256": digest(e.data), "classification": e.classification}
                                  for e in entries]})


def build_bundle(bundle, result, repository_root, verification=None, *, publication=None):
    try:
        bound = bind_evidence(bundle, result, verification, publication=publication)
    except ReportingError:
        raise ArtifactError("artifact input binding rejected") from None
    try:
        patch = build_patch(bundle, result, repository_root, verification, publication=publication)
        state = PatchPreparationState.NOT_REQUIRED if patch is None else PatchPreparationState.PREPARED
    except PatchUnavailable:
        patch, state = None, PatchPreparationState.UNAVAILABLE
    # Preparation is not delivery. AVAILABLE needs a later trusted upload receipt.
    metadata = {"schema_version": 1, "identity": asdict(bound.identity),
                "final_status": report_status(bound), "patch_preparation": str(state),
                "patch_delivery": str(DeliveryState.UNAVAILABLE if result.proposal and result.proposal.edits
                                      else DeliveryState.NOT_REQUIRED),
                "required_edits": bool(result.proposal and result.proposal.edits),
                "skills": [asdict(s) for s in bound.skills]}
    data = {"verification.json": canonical(verification_metadata(bound)),
            "report-metadata.json": canonical(metadata)}
    if patch is not None:
        data["generated-tests.patch"] = patch
    entries = tuple(ArtifactEntry(name, data[name], CLASSIFICATIONS[name]) for name in sorted(data))
    manifest = ArtifactEntry("manifest.json", _manifest(bound.identity, entries), CLASSIFICATIONS["manifest.json"])
    entries = tuple(sorted((*entries, manifest), key=lambda e: e.name))
    validate_entries(entries)
    return ArtifactBundle(entries, upload_spec(entries), state)
