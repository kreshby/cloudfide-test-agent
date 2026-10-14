"""B10.1 finite snapshot imports and the live query-key one-repair regression."""

from dataclasses import replace
import json
import shutil

import pytest

from automation.test_agent import agent as a, verification as v
from automation.test_agent.context import CandidateTest, Evidence
from automation.test_agent.contracts import Target
from automation.test_agent.import_contract import (MAX_PACKAGE_BYTES, MAX_PACKAGE_ROOTS,
    captured_package_roots, frontend_import_contract, package_roots)
from automation.test_agent.policy import PolicyError
from automation.test_agent.provider import FakeProvider, Stage, canonical, digest
from automation.tests.t04_fixtures import bundle_for, decisions_for, transcript
from automation.tests.t07b2_fixtures import live_bundle
from automation.tests.test_t07b8_repair import CompilerExecutor
from automation.tests.test_t07b9_structure import parser_image


TEST = 'frontend/src/features/todos/api/queryKeys.test.ts'
SUBJECT = 'frontend/src/features/todos/api/queryKeys.ts'
BAD = """import { describe, expect, it } from 'vitest'
import { todoKeys } from '../queryKeys'
import { renderHook } from '@testing-library/react-hooks'
describe('todoKeys', () => {
  it('provides the priority-list query-key prefix', () => {
    expect(todoKeys.priorityLists).toEqual(['todos', 'list', 'priority'])
  })
})
"""
GOOD = BAD.replace("'../queryKeys'", "'./queryKeys'").replace(
    "import { renderHook } from '@testing-library/react-hooks'\n", '')
MANIFEST = (v.TRUSTED_ROOT / 'frontend/package.json').read_text()


def test_manifest_roots_are_sorted_deduplicated_and_declared_only():
    assert package_roots(json.dumps({'dependencies': {'z': '1', 'same': '1'},
        'devDependencies': {'vitest': '1', 'same': '1'},
        'peerDependencies': {'@scope/peer': '1'}})) == ['@scope/peer', 'same', 'vitest', 'z']
    roots = package_roots(MANIFEST)
    assert {'vitest', '@testing-library/react', '@tanstack/react-query'} <= set(roots)
    assert '@testing-library/react-hooks' not in roots
    assert len(roots) <= MAX_PACKAGE_ROOTS and sum(len(p.encode()) for p in roots) <= MAX_PACKAGE_BYTES


@pytest.mark.parametrize('value', [
    'null', '[]', '{"dependencies":[]}', '{"dependencies":{"bad/name":"1"}}',
    '{"dependencies":{"@scope":"1"}}', '{"dependencies":{"ok":null}}',
    '{"dependencies":{"ok":"1","ok":"2"}}',
    json.dumps({'dependencies': {f'p{i}': '1' for i in range(65)}}),
    json.dumps({'dependencies': {'p' * 129: '1'}}),
    json.dumps({'dependencies': {f'p{i}' + 'a' * 120: '1' for i in range(20)}}),
    ' ' * 16385,
])
def test_malformed_or_overbound_manifest_fails_closed(value):
    with pytest.raises(PolicyError):
        package_roots(value)


@pytest.mark.parametrize('mutation', ['stale', 'clipped', 'missing', 'duplicate', 'arbitrary-manifest'])
def test_manifest_authority_is_exact_head_and_fixed_path(mutation):
    b = bundle_for('frontend-generate')
    e = next(e for e in b.configuration if e.path == 'frontend/package.json')
    if mutation == 'stale': e = replace(e, source_sha='9' * 40)
    if mutation == 'clipped': e = replace(e, state='clipped')
    if mutation == 'arbitrary-manifest': e = replace(e, path='frontend/src/package.json')
    entries = () if mutation == 'missing' else (e, e) if mutation == 'duplicate' else (e,)
    b = replace(b, configuration=entries)
    if mutation in {'missing', 'arbitrary-manifest'}:
        assert captured_package_roots(b) == []
    else:
        with pytest.raises(PolicyError): captured_package_roots(b)


