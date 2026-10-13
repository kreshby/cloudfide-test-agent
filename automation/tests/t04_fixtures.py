"""Trusted synthetic context and finite transcripts. Proposed code stays data."""

from dataclasses import asdict
import difflib

from automation.benchmarks.scenarios import SCENARIOS
from automation.test_agent.agent import project_context
from automation.test_agent.context import (CandidateTest, ChangedFile, ContextBundle,
                                          Evidence, WorkflowIdentity)
from automation.test_agent.contracts import Target
from automation.test_agent.provider import Stage, canonical, digest, strict_json


FRONTEND_CODE = """import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { SubmitButton } from './SubmitButton'

it('disables whitespace names and permits a named submission', () => {
  const onSubmit = vi.fn()
  const { rerender } = render(<SubmitButton name="   " onSubmit={onSubmit} />)
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  rerender(<SubmitButton name="Alice" onSubmit={onSubmit} />)
  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
})
"""
BACKEND_CODE = """from fastapi.testclient import TestClient
from app.tasks import app
client = TestClient(app)

def test_zero_limit():
    response = client.get('/tasks?limit=0')
    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['query', 'limit']
"""
COUNT_CODE = """import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { TaskCount } from './TaskCount'

it('hides zero tasks and renders positive counts', () => {
  const { rerender } = render(<TaskCount count={0} />)
  expect(screen.queryByText('0 tasks')).toBeNull()
  rerender(<TaskCount count={2} />)
  expect(screen.getByText('2 tasks')).toBeInTheDocument()
})
"""
GREETING_CODE = """from fastapi.testclient import TestClient
from app.greeting import app

def test_greeting():
    with TestClient(app) as client:
        response = client.get('/greeting?name=Ada')
    assert response.status_code == 200
    assert response.json() == {'message': 'Hello Ada'}
"""


def bundle_for(case):
    scenario = next(s for s in SCENARIOS if s.id == case)
    identity = scenario.source
    sources, changed = [], []
    for path, old, new in scenario.sources:
        patch = '\n'.join(list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=''))[2:])
        adds = sum(line.startswith('+') for line in patch.splitlines())
        deletes = sum(line.startswith('-') for line in patch.splitlines())
        changed.append(ChangedFile(path, None, 'modified' if old else 'added', adds, deletes,
                                   adds + deletes, 'e' * 40, identity.head_sha, 'complete', patch,
                                   len(patch.encode()), 'supported'))
        sources.append(Evidence(path, identity.head_sha, 'e' * 40, 'source', ('exact-changed-file',),
                                'available', len(new.encode()), new))
    tests = tuple(Evidence(path, identity.head_sha, 'f' * 40, 'existing-test',
                           ('named-module-test:' + next(p for p, _, _ in scenario.sources
                                                       if p.split('/')[0] == path.split('/')[0]),), 'available',
                           len(content.encode()), content) for path, content in scenario.existing_tests)
    existing = {path for path, _ in scenario.existing_tests}
    candidates = tuple(CandidateTest(path, ('named-module-test',), path in existing)
                       for path in scenario.candidates)
    config = (Evidence('frontend/src/test/setup.ts', identity.head_sha, 'f' * 40, 'configuration',
                       ('known-config',), 'available', 48, "import '@testing-library/jest-dom/vitest'\n"),
              Evidence('backend/tests/conftest.py', identity.head_sha, 'f' * 40, 'configuration',
                       ('known-config',), 'available', 38, '# TestClient and isolated mocks in fixtures\n'))
    return ContextBundle(identity, WorkflowIdentity(42, 7, 1, 101, 'pull_request', identity.head_sha),
                         tuple(changed), tuple(sources), tests, (), config, candidates, (), (), ())


