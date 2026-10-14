"""Trusted T03 evidence collector; repository text is data, never instructions.

No local checkout reads, execution, test-needed decisions, models, or edits.
Only this module's host constants and fixed layout heuristics select evidence.
"""

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .contracts import SourceIdentity
from .github import (
    CollectionError, GitHubReader, MAX_CHANGED_FILES, MAX_COMPARE_COMMITS,
    MAX_PAGES, MAX_REQUESTS, MAX_RESPONSE_BYTES, MAX_RESPONSE_TOTAL_BYTES,
    MAX_TREE_ENTRIES, integer, object_value, repository, sha,
)
from .policy import PolicyError, TrustedContext, normalized_path


MAX_PATCH_BYTES = 16 * 1024
MAX_SOURCE_BYTES = 64 * 1024
MAX_TEST_BYTES = 32 * 1024
MAX_CONFIG_FIXTURE_BYTES = 16 * 1024
MAX_TOTAL_CONTEXT_BYTES = 512 * 1024  # Also bounds the complete canonical JSON.
MAX_NEARBY_TEST_FILES = 16
MAX_CONFIG_FIXTURE_FILES = 8
MAX_TEST_SCAN_FILES = 64
MAX_TEST_SCAN_BYTES = 256 * 1024
MAX_PERMITTED_TEST_PATHS = 32
CONFIG_PATHS = (
    "frontend/package.json", "frontend/vite.config.ts", "frontend/src/test/setup.ts",
    "backend/pyproject.toml", "backend/tests/conftest.py", "pytest.ini",
)
FIXTURE_PATHS = ("frontend/src/test/todoFixture.ts", "backend/tests/mocks.py")


def _path(value):
    try:
        return normalized_path(value)
    except PolicyError:
        raise CollectionError("unsupported repository path") from None


def _sensitive(path):
    parts = path.lower().split("/")
    return any(part.startswith(".env") or part in {
        ".git", ".ssh", "credentials", "credentials.json", "secrets", "secrets.json",
        "id_rsa", "id_ed25519", "token", "token.txt",
    } or part.endswith((".pem", ".key")) for part in parts)


def _test(path):
    if _sensitive(path):
        return False
    return (path.startswith("frontend/src/") and path.endswith((".test.ts", ".test.tsx"))
            and PurePosixPath(path).name not in (".test.ts", ".test.tsx")) or (
        path.startswith("backend/tests/")
        and re.fullmatch(r"test_[A-Za-z0-9_.-]+\.py", PurePosixPath(path).name) is not None)


def _source(path):
    return not _sensitive(path) and not _test(path) and (
        (path.startswith("frontend/src/") and path.endswith((".ts", ".tsx")))
        or (path.startswith("backend/app/") and path.endswith(".py")))


def _supported(path):
    return not _sensitive(path) and (_source(path) or _test(path)
                                     or path in CONFIG_PATHS + FIXTURE_PATHS)


@dataclass(frozen=True)
class WorkflowIdentity:
    run_id: int
    workflow_id: int
    run_attempt: int
    repository_id: int
    event: str
    head_sha: str


def _repo_metadata(value, expected_repository, expected_id=None):
    obj = object_value(value)
    if repository(obj.get("full_name")) != expected_repository:
        raise CollectionError("repository identity mismatch")
    identifier = integer(obj.get("id"), 1)
    if expected_id is not None and identifier != expected_id:
        raise CollectionError("repository numeric identity mismatch")
    return identifier


def _workflow(reader, run_id, workflow_id, expected_head_sha):
    run = reader.workflow_run(run_id)
    if integer(run.get("id"), 1) != run_id or integer(run.get("workflow_id"), 1) != workflow_id:
        raise CollectionError("workflow run identity mismatch")
    repo_id = _repo_metadata(run.get("repository"), reader.repository)
    _repo_metadata(run.get("head_repository"), reader.repository, repo_id)
    if sha(run.get("head_sha")) != expected_head_sha:
        raise CollectionError("stale workflow head")
    if run.get("event") not in ("pull_request", "push"):
        raise CollectionError("unsupported workflow event")
    return WorkflowIdentity(run_id, workflow_id, integer(run.get("run_attempt"), 1),
                            repo_id, run["event"], expected_head_sha)


