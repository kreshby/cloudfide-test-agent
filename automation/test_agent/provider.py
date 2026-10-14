"""Bounded inference protocol; local runtime bootstrap is outside this adapter."""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import http.client
import json
import math
import re
import socket
import threading
import time
from types import MappingProxyType
from typing import Protocol

from .contracts import Target
from .diagnostics import Diagnostic, safe_diagnostic
from .protocol import stage_schema


MAX_REQUEST_BYTES = 24 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_HTTP_BYTES = 128 * 1024
MAX_CALL_SECONDS = 600
CONTEXT_TOKENS = 8192
LLAMA_COMMIT = "d81235049384534c167caea52b85a694f6103d14"


@dataclass(frozen=True)
class RuntimePin:
    id: str = "qwen25-coder-7b-q4km"
    repository: str = "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"
    revision: str = "13fb94bfda8c8cf22497dc57b78f391a9acb426a"
    filename: str = "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
    bytes: int = 4683073536
    sha256: str = "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c"


SELECTED_RUNTIME = RuntimePin()


class Stage(StrEnum):
    DECISION = "decision"
    EDITS = "edits"
    REPAIR = "repair"


COMPLETION_TOKENS = MappingProxyType({Stage.DECISION: 256, Stage.EDITS: 768,
                                     Stage.REPAIR: 768})
RESPONSE_LIMITS = MappingProxyType({Stage.DECISION: 4096, Stage.EDITS: MAX_RESPONSE_BYTES,
                                  Stage.REPAIR: MAX_RESPONSE_BYTES})


class ProviderError(ValueError):
    """Fixed rule messages only, never remote exception/response text."""

    def __init__(self, message, *, diagnostic=Diagnostic.PROVIDER_HTTP):
        super().__init__(message)
        self.diagnostic = safe_diagnostic(diagnostic)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("duplicate JSON field")
        result[key] = value
    return result


def strict_json(raw: str, maximum: int):
    if type(raw) is not str:
        raise ProviderError("provider response must be text")
    try:
        if len(raw.encode("utf-8")) > maximum:
            raise ProviderError("provider response byte limit")
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise ProviderError("invalid or oversized provider JSON", diagnostic=Diagnostic.PROVIDER_JSON) from None


@dataclass(frozen=True)
class ProviderRequest:
    stage: Stage
    target: Target
    binding: str
    system: str
    user: str

    def __post_init__(self):
        if (type(self.stage) is not Stage or type(self.target) is not Target
                or type(self.binding) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.binding) is None
                or type(self.system) is not str or type(self.user) is not str):
            raise ProviderError("invalid host provider request")
        if len(self.to_bytes()) > MAX_REQUEST_BYTES:
            raise ProviderError("provider request byte limit")

    def to_bytes(self):
        return canonical({"stage": self.stage, "target": self.target, "binding": self.binding,
                          "system": self.system, "user": self.user,
                          "response_schema_sha256": digest(canonical(self.response_schema)),
                          "completion_tokens": COMPLETION_TOKENS[self.stage]})

    @property
    def response_schema(self):
        # No caller/model-supplied schema. A fresh shape also prevents mutation
        # of a returned dict from changing subsequent requests.
        value = strict_json(self.user, MAX_REQUEST_BYTES)
        evidence = value.get("input") if type(value) is dict else None
        return stage_schema(self.stage, self.target, self.binding, evidence, constrain_actions=True)


class Provider(Protocol):
    def infer(self, request: ProviderRequest, timeout: float) -> str: ...


class FakeProvider:
    """Finite raw transcripts; captured exact requests establish skill use."""

    def __init__(self, responses: tuple[str, ...]):
        if (type(responses) is not tuple or len(responses) > 6
                or any(type(item) is not str or len(item.encode()) > MAX_HTTP_BYTES
                       for item in responses)):
            raise ProviderError("invalid bounded fake transcript")
        self._responses = responses
        self.requests: list[ProviderRequest] = []

    def infer(self, request, timeout):
        _validate_call(request, timeout)
        if len(self.requests) >= len(self._responses):
            raise ProviderError("fake transcript exhausted")
        response = self._responses[len(self.requests)]
        self.requests.append(request)
        return response


