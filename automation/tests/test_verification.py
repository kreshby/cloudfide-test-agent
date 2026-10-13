"""Deterministic host orchestration tests; generated code is always inert data."""

from dataclasses import asdict, replace
import base64
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from automation.test_agent import verification as v
from automation.test_agent.contracts import EditOperation, FinalHostStatus, SourceIdentity, Target
from automation.test_agent.patches import prepare_changes
from automation.test_agent.policy import TrustedContext
from automation.verifier.profiles import PROFILES, command


FRONTEND = "frontend/src/value.test.ts"
BACKEND = "backend/tests/test_value.py"
NEW_FRONTEND = "frontend/src/new.test.ts"
NEW_BACKEND = "backend/tests/test_new.py"


def capture(data=b"", exit_code=0, **kwargs):
    return v.Capture(exit_code, data, stdout_bytes=len(data), stdout_sha256=v.digest(data), **kwargs)


def image_identity():
    manifests = tuple(sorted((p, v.digest(data)) for p, data in v.build_entries().items()))
    return v.ImageIdentity("sha256:" + "f" * 64, (), v.BASE_IMAGES, dict(manifests)["Dockerfile"],
                           manifests, v.digest(v.canonical(manifests)), 0, capture())


def frontend_report(paths, *, failed=False, zero=False):
    n = 0 if zero else len(paths)
    files = [{"name": "/work/repo/" + path, "status": "failed" if failed else "passed",
              "assertionResults": [] if zero else [{"fullName": "case", "status": "failed" if failed else "passed"}]}
             for path in paths]
    return v.canonical({"numTotalTestSuites": len(files), "numPassedTestSuites": 0 if failed else len(files),
                        "numFailedTestSuites": len(files) if failed else 0, "numPendingTestSuites": 0,
                        "numTotalTests": n, "numPassedTests": 0 if failed else n,
                        "numFailedTests": n if failed else 0, "numPendingTests": 0, "numTodoTests": 0,
                        "success": not failed, "testResults": files})


def backend_reports(paths, *, failed=False, zero=False):
    n = 0 if zero else len(paths)
    cases = "" if zero else "".join(f'<testcase classname="tests.case{i}" name="test_value">'
                                    + ('<failure message="dummy" />' if failed else '') + '</testcase>'
                                    for i, _ in enumerate(paths))
    xml = f'<testsuites><testsuite tests="{n}" failures="{n if failed else 0}" errors="0" skipped="0">{cases}</testsuite></testsuites>'
    collected = v.canonical({"collected": n, "files": {p.split("/", 1)[1]: 0 if zero else 1 for p in paths}})
    return xml.encode(), collected


