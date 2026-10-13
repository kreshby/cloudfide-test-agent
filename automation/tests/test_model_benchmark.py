"""Deterministic fake responses only; no model downloads or generated-code execution."""

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path
import re
import signal
import time

import pytest

from automation.benchmarks import model_benchmark as runtime
from automation.benchmarks.scenarios import SCENARIOS, SYSTEM_PROMPT, messages
from automation.benchmarks.scoring import Inference, MAX_RESPONSE_BYTES, aggregate, score
from automation.test_agent.contracts import ContractError, ModelProposal


CONTENTS = {
    "button-trim": '''import { it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SubmitButton } from './SubmitButton';
it('disables whitespace and enables a real name', () => {
  const { rerender } = render(<SubmitButton name="   " onSubmit={vi.fn()} />);
  expect(screen.getByRole('button', {name: 'Save'})).toBeDisabled();
  rerender(<SubmitButton name="Alice" onSubmit={vi.fn()} />);
  expect(screen.getByRole('button', {name: 'Save'})).toBeEnabled();
});
''',
    "zero-limit-422": '''from fastapi.testclient import TestClient
from app.tasks import app
client = TestClient(app)

def test_zero_limit():
    response = client.get('/tasks?limit=0')
    assert response.status_code == 422
''',
    "greeting-ada": '''from fastapi.testclient import TestClient
from app.greeting import app
client = TestClient(app)

def test_greeting():
    response = client.get('/greeting?name=Ada')
    assert response.status_code == 200
    assert response.json() == {'message': 'Hello Ada'}
''',
    "count-zero": '''import { it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TaskCount } from './TaskCount';
it('hides zero and displays positive counts', () => {
  const { rerender } = render(<TaskCount count={0} />);
  expect(screen.queryByText('0 tasks')).toBeNull();
  rerender(<TaskCount count={2} />);
  expect(screen.getByText('2 tasks')).toBeInTheDocument();
});
''',
}


def perfect(scenario):
    decisions, edits = [], []
    for expected in scenario.expected:
        decisions.append({"target": expected.target, "action": expected.action, "plan": {
            "changed_behavior": "negative values clamp to zero; health returns ok; changed behavior",
            "rationale": "negative zero and health ok evidence; cover uncovered behavior",
            "existing_test_evidence": [p for p, _ in scenario.existing_tests],
            "intended_test_files": [expected.path] if expected.path else [], "skill_ids": [],
        }})
        if expected.path:
            originals = dict(scenario.existing_tests)
            edits.append({"target": expected.target, "path": expected.path, "operation": expected.operation,
                          "original_sha256": hashlib.sha256(originals[expected.path].encode()).hexdigest()
                          if expected.operation == "update" else None, "content": CONTENTS[expected.behavior]})
    return {"schema_version": 1, "source": asdict(scenario.source), "decisions": decisions, "edits": edits}


def scored(scenario, payload):
    return score(scenario, Inference(json.dumps(payload)))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_perfect_proposals_pass_real_t02_and_all_gates(scenario):
    case = scored(scenario, perfect(scenario))
    assert case["score"] == 10 and case["full_quality"]
    assert case["policy_rejection_reason"] is None and all(case["gates"].values())


def test_wrong_decision_loses_points_even_if_policy_accepts():
    proposal = perfect(SCENARIOS[0])
    proposal["decisions"][0]["action"] = "unsupported"
    proposal["decisions"][0]["plan"]["intended_test_files"] = []
    proposal["edits"] = []
    case = scored(SCENARIOS[0], proposal)
    assert case["gates"]["policy_accepted"]
    assert not case["gates"]["correct_decisions"] and case["score"] < 10


def test_blind_generation_on_covered_behavior_loses_points():
    scenario = SCENARIOS[2]
    proposal = perfect(scenario)
    proposal["decisions"][0]["action"] = "generate"
    path, content = scenario.existing_tests[0]
    proposal["decisions"][0]["plan"]["intended_test_files"] = [path]
    proposal["edits"] = [{"target": "frontend", "path": path, "operation": "create",
                          "original_sha256": None, "content": content}]
    case = scored(scenario, proposal)
    assert not case["gates"]["correct_decisions"]
    assert not case["gates"]["policy_accepted"] and not case["gates"]["behavior_evidence"]


@pytest.mark.parametrize("raw", ["invalid", "```json\n{}\n```", "{} trailing", "[]", '{"x":1,"x":2}',
                                 '{"x": NaN}', "[" * 2000 + "]" * 2000])
def test_raw_json_is_strict_and_never_repaired(raw):
    case = score(SCENARIOS[0], Inference(raw))
    assert not case["gates"]["raw_json_valid"] and not case["gates"]["contract_valid"]


@pytest.fixture(params=["nested-edits", "nested-edits-only", "union-target"])
def malformed_shape(request):
    proposal = perfect(SCENARIOS[0])
    if request.param == "union-target":
        proposal["decisions"][0]["target"] = "frontend|backend"
        rejection = "target: unsupported value"
    else:
        proposal["decisions"][0]["edits"] = proposal.pop("edits")
        if request.param == "nested-edits":
            proposal["edits"] = []
            rejection = "decision: expected exactly"
        else:
            rejection = "proposal: expected exactly"
    return json.dumps(proposal), rejection


