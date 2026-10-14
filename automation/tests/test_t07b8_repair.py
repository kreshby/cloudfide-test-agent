"""Host repair boundaries through the actual agent, verifier and local handoff.

Finite provider/Docker seams never execute proposed source. Real Qwen acceptance
is a separate opt-in local rehearsal, never a prerequisite of ordinary CI.
"""

from dataclasses import asdict, replace
import json
import shutil

import pytest

from automation.test_agent import agent as a, verification as v, workflow as w
from automation.test_agent.agent import State, UnitTestAgent
from automation.test_agent.contracts import FinalHostStatus, Target
from automation.test_agent.context import Evidence
from automation.test_agent.handoff import decode, encode, report, successful
from automation.test_agent.provider import FakeProvider, Stage, canonical, digest
from automation.test_agent.repair import (CompilerDiagnostic, MAX_DIAGNOSTICS, MESSAGES,
                                         compiler_diagnostics, language_contract)
from automation.test_agent.reporting import AgentRun, bind_evidence
from automation.tests.t04_fixtures import FRONTEND_CODE, bundle_for, decisions_for, transcript
from automation.tests.test_verification import FakeExecutor, capture
from automation.tests.test_workflow import gate_reader, pre_for


class RepairProvider(FakeProvider):
    def __init__(self, bundle, change=None):
        actions = {"backend": "update"} if len(bundle.changed_files) == 2 else None
        super().__init__(transcript(bundle, decisions_for(bundle, actions)))
        self.change = change or (lambda value: value)

    def infer(self, request, timeout):
        if request.stage != Stage.REPAIR:
            return super().infer(request, timeout)
        assert 0 < timeout <= a.MAX_CALL_SECONDS
        self.requests.append(request)
        prior = json.loads(request.user)["input"]["previous_content"]
        return canonical(self.change({"binding": request.binding, "target": "frontend",
                                      "replacement_content": prior + "\n// syntax repaired\n"})).decode()


class CompilerExecutor(FakeExecutor):
    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if (argv[:3] == v.DOCKER and argv[3] == "exec" and argv[8] == self.fail_step
                and self.fail_step.endswith("typecheck")):
            return capture(self.compiler_output, exit_code=2)
        return result


@pytest.fixture
def rig(tmp_path, request):
    case = getattr(request, "param", "frontend-generate")
    bundle = bundle_for("mixed-stack" if case == "mixed-stack" else "frontend-generate")
    bundle = replace(bundle, source=replace(bundle.source, run_id=str(bundle.workflow.run_id)))
    if case == "update":
        path = bundle.candidate_tests[0].path
        bundle = replace(bundle, existing_tests=(Evidence(
            path, bundle.source.head_sha, "f" * 40, "existing-test", ("named-module-test",),
            "available", len(FRONTEND_CODE.encode()), FRONTEND_CODE),),
            candidate_tests=(replace(bundle.candidate_tests[0], exists=True),))
    root = tmp_path / "checkout"
    root.mkdir()
    files = {p: v.safe_read(v.TRUSTED_ROOT, p) for p in v.TRUSTED_FILES}
    files.update({e.path: (e.content.encode(), 0o644) for e in bundle.sources})
    files.update({e.path: (e.content.encode(), 0o644) for e in bundle.existing_tests})
    files["frontend/src/baseline.test.ts"] = (b"import {it,expect} from 'vitest'\nit('base',()=>expect(1).toBe(1))\n", 0o644)
    files["backend/tests/test_baseline.py"] = (b"def test_base():\n    assert 1 == 1\n", 0o644)
    for path, (data, mode) in files.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(mode)
    executor = CompilerExecutor(root, bundle.source, files, changed=tuple(f.path for f in bundle.changed_files))
    path = next(c.path for c in bundle.candidate_tests if c.path.startswith("frontend/"))
    executor.compiler_output = f"{path.removeprefix('frontend/')}\u00286,3): error TS1005: ';' expected.\n".encode()
    provider = RepairProvider(bundle)
    agent = UnitTestAgent(bundle, root, provider)
    assert agent.run().state == State.AWAITING_VERIFICATION
    reports = []
    def verify():
        evidence = v.Verifier(executor=executor).verify(
            bundle.source, root, agent.result().policy.changes,
            profile_ids=(("frontend-v1", "backend-v1") if case == "mixed-stack" else ("frontend-v1",)),
            image=executor.image)
        reports.append(evidence)
        bind_evidence(bundle, agent.result(), evidence, publication=AgentRun(100, 1))
        assert not executor.containers
        return evidence
    yield bundle, root, executor, provider, agent, verify
    for evidence in reports:
        shutil.rmtree(evidence.output_directory)