def _pr(value, reader, run):
    pr = object_value(value)
    number = integer(pr.get("number"), 1)
    if pr.get("state") not in ("open", "closed"):
        raise CollectionError("malformed PR state")
    base, head = object_value(pr.get("base")), object_value(pr.get("head"))
    _repo_metadata(base.get("repo"), reader.repository, run.repository_id)
    _repo_metadata(head.get("repo"), reader.repository, run.repository_id)
    return number, pr["state"], sha(base.get("sha")), sha(head.get("sha"))


def associate_pr(reader, run):
    """Use exact commit association; never artifact hints or the first candidate."""
    candidates = []
    seen = set()
    for raw in reader.associated_prs(run.head_sha):
        number, state, base, head = _pr(raw, reader, run)
        if number in seen:
            raise CollectionError("duplicate PR association")
        seen.add(number)
        if state == "open" and head == run.head_sha:
            candidates.append((number, state, base, head))
    if len(candidates) != 1:
        raise CollectionError("expected exactly one open PR at workflow head")
    result = reader.pull_request(candidates[0][0])
    if _pr(result, reader, run) != candidates[0]:
        raise CollectionError("PR association changed during collection")
    integer(result.get("changed_files"), 0, MAX_CHANGED_FILES)
    return result


@dataclass(frozen=True)
class ChangedFile:
    path: str
    previous_path: str | None
    status: str
    additions: int
    deletions: int
    changes: int
    blob_sha: str
    head_sha: str
    patch_state: str
    patch: str | None
    patch_bytes: int
    support: str
    trust: str = "untrusted-repository-data"


def _patch_state(patch, additions, deletions):
    """Check hunk lengths and totals. This is evidence completeness, not execution."""
    if patch is None:
        return "missing", None, 0
    if type(patch) is not str:
        raise CollectionError("malformed patch metadata")
    try:
        size = len(patch.encode("utf-8"))
    except UnicodeError:
        raise CollectionError("invalid patch encoding") from None
    if size > MAX_PATCH_BYTES:
        return "oversized", None, size
    if any(ord(char) < 32 and char not in "\n\r\t" for char in patch):
        return "binary-or-unsupported", None, size
    added = removed = 0
    remaining_old = remaining_new = 0
    hunks = 0
    valid = True
    for line in patch.splitlines():
        match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", line)
        if match:
            if remaining_old or remaining_new:
                valid = False
            if any(len(group) > 9 for group in match.groups() if group is not None):
                return "insufficient", patch, size
            remaining_old = int(match[2]) if match[2] is not None else 1
            remaining_new = int(match[4]) if match[4] is not None else 1
            hunks += 1
        elif line == "\\ No newline at end of file":
            if not hunks:
                valid = False
        elif hunks and line.startswith("+"):
            remaining_new -= 1
            added += 1
        elif hunks and line.startswith("-"):
            remaining_old -= 1
            removed += 1
        elif hunks and line.startswith(" "):
            remaining_old -= 1
            remaining_new -= 1
        else:
            valid = False
        if remaining_old < 0 or remaining_new < 0:
            valid = False
    complete = (valid and hunks and not remaining_old and not remaining_new
                and added == additions and removed == deletions)
    return ("complete" if complete else "insufficient"), patch, size


def _changed(raw, head_sha):
    obj = object_value(raw)
    path = _path(obj.get("filename"))
    status = obj.get("status")
    if status not in ("added", "modified", "removed", "renamed"):
        raise CollectionError("unsupported changed-file status")
    previous = _path(obj.get("previous_filename")) if status == "renamed" else None
    if previous == path or (status != "renamed" and obj.get("previous_filename") is not None):
        raise CollectionError("inconsistent rename metadata")
    additions, deletions, changes = (integer(obj.get(key), 0, 10**9)
                                   for key in ("additions", "deletions", "changes"))
    if changes != additions + deletions:
        raise CollectionError("inconsistent changed-line counts")
    state, patch, size = _patch_state(obj.get("patch"), additions, deletions)
    support = "supported" if _supported(path) else "unsupported-path"
    if _sensitive(path) or (previous is not None and _sensitive(previous)):
        support = "sensitive-path"
    if support != "supported":
        state, patch = support, None
    return ChangedFile(path, previous, status, additions, deletions, changes,
                       sha(obj.get("sha")), head_sha, state, patch, size, support)