def test_hosted_failure_shapes_remain_rejected_by_real_t02(malformed_shape):
    raw, rejection = malformed_shape
    with pytest.raises(ContractError, match=rejection):
        ModelProposal.from_payload(raw)
    case = score(SCENARIOS[0], Inference(raw))
    assert case["raw_response"] == raw and case["gates"]["raw_json_valid"]
    assert not case["gates"]["contract_valid"] and not case["gates"]["policy_accepted"]
    assert case["contract_rejection_reason"].startswith(rejection)


def test_common_prompt_has_unambiguous_enums_nesting_and_decision_cardinality():
    assert '"target":"frontend|backend"' not in SYSTEM_PROMPT
    # Covers spaces/newlines around the colon and unions in every enum field.
    assert not re.search(r'"(?:target|action|operation)"\s*:\s*"[^"\n]*\|[^"\n]*"', SYSTEM_PROMPT)
    assert 'target must be exactly one of:\n- "frontend"\n- "backend"' in SYSTEM_PROMPT
    assert 'action must be exactly one of:\n- "generate"\n- "update"\n- "no-tests-needed"\n- "unsupported"' in SYSTEM_PROMPT
    assert 'operation must be exactly one of:\n- "create"\n- "update"' in SYSTEM_PROMPT
    assert '"edits" is a TOP-LEVEL sibling of "decisions"' in SYSTEM_PROMPT
    assert 'NEVER place "edits" inside a decision' in SYSTEM_PROMPT
    assert 'NEVER output literal "frontend|backend"' in SYSTEM_PROMPT
    assert 'exactly one decision for each target represented by the changed files' in SYSTEM_PROMPT
    assert 'Do not invent a decision for a target absent from the changed files' in SYSTEM_PROMPT
    assert 'Mixed frontend/backend context requires two distinct decision objects' in SYSTEM_PROMPT


def test_prompt_shape_example_is_valid_and_contains_no_scenario_answer():
    start = SYSTEM_PROMPT.index('\n{\n') + 1
    example, end = json.JSONDecoder().raw_decode(SYSTEM_PROMPT[start:])
    assert SYSTEM_PROMPT[start + end:].startswith('\nEnd SHAPE example.')
    assert 'nesting only; dummy values, not a suggested decision' in SYSTEM_PROMPT[:start]
    assert set(example) == {"schema_version", "source", "decisions", "edits"}
    assert example["edits"] == [] and len(example["decisions"]) == 1
    assert set(example["decisions"][0]) == {"target", "action", "plan"}
    ModelProposal.from_payload(json.dumps(example))
    shape_text = SYSTEM_PROMPT[start:start + end]
    for scenario in SCENARIOS:
        paths = {p for p, _, _ in scenario.sources} | {p for p, _ in scenario.existing_tests}
        paths |= set(scenario.candidates)
        paths |= {e.path for e in scenario.expected if e.path}
        paths |= {e.evidence_path for e in scenario.expected if e.evidence_path}
        assert all(path not in SYSTEM_PROMPT for path in paths)
        assert all(e.behavior not in shape_text for e in scenario.expected)
        assert example["source"] != asdict(scenario.source)
        assert scenario.source.repository not in shape_text and scenario.source.run_id not in shape_text


@pytest.mark.parametrize("location", ["top", "source", "decision", "plan", "edit"])
def test_forbidden_trusted_fields_rejected_at_every_level(location):
    proposal = perfect(SCENARIOS[0])
    target = {"top": proposal, "source": proposal["source"], "decision": proposal["decisions"][0],
              "plan": proposal["decisions"][0]["plan"], "edit": proposal["edits"][0]}[location]
    target["status"] = "verified"
    case = scored(SCENARIOS[0], proposal)
    assert case["gates"]["raw_json_valid"]
    assert not case["gates"]["no_forbidden_fields"] and not case["gates"]["contract_valid"]


def test_policy_rejection_does_not_become_contract_failure():
    proposal = perfect(SCENARIOS[0])
    proposal["edits"][0]["content"] = proposal["edits"][0]["content"].replace("it(", "it.only(")
    case = scored(SCENARIOS[0], proposal)
    assert case["gates"]["contract_valid"] and not case["gates"]["policy_accepted"]
    assert case["policy_rejection_reason"] == "new skip/only/xfail mechanism"


def test_wrong_path_loses_path_and_policy_gates():
    proposal = perfect(SCENARIOS[0])
    proposal["edits"][0]["path"] = "frontend/src/unrelated.test.tsx"
    proposal["decisions"][0]["plan"]["intended_test_files"] = ["frontend/src/unrelated.test.tsx"]
    case = scored(SCENARIOS[0], proposal)
    assert case["gates"]["contract_valid"]
    assert not case["gates"]["expected_paths"] and not case["gates"]["policy_accepted"]


