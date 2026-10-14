"""Full historical PR #2 create/repair contract; finite fakes, code stays data."""

from dataclasses import replace
import json

import pytest

from automation.test_agent.agent import (AgentError, MAX_FRONTEND_REPAIRS, MAX_PROVIDER_CALLS,
    MAX_STAGE_ROUNDS, State, UnitTestAgent, project_context)
from automation.test_agent.contracts import Target
from automation.test_agent.frontend_create import (assemble_frontend_create, direct_named_exports,
    frontend_create_contract)
from automation.test_agent.policy import PolicyError
from automation.test_agent.provider import FakeProvider, MAX_REQUEST_BYTES, Stage, canonical, digest
from automation.test_agent.repair import CompilerDiagnostic, CompilerFailure, MESSAGES
from automation.tests.t04_fixtures import decisions_for, install_originals, transcript
from automation.tests.test_live_boundaries import full_stack_transcript
from automation.tests.test_t07b102_projection import manifest_bundle


TEST = 'frontend/src/features/todos/api/queryKeys.test.ts'
SUBJECT = 'frontend/src/features/todos/api/queryKeys.ts'
BODY = "describe('query keys', () => {\n  it('retains the priority prefix', () => {\n    expect(todoKeys.priorityLists).toEqual(['todos', 'list', 'priority'])\n  })\n})\n"


def structured(body=BODY):
    return {'subject_imports': ['todoKeys'], 'package_imports': [
        {'module': 'vitest', 'names': ['describe', 'it', 'expect']}], 'test_body': body}


def live_create_transcript(bundle):
    decision = decisions_for(bundle)[0]
    decision['action'] = 'generate'
    decision['selected_source_path'] = SUBJECT
    decision['plan']['intended_test_files'] = [TEST]
    decision['plan']['existing_test_evidence'] = []
    responses = list(transcript(bundle, [decision]))
    value = json.loads(responses[1])
    responses[1] = canonical({**value, **structured()}).decode()
    # Retain the full-stack historical backend update verbatim. The bundle is
    # not reduced to queryKeys, and both backend inference stages still occur.
    return tuple(responses) + full_stack_transcript(bundle)[2:]


def live_agent(tmp_path, *, change=None, repair_change=None):
    bundle = manifest_bundle()
    install_originals(bundle, tmp_path)
    responses = list(live_create_transcript(bundle))
    if change:
        responses[1] = canonical(change(json.loads(responses[1]))).decode()
    provider = FakeProvider(tuple(responses))
    ordinary = provider.infer

    def infer(request, timeout):
        if request.stage != Stage.REPAIR:
            return ordinary(request, timeout)
        assert len(provider.requests) == 4 and 0 < timeout <= 600
        provider.requests.append(request)
        value = {'binding': request.binding, 'target': 'frontend',
                 **structured(BODY + '// repaired after compiler feedback\n')}
        return canonical(repair_change(value) if repair_change else value).decode()

    provider.infer = infer
    return bundle, provider, UnitTestAgent(bundle, tmp_path, provider)


def compiler_feedback(agent):
    result = agent.result()
    edit = next(e for e in result.proposal.edits if e.target == Target.FRONTEND)
    # Trusted synthetic compiler feedback exercises reconstruction, not a claim
    # that TypeScript, Vitest or generated code was executed in these tests.
    return CompilerFailure(result.proposal.source, edit.path, digest(result.policy.changes.to_bytes()),
        digest(edit.content.encode()), 'a' * 64,
        (CompilerDiagnostic('TS2345', 4, 1, MESSAGES['TS2345']),))


