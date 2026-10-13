"""Finite wire protocol integration; synthetic feedback is not execution proof."""

import json

import pytest

from automation.test_agent.agent import State, UnitTestAgent, project_context
from automation.test_agent.contracts import Target
from automation.test_agent.diagnostics import Diagnostic
from automation.test_agent.provider import LocalLlamaProvider, ProviderError, canonical, digest
from automation.test_agent.protocol import stage_shape
from automation.tests.t04_fixtures import install_originals
from automation.tests.test_live_boundaries import full_stack_transcript
from automation.tests.t07b2_fixtures import live_bundle
from automation.tests.t07b3_fixtures import WireAPI


@pytest.mark.parametrize("close", [True, False])
def test_exact_pr_fixture_reaches_both_targets_via_concrete_provider(tmp_path, close):
    bundle = live_bundle()
    install_originals(bundle, tmp_path)
    replies = iter(full_stack_transcript(bundle))
    requests = []
    def reply(method, path, raw):
        assert method == "POST"
        payload = json.loads(raw)
        if path == "/apply-template":
            system, user = payload["messages"]
            request = json.loads(user["content"])
            assert request["response_shape_sha256"] == digest(canonical(stage_shape(request["stage"])))
            schema = json.loads(system["content"].split("\nResponse JSON schema (all fields required; no extra fields):\n")[1])
            requests.append((request, schema))
            return 200, {"prompt": "finite wire fixture"}
        if path == "/tokenize":
            return 200, {"tokens": [1, 2]}
        assert path == "/completion"
        request, schema = requests[-1]
        native = payload["json_schema"]
        if request["stage"] == "decision":
            for variant in native["oneOf"]:
                assert variant["required"] == schema["required"]
                assert variant["additionalProperties"] is False
                assert variant["properties"]["binding"] == schema["properties"]["binding"]
                assert variant["properties"]["target"] == schema["properties"]["target"]
        else:
            assert native == schema
        assert schema["properties"]["binding"] == {"const": request["binding"]}
        assert schema["properties"]["target"] == {"const": request["target"]}
        return 200, {"content": next(replies), "tokens_predicted": 10, "truncated": False, "stop_type": "eos"}
    api = WireAPI(reply, close=close)
    with api.serve() as port:
        result = UnitTestAgent(bundle, tmp_path, LocalLlamaProvider(port)).run()
    assert result.state == State.AWAITING_VERIFICATION
    assert result.policy.decision == "proposal-accepted"
    assert [(r["stage"], r["target"]) for r, _ in requests] == [
        ("decision", "frontend"), ("edits", "frontend"), ("decision", "backend"), ("edits", "backend")]
    assert len(api.operations) == 12
    frontend = requests[0][1]["properties"]
    available = {e["path"] for e in json.loads(project_context(bundle, Target.FRONTEND))["existing_tests"]
                 if e["state"] == "available"}
    assert set(frontend["existing_test_evidence"]["items"]["enum"]) == available


def test_native_action_path_constraints_are_derived_from_bound_host_facts(tmp_path):
    from automation.test_agent.provider import FakeProvider
    bundle = live_bundle()
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == State.AWAITING_VERIFICATION
    for request in (r for r in provider.requests if r.stage == "decision"):
        evidence = json.loads(request.user)["input"]
        exists = {c["path"]: c["exists"] for c in evidence["candidate_tests"]}
        available = {e["path"] for e in evidence["existing_tests"] if e["state"] == "available"}
        for variant in request.response_schema["oneOf"]:
            props = variant["properties"]
            action = props["action"]["const"]
            paths = props["intended_test_files"]
            if action in {"generate", "update"}:
                assert paths["minItems"] == paths["maxItems"] == 1
                assert all(exists[p] == (action == "update") for p in paths["items"]["enum"])
                if action == "update":
                    assert set(paths["items"]["enum"]) <= available
            else:
                assert paths["maxItems"] == 0
        serialized = json.loads(request.to_bytes())
        assert serialized["response_schema_sha256"] == digest(canonical(request.response_schema))


@pytest.mark.parametrize("stop,count,category", [("limit", 256, Diagnostic.PROVIDER_TRUNCATED),
                                                ("none", 10, Diagnostic.PROVIDER_PROTOCOL),
                                                (None, 10, Diagnostic.PROVIDER_PROTOCOL)])
def test_native_output_stop_evidence_cannot_hide_exhaustion(monkeypatch, stop, count, category):
    from automation.tests.test_provider import Response, fake_http, request
    fake_http(monkeypatch, [Response({"prompt": "p"}), Response({"tokens": [1]}),
                           Response({"content": "{}", "truncated": False, "tokens_predicted": count,
                                     "stop_type": stop})])
    with pytest.raises(ProviderError) as caught:
        LocalLlamaProvider().infer(request(), 10)
    assert caught.value.diagnostic == category


def test_schema_cannot_be_overridden_or_mutated_by_caller():
    from automation.tests.test_provider import request
    from dataclasses import replace
    value = request()
    original = value.to_bytes()
    value.response_schema["additionalProperties"] = True
    assert value.to_bytes() == original
    with pytest.raises(TypeError):
        replace(value, response_schema={})


def test_multiple_intended_files_fail_before_bounded_edit_completion(tmp_path):
    from automation.test_agent.provider import FakeProvider
    bundle = live_bundle()
    install_originals(bundle, tmp_path)
    response = json.loads(full_stack_transcript(bundle)[0])
    response["intended_test_files"].append("frontend/src/features/todos/api/queryKeys.test.ts")
    provider = FakeProvider((canonical(response).decode(),))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == State.FAILED and result.diagnostic == Diagnostic.PATH
    assert result.failure == "decision path count limit"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("target,path,subject", [
    (Target.FRONTEND, "frontend/src/features/todos/api/queryKeys.test.ts", "frontend/src/features/todos/api/queryKeys.ts"),
    (Target.BACKEND, "backend/tests/features/todos/test_router.py", "backend/app/features/todos/router.py"),
])
def test_edit_subject_has_exact_current_head_source_without_historical_copy(target, path, subject):
    from automation.test_agent.agent import MAX_PROJECTION_BYTES
    bundle = live_bundle()
    raw = project_context(bundle, target, [path])
    assert len(raw) <= MAX_PROJECTION_BYTES
    evidence = json.loads(raw)
    captured = next(e for e in bundle.sources if e.path == subject and e.source_sha == bundle.source.head_sha)
    selected = [e for e in evidence["sources"] if e["path"] == subject]
    assert len(selected) == 1
    assert selected[0]["state"] == "available" and selected[0]["content"] == captured.content
    assert selected[0]["content_sha256"] == digest(captured.content.encode())
    assert "source_sha" not in selected[0]  # Exact head is bound once in source identity.