def test_update_requires_update_and_exact_original_hash():
    scenario = SCENARIOS[1]
    proposal = perfect(scenario)
    proposal["edits"][0].update(operation="create", original_sha256=None)
    case = scored(scenario, proposal)
    assert not case["gates"]["correct_operations"] and not case["gates"]["policy_accepted"]
    proposal = perfect(scenario)
    proposal["edits"][0]["original_sha256"] = "0" * 64
    case = scored(scenario, proposal)
    assert case["gates"]["contract_valid"] and not case["gates"]["policy_accepted"]
    assert "SHA-256" in case["policy_rejection_reason"]


def test_mixed_stack_partial_answer_loses_decision_and_quality():
    scenario = SCENARIOS[4]
    proposal = perfect(scenario)
    proposal["decisions"].pop()
    case = scored(scenario, proposal)
    assert case["gates"]["policy_accepted"]
    assert not case["gates"]["correct_decisions"] and not case["full_quality"]
    assert not case["targets"][1]["decision_matched"]


@pytest.mark.parametrize("scenario", [SCENARIOS[0], SCENARIOS[1], SCENARIOS[3], SCENARIOS[4]], ids=lambda s: s.id)
def test_tautologies_cannot_get_full_quality(scenario):
    proposal = perfect(scenario)
    proposal["edits"][0]["content"] = ("it('unrelated', () => expect(1).toBe(1));\n" if
                                         scenario.expected[0].target == "frontend" else
                                         "def test_unrelated():\n    assert 1 == 1\n")
    case = scored(scenario, proposal)
    assert case["gates"]["policy_accepted"]  # T02 is not a semantic verifier.
    assert not case["gates"]["substantive_tests"] and not case["gates"]["behavior_evidence"]


def test_old_behavior_has_substantive_test_but_loses_behavior_score():
    proposal = perfect(SCENARIOS[1])
    proposal["edits"][0]["content"] = proposal["edits"][0]["content"].replace("== 422", "== 200")
    case = scored(SCENARIOS[1], proposal)
    assert case["gates"]["policy_accepted"] and case["gates"]["substantive_tests"]
    assert not case["gates"]["behavior_evidence"]


def test_behavior_markers_in_comments_or_docstrings_do_not_suffice():
    for scenario in (SCENARIOS[0], SCENARIOS[1]):
        proposal = perfect(scenario)
        content = proposal["edits"][0]["content"]
        proposal["edits"][0]["content"] = ("/*" + content + "*/\nit('x',()=>expect(1).toBe(1));" if
                                             scenario.expected[0].target == "frontend" else
                                             '"""' + content + '"""\ndef test_x():\n    assert True\n')
        assert not scored(scenario, proposal)["gates"]["behavior_evidence"]


def test_no_tests_needed_requires_reference_to_real_coverage():
    proposal = perfect(SCENARIOS[2])
    proposal["decisions"][0]["plan"]["existing_test_evidence"] = []
    case = scored(SCENARIOS[2], proposal)
    assert case["gates"]["correct_decisions"] and not case["gates"]["behavior_evidence"]


@pytest.mark.parametrize("raw", ["x" * (MAX_RESPONSE_BYTES + 1), "😀" * MAX_RESPONSE_BYTES])
def test_oversized_response_is_bounded_and_cannot_pass(raw):
    case = score(SCENARIOS[0], Inference(raw))
    assert case["failure"] == "response-byte-limit" and case["response_truncated"]
    assert len(case["raw_response"].encode()) <= MAX_RESPONSE_BYTES and case["score"] == 0


def test_aggregation_preserves_cases_is_deterministic_and_selects_no_winner():
    cases = [scored(s, perfect(s)) for s in SCENARIOS]
    cases[0] = score(SCENARIOS[0], Inference(failure="inference-timeout", wall_seconds=runtime.INFERENCE_SECONDS))
    result = aggregate(cases)
    assert result == aggregate(list(reversed(cases)))
    assert result["cases"] == 5 and result["maximum_score"] == 50
    assert result["score"] == 40 and result["failures"] == 1 and result["raw_json_valid_rate"] == 0.8
    assert "winner" not in result and "selected_model" not in result


class FakeProvider:
    instances = []
    fail_start = False
    fail_infer = False

    def __init__(self, executable, model, root, deadline, metrics):
        self.closed = False
        self.calls = []
        self.metrics = metrics
        self.instances.append(self)

    def start(self):
        if self.fail_start:
            raise runtime.BenchmarkFailure("fake-startup-failure")

    def infer(self, scenario, metadata):
        self.calls.append(scenario.id)
        if self.fail_infer:
            return Inference(failure="inference-timeout", wall_seconds=runtime.INFERENCE_SECONDS)
        return Inference(json.dumps(perfect(scenario)), wall_seconds=1, completion_tokens=100, tokens_per_second=100)

    def close(self):
        self.closed = True


def fake_acquire(candidate, directory, deadline, metrics):
    data = directory / "fake-model-data"
    data.write_bytes(b"deterministic fake data, not a model")
    metrics.update(downloaded_bytes=data.stat().st_size, model_acquisition_seconds=0)
    return data


@pytest.fixture
def fake_provider(monkeypatch):
    monkeypatch.setattr(FakeProvider, "instances", [])
    monkeypatch.setattr(FakeProvider, "fail_start", False)
    monkeypatch.setattr(FakeProvider, "fail_infer", False)
    return FakeProvider