def test_full_pr2_exact_head_exports_path_and_host_assembly(tmp_path):
    bundle, provider, agent = live_agent(tmp_path)
    original = manifest_bundle()
    assert bundle == original  # All 11 changed files, sources/tests/config/fixtures.
    assert len(bundle.changed_files) == 11 and len(bundle.existing_tests) == 14
    contract = frontend_create_contract(bundle, TEST, SUBJECT)
    assert contract['available_subject_exports'] == ['todoKeys']
    assert contract['required_subject_import'] == './queryKeys'
    result = agent.run()
    assert result.state == State.AWAITING_VERIFICATION, result.failure
    assert [(r.stage, r.target) for r in provider.requests] == [
        (Stage.DECISION, Target.FRONTEND), (Stage.EDITS, Target.FRONTEND),
        (Stage.DECISION, Target.BACKEND), (Stage.EDITS, Target.BACKEND)]
    edit = next(e for e in result.proposal.edits if e.target == Target.FRONTEND)
    assert edit.path == TEST and edit.operation == 'create'
    assert edit.content == ("import { todoKeys } from './queryKeys'\n"
        "import { describe, expect, it } from 'vitest'\n\n" + BODY)
    assert edit.content.count("from './queryKeys'") == 1 and '../queryKeys' not in edit.content
    assert not (tmp_path / TEST).exists()  # Assembly is data, never executed/installed.
    request = provider.requests[1]
    data = json.loads(request.user)['input']
    assert {k: data[k] for k in contract} == contract
    props = request.response_schema['properties']
    assert set(props) == {'binding', 'target', 'subject_imports', 'package_imports', 'test_body'}
    assert props['subject_imports']['items'] == {'enum': ['todoKeys']}
    assert props['package_imports']['items']['properties']['module']['enum'] == contract['allowed_package_roots']
    assert 'nonExistentFunction' not in canonical(request.response_schema).decode()
    # Existing backend decision/content/bytes and projected evidence stay intact.
    backend_before = UnitTestAgent(bundle, tmp_path, FakeProvider(full_stack_transcript(bundle))).run()
    assert backend_before.state == State.AWAITING_VERIFICATION
    assert [e for e in result.proposal.edits if e.target == Target.BACKEND] == [
        e for e in backend_before.proposal.edits if e.target == Target.BACKEND]
    assert [d for d in result.proposal.decisions if d.target == Target.BACKEND] == [
        d for d in backend_before.proposal.decisions if d.target == Target.BACKEND]
    for request in provider.requests:
        assert len(request.to_bytes()) <= MAX_REQUEST_BYTES == 24576
        if request.target == Target.BACKEND:
            assert 'available_subject_exports' not in request.user
    assert project_context(bundle, Target.BACKEND) == project_context(original, Target.BACKEND)


@pytest.mark.parametrize('change,reason', [
    (lambda v: {**v, 'subject_imports': ['todoKeys', 'nonExistentFunction']}, 'subject export outside captured allowlist'),
    (lambda v: {**v, 'package_imports': [{'module': 'unlisted-package', 'names': ['it']}]}, 'package outside captured allowlist'),
    (lambda v: {**v, 'package_imports': [{'module': 'vitest/config', 'names': ['it']}]}, 'package outside captured allowlist'),
    (lambda v: {**v, 'test_body': "import { todoKeys } from '../queryKeys'\n" + BODY}, 'test body loader literal rejected'),
    (lambda v: {**v, 'required_subject_import': '../queryKeys'}, 'stage fields rejected'),
    (lambda v: {**v, 'path': 'frontend/src/neighbor.test.ts'}, 'stage fields rejected'),
])
def test_fake_create_revalidates_without_relying_on_native_schema(tmp_path, change, reason):
    _, provider, agent = live_agent(tmp_path, change=change)
    result = agent.run()
    assert result.state == State.FAILED and reason in result.failure
    assert result.proposal is None and len(provider.requests) == 2
    assert not (tmp_path / TEST).exists()


