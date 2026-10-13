"""Opt-in actual Docker smoke. All synthetic executable tests stay in tmpfs.

Run: T05_DOCKER_SMOKE=1 backend/.venv/bin/python -m pytest
     automation/tests/test_verification_docker.py -q
"""

from dataclasses import asdict
import os
from pathlib import Path
import tempfile

import pytest

from automation.test_agent import verification as v
from automation.test_agent.contracts import FinalHostStatus, SourceIdentity
from automation.test_agent.patches import prepare_changes
from automation.test_agent.policy import TrustedContext


pytestmark = pytest.mark.skipif(os.environ.get("T05_DOCKER_SMOKE") != "1", reason="explicit Docker smoke only")
FRONTEND_PATH = "frontend/src/t05_smoke.test.ts"
BACKEND_PATH = "backend/tests/test_t05_smoke.py"
FRONTEND_GOOD = "import {it,expect} from 'vitest'\nit('dummy smoke', () => expect(1).toBe(1))\n"
BACKEND_GOOD = "def test_dummy_smoke():\n    assert 1 == 1\n"


class RecordingExecutor(v.Executor):
    def __init__(self):
        self.operations = []

    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        self.operations.append({"argv": argv, "exit_code": result.exit_code, "timed_out": result.timed_out,
                                "stdout_bytes": result.stdout_bytes, "stderr_bytes": result.stderr_bytes,
                                "stdout_sha256": result.stdout_sha256, "stderr_sha256": result.stderr_sha256})
        return result


class Smoke:
    def __init__(self):
        self.executor = RecordingExecutor()
        self.root = v.TRUSTED_ROOT
        def git(*args):
            return v.checked(self.executor, (v.GIT, "-C", str(self.root), *args)).strip().decode()
        # The trusted caller captures HEAD; production verification rechecks it.
        head = git("rev-parse", "HEAD")
        base = git("rev-parse", "main")
        merge = git("merge-base", base, head)
        self.source = SourceIdentity("kreshby/cloudfide-test-agent", None, base, merge, head, head, "t05-smoke")
        self.changed = frozenset(p.decode() for p in v.checked(self.executor, (
            v.GIT, "-C", str(self.root), "diff", "--no-renames", "--name-only", "-z", merge, head)).split(b"\0") if p)
        self.image = v.build_image(self.executor)
        self.env = v.validate_image(self.executor, self.image)
        v.validate_daemon(self.executor)
        self.results = []

    def changes(self, content_by_path):
        decisions, edits = [], []
        for target in ("frontend", "backend"):
            paths = [p for p in content_by_path if p.startswith(target + "/")]
            if not paths:
                continue
            decisions.append({"target": target, "action": "generate", "plan": {
                "changed_behavior": "dummy sandbox probe", "existing_test_evidence": [],
                "rationale": "deterministic isolation evidence", "intended_test_files": paths, "skill_ids": []}})
            edits.extend({"target": target, "path": path, "operation": "create", "original_sha256": None,
                          "content": content_by_path[path]} for path in paths)
        return prepare_changes({"schema_version": 1, "source": asdict(self.source), "decisions": decisions, "edits": edits},
                               TrustedContext(self.root, self.source, self.changed, frozenset(content_by_path)))

    def verify(self, name, contents):
        report = v.Verifier(executor=self.executor).verify(self.source, self.root, self.changes(contents), image=self.image)
        self.results.append({"name": name, "report": v.parse_json(report.to_bytes(), v.MAX_SOURCE_BYTES)})
        return report

    def targeted(self, name, contents):
        changes = self.changes(contents)
        files = v.snapshot(self.executor, self.root, self.source, changes)
        path = next(iter(contents))
        stack = path.split("/", 1)[0]
        with tempfile.TemporaryDirectory(prefix="t05-probe-") as temp:
            roots = v.prepare_states(Path(temp), files, changes)
            result, reports, error = v.Sandbox(self.executor, self.image, self.env).execute(
                stack + "-patched-targeted", roots[v.ExecutionKind.PATCHED], (path,))
        counts = None
        if not error and not result.timed_out:
            counts = (v.frontend_counts(reports["tests.json"], (path,)) if stack == "frontend" else
                      v.backend_counts(reports["tests.xml"], reports["collection.json"], (path,)))
        evidence = {"name": name, "source_sha": self.source.head_sha, "changes_sha256": v.digest(changes.to_bytes()),
                    "exit_code": result.exit_code, "timed_out": result.timed_out, "error": error,
                    "counts": asdict(counts) if counts else None,
                    "reports": {p: {"bytes": len(d), "sha256": v.digest(d)} for p, d in reports.items()}}
        self.results.append(evidence)
        return result, counts, error

    def save(self):
        image = asdict(self.image)
        image.pop("build_output")
        Path("/mnt/data/T05-docker-evidence.json").write_bytes(v.canonical({
            "source": asdict(self.source), "image": image, "results": self.results,
            "operations": self.executor.operations, "real_credentials_used": False, "model_inference": False}))


