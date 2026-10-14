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
    def isclosed(self):
        return not self.raw
    def read1(self, maximum):
        chunk, self.raw = self.raw[:maximum], self.raw[maximum:]
        return chunk


def fake_http(monkeypatch, responses):
    connections = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == '127.0.0.1'
            assert 1024 <= port <= 65535
            assert 0 < timeout <= module.MAX_CALL_SECONDS
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
                 Response({'content': raw, 'truncated': False, 'tokens_predicted': 20, 'stop_type': 'eos'})]
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
                        'seed': 42, 'repeat_penalty': 1.0, 'cache_prompt': False, 'stream': False,
                        'json_schema': request(stage).response_schema}
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
    {'content': '{}', 'truncated': True, 'tokens_predicted': 5, 'stop_type': 'eos'},
    {'content': '{}', 'tokens_predicted': 5, 'stop_type': 'eos'},
    {'content': '{}', 'truncated': False, 'stop_type': 'eos'},
    {'content': '{}', 'truncated': False, 'tokens_predicted': 257, 'stop_type': 'eos'},
    {'content': '{}', 'truncated': False, 'tokens_predicted': True, 'stop_type': 'eos'},
    {'content': '{} trailing', 'truncated': False, 'tokens_predicted': 5, 'stop_type': 'eos'},
    {'content': 'x' * 4097, 'truncated': False, 'tokens_predicted': 5, 'stop_type': 'eos'},
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


@pytest.mark.parametrize('timeout', [True, 0, -1, 601, float('inf'), float('nan'), '10'])
def test_timeout_host_upper_bound(timeout):
    with pytest.raises(ProviderError, match='timeout'):
        LocalLlamaProvider().infer(request(), timeout)


def test_exact_inference_deadline_upper_bound_is_accepted(monkeypatch):
    assert module.MAX_CALL_SECONDS == 600
    connections = fake_http(monkeypatch, [Response({'prompt': 'p'}), Response({'tokens': [1]}),
        Response({'content': '{}', 'truncated': False, 'tokens_predicted': 1, 'stop_type': 'eos'})])
    assert LocalLlamaProvider().infer(request(), 600) == '{}'
    assert len(connections) == 3
    assert all(0 < t <= module.MAX_CALL_SECONDS for c in connections for t in c.sock.timeouts)


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


def timing_provider(monkeypatch, extra, *, elapsed_ms=1250):
    provider = LocalLlamaProvider()
    completion = {'content': '{"private_generated_content":"fixture"}', 'truncated': False,
                  'tokens_predicted': 20, 'stop_type': 'eos', **extra}
    times = iter((10.0, 10.0 + elapsed_ms / 1000))
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(times))
    operations = []
    def post(endpoint, payload, deadline):
        assert deadline == 610.0
        operations.append(endpoint)
        if endpoint == '/apply-template':
            return {'prompt': 'private_prompt_text'}
        if endpoint == '/tokenize':
            return {'tokens': [1, 2, 3]}
        assert endpoint == '/completion'
        return completion
    monkeypatch.setattr(provider, '_post', post)
    return provider, completion['content'], operations


def test_inference_records_only_valid_allowlisted_numeric_timing(monkeypatch, capsys):
    provider, raw, operations = timing_provider(monkeypatch, {
        'tokens_evaluated': 3,
        'private_server_field': 'Authorization: PRIVATE_AUTH_FIXTURE',
        'timings': {'prompt_n': 2, 'predicted_n': 20, 'prompt_ms': 12.5, 'predicted_ms': 1000,
                    'prompt_per_second': 240.0, 'predicted_per_second': 20.0,
                    'private_timing_field': 'private_repository_text'},
    })
    assert provider.infer(request(), 600) == raw
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        'event': 'inference_timing', 'call_elapsed_ms': 1250.0, 'prompt_tokens': 3,
        'completion_tokens': 20, 'prompt_ms': 12.5, 'generation_ms': 1000,
        'prompt_tokens_per_second': 240.0, 'generation_tokens_per_second': 20.0,
    }
    assert operations == ['/apply-template', '/tokenize', '/completion']
    for private in ('private_prompt_text', 'private_generated_content', 'private_repository_text',
                    'private_server_field', 'private_timing_field', 'PRIVATE_AUTH_FIXTURE'):
        assert private not in output.out + output.err
    assert output.err == ''


@pytest.mark.parametrize('extra', [{}, {'timings': None}, {'timings': 'private_server_text'},
                                 {'timings': []}])