@pytest.mark.parametrize('location', ['decision', 'edits', 'edit-item'])
def test_model_cannot_expand_manifest_allowlist(tmp_path, location):
    b = bundle_for('frontend-generate')
    responses = list(transcript(b))
    i = 0 if location == 'decision' else 1
    value = json.loads(responses[i])
    obj = value['edits'][0] if location == 'edit-item' else value
    obj['allowed_package_roots'] = ['@testing-library/react-hooks']
    responses[i] = canonical(value).decode()
    p = FakeProvider(tuple(responses))
    result = a.UnitTestAgent(b, tmp_path, p).run()
    assert result.state == a.State.FAILED
    if i == 1:
        data = json.loads(p.requests[1].user)
        assert data['source']['head_sha'] == b.source.head_sha
        assert data['input']['allowed_package_roots'] == package_roots(MANIFEST)
        assert 'allowed_package_roots' not in p.requests[1].response_schema['properties']


def parse_snapshot(parser_image, root, content, *, test=TEST, subject=SUBJECT, modules=None, manifest=MANIFEST):
    root.mkdir(mode=0o755)
    files = {'frontend/package.json': manifest, subject: 'export const value = 1\n',
             **(modules or {}), test: content}
    for p, text in files.items():
        dest = root / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    contract = {'source': {'head_sha': 'c' * 40}, 'changes_sha256': 'd' * 64,
        'path': test, 'selected_source_path': subject, 'operation': 'create',
        'content_sha256': digest(content.encode()), 'language': 'typescript', 'jsx_allowed': test.endswith('.tsx')}
    (root / 'structural-input.json').write_bytes(canonical([contract]))
    image, env = parser_image
    result, reports, error = v.Sandbox(v.Executor(), image, env).execute('frontend-patched-structural', root, ())
    assert error is None and not result.timed_out and not result.truncated and reports == {}
    assert (root / test).read_text() == content
    assert not (root / 'executed').exists()
    fact = v.validate_structural(v.parse_json(result.stdout), [contract])[0]
    assert result.exit_code == (0 if fact['passed'] else 1)
    return fact


@pytest.mark.parametrize('specifier,module', [
    ('./queryKeys', SUBJECT), ('./queryKeys.ts', SUBJECT),
    ('./widget', 'frontend/src/features/todos/api/widget.tsx'),
    ('./widget.tsx', 'frontend/src/features/todos/api/widget.tsx'),
    ('../types', 'frontend/src/features/todos/types.ts'),
    ('./keys', 'frontend/src/features/todos/api/keys/index.ts'),
    ('./keys', 'frontend/src/features/todos/api/keys/index.tsx'),
    ('./keys.js', 'frontend/src/features/todos/api/keys.js'),
    ('./keys.jsx', 'frontend/src/features/todos/api/keys.jsx'),
    ('./keys.json', 'frontend/src/features/todos/api/keys.json'),
    ('./keys', 'frontend/src/features/todos/api/keys/index.js'),
    ('./keys', 'frontend/src/features/todos/api/keys/index.jsx'),
])
def test_finite_relative_candidates(parser_image, tmp_path, specifier, module):
    code = f"import {{ value }} from '{specifier}'\n"
    fact = parse_snapshot(parser_image, tmp_path/'input', code, subject=module)
    assert fact['passed'] and fact['subject_imported'] and fact['categories'] == []


@pytest.mark.parametrize('specifier', ['../queryKeys', './missing', '../../../../../outside',
    '../../../../../frontend/src/features/todos/api/queryKeys',
    '../node_modules/vitest', './node_modules/vitest', '.bad', './bad//path', './bad%2fpath'])
@pytest.mark.parametrize('statement', ['import { value } from', 'import type { Value } from', 'export { value } from', 'import'])
def test_all_static_relative_imports_exports_are_checked(parser_image, tmp_path, specifier, statement):
    fact = parse_snapshot(parser_image, tmp_path/'input', f"{statement} '{specifier}'\n")
    assert not fact['passed'] and 'unresolved-relative-import' in fact['categories']


@pytest.mark.parametrize('specifier', ['vitest', 'vitest/config', '@testing-library/react',
    '@testing-library/react/something', '@tanstack/react-query', 'react', '@scope/peer'])
