"""T05 trusted, fixed-operation Docker verifier. No provider or model authority.

Trusted callers supply T02 TrustedChanges, SourceIdentity, a canonical checkout,
and fixed stack profile IDs. Injection is a host-only deterministic testing seam.
No proposal code is ever imported, compiled or executed by the host.
"""

from dataclasses import asdict, dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from types import MappingProxyType
import uuid
import xml.etree.ElementTree as ET

from automation.verifier.profiles import PROFILES, STACK_PROFILES, command
from .contracts import (EditOperation, ExecutionKind, ExecutionResult,
                        FinalHostStatus, HostResult, SourceIdentity, Target, VerifierResult)
from .patches import TrustedChanges
from .policy import (CREATE_FILE_MODE, MAX_EDITED_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES,
                     SUPPORTED_FILE_MODES, PolicyError, ValidatedEdit,
                     check_weakening, normalized_path, test_path)


DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
GIT = "/usr/bin/git"
HOST_ENV = MappingProxyType({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                             "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent",
                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                             "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
                             "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                             "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "core.hooksPath",
                             "GIT_CONFIG_VALUE_1": "/dev/null"})
MAX_REPORT_BYTES = 128 * 1024
MAX_TRANSFER_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_FILES = 1000
WALL_SECONDS = 120
CLIENT_SECONDS = 20
BUILD_SECONDS = 600
MEMORY = 1024 * 1024 * 1024
NANO_CPUS = 2_000_000_000
PIDS = 128
NOFILE = 1024
TMPFS = MappingProxyType({
    "/work": "rw,nosuid,nodev,size=268435456,uid=10001,gid=10001,mode=0700",
    "/tmp": "rw,nosuid,nodev,noexec,size=67108864,uid=10001,gid=10001,mode=0700",
    "/reports": "rw,nosuid,nodev,noexec,size=1048576,uid=10001,gid=10001,mode=0700",
})
BASE_IMAGES = (
    "node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9",
    "python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2",
)
TRUSTED_ROOT = Path(__file__).resolve().parents[2]
# Fixed host policy, never discovered/extended from PR instructions.
TRUSTED_FILES = MappingProxyType({
    "frontend/package.json": "fcdfb41cc4e9e204c9e476ffcb1527d01a6b5bfaafa7be3029e52031dda0f2ea",
    "frontend/package-lock.json": "783aceb1fb35a20c058336bf9aa60f7010c258206b49c7018f40cecdaa60945b",
    "frontend/vite.config.ts": "ffea2bd33b7c57237fbe08674b243399132335aa2e6700677bef244fa364aab4",
    "frontend/tsconfig.json": "82801034ba4da9697c2440cf78260697fce5b145fe0a85809e435503174b1afe",
    "frontend/src/test/setup.ts": "e20f6c79ce7842f29f0dd93957e8230a5cbb3b5f6ee691fef96d4f393b964cb3",
    "backend/requirements.in": "6de733ad24ddc1808e51ecd701cb158be34badc1bbe1e1e5290c677e787a79d6",
    "backend/requirements.txt": "36bb88adf08fdcbe59cdb787649972313342b933dd473d5beff07db23c5c3a81",
    "backend/pyproject.toml": "df5f9d8c32676c51e83fde7510b4a2718968cba12b780153751b636cc2d4643a",
    "backend/tests/conftest.py": "a602131a9d1eeec96088ab31434dc722a11fe45a9d34bbc794f5310c62afb2df",
    "pytest.ini": "56bbf8895685447aefd9a5eb763124dcf1da0fc293965ad9c27bd9e5f62623c2",
})
PREPARATION_FILES = frozenset({"automation/test_agent/README.md"})
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_CONTAINER_NAME = re.compile(r"t05-[0-9a-f]{32}")


class VerificationError(ValueError):
    """Fixed, bounded host messages only."""


class Stale(VerificationError):
    pass


class Unsupported(VerificationError):
    pass


class Unavailable(VerificationError):
    pass