class FakeExecutor:
    """Finite Docker/Git behavior; never calls a subprocess or imports test code."""

    def __init__(self, root, source, files, changed=("frontend/src/value.ts", "backend/app/value.py")):
        self.root, self.source, self.files, self.changed = root, source, files, changed
        self.calls, self.started, self.removed = [], [], []
        self.image = image_identity()
        self.containers = {}
        self.current = None
        self.fail_step = self.zero_step = self.timeout_step = self.bad_report_step = None
        self.fault = None
        self.mutate_inspect = lambda data: None
        self.image_env = ["PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.12.13"]
        self.dirty, self.untracked = (), ()

    def image_data(self):
        return {"Id": self.image.image_id, "RepoDigests": [], "Config": {
            "User": "10001:10001", "Env": self.image_env,
            "Entrypoint": ["/usr/local/bin/python3.12", "-I", "/opt/verifier/runner.py"],
            "Cmd": ["idle"], "Labels": {"t05.build": self.image.build_digest}}}

    def run(self, argv, *, timeout=v.CLIENT_SECONDS, limit=v.MAX_OUTPUT_BYTES):
        assert type(argv) is tuple
        self.calls.append((argv, timeout, limit))
        if argv[0] == v.GIT:
            args = argv[3:]
            if args == ("rev-parse", "HEAD"):
                return capture((self.source.head_sha + "\n").encode())
            if args == ("rev-parse", "--show-toplevel"):
                return capture((str(self.root) + "\n").encode())
            if args[0] == "config":
                return capture(("https://github.com/" + self.source.repository + ".git\n").encode())
            if args[0] == "merge-base":
                return capture((self.source.merge_base_sha + "\n").encode())
            if args[0] == "diff":
                values = self.dirty if args[-1] == "HEAD" else self.changed
                return capture(b"".join(p.encode() + b"\0" for p in values))
            if args[0] == "ls-files":
                return capture(b"".join(p.encode() + b"\0" for p in self.untracked))
            if args[0] == "ls-tree":
                entries = []
                for path, (data, _) in self.files.items():
                    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                    entries.append(f"100644 blob {blob}\t{path}".encode() + b"\0")
                return capture(b"".join(entries))
            raise AssertionError("unexpected Git operation")
        assert argv[:3] == v.DOCKER
        operation = argv[3]
        if self.fault == operation:
            raise v.Unavailable("dummy setup failure")
        if operation == "build":
            context = Path(argv[-1])
            assert sorted(p.name for p in context.iterdir()) == sorted(v.build_entries())
            assert not any(p.is_symlink() for p in context.iterdir())
            assert not (context / "src").exists()
            return capture()
        if operation == "image":
            return capture(v.canonical([self.image_data()]))
        if operation == "info":
            return capture(v.canonical(["name=seccomp,profile=builtin"]))
        if operation == "container":
            return capture(b"" if not self.containers else b"dummy\n")
        if operation == "create":
            name = argv[argv.index("--name") + 1]
            container_id = f"{len(self.calls):064x}"
            root = Path(argv[argv.index("--mount") + 1].split("source=", 1)[1].split(",", 1)[0])
            self.containers[name] = {"id": container_id, "root": root, "paused": False}
            self.current = self.containers[name]
            if self.fault == "create-timeout":
                return capture(timed_out=True, exit_code=None)
            if self.fault == "create-id":
                return capture(b"--privileged\n")
            return capture((container_id + "\n").encode())
        if operation == "inspect":
            if v._CONTAINER_NAME.fullmatch(argv[4]):
                return capture(exit_code=1 if argv[4] not in self.containers else 0)
            c = self.current
            data = {"Id": c["id"], "Image": self.image.image_id, "Config": self.image_data()["Config"],
                    "State": {"Running": c["paused"], "Paused": c["paused"], "OOMKilled": False},
                    "HostConfig": {"NetworkMode": "none", "Privileged": False, "ReadonlyRootfs": True,
                                   "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                                   "Memory": v.MEMORY, "MemorySwap": v.MEMORY, "NanoCpus": v.NANO_CPUS,
                                   "PidsLimit": v.PIDS, "IpcMode": "none", "Tmpfs": dict(v.TMPFS),
                                   "LogConfig": {"Type": "none"}, "RestartPolicy": {"Name": "no"},
                                   "Ulimits": [{"Name": "nofile", "Hard": v.NOFILE, "Soft": v.NOFILE}]},
                    "Mounts": [{"Type": "bind", "Source": str(c["root"]), "Destination": "/input", "RW": False, "Propagation": "rprivate"}]}
            self.mutate_inspect(data)
            return capture(v.canonical([data]))
        if operation == "start":
            self.started.append(argv[4])
            return capture(argv[4].encode())
        if operation == "exec":
            if argv[8] == "export":
                if self.fault == "export":
                    raise v.Unavailable("dummy export failure")
                step, paths = self.current["step"], self.current["paths"]
                names = ["tests.json"] if PROFILES[step].stack == "frontend" else ["tests.xml", "collection.json"]
                if PROFILES[step].coverage:
                    names.append("coverage/coverage-summary.json" if PROFILES[step].stack == "frontend" else "coverage.json")
                reports = {}
                for name in names:
                    if self.bad_report_step == step:
                        data = b"{\"numTotalTests\":NaN}"
                    elif name == "tests.json":
                        data = frontend_report(paths, failed=step == self.fail_step, zero=step == self.zero_step)
                    elif name in ("tests.xml", "collection.json"):
                        xml, collection = backend_reports(paths, failed=step == self.fail_step, zero=step == self.zero_step)
                        data = xml if name == "tests.xml" else collection
                    else:
                        data = v.canonical({"total" if PROFILES[step].stack == "frontend" else "totals": {"percent_covered": 0}})
                    reports[name] = base64.b64encode(data).decode()
                return capture(v.canonical({"quiesced": True, "reports": reports}))
            self.current["step"] = argv[8]
            self.current["paths"] = tuple(PROFILES[argv[8]].stack + "/" + p for p in argv[9:])
            if self.timeout_step == argv[8]:
                return capture(exit_code=None, timed_out=True)
            return capture(b"::error::UNTRUSTED\x1b[31m", exit_code=1 if self.fail_step == argv[8] else 0)
        if operation == "pause":
            self.current["paused"] = True
            return capture()
        if operation == "rm":
            name = argv[-1]
            self.removed.append(name)
            self.containers.pop(name, None)
            return capture(name.encode())
        raise AssertionError("unexpected Docker operation")


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    files = {p: v.safe_read(v.TRUSTED_ROOT, p) for p in v.TRUSTED_FILES}
    files.update({"frontend/src/value.ts": (b"export const value = 1\n", 0o644),
                  "backend/app/value.py": (b"value = 1\n", 0o644),
                  FRONTEND: (b"import {it,expect} from 'vitest'\nit('old', () => expect(1).toBe(1))\n", 0o644),
                  BACKEND: (b"def test_old():\n    assert 1 == 1\n", 0o644)})
    for path, (data, mode) in files.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(mode)
    source = SourceIdentity("example/taskly", 1, "a" * 40, "a" * 40, "c" * 40, "d" * 40, "unit")
    executor = FakeExecutor(root, source, files)
    return root, source, files, executor


