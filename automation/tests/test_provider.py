"""Adapter tests with fake HTTP only; no server, model, network, or download."""

import json

import pytest

from automation.benchmarks.model_benchmark import CANDIDATES, LLAMA_COMMIT as P01_COMMIT
from automation.test_agent.contracts import Target
from automation.test_agent import provider as module
from automation.test_agent.provider import (COMPLETION_TOKENS, FakeProvider, LocalLlamaProvider,
                                           MAX_HTTP_BYTES, MAX_REQUEST_BYTES, ProviderError,
                                           ProviderRequest, SELECTED_RUNTIME, Stage, canonical)


def request(stage=Stage.DECISION):
    return ProviderRequest(stage, Target.FRONTEND, 'a' * 64, 'trusted system', '{"input":"data"}')


class Socket:
    def __init__(self):
        self.timeouts = []
    def settimeout(self, timeout):
        self.timeouts.append(timeout)
    def shutdown(self, how):
        pass
    def close(self):
        pass


class Response:
    def __init__(self, value=None, *, status=200, raw=None, headers=None):
        self.status = status
        self.raw = canonical(value) if raw is None else raw
        self.headers = headers or {}
    def getheader(self, key, default=None):
        return self.headers.get(key, default)
    def read1(self, maximum):
        chunk, self.raw = self.raw[:maximum], self.raw[maximum:]
        return chunk


def fake_http(monkeypatch, responses):
    connections = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == '127.0.0.1'
            assert 1024 <= port <= 65535
            assert 0 < timeout <= 150
            self.host, self.port = host, port
            self.sock = Socket()
            self.closed = False
            self.calls = []
            connections.append(self)
        def connect(self):
            pass
        def request(self, method, endpoint, body, headers):
            self.calls.append((method, endpoint, json.loads(body), headers))
        def getresponse(self):
            return responses.pop(0)
        def close(self):
            self.closed = True
    monkeypatch.setattr(module.http.client, 'HTTPConnection', Connection)
    return connections


@pytest.mark.parametrize('stage', list(Stage))
def test_local_native_protocol_is_fixed_deterministic_and_bounded(monkeypatch, stage):
    raw = '{"result":"data"}'
    responses = [Response({'prompt': 'rendered prompt'}), Response({'tokens': [1, 2, 3]}),
                 Response({'content': raw, 'truncated': False, 'tokens_predicted': 20})]
    connections = fake_http(monkeypatch, responses)
    monkeypatch.setenv('HTTP_PROXY', 'https://outside.invalid')
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-value')
    assert LocalLlamaProvider(8766).infer(request(stage), 150) == raw
    assert [c.calls[0][1] for c in connections] == ['/apply-template', '/tokenize', '/completion']
    assert all(c.closed and c.port == 8766 for c in connections)
    assert all(c.calls[0][0] == 'POST' for c in connections)
    apply = connections[0].calls[0][2]
    assert apply == {'messages': [{'role': 'system', 'content': 'trusted system'},
                                 {'role': 'user', 'content': '{"input":"data"}'}]}
    settings = connections[2].calls[0][2]
    assert settings == {'prompt': [1, 2, 3], 'n_predict': COMPLETION_TOKENS[stage],
                        'temperature': 0.0, 'top_k': 1, 'top_p': 1.0, 'min_p': 0.0,
                        'seed': 42, 'repeat_penalty': 1.0, 'cache_prompt': False, 'stream': False}
    assert all('Authorization' not in c.calls[0][3] for c in connections)
    assert 'unused-test-value' not in repr([c.calls for c in connections])


@pytest.mark.parametrize('bad', [Response(status=302, raw=b'', headers={'Location': 'https://outside.invalid'}),
                               Response(status=500, raw=b'private error text'),
                               Response(raw=b'x' * (MAX_HTTP_BYTES + 1)),
                               Response(raw=b'{}', headers={'Content-Length': str(MAX_HTTP_BYTES + 1)}),
                               Response(raw=b'{}', headers={'Content-Length': '-1'}),
                               Response(raw=b'{}', headers={'Content-Encoding': 'gzip'}),
                               Response(raw=b'{"a":1,"a":2}'),
                               Response(raw=b'[]'), Response(raw=b'NaN'), Response(raw=b'\xff')])