def test_missing_or_malformed_timing_container_does_not_fail_inference(monkeypatch, capsys, extra):
    provider, raw, _ = timing_provider(monkeypatch, extra)
    assert provider.infer(request(), 600) == raw
    assert json.loads(capsys.readouterr().out) == {
        'event': 'inference_timing', 'call_elapsed_ms': 1250.0, 'completion_tokens': 20,
    }


@pytest.mark.parametrize('bad', [None, True, 'PRIVATE_NON_NUMERIC', [], {}, -1,
                               float('inf'), float('-inf'), float('nan'), 10**100])
def test_invalid_optional_timing_values_are_omitted_without_inference_failure(monkeypatch, capsys, bad):
    provider, raw, _ = timing_provider(monkeypatch, {
        'tokens_evaluated': bad,
        'timings': {key: bad for key in ('prompt_n', 'prompt_ms', 'predicted_ms',
                                        'prompt_per_second', 'predicted_per_second')},
    })
    assert provider.infer(request(), 600) == raw
    assert json.loads(capsys.readouterr().out) == {
        'event': 'inference_timing', 'call_elapsed_ms': 1250.0, 'completion_tokens': 20,
    }


@pytest.mark.parametrize('offset', [0, 1])
def test_optional_timing_upper_bounds_and_prompt_count_fallback(monkeypatch, capsys, offset):
    provider, raw, _ = timing_provider(monkeypatch, {
        'timings': {'prompt_n': module.CONTEXT_TOKENS + offset,
                    'prompt_ms': 600_000 + offset, 'predicted_ms': 600_000 + offset,
                    'prompt_per_second': 1_000_000 + offset, 'predicted_per_second': 1_000_000 + offset},
    })
    assert provider.infer(request(), 600) == raw
    metrics = json.loads(capsys.readouterr().out)
    expected = {'event': 'inference_timing', 'call_elapsed_ms': 1250.0, 'completion_tokens': 20}
    if offset == 0:
        expected.update(prompt_tokens=8192, prompt_ms=600_000, generation_ms=600_000,
                        prompt_tokens_per_second=1_000_000, generation_tokens_per_second=1_000_000)
    assert metrics == expected


def test_fractional_token_count_ignored_but_valid_other_metrics_survive(monkeypatch, capsys):
    provider, raw, _ = timing_provider(monkeypatch, {
        'tokens_evaluated': 3.0,
        'timings': {'prompt_n': 2.5, 'prompt_ms': 0.0, 'predicted_ms': 1},
    })
    assert provider.infer(request(), 600) == raw
    assert json.loads(capsys.readouterr().out) == {
        'event': 'inference_timing', 'call_elapsed_ms': 1250.0, 'completion_tokens': 20,
        'prompt_ms': 0.0, 'generation_ms': 1,
    }


def test_failed_inference_records_host_elapsed_without_exception_text_or_retry(monkeypatch, capsys):
    from automation.test_agent.diagnostics import Diagnostic
    times = iter((10.0, 610.1))
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(times))
    provider = LocalLlamaProvider()
    calls = []
    def fail(endpoint, *_):
        calls.append(endpoint)
        raise ProviderError('PRIVATE_EXCEPTION_TEXT', diagnostic=Diagnostic.PROVIDER_TIMEOUT)
    monkeypatch.setattr(provider, '_post', fail)
    with pytest.raises(ProviderError) as caught:
        provider.infer(request(), 600)
    assert caught.value.diagnostic == Diagnostic.PROVIDER_TIMEOUT
    assert calls == ['/apply-template']
    output = capsys.readouterr()
    assert json.loads(output.out) == {'event': 'inference_timing', 'call_elapsed_ms': 600100.0}
    assert 'PRIVATE_EXCEPTION_TEXT' not in output.out + output.err


@pytest.mark.parametrize('elapsed', [1_800_000, 1_800_001, -1, float('inf'), float('nan')])
def test_host_elapsed_is_bounded_optional_evidence(monkeypatch, capsys, elapsed):
    provider, raw, _ = timing_provider(monkeypatch, {}, elapsed_ms=elapsed)
    assert provider.infer(request(), 600) == raw
    metrics = json.loads(capsys.readouterr().out)
    assert metrics.get('call_elapsed_ms') == (elapsed if elapsed == 1_800_000 else None)
    assert metrics['completion_tokens'] == 20


def test_optional_logging_failure_does_not_break_valid_inference(monkeypatch):
    provider, raw, _ = timing_provider(monkeypatch, {})
    def fail(*_):
        raise OSError('PRIVATE_OUTPUT_FAILURE')
    monkeypatch.setattr(module, 'print', fail, raising=False)
    assert provider.infer(request(), 600) == raw
