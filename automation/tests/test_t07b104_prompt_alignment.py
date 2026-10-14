"""Actual combined prompts and finite CREATE diagnostics; generated code is data."""

from dataclasses import replace
import json
import re

import pytest

from automation.test_agent import agent as a
from automation.test_agent.contracts import Target
from automation.test_agent.diagnostics import Diagnostic, EXPLANATIONS
from automation.test_agent.policy import PolicyError
from automation.test_agent.provider import FakeProvider, Stage, canonical
from automation.test_agent.skills import load_skill
from automation.tests.t04_fixtures import install_originals
from automation.tests.test_live_boundaries import full_stack_transcript
from automation.tests.test_t07b102_projection import manifest_bundle
from automation.tests.test_t07b103_host_imports import (
    BODY, SUBJECT, TEST, compiler_feedback, live_agent)


def assert_create_prompt(request):
    system = request.system
    assert a.UNIT_TEST_AGENT in system
    skill = load_skill('frontend-unit-tests', Target.FRONTEND)
    assert skill.record.version == '1.3.0' and skill.body in system
    assert 'CREATE (including CREATE repair): return only the structured response' in system
    assert 'subject_imports, package_imports, test_body plus binding and target.' in system
    assert 'The host constructs all imports and file bytes' in system
    assert 'Do not return a complete file or write repository-local import declarations.' in system
    assert 'UPDATE only: return complete updated code' in system
    # Every full-file instruction is explicitly scoped to backend or UPDATE.
    complete_lines = [line for line in system.splitlines() if re.search(r'return complete', line, re.I)]
    assert complete_lines == [
        '.test.tsx. For backend edits and frontend UPDATE, return complete file contents',
        'UPDATE only: return complete updated code as data under the existing UPDATE schema.']
    # No import declaration in any CREATE example, even if a future edit moves it.
    assert not re.search(r'^\s*import\s', system, re.M)
    assert not re.search(r'from\s+[\'"]\.{1,2}/', system)
    create_guidance = system.split('CREATE (including CREATE repair):')[1].split('UPDATE only:')[0]
    assert 'subject_imports: ["buildKey"]' in create_guidance
    assert 'replace with actual captured exports' in create_guidance
    assert 'Vitest' in system and 'meaningful assertions' in system and 'no real network' in system
    assert 'For .test.ts, JSX is forbidden' in system and 'React/RTL .test.tsx' in system
    schema_text = system.split('Response JSON schema (all fields required; no extra fields):\n')[1]
    props = json.loads(schema_text)['properties']
    assert set(props) == {'binding', 'target', 'subject_imports', 'package_imports', 'test_body'}
    assert props['subject_imports']['uniqueItems'] is True
    assert request.response_schema['properties'] == props
    assert 'content' not in props and 'replacement_content' not in props


def test_actual_full_pr2_create_and_repair_system_prompts(tmp_path):
    _, provider, agent = live_agent(tmp_path)
    assert agent.run().state == a.State.AWAITING_VERIFICATION
    assert_create_prompt(provider.requests[1])
    assert agent.repair(compiler_feedback(agent)).state == a.State.AWAITING_VERIFICATION
    assert_create_prompt(provider.requests[-1])
    assert 'Return COMPLETE replacement file content.' not in provider.requests[-1].system
    assert 'Return the complete replacement file only.' not in provider.requests[-1].system


def test_actual_update_keeps_complete_file_schema_original_sha_and_guidance(tmp_path):
    bundle = manifest_bundle()
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    result = a.UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == a.State.AWAITING_VERIFICATION
    request = provider.requests[1]
    assert request.stage == Stage.EDITS
    assert 'UPDATE only: return complete updated code as data under the existing UPDATE schema.' in request.system
    assert 'Preserve unrelated meaningful tests' in request.system
    assert 'Echo the exact original SHA-256' in request.system
    props = request.response_schema['properties']
    assert set(props) == {'binding', 'target', 'edits'}
    assert 'content' in props['edits']['items']['properties']
    data = json.loads(request.user)['input']
    edit = result.proposal.edits[0]
    assert edit.operation == 'update' and edit.original_sha256 == data['original_sha256'][edit.path]