def test_fake_candidate_success_and_cleanup(tmp_path, fake_provider):
    result = runtime.run_candidate(runtime.CANDIDATES[0], Path('/fixed/executable'), tmp_path,
                                   time.monotonic() + 10, fake_acquire, fake_provider)
    assert result["availability"] == "completed" and result["failure"] is None
    assert result["aggregate"]["score"] == 50 and fake_provider.instances[0].closed
    assert not list(tmp_path.iterdir())
    metrics = result["resource_metrics"]
    assert metrics["model_cache_bytes_before_cleanup"] > 0 and metrics["model_cache_bytes_after_cleanup"] == 0
    assert metrics["runner_disk_free_after_cleanup"] > 0


@pytest.mark.parametrize("stage", ["acquire", "start", "infer"])
def test_failures_remain_failures_with_five_cases_and_cleanup(tmp_path, fake_provider, monkeypatch, stage):
    def failing_acquire(*args):
        raise runtime.BenchmarkFailure("fake-download-unavailable")
    if stage == "start":
        monkeypatch.setattr(fake_provider, "fail_start", True)
    if stage == "infer":
        monkeypatch.setattr(fake_provider, "fail_infer", True)
    result = runtime.run_candidate(runtime.CANDIDATES[0], Path('/fixed/executable'), tmp_path,
                                   time.monotonic() + 10, failing_acquire if stage == "acquire" else fake_acquire,
                                   fake_provider)
    assert result["availability"] == "failed" and result["failure"]
    assert len(result["scenarios"]) == 5 and result["aggregate"]["score"] == 0
    assert not list(tmp_path.iterdir())
    if stage != "acquire":
        assert fake_provider.instances[0].closed
    if stage == "infer":
        assert len(fake_provider.instances[0].calls) == 1 and result["failure"] == "inference-timeout"


def test_sequential_cleanup_precedes_next_acquisition(tmp_path, fake_provider):
    order = []
    def acquire(candidate, directory, deadline, metrics):
        assert set(p.name for p in tmp_path.iterdir()) == {candidate.id}
        if fake_provider.instances:
            assert fake_provider.instances[-1].closed
        order.append(candidate.id)
        return fake_acquire(candidate, directory, deadline, metrics)
    for candidate in runtime.CANDIDATES:
        runtime.run_candidate(candidate, Path('/fixed/executable'), tmp_path,
                              time.monotonic() + 10, acquire, fake_provider)
    assert order == [c.id for c in runtime.CANDIDATES]


def test_raw_response_cannot_execute_or_write_proposed_code(tmp_path, monkeypatch):
    marker = tmp_path / "should-not-exist"
    proposal = perfect(SCENARIOS[1])
    proposal["edits"][0]["content"] += f"\n__import__('pathlib').Path({str(marker)!r}).touch()\n"
    def forbidden(*args, **kwargs):
        raise AssertionError("attempted model-controlled execution")
    with monkeypatch.context() as guard:
        guard.setattr(runtime.subprocess, "Popen", forbidden)
        guard.setattr("builtins.eval", forbidden)
        guard.setattr("builtins.exec", forbidden)
        case = scored(SCENARIOS[1], proposal)
    assert case["gates"]["policy_accepted"] and not marker.exists() and not list(tmp_path.iterdir())


def test_candidate_and_model_cannot_override_host_limits(tmp_path):
    candidate = replace(runtime.CANDIDATES[0], filename='../../escaped', bytes=1)
    with pytest.raises(runtime.BenchmarkFailure, match="unapproved-candidate"):
        runtime.run_candidate(candidate, Path('/fixed/executable'), tmp_path, time.monotonic() + 1)
    proposal = perfect(SCENARIOS[0])
    proposal["limits"] = {"inference_seconds": 999999, "threads": 999, "network": True}
    assert not scored(SCENARIOS[0], proposal)["gates"]["contract_valid"]
    assert runtime.INFERENCE_SECONDS == 90 and runtime.THREADS == 4


@pytest.mark.parametrize('field,value', [
    ('benchmark_revision', 'model-controlled-revision'),
    ('configuration', {'completion_tokens': 999999, 'inference_seconds': 999999}),
    ('system_prompt_sha256', '0' * 64),
])
def test_model_cannot_supply_benchmark_metadata(field, value):
    proposal = perfect(SCENARIOS[0])
    proposal[field] = value
    with pytest.raises(ContractError, match='proposal: expected exactly'):
        ModelProposal.from_payload(json.dumps(proposal))
    assert not scored(SCENARIOS[0], proposal)['gates']['contract_valid']


