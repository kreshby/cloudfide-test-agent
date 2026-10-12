"""P01 hosted-only CPU experiment. Every command, URL and limit is host-owned."""

from dataclasses import asdict, dataclass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .scenarios import SCENARIOS, SYSTEM_PROMPT, messages
from .scoring import Inference, MAX_RESPONSE_BYTES, aggregate, score


LLAMA_TAG = "v0.6.0"
LLAMA_COMMIT = "d81235049384534c167caea52b85a694f6103d14"
LLAMA_URL = "https://github.com/ggml-org/llama.cpp.git"
BENCHMARK_REVISION = "P01-R2"
THREADS = 4
CONTEXT_TOKENS = 8192
OUTPUT_TOKENS = 512  # R2 section 9 fallback: 768 / 120 cannot fit all phase bounds.
SEED = 42
SETUP_SECONDS = 420
ACQUISITION_SECONDS = 180
STARTUP_SECONDS = 90
INFERENCE_SECONDS = 90
TOTAL_SECONDS = 2640  # 44 minutes: 2580 phase bounds + 60 overhead; 60 left for workflow.
SOCKET_SECONDS = 10
CAPTURE_BYTES = 16 * 1024  # Per stdout/stderr stream; excess is drained/discarded.
HTTP_BYTES = 128 * 1024
EVIDENCE_BYTES = 1024 * 1024
DISK_RESERVE_BYTES = 2 * 1024**3
MODEL_MAX_BYTES = 6 * 1024**3
ADDRESS_SPACE_BYTES = 12 * 1024**3
PORT = 8765
EVIDENCE_FILES = ("benchmark-results.json", "benchmark-summary.md")


@dataclass(frozen=True)
class Candidate:
    id: str
    repository: str
    revision: str
    filename: str
    bytes: int
    sha256: str
    quantization: str = "Q4_K_M"


CANDIDATES = (
    Candidate("qwen3-4b-q4km", "Qwen/Qwen3-4B-GGUF", "bc640142c66e1fdd12af0bd68f40445458f3869b",
              "Qwen3-4B-Q4_K_M.gguf", 2497280256,
              "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"),
    Candidate("qwen3-8b-q4km", "Qwen/Qwen3-8B-GGUF", "7c41481f57cb95916b40956ab2f0b139b296d974",
              "Qwen3-8B-Q4_K_M.gguf", 5027783488,
              "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"),
    Candidate("qwen25-coder-7b-q4km", "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
              "13fb94bfda8c8cf22497dc57b78f391a9acb426a", "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
              4683073536, "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c"),
)


class BenchmarkFailure(RuntimeError):
    """Fixed rule identifiers only; never include remote exception text or URLs."""


def remaining(deadline, maximum):
    value = min(maximum, deadline - time.monotonic())
    if value <= 0:
        raise BenchmarkFailure("time-budget-exhausted")
    return value


def fixed_environment(root):
    # No ambient model/tool configuration, proxies, keys, credential helpers or tokens.
    home = root / "home"
    home.mkdir(exist_ok=True)
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(home), "TMPDIR": str(root), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "OMP_NUM_THREADS": str(THREADS)}


