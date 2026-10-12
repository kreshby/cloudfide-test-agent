"""Host-owned boundaries and read-only checks for untrusted desired edits.

No permissions, limits, context, or command choices are taken from the model.
The caller must obtain TrustedContext from trusted configuration/context code.
Validation is a snapshot: a future writer must revalidate immediately before
writing and protect against concurrent changes. T02 deliberately has no writer.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat

from .contracts import EditOperation, ModelAction, ModelProposal, SourceIdentity, Target


# Small reviewable proposals, enough for both stacks; never payload options.
MAX_EDITED_FILES = 8
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
SUPPORTED_FILE_MODES = frozenset({0o600, 0o640, 0o644, 0o664})
CREATE_FILE_MODE = 0o644


class PolicyError(ValueError):
    """A proposal is outside the trusted policy boundary."""


def normalized_path(value: str) -> str:
    """Accept canonical portable relative POSIX paths; do not repair input.

    Reject Unicode/percent/separator normalization ambiguities before checking
    the allowlist. Windows reserved names and trailing dots are excluded too.
    """
    if type(value) is not str or not value or len(value) > 240:
        raise PolicyError("invalid repository-relative path")
    parts = value.split("/")
    for part in parts:
        if (part in ("", ".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
                or part.endswith(".") or re.fullmatch(
                    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I
                )):
            raise PolicyError("path must be canonical relative POSIX form")
    return "/".join(parts)


def test_path(value: str, target: Target) -> str:
    path = normalized_path(value)
    parts = path.split("/")
    if target == Target.FRONTEND:
        allowed = (len(parts) >= 3 and parts[:2] == ["frontend", "src"]
                   and parts[-1].endswith((".test.ts", ".test.tsx"))
                   and parts[-1] not in (".test.ts", ".test.tsx"))
    elif target == Target.BACKEND:
        allowed = (len(parts) >= 3 and parts[:2] == ["backend", "tests"]
                   and re.fullmatch(r"test_[A-Za-z0-9_.-]+\.py", parts[-1]) is not None)
    else:
        allowed = False
    if not allowed:
        raise PolicyError("path is outside the target test allowlist")
    return path


@dataclass(frozen=True)
class TrustedContext:
    """Host-only input; not parsed from or merged with a model proposal.

    changed_files binds the host's context snapshot. permitted_test_paths is
    the explicit set of candidate tests selected by the host within that
    context (new tests need not themselves be changed source files). It can
    only narrow the global test allowlist. T03 will collect this context.
    """

    repository_root: Path
    source: SourceIdentity
    changed_files: frozenset[str]
    permitted_test_paths: frozenset[str]

    def __post_init__(self):
        root = self.repository_root
        if type(self.source) is not SourceIdentity:
            raise PolicyError("context requires trusted source identity")
        if not isinstance(root, Path) or not root.is_absolute():
            raise PolicyError("trusted root must be an absolute path")
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise PolicyError("trusted root must be a canonical directory")
        for values in (self.changed_files, self.permitted_test_paths):
            if type(values) is not frozenset:
                raise PolicyError("host path sets must be immutable")
            for path in values:
                normalized_path(path)
        for path in self.permitted_test_paths:
            target = Target.FRONTEND if path.startswith("frontend/") else Target.BACKEND
            test_path(path, target)


def _current_content(root: Path, path: str) -> tuple[bytes, int] | None:
    """Read beneath a trusted root using no-follow directory/file handles.

    No resolve-then-follow race, symlink traversal, FIFO blocking, or hardlink
    escape. Fail closed on platforms lacking the required descriptor APIs.
    """
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd):
        raise PolicyError("safe repository inspection is unavailable")
    directory = None
    file_fd = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = path.split("/")
        for part in parts[:-1]:
            try:
                next_directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
            except FileNotFoundError:
                return None
            os.close(directory)
            directory = next_directory
        try:
            metadata = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _regular_file(metadata)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory)
        opened = os.fstat(file_fd)
        _regular_file(opened)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PolicyError("target changed during inspection")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise PolicyError("existing test exceeds inspection limit")
        return content, stat.S_IMODE(opened.st_mode)
    except OSError as error:
        raise PolicyError("unsafe or inaccessible repository target") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def _regular_file(metadata) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) not in SUPPORTED_FILE_MODES):
        raise PolicyError("target must be a regular non-linked test with supported mode")


# These are lexical heuristics, not TypeScript/Python parsers. Counts preserve
# pre-existing mechanisms while rejecting increases. Literal/comment matches
# may conservatively reject; aliases, computed names, balanced substitutions,
# semantic assertion weakening and per-test body analysis remain limitations.
_FRONTEND_MODIFIERS = r"skipIf|runIf|skip|only|todo|fails"
_FRONTEND_WEAKENING = re.compile(
    r"\b(?:test|it|describe)\s*(?:\.\s*(?:concurrent|sequential)\s*)*"
    r"(?:\.\s*each\b[^\n;]{0,512}?)?"
    rf"(?:\.\s*({_FRONTEND_MODIFIERS})\b|\[\s*['\"]({_FRONTEND_MODIFIERS})['\"]\s*\])"
    r"|\b(fit|fdescribe|xit|xtest|xdescribe)\s*\("
)
_BACKEND_WEAKENING = re.compile(
    r"\b(?:pytest\s*\.\s*(?:mark\s*\.\s*)?|mark\s*\.)"
    r"(skipif|skip|xfail|importorskip)\b"
    r"|\b(?:unittest\s*\.\s*)?(skipIf|skipUnless|SkipTest|expectedFailure)\b"
    r"|\b(?:unittest\s*\.\s*)?(skip)\s*\("
    r"|\b(skipTest)\s*\("
    r"|@\s*(skipif|skip|xfail)\b"
)
_FRONTEND_TEST = re.compile(r"\b(?:it|test)\s*(?:\.\s*\w+\s*)*\(")
_BACKEND_TEST = re.compile(r"^\s*(?:async\s+)?def\s+test_\w+\s*\(", re.M)
_FRONTEND_ASSERT = re.compile(r"\b(?:expect|assert)\s*\(")
_BACKEND_ASSERT = re.compile(r"\bassert\s+|\bpytest\s*\.\s*raises\s*\(|\.assert\w+\s*\(")


def _mechanisms(pattern, content):
    return Counter(next(group for group in match.groups() if group)
                   for match in pattern.finditer(content))


def check_weakening(target: Target, old: bytes, new: bytes) -> None:
    try:
        old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PolicyError("tests must contain UTF-8 text") from error
    if "\x00" in old_text or "\x00" in new_text:
        raise PolicyError("binary test content is unsupported")
    if target == Target.FRONTEND:
        weakening, tests, assertions = _FRONTEND_WEAKENING, _FRONTEND_TEST, _FRONTEND_ASSERT
    else:
        weakening, tests, assertions = _BACKEND_WEAKENING, _BACKEND_TEST, _BACKEND_ASSERT
    if _mechanisms(weakening, new_text) - _mechanisms(weakening, old_text):
        raise PolicyError("new skip/only/xfail mechanism")
    new_tests, old_tests = len(tests.findall(new_text)), len(tests.findall(old_text))
    if new_tests == 0 or new_tests < old_tests:
        raise PolicyError("obvious test removal or empty test file")
    if not assertions.search(new_text):
        raise PolicyError("test file has no recognizable assertions")


@dataclass(frozen=True)
class ValidatedEdit:
    target: Target
    path: str
    operation: EditOperation
    original_sha256: str | None
    content: bytes
    mode: int


def validate_proposal(proposal: ModelProposal, context: TrustedContext) -> tuple[ValidatedEdit, ...]:
    """Internal typed validation; external callers use patches.prepare_changes."""
    if type(proposal) is not ModelProposal:
        raise PolicyError("validated model contract is required")
    if type(context) is not TrustedContext:
        raise PolicyError("host context is required")
    if proposal.source != context.source:
        raise PolicyError("proposal source identity is stale or mismatched")
    if len(proposal.edits) > MAX_EDITED_FILES:
        raise PolicyError("too many edited files")
    if any(edit.byte_size > MAX_FILE_BYTES for edit in proposal.edits):
        raise PolicyError("generated file exceeds byte limit")
    if sum(edit.byte_size for edit in proposal.edits) > MAX_TOTAL_BYTES:
        raise PolicyError("total generated bytes exceed limit")
    paths = [edit.path for edit in proposal.edits]
    if len(set(paths)) != len(paths):
        raise PolicyError("duplicate edit path")
    decisions = {decision.target: decision for decision in proposal.decisions}
    for decision in proposal.decisions:
        planned = decision.plan.intended_test_files
        for path in planned:
            test_path(path, decision.target)
            if path not in context.permitted_test_paths:
                raise PolicyError("planned path outside host context boundary")
        actual = {edit.path for edit in proposal.edits if edit.target == decision.target}
        if set(planned) != actual:
            raise PolicyError("intended test files must match proposed edits")
        if decision.action in (ModelAction.NO_TESTS_NEEDED, ModelAction.UNSUPPORTED):
            if actual:
                raise PolicyError("non-edit decision cannot contain edits")
        elif not actual:
            raise PolicyError("generate/update decision requires edits")
        elif decision.action == ModelAction.UPDATE and not any(
            edit.target == decision.target and edit.operation == EditOperation.UPDATE
            for edit in proposal.edits
        ):
            raise PolicyError("update decision requires at least one update operation")
    validated = []
    for edit in proposal.edits:
        path = test_path(edit.path, edit.target)
        if path not in context.permitted_test_paths:
            raise PolicyError("edit outside host context boundary")
        if edit.target not in decisions:
            raise PolicyError("edit requires a target decision")
        if (decisions[edit.target].action == ModelAction.GENERATE
                and edit.operation != EditOperation.CREATE):
            raise PolicyError("generate decision requires create operations")
        current = _current_content(context.repository_root, path)
        if edit.operation == EditOperation.CREATE:
            if current is not None:
                raise PolicyError("create cannot overwrite an existing target")
            old, mode = b"", CREATE_FILE_MODE
        elif edit.operation == EditOperation.UPDATE:
            if current is None:
                raise PolicyError("update requires an existing target")
            old, mode = current
            if hashlib.sha256(old).hexdigest() != edit.original_sha256:
                raise PolicyError("stale original-content SHA-256")
        else:
            raise PolicyError("unsupported edit operation")
        content = edit.content.encode("utf-8")
        check_weakening(edit.target, old, content)
        validated.append(ValidatedEdit(edit.target, path, edit.operation,
                                       edit.original_sha256, content, mode))
    return tuple(sorted(validated, key=lambda edit: edit.path))