class ReportError(VerificationError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReportError("duplicate report field")
        value[key] = item
    return value


def parse_json(data, maximum=MAX_REPORT_BYTES):
    if type(data) is not bytes or len(data) > maximum:
        raise ReportError("report size limit")
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ReportError("malformed structured report") from None


@dataclass(frozen=True)
class Capture:
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_sha256: str = digest(b"")
    stderr_sha256: str = digest(b"")
    timed_out: bool = False
    duration: float = 0.0

    @property
    def truncated(self):
        return self.stdout_bytes > len(self.stdout) or self.stderr_bytes > len(self.stderr)


class Executor:
    """Production fixed executables/subcommands, no shell or inherited environment.

    Drains both pipes into incremental hashes/counters and bounded byte buffers.
    Killing a Docker client is insufficient: Sandbox always removes the container.
    """

    def run(self, argv, *, timeout=CLIENT_SECONDS, limit=MAX_OUTPUT_BYTES):
        if type(argv) is not tuple or not argv or any(type(a) is not str or "\x00" in a for a in argv):
            raise VerificationError("invalid fixed operation")
        if argv[0] == GIT:
            if len(argv) < 4 or argv[1] != "-C" or argv[3] not in {
                "rev-parse", "config", "diff", "ls-tree", "ls-files", "merge-base"
            }:
                raise VerificationError("unsupported Git operation")
        elif argv[:3] == DOCKER:
            if len(argv) < 4 or argv[3] not in {
                "build", "image", "info", "container", "create", "inspect", "start", "exec", "pause", "rm"
            }:
                raise VerificationError("unsupported Docker operation")
        else:
            raise VerificationError("unsupported executable")
        if not 0 < timeout <= BUILD_SECONDS or not 0 < limit <= MAX_SOURCE_BYTES:
            raise VerificationError("invalid host operation limits")
        started = time.monotonic()
        buffers, hashes, counts = [bytearray(), bytearray()], [hashlib.sha256(), hashlib.sha256()], [0, 0]
        timed_out = False
        client_home = tempfile.TemporaryDirectory(prefix="t05-client-")
        env = dict(HOST_ENV)
        env.update(HOME=client_home.name, DOCKER_CONFIG=client_home.name + "/.docker")
        try:
            proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, start_new_session=True)
        except OSError:
            client_home.cleanup()
            raise Unavailable("fixed host executable unavailable") from None
        try:
            with selectors.DefaultSelector() as selector:
                for index, pipe in enumerate((proc.stdout, proc.stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, index)
                while selector.get_map():
                    remaining = started + timeout - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        index = key.data
                        counts[index] += len(data)
                        hashes[index].update(data)
                        buffers[index].extend(data[:max(0, limit // 2 - len(buffers[index]))])
                remaining = started + timeout - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                if timed_out:
                    os.killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=max(0.01, remaining) if not timed_out else 5)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
            client_home.cleanup()
        return Capture(None if timed_out else proc.returncode, bytes(buffers[0]), bytes(buffers[1]),
                       *counts, hashes[0].hexdigest(), hashes[1].hexdigest(), timed_out,
                       time.monotonic() - started)


def checked(executor, argv, *, timeout=CLIENT_SECONDS, limit=MAX_OUTPUT_BYTES):
    result = executor.run(argv, timeout=timeout, limit=limit)
    if result.timed_out or result.exit_code != 0 or result.truncated:
        raise Unavailable("fixed host preparation operation failed")
    return result.stdout


def safe_read(root, path, maximum=MAX_SOURCE_FILE_BYTES):
    """No-follow descriptor traversal, nonblocking regular single-link files."""
    normalized_path(path)
    directory = fd = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = path.split("/")
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) not in SUPPORTED_FILE_MODES
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > maximum):
            raise Stale("unsafe source file state")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise Stale("source file byte limit")
        after = os.fstat(fd)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise Stale("source changed during inspection")
        return b"".join(chunks), stat.S_IMODE(opened.st_mode)
    except (OSError, PolicyError):
        raise Stale("unsafe or missing source file") from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def _root(root):
    if (not isinstance(root, Path) or not root.is_absolute() or root.is_symlink()
            or not root.is_dir() or root.resolve() != root or any(c in str(root) for c in ",\n\r\x00")):
        raise Stale("checkout must be a canonical trusted directory")
    return root


def safely_missing(root, path):
    """A CREATE must also reject a symlink/special-file ancestor in the checkout."""
    normalized_path(path)
    directory = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = path.split("/")
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                return True
            os.close(directory)
            directory = nxt
        try:
            os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except OSError:
        raise Stale("unsafe create target ancestor") from None
    finally:
        if directory is not None:
            os.close(directory)


def unsupported_config(path):
    normalized_path(path)
    name = path.rsplit("/", 1)[-1]
    return path in TRUSTED_FILES or name in {
        "conftest.py", "pytest.ini", ".coveragerc", "tox.ini", "setup.cfg", "setup.py",
        "pyproject.toml", "package.json", "package-lock.json", "requirements.in", "requirements.txt",
        "uv.lock", "poetry.lock", ".npmrc", ".yarnrc", "pnpm-lock.yaml", "yarn.lock",
    } or name.startswith(("vite.config.", "vitest.config.", "tsconfig", "jest.config.", "babel.config."))


def admitted(path):
    parts = path.lower().split("/")
    if any(part.startswith(".env") or part in {
        ".git", ".github", "automation", "node_modules", ".venv", "venv", ".ssh",
        "__pycache__", ".pytest_cache", ".cache", "coverage", "dist", "build", "credentials",
        "secrets", "credentials.json", "secrets.json", "token", "token.txt",
        "review", "artifacts", "models", "id_rsa", "id_ed25519",
    } or part.endswith((".pem", ".key", ".gguf")) for part in parts):
        return False
    return path in TRUSTED_FILES or (
        path.startswith(("frontend/src/", "frontend/.storybook/")) and path.endswith((".ts", ".tsx", ".css", ".json"))
    ) or (path.startswith(("backend/app/", "backend/tests/")) and path.endswith(".py")) or path == "frontend/index.html"


def recognized(path, target):
    try:
        return test_path(path, target) == path
    except PolicyError:
        return False


def _paths(paths, target):
    if type(paths) is not tuple or len(paths) > 256 or len(set(paths)) != len(paths):
        raise VerificationError("bounded unique test inventory required")
    for path in paths:
        test_path(path, target)
        if any(part.startswith("-") for part in path.split("/")):
            raise VerificationError("option-shaped test path")
    return tuple(path.split("/", 1)[1] for path in paths)


def _changes(changes, source):
    if type(changes) is not TrustedChanges or type(source) is not SourceIdentity:
        raise VerificationError("host SourceIdentity and T02 TrustedChanges required")
    source.__post_init__()
    if (type(changes.edits) is not tuple or not 1 <= len(changes.edits) <= MAX_EDITED_FILES
            or type(changes.changed_files) is not tuple or type(changes.permitted_test_paths) is not tuple):
        raise VerificationError("invalid accepted change envelope")
    for values in (changes.changed_files, changes.permitted_test_paths):
        if len(values) > 100 or len(set(values)) != len(values):
            raise VerificationError("invalid accepted path set")
        for path in values:
            normalized_path(path)
    if len({e.path for e in changes.edits}) != len(changes.edits):
        raise VerificationError("duplicate accepted edits")
    for edit in changes.edits:
        if (type(edit) is not ValidatedEdit or type(edit.target) is not Target
                or type(edit.operation) is not EditOperation or type(edit.content) is not bytes
                or edit.mode not in SUPPORTED_FILE_MODES or len(edit.content) > MAX_FILE_BYTES):
            raise VerificationError("invalid accepted edit")
        _paths((edit.path,), edit.target)
        if edit.path not in changes.permitted_test_paths:
            raise VerificationError("accepted edit outside authorized paths")
        if edit.operation == EditOperation.UPDATE:
            if type(edit.original_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", edit.original_sha256) is None:
                raise VerificationError("invalid original content digest")
        elif edit.original_sha256 is not None or edit.mode != CREATE_FILE_MODE:
            raise VerificationError("invalid create metadata")
    if sum(len(e.content) for e in changes.edits) > MAX_TOTAL_BYTES:
        raise VerificationError("accepted edit total byte limit")


def snapshot(executor, root, source, changes):
    """Cross-check Git metadata plus copied bytes; no PR-controlled executable."""
    _root(root)
    git = lambda *args: checked(executor, (GIT, "-C", str(root), *args), limit=MAX_SOURCE_BYTES)
    if git("rev-parse", "HEAD").strip().decode() != source.head_sha:
        raise Stale("checkout head mismatch")
    if git("rev-parse", "--show-toplevel").strip().decode() != str(root):
        raise Stale("checkout root mismatch")
    origin = git("config", "--get", "remote.origin.url").strip().decode()
    if origin not in {f"https://github.com/{source.repository}.git", f"https://github.com/{source.repository}",
                      f"git@github.com:{source.repository}.git"}:
        raise Stale("checkout repository identity mismatch")
    if git("merge-base", source.base_sha, source.head_sha).strip().decode() != source.merge_base_sha:
        raise Stale("checkout merge-base mismatch")
    changed = tuple(sorted(p.decode() for p in git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                                                   source.merge_base_sha, source.head_sha).split(b"\0") if p))
    if changed != tuple(sorted(changes.changed_files)):
        raise Stale("accepted PR change inventory mismatch")
    if any(unsupported_config(p) for p in changed):
        raise Unsupported("dependency or test configuration changes unsupported")
    # Staged comparison touches immutable objects only. Worktree bytes are
    # checked below without Git clean filters, textconv, hooks or fsmonitor.
    dirty = {p.decode() for p in git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD").split(b"\0") if p}
    if dirty - PREPARATION_FILES:
        raise Stale("dirty tracked checkout")
    untracked = [p.decode() for p in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    if any(admitted(p) for p in untracked):
        raise Stale("untracked application material")
    files, total, inventory = {}, 0, git("ls-tree", "-r", "-z", "--full-tree", source.head_sha)
    for entry in inventory.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        path = normalized_path(raw_path.decode())
        if mode != "100644" or kind != "blob":
            raise Stale("unexpected tracked file type or mode")
        data, file_mode = safe_read(root, path)
        if (path not in PREPARATION_FILES and
                hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != blob):
            raise Stale("dirty tracked checkout content")
        if not admitted(path):
            continue
        if unsupported_config(path) and path not in TRUSTED_FILES:
            raise Unsupported("unknown executable runner configuration")
        if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != blob:
            raise Stale("checkout content does not match exact head")
        if path in TRUSTED_FILES and digest(data) != TRUSTED_FILES[path]:
            raise Unsupported("checkout trusted configuration mismatch")
        files[path] = (data, file_mode)
        total += len(data)
        if len(files) > MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
            raise Unsupported("sanitized workspace budget exceeded")
    if not set(TRUSTED_FILES).issubset(files):
        raise Unsupported("required trusted configuration missing")
    for path in files:
        if path.startswith("frontend/src/") and re.search(r"\.(?:test|spec)\.[^.]+$", path) and not recognized(path, Target.FRONTEND):
            raise Unsupported("unsupported frontend test discovery convention")
        if path.startswith("backend/tests/") and path.endswith("_test.py") and not recognized(path, Target.BACKEND):
            raise Unsupported("unsupported backend test discovery convention")
    for edit in changes.edits:
        if edit.operation == EditOperation.UPDATE:
            if edit.path not in files or digest(files[edit.path][0]) != edit.original_sha256 or files[edit.path][1] != edit.mode:
                raise Stale("accepted update does not match checkout")
            old = files[edit.path][0]
        else:
            if edit.path in files or not safely_missing(root, edit.path):
                raise Stale("accepted create target is present")
            old = b""
        check_weakening(edit.target, old, edit.content)
    # Bracket the snapshot; content was independently bound to Git blobs.
    if git("rev-parse", "HEAD").strip().decode() != source.head_sha:
        raise Stale("checkout changed during preparation")
    return files


def apply_edit(root, edit):
    """Revalidate and write through the same descriptor in a private temp tree."""
    _paths((edit.path,), edit.target)
    directory = fd = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = edit.path.split("/")
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        if edit.operation == EditOperation.CREATE:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         edit.mode, dir_fd=directory)
        elif edit.operation == EditOperation.UPDATE:
            before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            fd = os.open(parts[-1], os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            opened = os.fstat(fd)
            if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) != edit.mode or opened.st_size > MAX_FILE_BYTES
                    or digest(os.read(fd, MAX_FILE_BYTES + 1)) != edit.original_sha256):
                raise Stale("update content or file state changed before write")
            current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_nlink) != (opened.st_dev, opened.st_ino, 1):
                raise Stale("update target substituted before write")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            raise Stale("unexpected edit operation")
        view = memoryview(edit.content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Stale("temporary edit write failed")
            view = view[count:]
        os.fchmod(fd, edit.mode)
        current, opened = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False), os.fstat(fd)
        if (current.st_dev, current.st_ino, current.st_nlink) != (opened.st_dev, opened.st_ino, 1):
            raise Stale("temporary edit target substituted during write")
    except OSError:
        raise Stale("unsafe temporary edit target") from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def prepare_states(directory, files, changes):
    roots = {}
    for kind in ExecutionKind:
        root = directory / str(kind)
        root.mkdir(mode=0o755)
        for path, (data, mode) in files.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            dest.write_bytes(data)
            dest.chmod(mode)
        if kind == ExecutionKind.PATCHED:
            for edit in changes.edits:
                apply_edit(root, edit)
        else:
            updates = [e.path.split("/", 1)[1] for e in changes.edits
                       if e.target == Target.FRONTEND and e.operation == EditOperation.UPDATE]
            if updates:
                config = parse_json(files["frontend/tsconfig.json"][0])
                # Extends replaces exclude; preserve the project's existing values.
                excluded = config.get("exclude", ["node_modules", "bower_components", "jspm_packages"])
                if type(excluded) is not list or any(type(v) is not str for v in excluded):
                    raise Unsupported("unsupported trusted TypeScript exclusion configuration")
                (root / "frontend/tsconfig.verifier.json").write_bytes(canonical({
                    "extends": "./tsconfig.json", "exclude": excluded + sorted(updates)}))
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        roots[kind] = root
    return roots