def accepted(fixture, *, update=False):
    root, source, _, executor = fixture
    paths = (FRONTEND, BACKEND) if update else (NEW_FRONTEND, NEW_BACKEND)
    context = TrustedContext(root, source, frozenset(executor.changed), frozenset(paths))
    edits, decisions = [], []
    for target, path in zip(Target, paths):
        content = ("import {it,expect} from 'vitest'\nit('new', () => expect(1).toBe(1))\n"
                   if target == Target.FRONTEND else "def test_new():\n    assert 1 == 1\n")
        if update:
            # Add a second baseline test to demonstrate guards retain other files.
            extra = "frontend/src/other.test.ts" if target == Target.FRONTEND else "backend/tests/test_other.py"
            executor.files[extra] = (content.encode(), 0o644)
            (root / extra).write_text(content)
        edits.append({"target": str(target), "path": path, "operation": "update" if update else "create",
                      "original_sha256": v.digest((root / path).read_bytes()) if update else None, "content": content})
        decisions.append({"target": str(target), "action": "update" if update else "generate", "plan": {
            "changed_behavior": "fixed dummy behavior", "existing_test_evidence": [], "rationale": "test boundary",
            "intended_test_files": [path], "skill_ids": []}})
    return prepare_changes({"schema_version": 1, "source": asdict(source), "decisions": decisions, "edits": edits}, context)


def verify(fixture, changes=None):
    root, source, _, executor = fixture
    return v.Verifier(executor=executor).verify(source, root, changes or accepted(fixture), image=executor.image)