def test_one_repair_uses_same_structured_host_contract(tmp_path):
    bundle, provider, agent = live_agent(tmp_path)
    initial = agent.run()
    assert initial.state == State.AWAITING_VERIFICATION
    before = next(e for e in initial.proposal.edits if e.target == Target.FRONTEND)
    result = agent.repair(compiler_feedback(agent))
    assert result.state == State.AWAITING_VERIFICATION
    after = next(e for e in result.proposal.edits if e.target == Target.FRONTEND)
    assert result.proposal.decisions == initial.proposal.decisions
    assert after.path == before.path == TEST
    assert after.content.startswith("import { todoKeys } from './queryKeys'\n")
    assert '../queryKeys' not in after.content and 'repaired after compiler feedback' in after.content
    data = json.loads(provider.requests[-1].user)['input']
    assert data['accepted_path'] == TEST and data['selected_source_path'] == SUBJECT
    contract = frontend_create_contract(bundle, TEST, SUBJECT)
    assert {k: data[k] for k in contract} == contract
    assert provider.requests[-1].response_schema['properties']['subject_imports']['items']['enum'] == ['todoKeys']
    assert 'replacement_content' not in provider.requests[-1].response_schema['properties']
    assert MAX_FRONTEND_REPAIRS == MAX_STAGE_ROUNDS == 1 and MAX_PROVIDER_CALLS == 5
    assert result.provider_calls == 5
    assert agent.repair(compiler_feedback(agent)).state == State.FAILED
    assert len(provider.requests) == 5
    with pytest.raises(AgentError):
        agent.repair(compiler_feedback(agent))


@pytest.mark.parametrize('change', [
    lambda v: {**v, 'accepted_path': 'frontend/src/neighbor.test.ts'},
    lambda v: {**v, 'selected_source_path': 'frontend/src/features/todos/api/todos.ts'},
    lambda v: {**v, 'required_subject_import': '../queryKeys'},
    lambda v: {**v, 'replacement_content': "import { todoKeys } from '../queryKeys'\n" + BODY},
    lambda v: {**v, 'subject_imports': ['todoKeys', 'nonExistentFunction']},
    lambda v: {**v, 'test_body': "import { todoKeys } from '../queryKeys'\n" + BODY},
    lambda v: {**v, 'package_imports': [{'module': 'unlisted-package', 'names': ['it']}]},
])
def test_repair_cannot_change_host_facts_or_bypass_imports(tmp_path, change):
    _, provider, agent = live_agent(tmp_path, repair_change=change)
    initial = agent.run()
    assert initial.state == State.AWAITING_VERIFICATION
    result = agent.repair(compiler_feedback(agent))
    assert result.state == State.FAILED and result.proposal == initial.proposal
    assert len(provider.requests) == 5 and not (tmp_path / TEST).exists()


@pytest.mark.parametrize('content,expected', [
    ('export const value = 1', ['value']), ('export let counter: number = 0', ['counter']),
    ('export function calculate() { return 1 }', ['calculate']),
    ('export class Widget {}', ['Widget']), ('export type Entry = {id: string}', ['Entry']),
    ('export interface Entry { id: string }', ['Entry']), ('export enum Mode { On, Off }', ['Mode']),
    ('export const alpha = 1, beta = 2', ['alpha']),
    ('// export const invented = 1\nexport const real = "export const fake = 2"', ['real']),
    ('/* export function fake(){} */ export let real = 1', ['real']),
    ('function outer(){ const hidden = 1 }\nexport const real = 1', ['real']),
    ("export { remote } from './other'; export * from './other'; export default 1; export const real = 1", ['real']),
    ('export interface Zebra {}\nexport type Alpha = number; export const zebra = 1', ['Alpha', 'Zebra', 'zebra']),
])
def test_generic_bounded_direct_exports(content, expected):
    assert direct_named_exports(content) == expected


@pytest.mark.parametrize('content', [
    "export { foo } from './other'", "export * from './other'", 'export default function foo() {}',
    'const foo = 1; export { foo }', 'export const { foo } = {foo: 1}',
    'export const [foo] = [1]', 'export async function foo() {}', 'export declare const foo: number',
    'export namespace Names { export const fake = 1 }',
    'const text = `export const fake = 1`', 'const text = /export const fake = 1/',
    'const foo = 1; /* export const fake = 1 */', 'const foo = "export const fake = 1"',
    'const value = object.export\nconst fake = 1', 'const view = <div>export const fake = 1</div>',
    'export const foo = {', 'export const f\\u006fo = 1', 'export const café = 1',
    'export const value = 1\n' + ' ' * 4096,
])
def test_unsupported_ambiguous_or_absent_exports_fail_closed(content):
    with pytest.raises(PolicyError):
        direct_named_exports(content)


