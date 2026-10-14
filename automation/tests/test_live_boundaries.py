"""Regressions for the exact first live PR context and publication boundary."""

from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from automation.test_agent import publisher, workflow as w
from automation.test_agent.agent import AgentError, MAX_PROJECTION_BYTES, State, UnitTestAgent, project_context
from automation.test_agent.contracts import Target
from automation.test_agent.handoff import decode, encode, report, successful, validate
from automation.test_agent.repair import language_contract
from automation.test_agent.import_contract import frontend_import_contract
from automation.test_agent.provider import (COMPLETION_TOKENS, CONTEXT_TOKENS, MAX_REQUEST_BYTES,
                                           FakeProvider, LocalLlamaProvider, ProviderError, Stage, canonical, digest)
from automation.test_agent.publisher import GitHubPublisherAdapter, PublisherAuthor, publish
from automation.test_agent.reporting import DeliveryState, parse_marker
from automation.tests.t04_fixtures import decisions_for, install_originals, _binding
from automation.tests.t07b2_fixtures import (BACKEND_TEST, BASE, CONTEXT_HASH, FRONTEND_TEST, HANDOFF_HASH,
                                          HEAD, HTTPFixture, live_bundle, live_handoff, publisher_environment)


def full_stack_transcript(bundle):
    """Finite deterministic replies bound to the real projected evidence."""
    responses = []
    for decision in decisions_for(bundle):
        target = Target(decision["target"])
        path = FRONTEND_TEST if target == Target.FRONTEND else BACKEND_TEST
        if target == Target.FRONTEND:
            decision['selected_source_path'] = 'frontend/src/features/todos/api/todos.ts'
        plan = decision["plan"]
        plan["existing_test_evidence"] = [path]
        plan["intended_test_files"] = [path]
        projection = json.loads(project_context(bundle, target))
        responses.append(canonical({"binding": _binding(bundle, Stage.DECISION, target, projection),
                                    "target": str(target), "action": "update",
                                    **({'selected_source_path': decision['selected_source_path']}
                                       if target == Target.FRONTEND else {}),
                                    **{k: v for k, v in plan.items() if k != "skill_ids"}}).decode())
        edits = json.loads(project_context(bundle, target, [path], decision['selected_source_path']))
        original = next(e for e in edits["existing_tests"] if e["path"] == path)
        assert original["state"] == "available"
        addition = ("\nit('sends a priority filter when listing tasks', async () => {\n"
                    "  vi.mocked(request).mockResolvedValue([todoFixture])\n"
                    "  await todosApi.list(undefined, 'high')\n"
                    "  expect(request).toHaveBeenCalledWith('/todos?priority=high', { signal: undefined })\n"
                    "})\n" if target == Target.FRONTEND else
                    "\ndef test_invalid_priority_never_calls_repository(client, repository_mock):\n"
                    "    assert client.get('/api/todos?priority=urgent').status_code == 422\n"
                    "    repository_mock.list.assert_not_called()\n")
        payload = {"decision": decision, "evidence": edits, "permitted_test_paths": [path],
                   **(frontend_import_contract(bundle, path, decision['selected_source_path'])
                      if target == Target.FRONTEND else {}),
                   **({'test_operation': 'update'} if target == Target.FRONTEND else {}),
                   "original_sha256": {path: original["content_sha256"]},
                   **({"file_language": {path: language_contract(path)}} if target == Target.FRONTEND else {})}
        responses.append(canonical({"binding": _binding(bundle, Stage.EDITS, target, payload),
                                    "target": str(target), "edits": [{"path": path, "operation": "update",
                                        "original_sha256": original["content_sha256"],
                                        "content": original["content"] + addition}]}).decode())
    return tuple(responses)