def test_http_rejection_no_redirects_and_no_raw_error_leak(monkeypatch, bad):
    connections = fake_http(monkeypatch, [bad])
    with pytest.raises(ProviderError) as caught:
        LocalLlamaProvider().infer(request(), 10)
    assert len(connections) == 1
    assert connections[0].closed
    assert 'private error text' not in str(caught.value)
    assert 'outside.invalid' not in str(caught.value)


@pytest.mark.parametrize('tokens', [[], [True], [-1], [2**31], ['1'], [1] * 8000, None])
def test_prompt_token_format_and_context_bound(monkeypatch, tokens):
    connections = fake_http(monkeypatch, [Response({'prompt': 'p'}), Response({'tokens': tokens})])
    with pytest.raises(ProviderError, match='prompt token'):
        LocalLlamaProvider().infer(request(), 10)
    assert len(connections) == 2


@pytest.mark.parametrize('reply', [
    {'content': '{}', 'truncated': True, 'tokens_predicted': 5},
    {'content': '{}', 'tokens_predicted': 5},
    {'content': '{}', 'truncated': False},
    {'content': '{}', 'truncated': False, 'tokens_predicted': 257},
    {'content': '{}', 'truncated': False, 'tokens_predicted': True},
    {'content': '{} trailing', 'truncated': False, 'tokens_predicted': 5},
    {'content': 'x' * 4097, 'truncated': False, 'tokens_predicted': 5},
])
def test_completion_semantics_and_stage_output_budgets(monkeypatch, reply):
    connections = fake_http(monkeypatch, [Response({'prompt': 'p'}), Response({'tokens': [1]}), Response(reply)])
    with pytest.raises(ProviderError):
        LocalLlamaProvider().infer(request(), 10)
    assert len(connections) == 3


@pytest.mark.parametrize('port', [True, 0, 1023, 65536, '8765', 'https://outside.invalid'])
def test_no_arbitrary_provider_url_or_port(port):
    with pytest.raises(ProviderError):
        LocalLlamaProvider(port)


@pytest.mark.parametrize('operation', ['shell', 'network', 'write_file', {'tool': 'shell'}])
def test_adapter_interface_rejects_non_inference_operations(operation):
    with pytest.raises(ProviderError, match='unsupported provider operation'):
        LocalLlamaProvider().infer(operation, 10)


@pytest.mark.parametrize('timeout', [True, 0, -1, 151, float('inf'), float('nan'), '10'])
def test_timeout_host_upper_bound(timeout):
    with pytest.raises(ProviderError, match='timeout'):
        LocalLlamaProvider().infer(request(), timeout)


def test_deadline_is_shared_across_endpoints_and_socket_has_hard_stop(monkeypatch):
    connections = fake_http(monkeypatch, [Response({'prompt': 'p'}), Response({'tokens': [1]})])
    class Clock:
        now = 0
        def monotonic(self):
            self.now += 1
            return self.now
    timers = []
    class Timer:
        def __init__(self, interval, callback):
            self.interval, self.callback = interval, callback
            self.cancelled = False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(module.time, 'monotonic', Clock().monotonic)
    monkeypatch.setattr(module.threading, 'Timer', Timer)
    with pytest.raises(ProviderError):
        LocalLlamaProvider().infer(request(), 6)
    assert len(connections) == 1
    assert timers[0].interval <= 6
    assert timers[0].cancelled
    assert connections[0].closed


def test_pinned_runtime_exactly_matches_p01_without_bootstrap():
    candidate = next(c for c in CANDIDATES if c.id == SELECTED_RUNTIME.id)
    for name in ('id', 'repository', 'revision', 'filename', 'bytes', 'sha256'):
        assert getattr(SELECTED_RUNTIME, name) == getattr(candidate, name)
    assert module.LLAMA_COMMIT == P01_COMMIT
    assert not hasattr(LocalLlamaProvider, 'download')
    assert not hasattr(LocalLlamaProvider, 'start')


def test_request_and_fake_transcript_limits():
    with pytest.raises(ProviderError, match='request byte limit'):
        ProviderRequest(Stage.DECISION, Target.FRONTEND, 'a' * 64, 'x' * MAX_REQUEST_BYTES, '{}')
    with pytest.raises(ProviderError, match='bounded fake transcript'):
        FakeProvider(('{}',) * 7)
    fake = FakeProvider(('{}',))
    assert fake.infer(request(), 10) == '{}'
    with pytest.raises(ProviderError, match='exhausted'):
        fake.infer(request(), 10)
    with pytest.raises(ProviderError, match='unsupported local endpoint'):
        LocalLlamaProvider()._post('/shell', {}, 1)
