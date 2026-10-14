"""Host-owned bootstrap of one P01-selected local runtime; no caller URL/argv.

P01's fixed exact-commit CPU build and bounded process supervisor are reused.
The production downloader has one fixed model and an absolute acquisition timer.
No transcript, raw process output or environment is exported from this module.
"""

import hashlib
import http.client
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

from automation.benchmarks.model_benchmark import (
    LLAMA_COMMIT, LLAMA_TAG, LLAMA_URL, Process, build_llama, fixed_environment,
    DISK_RESERVE_BYTES, CAPTURE_BYTES,
)
from .provider import (SELECTED_RUNTIME, COMPLETION_TOKENS, LocalLlamaProvider, strict_json,
                       canonical, _timing_number)


SETUP_SECONDS = 420
ACQUISITION_SECONDS = 180
STARTUP_SECONDS = 90
SOCKET_SECONDS = 10
MAX_REDIRECTS = 4
PORT = 8765
MODEL_URL = (f"https://huggingface.co/{SELECTED_RUNTIME.repository}/resolve/"
             f"{SELECTED_RUNTIME.revision}/{SELECTED_RUNTIME.filename}")


class RuntimeUnavailable(ValueError):
    """Fixed rule messages only, with no remote exception text or process output."""


def remaining(deadline, maximum=SOCKET_SECONDS):
    seconds = min(maximum, deadline - time.monotonic())
    if seconds <= 0:
        raise RuntimeUnavailable("runtime deadline exceeded")
    return seconds


