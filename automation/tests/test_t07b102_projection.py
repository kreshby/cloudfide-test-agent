"""Full PR #2 context with host-only manifest evidence; no real inference."""

from dataclasses import replace
import json

from automation.test_agent.agent import MAX_FRONTEND_REPAIRS, MAX_PROJECTION_BYTES, State, UnitTestAgent, project_context
from automation.test_agent.context import Evidence
from automation.test_agent.contracts import Target
from automation.test_agent.import_contract import captured_package_roots, frontend_import_contract
from automation.test_agent.provider import FakeProvider, MAX_REQUEST_BYTES, Stage, canonical, digest
from automation.test_agent.repair import CompilerDiagnostic, CompilerFailure, MESSAGES
from automation.tests.t04_fixtures import install_originals
from automation.tests.t07b2_fixtures import FRONTEND_TEST, live_bundle
from automation.tests.test_live_boundaries import full_stack_transcript
from automation.tests.test_t07b101_import_contract import MANIFEST


SUBJECT = 'frontend/src/features/todos/api/todos.ts'


def manifest_bundle():
    # The historical fixture predates manifest capture. Add the same exact-head
    # evidence as B10.1, keeping every represented changed file and evidence item.
    bundle = live_bundle()
    manifest = Evidence('frontend/package.json', bundle.source.head_sha, 'f' * 40,
        'configuration', ('known-test-configuration',), 'available', len(MANIFEST.encode()), MANIFEST)
    return replace(bundle, configuration=(*bundle.configuration, manifest))


def assert_frontend_evidence(raw, bundle):
    assert len(raw) <= MAX_PROJECTION_BYTES
    projection = json.loads(raw)
    assert b'frontend/package.json' not in raw
    assert canonical(MANIFEST) not in raw
    expected = {e.path: e.content for e in bundle.configuration
                if e.path.startswith('frontend/') and e.path != 'frontend/package.json'}
    assert {'frontend/vite.config.ts', 'frontend/src/test/setup.ts'} <= expected.keys()
    assert {e['path']: e['content'] for e in projection['configuration']} == expected
    assert {f['path'] for f in projection['changed_files']} == {
        f.path for f in bundle.changed_files if f.path.startswith('frontend/')}
    return projection


def test_full_live_context_keeps_manifest_host_only_in_decision_and_edits():
    original = live_bundle()
    bundle = manifest_bundle()
    assert replace(bundle, configuration=original.configuration) == original
    assert sum(f.path.startswith('frontend/') for f in bundle.changed_files) == 10
    manifest, = (e for e in bundle.configuration if e.path == 'frontend/package.json')
    assert manifest.source_sha == bundle.source.head_sha
    assert manifest.category == 'configuration' and manifest.state == 'available'
    assert manifest.content == MANIFEST and manifest.observed_bytes == len(MANIFEST.encode())
    roots = captured_package_roots(bundle)
    assert {'vitest', '@testing-library/react', '@tanstack/react-query'} <= set(roots)
    assert '@testing-library/react-hooks' not in roots
    assert_frontend_evidence(project_context(bundle, Target.FRONTEND), bundle)
    edits = assert_frontend_evidence(
        project_context(bundle, Target.FRONTEND, [FRONTEND_TEST], SUBJECT), bundle)
    assert any(e['path'] == SUBJECT and e['state'] == 'available' for e in edits['sources'])
    assert any(e['path'] == FRONTEND_TEST and e['state'] == 'available' for e in edits['existing_tests'])
    assert frontend_import_contract(bundle, FRONTEND_TEST, SUBJECT) == {
        'test_path': FRONTEND_TEST, 'selected_source_path': SUBJECT,
        'required_subject_import': './todos', 'allowed_package_roots': roots}
    # Adding host-only frontend evidence changes the bundle digest, but no
    # backend projection evidence or ordering (for decision or edit stages).
    for intended in (None, ['backend/tests/features/todos/test_router.py']):
        before = json.loads(project_context(original, Target.BACKEND, intended))
        after = json.loads(project_context(bundle, Target.BACKEND, intended))
        before.pop('context_sha256')
        after.pop('context_sha256')
        assert canonical(before) == canonical(after)


def test_full_live_fake_requests_keep_host_contract_through_one_repair(tmp_path):
    bundle = manifest_bundle()
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    infer = provider.infer

    def fake(request, timeout):
        if request.stage != Stage.REPAIR:
            return infer(request, timeout)
        provider.requests.append(request)
        previous = json.loads(request.user)['input']['previous_content']
        return canonical({'binding': request.binding, 'target': 'frontend',
                          'replacement_content': previous + '\n// repaired fixture\n'}).decode()

    provider.infer = fake
    agent = UnitTestAgent(bundle, tmp_path, provider)
    initial = agent.run()
    assert initial.state == State.AWAITING_VERIFICATION, initial.failure
    edit, = (e for e in initial.proposal.edits if e.target == Target.FRONTEND)
    # Synthetic host feedback exercises request construction only, not a claim
    # that the generated application test or verifier was executed.
    feedback = CompilerFailure(bundle.source, edit.path, digest(initial.policy.changes.to_bytes()),
        digest(edit.content.encode()), 'a' * 64,
        (CompilerDiagnostic('TS1005', 1, 1, MESSAGES['TS1005']),))
    repaired = agent.repair(feedback)
    assert repaired.state == State.AWAITING_VERIFICATION, repaired.failure
    requests = [r for r in provider.requests if r.target == Target.FRONTEND]
    assert [r.stage for r in requests] == [Stage.DECISION, Stage.EDITS, Stage.REPAIR]
    for request in requests:
        assert len(request.to_bytes()) <= MAX_REQUEST_BYTES
        data = json.loads(request.user)['input']
        evidence = data if request.stage == Stage.DECISION else data['evidence']
        assert_frontend_evidence(canonical(evidence), bundle)
        assert canonical(MANIFEST).decode() not in request.user
        if request.stage != Stage.DECISION:
            contract = frontend_import_contract(bundle, FRONTEND_TEST, SUBJECT)
            assert {key: data[key] for key in contract} == contract
    assert 'allowed_package_roots' not in requests[1].response_schema['properties']
    assert MAX_FRONTEND_REPAIRS == 1
    assert agent.repair(feedback).state == State.FAILED
    assert len(provider.requests) == 5
