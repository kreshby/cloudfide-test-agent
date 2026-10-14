"""B10.5: deterministic host allocation, with no inference server or network.

Fast smoke: backend/.venv/bin/python -m pytest -q automation/tests/test_t07b105_budget.py::test_historical_fourth_call_budget
"""

import json
import socket

import pytest

from automation.test_agent import agent as a
from automation.test_agent.contracts import Target
from automation.test_agent.diagnostics import Diagnostic
from automation.test_agent.provider import MAX_CALL_SECONDS, FakeProvider, ProviderError, Stage, canonical
from automation.tests.t04_fixtures import install_originals
from automation.tests.t07b2_fixtures import live_bundle
from automation.tests.test_agent import make_agent
from automation.tests.test_live_boundaries import full_stack_transcript
from automation.tests.test_t07b103_host_imports import compiler_feedback, live_agent


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('budget regression must not open sockets')
    monkeypatch.setattr(socket, 'socket', forbidden)


def allocations(result):
    return [event for raw in result.trace
            if (event := json.loads(raw))['event'] == 'provider-call']


@pytest.mark.parametrize('budget,fourth_timeout', [(2100, 600), (1800, 359)])
def test_historical_fourth_call_budget(tmp_path, monkeypatch, budget, fourth_timeout):
    # Run both budgets through the actual full-stack run/_ask/_remaining path.
    assert a.MAX_ACTIVE_SECONDS == 2100
    assert a.MAX_CALL_SECONDS == MAX_CALL_SECONDS == 600
    monkeypatch.setattr(a, 'MAX_ACTIVE_SECONDS', budget)
    bundle = live_bundle()
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    clock = Clock()
    agent = a.UnitTestAgent(bundle, tmp_path, provider, clock=clock)
    original = provider.infer
    timeouts = []
    elapsed = (564, 504, 372, 400)

    def timed(request, timeout):
        index = len(timeouts)
        event = allocations(agent.result())[-1]
        assert event['timeout_seconds'] == timeout
        if index == 3:
            assert clock.now == 1441
        timeouts.append(timeout)
        response = original(request, timeout)
        clock.now += elapsed[index]
        if index == 2:
            clock.now += 1  # Approximate host overhead: 1440 s inference + 1 s.
        return response

    provider.infer = timed
    result = agent.run()
    assert timeouts == [600, 600, 600, fourth_timeout]
    assert result.provider_calls == len(provider.requests) == 4
    assert [(r.stage, r.target) for r in provider.requests] == [
        (Stage.DECISION, Target.FRONTEND), (Stage.EDITS, Target.FRONTEND),
        (Stage.DECISION, Target.BACKEND), (Stage.EDITS, Target.BACKEND)]
    events = allocations(result)
    assert [e['remaining_active_seconds'] for e in events] == [
        budget, budget - 564, budget - 1068, budget - 1441]
    assert events[-1]['timeout_seconds'] == fourth_timeout
    if budget == 2100:
        assert result.state == a.State.AWAITING_VERIFICATION, result.failure
        assert result.diagnostic is None
    else:
        # A 400-second fourth reply fits the new allowance but exceeds the old.
        assert result.state == a.State.FAILED
        assert result.failure == 'provider call timeout exceeded'
        assert result.diagnostic == Diagnostic.PROVIDER_TIMEOUT


@pytest.mark.parametrize('consumed,expected', [(0, 600), (1500, 600), (2075, 25)])
def test_allocation_is_capped_and_retains_timeout_diagnostic(tmp_path, consumed, expected):
    clock = Clock()
    _, provider, agent = make_agent(tmp_path, clock=clock)
    agent._active_seconds = consumed
    original = provider.infer

    def fail(request, timeout):
        assert timeout == expected
        assert allocations(agent.result()) == [{
            'event': 'provider-call', 'stage': 'decision', 'target': 'frontend',
            'timeout_seconds': expected, 'remaining_active_seconds': 2100 - consumed}]
        original(request, timeout)
        raise ProviderError('PRIVATE arbitrary error and model text 999999999',
                            diagnostic=Diagnostic.PROVIDER_TIMEOUT)

    provider.infer = fail
    result = agent.run()
    assert result.state == a.State.FAILED
    assert result.failure == 'provider inference failed'
    assert result.diagnostic == Diagnostic.PROVIDER_TIMEOUT
    assert 'PRIVATE' not in result.to_bytes().decode()
    assert '999999999' not in result.to_bytes().decode()
    assert len(allocations(result)) == len(provider.requests) == 1


def test_allocation_trace_is_deterministic_bounded_and_host_owned(tmp_path):
    results = []
    for _ in range(2):
        clock = Clock()
        _, provider, agent = make_agent(tmp_path, clock=clock)
        results.append(agent.run())
        events = allocations(results[-1])
        assert len(events) == len(provider.requests) == 2
        for event, request in zip(events, provider.requests):
            assert set(event) == {'event', 'stage', 'target', 'timeout_seconds',
                                  'remaining_active_seconds'}
            assert event['stage'] == str(request.stage)
            assert event['target'] == str(request.target)
            assert event['timeout_seconds'] == 600
            assert event['remaining_active_seconds'] == 2100
            assert len(canonical(event)) <= 200
            for name, maximum in [('timeout_seconds', 600), ('remaining_active_seconds', 2100)]:
                assert type(event[name]) in (int, float)
                assert 0 < event[name] <= maximum
            assert request.system not in canonical(event).decode()
            assert request.user not in canonical(event).decode()
        assert len(results[-1].trace) <= a.MAX_TRACE_EVENTS
        assert sum(len(e.encode()) for e in results[-1].trace) <= a.MAX_TRACE_BYTES
    assert results[0].to_bytes() == results[1].to_bytes()


def test_repair_call_also_records_remaining_budget(tmp_path):
    _, provider, agent = live_agent(tmp_path)
    clock = Clock()
    agent._clock = clock
    assert agent.run().state == a.State.AWAITING_VERIFICATION
    agent._active_seconds = 2075
    result = agent.repair(compiler_feedback(agent))
    assert result.state == a.State.AWAITING_VERIFICATION, result.failure
    events = allocations(result)
    assert len(events) == len(provider.requests) == 5
    assert events[-1] == {
        'event': 'provider-call', 'stage': 'repair', 'target': 'frontend',
        'timeout_seconds': 25, 'remaining_active_seconds': 25}