def _files(values, head_sha):
    if type(values) is not list or len(values) > MAX_CHANGED_FILES:
        raise CollectionError("changed-file count exceeds limit")
    result = tuple(sorted((_changed(value, head_sha) for value in values), key=lambda f: f.path))
    if len({file.path for file in result}) != len(result):
        raise CollectionError("duplicate changed-file path")
    return result


def _file_key(file):
    return (file.path, file.previous_path, file.status, file.additions,
            file.deletions, file.changes, file.blob_sha)


def _compare(reader, base_sha, head_sha):
    obj = reader.compare(base_sha, head_sha)
    if sha(object_value(obj.get("base_commit")).get("sha")) != base_sha:
        raise CollectionError("compare base identity mismatch")
    merge_base = sha(object_value(obj.get("merge_base_commit")).get("sha"))
    total = integer(obj.get("total_commits"), 0, MAX_COMPARE_COMMITS)
    commits = obj.get("commits")
    if type(commits) is not list or len(commits) != total:
        raise CollectionError("incomplete compare commit evidence")
    commit_shas = [sha(object_value(commit).get("sha")) for commit in commits]
    if len(set(commit_shas)) != total or (total and commit_shas[-1] != head_sha):
        raise CollectionError("compare head identity mismatch")
    if not total and head_sha != merge_base:
        raise CollectionError("empty compare head mismatch")
    status = obj.get("status")
    expected_status = ("identical" if base_sha == head_sha else "behind"
                       if merge_base == head_sha else "ahead" if merge_base == base_sha else "diverged")
    if status != expected_status:
        raise CollectionError("malformed compare relationship")
    files = _files(obj.get("files"), head_sha)
    if not total and files:
        raise CollectionError("empty comparison contains changed files")
    return merge_base, files


@dataclass(frozen=True)
class TreeFile:
    path: str
    blob_sha: str
    size: int
    mode: str
    kind: str


def _inventory(reader, commit_sha):
    result = {}
    for raw in reader.tree_at(commit_sha):
        obj = object_value(raw)
        path = _path(obj.get("path"))
        if path in result:
            raise CollectionError("duplicate tree path")
        kind, mode = obj.get("type"), obj.get("mode")
        if (kind, mode) not in {("tree", "040000"), ("blob", "100644"),
                                ("blob", "100755"), ("blob", "120000"), ("commit", "160000")}:
            raise CollectionError("malformed tree mode")
        result[path] = TreeFile(path, sha(obj.get("sha")),
                                integer(obj.get("size")) if kind == "blob" else 0, mode, kind)
    # Do not traverse file/symlink/submodule ancestors, even in a malformed inventory.
    for path in result:
        for parent in PurePosixPath(path).parents:
            if str(parent) in result and result[str(parent)].kind != "tree":
                raise CollectionError("unsafe tree ancestor")
    return result


@dataclass(frozen=True)
class Evidence:
    path: str
    source_sha: str
    blob_sha: str | None
    category: str
    reasons: tuple[str, ...]
    state: str
    observed_bytes: int
    content: str | None
    trust: str = "untrusted-repository-data"


def _evidence(reader, tree, path, commit_sha, category, reasons, maximum, cache):
    entry = tree.get(path)
    if entry is None:
        return Evidence(path, commit_sha, None, category, tuple(sorted(reasons)), "missing", 0, None)
    state, content = "available", None
    if _sensitive(path):
        state = "sensitive-path"
    elif entry.kind != "blob" or entry.mode != "100644":
        state = "unsupported-mode"
    elif entry.size > maximum:
        state = "oversized"
    else:
        key = entry.blob_sha
        if key not in cache:
            cache[key] = reader.blob(key, entry.size, maximum)
        data = cache[key]
        if len(data) != entry.size:
            raise CollectionError("cached blob size mismatch")
        try:
            content = data.decode("utf-8")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in content):
                state, content = "binary-or-unsupported", None
        except UnicodeError:
            state = "binary-or-unsupported"
    return Evidence(path, commit_sha, entry.blob_sha, category, tuple(sorted(reasons)),
                    state, entry.size, content)


def _named_test(path):
    if _test(path):
        return path
    part = PurePosixPath(path)
    if path.startswith("frontend/src/"):
        return str(part.with_suffix(".test" + part.suffix))
    return "backend/tests/" + str(part.relative_to("backend/app").with_name("test_" + part.name))