def test_declared_dependency_devdependency_peer_and_subpaths(parser_image, tmp_path, specifier):
    manifest = json.loads(MANIFEST); manifest['peerDependencies'] = {'@scope/peer': '1'}
    fact = parse_snapshot(parser_image, tmp_path/'input', f"import '{specifier}'\n", manifest=json.dumps(manifest))
    assert fact['passed'] and fact['categories'] == ['selected-subject-not-directly-imported']


@pytest.mark.parametrize('specifier', ['@testing-library/react-hooks', 'missing', '@scope',
    '@scope/', 'vitest//config', 'vitest/../react', '@scope/../react', 'https://example.com/pkg',
    'node:fs', '#alias', 'vitest?x', ' vitest', 'vitest/'])
def test_unknown_and_malformed_packages_fail_closed(parser_image, tmp_path, specifier):
    fact = parse_snapshot(parser_image, tmp_path/'input', f"import '{specifier}'\n")
    assert not fact['passed'] and 'unknown-package-import' in fact['categories']


def test_candidate_order_exact_then_ts_then_tsx_then_index(parser_image, tmp_path):
    paths = ['frontend/src/features/todos/api/keys' + suffix
             for suffix in ('', '.ts', '.tsx', '/index.ts', '/index.tsx')]
    # A file and directory cannot share a name. Test the extensionless exact case separately.
    exact = parse_snapshot(parser_image, tmp_path/'exact', "import { value } from './keys'\n",
        subject=paths[0], modules={paths[1]: 'export const value = 2'})
    assert exact['subject_imported']
    modules = {p: 'export const value = 2' for p in paths[1:]}
    first = parse_snapshot(parser_image, tmp_path/'ts', "import { value } from './keys'\n",
        subject=paths[1], modules=modules)
    second = parse_snapshot(parser_image, tmp_path/'tsx', "import { value } from './keys'\n",
        subject=paths[2], modules=modules)
    assert first['passed'] and first['subject_imported']
    assert second['passed'] and not second['subject_imported']  # Advisory, first candidate wins.


def test_valid_indirect_behavior_and_no_execution(parser_image, tmp_path):
    code = """import { describe, expect, it } from 'vitest'
import { value } from './barrel'
it('behavior', () => expect(value).toBe(1))
throw new Error('MUST NEVER EXECUTE')
"""
    fact = parse_snapshot(parser_image, tmp_path/'input', code,
        modules={'frontend/src/features/todos/api/barrel.ts': "export { todoKeys as value } from './queryKeys'"})
    assert fact['passed'] and fact['categories'] == ['selected-subject-not-directly-imported']


@pytest.mark.parametrize('specifier,category', [
    ('./node_modules/vitest', 'unresolved-relative-import'),
    ('injected-package', 'unknown-package-import'),
])
def test_node_modules_and_other_manifests_cannot_authorize_imports(parser_image, tmp_path, specifier, category):
    assert not v.admitted('frontend/src/features/todos/api/node_modules/vitest/index.ts')
    assert not v.admitted('frontend/node_modules/injected-package/index.ts')
    fact = parse_snapshot(parser_image, tmp_path/'input', f"import '{specifier}'\n", modules={
        'frontend/src/features/todos/api/node_modules/vitest/index.ts': 'throw new Error("DO NOT LOAD")',
        'frontend/src/package.json': '{"dependencies":{"injected-package":"1"}}',
    })
    assert not fact['passed'] and category in fact['categories']


def live_query_keys_bundle():
    b = live_bundle()
    manifest = Evidence('frontend/package.json', b.source.head_sha, 'f'*40, 'configuration',
        ('known-test-configuration',), 'available', len(MANIFEST.encode()), MANIFEST)
    return replace(b, changed_files=tuple(f for f in b.changed_files if f.path == SUBJECT),
        sources=tuple(e for e in b.sources if e.path == SUBJECT), existing_tests=(), fixtures=(),
        configuration=(manifest,), candidate_tests=(CandidateTest(TEST, ('named-module-test',), False),))


@pytest.mark.parametrize('replacement,expected', [(GOOD, 'verified'),
    (BAD + '// changed but imports still invalid\n', 'verification-failed')])