@dataclass(frozen=True)
class ImageIdentity:
    image_id: str
    repo_digests: tuple[str, ...]
    base_images: tuple[str, ...]
    dockerfile_sha256: str
    manifests: tuple[tuple[str, str], ...]
    build_digest: str
    build_exit_code: int
    build_output: Capture


def build_entries():
    """No checkout/model input can supply build context contents."""
    entries = {}
    for dest, source in {
        "Dockerfile": "automation/verifier/Dockerfile", "runner.py": "automation/verifier/runner.py",
        "profiles.py": "automation/verifier/profiles.py", "pytest_evidence.py": "automation/verifier/pytest_evidence.py",
        "package.json": "frontend/package.json", "package-lock.json": "frontend/package-lock.json",
        "requirements.txt": "backend/requirements.txt",
    }.items():
        data, _ = safe_read(TRUSTED_ROOT, source)
        if source in TRUSTED_FILES and digest(data) != TRUSTED_FILES[source]:
            raise Unavailable("trusted build dependency mismatch")
        entries[dest] = data
    refs = tuple(re.findall(r"^FROM (\S+)", entries["Dockerfile"].decode(), re.M))
    if refs != BASE_IMAGES:
        raise Unavailable("trusted base image reference mismatch")
    return entries


def build_image(executor=None):
    """Separate build plane: only fixed trusted installation files enter context."""
    executor = executor or Executor()
    entries = build_entries()
    refs = BASE_IMAGES
    manifests = tuple(sorted((p, digest(data)) for p, data in entries.items()))
    build_digest = digest(canonical(manifests))
    with tempfile.TemporaryDirectory(prefix="t05-build-") as temp:
        root = Path(temp)
        for path, data in entries.items():
            (root / path).write_bytes(data)
        result = executor.run((*DOCKER, "build", "--pull", "--no-cache", "--network", "default",
                               "--label", "t05.build=" + build_digest, "--tag", "t05-verifier:" + build_digest,
                               str(root)), timeout=BUILD_SECONDS)
        if result.timed_out or result.exit_code != 0:
            raise Unavailable("trusted verifier image build failed")
    raw = checked(executor, (*DOCKER, "image", "inspect", "t05-verifier:" + build_digest), limit=512 * 1024)
    value = parse_json(raw, 256 * 1024)
    if type(value) is not list or len(value) != 1 or _IMAGE_ID.fullmatch(value[0].get("Id", "")) is None:
        raise Unavailable("invalid built image identity")
    digests = value[0].get("RepoDigests") or []
    if type(digests) is not list or any(re.fullmatch(r"[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64}", d) is None for d in digests):
        raise Unavailable("invalid image repository digest")
    return ImageIdentity(value[0]["Id"], tuple(digests), refs, digest(entries["Dockerfile"]),
                         manifests, build_digest, result.exit_code, result)