def test_exact_live_full_stack_context_reaches_both_decisions_and_safe_updates(tmp_path):
    bundle = live_bundle()
    assert bundle.sha256 == CONTEXT_HASH and len(bundle.to_bytes()) == 73314
    assert bundle.source.base_sha == BASE and bundle.source.head_sha == HEAD
    assert len(bundle.changed_files) == 11
    assert sum(f.path.startswith("frontend/") for f in bundle.changed_files) == 10
    assert sum(f.path.startswith("backend/") for f in bundle.changed_files) == 1
    # On the trusted base this assertion reproduces the pre-_ask overflow.
    provider = FakeProvider(("{}",))
    probe = UnitTestAgent(bundle, tmp_path, provider)
    with patch.object(probe, "_ask", wraps=probe._ask) as ask:
        initial = probe.run()
    assert ask.call_count == 1, (initial.failure, initial.provider_calls)
    assert len(provider.requests) == 1 and provider.requests[0].target == Target.FRONTEND
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == State.AWAITING_VERIFICATION, result.failure
    assert [(r.stage, r.target) for r in provider.requests] == [
        (Stage.DECISION, Target.FRONTEND), (Stage.EDITS, Target.FRONTEND),
        (Stage.DECISION, Target.BACKEND), (Stage.EDITS, Target.BACKEND)]
    assert MAX_PROJECTION_BYTES == 16384 and MAX_REQUEST_BYTES == 24576
    for request in provider.requests:
        assert len(request.to_bytes()) <= MAX_REQUEST_BYTES
        evidence = json.loads(request.user)["input"]
        if request.stage == Stage.EDITS:
            evidence = evidence["evidence"]
            original = evidence["existing_tests"][0]
            supplied = next(e for e in bundle.existing_tests if e.path == original["path"])
            assert original["state"] == "available" and original["content"] == supplied.content
            assert original["content_sha256"] == digest(supplied.content.encode())
        assert len(canonical(evidence)) <= MAX_PROJECTION_BYTES
        assert {c["path"] for c in evidence["candidate_tests"]} <= {c.path for c in bundle.candidate_tests}
    # Proposals remain data. Exact original tests are still on disk.
    assert all((tmp_path / e.path).read_text() == e.content for e in bundle.existing_tests)


def test_global_projection_budget_is_deterministic_and_clipping_is_explicit():
    bundle = live_bundle()
    reordered = replace(bundle, changed_files=tuple(reversed(bundle.changed_files)),
                        sources=tuple(reversed(bundle.sources)), existing_tests=tuple(reversed(bundle.existing_tests)),
                        candidate_tests=tuple(reversed(bundle.candidate_tests)))
    for target in Target:
        raw = project_context(bundle, target)
        assert raw == project_context(bundle, target)
        # Digest changes with bundle serialization; the evidence ordering does not.
        a, b = json.loads(raw), json.loads(project_context(reordered, target))
        a.pop("context_sha256")
        b.pop("context_sha256")
        assert canonical(a) == canonical(b)
        assert len(raw) <= MAX_PROJECTION_BYTES
    frontend = json.loads(project_context(bundle, Target.FRONTEND))
    assert any(e["state"] == "projection-clipped" and e["content"] is None for e in frontend["existing_tests"])
    assert any(e["state"] == "available" for e in frontend["existing_tests"])
    assert all(f.get("head_content_state") == "represented-by-complete-patch"
               for f in frontend["changed_files"] if f["support"] == "supported")
    assert all(f["patch_state"] == "complete" for f in frontend["changed_files"] if f["support"] == "supported")