@pytest.mark.parametrize('field,value,diagnostic,rejected', [
    ('subject_imports', ['nonExistentFunction'], Diagnostic.FRONTEND_CREATE_SUBJECT, 'nonExistentFunction'),
    ('package_imports', [{'module': 'unlisted-package', 'names': ['it']}], Diagnostic.FRONTEND_CREATE_PACKAGE, 'unlisted-package'),
    ('package_imports', [{'module': 'vitest/config', 'names': ['it']}], Diagnostic.FRONTEND_CREATE_PACKAGE, 'vitest/config'),
    ('test_body', "import { todoKeys } from '../queryKeys'\n" + BODY, Diagnostic.FRONTEND_CREATE_BODY, '../queryKeys'),
    ('test_body', "const keys = require('../queryKeys');\n" + BODY, Diagnostic.FRONTEND_CREATE_BODY, '../queryKeys'),
    ('test_body', "const keys = import('../queryKeys');\n" + BODY, Diagnostic.FRONTEND_CREATE_BODY, '../queryKeys'),
    ('test_body', 'const secretMarker = eval(value);\n' + BODY, Diagnostic.FRONTEND_CREATE_BODY, 'secretMarker'),
    ('test_body', 'const secretMarker = new Function(value);\n' + BODY, Diagnostic.FRONTEND_CREATE_BODY, 'secretMarker'),
    ('test_body', 'const secretMarker = `literal`;\n' + BODY, Diagnostic.FRONTEND_CREATE_BODY, 'secretMarker'),
    ('subject_imports', 'secretMarker', Diagnostic.FRONTEND_CREATE_SHAPE, 'secretMarker'),
    ('subject_imports', ['todoKeys', 'todoKeys'], Diagnostic.FRONTEND_CREATE_SHAPE, 'todoKeys'),
    ('package_imports', [{'module': 'vitest', 'names': ['todoKeys']}], Diagnostic.FRONTEND_CREATE_SHAPE, 'todoKeys'),
    ('package_imports', [{'module': 'vitest', 'names': ['it', 'it']}], Diagnostic.FRONTEND_CREATE_SHAPE, 'vitest'),
    ('package_imports', [{'module': 'vitest', 'names': ['it as secretMarker']}], Diagnostic.FRONTEND_CREATE_SHAPE, 'secretMarker'),
    ('package_imports', [{'module': 'vitest', 'names': ['it'], 'secretMarker': True}], Diagnostic.FRONTEND_CREATE_SHAPE, 'secretMarker'),
    ('test_body', None, Diagnostic.FRONTEND_CREATE_SHAPE, 'secretMarker'),
    ('secretMarker', True, Diagnostic.FRONTEND_CREATE_SHAPE, 'secretMarker'),
])
@pytest.mark.parametrize('repair', [False, True])
def test_exact_create_diagnostic_survives_run_and_repair_without_raw_content(
        tmp_path, field, value, diagnostic, rejected, repair):
    change = lambda response: {**response, field: value}
    _, provider, agent = live_agent(tmp_path, **({'repair_change': change} if repair else {'change': change}))
    result = agent.run()
    if repair:
        assert result.state == a.State.AWAITING_VERIFICATION
        result = agent.repair(compiler_feedback(agent))
    assert result.state == a.State.FAILED and result.diagnostic is diagnostic
    assert result.provider_calls == len(provider.requests) == (5 if repair else 2)
    if repair:
        assert result.proposal is not None
    else:
        assert result.proposal is None
    # Rejected fields/code must never enter failure/trace, or the public explanation.
    public = canonical({'failure': result.failure, 'trace': result.trace,
                        'diagnostic': result.diagnostic, 'explanation': EXPLANATIONS[result.diagnostic]}).decode()
    assert rejected not in public
    assert not (tmp_path / TEST).exists()


@pytest.mark.parametrize('kind,diagnostic', [
    ('subject', Diagnostic.FRONTEND_CREATE_SUBJECT),
    ('package', Diagnostic.FRONTEND_CREATE_PACKAGE),
])
def test_unusable_host_evidence_has_precise_diagnostic_before_edit_inference(tmp_path, kind, diagnostic):
    bundle, provider, _ = live_agent(tmp_path)
    if kind == 'subject':
        bundle = replace(bundle, sources=tuple(replace(e, content='const localOnly = 1')
                         if e.path == SUBJECT else e for e in bundle.sources))
    else:
        bundle = replace(bundle, configuration=tuple(replace(e, state='clipped')
                         if e.path == 'frontend/package.json' else e for e in bundle.configuration))
    # Echo actual request binding after trusted fixture evidence changes.
    original = provider.infer
    def infer(request, timeout):
        value = json.loads(original(request, timeout))
        return canonical({**value, 'binding': request.binding}).decode()
    provider.infer = infer
    result = a.UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.diagnostic is diagnostic and result.provider_calls == 1
    assert result.proposal is None


def test_ordinary_policy_error_outside_create_keeps_path_diagnostic(tmp_path, monkeypatch):
    bundle = manifest_bundle()
    install_originals(bundle, tmp_path)
    provider = FakeProvider(full_stack_transcript(bundle))
    def reject_path(*_):
        raise PolicyError('test path rejected')
    monkeypatch.setattr(a, 'test_path', reject_path)
    result = a.UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == a.State.FAILED and result.diagnostic is Diagnostic.PATH
    assert result.provider_calls == 1