def test_exact_candidate_set_pins_and_equivalent_configuration():
    assert runtime.CANDIDATES == (
        runtime.Candidate('qwen3-4b-q4km', 'Qwen/Qwen3-4B-GGUF', 'bc640142c66e1fdd12af0bd68f40445458f3869b',
                          'Qwen3-4B-Q4_K_M.gguf', 2497280256,
                          '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'),
        runtime.Candidate('qwen3-8b-q4km', 'Qwen/Qwen3-8B-GGUF', '7c41481f57cb95916b40956ab2f0b139b296d974',
                          'Qwen3-8B-Q4_K_M.gguf', 5027783488,
                          'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'),
        runtime.Candidate('qwen25-coder-7b-q4km', 'Qwen/Qwen2.5-Coder-7B-Instruct-GGUF',
                          '13fb94bfda8c8cf22497dc57b78f391a9acb426a', 'qwen2.5-coder-7b-instruct-q4_k_m.gguf',
                          4683073536, '509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c'),
    )
    assert runtime.LLAMA_TAG == 'v0.6.0' and runtime.LLAMA_COMMIT == 'd81235049384534c167caea52b85a694f6103d14'
    assert runtime.THREADS == 4 and runtime.CONTEXT_TOKENS == 8192 and runtime.SEED == 42
    for candidate in runtime.CANDIDATES:
        assert candidate.quantization == 'Q4_K_M'
        assert len(candidate.revision) == 40 and len(candidate.sha256) == 64
        args = runtime.server_argv(Path('/fixed/llama-server'), Path('/fixed/model'))
        assert args[args.index('--threads') + 1] == '4'
        assert args[args.index('--n-gpu-layers') + 1] == '0'
        assert args[args.index('--reasoning') + 1] == 'off'
        assert '--offline' in args and '--tools' not in args
        assert not any(a.startswith(('--grammar', '--json-schema')) for a in args)
    prompts = [messages(s) for s in SCENARIOS]
    assert len({p[0]['content'] for p in prompts}) == 1
    assert all('expected' not in p[1]['content'] for p in prompts)


def test_r2_preserves_complete_scenarios_expectations_and_scoring():
    # Golden snapshots from Run 1 source 2d7144827b3401a6ed92b722f80ce0baee3eb45e.
    snapshot = json.dumps([asdict(s) for s in SCENARIOS], sort_keys=True, separators=(',', ':')).encode()
    assert hashlib.sha256(snapshot).hexdigest() == 'd7a2395d659f19c7c21fc0df56988781c999306d6a5ab4a4398eeaa0e1400b53'
    scoring_source = Path('automation/benchmarks/scoring.py').read_bytes()
    assert hashlib.sha256(scoring_source).hexdigest() == '563c29cc12c470eefb6ea90ba5dfd8b945a58018380fd4635be3435e01d82e27'
    assert len(SCENARIOS) == 5
    cases = [scored(s, perfect(s)) for s in SCENARIOS]
    assert all(case['maximum_score'] == 10 for case in cases)
    assert aggregate(cases)['maximum_score'] == 50


def test_subprocess_environment_has_no_keys_tools_or_proxies(tmp_path, monkeypatch):
    monkeypatch.setenv('LLAMA_ARG_TOOLS', 'exec_shell_command')
    monkeypatch.setenv('GITHUB_TOKEN', 'sanitized-placeholder')
    monkeypatch.setenv('HTTPS_PROXY', 'https://untrusted.invalid')
    environment = runtime.fixed_environment(tmp_path)
    assert 'LLAMA_ARG_TOOLS' not in environment and 'GITHUB_TOKEN' not in environment
    assert 'HTTPS_PROXY' not in environment


@pytest.mark.parametrize('url', ['https://attacker.invalid/x', 'http://huggingface.co/x',
                               'https://huggingface.co.attacker.invalid/x', 'https://user@huggingface.co/x',
                               'https://huggingface.co:8443/x', 'file:///etc/passwd'])
def test_download_redirect_destinations_are_bounded(url):
    with pytest.raises(runtime.BenchmarkFailure, match='destination-rejected'):
        runtime._download_location(url)


def test_only_fixed_local_endpoints_allowed():
    with pytest.raises(runtime.BenchmarkFailure, match='endpoint-rejected'):
        runtime._local_json('https://evil.invalid', {}, time.monotonic() + 1)