@pytest.mark.parametrize("case", ["mandatory-content", "metadata", "intended-test"])
def test_hostile_projection_still_fails_closed_without_context_expansion(tmp_path, case):
    bundle = live_bundle()
    if case == "mandatory-content":
        sources = tuple(replace(e, content="x" * 32000, observed_bytes=32000) for e in bundle.sources)
        bundle = replace(bundle, sources=sources)
    elif case == "metadata":
        bundle = replace(bundle, markers=("x" * 16384,))
    else:
        test = next(e for e in bundle.existing_tests if e.path == FRONTEND_TEST)
        bundle = replace(bundle, existing_tests=(replace(test, content="x" * 32000, observed_bytes=32000),))
        projection = json.loads(project_context(bundle, Target.FRONTEND, [FRONTEND_TEST]))
        assert projection["existing_tests"][0]["state"] == "projection-clipped"
        assert len(projection["existing_tests"][0]["content"].encode()) <= 4096
        plan = decisions_for(bundle)[0]["plan"]
        plan["intended_test_files"] = [FRONTEND_TEST]
        decision_projection = json.loads(project_context(bundle, Target.FRONTEND))
        reply = {"binding": _binding(bundle, Stage.DECISION, Target.FRONTEND, decision_projection),
                 "target": "frontend", "action": "update",
                 "selected_source_path": 'frontend/src/features/todos/api/todos.ts',
                 **{k: v for k, v in plan.items() if k != "skill_ids"}}
        provider = FakeProvider((canonical(reply).decode(),))
        result = UnitTestAgent(bundle, tmp_path, provider).run()
        assert result.failure == "decision cites unavailable test evidence"
        assert len(provider.requests) == 1 and provider.requests[0].stage == Stage.DECISION
        return
    provider = FakeProvider(("{}",))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.failure == "context projection byte limit" and result.provider_calls == 0
    assert not provider.requests
    with pytest.raises(AgentError, match="context projection byte limit"):
        project_context(bundle, Target.FRONTEND)


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("stage", [Stage.DECISION, Stage.EDITS])
def test_live_request_token_gate_reserves_completion_and_rejects_overflow(tmp_path, stage, extra):
    bundle = live_bundle()
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    UnitTestAgent(bundle, tmp_path, provider).run()
    request = next(r for r in provider.requests if r.target == Target.FRONTEND and r.stage == stage)
    assert len(request.to_bytes()) <= MAX_REQUEST_BYTES
    operations = []
    local = LocalLlamaProvider()
    def post(endpoint, payload, deadline):
        operations.append(endpoint)
        if endpoint == "/apply-template":
            assert payload["messages"][1]["content"] == request.user
            return {"prompt": "fixed tokenizer fixture"}
        if endpoint == "/tokenize":
            return {"tokens": [1] * (CONTEXT_TOKENS - COMPLETION_TOKENS[stage] + extra)}
        assert len(payload["prompt"]) + payload["n_predict"] <= CONTEXT_TOKENS
        return {"content": "{}", "truncated": False, "tokens_predicted": 1, "stop_type": "eos"}
    with patch.object(local, "_post", side_effect=post):
        if extra:
            with pytest.raises(ProviderError, match="prompt token budget"):
                local.infer(request, 150)
            assert operations == ["/apply-template", "/tokenize"]
        else:
            assert local.infer(request, 150) == "{}"
            assert operations == ["/apply-template", "/tokenize", "/completion"]