class Process:
    """Private process supervisor for argv built by trusted code below, never responses."""

    def __init__(self, argv, root):
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = {"stdout": False, "stderr": False}
        self.peak_rss_bytes = None
        self.process = subprocess.Popen(argv, cwd=root, env=fixed_environment(root),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        self.readers = []
        for name in ("stdout", "stderr"):
            worker = threading.Thread(target=self._drain, args=(name,), daemon=True)
            worker.start()
            self.readers.append(worker)
        self.monitor = threading.Thread(target=self._rss, daemon=True)
        self.monitor.start()

    def _drain(self, name):
        stream = getattr(self.process, name)
        buffer = getattr(self, name)
        try:
            while chunk := stream.read(4096):
                available = CAPTURE_BYTES - len(buffer)
                buffer.extend(chunk[:available])
                if len(chunk) > available:
                    self.truncated[name] = True
        finally:
            stream.close()

    def _rss(self):
        while self.process.poll() is None:
            try:
                text = Path(f"/proc/{self.process.pid}/status").read_text()
                match = re.search(r"^VmHWM:\s+(\d+) kB", text, re.M)
                if match:
                    self.peak_rss_bytes = max(self.peak_rss_bytes or 0, int(match[1]) * 1024)
            except OSError:
                pass
            time.sleep(0.05)

    def stop(self):
        # Kill the entire session group even when its parent has already exited.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=2)
        for reader in self.readers:
            reader.join(timeout=1)
        self.monitor.join(timeout=1)


def _command(argv, root, deadline, maximum, include_stderr=False):
    process = Process(argv, root)
    try:
        try:
            code = process.process.wait(timeout=remaining(deadline, maximum))
        except subprocess.TimeoutExpired:
            raise BenchmarkFailure("setup-command-timeout") from None
        if code != 0:
            raise BenchmarkFailure("setup-command-nonzero")
    finally:
        process.stop()
    captured = bytes(process.stdout) + (bytes(process.stderr) if include_stderr else b"")
    return captured.decode("utf-8", errors="replace")


def build_llama(root, deadline, record):
    started = time.monotonic()
    source = root / "llama"
    source.mkdir()
    build = source / "build"
    record.update(tag=LLAMA_TAG, commit=LLAMA_COMMIT, acquisition_strategy="exact-commit-source-build",
                  source_acquisition_seconds=None, build_seconds=None, executable_version=None)
    phase = time.monotonic()
    try:
        if shutil.disk_usage(root).free < DISK_RESERVE_BYTES + 2 * 1024**3:
            raise BenchmarkFailure("insufficient-llama-build-disk-budget")
        _command(["git", "init", str(source)], root, deadline, 10)
        _command(["git", "-C", str(source), "fetch", "--depth=1", LLAMA_URL, LLAMA_COMMIT], root, deadline, 90)
        _command(["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"], root, deadline, 10)
        actual = _command(["git", "-C", str(source), "rev-parse", "HEAD"], root, deadline, 10).strip()
        if actual != LLAMA_COMMIT:
            raise BenchmarkFailure("llama-commit-mismatch")
    finally:
        record["source_acquisition_seconds"] = time.monotonic() - phase
    phase = time.monotonic()
    try:
        # Native CPU build; no dependency fetching, accelerators, HTTPS or tools configured.
        _command(["cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
                  "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_SERVER=ON",
                  "-DLLAMA_BUILD_UI=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF",
                  "-DLLAMA_OPENSSL=OFF", "-DGGML_CUDA=OFF", "-DGGML_VULKAN=OFF",
                  "-DGGML_BLAS=OFF", "-DGGML_NATIVE=ON"], root, deadline, 30)
        _command(["cmake", "--build", str(build), "--target", "llama-server", "--parallel", str(THREADS)],
                 root, deadline, 300)
        if shutil.disk_usage(root).free < DISK_RESERVE_BYTES:
            raise BenchmarkFailure("llama-build-disk-reserve")
    finally:
        record["build_seconds"] = time.monotonic() - phase
        record["acquisition_build_seconds"] = time.monotonic() - started
    executable = build / "bin" / "llama-server"
    record["executable_version"] = _command([str(executable), "--version"], root, deadline, 10, include_stderr=True)
    return executable


def _download_location(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    # HF resolve redirects only to its public object-storage domains, never arbitrary destinations.
    allowed = host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co"))
    if (parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password
            or parts.fragment or not allowed):
        raise BenchmarkFailure("model-download-destination-rejected")
    return host, parts.path + ("?" + parts.query if parts.query else "")


def acquire_model(candidate, directory, deadline, metrics):
    if candidate not in CANDIDATES:
        raise BenchmarkFailure("unapproved-candidate")
    started = time.monotonic()
    target = directory / candidate.filename
    if candidate.bytes > MODEL_MAX_BYTES or shutil.disk_usage(directory).free < candidate.bytes + DISK_RESERVE_BYTES:
        raise BenchmarkFailure("insufficient-model-disk-budget")
    url = f"https://huggingface.co/{candidate.repository}/resolve/{candidate.revision}/{candidate.filename}"
    received = 0
    digest = hashlib.sha256()
    try:
        for _ in range(5):
            host, path = _download_location(url)
            connection = http.client.HTTPSConnection(host, timeout=remaining(deadline, SOCKET_SECONDS))
            try:
                connection.request("GET", path, headers={"User-Agent": "p01-local-benchmark", "Accept-Encoding": "identity"})
                sock = connection.sock  # Response may detach the connection when Connection: close is used.
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = response.getheader("Location", "")
                    _download_location(url)
                    continue
                if response.status != 200:
                    raise BenchmarkFailure("model-download-http-failure")
                if response.getheader("Content-Encoding", "identity") != "identity":
                    raise BenchmarkFailure("model-download-encoding-rejected")
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) != candidate.bytes):
                    raise BenchmarkFailure("model-download-size-mismatch")
                with target.open("xb") as output:
                    while True:
                        sock.settimeout(remaining(deadline, SOCKET_SECONDS))
                        chunk = response.read1(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > candidate.bytes:
                            raise BenchmarkFailure("model-download-size-limit")
                        if shutil.disk_usage(directory).free < DISK_RESERVE_BYTES + len(chunk):
                            raise BenchmarkFailure("model-download-disk-reserve")
                        output.write(chunk)
                        digest.update(chunk)
                if received != candidate.bytes or digest.hexdigest() != candidate.sha256:
                    raise BenchmarkFailure("model-download-integrity-mismatch")
                metrics["download_sha256"] = digest.hexdigest()
                return target
            finally:
                connection.close()
        raise BenchmarkFailure("model-download-redirect-limit")
    except (OSError, http.client.HTTPException, ValueError):
        raise BenchmarkFailure("model-acquisition-unavailable") from None
    finally:
        metrics["downloaded_bytes"] = received
        metrics["model_acquisition_seconds"] = time.monotonic() - started


def server_argv(executable, model):
    return [str(executable), "--model", str(model), "--host", "127.0.0.1", "--port", str(PORT),
            "--n-gpu-layers", "0", "--threads", str(THREADS), "--threads-batch", str(THREADS),
            "--ctx-size", str(CONTEXT_TOKENS), "--predict", str(OUTPUT_TOKENS),
            "--parallel", "1", "--seed", str(SEED), "--temp", "0", "--jinja",
            "--reasoning", "off", "--reasoning-format", "none", "--reasoning-budget", "0",
            "--offline", "--no-webui", "--no-webui-mcp-proxy", "--no-context-shift"]


def _local_json(endpoint, payload, deadline):
    if endpoint not in ("/health", "/apply-template", "/tokenize", "/completion"):
        raise BenchmarkFailure("local-endpoint-rejected")
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=remaining(deadline, SOCKET_SECONDS))
    try:
        body = None if payload is None else json.dumps(payload).encode()
        connection.request("GET" if body is None else "POST", endpoint, body=body,
                           headers={"Content-Type": "application/json"})
        sock = connection.sock
        # Inference may legitimately produce no bytes until completion; bounded by request deadline.
        sock.settimeout(remaining(deadline, INFERENCE_SECONDS))
        response = connection.getresponse()
        if response.status != 200:
            raise BenchmarkFailure("local-server-http-failure")
        length = response.getheader("Content-Length")
        if length and (not length.isdigit() or int(length) > HTTP_BYTES):
            raise BenchmarkFailure("local-server-response-limit")
        chunks = bytearray()
        while True:
            sock.settimeout(remaining(deadline, INFERENCE_SECONDS))
            chunk = response.read1(min(4096, HTTP_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > HTTP_BYTES:
                raise BenchmarkFailure("local-server-response-limit")
        result = json.loads(chunks)
        if type(result) is not dict:
            raise BenchmarkFailure("local-server-malformed-json")
        return result
    except TimeoutError:
        raise BenchmarkFailure("inference-timeout") from None
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise BenchmarkFailure("local-server-unavailable") from None
    finally:
        connection.close()


class LocalInference:
    def __init__(self, executable, model, root, deadline, metrics):
        self.deadline = deadline
        self.metrics = metrics
        self.process = Process(server_argv(executable, model), root)

    def start(self):
        started = time.monotonic()
        deadline = min(self.deadline, started + STARTUP_SECONDS)
        try:
            while True:
                remaining(deadline, STARTUP_SECONDS)
                if self.process.process.poll() is not None:
                    raise BenchmarkFailure("model-startup-exit")
                try:
                    _local_json("/health", None, min(deadline, time.monotonic() + 1))
                    return
                except BenchmarkFailure as error:
                    if str(error) == "host-termination":
                        raise
                    time.sleep(0.1)
        except BenchmarkFailure as error:
            if str(error) == "host-termination":
                raise
            raise BenchmarkFailure("model-startup-failed-or-timeout") from None
        finally:
            # Includes llama.cpp's default warmup; acquisition is measured separately.
            self.metrics["startup_model_load_seconds"] = time.monotonic() - started

    def infer(self, scenario, case_metadata):
        started = time.monotonic()
        deadline = min(self.deadline, started + INFERENCE_SECONDS)
        try:
            applied = _local_json("/apply-template", {"messages": messages(scenario)}, deadline)
            prompt = applied.get("prompt")
            if type(prompt) is not str or len(prompt.encode()) > HTTP_BYTES:
                raise BenchmarkFailure("rendered-prompt-limit-or-format")
            case_metadata["rendered_prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            case_metadata["rendered_prompt_suffix"] = prompt[-512:]
            tokens = _local_json("/tokenize", {"content": prompt, "add_special": True}, deadline).get("tokens")
            if type(tokens) is not list or not all(type(t) is int and 0 <= t < 2**31 for t in tokens):
                raise BenchmarkFailure("tokenizer-format-rejected")
            case_metadata["tokenized_prompt_tokens"] = len(tokens)
            if len(tokens) + OUTPUT_TOKENS > CONTEXT_TOKENS:
                raise BenchmarkFailure("prompt-context-token-limit")
            reply = _local_json("/completion", {
                "prompt": tokens, "n_predict": OUTPUT_TOKENS, "temperature": 0.0,
                "top_k": 1, "top_p": 1.0, "min_p": 0.0, "seed": SEED,
                "repeat_penalty": 1.0, "cache_prompt": False, "stream": False,
            }, deadline)
            raw = reply.get("content")
            if type(raw) is not str:
                raise BenchmarkFailure("completion-format-rejected")
            timings = reply.get("timings", {})
            if type(timings) is not dict:
                timings = {}
            def metric(key):
                value = timings.get(key)
                try:
                    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
                except OverflowError:
                    return None
            failure = "completion-context-truncated" if reply.get("truncated") is True else None
            return Inference(raw, failure, time.monotonic() - started, len(tokens),
                             metric("predicted_n"), metric("predicted_per_second"))
        except BenchmarkFailure as error:
            if str(error) == "host-termination":
                raise
            return Inference(failure=str(error), wall_seconds=time.monotonic() - started)

    def close(self):
        self.process.stop()
        self.metrics["process_peak_rss_bytes"] = self.process.peak_rss_bytes
        self.metrics["process_capture_truncated"] = self.process.truncated
        self.metrics["process_exit_code"] = self.process.process.returncode
        self.metrics["process_stdout"] = bytes(self.process.stdout).decode("utf-8", errors="replace")
        self.metrics["process_stderr"] = bytes(self.process.stderr).decode("utf-8", errors="replace")


def tree_bytes(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def run_candidate(candidate, executable, root, deadline, acquire=acquire_model, factory=LocalInference):
    """Injection is for trusted deterministic tests only; responses cannot supply capabilities."""
    if candidate not in CANDIDATES:
        raise BenchmarkFailure("unapproved-candidate")
    started = time.monotonic()
    data = {**asdict(candidate), "availability": "failed", "failure": None,
            "resource_metrics": {}, "scenarios": []}
    metrics = data["resource_metrics"]
    metrics.update(downloaded_bytes=0, model_acquisition_seconds=None, startup_model_load_seconds=None,
                   process_peak_rss_bytes=None, runner_disk_free_before_model=shutil.disk_usage(root).free,
                   runner_disk_free_after_model_load=None)
    directory = root / candidate.id
    directory.mkdir()
    provider = None
    failure = None
    try:
        remaining(deadline, TOTAL_SECONDS)
        model = acquire(candidate, directory, min(deadline, time.monotonic() + ACQUISITION_SECONDS), metrics)
        remaining(deadline, TOTAL_SECONDS)
        provider = factory(executable, model, directory, deadline, metrics)
        provider.start()
        metrics["runner_disk_free_after_model_load"] = shutil.disk_usage(root).free
        for scenario in SCENARIOS:
            metadata = {}
            inference = provider.infer(scenario, metadata)
            case = score(scenario, inference)
            case.update(metadata)
            data["scenarios"].append(case)
            if case["failure"]:
                failure = case["failure"]
                break  # Do not queue more work after timeout/truncation; stop the model.
        if failure is None:
            data["availability"] = "completed"
    except BenchmarkFailure as error:
        if str(error) == "host-termination":
            raise
        failure = str(error)
    except (OSError, ValueError, subprocess.SubprocessError):
        failure = "candidate-runtime-unavailable"
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            metrics["model_cache_bytes_before_cleanup"] = tree_bytes(directory)
            metrics["runner_disk_free_before_cleanup"] = shutil.disk_usage(root).free
            shutil.rmtree(directory)
            metrics["model_cache_bytes_after_cleanup"] = 0 if not directory.exists() else tree_bytes(directory)
            metrics["runner_disk_free_after_cleanup"] = shutil.disk_usage(root).free
            metrics["total_benchmark_seconds"] = time.monotonic() - started
    data["failure"] = failure
    for scenario in SCENARIOS[len(data["scenarios"]):]:
        data["scenarios"].append(score(scenario, Inference(failure=failure or "not-attempted")))
    data["aggregate"] = aggregate(data["scenarios"])
    return data


def summary(results):
    lines = ["# P01 local CPU model benchmark", "", "Selection evidence only; no production winner selected.",
             "Generated tests were not executed or verified.", "",
             "| Candidate | Availability/failure | Quality | JSON / contract / policy | Median / total inference s | Tok/s | Peak RSS bytes | Download bytes / s |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    def show(value):
        return "n/a" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)
    for candidate in results["candidates"]:
        a, m = candidate["aggregate"], candidate["resource_metrics"]
        lines.append(f"| {candidate['id']} | {candidate['availability']} / {candidate['failure'] or 'none'} | "
                     f"{a['score']}/{a['maximum_score']} | {a['raw_json_valid_rate']:.0%} / "
                     f"{a['contract_valid_rate']:.0%} / {a['policy_accepted_rate']:.0%} | "
                     f"{show(a['median_inference_seconds'])} / {show(a['total_inference_seconds'])} | "
                     f"{show(a['median_completion_tokens_per_second'])} | {show(m.get('process_peak_rss_bytes'))} | "
                     f"{show(m.get('downloaded_bytes'))} / {show(m.get('model_acquisition_seconds'))} |")
    lines += ["", "All five scenarios remain in each denominator. n/a means unavailable, not zero.",
              "Inference wall time includes template rendering/tokenization. Tok/s is llama.cpp generation timing.",
              "Startup includes warmup. RSS is Linux process VmHWM sampled every 50 ms; very short peaks may be missed.",
              f"Setup failure: {results.get('setup_failure') or 'none'}.", ""]
    return "\n".join(lines)


def write_evidence(results, directory):
    # Work on a copy: evidence truncation must not change score or measurements.
    document = json.loads(json.dumps(results, allow_nan=False))
    document["evidence_truncated"] = False
    human = summary(document).encode()
    while True:
        machine = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        if len(machine) + len(human) <= EVIDENCE_BYTES:
            break
        reduced = False
        for candidate in document["candidates"]:
            for case in candidate["scenarios"]:
                raw = case["raw_response"]
                if raw:
                    case["raw_response"] = raw[:len(raw) // 2]
                    case["evidence_response_truncated"] = True
                    reduced = True
        document["evidence_truncated"] = True
        if not reduced:
            raise BenchmarkFailure("evidence-metadata-budget-exceeded")
    directory.mkdir(exist_ok=True)
    for name, content in zip(EVIDENCE_FILES, (machine, human)):
        temporary = directory / (name + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(directory / name)
    check_evidence(directory)


def check_evidence(directory):
    if set(p.name for p in directory.iterdir()) != set(EVIDENCE_FILES):
        raise BenchmarkFailure("unexpected-evidence-files")
    for name in EVIDENCE_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise BenchmarkFailure("unsafe-evidence-file")
    if sum((directory / name).stat().st_size for name in EVIDENCE_FILES) > EVIDENCE_BYTES:
        raise BenchmarkFailure("evidence-byte-budget-exceeded")


def require_hosted_runner():
    if not (os.environ.get("GITHUB_ACTIONS") == "true" and
            os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted" and
            os.environ.get("RUNNER_OS") == "Linux" and os.environ.get("ImageOS") == "ubuntu24"):
        raise BenchmarkFailure("benchmark-requires-ubuntu24-standard-hosted-runner")


def main():
    require_hosted_runner()  # Refuse model downloads from local review commands.
    root_parent = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    evidence = root_parent / "p01-evidence"
    started = time.monotonic()
    deadline = started + TOTAL_SECONDS
    results = {
        "schema_version": 1, "purpose": "P01-selection-evidence-only",
        "benchmark_revision": BENCHMARK_REVISION,
        "runner": {"os": platform.platform(), "cpu_count": os.cpu_count(),
                   "cpu_model": next((line.split(':', 1)[1].strip() for line in
                                      Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')), None),
                   "memory_total_bytes": os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'),
                   "image_version": os.environ.get("ImageVersion"), "python": platform.python_version()},
        "configuration": {"cpu_threads": THREADS, "context_tokens": CONTEXT_TOKENS,
                          "completion_tokens": OUTPUT_TOKENS, "seed": SEED, "temperature": 0,
                          "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
                          "top_k": 1, "top_p": 1, "min_p": 0, "repeat_penalty": 1,
                          "cache_prompt": False, "grammar": None, "chat_template": "GGUF-native Jinja",
                          "reasoning": "off", "reasoning_format": "none", "reasoning_budget": 0,
                          "model_network_tools": False, "setup_seconds": SETUP_SECONDS,
                          "acquisition_seconds": ACQUISITION_SECONDS, "startup_seconds": STARTUP_SECONDS,
                          "inference_seconds": INFERENCE_SECONDS, "total_seconds": TOTAL_SECONDS,
                          "response_bytes": MAX_RESPONSE_BYTES, "http_bytes": HTTP_BYTES,
                          "capture_bytes_per_stream": CAPTURE_BYTES, "evidence_bytes": EVIDENCE_BYTES,
                          "disk_reserve_bytes": DISK_RESERVE_BYTES, "model_max_bytes": MODEL_MAX_BYTES,
                          "address_space_bytes": ADDRESS_SPACE_BYTES},
        "effective_messages": {s.id: messages(s) for s in SCENARIOS},
        "llama_cpp": {}, "candidates": [], "setup_failure": None,
    }
    def terminate(_signum, _frame):
        raise BenchmarkFailure("host-termination")
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    # Inherited by compiler and server; no core dumps or unbounded process files.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MODEL_MAX_BYTES, MODEL_MAX_BYTES))
    with tempfile.TemporaryDirectory(prefix="p01-runtime-", dir=root_parent) as temporary:
        root = Path(temporary)
        try:
            results["source_sha"] = _command(["git", "-C", str(Path.cwd()), "rev-parse", "HEAD"], root, deadline, 10).strip()
            if re.fullmatch(r"[0-9a-f]{40}", results["source_sha"]) is None:
                raise BenchmarkFailure("invalid-source-sha")
            executable = build_llama(root, min(deadline, time.monotonic() + SETUP_SECONDS), results["llama_cpp"])
            for candidate in CANDIDATES:
                results["candidates"].append(run_candidate(candidate, executable, root, deadline))
                write_evidence(results, evidence)
        except BenchmarkFailure as error:
            results["setup_failure"] = str(error)
        except (OSError, subprocess.SubprocessError):
            results["setup_failure"] = "host-setup-or-total-budget-failure"
        finally:
            for candidate in CANDIDATES[len(results["candidates"]):]:
                cases = [score(s, Inference(failure="setup-unavailable")) for s in SCENARIOS]
                results["candidates"].append({**asdict(candidate), "availability": "unavailable",
                                               "failure": results["setup_failure"], "resource_metrics": {},
                                               "scenarios": cases, "aggregate": aggregate(cases)})
            results["total_wall_seconds"] = time.monotonic() - started
            write_evidence(results, evidence)
    return int(results["setup_failure"] is not None or any(c["failure"] for c in results["candidates"]))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check-evidence":
        check_evidence(Path(sys.argv[2]))
    elif len(sys.argv) == 1:
        sys.exit(main())
    else:
        raise SystemExit("Only the hosted benchmark and fixed evidence check are supported")