def failing(rig):
    _, _, executor, _, _, verify = rig
    executor.fail_step = "frontend-patched-typecheck"
    evidence = verify()
    assert evidence.host_result.status == FinalHostStatus.VERIFICATION_FAILED
    assert evidence.compiler_failure is not None
    return evidence


def test_trusted_success_needs_no_repair_or_model_interpretation(rig):
    bundle, _, _, provider, agent, verify = rig
    evidence = verify()
    assert evidence.compiler_failure is None
    result = agent.resume(evidence.host_result)
    assert result.outcome == "verified" and result.provider_calls == 2
    assert result.recommendations == ()
    assert [r.stage for r in provider.requests] == [Stage.DECISION, Stage.EDITS]
    bind_evidence(bundle, result, evidence, publication=AgentRun(100, 1))


def test_one_bound_replacement_then_fresh_verification_and_tests(rig):
    bundle, root, executor, provider, agent, verify = rig
    initial = agent.result()
    evidence = failing(rig)
    feedback = evidence.compiler_failure
    repaired = agent.repair(feedback)
    assert repaired.state == State.AWAITING_VERIFICATION and repaired.provider_calls == 3
    assert len(repaired.proposal.edits) == 1
    assert repaired.proposal.edits[0].path == initial.proposal.edits[0].path == feedback.path
    assert repaired.policy.decision == "proposal-accepted"
    assert not (root / feedback.path).exists()
    request = provider.requests[-1]
    payload = json.loads(request.user)
    data = payload["input"]
    assert request.stage == Stage.REPAIR and request.target == Target.FRONTEND
    assert data["accepted_path"] == feedback.path
    assert data["previous_content"] == initial.proposal.edits[0].content
    assert data["attempt"] == 1 and data["jsx_allowed"] is True
    assert data["diagnostics"] == [asdict(d) for d in feedback.diagnostics]
    assert data["diagnostics_sha256"] == feedback.sha256
    assert data["verification_sha256"] == feedback.verification_sha256
    assert payload["source"] == asdict(bundle.source) and payload["context_sha256"] == bundle.sha256
    assert request.response_schema["additionalProperties"] is False
    assert set(request.response_schema["required"]) == {"binding", "target", "replacement_content"}
    assert "no application source" in request.system and "jsx_allowed=false" in request.system
    assert "UNTRUSTED" not in request.user and "stdout" not in request.user
    executor.fail_step = None
    second = verify()
    assert second.changes_sha256 != evidence.changes_sha256
    assert second.compiler_failure is None and all(c.passed for c in second.commands)
    targeted = next(c for c in second.commands if c.profile_id == "frontend-patched-targeted")
    assert targeted.counts.collected > 0 and targeted.counts.passed > 0
    final = agent.resume(second.host_result)
    assert final.outcome == "verified" and final.provider_calls == 3
    bind_evidence(bundle, final, second, publication=AgentRun(100, 1))