@pytest.mark.parametrize('mutation', ['stale', 'clipped', 'duplicate', 'wrong-category', 'absent', 'no-exports'])
def test_subject_exports_require_unique_complete_exact_head_evidence(mutation):
    bundle = manifest_bundle()
    entry = next(e for e in bundle.sources if e.path == SUBJECT and e.source_sha == bundle.source.head_sha)
    entries = list(bundle.sources)
    entries.remove(entry)
    if mutation == 'stale': entry = replace(entry, source_sha=bundle.source.base_sha)
    if mutation == 'clipped': entry = replace(entry, state='clipped')
    if mutation == 'wrong-category': entry = replace(entry, category='fixture')
    if mutation == 'no-exports': entry = replace(entry, content='const local = 1')
    if mutation != 'absent': entries.append(entry)
    if mutation == 'duplicate': entries.append(entry)
    with pytest.raises(PolicyError):
        frontend_create_contract(replace(bundle, sources=tuple(entries)), TEST, SUBJECT)


@pytest.mark.parametrize('prefix', [
    "import '../queryKeys';", "import type { Key } from './queryKeys';", "export {todoKeys};",
    "export * from '../queryKeys';", "import keys = require('../queryKeys');",
    "const keys = require('../queryKeys');", "const keys = import('../queryKeys');",
    "const keys = import(moduleName);", "const load = require;", "globalThis['require']('../queryKeys');",
    "globalThis['\\u0072equire']('../queryKeys');",
    "vi.importActual('../queryKeys');", "vi.mock('./queryKeys');",
    "eval(\"import('../queryKeys')\");", "new Function('return require(1)')();",
    "const text = `${import('../queryKeys')}`;", "im\\u0070ort('../queryKeys');",
    "const fake = /export/;",
])
def test_body_cannot_supply_module_declarations_or_loaders(prefix):
    contract = frontend_create_contract(manifest_bundle(), TEST, SUBJECT)
    with pytest.raises(PolicyError):
        assemble_frontend_create(structured(prefix + '\n' + BODY), contract)


def test_deterministic_order_bounds_and_verifier_responsibility():
    contract = frontend_create_contract(manifest_bundle(), TEST, SUBJECT)
    value = structured()
    first = assemble_frontend_create(value, contract)
    value['package_imports'][0]['names'].reverse()
    assert assemble_frontend_create(value, contract) == first
    # Existence of named package exports, identifier use, and assertion semantics
    # stay with the compiler/verifier. Host only permits the package root.
    value['package_imports'][0]['names'] = ['notARealPackageExport']
    assert 'notARealPackageExport' in assemble_frontend_create(value, contract)
    for subjects in ([], ['todoKeys', 'todoKeys'], ['todoKeys as alias'], [1]):
        with pytest.raises(PolicyError):
            assemble_frontend_create({**structured(), 'subject_imports': subjects}, contract)
    for packages in ([{'module': 'vitest', 'names': ['it as alias']}],
                     [{'module': 'vitest', 'names': ['todoKeys']}],
                     [{'module': 'vitest', 'names': ['it']}] * 2):
        with pytest.raises(PolicyError):
            assemble_frontend_create({**structured(), 'package_imports': packages}, contract)


def test_export_and_body_bounds_and_empty_manifest_allowlist():
    with pytest.raises(PolicyError):
        direct_named_exports('\n'.join(f'export const symbol{i} = 1' for i in range(65)))
    with pytest.raises(PolicyError):
        direct_named_exports('export const ' + 'a' * 129 + ' = 1')
    contract = frontend_create_contract(manifest_bundle(), TEST, SUBJECT)
    for body in ('', None, BODY + ' ' * 16384):
        with pytest.raises(PolicyError):
            assemble_frontend_create(structured(body), contract)
    contract['allowed_package_roots'] = []
    with pytest.raises(PolicyError):
        assemble_frontend_create(structured(), contract)
    from automation.test_agent.protocol import stage_schema
    schema = stage_schema(Stage.EDITS, Target.FRONTEND, 'a' * 64,
        {**contract, 'test_operation': 'create'})
    assert schema['properties']['package_imports']['maxItems'] == 0