def test_local_execution_refuses_model_downloads(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with pytest.raises(runtime.BenchmarkFailure, match='requires-ubuntu24'):
        runtime.main()


def test_total_budget_is_enforced():
    with pytest.raises(runtime.BenchmarkFailure, match='time-budget-exhausted'):
        runtime.remaining(time.monotonic() - 1, 999)
    assert runtime.remaining(time.monotonic() + 100, 5) <= 5


def test_r2_section9_fallback_fits_all_phase_bounds_and_workflow():
    assert runtime.OUTPUT_TOKENS == 512 and runtime.INFERENCE_SECONDS == 90
    assert runtime.SETUP_SECONDS == 420 and runtime.ACQUISITION_SECONDS == 180 and runtime.STARTUP_SECONDS == 90
    workflow = Path('.github/workflows/model-benchmark.yml').read_text()
    workflow_seconds = int(re.search(r'timeout-minutes: (\d+)', workflow).group(1)) * 60
    assert workflow_seconds == 2700
    assert len(runtime.CANDIDATES) * len(SCENARIOS) * 120 == 1800
    requested = runtime.SETUP_SECONDS + len(runtime.CANDIDATES) * (
        runtime.ACQUISITION_SECONDS + runtime.STARTUP_SECONDS + len(SCENARIOS) * 120)
    assert requested == 3030 and requested > workflow_seconds
    phases = runtime.SETUP_SECONDS + len(runtime.CANDIDATES) * (
        runtime.ACQUISITION_SECONDS + runtime.STARTUP_SECONDS + len(SCENARIOS) * runtime.INFERENCE_SECONDS)
    assert phases == 2580 and runtime.TOTAL_SECONDS == 2640
    assert runtime.TOTAL_SECONDS - phases == 60 and workflow_seconds - runtime.TOTAL_SECONDS == 60
    assert runtime.OUTPUT_TOKENS / 8.5 < runtime.INFERENCE_SECONDS - 29  # Reported Run 1 rate, not measured here.


def test_evidence_truncation_preserves_scores(tmp_path, monkeypatch):
    cases = [scored(s, perfect(s)) for s in SCENARIOS]
    for case in cases:
        case['raw_response'] = '\x01' * MAX_RESPONSE_BYTES
    candidate = {**asdict(runtime.CANDIDATES[0]), 'availability': 'completed', 'failure': None,
                 'resource_metrics': {}, 'scenarios': cases, 'aggregate': aggregate(cases)}
    results = {'candidates': [candidate], 'setup_failure': None}
    original = deepcopy(results)
    monkeypatch.setattr(runtime, 'EVIDENCE_BYTES', 24 * 1024)
    runtime.write_evidence(results, tmp_path)
    assert results == original and sum(p.stat().st_size for p in tmp_path.iterdir()) <= 24 * 1024
    evidence = json.loads((tmp_path / 'benchmark-results.json').read_text())
    assert evidence['evidence_truncated'] and evidence['candidates'][0]['aggregate']['score'] == 50
    assert 'winner' not in evidence


def test_evidence_allowlist_rejects_extra_files_or_symlinks(tmp_path):
    for name in runtime.EVIDENCE_FILES:
        (tmp_path / name).write_text('{}')
    runtime.check_evidence(tmp_path)
    extra = tmp_path / 'model.gguf'
    extra.write_bytes(b'fake')
    with pytest.raises(runtime.BenchmarkFailure, match='unexpected-evidence-files'):
        runtime.check_evidence(tmp_path)
    extra.unlink()
    first = tmp_path / runtime.EVIDENCE_FILES[0]
    first.unlink()
    first.symlink_to(tmp_path / runtime.EVIDENCE_FILES[1])
    with pytest.raises(runtime.BenchmarkFailure, match='unsafe-evidence-file'):
        runtime.check_evidence(tmp_path)


def test_process_caps_capture_and_terminates_session(tmp_path, monkeypatch):
    # Fake Popen: no executable is launched.
    class FakeProcess:
        pid = 999999999
        returncode = None
        stdout = io.BytesIO(b'x' * (runtime.CAPTURE_BYTES + 20))
        stderr = io.BytesIO(b'y' * (runtime.CAPTURE_BYTES + 30))
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            self.returncode = -15
            return self.returncode
    calls, signals = [], []
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()
    monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
    monkeypatch.setattr(runtime.os, 'killpg', lambda pid, value: signals.append((pid, value)))
    process = runtime.Process(['/fixed/llama-server', '--version'], tmp_path)
    process.stop()
    assert len(process.stdout) == len(process.stderr) == runtime.CAPTURE_BYTES
    assert process.truncated == {'stdout': True, 'stderr': True}
    assert calls[0][1]['start_new_session'] is True and calls[0][1].get('shell', False) is False
    assert signals == [(FakeProcess.pid, signal.SIGTERM), (FakeProcess.pid, signal.SIGKILL)]


def test_workflow_is_manual_only_read_only_cpu_and_ci_is_separate():
    text = Path('.github/workflows/model-benchmark.yml').read_text()
    triggers = re.search(r'^on:\n(.*?)(?=^\S)', text, flags=re.M | re.S).group(1)
    assert triggers.strip() == 'workflow_dispatch:'  # No inputs or additional events.
    assert 'workflow_dispatch:' in text and 'push:' not in text
    assert 'runs-on: ubuntu-24.04' in text and 'timeout-minutes: 45' in text
    assert 'persist-credentials: false' in text and 'contents: read' in text and 'retention-days: 1' in text
    assert 'pull_request:' not in text and 'schedule:' not in text
    assert re.findall(r'^\s+uses:\s*(\S+)', text, flags=re.M) == [
        'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9',
    ]
    assert 'github.event.repository.private' in text and 'cache@' not in text and 'matrix:' not in text
    assert 'p01-evidence/benchmark-results.json' in text and 'p01-evidence/benchmark-summary.md' in text
    assert 'automation.benchmarks' not in Path('.github/workflows/ci.yml').read_text()


class FakeSocket:
    def settimeout(self, value):
        assert 0 < value <= runtime.INFERENCE_SECONDS


class FakeResponse:
    def __init__(self, body=b'{}', status=200, headers=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, maximum):
        assert maximum <= max(runtime.HTTP_BYTES + 1, 1024 * 1024)
        return self.body.read(maximum)


def install_fake_connections(monkeypatch, responses, secure):
    calls = []
    class Connection:
        def __init__(self, host, *args, **kwargs):
            self.sock = FakeSocket()
            self.response = responses.pop(0)
            calls.append(('connect', host, args, kwargs))

        def request(self, *args, **kwargs):
            calls.append(('request', args, kwargs))

        def getresponse(self):
            self.sock = None  # Exercise Connection: close detachment.
            return self.response

        def close(self):
            calls.append(('close',))
    monkeypatch.setattr(runtime.http.client, 'HTTPSConnection' if secure else 'HTTPConnection', Connection)
    return calls


def test_mock_download_verifies_fixed_revision_size_digest_and_close(tmp_path, monkeypatch):
    content = b'fake bytes only'
    candidate = replace(runtime.CANDIDATES[0], bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(runtime, 'CANDIDATES', (candidate,))  # Trusted test configuration, not model input.
    calls = install_fake_connections(monkeypatch, [FakeResponse(content, headers={'Content-Length': str(len(content))})], True)
    metrics = {}
    path = runtime.acquire_model(candidate, tmp_path, time.monotonic() + 1, metrics)
    assert path.read_bytes() == content and metrics['download_sha256'] == candidate.sha256
    assert metrics['downloaded_bytes'] == len(content)
    assert calls[0][1] == 'huggingface.co'
    assert candidate.revision in calls[1][1][1]
    assert 'Authorization' not in calls[1][2]['headers'] and calls[-1] == ('close',)


@pytest.mark.parametrize('mutation', ['hash', 'size', 'redirect', 'http', 'overflow'])
def test_mock_download_failure_is_bounded_without_network(tmp_path, monkeypatch, mutation):
    candidate = replace(runtime.CANDIDATES[0], bytes=3, sha256=hashlib.sha256(b'abc').hexdigest())
    monkeypatch.setattr(runtime, 'CANDIDATES', (candidate,))
    responses = {
        'hash': FakeResponse(b'xyz'),
        'size': FakeResponse(b'abc', headers={'Content-Length': '999999999'}),
        'redirect': FakeResponse(status=302, headers={'Location': 'https://evil.invalid/model'}),
        'http': FakeResponse(status=404),
        'overflow': FakeResponse(b'abcd'),
    }
    calls = install_fake_connections(monkeypatch, [responses[mutation]], True)
    metrics = {}
    with pytest.raises(runtime.BenchmarkFailure):
        runtime.acquire_model(candidate, tmp_path, time.monotonic() + 1, metrics)
    assert metrics['model_acquisition_seconds'] >= 0
    assert len([c for c in calls if c[0] == 'connect']) == 1 and calls[-1] == ('close',)
    assert all(p.stat().st_size <= 3 for p in tmp_path.iterdir())


def test_mock_local_http_is_loopback_bounded_and_closed(monkeypatch):
    calls = install_fake_connections(monkeypatch, [FakeResponse(b'{"content":"raw model text"}')], False)
    result = runtime._local_json('/completion', {'prompt': [1, 2]}, time.monotonic() + 1)
    assert result['content'] == 'raw model text'
    assert calls[0][1:3] == ('127.0.0.1', (runtime.PORT,))
    assert calls[1][1] == ('POST', '/completion') and calls[-1] == ('close',)


@pytest.mark.parametrize('response', [FakeResponse(b'x' * (runtime.HTTP_BYTES + 1)),
                                     FakeResponse(b'not JSON'), FakeResponse(b'[]'),
                                     FakeResponse(status=302)])
def test_mock_local_http_rejects_bad_or_oversized_responses(monkeypatch, response):
    calls = install_fake_connections(monkeypatch, [response], False)
    with pytest.raises(runtime.BenchmarkFailure):
        runtime._local_json('/completion', {}, time.monotonic() + 1)
    assert calls[-1] == ('close',)


def test_local_inference_token_bound_prevents_completion_and_reports_timeout(monkeypatch):
    provider = object.__new__(runtime.LocalInference)
    provider.deadline = time.monotonic() + 10
    calls = []
    def local(endpoint, payload, deadline):
        calls.append(endpoint)
        return {'prompt': 'synthetic prompt'} if endpoint == '/apply-template' else {'tokens': [1] * runtime.CONTEXT_TOKENS}
    monkeypatch.setattr(runtime, '_local_json', local)
    result = provider.infer(SCENARIOS[0], {})
    assert result.failure == 'prompt-context-token-limit'
    assert calls == ['/apply-template', '/tokenize']
    def timeout(*args):
        raise runtime.BenchmarkFailure('inference-timeout')
    monkeypatch.setattr(runtime, '_local_json', timeout)
    result = provider.infer(SCENARIOS[0], {})
    assert result.failure == 'inference-timeout' and result.wall_seconds >= 0


@pytest.mark.parametrize('candidate', runtime.CANDIDATES, ids=lambda c: c.id)
def test_nonthinking_template_then_completion_captures_raw_without_repair(tmp_path, monkeypatch, candidate):
    argv = []
    monkeypatch.setattr(runtime, 'Process', lambda args, root: argv.append(args))
    provider = runtime.LocalInference(Path('/fixed/llama-server'), tmp_path / candidate.filename,
                                      tmp_path, time.monotonic() + 10, {})
    calls = []
    def local(endpoint, payload, deadline):
        calls.append((endpoint, payload))
        if endpoint == '/apply-template':
            return {'prompt': 'model-native template: <think></think>'}
        if endpoint == '/tokenize':
            return {'tokens': [1, 2, 3]}
        return {'content': '<think>unexpected reasoning</think>{}',
                'timings': {'predicted_n': 5, 'predicted_per_second': 2.5}}
    monkeypatch.setattr(runtime, '_local_json', local)
    metadata = {}
    result = provider.infer(SCENARIOS[0], metadata)
    assert result.raw == '<think>unexpected reasoning</think>{}'
    assert result.prompt_tokens == 3 and result.completion_tokens == 5 and result.tokens_per_second == 2.5
    assert len(argv) == 1 and argv[0][argv[0].index('--predict') + 1] == str(runtime.OUTPUT_TOKENS)
    assert calls[0] == ('/apply-template', {'messages': messages(SCENARIOS[0])})
    assert calls[0][1]['messages'][0] == {'role': 'system', 'content': SYSTEM_PROMPT}
    assert set(calls[-1][1]) == {'prompt', 'n_predict', 'temperature', 'top_k', 'top_p', 'min_p',
                               'seed', 'repeat_penalty', 'cache_prompt', 'stream'}
    assert {k: calls[-1][1][k] for k in ('temperature', 'top_k', 'top_p', 'min_p', 'repeat_penalty')} == {
        'temperature': 0.0, 'top_k': 1, 'top_p': 1.0, 'min_p': 0.0, 'repeat_penalty': 1.0}
    assert calls[-1][1]['n_predict'] == runtime.OUTPUT_TOKENS and calls[-1][1]['seed'] == runtime.SEED
    assert calls[-1][1]['cache_prompt'] is False
    assert not score(SCENARIOS[0], result)['gates']['raw_json_valid']


def test_model_response_does_not_control_fixed_build_argv(tmp_path, monkeypatch):
    calls = []
    def command(argv, root, deadline, maximum, include_stderr=False):
        calls.append(argv)
        if argv[-1] == 'HEAD':
            return runtime.LLAMA_COMMIT
        return 'fake version output' if argv[-1] == '--version' else ''
    monkeypatch.setattr(runtime, '_command', command)
    record = {}
    runtime.build_llama(tmp_path, time.monotonic() + 10, record)
    fetch = next(a for a in calls if 'fetch' in a)
    assert fetch[-2:] == [runtime.LLAMA_URL, runtime.LLAMA_COMMIT]
    configure = next(a for a in calls if a[0] == 'cmake' and '-S' in a)
    assert '-DLLAMA_BUILD_UI=OFF' in configure and '-DLLAMA_USE_PREBUILT_UI=OFF' in configure
    assert all(a[0] in ('git', 'cmake') or a[0].endswith('/llama-server') for a in calls)
    assert record['commit'] == runtime.LLAMA_COMMIT and record['executable_version'] == 'fake version output'
    assert record['source_acquisition_seconds'] >= 0 and record['build_seconds'] >= 0


def test_host_cancellation_propagates_after_cleanup(tmp_path, fake_provider):
    def cancelled(*args):
        raise runtime.BenchmarkFailure('host-termination')
    with pytest.raises(runtime.BenchmarkFailure, match='host-termination'):
        runtime.run_candidate(runtime.CANDIDATES[0], Path('/fixed/executable'), tmp_path,
                              time.monotonic() + 10, cancelled, fake_provider)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('setup_failed', [False, True])
def test_complete_host_orchestration_with_fake_setup_and_inference(tmp_path, monkeypatch, fake_provider, setup_failed):
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    monkeypatch.setattr(runtime, 'require_hosted_runner', lambda: None)
    monkeypatch.setattr(runtime.resource, 'setrlimit', lambda *args: None)
    monkeypatch.setattr(runtime.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(runtime, '_command', lambda *args: 'd7360f2b340c3d1e93479f77f14c897b8022a8c6')
    def build(*args):
        if setup_failed:
            raise runtime.BenchmarkFailure('fake-setup-failure')
        return Path('/fixed/llama-server')
    monkeypatch.setattr(runtime, 'build_llama', build)
    real_run = runtime.run_candidate
    monkeypatch.setattr(runtime, 'run_candidate', lambda c, e, r, d: real_run(c, e, r, d, fake_acquire, fake_provider))
    code = runtime.main()
    result = json.loads((tmp_path / 'p01-evidence' / 'benchmark-results.json').read_text())
    assert code == int(setup_failed) and len(result['candidates']) == 3
    assert all(len(c['scenarios']) == 5 for c in result['candidates'])
    assert all(c['aggregate']['score'] == (0 if setup_failed else 50) for c in result['candidates'])
    assert result['setup_failure'] == ('fake-setup-failure' if setup_failed else None)
    assert result['benchmark_revision'] == 'P01-R2'
    configuration = result['configuration']
    assert configuration['completion_tokens'] == runtime.OUTPUT_TOKENS
    assert configuration['inference_seconds'] == runtime.INFERENCE_SECONDS
    assert configuration['system_prompt_sha256'] == hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest()
    assert configuration['grammar'] is None
    assert all(m[0] == {'role': 'system', 'content': SYSTEM_PROMPT} for m in result['effective_messages'].values())
    assert set(p.name for p in tmp_path.iterdir()) == {'p01-evidence'}