def _test_reasons(test_path, changed, text):
    reasons = set()
    for file in changed:
        for source_path in (file.path, file.previous_path):
            if source_path is None or not (_source(source_path) or _test(source_path)):
                continue
            if source_path == test_path:
                reasons.add("changed-existing-test")
            if _named_test(source_path) == test_path:
                reasons.add("named-module-test:" + source_path)
            if source_path.startswith("frontend/src/") and test_path.startswith("frontend/src/"):
                source_parts = source_path.split("/")
                feature = "/".join(source_parts[:4]) + "/"
                if len(source_parts) > 4 and source_parts[2] == "features" and test_path.startswith(feature):
                    reasons.add("same-feature:" + source_path)
                elif PurePosixPath(test_path).parent == PurePosixPath(source_path).parent:
                    reasons.add("same-directory:" + source_path)
                if text is not None:
                    # Static import tokens are matched, never resolved/fetched/executed.
                    module = PurePosixPath(source_path).with_suffix("")
                    for token in re.findall(r"['\"]([A-Za-z0-9_./@-]{1,240})['\"]", text):
                        if token.startswith("."):
                            parts = list(PurePosixPath(test_path).parent.parts)
                            for component in token.split("/"):
                                if component == "..":
                                    if parts:
                                        parts.pop()
                                elif component not in ("", "."):
                                    parts.append(component)
                            target = "/".join(parts)
                        elif token.startswith("@/"):
                            target = "frontend/src/" + token[2:]
                        else:
                            continue
                        if target in (str(module), source_path):
                            reasons.add("module-reference:" + source_path)
            if source_path.startswith("backend/app/") and test_path.startswith("backend/tests/"):
                relative = str(PurePosixPath(source_path).relative_to("backend/app"))
                directory = str(PurePosixPath(relative).parent)
                if directory != "." and test_path.startswith("backend/tests/" + directory + "/"):
                    reasons.add("same-module-directory:" + source_path)
                module = "app." + str(PurePosixPath(relative).with_suffix("")).replace("/", ".")
                if text is not None and re.search(r"(?<![\w.])" + re.escape(module) + r"(?![\w.])", text):
                    reasons.add("module-reference:" + source_path)
    return tuple(sorted(reasons))


@dataclass(frozen=True)
class CandidateTest:
    path: str
    reasons: tuple[str, ...]
    exists: bool


@dataclass(frozen=True)
class ContextBundle:
    source: SourceIdentity
    workflow: WorkflowIdentity
    changed_files: tuple[ChangedFile, ...]
    sources: tuple[Evidence, ...]
    existing_tests: tuple[Evidence, ...]
    fixtures: tuple[Evidence, ...]
    configuration: tuple[Evidence, ...]
    candidate_tests: tuple[CandidateTest, ...]
    markers: tuple[str, ...]
    limits: tuple[tuple[str, int], ...]
    usage: tuple[tuple[str, int], ...]
    schema_version: int = 1

    def to_bytes(self):
        value = asdict(self)
        value["limits"] = dict(self.limits)
        value["usage"] = dict(self.usage)
        data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > MAX_TOTAL_CONTEXT_BYTES:
            raise CollectionError("canonical context budget exhausted")
        return data

    @property
    def sha256(self):
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def trusted_context(self, repository_root: Path):
        """Host-only bridge. T02 still validates paths/root and every later proposal.

        The caller must independently prepare an exact-head checkout before using
        T02's filesystem checks; this method neither creates nor verifies a checkout.
        Never reconstruct a trusted bundle from a model or PR artifact.
        """
        return TrustedContext(repository_root, self.source,
                              frozenset(file.path for file in self.changed_files),
                              frozenset(test.path for test in self.candidate_tests))


