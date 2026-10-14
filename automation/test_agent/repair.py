"""Bounded compiler feedback. Raw verifier output is never a model input."""

from dataclasses import asdict, dataclass
import re
from types import MappingProxyType

from .contracts import SourceIdentity, Target
from .policy import test_path
from .provider import canonical, digest


MAX_DIAGNOSTICS = 8
MAX_MESSAGE_CHARS = 256
MAX_POSITION = 65536
# Compiler messages can quote arbitrary source literals, including secrets.
# Supply a fixed explanation of each eligible code instead of those literals.
MESSAGES = MappingProxyType({
    "UNRESOLVED_RELATIVE_IMPORT": "A relative import does not resolve from this test path; use only captured repository modules and the host-provided import contract.",
    "UNKNOWN_PACKAGE_IMPORT": "A package import is not declared by the captured frontend package manifest; use only host-listed installed package roots.",
    "TS1005": "A required TypeScript token is missing; check syntax at this position.",
    "TS1109": "An expression is required at this position.",
    "TS1128": "A declaration or statement is required at this position.",
    "TS1161": "Unterminated regular expression literal; check for JSX in a .ts file.",
    "TS2304": "Cannot find the referenced name; use the captured current exports and imports.",
    "TS2305": "The imported member is not exported by the referenced module.",
    "TS2307": "Cannot find the imported module or its type declarations.",
    "TS2322": "The assigned value has an incompatible type.",
    "TS2339": "The referenced property does not exist on this type.",
    "TS2345": "An argument has an incompatible type.",
    "TS2554": "The call has an incorrect number of arguments.",
    "TS2741": "A required property is missing from this value.",
    "TS2769": "No overload matches this call.",
    "TS7006": "A parameter has an implicit any type.",
    "TS7031": "A binding element has an implicit any type.",
})
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LINE = re.compile(r"([^()\r\n]+)\(([1-9][0-9]{0,4}),([1-9][0-9]{0,4})\): error (TS[0-9]{4}): (.+)")


def language_contract(path):
    path = test_path(path, Target.FRONTEND)
    return {"language": "typescript", "jsx_allowed": path.endswith(".tsx")}


@dataclass(frozen=True)
class CompilerDiagnostic:
    code: str
    line: int
    column: int
    message: str

    def __post_init__(self):
        if (type(self.code) is not str or self.code not in MESSAGES
                or type(self.line) is not int or not 1 <= self.line <= MAX_POSITION
                or type(self.column) is not int or not 1 <= self.column <= MAX_POSITION
                or self.message != MESSAGES[self.code]):
            raise ValueError("invalid bounded compiler diagnostic")


def compiler_diagnostics(stdout, stderr, path, content):
    """Fail closed on mixed files, non-diagnostic logs, clipping or unknown codes.

    Called only for the trusted, non-pretty tsc operation after passing guards.
    All lines must be confidently associated with the sole accepted test file.
    """
    language_contract(path)
    try:
        text = _ANSI.sub("", (stdout + stderr).decode("utf-8"))
        # Reject controls rather than allowing them to disguise paths/lines.
        if any(ord(c) < 32 and c not in "\n\r\t" or 127 <= ord(c) <= 159 for c in text):
            return ()
        lines = [line for line in text.splitlines() if line.strip()]
        if not 1 <= len(lines) <= MAX_DIAGNOSTICS:
            return ()
        source_lines = content.decode("utf-8").splitlines()
        allowed = {path, path.removeprefix("frontend/"),
                   "/work/repo/" + path}
        result = []
        for line in lines:
            match = _LINE.fullmatch(line)
            if match is None:
                return ()
            filename, row, column, code, message = match.groups()
            row, column = int(row), int(column)
            if (filename not in allowed or code not in MESSAGES or len(message) > MAX_MESSAGE_CHARS
                    or row > len(source_lines) or column > len(source_lines[row - 1]) + 1):
                return ()
            result.append(CompilerDiagnostic(code, row, column, MESSAGES[code]))
        return tuple(result)
    except (UnicodeError, ValueError):
        return ()


@dataclass(frozen=True)
class CompilerFailure:
    """Host-only feedback; never reconstructed from model/PR/transport JSON."""
    source: SourceIdentity
    path: str
    changes_sha256: str
    previous_content_sha256: str
    verification_sha256: str
    diagnostics: tuple[CompilerDiagnostic, ...]

    def __post_init__(self):
        if type(self.source) is not SourceIdentity:
            raise ValueError("invalid repair source")
        self.source.__post_init__()
        language_contract(self.path)
        for value in (self.changes_sha256, self.previous_content_sha256, self.verification_sha256):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("invalid repair binding")
        if type(self.diagnostics) is not tuple or not 1 <= len(self.diagnostics) <= MAX_DIAGNOSTICS:
            raise ValueError("invalid repair diagnostics")
        for diagnostic in self.diagnostics:
            if type(diagnostic) is not CompilerDiagnostic:
                raise ValueError("invalid repair diagnostic type")
            diagnostic.__post_init__()

    @property
    def sha256(self):
        return digest(canonical(asdict(self)))


REPAIR_INSTRUCTIONS = """You are repairing your previously generated unit test.
Keep EXACTLY the accepted path. Return COMPLETE replacement file content.
Modify no application source. Fix only the reported compiler or import problem.
Do not invent dependencies or use packages outside allowed_package_roots.
Relative imports must resolve from test_path. A direct selected-source import
must use required_subject_import. Return the complete replacement file only.
Preserve the intended behavioral coverage and all existing tests/assertions.
Do not weaken expectations merely to pass, skip assertions, or add skip/only.
The host language contract is authoritative: do not use JSX when jsx_allowed=false.
Do not use Markdown fences. Do not explain; return only the trusted schema.
"""