def test_good_both_stacks_exact_argv_and_real_count_contract(fixture):
    root, source, files, executor = fixture
    before = {p: (root / p).read_bytes() for p in files}
    report = verify(fixture)
    assert report.host_result.status == FinalHostStatus.VERIFIED
    assert report.host_result.source == source
    assert len(report.host_result.verifier_results) == 6
    assert len(report.commands) == 8
    assert all(r.collected_tests >= 1 for r in report.host_result.verifier_results)
    assert all(c.counts is None for c in report.commands if c.command_id == "tsc")
    assert all(c.source_sha == source.head_sha and c.changes_sha256 == report.changes_sha256 for c in report.commands)
    assert {p: (root / p).read_bytes() for p in files} == before
    assert not (root / NEW_FRONTEND).exists()
    assert len(executor.started) == len(executor.removed) == 8 and not executor.containers
    for argv, timeout, limit in executor.calls:
        if argv[0] == v.GIT:
            continue
        if argv[3] == "create":
            input_root = Path(argv[argv.index("--mount") + 1].split("source=", 1)[1].split(",", 1)[0])
            name = argv[argv.index("--name") + 1]
            expected = (*v.DOCKER, "create", "--name", name, "--user", "10001:10001",
                        "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--read-only", "--memory", "1073741824", "--memory-swap", "1073741824",
                        "--cpus", "2", "--pids-limit", "128", "--ulimit", "nofile=1024:1024",
                        "--ipc", "none", "--log-driver", "none", "--mount",
                        f"type=bind,source={input_root},target=/input,readonly,bind-propagation=rprivate",
                        "--tmpfs", "/work:rw,nosuid,nodev,size=268435456,uid=10001,gid=10001,mode=0700",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=67108864,uid=10001,gid=10001,mode=0700",
                        "--tmpfs", "/reports:rw,nosuid,nodev,noexec,size=1048576,uid=10001,gid=10001,mode=0700",
                        executor.image.image_id)
            assert argv == expected
            assert str(root) not in argv and "--env" not in argv
        if argv[3] == "exec":
            if argv[8] == "export":
                assert timeout == 20 and limit == 2 * v.MAX_TRANSFER_BYTES
            else:
                assert timeout == 120 and limit == 65536
            assert argv[5:8] == ("/usr/local/bin/python3.12", "-I", "/opt/verifier/runner.py")
    portable = report.to_bytes()
    assert str(root).encode() not in portable and str(report.output_directory).encode() not in portable
    assert b"::error::" not in portable and b"\x1b" not in portable


@pytest.mark.parametrize("step,status", [
    ("frontend-baseline-typecheck", FinalHostStatus.BASELINE_FAILED),
    ("frontend-baseline-suite", FinalHostStatus.BASELINE_FAILED),
    ("backend-baseline-suite", FinalHostStatus.BASELINE_FAILED),
    ("frontend-patched-typecheck", FinalHostStatus.VERIFICATION_FAILED),
    ("frontend-patched-targeted", FinalHostStatus.VERIFICATION_FAILED),
    ("backend-patched-targeted", FinalHostStatus.VERIFICATION_FAILED),
    ("frontend-patched-suite", FinalHostStatus.VERIFICATION_FAILED),
    ("backend-patched-suite", FinalHostStatus.VERIFICATION_FAILED),
])
def test_guard_and_broken_test_classification(fixture, step, status):
    fixture[3].fail_step = step
    report = verify(fixture)
    assert report.host_result.status == status
    assert report.commands[-1].profile_id == step and report.commands[-1].exit_code == 1
    assert not report.commands[-1].passed and not fixture[3].containers


@pytest.mark.parametrize("stack", ["frontend", "backend"])
@pytest.mark.parametrize("mode", ["zero", "malformed", "timeout"])
def test_bad_targeted_evidence_cannot_verify(fixture, stack, mode):
    executor = fixture[3]
    setattr(executor, {"zero": "zero_step", "malformed": "bad_report_step", "timeout": "timeout_step"}[mode], stack + "-patched-targeted")
    report = verify(fixture)
    assert report.host_result.status == FinalHostStatus.VERIFICATION_FAILED
    assert not report.commands[-1].passed
    assert not executor.containers and len(executor.removed) == len(executor.started)


def test_update_excluded_only_from_baseline_guard(fixture):
    changes = accepted(fixture, update=True)
    report = verify(fixture, changes)
    assert report.host_result.status == FinalHostStatus.VERIFIED
    for evidence in report.commands:
        if evidence.counts:
            files = dict(evidence.counts.files)
            target = PROFILES[evidence.profile_id].stack
            changed = FRONTEND if target == "frontend" else BACKEND
            assert (changed in files) == (evidence.kind == v.ExecutionKind.PATCHED)
    inputs = [call[0] for call in fixture[3].calls if call[0][:4] == (*v.DOCKER, "exec")]
    assert all("other.test.ts" in a[-1] or "test_other.py" in a[-1] for a in inputs if "baseline-suite" in a[8])


