"""B9.1 canonical host imports; finite providers and isolated compiler fixtures."""

import json

import pytest

from automation.test_agent import verification as v
from automation.test_agent.agent import State, UnitTestAgent
from automation.test_agent.contracts import Target
from automation.test_agent.diagnostics import Diagnostic
from automation.test_agent.policy import PolicyError
from automation.test_agent.provider import FakeProvider, Stage, canonical, digest
from automation.test_agent.repair import language_contract
from automation.test_agent.subjects import required_subject_import
from automation.tests.t04_fixtures import bundle_for, install_originals, transcript
from automation.tests.test_t07b9_structure import parser_image


@pytest.mark.parametrize("test,source,expected", [
    ("frontend/src/module.test.ts", "frontend/src/module.ts", "./module"),
    ("frontend/src/module.test.tsx", "frontend/src/module.tsx", "./module"),
    ("frontend/src/tests/module.test.ts", "frontend/src/module.ts", "../module"),
    ("frontend/src/module.test.ts", "frontend/src/sub/module.ts", "./sub/module"),
    ("frontend/src/tests/module.test.tsx", "frontend/src/sub/module.tsx", "../sub/module"),
    ("frontend/src/consumer.test.ts", "frontend/src/sub/index.ts", "./sub/index"),
])
def test_canonical_relative_specifier(test, source, expected):
    assert required_subject_import(test, source) == expected


@pytest.mark.parametrize("test,source", [
    ("../module.test.ts", "frontend/src/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/../../../module.ts"),
    ("frontend/src/../module.test.ts", "frontend/src/module.ts"),
    ("/frontend/src/module.test.ts", "frontend/src/module.ts"),
    ("frontend/src/module.test.ts", "/frontend/src/module.ts"),
    ("frontend\\src\\module.test.ts", "frontend/src/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/./module.ts"),
    ("frontend/src/module.test.ts", "frontend//src/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/%2e%2e/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/module.ts\x00"),
    (None, "frontend/src/module.ts"),
    ("frontend/src/module.test.ts", None),
    ("frontend/src/module.ts", "frontend/src/module.ts"),
    ("backend/tests/test_module.py", "frontend/src/module.ts"),
    ("frontend/src/module.test.ts", "backend/app/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/module.js"),
    ("frontend/src/module.test.ts", "frontend/src/module.d.ts"),
    ("frontend/src/module.test.ts", "frontend/src/module.test.ts"),
    ("frontend/src/module.test.ts", "frontend/src/module.spec.tsx"),
    ("frontend/src/module.test.ts", "frontend/src/fixtures/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/node_modules/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/vite.config.ts"),
    ("frontend/src/module.test.ts", "frontend/src/.tsx"),
])
def test_invalid_paths_fail_closed(test, source):
    with pytest.raises(PolicyError):
        required_subject_import(test, source)


@pytest.mark.parametrize("case,expected_import", [
    ("frontend-generate", "./SubmitButton"), ("mixed-stack", "./TaskCount"),
])
def test_edit_request_and_trace_bind_host_import(tmp_path, case, expected_import):
    bundle = bundle_for(case)
    install_originals(bundle, tmp_path)
    provider = FakeProvider(transcript(bundle))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == State.AWAITING_VERIFICATION, result.failure
    for request in provider.requests:
        data = json.loads(request.user)["input"]
        if request.target == Target.FRONTEND and request.stage == Stage.EDITS:
            decision = data["decision"]
            path = data["permitted_test_paths"][0]
            expected = required_subject_import(path, decision["selected_source_path"])
            assert data["required_subject_import"] == expected == expected_import
            assert data["file_language"][path] == {"language": "typescript", "jsx_allowed": True}
            assert "exactly the host-provided required_subject_import" in request.system
            assert "actual captured exports/signatures" in request.system
            event = next(json.loads(e) for e in result.trace
                         if json.loads(e)["event"] == "decision-accepted")
            assert event["required_subject_import"] == expected
            assert "required_subject_import" not in request.response_schema["properties"]
        elif request.target == Target.BACKEND:
            assert "required_subject_import" not in data
            assert "host-provided required_subject_import" not in request.system


@pytest.mark.parametrize("location", ["decision", "edits", "edit-item"])
def test_model_cannot_override_host_import(tmp_path, location):
    bundle = bundle_for("frontend-generate")
    replies = list(transcript(bundle))
    index = 0 if location == "decision" else 1
    response = json.loads(replies[index])
    obj = response["edits"][0] if location == "edit-item" else response
    obj["required_subject_import"] = "./neighbor"
    replies[index] = canonical(response).decode()
    provider = FakeProvider(tuple(replies))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == State.FAILED and result.diagnostic == Diagnostic.SCHEMA
    if index == 1:
        assert json.loads(provider.requests[1].user)["input"]["required_subject_import"] == "./SubmitButton"


@pytest.mark.parametrize("test,source", [
    ("frontend/src/module.test.ts", "frontend/src/module.ts"),
    ("frontend/src/module.test.tsx", "frontend/src/module.tsx"),
    ("frontend/src/checks/module.test.ts", "frontend/src/module.ts"),
    ("frontend/src/module.test.ts", "frontend/src/sub/module.ts"),
])
@pytest.mark.parametrize("import_kind", ["canonical", "wrong-relative", "neighbor"])
def test_independent_structural_resolution(parser_image, tmp_path, test, source, import_kind):
    image, env = parser_image
    root = tmp_path / "input"
    root.mkdir(mode=0o755)
    required = required_subject_import(test, source)
    specifier = required if import_kind == "canonical" else "./missing" if import_kind == "wrong-relative" else "./neighbor"
    content = ("import { it, expect } from 'vitest'\n"
               f"import {{ value }} from '{specifier}'\n"
               "it('uses captured export', () => expect(value).toBe(1))\n")
    for path, text in {source: "export const value = 1\n", test: content,
                       test.rsplit("/", 1)[0] + "/neighbor.ts": "export const value = 2\n"}.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    contract = {"source": {"head_sha": "c" * 40}, "changes_sha256": "d" * 64,
                "path": test, "selected_source_path": source, "operation": "create",
                "content_sha256": digest(content.encode()), **language_contract(test)}
    (root / "structural-input.json").write_bytes(v.canonical([contract]))
    result, reports, error = v.Sandbox(v.Executor(), image, env).execute("frontend-patched-structural", root, ())
    assert error is None and not result.timed_out and reports == {}
    facts = v.validate_structural(v.parse_json(result.stdout), [contract])[0]
    expected = import_kind == "canonical"
    assert result.exit_code == 0 and facts["passed"]
    assert facts["subject_imported"] == expected
    assert facts["syntax_diagnostics"] == facts["jsx_nodes"] == 0
    assert facts["categories"] == ([] if expected else ["selected-subject-not-directly-imported"])
    assert (root / test).read_text() == content