def collect_context(reader: GitHubReader, *, run_id: int, workflow_id: int,
                    expected_head_sha: str, policy_sha: str) -> ContextBundle:
    """Collect a portable exact-SHA snapshot from trusted caller inputs and API reads."""
    integer(run_id, 1)
    integer(workflow_id, 1)
    sha(expected_head_sha)
    sha(policy_sha)  # Only caller input; PR/workflow text cannot supply policy code identity.
    run = _workflow(reader, run_id, workflow_id, expected_head_sha)
    pr = associate_pr(reader, run)
    number, _, base, head = _pr(pr, reader, run)
    merge_base, initial_files = _compare(reader, base, head)
    exact_merge_base, changed = _compare(reader, merge_base, head)
    if exact_merge_base != merge_base:
        raise CollectionError("merge-base identity mismatch")
    pr_files = _files(reader.changed_files(number), head)
    expected_keys = tuple(map(_file_key, changed))
    if (tuple(map(_file_key, initial_files)) != expected_keys
            or tuple(map(_file_key, pr_files)) != expected_keys
            or len(changed) != pr["changed_files"]):
        raise CollectionError("PR and exact merge-base diff disagree")
    source = SourceIdentity(reader.repository, number, base, merge_base, head, policy_sha, str(run_id))
    tree = _inventory(reader, head)
    historical = _inventory(reader, merge_base) if any(
        file.status in ("removed", "renamed") and file.support == "supported" for file in changed) else {}
    cache = {}
    sources = []
    for file in changed:
        if file.support != "supported":
            continue
        maximum = (MAX_TEST_BYTES if _test(file.path) else MAX_CONFIG_FIXTURE_BYTES
                   if file.path in CONFIG_PATHS + FIXTURE_PATHS else MAX_SOURCE_BYTES)
        if file.status == "removed":
            if file.path in tree:
                raise CollectionError("deleted path present at head")
        else:
            if file.path not in tree or tree[file.path].blob_sha != file.blob_sha:
                raise CollectionError("changed-file head blob mismatch")
            sources.append(_evidence(reader, tree, file.path, head, "source",
                                     ("exact-changed-file",), maximum, cache))
        if file.status in ("removed", "renamed"):
            old_path = file.previous_path if file.status == "renamed" else file.path
            if old_path not in historical:
                raise CollectionError("missing historical changed file")
            if file.status == "removed" and historical[old_path].blob_sha != file.blob_sha:
                raise CollectionError("deleted-file historical blob mismatch")
            sources.append(_evidence(reader, historical, old_path, merge_base, "historical-source",
                                     ("rename-predecessor" if file.status == "renamed"
                                      else "deleted-previous-content",), maximum, cache))
    markers = set()
    tests = []
    test_paths = sorted(path for path in tree if _test(path)) if any(
        file.support == "supported" and (_source(file.path) or _test(file.path)
                                         or (file.previous_path and _source(file.previous_path)))
        for file in changed) else []
    if len(test_paths) > MAX_TEST_SCAN_FILES:
        raise CollectionError("test inventory scan budget exhausted")
    scan_bytes = 0
    for path in test_paths:
        evidence = _evidence(reader, tree, path, head, "existing-test", (), MAX_TEST_BYTES, cache)
        scan_bytes += len(evidence.content.encode("utf-8")) if evidence.content is not None else 0
        if scan_bytes > MAX_TEST_SCAN_BYTES:
            raise CollectionError("test content scan budget exhausted")
        reasons = _test_reasons(path, changed, evidence.content)
        if evidence.state != "available":
            markers.add("test-reference-scan-partial")
        if reasons:
            tests.append(replace(evidence, reasons=reasons))
    # Named/reference evidence precedes broader directory relationships, then path.
    tests.sort(key=lambda item: (not any(reason.startswith(("named-module-test", "module-reference",
                                                           "changed-existing-test"))
                                       for reason in item.reasons), item.path))
    omitted_tests = max(0, len(tests) - MAX_NEARBY_TEST_FILES)
    if omitted_tests:
        markers.add("nearby-test-count-limit")
    tests = sorted(tests[:MAX_NEARBY_TEST_FILES], key=lambda item: item.path)
    candidates = {test.path: CandidateTest(test.path, test.reasons, True)
                  for test in tests if test.state == "available"}
    available_sources = {item.path for item in sources if item.source_sha == head and item.state == "available"}
    for file in changed:
        if file.path in available_sources and _source(file.path) and file.status != "removed":
            path = _path(_named_test(file.path))
            if path not in tree:
                candidates[path] = CandidateTest(path, ("named-test-candidate:" + file.path,), False)
    if len(candidates) > MAX_PERMITTED_TEST_PATHS:
        raise CollectionError("permitted test path budget exhausted")
    frontend = any(file.support == "supported" and file.path.startswith("frontend/") for file in changed)
    backend = any(file.support == "supported" and file.path.startswith("backend/") for file in changed)
    config_paths = [path for path in CONFIG_PATHS if path in tree and (
        (frontend and path.startswith("frontend/")) or (backend and (
            path.startswith("backend/") or path == "pytest.ini")))]
    configuration = [_evidence(reader, tree, path, head, "configuration",
                               ("known-test-configuration",), MAX_CONFIG_FIXTURE_BYTES, cache)
                     for path in config_paths]
    fixture_paths = []
    selected_text = "\n".join(item.content or "" for item in tests + configuration)
    for path, reference in zip(FIXTURE_PATHS, ("todoFixture", "tests.mocks")):
        if path in tree and reference in selected_text:
            fixture_paths.append(path)
    if len(fixture_paths) + len(configuration) > MAX_CONFIG_FIXTURE_FILES:
        raise CollectionError("fixture/config file budget exhausted")
    fixtures = [_evidence(reader, tree, path, head, "fixture", ("known-fixture-reference",),
                          MAX_CONFIG_FIXTURE_BYTES, cache) for path in fixture_paths]
    # A binary blob cannot be presented as a textual diff even if an API supplies one.
    unsupported_paths = {item.path: item.state for item in sources
                         if item.state in ("binary-or-unsupported", "unsupported-mode")}
    changed = tuple(replace(file, patch=None, patch_state=unsupported_paths.get(
        file.path, unsupported_paths.get(file.previous_path)))
        if file.path in unsupported_paths or file.previous_path in unsupported_paths else file for file in changed)
    categories = {"source_bytes": sources, "test_bytes": tests,
                  "fixture_bytes": fixtures, "configuration_bytes": configuration}
    usage = {key: sum(len(item.content.encode("utf-8")) if item.content is not None else 0
                      for item in values) for key, values in categories.items()}
    usage["patch_bytes"] = sum(len(file.patch.encode("utf-8")) if file.patch is not None else 0
                               for file in changed)
    usage["content_bytes"] = sum(usage.values())
    if usage["content_bytes"] > MAX_TOTAL_CONTEXT_BYTES:
        raise CollectionError("context content budget exhausted")
    usage.update(changed_files=len(changed), source_files=len(sources), existing_test_files=len(tests),
                 omitted_test_files=omitted_tests, fixture_config_files=len(fixtures) + len(configuration),
                 permitted_test_paths=len(candidates), test_scan_files=len(test_paths), test_scan_bytes=scan_bytes)
    # Bracket mutable PR metadata reads; pinned Git objects remain immutable.
    final_pr = associate_pr(reader, run)
    if (_pr(final_pr, reader, run) != _pr(pr, reader, run)
            or integer(final_pr.get("changed_files"), 0, MAX_CHANGED_FILES) != pr["changed_files"]
            or _workflow(reader, run_id, workflow_id, head) != run):
        raise CollectionError("source identity changed during collection")
    usage.update(github_requests=reader.requests, github_response_bytes=reader.response_bytes)
    limits = {
        "changed_files": MAX_CHANGED_FILES, "patch_bytes_per_file": MAX_PATCH_BYTES,
        "source_bytes_per_file": MAX_SOURCE_BYTES, "test_bytes_per_file": MAX_TEST_BYTES,
        "fixture_config_bytes_per_file": MAX_CONFIG_FIXTURE_BYTES,
        "total_context_bytes": MAX_TOTAL_CONTEXT_BYTES, "nearby_test_files": MAX_NEARBY_TEST_FILES,
        "fixture_config_files": MAX_CONFIG_FIXTURE_FILES, "test_scan_files": MAX_TEST_SCAN_FILES,
        "test_scan_bytes": MAX_TEST_SCAN_BYTES, "permitted_test_paths": MAX_PERMITTED_TEST_PATHS,
        "github_pages": MAX_PAGES, "github_requests": MAX_REQUESTS,
        "github_response_bytes": MAX_RESPONSE_BYTES, "github_total_response_bytes": MAX_RESPONSE_TOTAL_BYTES,
        "tree_entries": MAX_TREE_ENTRIES, "compare_commits": MAX_COMPARE_COMMITS,
    }
    bundle = ContextBundle(source, run, changed, tuple(sorted(sources, key=lambda item: (item.path, item.source_sha))),
                           tuple(tests), tuple(sorted(fixtures, key=lambda item: item.path)),
                           tuple(sorted(configuration, key=lambda item: item.path)),
                           tuple(candidates[path] for path in sorted(candidates)), tuple(sorted(markers)),
                           tuple(sorted(limits.items())), tuple(sorted(usage.items())))
    bundle.to_bytes()  # Enforce complete serialized-size bound before returning evidence.
    return bundle