def decisions_for(bundle, actions=None):
    actions = actions or {}
    result = []
    for target in Target:
        if not any(f.path.startswith(str(target) + '/') for f in bundle.changed_files):
            continue
        existing = [e.path for e in bundle.existing_tests if e.path.startswith(str(target) + '/')]
        action = actions.get(str(target), 'update' if existing else 'generate')
        if any(f.path == 'frontend/src/clamp.ts' for f in bundle.changed_files) and target == Target.FRONTEND:
            action = actions.get(str(target), 'no-tests-needed')
        if any(f.path == 'backend/app/health.py' for f in bundle.changed_files) and target == Target.BACKEND:
            action = actions.get(str(target), 'no-tests-needed')
        paths = [c.path for c in bundle.candidate_tests if c.path.startswith(str(target) + '/')]
        result.append({'target': str(target), 'action': action, 'plan': {
            'changed_behavior': 'Validate observable boundary behavior',
            'existing_test_evidence': existing, 'rationale': 'Compare changed behavior with existing coverage',
            'intended_test_files': paths if action in ('generate', 'update') else [],
            'skill_ids': ['analyze-change', str(target) + '-unit-tests'],
        }})
    return result


def _binding(bundle, stage, target, payload):
    return digest(canonical({'source': asdict(bundle.source), 'context_sha256': bundle.sha256,
                             'stage': stage, 'target': target, 'input': payload}))


def transcript(bundle, decisions=None, feedback=None, recommendation='accept'):
    decisions = decisions or decisions_for(bundle)
    result = []
    for decision in decisions:
        target = Target(decision['target'])
        plan = decision['plan']
        projection = strict_json(project_context(bundle, target).decode(), 16384)
        response = {'binding': _binding(bundle, Stage.DECISION, target, projection),
                    'target': str(target), 'action': decision['action'],
                    **{k: v for k, v in plan.items() if k != 'skill_ids'}}
        result.append(canonical(response).decode())
        if decision['action'] not in ('generate', 'update'):
            continue
        projection = strict_json(project_context(bundle, target, plan['intended_test_files']).decode(), 16384)
        originals = {e['path']: e['content_sha256'] for e in projection['existing_tests']
                     if e['state'] == 'available' and e.get('content') is not None}
        payload = {'decision': decision, 'evidence': projection,
                   'permitted_test_paths': plan['intended_test_files'], 'original_sha256': originals}
        edits = []
        for path in plan['intended_test_files']:
            code = (COUNT_CODE if 'TaskCount' in path else FRONTEND_CODE if target == Target.FRONTEND
                    else GREETING_CODE if 'greeting' in path else
                    next(e.content for e in bundle.existing_tests if e.path == path)
                    if 'health' in path else BACKEND_CODE)
            edits.append({'path': path, 'operation': 'update' if path in originals else 'create',
                          'original_sha256': originals.get(path), 'content': code})
        result.append(canonical({'binding': _binding(bundle, Stage.EDITS, target, payload),
                                 'target': str(target), 'edits': edits}).decode())
    if feedback is not None:
        edits = []
        for raw in result:
            value = strict_json(raw, 16384)
            edits.extend([{**edit, 'target': value['target']} for edit in value.get('edits', [])])
        proposal = {'schema_version': 1, 'source': asdict(bundle.source), 'decisions': decisions, 'edits': edits}
        for target in Target:
            if not any(e['target'] == str(target) for e in edits):
                continue
            summary = {'status': feedback.status, 'records': [{
                'kind': r.kind, 'result': r.result, 'collected_tests': r.collected_tests,
                'exit_code': r.exit_code, 'output_bytes': r.output_bytes,
            } for r in feedback.verifier_results]}
            payload = {'proposal_sha256': digest(canonical(proposal)), 'verification': summary}
            result.append(canonical({'binding': _binding(bundle, Stage.VERIFICATION, target, payload),
                                     'target': str(target), 'recommendation': recommendation,
                                     'interpretation': 'Model recommends acceptance'}).decode())
    return tuple(result)


def install_originals(bundle, root):
    # Only trusted fixture originals are written; proposed content is never installed.
    for evidence in bundle.existing_tests:
        path = root / evidence.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(evidence.content)
        path.chmod(0o644)
