"""B9 host contracts and pinned compiler AST fixtures; no model inference."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from automation.test_agent import verification as v
from automation.test_agent.agent import State, UnitTestAgent, project_context
from automation.test_agent.contracts import Target
from automation.test_agent.provider import FakeProvider, Stage, canonical, digest
from automation.test_agent.skills import MAX_SKILL_BYTES, REGISTRY, load_skill
from automation.test_agent.context import CandidateTest
from automation.test_agent.subjects import captured_frontend_subjects
from automation.tests.t04_fixtures import bundle_for, decisions_for, install_originals, transcript
from automation.tests.test_t07b8_repair import rig, failing, CompilerExecutor
from automation.tests.test_verification import capture


def test_authenticated_canonical_patterns_and_backend_unchanged(tmp_path):
    skill = load_skill('frontend-unit-tests', Target.FRONTEND)
    assert len(skill.body.encode()) <= MAX_SKILL_BYTES == 2048
    assert skill.record.version == REGISTRY['frontend-unit-tests'].version == '1.2.0'
    assert skill.record.content_sha256 == digest(skill.body.encode()) == REGISTRY['frontend-unit-tests'].sha256
    assert 'Pure .test.ts pattern' in skill.body and "import { buildKey } from './buildKey'" in skill.body
    pure = skill.body.split('```ts\n')[1].split('```')[0]
    assert '<' not in pure and 'render(' not in pure and "toEqual(['items', 'high'])" in pure
    assert 'React/RTL .test.tsx pattern' in skill.body and '<StatusBadge status="done" />' in skill.body
    assert "import { StatusBadge } from './StatusBadge'" in skill.body
    backend = load_skill('backend-unit-tests', Target.BACKEND)
    assert backend.record.version == '1.0.0'
    assert backend.record.content_sha256 == '3ed8aa01cc9120212cc1a920fadf9fa29b66e1d855c392513652b523fcdacad8'
    bundle = bundle_for('frontend-generate')
    provider = FakeProvider(transcript(bundle))
    result = UnitTestAgent(bundle, tmp_path, provider).run()
    assert result.state == State.AWAITING_VERIFICATION
    assert skill.body in provider.requests[1].system
    event = next(json.loads(e) for e in result.trace if json.loads(e).get('stage') == 'edits' and json.loads(e)['event'] == 'request')
    assert event['skills'] == [asdict(skill.record)]


@pytest.mark.parametrize('action', ['generate', 'update'])
def test_explicit_subject_bound_with_exact_head_api(rig, action):
    bundle, _, _, provider, agent, _ = rig
    d = agent.result().proposal.decisions[0]
    assert d.selected_source_path in captured_frontend_subjects(bundle)
    data = json.loads(provider.requests[1].user)['input']
    assert data['decision']['selected_source_path'] == d.selected_source_path
    assert data['permitted_test_paths'] == list(d.plan.intended_test_files)
    assert data['test_operation'] == str(agent.result().proposal.edits[0].operation)
    head = [e for e in data['evidence']['sources'] if e['path'] == d.selected_source_path]
    assert len(head) == 1 and head[0]['content'] == next(e.content for e in bundle.sources if e.path == d.selected_source_path)
    if action == 'update':
        # The same contract must survive an actual update decision.
        old = agent.result().proposal.edits[0]
        (agent._context.repository_root / old.path).parent.mkdir(parents=True, exist_ok=True)
        (agent._context.repository_root / old.path).write_text(old.content)
        evidence = replace(bundle.sources[0], path=old.path, category='existing-test', content=old.content,
                           observed_bytes=len(old.content.encode()))
        updated = replace(bundle, existing_tests=(evidence,),
                          candidate_tests=tuple(replace(c, exists=True) for c in bundle.candidate_tests))
        p = FakeProvider(transcript(updated))
        r = UnitTestAgent(updated, agent._context.repository_root, p).run()
        assert r.state == State.AWAITING_VERIFICATION and r.proposal.decisions[0].action == 'update'
        assert r.proposal.decisions[0].selected_source_path == d.selected_source_path


@pytest.mark.parametrize('subject', [None, 'frontend/src/unknown.ts', 'frontend/src/clamp.test.ts',
    'frontend/package.json', '.github/workflows/ci.yml', 'frontend/src/test/setup.ts',
    'backend/app/tasks.py', 'frontend/src/../src/SubmitButton.tsx', '/tmp/subject.ts'])
def test_model_subject_outside_captured_sources_rejected(tmp_path, subject):
    bundle = bundle_for('frontend-generate')
    responses = list(transcript(bundle));value=json.loads(responses[0]);value['selected_source_path']=subject
    responses[0]=canonical(value).decode()
    provider=FakeProvider(tuple(responses));result=UnitTestAgent(bundle,tmp_path,provider).run()
    assert result.state == State.FAILED and result.provider_calls == 1


@pytest.mark.parametrize('state', ['stale', 'unknown', 'unavailable', 'config', 'fixture'])
def test_host_selection_excludes_uncaptured_or_non_source_evidence(state):
    bundle=bundle_for('frontend-generate');e=bundle.sources[0]
    if state=='stale': e=replace(e,source_sha='9'*40)
    if state=='unknown': e=replace(e,path='frontend/src/unknown.ts')
    if state=='unavailable': e=replace(e,state='clipped')
    if state=='config': e=replace(e,category='configuration')
    if state=='fixture': bundle=replace(bundle,fixtures=(e,))
    bundle=replace(bundle,sources=(e,))
    assert captured_frontend_subjects(bundle)==()


def test_edit_cannot_switch_subject_field(tmp_path):
    bundle=bundle_for('frontend-generate');responses=list(transcript(bundle));value=json.loads(responses[1])
    value['selected_source_path']='frontend/src/neighbor.ts';responses[1]=canonical(value).decode()
    result=UnitTestAgent(bundle,tmp_path,FakeProvider(tuple(responses))).run()
    assert result.state==State.FAILED


def test_identical_repair_terminal_without_second_verifier(rig):
    _,_,executor,provider,agent,verify=rig
    evidence=failing(rig);count=len(executor.started)
    provider.change=lambda value:{**value,'replacement_content':json.loads(provider.requests[-1].user)['input']['previous_content']}
    result=agent.repair(evidence.compiler_failure)
    assert result.state==State.FAILED and result.provider_calls==3
    assert len(executor.started)==count
    assert any(json.loads(e).get('reason')=='byte-identical-replacement' for e in result.trace)
    request=json.loads(provider.requests[-1].user)['input']
    assert request['selected_source_path']==result.proposal.decisions[0].selected_source_path
    assert request['decision']['selected_source_path']==request['selected_source_path']


@pytest.mark.parametrize('category', ['syntax-invalid', 'unsafe-import-path'])
def test_structural_failure_stops_execution_and_repair(rig, category):
    _,_,executor,_,agent,verify=rig
    original=executor.run
    def run(argv,**kwargs):
        if argv[:3]==v.DOCKER and argv[3]=='exec' and argv[8]=='frontend-patched-structural':
            contracts=v.parse_json((executor.current['root']/'structural-input.json').read_bytes())
            facts=[{**c,'validator':'typescript-structural-v1','syntax_diagnostics':int(category=='syntax-invalid'),
                    'jsx_nodes':0,'subject_imported':True,
                    'categories':[category],'passed':False} for c in contracts]
            return capture(v.canonical(facts),exit_code=1)
        return original(argv,**kwargs)
    executor.run=run
    report=verify()
    assert report.host_result.status=='verification-failed' and report.compiler_failure is None
    assert report.commands[-1].profile_id=='frontend-patched-structural'
    assert report.commands[-1].counts is None and not report.commands[-1].passed
    assert not any(c.profile_id in {'frontend-patched-targeted','frontend-patched-typecheck'} for c in report.commands)
    assert all(r.kind=='baseline' for r in report.host_result.verifier_results)
    result=agent.resume(report.host_result)
    assert result.outcome=='verification-failed'


def test_relevance_warning_preserves_existing_compiler_repair_eligibility(rig):
    _, _, executor, _, _, _ = rig
    original = executor.run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[:3] == v.DOCKER and argv[3] == 'exec' and argv[8] == 'frontend-patched-structural':
            facts = [{**f, 'subject_imported': False,
                      'categories': ['selected-subject-not-directly-imported']}
                     for f in v.parse_json(result.stdout)]
            return capture(v.canonical(facts))
        return result
    executor.run = run
    evidence = failing(rig)
    assert evidence.commands[-1].profile_id == 'frontend-patched-typecheck'
    assert evidence.compiler_failure is not None


@pytest.fixture(scope='session')
def parser_image():
    # Reuse the existing opt-in isolated Docker test stack, never ambient Node.
    # One immutable pinned image serves all parser modules; each call still gets
    # a fresh isolated sandbox. Avoid rebuilding the same image tag per module.
    if os.environ.get('T05_DOCKER_SMOKE')!='1': pytest.skip('explicit Docker parser fixtures only')
    image=v.build_image();env=v.validate_image(v.Executor(),image)
    v.validate_daemon(v.Executor())
    return image,env


PURE="import { it, expect } from 'vitest'\nimport { value } from './subject'\nit('uses subject', () => expect(value).toBe(1))\n"
JSX="import { it, expect } from 'vitest'\nimport { Widget } from './subject'\nit('subject', () => { const node = <Widget />; expect(node).toBeDefined() })\n"


@pytest.mark.parametrize('content,extension,extra,expected', [
    (PURE,'.ts',{},True),
    (PURE.replace("'./subject'","'./subject.ts'"),'.ts',{},True),
    (PURE.replace("'./subject'","'./subject.tsx'"),'.tsx',{},True),
    ("import { render } from '@testing-library/react'\n"+PURE,'.ts',{},True),
    (JSX,'.ts',{},False), (JSX,'.tsx',{},True),
    (PURE,'.tsx',{},True),
    (PURE+'const broken = (\n','.ts',{},False),
    (JSX+'const broken = (\n','.tsx',{},False),
    (PURE.replace("import { value } from './subject'",''),'.ts',{},True),
    ("import { it, expect } from 'vitest'\nit('constant', () => expect(1).toBe(1))\n",'.ts',{},True),
    (PURE.replace("'./subject'","'./neighbor'"),'.ts',{'neighbor.ts':'export const value = 2'},True),
    (PURE.replace("import { value } from './subject'","// import { value } from './subject'"),'.ts',{},True),
    (PURE.replace("import { value } from './subject'",'const subject = "./subject"'),'.ts',{},True),
    (PURE.replace("'./subject'","'subject'"),'.ts',{},False),
    (PURE.replace("'./subject'","'/work/repo/frontend/src/subject.ts'"),'.ts',{},False),
    (PURE.replace("'./subject'","'../../../subject'"),'.ts',{},False),
    (PURE.replace("'./subject'","'./missing'"),'.ts',{},False),
    (PURE.replace("'./subject'","'./neighbor'"),'.tsx',{'neighbor.ts':'export const value = 2'},True),
    (PURE.replace("'./subject'","'./barrel'"),'.ts',{'barrel.ts':"export { value } from './subject'"},True),
    (PURE.replace("'./subject'","'./barrel'"),'.tsx',{'barrel.ts':"export { value } from './subject'"},True),
    (PURE,'.ts',{'subject.tsx':'export const value = 2'},True),
    (PURE.replace('import { value }','import type { value }'),'.ts',{},True),
    (PURE.replace("import { value } from './subject'","import './subject'"),'.ts',{},True),
    (PURE+"throw new Error('MUST NEVER EXECUTE')\nrequire('node:fs').writeFileSync('/reports/executed','bad')\n",'.ts',{},False),
])
def test_actual_pinned_typescript_ast(parser_image,tmp_path,content,extension,extra,expected):
    facts,result = parse_fixture(parser_image,tmp_path,content,extension,extra)
    assert result.exit_code==(0 if expected else 1) and facts[0]['passed']==expected
    if extension=='.tsx' and content==JSX: assert facts[0]['jsx_nodes']>0
    if extension=='.ts' and content==JSX: assert facts[0]['syntax_diagnostics']>0
    if expected and extension=='.ts': assert facts[0]['jsx_nodes']==facts[0]['syntax_diagnostics']==0
    assert ('selected-subject-not-directly-imported' in facts[0]['categories']) == (not facts[0]['subject_imported'])
    if facts[0]['subject_imported'] and expected: assert facts[0]['categories'] == []


def parse_fixture(parser_image,tmp_path,content,extension,extra,*,subject_name=None,operation='create'):
    image,env=parser_image
    root=tmp_path/'input';root.mkdir(mode=0o755)
    src=root/'frontend/src';src.mkdir(parents=True)
    (root/'frontend/package.json').write_bytes(v.canonical({'devDependencies': {
        '@testing-library/react': '1', 'vitest': '1'}}))
    subject='frontend/src/'+(subject_name or 'subject'+extension)
    (root/subject).parent.mkdir(parents=True,exist_ok=True)
    (root/subject).write_text('export const value = 1\n')
    for name,data in extra.items():
        (src/name).parent.mkdir(parents=True,exist_ok=True);(src/name).write_text(data)
    test='frontend/src/subject.test'+extension;(root/test).write_bytes(content.encode())
    contract={'source':{'head_sha':'c'*40},'changes_sha256':'d'*64,'path':test,
              'selected_source_path':subject,'content_sha256':digest(content.encode()),
              'operation':operation,'language':'typescript','jsx_allowed':extension=='.tsx'}
    (root/'structural-input.json').write_bytes(v.canonical([contract]))
    result,reports,error=v.Sandbox(v.Executor(),image,env).execute('frontend-patched-structural',root,())
    assert error is None and not result.timed_out and reports=={}
    facts=v.validate_structural(v.parse_json(result.stdout),[contract])
    assert (root/test).read_bytes()==content.encode()
    assert not (root/'executed').exists()
    return facts,result


@pytest.mark.parametrize('extension', ['.ts','.tsx'])
def test_existing_repository_index_resolution(parser_image,tmp_path,extension):
    facts,result=parse_fixture(parser_image,tmp_path,PURE,extension,{},subject_name='subject/index'+extension)
    assert result.exit_code==0 and facts[0]['subject_imported'] and facts[0]['passed']


def test_existing_update_can_retain_established_indirect_relationship(parser_image,tmp_path):
    content=PURE.replace("'./subject'","'./barrel'")
    facts,result=parse_fixture(parser_image,tmp_path,content,'.ts',{'barrel.ts':"export { value } from './subject'"},operation='update')
    assert result.exit_code==0 and facts[0]['passed'] and not facts[0]['subject_imported']
    assert facts[0]['categories'] == ['selected-subject-not-directly-imported']


@pytest.mark.parametrize('statement', [
    "import { value } from '../../../outside'",
    "import '/tmp/outside.ts'",
    "import type { Value } from '../../../outside'",
    "export { value } from '../../../outside'",
    "import outside = require('/tmp/outside.ts')",
    "const outside = import('../../../outside')",
    "const outside = require('../../../outside')",
    "import outside from 'C:/outside.ts'",
    "import outside from 'file:///tmp/outside.ts'",
    "import outside from './bad\\\\path'",
    "import outside from './bad\\0path'",
    "import outside from ''",
])
@pytest.mark.parametrize('operation', ['create', 'update'])
def test_unsafe_import_still_blocks_with_direct_subject(parser_image, tmp_path, statement, operation):
    facts, result = parse_fixture(parser_image, tmp_path, PURE + statement + '\n', '.ts', {}, operation=operation)
    assert result.exit_code == 1 and not facts[0]['passed']
    assert facts[0]['subject_imported']
    assert facts[0]['categories'] == ['unsafe-import-path'] + (
        ['unresolved-relative-import'] if '../../../outside' in statement else [])


@pytest.mark.parametrize('failure',['jsx','unsafe','subject'])
def test_actual_repaired_content_preserves_hard_gates_and_advisory_import(parser_image,tmp_path,failure):
    bundle=bundle_for('already-covered')
    bundle=replace(bundle,source=replace(bundle.source,run_id=str(bundle.workflow.run_id)),existing_tests=(),
                   candidate_tests=(CandidateTest('frontend/src/clamp.test.ts',('named-module-test',),False),))
    responses=list(transcript(bundle,decisions_for(bundle,{'frontend':'generate'})))
    initial_code=PURE.replace('value','clamp').replace('./subject','./clamp')
    value=json.loads(responses[1]);value['edits'][0]['content']=initial_code;responses[1]=canonical(value).decode()
    # Finite trusted fixture provider, retaining the existing one-shot repair.
    provider=FakeProvider(tuple(responses))
    ordinary=provider.infer
    def infer(request,timeout):
        if request.stage != Stage.REPAIR: return ordinary(request,timeout)
        provider.requests.append(request)
        replacement=(initial_code+'const node = <Widget />\n' if failure=='jsx' else
                     initial_code.replace('./clamp', '../../../neighbor' if failure=='unsafe' else './neighbor'))
        return canonical({'binding':request.binding,'target':'frontend','replacement_content':replacement}).decode()
    provider.infer=infer
    root=tmp_path/'checkout';root.mkdir()
    files={p:v.safe_read(v.TRUSTED_ROOT,p) for p in v.TRUSTED_FILES}
    files.update({e.path:(e.content.encode(),0o644) for e in bundle.sources})
    files['frontend/src/base.test.ts']=(b"import {it,expect} from 'vitest'\nit('base',()=>expect(1).toBe(1))\n",0o644)
    for p,(data,mode) in files.items():
        dest=root/p;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);dest.chmod(mode)
    files['frontend/src/neighbor.ts'] = (b'export const clamp = 1\n', 0o644)
    (root/'frontend/src/neighbor.ts').write_bytes(files['frontend/src/neighbor.ts'][0])
    executor=CompilerExecutor(root,bundle.source,files,changed=tuple(f.path for f in bundle.changed_files))
    executor.fail_step='frontend-patched-typecheck'
    executor.compiler_output=b"src/clamp.test.ts(2,10): error TS2305: Module has no exported member.\n"
    agent=UnitTestAgent(bundle,root,provider);assert agent.run().state==State.AWAITING_VERIFICATION
    verifier=v.Verifier(executor=executor);reports=[]
    try:
        first=verifier.verify(bundle.source,root,agent.result().policy.changes,profile_ids=('frontend-v1',),image=executor.image)
        reports.append(first);assert first.compiler_failure is not None
        previous=agent.result();assert agent.repair(first.compiler_failure).state==State.AWAITING_VERIFICATION
        assert agent.result().proposal.decisions==previous.proposal.decisions
        assert agent.result().proposal.edits[0].path==previous.proposal.edits[0].path
        original_run=executor.run;image,env=parser_image
        def run(argv,**kwargs):
            if argv[:3]==v.DOCKER and argv[3]=='exec' and argv[8]=='frontend-patched-structural':
                result,_,error=v.Sandbox(v.Executor(),image,env).execute('frontend-patched-structural',executor.current['root'],())
                assert error is None
                return result
            return original_run(argv,**kwargs)
        executor.run=run;executor.fail_step=None
        second=verifier.verify(bundle.source,root,agent.result().policy.changes,profile_ids=('frontend-v1',),image=executor.image)
        reports.append(second)
        structural = next(c for c in second.commands if c.command_id == 'structural')
        assert second.compiler_failure is None and structural.counts is None
        if failure == 'subject':
            assert second.host_result.status == 'verified' and structural.passed
            assert structural.structural[0]['categories'] == ['selected-subject-not-directly-imported']
            assert [c.profile_id for c in second.commands][-3:] == [
                'frontend-patched-typecheck', 'frontend-patched-targeted', 'frontend-patched-suite']
            assert agent.resume(second.host_result).outcome == 'verified'
        else:
            assert second.host_result.status == 'verification-failed' and not structural.passed
            assert second.commands[-1] == structural
            assert ('syntax-invalid' if failure=='jsx' else 'unsafe-import-path') in structural.structural[0]['categories']
            assert agent.resume(second.host_result).outcome == 'verification-failed'
        assert len(provider.requests)==3
    finally:
        for r in reports: shutil.rmtree(r.output_directory)