@pytest.mark.parametrize("change", [
    lambda x: {**x, "path": "frontend/src/new.test.ts"},
    lambda x: {**x, "path": "frontend/src/SubmitButton.tsx"},
    lambda x: {**x, "edits": []},
    lambda x: {**x, "binding": "0" * 64},
    lambda x: {**x, "target": "backend"},
    lambda x: {**x, "replacement_content": ""},
    lambda x: {**x, "replacement_content": None},
    lambda x: {**x, "replacement_content": "```ts\n" + x["replacement_content"]},
    lambda x: {**x, "replacement_content": x["replacement_content"].replace("it(", "it.skip(")},
    lambda x: {**x, "replacement_content": "it('empty',()=>{})"},
    lambda x: {**x, "replacement_content": x["replacement_content"].split("  rerender(")[0] + "})\n"},
])
def test_repair_output_cannot_expand_authority_or_weaken_tests(rig, change):
    _, root, _, provider, agent, _ = rig
    initial = agent.result()
    evidence = failing(rig)
    provider.change = change
    final = agent.repair(evidence.compiler_failure)
    assert final.state == State.FAILED and final.provider_calls == 3
    assert final.proposal == initial.proposal
    assert not (root / initial.proposal.edits[0].path).exists()


@pytest.mark.parametrize("field,value", [("path", "frontend/src/new.test.ts"),
                                         ("changes_sha256", "0" * 64),
                                         ("previous_content_sha256", "0" * 64)])
def test_stale_repair_binding_rejected_before_inference(rig, field, value):
    _, _, _, provider, agent, _ = rig
    feedback = replace(failing(rig).compiler_failure, **{field: value})
    assert agent.repair(feedback).state == State.FAILED
    assert len(provider.requests) == 2


def test_raw_arbitrary_log_is_not_a_repair_request(rig):
    _, _, _, provider, agent, _ = rig
    assert agent.repair({"stdout": "arbitrary log"}).state == State.FAILED
    assert len(provider.requests) == 2
    assert "arbitrary log" not in agent.result().to_bytes().decode()


def test_failed_second_typecheck_is_terminal_and_never_loops(rig):
    _, _, _, provider, agent, verify = rig
    evidence = failing(rig)
    assert agent.repair(evidence.compiler_failure).state == State.AWAITING_VERIFICATION
    second = verify()
    assert second.compiler_failure is not None
    final = agent.resume(second.host_result)
    assert final.outcome == "verification-failed" and len(provider.requests) == 3
    with pytest.raises(a.AgentError):
        agent.repair(second.compiler_failure)


def test_second_repair_impossible_even_before_terminal_resume(rig):
    _, _, _, provider, agent, verify = rig
    assert agent.repair(failing(rig).compiler_failure).state == State.AWAITING_VERIFICATION
    assert agent.repair(verify().compiler_failure).state == State.FAILED
    assert len(provider.requests) == 3


@pytest.mark.parametrize("failure", ["baseline-typecheck", "baseline-suite", "timeout", "unrelated", "malformed", "oversized", "truncated"])
def test_nonrepairable_failures_produce_no_feedback_or_repair_call(rig, failure):
    _, _, executor, provider, agent, verify = rig
    executor.fail_step = "frontend-patched-typecheck"
    if failure.startswith("baseline"):
        executor.fail_step = "frontend-" + failure
    elif failure == "timeout":
        executor.fail_step = None
        executor.timeout_step = "frontend-patched-typecheck"
    elif failure == "unrelated":
        executor.compiler_output = b"src/SubmitButton.tsx(1,1): error TS1005: token expected.\n"
    elif failure == "malformed":
        executor.compiler_output = b"raw Docker error or stack trace\n"
    elif failure == "oversized":
        executor.compiler_output = executor.compiler_output.rstrip() + b"X" * 257 + b"\n"
    elif failure == "truncated":
        executor.compiler_output *= MAX_DIAGNOSTICS + 1
    evidence = verify()
    assert evidence.host_result.status != FinalHostStatus.VERIFIED
    assert evidence.compiler_failure is None
    result = agent.resume(evidence.host_result)
    assert result.outcome != "verified" and len(provider.requests) == 2