def validate_image(executor, image):
    if (type(image) is not ImageIdentity or _IMAGE_ID.fullmatch(image.image_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", image.build_digest) is None or image.base_images != BASE_IMAGES):
        raise Unavailable("trusted built image provenance required")
    manifests = tuple(sorted((p, digest(data)) for p, data in build_entries().items()))
    if (image.manifests != manifests or image.build_digest != digest(canonical(manifests))
            or image.dockerfile_sha256 != dict(manifests)["Dockerfile"] or image.build_exit_code != 0):
        raise Unavailable("built image does not match current trusted preparation")
    data = parse_json(checked(executor, (*DOCKER, "image", "inspect", image.image_id), limit=512 * 1024), 256 * 1024)
    if type(data) is not list or len(data) != 1:
        raise Unavailable("invalid image inspection")
    config = data[0].get("Config", {})
    if (data[0].get("Id") != image.image_id or config.get("User") != "10001:10001"
            or config.get("Entrypoint") != ["/usr/local/bin/python3.12", "-I", "/opt/verifier/runner.py"]
            or config.get("Cmd") != ["idle"] or config.get("Labels", {}).get("t05.build") != image.build_digest
            or config.get("Volumes") or config.get("Healthcheck")):
        raise Unavailable("image configuration mismatch")
    env = config.get("Env")
    allowed = {"PATH", "LANG", "PYTHON_VERSION", "PYTHON_SHA256", "GPG_KEY"}
    if type(env) is not list or any(type(e) is not str or e.split("=", 1)[0] not in allowed for e in env):
        raise Unavailable("unexpected image environment")
    return tuple(env)


def validate_daemon(executor):
    options = parse_json(checked(executor, (*DOCKER, "info", "--format", "{{json .SecurityOptions}}")))
    if type(options) is not list or "name=seccomp,profile=builtin" not in options:
        raise Unavailable("Docker builtin default seccomp unavailable")


def create_argv(name, image_id, input_root):
    if _CONTAINER_NAME.fullmatch(name) is None or _IMAGE_ID.fullmatch(image_id) is None:
        raise VerificationError("invalid host container or image identifier")
    _root(input_root)
    argv = (*DOCKER, "create", "--name", name, "--user", "10001:10001",
            "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--memory", str(MEMORY), "--memory-swap", str(MEMORY),
            "--cpus", "2", "--pids-limit", str(PIDS), "--ulimit", f"nofile={NOFILE}:{NOFILE}",
            "--ipc", "none", "--log-driver", "none",
            "--mount", f"type=bind,source={input_root},target=/input,readonly,bind-propagation=rprivate")
    for path, options in TMPFS.items():
        argv += ("--tmpfs", path + ":" + options)
    return argv + (image_id,)


def validate_container(value, image, input_root, image_env):
    """Check effective daemon configuration, not merely requested CLI flags."""
    c, h, state = value.get("Config", {}), value.get("HostConfig", {}), value.get("State", {})
    if (value.get("Image") != image.image_id or c.get("User") != "10001:10001"
            or tuple(c.get("Env") or ()) != image_env
            or c.get("Entrypoint") != ["/usr/local/bin/python3.12", "-I", "/opt/verifier/runner.py"]
            or c.get("Cmd") != ["idle"] or state.get("Running") is not False
            or h.get("NetworkMode") != "none" or h.get("Privileged") is not False
            or h.get("ReadonlyRootfs") is not True or h.get("CapDrop") != ["ALL"] or h.get("CapAdd")
            or set(h.get("SecurityOpt") or ()) != {"no-new-privileges"}
            or h.get("Memory") != MEMORY or h.get("MemorySwap") != MEMORY
            or h.get("NanoCpus") != NANO_CPUS or h.get("PidsLimit") != PIDS
            or h.get("IpcMode") != "none" or h.get("PidMode") not in ("", None)
            or h.get("Devices") or h.get("DeviceRequests") or h.get("DeviceCgroupRules")
            or h.get("VolumesFrom") or h.get("Binds") or h.get("ExtraHosts")
            or h.get("PortBindings") or h.get("PublishAllPorts")
            or h.get("Tmpfs") != dict(TMPFS) or h.get("LogConfig", {}).get("Type") != "none"
            or h.get("Ulimits") != [{"Name": "nofile", "Hard": NOFILE, "Soft": NOFILE}]
            or h.get("RestartPolicy", {}).get("Name") not in ("no", "")):
        raise Unavailable("effective Docker isolation rejected")
    mounts = value.get("Mounts", [])
    binds = [m for m in mounts if m.get("Type") != "tmpfs"]
    if len(binds) != 1 or any((binds[0].get(key) != expected) for key, expected in {
        "Type": "bind", "Source": str(input_root), "Destination": "/input", "RW": False,
        "Propagation": "rprivate",
    }.items()) or any(m.get("Destination") not in TMPFS for m in mounts if m.get("Type") == "tmpfs"):
        raise Unavailable("effective Docker mount allowlist rejected")


def decode_reports(data, expected):
    packet = parse_json(data, MAX_TRANSFER_BYTES)
    if (type(packet) is not dict or set(packet) != {"quiesced", "reports"} or packet["quiesced"] is not True
            or type(packet["reports"]) is not dict or set(packet["reports"]) != set(expected)):
        raise ReportError("report transfer schema or inventory mismatch")
    reports = {}
    for name, encoded in packet["reports"].items():
        if type(encoded) is not str or len(encoded) > 4 * ((MAX_REPORT_BYTES + 2) // 3):
            raise ReportError("report transfer byte limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ReportError("invalid report transfer encoding") from None
        if len(content) > MAX_REPORT_BYTES or base64.b64encode(content).decode() != encoded:
            raise ReportError("noncanonical or oversized report transfer")
        reports[name] = content
    return reports


class Sandbox:
    def __init__(self, executor, image, image_env):
        self.executor, self.image, self.image_env = executor, image, image_env

    def execute(self, profile_id, root, paths):
        if profile_id not in PROFILES:
            raise VerificationError("unknown trusted profile")
        p = PROFILES[profile_id]
        relative = _paths(paths, Target(p.stack))
        if p.command_id == "tsc" and paths:
            raise VerificationError("typecheck does not accept executable arguments")
        name, container = "t05-" + uuid.uuid4().hex, None
        try:
            raw = checked(self.executor, create_argv(name, self.image.image_id, root))
            container = raw.strip().decode()
            if _CONTAINER_ID.fullmatch(container) is None:
                raise Unavailable("invalid container ID")
            values = parse_json(checked(self.executor, (*DOCKER, "inspect", container), limit=512 * 1024), 256 * 1024)
            if type(values) is not list or len(values) != 1 or values[0].get("Id") != container:
                raise Unavailable("invalid container inspection")
            validate_container(values[0], self.image, root, self.image_env)
            checked(self.executor, (*DOCKER, "start", container))
            # Main container is an image-owned idle process. Docker exec gives a
            # host-observed exit code for the actual tool, not a test-written file.
            result = self.executor.run((*DOCKER, "exec", container, "/usr/local/bin/python3.12", "-I",
                                        "/opt/verifier/runner.py", profile_id, *relative), timeout=WALL_SECONDS)
            if result.timed_out:
                return result, {}, "sandbox wall timeout"
            reports, error = {}, None
            if p.command_id != "tsc":
                exported = self.executor.run((*DOCKER, "exec", container, "/usr/local/bin/python3.12", "-I",
                                              "/opt/verifier/runner.py", "export", profile_id),
                                             limit=2 * MAX_TRANSFER_BYTES)
                names = ["tests.json"] if p.stack == "frontend" else ["tests.xml", "collection.json"]
                if p.coverage:
                    names.append("coverage/coverage-summary.json" if p.stack == "frontend" else "coverage.json")
                if exported.exit_code != 0 or exported.timed_out or exported.truncated:
                    error = "missing, unsafe or oversized framework report"
                else:
                    try:
                        reports = decode_reports(exported.stdout, names)
                    except ReportError:
                        error = "unsafe or oversized framework report transfer"
            # The image exporter stops remaining test processes before reading
            # tmpfs. Freeze the whole container before host parsing/cleanup too.
            checked(self.executor, (*DOCKER, "pause", container))
            state = parse_json(checked(self.executor, (*DOCKER, "inspect", container), limit=512 * 1024), 256 * 1024)
            if (len(state) != 1 or state[0].get("State", {}).get("Paused") is not True
                    or state[0].get("State", {}).get("OOMKilled") is not False):
                raise Unavailable("sandbox did not quiesce safely")
            return result, reports, error
        finally:
            # Name is host-generated before creation, so even a create/client
            # timeout or malformed returned ID has a safe cleanup handle.
            removed = None
            for _ in range(3):
                try:
                    removed = self.executor.run((*DOCKER, "rm", "--force", name))
                    if removed.exit_code == 0 and not removed.timed_out:
                        break
                except Exception:
                    continue
            # A create failure may leave no container. A successful daemon list
            # proves absence even when rm reports "no such container"; a client
            # failure cannot be mistaken for successful removal.
            remaining = self.executor.run((*DOCKER, "container", "ls", "--all", "--filter",
                                           "name=^/" + name + "$", "--format", "{{.ID}}"))
            if remaining.exit_code != 0 or remaining.timed_out or remaining.truncated or remaining.stdout.strip():
                raise Unavailable("container removal could not be confirmed")


@dataclass(frozen=True)
class Counts:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    files: tuple[tuple[str, int], ...]


def count(value):
    if type(value) is not int or not 0 <= value <= 100000:
        raise ReportError("impossible report count")
    return value


def frontend_counts(data, paths):
    report = parse_json(data)
    if type(report) is not dict or type(report.get("success")) is not bool or type(report.get("testResults")) is not list:
        raise ReportError("invalid Vitest JSON schema")
    total, passed, failed, pending, todo = (count(report.get(key)) for key in (
        "numTotalTests", "numPassedTests", "numFailedTests", "numPendingTests", "numTodoTests"))
    if total != passed + failed + pending + todo:
        raise ReportError("inconsistent Vitest counts")
    suites, good_suites, bad_suites, pending_suites = (count(report.get(key)) for key in (
        "numTotalTestSuites", "numPassedTestSuites", "numFailedTestSuites", "numPendingTestSuites"))
    if suites != good_suites + bad_suites + pending_suites:
        raise ReportError("inconsistent Vitest suite counts")
    statuses, files, errors = {}, {}, 0
    for file in report["testResults"]:
        if type(file) is not dict or type(file.get("name")) is not str or type(file.get("assertionResults")) is not list:
            raise ReportError("invalid Vitest file schema")
        prefix = "/work/repo/frontend/"
        if not file["name"].startswith(prefix):
            raise ReportError("unexpected Vitest report path")
        path = "frontend/" + file["name"][len(prefix):]
        if path in files or path not in paths or file.get("status") not in ("passed", "failed"):
            raise ReportError("duplicate or unexpected collected file")
        files[path] = len(file["assertionResults"])
        seen = set()
        for case in file["assertionResults"]:
            if type(case) is not dict or type(case.get("fullName")) is not str:
                raise ReportError("invalid Vitest assertion schema")
            if case["fullName"] in seen or case.get("status") not in ("passed", "failed", "pending", "skipped", "todo"):
                raise ReportError("duplicate or invalid assertion result")
            seen.add(case["fullName"])
            key = case["status"]
            statuses[key] = statuses.get(key, 0) + 1
        if file["status"] == "failed" and not any(c["status"] == "failed" for c in file["assertionResults"]):
            errors += 1
    if (set(files) != set(paths) or sum(files.values()) != total or statuses.get("passed", 0) != passed
            or statuses.get("failed", 0) != failed or statuses.get("todo", 0) != todo
            or statuses.get("pending", 0) + statuses.get("skipped", 0) != pending
            or report["success"] != (not bad_suites and not failed)):
        raise ReportError("Vitest report inventory or summary mismatch")
    return Counts(total, passed, failed, errors + pending_suites, pending + todo, tuple(sorted(files.items())))


def backend_counts(data, collection, paths):
    if len(data) > MAX_REPORT_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ReportError("unsafe JUnit XML")
    try:
        root = ET.fromstring(data)
        if root.tag != "testsuites" or len(root) != 1 or root[0].tag != "testsuite":
            raise ReportError("invalid pinned pytest JUnit schema")
        suite = root[0]
        values = []
        for key in ("tests", "failures", "errors", "skipped"):
            raw = suite.get(key, "")
            if re.fullmatch(r"0|[1-9][0-9]{0,5}", raw) is None:
                raise ReportError("invalid JUnit count")
            values.append(count(int(raw)))
        tests, failed, errors, skipped = values
        observed, seen, cases = {"failure": 0, "error": 0, "skipped": 0}, set(), 0
        for case in suite:
            if case.tag == "properties":
                continue
            if case.tag != "testcase" or not case.get("name") or not case.get("classname"):
                raise ReportError("invalid JUnit testcase")
            identity = (case.get("classname"), case.get("name"))
            if identity in seen:
                raise ReportError("duplicate JUnit testcase")
            seen.add(identity)
            cases += 1
            outcomes = [child.tag for child in case if child.tag in observed]
            if len(outcomes) > 1:
                raise ReportError("ambiguous JUnit outcome")
            for outcome in outcomes:
                observed[outcome] += 1
        if (tests != cases or observed != {"failure": failed, "error": errors, "skipped": skipped}
                or failed + errors + skipped > tests):
            raise ReportError("inconsistent JUnit counts")
        collected = parse_json(collection)
        if type(collected) is not dict or set(collected) != {"collected", "files"} or type(collected["files"]) is not dict:
            raise ReportError("invalid pytest collection schema")
        total = count(collected["collected"])
        files = {"backend/" + path: count(n) for path, n in collected["files"].items()}
        # pytest's session.items is genuinely empty when an explicit file has
        # zero tests. Preserve that observed zero rather than losing it to the
        # usual nonempty inventory check; zero can never pass verification.
        inventory_matches = set(files) == set(paths) or (total == 0 and not files)
        if not inventory_matches or sum(files.values()) != total or (not errors and tests != total):
            raise ReportError("pytest collection inventory mismatch")
        return Counts(total, tests - failed - errors - skipped, failed, errors, skipped, tuple(sorted(files.items())))
    except (ET.ParseError, ValueError, RecursionError):
        raise ReportError("malformed or inconsistent pytest evidence") from None


@dataclass(frozen=True)
class CommandEvidence:
    profile_id: str
    command_id: str
    kind: ExecutionKind
    image_id: str
    source_sha: str
    changes_sha256: str
    duration_seconds: float
    exit_code: int | None
    timed_out: bool
    stdout_bytes: int
    stderr_bytes: int
    stdout_sha256: str
    stderr_sha256: str
    reports: tuple[tuple[str, int, str], ...]
    counts: Counts | None
    error: str | None
    passed: bool


@dataclass(frozen=True)
class VerificationReport:
    host_result: HostResult
    changes_sha256: str
    image: ImageIdentity | None
    commands: tuple[CommandEvidence, ...]
    reason: str | None
    output_directory: Path  # Host-only handle; excluded from portable metadata.

    def to_bytes(self):
        return canonical({"host_result": asdict(self.host_result), "changes_sha256": self.changes_sha256,
                          "image": None if self.image is None else {
                              k: v for k, v in asdict(self.image).items() if k != "build_output"},
                          "commands": [asdict(c) for c in self.commands], "reason": self.reason})


class Verifier:
    def __init__(self, *, executor=None):
        self.executor = executor or Executor()

    def verify(self, source, repository_root, changes, *, profile_ids=("frontend-v1", "backend-v1"), image=None):
        _changes(changes, source)
        if (type(profile_ids) is not tuple or not profile_ids or len(set(profile_ids)) != len(profile_ids)
                or any(p not in STACK_PROFILES for p in profile_ids)):
            raise VerificationError("unknown trusted stack profile")
        stacks = {p.split("-", 1)[0] for p in profile_ids}
        required = {str(e.target) for e in changes.edits} | {
            p.split("/", 1)[0] for p in changes.changed_files if p.startswith(("frontend/", "backend/"))}
        if required - stacks:
            raise VerificationError("required stack profile omitted")
        output = Path(tempfile.mkdtemp(prefix="t05-evidence-"))
        commands, records, reason = [], [], None
        changes_digest = digest(changes.to_bytes())
        status = FinalHostStatus.UNAVAILABLE
        try:
            if changes.source != source:
                raise Stale("accepted changes source mismatch")
            files = snapshot(self.executor, repository_root, source, changes)
            image = image or build_image(self.executor)
            env = validate_image(self.executor, image)
            validate_daemon(self.executor)
            sandbox = Sandbox(self.executor, image, env)
            with tempfile.TemporaryDirectory(prefix="t05-input-") as directory:
                roots = prepare_states(Path(directory), files, changes)
                updates = {e.path for e in changes.edits if e.operation == EditOperation.UPDATE}
                full = {target: tuple(sorted(p for p in files if recognized(p, target))) for target in Target}
                patched = {target: tuple(sorted(set(full[target]) | {e.path for e in changes.edits if e.target == target})) for target in Target}
                steps = [step for selected in profile_ids for step in STACK_PROFILES[selected]]
                # All baseline guards precede any patched execution, for both stacks.
                steps.sort(key=lambda step: PROFILES[step].kind != "baseline")
                status = FinalHostStatus.VERIFIED
                for index, step in enumerate(steps):
                    p = PROFILES[step]
                    target, kind = Target(p.stack), ExecutionKind(p.kind)
                    if p.command_id == "tsc":
                        paths = ()
                    elif kind == ExecutionKind.BASELINE:
                        paths = tuple(path for path in full[target] if path not in updates)
                    elif p.targeted:
                        paths = tuple(e.path for e in changes.edits if e.target == target)
                        if not paths:
                            continue
                    else:
                        paths = patched[target]
                    if not paths and p.command_id != "tsc":
                        # Never fall back to implicit discovery or manufacture a count.
                        raise Unsupported("empty baseline guard inventory")
                    result, reports, error = sandbox.execute(step, roots[kind], paths)
                    references = []
                    for name, data in reports.items():
                        reference = f"reports/{index}/" + name
                        dest = output / reference
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                        references.append((reference, len(data), digest(data)))
                    counts = None
                    if p.command_id != "tsc" and not error:
                        try:
                            counts = (frontend_counts(reports["tests.json"], paths) if target == Target.FRONTEND else
                                      backend_counts(reports["tests.xml"], reports["collection.json"], paths))
                            if p.coverage:
                                coverage = parse_json(reports["coverage/coverage-summary.json" if target == Target.FRONTEND else "coverage.json"])
                                if type(coverage) is not dict or ("total" if target == Target.FRONTEND else "totals") not in coverage:
                                    raise ReportError("invalid coverage evidence schema")
                        except (ReportError, KeyError):
                            error = "framework report validation failed"
                    passed = (not error and not result.timed_out and result.exit_code == 0
                              and (p.command_id == "tsc" or (counts is not None and counts.collected > 0
                                   and counts.passed > 0 and not counts.failed and not counts.errors
                                   and all(n > 0 for _, n in counts.files))))
                    evidence = CommandEvidence(step, p.command_id, kind, image.image_id, source.head_sha,
                                               changes_digest, result.duration, result.exit_code, result.timed_out,
                                               result.stdout_bytes, result.stderr_bytes, result.stdout_sha256,
                                               result.stderr_sha256, tuple(references), counts, error, passed)
                    commands.append(evidence)
                    evidence_ref = f"commands/{index}.json"
                    (output / "commands").mkdir(exist_ok=True)
                    (output / evidence_ref).write_bytes(canonical(asdict(evidence)))
                    if p.command_id != "tsc":
                        outcome = (ExecutionResult.PASSED if passed else ExecutionResult.TIMEOUT if result.timed_out
                                   else ExecutionResult.FAILED if result.exit_code not in (0, None) else ExecutionResult.ERROR)
                        records.append(VerifierResult(step, p.command_id, kind, outcome,
                                                      counts.collected if counts is not None else None,
                                                      result.exit_code, evidence_ref,
                                                      len(result.stdout) + len(result.stderr)))
                    if not passed:
                        status = (FinalHostStatus.BASELINE_FAILED if kind == ExecutionKind.BASELINE
                                  else FinalHostStatus.VERIFICATION_FAILED)
                        reason = "required baseline guard failed" if kind == ExecutionKind.BASELINE else "required patched verification failed"
                        break
        except Stale:
            status, reason = FinalHostStatus.STALE, "source or accepted edit mismatch"
        except Unsupported:
            status, reason = FinalHostStatus.UNSUPPORTED, "unsupported dependency, runner configuration or inventory"
        except (Unavailable, OSError):
            status, reason = FinalHostStatus.UNAVAILABLE, "trusted image or Docker runtime unavailable"
        except (ReportError, PolicyError, UnicodeError, ValueError):
            status, reason = FinalHostStatus.STALE, "unsafe source or accepted changes"
        report = VerificationReport(HostResult(source, status, tuple(records)), changes_digest,
                                    image, tuple(commands), reason, output)
        (output / "verification.json").write_bytes(report.to_bytes())
        return report
