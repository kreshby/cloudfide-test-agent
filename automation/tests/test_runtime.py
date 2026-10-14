"""Pinned production runtime with finite HTTP/process fakes; no model/network."""

from dataclasses import replace
import io
import json
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from automation.test_agent import runtime as r
from automation.test_agent.provider import SELECTED_RUNTIME, digest
from automation.benchmarks import model_benchmark as p01


class Socket:
    def settimeout(self, timeout):
        assert 0 < timeout <= 180
    def shutdown(self, *_): pass
    def close(self): pass


class Remote:
    status = 200
    def __init__(self, data=b"selected-model", headers=None):
        self.data = data
        self.headers = headers or {}
    def getheader(self, key, default=None):
        return self.headers.get(key, default)
    def read1(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result


def http(monkeypatch, responses):
    requests = []
    class Connection:
        sock = Socket()
        def __init__(self, host, timeout):
            self.host = host
        def connect(self): pass
        def request(self, method, path, headers):
            requests.append((self.host, method, path, headers))
        def getresponse(self):
            return responses.pop(0)
        def close(self): pass
    monkeypatch.setattr(r.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(r.shutil, "disk_usage", lambda *_: SimpleNamespace(free=20 * 1024**3))
    return requests


def small_pin(monkeypatch):
    # Trusted test injection only; the production API accepts no pin argument.
    data = b"selected-model"
    monkeypatch.setattr(r, "SELECTED_RUNTIME", replace(SELECTED_RUNTIME, bytes=len(data), sha256=digest(data)))
    return data


def test_selected_exact_pins_and_fixed_server_settings(tmp_path):
    assert r.SELECTED_RUNTIME == SELECTED_RUNTIME
    assert SELECTED_RUNTIME.id == "qwen25-coder-7b-q4km"
    assert SELECTED_RUNTIME.repository == "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"
    assert SELECTED_RUNTIME.revision == "13fb94bfda8c8cf22497dc57b78f391a9acb426a"
    assert SELECTED_RUNTIME.filename == "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
    assert SELECTED_RUNTIME.bytes == 4683073536
    assert SELECTED_RUNTIME.sha256 == "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c"
    assert r.LLAMA_COMMIT == "d81235049384534c167caea52b85a694f6103d14" and r.LLAMA_TAG == "v0.6.0"
    assert r.LLAMA_URL == "https://github.com/ggml-org/llama.cpp.git"
    argv = r.server_argv(tmp_path)
    for flag, value in {"--host": "127.0.0.1", "--port": "8765", "--n-gpu-layers": "0", "--threads": "4",
                        "--threads-batch": "4", "--ctx-size": "8192", "--parallel": "1", "--seed": "42",
                        "--temp": "0", "--reasoning": "off", "--predict": "768"}.items():
        assert argv[argv.index(flag) + 1] == value
    assert {"--jinja", "--offline", "--no-webui", "--no-context-shift"}.issubset(argv)
    assert "0.0.0.0" not in argv and "--tools" not in argv
    with pytest.raises(TypeError):
        r.acquire_model(tmp_path, candidate="other")


@pytest.mark.parametrize("url", ["http://huggingface.co/a", "https://evil.example/a", "https://huggingface.co.evil/a",
                                 "https://user@huggingface.co/a", "https://huggingface.co:444/a", "https://huggingface.co/a#x",
                                 "https://huggingface.co/a\n", "https://hf.co.evil/a"])
def test_redirect_hosts_are_not_data_selected(url):
    with pytest.raises(r.RuntimeUnavailable):
        r.download_location(url)


def test_acquisition_fixed_revision_size_hash_and_bounded_redirects(tmp_path, monkeypatch):
    data = small_pin(monkeypatch)
    redirect = Remote(headers={"Location": "https://cas-bridge.xethub.hf.co/object?public=1"})
    redirect.status = 302
    calls = http(monkeypatch, [redirect, Remote(data)])
    model = r.acquire_model(tmp_path)
    assert model.read_bytes() == data
    assert calls[0][:3] == ("huggingface.co", "GET", "/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/13fb94bfda8c8cf22497dc57b78f391a9acb426a/qwen2.5-coder-7b-instruct-q4_k_m.gguf")
    assert calls[1][0] == "cas-bridge.xethub.hf.co"
    assert all("Authorization" not in c[3] for c in calls)


@pytest.mark.parametrize("mode", ["size", "hash", "long", "header", "redirect", "redirect-limit", "disk", "timeout", "http"])
def test_unavailable_acquisition_removes_temporary_model(tmp_path, monkeypatch, mode):
    data = small_pin(monkeypatch)
    remote = Remote(data)
    if mode == "size": remote.data = data[:-1]
    elif mode == "hash": remote.data = b"x" * len(data)
    elif mode == "long": remote.data = data + b"x"
    elif mode == "header": remote.headers["Content-Length"] = "999"
    elif mode in {"redirect", "redirect-limit"}:
        remote.status = 302
        remote.headers["Location"] = "https://evil.invalid" if mode == "redirect" else "https://huggingface.co/repeat"
    elif mode == "http": remote.status = 500
    http(monkeypatch, [remote] * 6)
    if mode == "disk": monkeypatch.setattr(r.shutil, "disk_usage", lambda *_: SimpleNamespace(free=1))
    elif mode == "timeout": monkeypatch.setattr(r, "ACQUISITION_SECONDS", -1)
    with pytest.raises(r.RuntimeUnavailable):
        r.acquire_model(tmp_path)
    assert not (tmp_path / SELECTED_RUNTIME.filename).exists()


def test_actual_child_env_is_allowlisted_capture_bounded_and_entire_group_stopped(tmp_path, monkeypatch):
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
                "AWS_SECRET_ACCESS_KEY", "AZURE_TOKEN", "GOOGLE_TOKEN", "SSH_AUTH_SOCK", "https_proxy",
                "HTTPS_PROXY", "LLAMA_ARG_TOOLS", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(key, "DUMMY_T07_ONLY")
    captured, signals = [], []
    class Child:
        pid = 999999999
        returncode = None
        stdout = io.BytesIO(b"x" * (r.CAPTURE_BYTES + 100))
        stderr = io.BytesIO(b"y" * (r.CAPTURE_BYTES + 100))
        def poll(self): return self.returncode
        def wait(self, timeout):
            self.returncode = -15
            return self.returncode
    def popen(argv, **kwargs):
        captured.append((argv, kwargs))
        return Child()
    monkeypatch.setattr(p01.subprocess, "Popen", popen)
    monkeypatch.setattr(p01.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    proc = r.Process(r.server_argv(tmp_path), tmp_path)
    proc.stop()
    assert len(proc.stdout) == len(proc.stderr) == r.CAPTURE_BYTES
    assert proc.truncated == {"stdout": True, "stderr": True}
    env = captured[0][1]["env"]
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                        "GIT_TERMINAL_PROMPT", "OMP_NUM_THREADS"}
    assert "DUMMY_T07_ONLY" not in env.values()
    assert captured[0][1]["start_new_session"] is True and captured[0][1].get("shell", False) is False
    assert signals == [(Child.pid, signal.SIGTERM), (Child.pid, signal.SIGKILL)]


def runtime_fakes(monkeypatch, *, health=True, exited=False):
    events = []
    def build(root, deadline, record):
        events.append("build")
        assert 0 < deadline - r.time.monotonic() <= r.SETUP_SECONDS
        return root / "llama/build/bin/llama-server"
    def acquire(root):
        events.append("acquire")
        (root / SELECTED_RUNTIME.filename).write_bytes(b"dummy")
    class Process:
        def __init__(self, argv, root):
            assert argv == r.server_argv(root)
            events.append("start")
            self.process = SimpleNamespace(poll=lambda: 1 if exited else None)
        def stop(self): events.append("stop-group")
    monkeypatch.setattr(r, "build_llama", build)
    monkeypatch.setattr(r, "acquire_model", acquire)
    monkeypatch.setattr(r, "Process", Process)
    monkeypatch.setattr(r, "health", lambda *_: health)
    return events


def test_lifecycle_stops_before_restart_and_removes_all_runtime_data(monkeypatch):
    events = runtime_fakes(monkeypatch)
    with r.LocalRuntime() as runtime:
        root = runtime.root
        assert runtime.provider._port == 8765
        runtime.stop()
        with pytest.raises(r.RuntimeUnavailable):
            _ = runtime.provider
        runtime.start()
        runtime.stop()
        with pytest.raises(r.RuntimeUnavailable): runtime.start()
    assert events == ["build", "acquire", "start", "stop-group", "start", "stop-group"]
    assert not root.exists() and runtime.server is None


@pytest.mark.parametrize("mode", ["timeout", "exit", "setup", "cancellation"])
def test_startup_and_cancellation_cleanup(monkeypatch, mode):
    events = runtime_fakes(monkeypatch, health=mode != "timeout", exited=mode == "exit")
    if mode == "timeout": monkeypatch.setattr(r, "STARTUP_SECONDS", 0)
    if mode == "setup": monkeypatch.setattr(r, "acquire_model", lambda *_: (_ for _ in ()).throw(OSError("dummy")))
    runtime = r.LocalRuntime()
    if mode == "cancellation":
        with pytest.raises(RuntimeError):
            with runtime:
                root = runtime.root
                raise RuntimeError("cancel")
        assert not root.exists()
    else:
        with pytest.raises(r.RuntimeUnavailable): runtime.__enter__()
    assert runtime.root is None and runtime.temporary is None and runtime.server is None
    if mode != "setup" and mode != "timeout": assert "stop-group" in events


def test_reused_build_executes_only_exact_llama_pin(monkeypatch, tmp_path):
    commands = []
    def command(argv, root, deadline, maximum, **kwargs):
        commands.append(argv)
        return r.LLAMA_COMMIT if "rev-parse" in argv else "v0.6.0"
    monkeypatch.setattr(p01, "_command", command)
    monkeypatch.setattr(p01.shutil, "disk_usage", lambda *_: SimpleNamespace(free=20 * 1024**3))
    r.build_llama(tmp_path, r.time.monotonic() + 420, {})
    fetch = next(c for c in commands if "fetch" in c)
    assert fetch[-2:] == [r.LLAMA_URL, r.LLAMA_COMMIT]
    assert any("-DGGML_CUDA=OFF" in c for c in commands)
    commands.clear()
    monkeypatch.setattr(p01, "_command", lambda *_a, **_k: "0" * 40)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(p01.BenchmarkFailure): r.build_llama(other, r.time.monotonic() + 420, {})


def test_started_server_health_timeout_stops_group_and_temp_data(monkeypatch):
    events = runtime_fakes(monkeypatch, health=False)
    times = iter(float(i) / 4 for i in range(100))
    monkeypatch.setattr(r.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(r.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(r, 'STARTUP_SECONDS', 2)
    runtime = r.LocalRuntime()
    with pytest.raises(r.RuntimeUnavailable): runtime.__enter__()
    assert events == ['build', 'acquire', 'start', 'stop-group']
    assert runtime.root is None and runtime.server is None


def test_lifecycle_records_bounded_host_startup_and_cpu_count(monkeypatch, capsys):
    events = runtime_fakes(monkeypatch)
    clock = {'now': 0.0}
    monkeypatch.setattr(r.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(r.os, 'cpu_count', lambda: 4)
    def ready(_):
        clock['now'] += 2
        return True
    monkeypatch.setattr(r, 'health', ready)
    monkeypatch.setenv('GITHUB_TOKEN', 'PRIVATE_AUTH_FIXTURE')
    with r.LocalRuntime() as runtime:
        runtime.stop()
        runtime.start()
    output = capsys.readouterr()
    assert [json.loads(line) for line in output.out.splitlines()] == [
        {'event': 'runtime_timing', 'model_startup_ms': 2000.0, 'runner_cpu_count': 4},
    ] * 2
    assert 'PRIVATE_AUTH_FIXTURE' not in output.out + output.err
    assert output.err == ''
    assert events == ['build', 'acquire', 'start', 'stop-group', 'start', 'stop-group']


@pytest.mark.parametrize('cpus,expected', [(1, 1), (4096, 4096), (None, None), (True, None),
                                        (0, None), (-1, None), (4097, None), (2.0, None),
                                        ('PRIVATE_HOST_VALUE', None), (10**100, None)])
def test_cpu_count_is_optional_bounded_integer(monkeypatch, capsys, cpus, expected):
    monkeypatch.setattr(r.time, 'monotonic', lambda: 1.0)
    monkeypatch.setattr(r.os, 'cpu_count', lambda: cpus)
    r._record_startup_timing(0.0)
    metrics = json.loads(capsys.readouterr().out)
    assert metrics == {'event': 'runtime_timing', 'model_startup_ms': 1000.0,
                       **({'runner_cpu_count': expected} if expected is not None else {})}


@pytest.mark.parametrize('elapsed', [0, 90, 90.001, -1, float('inf'), float('nan')])
def test_startup_timing_is_optional_bounded_host_number(monkeypatch, capsys, elapsed):
    monkeypatch.setattr(r.time, 'monotonic', lambda: elapsed)
    monkeypatch.setattr(r.os, 'cpu_count', lambda: 4)
    r._record_startup_timing(0.0)
    metrics = json.loads(capsys.readouterr().out)
    expected = {'event': 'runtime_timing', 'runner_cpu_count': 4}
    if elapsed in (0, 90):
        expected['model_startup_ms'] = elapsed * 1000
    assert metrics == expected


def test_unavailable_cpu_count_keeps_startup_timing_and_no_exception_text(monkeypatch, capsys):
    monkeypatch.setattr(r.time, 'monotonic', lambda: 1.0)
    def unavailable():
        raise OSError('PRIVATE_CPU_FAILURE')
    monkeypatch.setattr(r.os, 'cpu_count', unavailable)
    r._record_startup_timing(0.0)
    output = capsys.readouterr()
    assert json.loads(output.out) == {'event': 'runtime_timing', 'model_startup_ms': 1000.0}
    assert 'PRIVATE_CPU_FAILURE' not in output.out + output.err


def test_logging_failure_does_not_change_runtime_lifecycle(monkeypatch):
    events = runtime_fakes(monkeypatch)
    monkeypatch.setattr(r.time, 'monotonic', lambda: 0.0)
    def unavailable(*_):
        raise OSError('PRIVATE_OUTPUT_FAILURE')
    monkeypatch.setattr(r, 'print', unavailable, raising=False)
    with r.LocalRuntime() as runtime:
        assert runtime.provider._port == 8765
    assert events == ['build', 'acquire', 'start', 'stop-group']