def _validate_call(request, timeout):
    if type(request) is not ProviderRequest:
        raise ProviderError("unsupported provider operation")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_CALL_SECONDS):
        raise ProviderError("invalid provider timeout")


def _timing_number(value, maximum, *, integer=False):
    # Exact builtin types exclude booleans; the range rejects NaN/infinity and
    # huge integers without converting them to floats or serializing them.
    if type(value) in ((int,) if integer else (int, float)) and 0 <= value <= maximum:
        return value
    return None


def _completion_timing(reply, maximum):
    timings = reply.get("timings")
    if type(timings) is not dict:
        timings = {}
    metrics = {}
    prompt = _timing_number(reply.get("tokens_evaluated"), CONTEXT_TOKENS, integer=True)
    if prompt is None:
        prompt = _timing_number(timings.get("prompt_n"), CONTEXT_TOKENS, integer=True)
    if prompt is not None:
        metrics["prompt_tokens"] = prompt
    completion = _timing_number(reply.get("tokens_predicted"), maximum, integer=True)
    if completion is not None:
        metrics["completion_tokens"] = completion
    for source, target, bound in (
        ("prompt_ms", "prompt_ms", MAX_CALL_SECONDS * 1000),
        ("predicted_ms", "generation_ms", MAX_CALL_SECONDS * 1000),
        ("prompt_per_second", "prompt_tokens_per_second", 1_000_000),
        ("predicted_per_second", "generation_tokens_per_second", 1_000_000),
    ):
        value = _timing_number(timings.get(source), bound)
        if value is not None:
            metrics[target] = value
    return metrics


def _record_inference_timing(started, metrics):
    try:
        # A larger evidence bound allows observing deadline/cleanup overhead;
        # it does not extend the inference deadline or active execution budget.
        elapsed = _timing_number((time.monotonic() - started) * 1000, 1_800_000)
        if elapsed is not None:
            metrics["call_elapsed_ms"] = round(elapsed, 3)
        print(canonical({"event": "inference_timing", **metrics}).decode())
    except Exception:
        pass  # Optional numeric evidence must not affect inference or errors.