@pytest.mark.parametrize("mutation", [
    {"NetworkMode": "bridge"}, {"Privileged": True}, {"ReadonlyRootfs": False}, {"CapAdd": ["SYS_ADMIN"]},
    {"SecurityOpt": ["seccomp=unconfined"]}, {"Memory": 0}, {"MemorySwap": -1}, {"NanoCpus": 0},
    {"PidsLimit": 0}, {"PidMode": "host"}, {"IpcMode": "host"}, {"Devices": ["dummy"]},
    {"Tmpfs": {}}, {"Ulimits": []}, {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
])
def test_effective_isolation_fails_closed_before_start(fixture, mutation):
    fixture[3].mutate_inspect = lambda data: data["HostConfig"].update(mutation)
    assert verify(fixture).host_result.status == FinalHostStatus.UNAVAILABLE
    assert not fixture[3].started and not fixture[3].containers


@pytest.mark.parametrize("field,value", [("User", "0"), ("Env", ["DUMMY_SECRET=value"]), ("Cmd", ["evil"])])
def test_effective_container_config_rejected(fixture, field, value):
    fixture[3].mutate_inspect = lambda data: data["Config"].update({field: value})
    assert verify(fixture).host_result.status == FinalHostStatus.UNAVAILABLE
    assert not fixture[3].started and not fixture[3].containers


@pytest.mark.parametrize("fault", ["create", "create-timeout", "create-id", "inspect", "start", "exec", "pause", "export"])
def test_cleanup_on_each_orchestration_failure(fixture, fault):
    fixture[3].fault = fault
    report = verify(fixture)
    assert report.host_result.status == FinalHostStatus.UNAVAILABLE
    assert fixture[3].removed and not fixture[3].containers


def test_mount_substitution_rejected(fixture):
    fixture[3].mutate_inspect = lambda data: data["Mounts"][0].update(Source="/var/run/docker.sock", RW=True)
    assert verify(fixture).host_result.status == FinalHostStatus.UNAVAILABLE
    assert not fixture[3].started


@pytest.mark.parametrize("path", list(v.TRUSTED_FILES) + ["backend/tests/nested/conftest.py", "frontend/vitest.config.js", "backend/setup.cfg"])
def test_unsupported_dependency_and_runner_changes_before_docker(fixture, path):
    fixture[3].changed = (path,)
    changes = accepted(fixture)
    report = verify(fixture, changes)
    assert report.host_result.status == FinalHostStatus.UNSUPPORTED
    assert not any(call[0][0] == v.DOCKER[0] for call in fixture[3].calls)


@pytest.mark.parametrize("case", ["head", "source", "dirty", "content", "symlink", "hardlink", "fifo", "mode", "create-present", "changed-files"])
def test_exact_source_and_file_state_fail_closed(fixture, case, tmp_path):
    root, source, _, executor = fixture
    changes = accepted(fixture)
    if case == "head":
        executor.source = replace(source, head_sha="b" * 40)
    elif case == "source":
        changes = replace(changes, source=replace(source, repository="wrong/repo"))
    elif case == "dirty":
        executor.dirty = ("backend/app/value.py",)
    elif case == "changed-files":
        changes = replace(changes, changed_files=())
    elif case == "create-present":
        (root / NEW_FRONTEND).write_text("dummy")
    else:
        path = root / FRONTEND
        if case == "content":
            path.write_bytes(b"different content")
        elif case == "mode":
            path.chmod(0o755)
        else:
            path.unlink()
            if case == "symlink":
                path.symlink_to(tmp_path / "outside")
            elif case == "hardlink":
                outside = tmp_path / "outside"
                outside.write_bytes(b"dummy")
                os.link(outside, path)
            else:
                os.mkfifo(path)
    assert verify(fixture, changes).host_result.status == FinalHostStatus.STALE
    assert not executor.started


def test_safe_temporary_writer_rechecks_content_mode_and_links(fixture, tmp_path):
    changes = accepted(fixture, update=True)
    root = tmp_path / "private"
    root.mkdir()
    edit = changes.edits[0]
    path = root / edit.path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"substituted")
    with pytest.raises(v.Stale):
        v.apply_edit(root, edit)
    assert path.read_bytes() == b"substituted"
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(fixture[2][edit.path][0])
    os.link(outside, path)
    with pytest.raises(v.Stale):
        v.apply_edit(root, edit)
    assert outside.read_bytes() == fixture[2][edit.path][0]
    create = accepted(fixture).edits[0]
    (root / create.path).write_bytes(b"existing")
    with pytest.raises(v.Stale):
        v.apply_edit(root, create)