def test_exact_live_regression_and_one_repair(parser_image, tmp_path, replacement, expected):
    before = parse_snapshot(parser_image, tmp_path/'before', BAD)
    assert before['syntax_diagnostics'] == before['jsx_nodes'] == 0
    assert not before['passed'] and {'unresolved-relative-import', 'unknown-package-import'} <= set(before['categories'])
    after = parse_snapshot(parser_image, tmp_path/'after', GOOD)
    assert after['passed'] and after['subject_imported'] and after['categories'] == []

    b = live_query_keys_bundle()
    responses = list(transcript(b, decisions_for(b)))
    value = json.loads(responses[1]); value['edits'][0]['content'] = BAD
    responses[1] = canonical(value).decode()
    p = FakeProvider(tuple(responses))
    infer = p.infer
    def fake(request, timeout):
        if request.stage != Stage.REPAIR: return infer(request, timeout)
        assert len(p.requests) == 2
        p.requests.append(request)
        data = json.loads(request.user)['input']
        assert {k: data[k] for k in frontend_import_contract(b, TEST, SUBJECT)} == frontend_import_contract(b, TEST, SUBJECT)
        assert [d['code'] for d in data['diagnostics']] == ['UNRESOLVED_RELATIVE_IMPORT', 'UNKNOWN_PACKAGE_IMPORT']
        assert all(d['message'] == v.MESSAGES[d['code']] for d in data['diagnostics'])
        assert 'stdout' not in data and 'stderr' not in data
        assert 'do not invent dependencies' in request.system.lower()
        assert 'allowed_package_roots' in request.system and 'required_subject_import' in request.system
        return canonical({'binding': request.binding, 'target': 'frontend', 'replacement_content': replacement}).decode()
    p.infer = fake
    root = tmp_path/'checkout'; root.mkdir()
    files = {path: v.safe_read(v.TRUSTED_ROOT, path) for path in v.TRUSTED_FILES}
    files[SUBJECT] = (b.sources[0].content.encode(), 0o644)
    files['frontend/src/base.test.ts'] = (b"import {it,expect} from 'vitest'\nit('base',()=>expect(1).toBe(1))\n", 0o644)
    for path, (content, mode) in files.items():
        dest = root/path; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(content); dest.chmod(mode)
    executor = CompilerExecutor(root, b.source, files, changed=(SUBJECT,))
    original = executor.run
    image, env = parser_image
    def run(argv, **kwargs):
        if argv[:3] == v.DOCKER and argv[3] == 'exec' and argv[8] == 'frontend-patched-structural':
            result, _, error = v.Sandbox(v.Executor(), image, env).execute(
                'frontend-patched-structural', executor.current['root'], ())
            assert error is None
            return result
        return original(argv, **kwargs)
    executor.run = run
    agent = a.UnitTestAgent(b, root, p)
    assert agent.run().state == a.State.AWAITING_VERIFICATION
    reports = []
    def verify():
        report = v.Verifier(executor=executor).verify(b.source, root, agent.result().policy.changes,
            profile_ids=('frontend-v1',), image=executor.image)
        reports.append(report)
        return report
    try:
        first = verify()
        assert first.commands[-1].profile_id == 'frontend-patched-structural'
        assert first.host_result.status == 'verification-failed' and first.compiler_failure is not None
        assert agent.repair(first.compiler_failure).state == a.State.AWAITING_VERIFICATION
        second = verify()
        assert second.host_result.status == expected
        if expected == 'verification-failed':
            assert second.commands[-1].profile_id == 'frontend-patched-structural'
            assert second.compiler_failure is not None
        else:
            assert second.compiler_failure is None
        assert agent.resume(second.host_result).outcome == expected
        assert len(p.requests) == 3 and sum(r.stage == Stage.REPAIR for r in p.requests) == 1
        assert a.MAX_FRONTEND_REPAIRS == a.MAX_STAGE_ROUNDS == 1 and a.MAX_PROVIDER_CALLS == 5
        with pytest.raises(a.AgentError): agent.repair(first.compiler_failure)
        assert len(p.requests) == 3 and not (root/TEST).exists()
    finally:
        for report in reports: shutil.rmtree(report.output_directory)