def download_location(url):
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        allowed = host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co"))
        if (type(url) is not str or len(url) > 8192 or p.scheme != "https" or p.port not in (None, 443)
                or p.username or p.password or p.fragment or not allowed
                or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            raise ValueError()
        return host, p.path + ("?" + p.query if p.query else "")
    except (ValueError, TypeError):
        raise RuntimeUnavailable("model redirect destination rejected") from None


def _expire(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
    except OSError:
        pass


def _stream_model(response, sock, target, deadline):
    pin = SELECTED_RUNTIME
    if response.getheader("Content-Encoding", "identity") != "identity":
        raise RuntimeUnavailable("model encoding rejected")
    length = response.getheader("Content-Length")
    if length is not None and (not length.isdigit() or len(length) > 16 or int(length) != pin.bytes):
        raise RuntimeUnavailable("model byte count rejected")
    total, hashed = 0, hashlib.sha256()
    with target.open("xb") as output:
        while True:
            sock.settimeout(remaining(deadline))
            chunk = response.read1(min(1024 * 1024, pin.bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > pin.bytes:
                raise RuntimeUnavailable("model byte count rejected")
            if shutil.disk_usage(target.parent).free < DISK_RESERVE_BYTES + len(chunk):
                raise RuntimeUnavailable("model disk reserve exhausted")
            output.write(chunk)
            hashed.update(chunk)
    remaining(deadline)
    if total != pin.bytes:
        raise RuntimeUnavailable("model byte count rejected")
    if hashed.hexdigest() != pin.sha256:
        raise RuntimeUnavailable("model SHA-256 rejected")
    return target


def acquire_model(directory):
    """No model/candidate/URL argument. Incomplete data is always deleted."""
    deadline = time.monotonic() + ACQUISITION_SECONDS
    target = directory / SELECTED_RUNTIME.filename
    if shutil.disk_usage(directory).free < SELECTED_RUNTIME.bytes + DISK_RESERVE_BYTES:
        raise RuntimeUnavailable("insufficient model disk reserve")
    url, complete = MODEL_URL, False
    try:
        for redirects in range(MAX_REDIRECTS + 1):
            host, path = download_location(url)
            connection = http.client.HTTPSConnection(host, timeout=remaining(deadline))
            timer = None
            try:
                connection.connect()
                sock = connection.sock
                timer = threading.Timer(remaining(deadline, ACQUISITION_SECONDS), _expire, args=(sock,))
                timer.daemon = True
                timer.start()
                sock.settimeout(remaining(deadline))
                connection.request("GET", path, headers={"User-Agent": "trusted-t07-runtime",
                                                         "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    if redirects == MAX_REDIRECTS:
                        raise RuntimeUnavailable("model redirect budget exhausted")
                    url = response.getheader("Location", "")
                    download_location(url)
                    continue
                if response.status != 200:
                    raise RuntimeUnavailable("selected model acquisition unavailable")
                result = _stream_model(response, sock, target, deadline)
                complete = True
                return result
            finally:
                if timer is not None:
                    timer.cancel()
                connection.close()
    except RuntimeUnavailable:
        raise
    except Exception:
        raise RuntimeUnavailable("selected model acquisition unavailable") from None
    finally:
        if not complete:
            target.unlink(missing_ok=True)


def server_argv(root):
    return [str(root / "llama/build/bin/llama-server"), "--model", str(root / SELECTED_RUNTIME.filename),
            "--host", "127.0.0.1", "--port", str(PORT), "--n-gpu-layers", "0",
            "--threads", "4", "--threads-batch", "4", "--ctx-size", "8192",
            "--predict", str(max(COMPLETION_TOKENS.values())), "--parallel", "1", "--seed", "42",
            "--temp", "0", "--jinja", "--reasoning", "off", "--reasoning-format", "none",
            "--reasoning-budget", "0", "--offline", "--no-webui", "--no-webui-mcp-proxy", "--no-context-shift"]


def health(deadline):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=remaining(deadline, 1))
    timer = None
    try:
        connection.connect()
        sock = connection.sock
        timer = threading.Timer(remaining(deadline, 1), _expire, args=(sock,))
        timer.daemon = True
        timer.start()
        connection.request("GET", "/health", headers={"Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            return False
        raw = response.read(4097)
        if len(raw) > 4096:
            return False
        return strict_json(raw.decode(), 4096).get("status") == "ok"
    except Exception:
        return False
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def _record_startup_timing(started):
    try:
        metrics = {}
        elapsed = _timing_number((time.monotonic() - started) * 1000, STARTUP_SECONDS * 1000)
        if elapsed is not None:
            metrics["model_startup_ms"] = round(elapsed, 3)
        try:
            cpus = _timing_number(os.cpu_count(), 4096, integer=True)
            if cpus is not None and cpus > 0:
                metrics["runner_cpu_count"] = cpus
        except Exception:
            pass  # CPU count may be unavailable; retain valid startup timing.
        print(canonical({"event": "runtime_timing", **metrics}).decode())
    except Exception:
        pass  # Neither logging nor optional metrics may change lifecycle rules.


class LocalRuntime:
    """Temporary source/build/GGUF; at most one server, stopped before Docker.

    Restart after verification reuses the same verified on-disk model. There is
    no fallback/retry generation or persistent cache. Always use as a context
    manager. Host cancellation raises through it so both group and files clean.
    """

    def __init__(self):
        self.temporary = None
        self.root = None
        self.server = None
        self.starts = 0

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="t07-runtime-")
        self.root = Path(self.temporary.name)
        try:
            build_llama(self.root, time.monotonic() + SETUP_SECONDS, {})
            acquire_model(self.root)
            self.start()
            return self
        except BaseException:
            self.close()
            raise RuntimeUnavailable("selected runtime setup unavailable") from None

    def start(self):
        if self.server is not None or self.root is None or self.starts >= 2:
            raise RuntimeUnavailable("runtime lifecycle rejected")
        self.starts += 1
        started = time.monotonic()
        deadline = started + STARTUP_SECONDS
        try:
            self.server = Process(server_argv(self.root), self.root)
            while True:
                remaining(deadline, STARTUP_SECONDS)
                if self.server.process.poll() is not None:
                    raise RuntimeUnavailable("selected server exited during startup")
                if health(deadline):
                    return LocalLlamaProvider(PORT)
                time.sleep(min(0.1, remaining(deadline)))
        except BaseException:
            self.stop()
            raise RuntimeUnavailable("selected server startup unavailable") from None
        finally:
            _record_startup_timing(started)

    @property
    def provider(self):
        if self.server is None:
            raise RuntimeUnavailable("selected server is stopped")
        return LocalLlamaProvider(PORT)

    def stop(self):
        if self.server is not None:
            try:
                self.server.stop()  # P01 kills the entire group even if the parent exited.
            except Exception:
                raise RuntimeUnavailable("selected server cleanup unavailable") from None
            self.server = None

    def close(self):
        try:
            self.stop()
        finally:
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None
                self.root = None

    def __exit__(self, *_):
        self.close()