def test_raw_proposal_profiles_paths_and_stale_image_rejected(fixture):
    root, source, _, executor = fixture
    verifier = v.Verifier(executor=executor)
    with pytest.raises(v.VerificationError):
        verifier.verify(source, root, {})
    changes = accepted(fixture)
    for profiles in (("model-command",), ("frontend-v1",), ("frontend-v1", "frontend-v1")):
        with pytest.raises(v.VerificationError):
            verifier.verify(source, root, changes, profile_ids=profiles)
    for path in ("frontend/src/../../escape.test.ts", "frontend/src/-x.test.ts", "--help"):
        with pytest.raises((v.VerificationError, v.PolicyError)):
            v._paths((path,), Target.FRONTEND)
    for executable in (("/bin/sh", "-c", "dummy"), (*v.DOCKER, "run", "dummy")):
        with pytest.raises(v.VerificationError):
            v.Executor().run(executable)
    report = verifier.verify(source, root, changes, image=replace(executor.image, build_digest="0" * 64))
    assert report.host_result.status == FinalHostStatus.UNAVAILABLE


def test_build_plane_only_trusted_inputs(fixture):
    image = v.build_image(fixture[3])
    assert image.base_images == v.BASE_IMAGES and image.image_id == fixture[3].image.image_id
    build = next(c[0] for c in fixture[3].calls if c[0][:4] == (*v.DOCKER, "build"))
    assert "--build-arg" not in build and "--secret" not in build
    assert "--no-cache" in build and "--pull" in build


@pytest.mark.parametrize("data", [b'{"a":1,"a":1}', b'{"a":NaN}', b'[]' * 65537, b'\xff'])
def test_json_report_bounds_duplicates_and_invalid_values(data):
    with pytest.raises(v.ReportError):
        v.parse_json(data)


@pytest.mark.parametrize("case", ["counts", "duplicate-file", "duplicate-case", "missing-file", "wrong-path", "boolean-count"])
def test_frontend_report_schema_and_inventory(case):
    report = v.parse_json(frontend_report((NEW_FRONTEND,)))
    if case == "counts":
        report["numTotalTests"] = 2
    elif case == "duplicate-file":
        report["testResults"] *= 2
    elif case == "duplicate-case":
        report["testResults"][0]["assertionResults"] *= 2
    elif case == "missing-file":
        report["testResults"] = []
    elif case == "wrong-path":
        report["testResults"][0]["name"] = "/host/private"
    else:
        report["numTotalTests"] = True
    with pytest.raises(v.ReportError):
        v.frontend_counts(v.canonical(report), (NEW_FRONTEND,))


@pytest.mark.parametrize("data", [b'<!DOCTYPE x [<!ENTITY e "dummy">]><x/>', b'<testsuites>',
                                  b'<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0" /></testsuites>'])
def test_backend_report_malformed_entities_and_impossible_counts(data):
    with pytest.raises(v.ReportError):
        v.backend_counts(data, b'{"collected":0,"files":{}}', (NEW_BACKEND,))