@pytest.mark.parametrize("close,framing", [(True, "length"), (False, "length"), (True, "chunked"), (True, "eof")])
def test_exact_failed_handoff_full_publisher_path_writes_once_then_exits_red(tmp_path, monkeypatch, capsys, close, framing):
    api = publisher_environment(tmp_path, monkeypatch)
    raw = live_handoff()
    value = json.loads(raw)
    identity, _ = validate(value)
    assert value["facts"]["reason"] is None and value["facts"]["status"] == "AGENT_FAILED"
    decoded = decode(raw, HANDOFF_HASH, expected_identity=identity, expected_source_ci=value["source_ci"],
                     expected_trusted_sha=BASE)
    prepared = report(decoded)
    assert parse_marker(prepared.body) == identity and len(prepared.body.encode()) == 1254
    assert prepared.delivery.state == DeliveryState.NOT_REQUIRED and not successful(prepared)
    http = HTTPFixture(api.pr, close=close, framing=framing)
    monkeypatch.setattr(publisher.http.client, "HTTPSConnection", http.connection)
    assert w.EXPECTED_AUTHOR == PublisherAuthor(41898282, "github-actions[bot]", "Bot")
    assert w.main("publish") == 1
    assert capsys.readouterr().out == "publisher-created\n"
    assert [method for method, _, _ in http.operations] == ["GET", "GET", "GET", "POST"]
    assert http.closed == 4 and len(http.comments) == 1
    assert http.comments[0]["body"] == prepared.body and "no execution evidence" in prepared.body
    assert w.main("publish") == 1  # Same identity: no second create.
    assert capsys.readouterr().out == "publisher-idempotent\n"
    assert len(http.comments) == 1 and sum(m == "POST" for m, _, _ in http.operations) == 1
    assert any("/compare/" in path for path, _ in api.calls)  # Real preflight ran.
    # A later attempt updates the same sticky comment through the concrete HTTP path.
    newer = replace(identity, run_attempt=2)
    (tmp_path / "t07-handoff/handoff.json").write_bytes(encode(
        newer, live_bundle().source, value["source_ci"], BASE, value["facts"]))
    monkeypatch.setenv("HANDOFF_SHA256", digest((tmp_path / "t07-handoff/handoff.json").read_bytes()))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    api.agent["run_attempt"] = 2
    assert w.main("publish") == 1
    assert capsys.readouterr().out == "publisher-updated\n"
    assert len(http.comments) == 1 and http.operations[-1][0] == "PATCH"


def test_closed_socket_with_incomplete_response_fails_closed(tmp_path, monkeypatch):
    api = publisher_environment(tmp_path, monkeypatch)
    http = HTTPFixture(api.pr)
    http.extra_length = 1
    monkeypatch.setattr(publisher.http.client, "HTTPSConnection", http.connection)
    with pytest.raises(w.WorkflowError, match="publisher-read-unavailable"):
        w.main("publish")
    assert len(http.operations) == 1 and not http.comments


@pytest.mark.parametrize("phase", ["preflight", "handoff", "report", "read", "write", "receipt"])
def test_failed_publication_has_fixed_phase_diagnostic_and_never_retries(tmp_path, monkeypatch, phase):
    api = publisher_environment(tmp_path, monkeypatch)
    http = HTTPFixture(api.pr)
    monkeypatch.setattr(publisher.http.client, "HTTPSConnection", http.connection)
    if phase == "preflight":
        api.repo["private"] = True
    elif phase == "handoff":
        monkeypatch.setenv("HANDOFF_SHA256", "0" * 64)
    elif phase == "report":
        monkeypatch.setenv("FINAL_ARTIFACT_ID", "11812029265")  # No prepared patch: reject.
    elif phase in {"read", "write"}:
        http.fail_method = "GET" if phase == "read" else "POST"
    else:
        http.receipt_author = PublisherAuthor(7, "foreign[bot]")
    with pytest.raises(w.WorkflowError) as caught:
        w.main("publish")
    expected = "write" if phase == "receipt" else phase
    category = {"write": "publisher-write-http-403", "receipt": "publisher-write-receipt"}.get(
        phase, f"publisher-{expected}-unavailable")
    assert str(caught.value) == category
    assert caught.value.__suppress_context__
    assert sum(m == "POST" for m, _, _ in http.operations) == int(phase in {"write", "receipt"})


@pytest.mark.parametrize("field", ["head", "base", "state"])
def test_source_change_before_sticky_write_remains_stale(tmp_path, monkeypatch, field):
    api = publisher_environment(tmp_path, monkeypatch)
    value = json.loads(live_handoff())
    prepared = report(value)
    http = HTTPFixture(json.loads(canonical(api.pr)))
    if field == "state":
        http.pr["state"] = "closed"
    else:
        http.pr[field]["sha"] = "9" * 40
    monkeypatch.setattr(publisher.http.client, "HTTPSConnection", http.connection)
    adapter = GitHubPublisherAdapter(w.REPOSITORY, publisher.GitHubPublisherHTTPTransport(w.REPOSITORY))
    assert publish(prepared, adapter, w.EXPECTED_AUTHOR).state == "stale"
    assert len(http.operations) == 1 and not http.comments