@pytest.fixture(scope="module")
def smoke():
    instance = Smoke()
    yield instance
    instance.save()


def test_actual_baseline_typecheck_suites_and_good_generated_tests(smoke):
    report = smoke.verify("good-both-stacks", {FRONTEND_PATH: FRONTEND_GOOD, BACKEND_PATH: BACKEND_GOOD})
    assert report.host_result.status == FinalHostStatus.VERIFIED
    assert len(report.host_result.verifier_results) == 6
    assert all(r.collected_tests > 0 for r in report.host_result.verifier_results)


@pytest.mark.parametrize("path,content", [(FRONTEND_PATH, FRONTEND_GOOD.replace("toBe(1)", "toBe(2)")),
                                          (BACKEND_PATH, BACKEND_GOOD.replace("1 == 1", "1 == 2"))])
def test_actual_broken_generated_tests_cannot_verify(smoke, path, content):
    report = smoke.verify("broken-" + path.split("/", 1)[0], {path: content})
    assert report.host_result.status == FinalHostStatus.VERIFICATION_FAILED
    assert report.commands[-1].exit_code != 0
    assert report.commands[-1].counts.failed >= 1


@pytest.mark.parametrize("path,content", [
    (FRONTEND_PATH, "import {it,expect} from 'vitest'\nconst inert = \"it('dummy', () => expect(1).toBe(1))\"\n"),
    (BACKEND_PATH, "inert = '''\ndef test_dummy():\n    assert 1 == 1\n'''\n"),
])
def test_actual_zero_collection_cannot_verify(smoke, path, content):
    report = smoke.verify("zero-" + path.split("/", 1)[0], {path: content})
    assert report.host_result.status == FinalHostStatus.VERIFICATION_FAILED
    assert not report.commands[-1].passed
    assert report.commands[-1].counts.collected == 0


def test_actual_security_isolation_dummy_values_only(smoke, monkeypatch, tmp_path):
    outside = tmp_path / "host-only-dummy.txt"
    outside.write_text("DUMMY ONLY")
    monkeypatch.setenv("T05_DUMMY_HOST_ONLY", "DUMMY ONLY")
    content = f'''import os
import socket
from pathlib import Path

def test_security_boundary():
    assert os.getuid() != 0
    assert "T05_DUMMY_HOST_ONLY" not in os.environ
    assert not Path({str(outside)!r}).exists()
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/input/.git").exists()
    assert not Path("/work/repo/.git").exists()
    assert not Path("/input/automation").exists()
    assert not Path("/input/.github").exists()
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    except OSError:
        pass
    else:
        assert False, "external network unexpectedly reachable"
    try:
        Path("/t05-root-write-probe").write_text("DUMMY ONLY")
    except OSError:
        pass
    else:
        assert False, "root filesystem unexpectedly writable"
'''
    result, counts, error = smoke.targeted("security-dummy-only", {BACKEND_PATH: content})
    assert result.exit_code == 0 and not error and counts.passed == counts.collected == 1


def test_actual_timeout_removes_container(smoke):
    content = "import time\ndef test_timeout():\n    assert 1 == 1\n    time.sleep(180)\n"
    result, counts, error = smoke.targeted("timeout-removal", {BACKEND_PATH: content})
    assert result.timed_out and counts is None
    removals = [o for o in smoke.executor.operations if o["argv"][:4] == (*v.DOCKER, "rm")]
    assert removals[-1]["exit_code"] == 0
    assert smoke.executor.operations[-1]["argv"][3:5] == ("container", "ls")
    assert smoke.executor.operations[-1]["exit_code"] == 0
    assert smoke.executor.operations[-1]["stdout_bytes"] == 0