@pytest.mark.parametrize("case", ["not-quiesced", "oversized", "wrong-name", "malformed"])
def test_copied_report_limits_and_types(case):
    content = b"x" * (v.MAX_REPORT_BYTES + 1) if case == "oversized" else b"{}"
    data = b"invalid" if case == "malformed" else v.canonical({
        "quiesced": case != "not-quiesced", "reports": {
            "evil" if case == "wrong-name" else "tests.json": base64.b64encode(content).decode()}})
    with pytest.raises(v.ReportError):
        v.decode_reports(data, ["tests.json"])


def test_production_subprocess_no_shell_no_environment_inheritance_and_bounding(monkeypatch):
    real_popen = subprocess.Popen
    observed = {}
    monkeypatch.setenv("T05_DUMMY_HOST_ONLY", "dummy")
    def popen(argv, **kwargs):
        observed.update(kwargs)
        # A fixed, inert producer stands in for Docker, never proposal code.
        return real_popen(("/usr/bin/head", "-c", "100000", "/dev/zero"), **kwargs)
    monkeypatch.setattr(v.subprocess, "Popen", popen)
    result = v.Executor().run((*v.DOCKER, "inspect", "dummy"))
    assert observed["shell"] is False and observed["stdin"] == subprocess.DEVNULL
    assert "T05_DUMMY_HOST_ONLY" not in observed["env"]
    assert observed["env"]["DOCKER_CONFIG"].startswith("/tmp/t05-client-")
    assert result.stdout_bytes == 100000 and len(result.stdout) == v.MAX_OUTPUT_BYTES // 2
    assert result.stdout_sha256 == v.digest(b"\0" * 100000) and result.truncated


def test_coverage_and_direct_commands_preserve_trusted_settings():
    frontend = command("frontend-patched-suite", ("src/value.test.ts",))
    backend = command("backend-patched-suite", ("tests/test_value.py",))
    assert frontend[0] == "/usr/local/bin/node" and "npm" not in frontend
    assert "--coverage.reporter=json-summary" in frontend
    assert not any("include" in arg or "exclude" in arg for arg in frontend)
    assert backend[:4] == ("/usr/local/bin/python3.12", "-I", "-m", "pytest")
    assert "--cov-config=pyproject.toml" in backend and "--cov-branch" in backend


def test_smoke_binds_current_head_without_review_sha(monkeypatch):
    from automation.tests import test_verification_docker as smoke_module

    source_text = Path(smoke_module.__file__).read_text()
    assert "b9dd41a28f223f210e73a3071656a422584c75e3" not in source_text
    assert "EXPECTED_HEAD" not in source_text
    root, base, merge = v.TRUSTED_ROOT, "a" * 40, "b" * 40
    changed = frozenset({"frontend/src/value.ts", "backend/app/value.py"})
    image = image_identity()
    monkeypatch.setattr(v, "build_image", lambda executor: image)
    monkeypatch.setattr(v, "validate_image", lambda executor, built: ())
    monkeypatch.setattr(v, "validate_daemon", lambda executor: None)

    for head in ("e" * 40, "f" * 40):
        expected = (
            (v.GIT, "-C", str(root), "rev-parse", "HEAD"),
            (v.GIT, "-C", str(root), "rev-parse", "main"),
            (v.GIT, "-C", str(root), "merge-base", base, head),
            (v.GIT, "-C", str(root), "diff", "--no-renames", "--name-only", "-z", merge, head),
        )

        class GitOnlyExecutor:
            def __init__(self):
                self.calls = []

            def run(self, argv, **kwargs):
                assert argv == expected[len(self.calls)]
                self.calls.append(argv)
                replies = ((head + "\n").encode(), (base + "\n").encode(),
                           (merge + "\n").encode(), b"\0".join(p.encode() for p in sorted(changed)) + b"\0")
                return capture(replies[len(self.calls) - 1])

        executor = GitOnlyExecutor()
        monkeypatch.setattr(smoke_module, "RecordingExecutor", lambda: executor)
        smoke = smoke_module.Smoke()
        assert smoke.root == root
        assert smoke.source == SourceIdentity("kreshby/cloudfide-test-agent", None,
                                              base, merge, head, head, "t05-smoke")
        assert smoke.changed == changed
        assert tuple(executor.calls) == expected