@pytest.mark.parametrize("path,jsx", [("frontend/src/value.test.ts", False), ("frontend/src/value.test.tsx", True)])
def test_language_contract_is_derived_only_from_test_path(path, jsx):
    assert language_contract(path) == {"language": "typescript", "jsx_allowed": jsx}
    with pytest.raises(ValueError):
        language_contract("frontend/src/value.tsx")


def test_compiler_messages_cannot_leak_quoted_source_literals():
    path = "frontend/src/value.test.ts"
    raw = b"\x1b[31msrc/value.test.ts(1,1): error TS2322: Type 'private-token' is not assignable.\x1b[0m\n"
    diagnostics = compiler_diagnostics(raw, b"", path, b"x\n")
    assert diagnostics == (CompilerDiagnostic("TS2322", 1, 1, MESSAGES["TS2322"]),)
    assert "private-token" not in canonical([asdict(d) for d in diagnostics]).decode()


@pytest.mark.parametrize("raw", [
    b"src/value.test.ts(0,1): error TS1005: token expected.\n",
    b"src/value.test.ts(1,99999): error TS1005: token expected.\n",
    b"src/value.test.ts(1,1): error TS9999: unknown error.\n",
    b"src/value.test.ts(1,1): error TS1005: token\x00expected.\n",
    b"src/value.test.ts(1,1): error TS1005: token expected.\nother log\n",
    b"/input/frontend/src/value.test.ts(1,1): error TS1005: token expected.\n",
])
def test_malformed_mixed_and_ambiguous_diagnostics_fail_closed(raw):
    assert compiler_diagnostics(raw, b"", "frontend/src/value.test.ts", b"x\n") == ()


def test_zero_generated_test_collection_after_repair_still_fails(rig):
    _, _, executor, _, agent, verify = rig
    assert agent.repair(failing(rig).compiler_failure).state == State.AWAITING_VERIFICATION
    executor.fail_step = None
    executor.zero_step = "frontend-patched-targeted"
    evidence = verify()
    assert evidence.compiler_failure is None
    assert agent.resume(evidence.host_result).outcome == "verification-failed"


@pytest.mark.parametrize("limit", ["calls", "rounds", "active", "per-call"])
def test_repair_obeys_existing_call_and_time_budgets(rig, monkeypatch, limit):
    _, _, _, provider, agent, _ = rig
    feedback = failing(rig).compiler_failure
    if limit == "calls":
        monkeypatch.setattr(a, "MAX_PROVIDER_CALLS", 2)
    elif limit == "rounds":
        agent._rounds.add((Stage.REPAIR, Target.FRONTEND))
    elif limit == "active":
        agent._active_seconds = a.MAX_ACTIVE_SECONDS
    else:
        now = [0.0]
        agent._clock = lambda: now[0]
        original = provider.infer
        def late(request, timeout):
            raw = original(request, timeout)
            now[0] += timeout + 1
            return raw
        provider.infer = late
    assert agent.repair(feedback).state == State.FAILED
    assert len(provider.requests) == (3 if limit == "per-call" else 2)