class LocalLlamaProvider:
    """Only fixed IPv4 loopback, native endpoints from the pinned P01 server.

    Each inference has at most three HTTP requests and a single shared deadline.
    No proxies, redirects, authentication, downloads, startup, or fallback.
    """

    def __init__(self, port: int = 8765):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ProviderError("invalid host-owned loopback port")
        self._port = port

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("provider deadline exhausted", diagnostic=Diagnostic.PROVIDER_TIMEOUT)
        return min(remaining, MAX_CALL_SECONDS)

    def _post(self, endpoint, payload, deadline):
        if endpoint not in ("/apply-template", "/tokenize", "/completion"):
            raise ProviderError("unsupported local endpoint")
        body = canonical(payload)
        if len(body) > MAX_HTTP_BYTES:
            raise ProviderError("local HTTP request byte limit")
        connection = http.client.HTTPConnection("127.0.0.1", self._port,
                                                timeout=self._remaining(deadline))
        timer = None
        try:
            connection.connect()
            sock = connection.sock
            # A socket timeout alone bounds inactivity, not slow-drip headers.
            # Close the socket at the shared absolute deadline as well.
            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                    sock.close()
                except OSError:
                    pass
            timer = threading.Timer(self._remaining(deadline), expire)
            timer.daemon = True
            timer.start()
            sock.settimeout(self._remaining(deadline))
            connection.request("POST", endpoint, body=body,
                               headers={"Content-Type": "application/json",
                                        "Accept-Encoding": "identity"})
            sock.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            if response.status != 200:
                raise ProviderError("local HTTP status rejected")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ProviderError("local HTTP encoding rejected")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or len(length) > 8
                                       or int(length) > MAX_HTTP_BYTES):
                raise ProviderError("local HTTP response byte limit")
            data = bytearray()
            while True:
                # The final framed byte closes response.fp. For Connection:
                # close this also closes the socket retained by the timer.
                if response.isclosed():
                    break
                sock.settimeout(self._remaining(deadline))
                chunk = response.read1(min(4096, MAX_HTTP_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_HTTP_BYTES:
                    raise ProviderError("local HTTP response byte limit")
            self._remaining(deadline)
            if length is not None and len(data) != int(length):
                raise ProviderError("local HTTP response length mismatch")
            value = strict_json(data.decode("utf-8"), MAX_HTTP_BYTES)
            if type(value) is not dict:
                raise ProviderError("local HTTP object required")
            return value
        except ProviderError:
            raise
        except (OSError, UnicodeError, http.client.HTTPException, ValueError, AttributeError):
            if time.monotonic() >= deadline:
                raise ProviderError("provider deadline exhausted", diagnostic=Diagnostic.PROVIDER_TIMEOUT) from None
            raise ProviderError("local HTTP unavailable or rejected") from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def infer(self, request, timeout):
        _validate_call(request, timeout)
        started = time.monotonic()
        metrics = {}
        try:
            return self._infer(request, started + timeout, metrics)
        finally:
            _record_inference_timing(started, metrics)

    def _infer(self, request, deadline, metrics):
        applied = self._post("/apply-template", {"messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ]}, deadline)
        prompt = applied.get("prompt")
        if type(prompt) is not str or len(prompt.encode()) > MAX_HTTP_BYTES:
            raise ProviderError("rendered prompt rejected", diagnostic=Diagnostic.PROVIDER_PROTOCOL)
        tokens = self._post("/tokenize", {"content": prompt, "add_special": True}, deadline).get("tokens")
        maximum = COMPLETION_TOKENS[request.stage]
        if (type(tokens) is not list or not tokens
                or any(type(t) is not int or not 0 <= t < 2**31 for t in tokens)):
            raise ProviderError("prompt token budget or format rejected", diagnostic=Diagnostic.PROVIDER_PROTOCOL)
        if len(tokens) + maximum > CONTEXT_TOKENS:
            raise ProviderError("prompt token budget or format rejected", diagnostic=Diagnostic.PROVIDER_TOKEN_BUDGET)
        reply = self._post("/completion", {
            "prompt": tokens, "n_predict": maximum, "temperature": 0.0, "top_k": 1,
            "top_p": 1.0, "min_p": 0.0, "seed": 42, "repeat_penalty": 1.0,
            "cache_prompt": False, "stream": False,
            "json_schema": request.response_schema,
        }, deadline)
        metrics.update(_completion_timing(reply, maximum))
        if reply.get("truncated") is not False:
            raise ProviderError("completion truncation missing or true", diagnostic=(
                Diagnostic.PROVIDER_TRUNCATED if reply.get("truncated") is True else Diagnostic.PROVIDER_PROTOCOL))
        count = reply.get("tokens_predicted")
        if type(count) is not int or not 0 <= count <= maximum:
            raise ProviderError("completion token evidence rejected", diagnostic=Diagnostic.PROVIDER_PROTOCOL)
        # The pinned native API's truncated flag describes prompt/context
        # truncation. Output exhaustion is reported separately as stop_type.
        stop = reply.get("stop_type")
        if stop == "limit":
            raise ProviderError("completion prediction budget exhausted", diagnostic=Diagnostic.PROVIDER_TRUNCATED)
        if type(stop) is not str or stop not in {"eos", "word"}:
            raise ProviderError("completion stop evidence rejected", diagnostic=Diagnostic.PROVIDER_PROTOCOL)
        raw = reply.get("content")
        strict_json(raw, RESPONSE_LIMITS[request.stage])
        return raw