@pytest.mark.parametrize("twice", [False, True])
def test_production_workflow_performs_one_repair_and_preserves_handoff_status(rig, monkeypatch, twice):
    bundle, root, executor, _, _, _ = rig
    provider = RepairProvider(bundle)
    events = []
    class Runtime:
        def __enter__(self):
            self.provider = provider
            events.append("start")
            return self
        def start(self):
            events.append("restart")
        def stop(self):
            events.append("stop")
        def __exit__(self, *_):
            events.append("cleanup")
    class Source:
        def __init__(self, *_):
            pass
        def __enter__(self):
            return root
        def __exit__(self, *_):
            pass
    class Verifier(v.Verifier):
        def verify(self, *args, **kwargs):
            events.append("verify")
            executor.fail_step = "frontend-patched-typecheck" if twice or events.count("verify") == 1 else None
            return super().verify(*args, **{**kwargs, "image": executor.image})
    monkeypatch.setattr(w, "collect_context", lambda *_a, **_k: bundle)
    pre = pre_for(bundle)
    facts, artifact = w.execute(pre, gate_reader(pre), source_factory=Source, runtime_factory=Runtime,
                                verifier_factory=lambda: Verifier(executor=executor))
    assert events == ["start", "stop", "verify", "restart", "stop", "verify", "cleanup"]
    assert len(provider.requests) == 3 and events.count("restart") == 1
    expected = "VERIFICATION_FAILED" if twice else "VERIFIED"
    assert facts["status"] == expected and artifact is not None
    raw = encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    decoded = decode(raw, digest(raw), expected_identity=pre.identity,
                     expected_source_ci=pre.source_ci, expected_trusted_sha="8" * 40)
    prepared = report(decoded, artifact_id=700)
    assert prepared.status == expected and successful(prepared) is (not twice)


def test_repair_native_provider_preflight_reserves_edit_sized_output(monkeypatch):
    from automation.test_agent.provider import LocalLlamaProvider, ProviderError, ProviderRequest
    from automation.tests.test_provider import Response, fake_http
    request = ProviderRequest(Stage.REPAIR, Target.FRONTEND, "a" * 64, "system", '{"input":{}}')
    connections = fake_http(monkeypatch, [Response({"prompt": "p"}), Response({"tokens": [1] * (8192 - 768 + 1)})])
    with pytest.raises(ProviderError) as caught:
        LocalLlamaProvider().infer(request, 300)
    assert caught.value.diagnostic == "provider-token-budget"
    assert len(connections) == 2  # No inference after failed token preflight.


@pytest.mark.parametrize("rig", ["update"], indirect=True)
def test_repair_update_keeps_original_hash_operation_and_checkout(rig):
    _, root, executor, _, agent, verify = rig
    prior = agent.result().proposal.edits[0]
    original = (root / prior.path).read_bytes()
    assert agent.repair(failing(rig).compiler_failure).state == State.AWAITING_VERIFICATION
    replacement = agent.result().proposal.edits[0]
    assert (replacement.path, replacement.operation, replacement.original_sha256) == (
        prior.path, prior.operation, prior.original_sha256)
    assert replacement.operation == "update" and (root / prior.path).read_bytes() == original
    executor.fail_step = None
    assert agent.resume(verify().host_result).outcome == "verified"


@pytest.mark.parametrize("rig", ["mixed-stack"], indirect=True)
def test_full_stack_worst_path_uses_five_calls_and_preserves_backend(rig):
    _, _, executor, provider, agent, verify = rig
    assert agent.result().provider_calls == 4
    prior = next(e for e in agent.result().proposal.edits if e.target == Target.BACKEND)
    assert agent.repair(failing(rig).compiler_failure).state == State.AWAITING_VERIFICATION
    assert next(e for e in agent.result().proposal.edits if e.target == Target.BACKEND) == prior
    executor.fail_step = None
    assert agent.resume(verify().host_result).outcome == "verified"
    assert len(provider.requests) == 5
    assert [r.stage for r in provider.requests] == [Stage.DECISION, Stage.EDITS,
                                                 Stage.DECISION, Stage.EDITS, Stage.REPAIR]


def test_tampered_diagnostic_cannot_cross_repair_boundary(rig):
    _, _, _, provider, agent, _ = rig
    failure = failing(rig).compiler_failure
    object.__setattr__(failure.diagnostics[0], "message", "arbitrary Docker log")
    assert agent.repair(failure).state == State.FAILED
    assert len(provider.requests) == 2


def test_nonpretty_tsc_is_fixed_for_both_guards():
    from automation.verifier.profiles import command
    for step in ("frontend-baseline-typecheck", "frontend-patched-typecheck"):
        argv = command(step)
        assert argv[argv.index("--pretty") + 1] == "false"
