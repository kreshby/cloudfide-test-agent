"""Exact host GitHub/source orchestration; every external service is replaced."""

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from automation.test_agent import workflow as w
from automation.test_agent.agent import State
from automation.test_agent.github import GitHubReader, CollectionError
from automation.test_agent.handoff import encode, execution_facts, report, successful, terminal_facts
from automation.test_agent.provider import FakeProvider
from automation.test_agent.publisher import GitHubPublisherAdapter, PublicationError, PublicationState, publish
from automation.test_agent.reporting import AgentRun, PublicationIdentity, ReportingError, parse_marker, prepare_report
from automation.tests.t03_fixtures import FixtureTransport, REPOSITORY as FIXTURE_REPO, RUN, WORKFLOW, HEAD, POLICY
from automation.tests.t04_fixtures import transcript
from automation.tests.t06_fixtures import AUTHOR, FakeTransport, fixture


class SourceAPI(FixtureTransport):
    def __init__(self):
        super().__init__()
        self.repo.update(full_name=w.REPOSITORY, private=False)
        for item in (self.run, self.pr, *self.associations):
            for part in ("repository", "head_repository") if item is self.run else ():
                item[part] = deepcopy(self.repo)
            for part in ("base", "head") if item is not self.run else ():
                item[part]["repo"] = deepcopy(self.repo)
        self.run.update(name=w.CI_NAME, path=w.CI_PATH, status="completed", conclusion="success")
        self.workflow = {"id": WORKFLOW, "name": w.CI_NAME, "path": w.CI_PATH, "state": "active"}
        self.agent_workflow = {"id": 8, "name": w.AGENT_NAME, "path": w.AGENT_PATH, "state": "active"}
        self.agent_run = {**self.run, "id": 100, "workflow_id": 8, "name": w.AGENT_NAME, "path": w.AGENT_PATH,
                          "status": "in_progress", "event": "workflow_run", "head_sha": "8" * 40, "head_branch": "main"}

    def get(self, path, query=()):
        endpoint = path.removeprefix("/repos/" + w.REPOSITORY)
        if endpoint == "":
            return self.response(self.repo)
        if endpoint == "/actions/workflows/ci.yml":
            return self.response(self.workflow)
        if endpoint == "/actions/workflows/test-agent.yml":
            return self.response(self.agent_workflow)
        if endpoint == "/actions/runs/100":
            return self.response(self.agent_run)
        return super().get(path.replace("/repos/" + w.REPOSITORY, "/repos/" + FIXTURE_REPO), query)

    def reader(self):
        return GitHubReader(w.REPOSITORY, self)

    def preflight(self, **kwargs):
        return w.preflight(self.reader(), RUN, AgentRun(100, 1), POLICY,
                           expected_repository_id=101, **kwargs)


def pre_for(bundle):
    ci = {**asdict(bundle.workflow), "name": w.CI_NAME, "path": w.CI_PATH, "status": "completed", "conclusion": "success"}
    s = bundle.source
    i = PublicationIdentity(s.repository, s.pr_number, s.head_sha, s.policy_sha, 100, 1, s.base_sha,
                            s.merge_base_sha, bundle.workflow.repository_id, bundle.workflow.run_id, bundle.workflow.run_attempt)
    return w.Preflight(s, ci, i, None)


def gate_reader(pre, private=False):
    from types import SimpleNamespace
    return SimpleNamespace(repository_metadata=lambda: {
        "id": pre.identity.repository_id, "full_name": w.REPOSITORY, "private": private})


def test_automatic_source_and_current_agent_are_separate():
    api = SourceAPI()
    pre = api.preflight(event_snapshot=deepcopy(api.run))
    assert pre.source.run_id == "42" and pre.source_ci["run_id"] == 42
    assert pre.identity.workflow_run_id == 100 and pre.identity.source_ci_run_id == 42
    assert pre.identity.head_sha == HEAD and pre.identity.policy_sha == POLICY
    w.validate_agent_run(api.reader(), AgentRun(100, 1), "8" * 40, 101, "workflow_run")
    assert pre.terminal_reason is None


def test_push_run_never_associates_or_starts_agent():
    api = SourceAPI()
    api.run["event"] = "push"
    assert api.preflight() is None
    assert not any("/pulls" in path or "/compare/" in path for path, _ in api.calls)


@pytest.mark.parametrize("location,key,value", [
    ("run", "name", "Other CI"), ("run", "path", ".github/workflows/evil.yml"),
    ("run", "workflow_id", 9), ("workflow", "id", 9), ("workflow", "path", "evil.yml"),
    ("workflow", "name", "Other CI"), ("run", "status", "in_progress"),
    ("repo", "private", True), ("repo", "full_name", "evil/repo"), ("repo", "id", 102),
    ("run", "event", "workflow_dispatch"), ("run", "conclusion", None), ("run", "run_attempt", True),
])
def test_invalid_source_and_private_repo_never_grant_a_target(location, key, value):
    api = SourceAPI()
    getattr(api, location)[key] = value
    with pytest.raises((w.WorkflowError, CollectionError)):
        api.preflight()


@pytest.mark.parametrize("mutation", ["zero", "multiple", "stale", "wrong-repository", "numeric-repository", "duplicates"])
def test_unknown_ambiguous_or_stale_pr_has_no_publication(mutation):
    api = SourceAPI()
    if mutation == "zero":
        api.associations = []
    elif mutation == "multiple":
        api.associations.append(deepcopy(api.pr))
        api.associations[-1]["number"] = 13
    elif mutation == "duplicates":
        api.associations.append(deepcopy(api.pr))
    elif mutation == "stale":
        api.pr["head"]["sha"] = "9" * 40
    else:
        api.associations[0]["base"]["repo"]["full_name" if mutation == "wrong-repository" else "id"] = "evil/repo" if mutation == "wrong-repository" else 2
    with pytest.raises((w.WorkflowError, CollectionError)):
        api.preflight()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral", "skipped", "startup_failure"])
def test_unsuccessful_ci_is_known_pr_unavailable_without_baseline_claim(conclusion):
    api = SourceAPI()
    api.run["conclusion"] = conclusion
    pre = api.preflight()
    facts, artifact = w.execute(pre, None, runtime_factory=lambda: pytest.fail("runtime started"))
    raw = encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    assert b"BASELINE_FAILED" not in raw and facts["verification"] is None and artifact is None
    assert facts["status"] == "UNAVAILABLE" and facts["reason"] == "source-ci-unsuccessful"


def test_known_fork_is_exactly_classified_and_never_collected_or_checked_out():
    api = SourceAPI()
    fork = {"id": 202, "full_name": "contributor/taskly"}
    for item in (api.pr, *api.associations):
        item["head"]["repo"] = deepcopy(fork)
    api.run["head_repository"] = deepcopy(fork)
    pre = api.preflight()
    assert pre.identity.head_repository_id == 202 and pre.terminal_reason == "fork-disabled"
    facts, artifact = w.execute(pre, gate_reader(pre), source_factory=lambda *_: pytest.fail("fork checkout"),
                                runtime_factory=lambda: pytest.fail("fork model"))
    assert artifact is None and facts == terminal_facts("UNSUPPORTED", "fork-disabled")
    assert encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)


@pytest.mark.parametrize("key", ["head_sha", "policy_sha", "base_sha", "merge_base_sha", "url"])
def test_dispatch_accepts_only_bounded_source_run_id(key):
    event = {"inputs": {"ordinary_ci_run_id": "42", key: "untrusted"}}
    with pytest.raises(w.WorkflowError):
        w.select_source("workflow_dispatch", event)


@pytest.mark.parametrize("value", ["0", "042", "-1", "1\n", "https://evil", "9" * 20, True, 42])
def test_dispatch_run_id_is_bounded(value):
    with pytest.raises((w.WorkflowError, CollectionError)):
        w.select_source("workflow_dispatch", {"inputs": {"ordinary_ci_run_id": value}})


def test_dispatch_same_source_new_agent_supersedes_and_is_idempotent(tmp_path):
    assert w.select_source("workflow_dispatch", {"inputs": {"ordinary_ci_run_id": "42"}}) == 42
    bundle, result, verification = fixture(tmp_path)
    first = prepare_report(bundle, result, verification, publication=AgentRun(100, 1))
    second = prepare_report(bundle, result, verification, publication=AgentRun(101, 1))
    assert first.identity.source_ci_run_id == second.identity.source_ci_run_id == 42
    assert bundle.source.run_id == "42"
    transport = FakeTransport(first.identity)
    adapter = lambda: GitHubPublisherAdapter(first.identity.repository, transport)
    assert publish(first, adapter(), AUTHOR).state == PublicationState.CREATED
    assert publish(second, adapter(), AUTHOR).state == PublicationState.UPDATED
    assert len(transport.comment_values) == 1
    assert publish(first, adapter(), AUTHOR).state == PublicationState.SUPERSEDED
    assert publish(second, adapter(), AUTHOR).state == PublicationState.IDEMPOTENT
    changed = replace(second, body=second.body + "changed facts\n")
    with pytest.raises(PublicationError):
        publish(changed, adapter(), AUTHOR)
    assert transport.writes == 2
    assert "Source CI run: 42" in second.body and "Agent workflow run: 101" in second.body
    swapped = second.body.replace("run=101", "run=42").replace("source_ci=42", "source_ci=101")
    with pytest.raises(ReportingError):
        parse_marker(swapped, expected=second.identity)


def test_publication_does_not_rewrite_source_or_order_by_source(tmp_path):
    bundle, result, verification = fixture(tmp_path)
    with pytest.raises(ReportingError):
        prepare_report(replace(bundle, source=replace(bundle.source, run_id="100")), result, verification,
                       publication=AgentRun(100, 1))
    first = prepare_report(bundle, result, verification, publication=AgentRun(100, 1))
    other_bundle, other_result, other_report = fixture(tmp_path, run=41)
    newer = prepare_report(other_bundle, other_result, other_report, publication=AgentRun(101, 1))
    assert newer.identity.source_ci_run_id < first.identity.source_ci_run_id
    transport = FakeTransport(first.identity, [FakeTransport.comment(500, first.body)])
    assert publish(newer, GitHubPublisherAdapter(newer.identity.repository, transport), AUTHOR).state == PublicationState.UPDATED


def test_known_docs_only_bypasses_model_and_verifier(tmp_path, monkeypatch):
    bundle, _, _ = fixture(tmp_path)
    bundle = replace(bundle, changed_files=(replace(bundle.changed_files[0], path="README.md", support="unsupported"),))
    monkeypatch.setattr(w, "collect_context", lambda *_a, **_k: bundle)
    facts, artifact = w.execute(pre_for(bundle), None, runtime_factory=lambda: pytest.fail("docs model"),
                                verifier_factory=lambda: pytest.fail("docs verifier"))
    assert facts == terminal_facts("UNSUPPORTED", "docs-only") and artifact is None


@pytest.mark.parametrize("status", ["verified", "verification-failed", "baseline-failed", "unavailable"])
def test_actual_agent_policy_verifier_feedback_flow_and_memory_lifetime(tmp_path, monkeypatch, status):
    from automation.test_agent.contracts import FinalHostStatus
    bundle, _, verification = fixture(tmp_path, status=FinalHostStatus(status))
    pre = pre_for(bundle)
    events = []
    provider = FakeProvider(transcript(bundle, feedback=verification.host_result))
    class Runtime:
        def __enter__(self):
            events.append("start")
            self.provider = provider
            return self
        def stop(self):
            events.append("stop")
        def start(self):
            events.append("restart")
        def __exit__(self, *_):
            events.append("cleanup")
    class Verifier:
        def verify(self, source, root, changes, **kwargs):
            assert source == bundle.source and changes.source == source
            events.append("verify")
            assert kwargs["profile_ids"] == ("frontend-v1",)
            return verification
    class Source:
        def __init__(self, *_):
            pass
        def __enter__(self):
            return tmp_path
        def __exit__(self, *_):
            events.append("source-cleanup")
    monkeypatch.setattr(w, "collect_context", lambda *_a, **_k: bundle)
    facts, artifact = w.execute(pre, gate_reader(pre), source_factory=Source, runtime_factory=Runtime, verifier_factory=Verifier)
    assert events == ["start", "stop", "verify", "cleanup", "source-cleanup"]
    assert facts["status"] == status.upper().replace("-", "_") and facts["verification"] is not None
    assert len(provider.requests) == 2  # Trusted verifier verdict requires no model call.
    assert artifact.patch_preparation.value == "PREPARED"
    assert not (tmp_path / facts["edits"][0]["path"]).exists()  # Proposed code never runs/writes on host.
    raw = encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    value = __import__('json').loads(raw)
    unresolved = report(value)
    assert not successful(unresolved)  # An unavailable required patch is never green.
    delivered = report(value, artifact_id=700)
    assert successful(delivered) == (status == "verified")


def test_known_pr_runtime_unavailable_has_publishable_envelope(tmp_path, monkeypatch):
    bundle, _, _ = fixture(tmp_path)
    pre = pre_for(bundle)
    monkeypatch.setattr(w, "collect_context", lambda *_a, **_k: bundle)
    class Source:
        def __init__(self, *_): pass
        def __enter__(self): return tmp_path
        def __exit__(self, *_): pass
    def unavailable():
        raise w.RuntimeUnavailable("dummy failure")
    facts, artifact = w.execute(pre, gate_reader(pre), source_factory=Source, runtime_factory=unavailable)
    assert artifact is None and facts == terminal_facts("UNAVAILABLE", "runtime-unavailable")
    assert encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)


@pytest.mark.parametrize("field,value", [("head_sha", "7" * 40), ("path", "evil.yml"), ("head_branch", "test-agent-workflow"),
                                         ("run_attempt", 2), ("workflow_id", 9), ("event", "push")])
def test_current_agent_identity_must_be_trusted_main(field, value):
    api = SourceAPI()
    api.agent_run[field] = value
    with pytest.raises(w.WorkflowError):
        w.validate_agent_run(api.reader(), AgentRun(100, 1), "8" * 40, 101, "workflow_run")


def test_workflow_run_snapshot_cannot_change_attempt_head_or_ci_identity():
    api = SourceAPI()
    for key, value in (("head_sha", "9" * 40), ("run_attempt", 2), ("workflow_id", 9)):
        snapshot = deepcopy(api.run)
        snapshot[key] = value
        with pytest.raises(w.WorkflowError):
            api.preflight(event_snapshot=snapshot)


@pytest.mark.parametrize("mismatch", [None, "head", "origin", "merge-base", "dirty"])
def test_source_checkout_has_fixed_public_origin_exact_head_and_cleanup(monkeypatch, mismatch):
    api = SourceAPI()
    pre = api.preflight()
    observed = []
    class Process:
        def __init__(self, argv, cwd):
            observed.append(argv)
            self.truncated = {"stdout": False, "stderr": False}
            data = ""
            if "rev-parse" in argv: data = "0" * 40 if mismatch == "head" else pre.source.head_sha
            elif "config" in argv: data = "https://evil.invalid/repo" if mismatch == "origin" else w.ORIGIN
            elif "merge-base" in argv: data = "0" * 40 if mismatch == "merge-base" else pre.source.merge_base_sha
            elif "status" in argv: data = "?? unexpected" if mismatch == "dirty" else ""
            self.stdout = data.encode()
            self.process = type('Child', (), {'wait': lambda *_a, **_k: 0})()
        def stop(self): pass
    monkeypatch.setattr(w, "Process", Process)
    source = w.SourceCheckout(pre.source)
    if mismatch:
        with pytest.raises(w.WorkflowError): source.__enter__()
    else:
        with source as root:
            assert root.is_dir() and not list(root.iterdir())
        assert not root.exists()
    assert source.temporary is None or not Path(source.temporary.name).exists()
    fetch = next(argv for argv in observed if 'fetch' in argv)
    assert fetch[-5:] == ['fetch', '--no-tags', 'origin', pre.source.head_sha, pre.source.base_sha]
    remote = next(argv for argv in observed if 'remote' in argv)
    assert remote[-1] == w.ORIGIN
    assert all('credential.helper=' in argv and 'core.hooksPath=/dev/null' in argv and 'http.followRedirects=false' in argv for argv in observed)


def test_policy_sha_comes_from_installed_trusted_blob_only(monkeypatch):
    import hashlib
    from automation.test_agent.verification import Capture
    root = Path(w.__file__).resolve().parents[2]
    installed = (root / 'automation/test_agent/policy.py').read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(installed)).encode() + b'\0' + installed).hexdigest()
    class Executor:
        def run(self, argv, **kwargs):
            value = '' if 'diff' in argv else blob if 'HEAD:automation/test_agent/policy.py' in argv else '8' * 40
            return Capture(0, value.encode())
    ref = w.REPOSITORY + '/' + w.AGENT_PATH + '@refs/heads/main'
    assert w.trusted_checkout(root, '8' * 40, ref, executor=Executor()) == blob
    with pytest.raises(w.WorkflowError):
        w.trusted_checkout(root, '9' * 40, ref, executor=Executor())
    with pytest.raises(w.WorkflowError):
        w.trusted_checkout(root, '8' * 40, ref.replace('main', 'test-agent-workflow'), executor=Executor())


@pytest.mark.parametrize('outcome', ['docs-only', 'source-ci-unsuccessful'])
def test_prepare_publish_cli_job_handoff_and_failure_exit_are_offline(tmp_path, monkeypatch, outcome):
    import json
    import resource
    import shutil
    api = SourceAPI()
    if outcome == 'source-ci-unsuccessful': api.run['conclusion'] = 'failure'
    event_file = tmp_path / 'event.json'
    event_file.write_text(json.dumps({'workflow_run': api.run}))
    output = tmp_path / 'github-output'
    env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'ImageOS': 'ubuntu24',
           'GITHUB_REPOSITORY': w.REPOSITORY, 'GITHUB_REPOSITORY_ID': '101', 'GITHUB_REF': 'refs/heads/main',
           'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'workflow_run',
           'GITHUB_EVENT_PATH': str(event_file), 'RUNNER_TEMP': str(tmp_path), 'GITHUB_OUTPUT': str(output),
           'TRUSTED_WORKFLOW_SHA': '8' * 40, 'TRUSTED_WORKFLOW_REF': w.REPOSITORY + '/' + w.AGENT_PATH + '@refs/heads/main',
           'GITHUB_TOKEN': 'DUMMY_T07_READ_OR_PUBLISH_ONLY'}
    for key, value in env.items(): monkeypatch.setenv(key, value)
    monkeypatch.setattr(resource, 'setrlimit', lambda *_: None)
    monkeypatch.setattr(w, 'trusted_checkout', lambda *_: POLICY)
    monkeypatch.setattr(w, 'GitHubHTTPTransport', lambda *_: api)
    monkeypatch.setattr(w, 'execute', lambda *_: (terminal_facts('UNSUPPORTED' if outcome == 'docs-only' else 'UNAVAILABLE', outcome), None))
    assert w.main('prepare') == 0
    outputs = dict(line.split('=', 1) for line in output.read_text().splitlines())
    assert outputs['source_run_id'] == '42' and outputs['pr_number'] == '12'
    assert outputs['publish'] == 'true' and outputs['artifact'] == 'false'
    directory = tmp_path / 't07-handoff'
    directory.mkdir()
    shutil.copyfile(tmp_path / 't07-evidence/handoff.json', directory / 'handoff.json')
    for key, value in {'SOURCE_CI_RUN_ID': '42', 'EXPECTED_PR_NUMBER': '12',
                       'HANDOFF_SHA256': outputs['handoff_sha256'], 'FINAL_ARTIFACT_ID': ''}.items():
        monkeypatch.setenv(key, value)
    pre = api.preflight()
    transport = FakeTransport(pre.identity)
    monkeypatch.setattr(w, 'EXPECTED_AUTHOR', AUTHOR)
    monkeypatch.setattr(w, 'GitHubPublisherHTTPTransport', lambda *_: transport)
    assert w.main('publish') == (0 if outcome == 'docs-only' else 1)
    assert transport.writes == 1
    body = transport.comment_values[0]['body']
    assert 'no execution evidence' in body and 'BASELINE_FAILED' not in body
    assert 'DUMMY_T07_READ_OR_PUBLISH_ONLY' not in body and str(tmp_path) not in body
    monkeypatch.setenv('HANDOFF_SHA256', '0' * 64)
    with pytest.raises(ValueError): w.main('publish')
    assert transport.writes == 1


def test_fork_terminal_publication_still_requires_exact_fork_and_author():
    api = SourceAPI()
    fork = {'id': 202, 'full_name': 'contributor/taskly'}
    for item in (api.pr, *api.associations): item['head']['repo'] = deepcopy(fork)
    api.run['head_repository'] = deepcopy(fork)
    pre = api.preflight()
    raw = encode(pre.identity, pre.source, pre.source_ci, '8' * 40, terminal_facts('UNSUPPORTED', 'fork-disabled'))
    prepared = report(__import__('json').loads(raw))
    transport = FakeTransport(pre.identity)
    transport.pr['head']['repo'] = fork
    assert publish(prepared, GitHubPublisherAdapter(w.REPOSITORY, transport), AUTHOR).state == PublicationState.CREATED
    transport.pr['head']['repo'] = {'id': 203, 'full_name': 'other/taskly'}
    assert publish(prepared, GitHubPublisherAdapter(w.REPOSITORY, transport), AUTHOR).state == PublicationState.STALE
    assert transport.writes == 1


@pytest.mark.parametrize('mode', ['verifier-unavailable', 'feedback-unavailable', 'artifact-unavailable', 'no-edit', 'agent-failed', 'policy-rejected'])
def test_expected_pipeline_failures_and_no_edit_keep_truthful_facts(tmp_path, monkeypatch, mode):
    bundle, _, verification = fixture(tmp_path, case='already-covered' if mode == 'no-edit' else 'frontend-generate')
    pre = pre_for(bundle)
    provider = FakeProvider(('malformed',) if mode == 'agent-failed' else transcript(bundle, feedback=verification.host_result if verification else None))
    if mode == 'policy-rejected':
        import json
        replies = list(transcript(bundle))
        edit_response = json.loads(replies[1])
        edit_response['test_body'] = "it.only('bad', () => expect(1).toBe(1))\n"
        replies[1] = json.dumps(edit_response)
        provider = FakeProvider(tuple(replies))
    events = []
    class Runtime:
        def __enter__(self):
            self.provider = provider
            return self
        def stop(self): events.append('stop')
        def start(self):
            events.append('restart')
            if mode == 'feedback-unavailable': raise w.RuntimeUnavailable('dummy')
        def __exit__(self, *_): events.append('cleanup')
    class Source:
        def __init__(self, *_): pass
        def __enter__(self): return tmp_path
        def __exit__(self, *_): pass
    class Verifier:
        def verify(self, *_a, **_k):
            events.append('verify')
            if mode == 'verifier-unavailable': raise OSError('dummy')
            assert mode not in {'no-edit', 'agent-failed', 'policy-rejected'}
            return verification
    monkeypatch.setattr(w, 'collect_context', lambda *_a, **_k: bundle)
    if mode == 'artifact-unavailable':
        monkeypatch.setattr(w, 'build_bundle', lambda *_a, **_k: (_ for _ in ()).throw(ValueError('dummy')))
    facts, artifact = w.execute(pre, gate_reader(pre), source_factory=Source, runtime_factory=Runtime, verifier_factory=Verifier)
    raw = encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    prepared = report(__import__('json').loads(raw))
    if mode == 'verifier-unavailable':
        assert facts['verification'] is None and facts['required_edits']
        assert facts['status'] == 'UNAVAILABLE' and len(provider.requests) == 2
        assert 'restart' not in events and 'verify' in events
    elif mode == 'feedback-unavailable':
        assert facts['status'] == 'VERIFIED'
        assert 'restart' not in events and len(provider.requests) == 2
        assert 'Host verification: **VERIFIED**' in prepared.body
    elif mode == 'artifact-unavailable':
        assert facts['status'] == 'VERIFIED' and artifact is None and not facts['patch_prepared']
        assert 'Host verification: **VERIFIED**' in prepared.body
    elif mode == 'no-edit':
        assert prepared.status == 'NO_TESTS_NEEDED' and facts['verification'] is None
        assert 'verify' not in events and len(provider.requests) == 1
        assert successful(prepared)
    else:
        assert prepared.status == 'AGENT_FAILED' and 'verify' not in events
    if mode != 'no-edit': assert not successful(prepared)


def test_legacy_owned_marker_requires_review_and_foreign_legacy_is_ignored(tmp_path):
    from automation.test_agent.publisher import PublisherAuthor
    bundle, result, verification = fixture(tmp_path)
    prepared = prepare_report(bundle, result, verification, publication=AgentRun(100, 1))
    legacy = prepared.body.replace('taskly-unit-test-agent-report:v2', 'taskly-unit-test-agent-report:v1')
    transport = FakeTransport(prepared.identity, [FakeTransport.comment(5, legacy)])
    with pytest.raises(PublicationError):
        publish(prepared, GitHubPublisherAdapter(prepared.identity.repository, transport), AUTHOR)
    assert transport.writes == 0
    transport = FakeTransport(prepared.identity, [FakeTransport.comment(5, legacy, PublisherAuthor(999, 'foreign'))])
    assert publish(prepared, GitHubPublisherAdapter(prepared.identity.repository, transport), AUTHOR).state == PublicationState.CREATED


def test_repository_becoming_private_after_context_blocks_model_work(tmp_path, monkeypatch):
    bundle, _, _ = fixture(tmp_path)
    pre = pre_for(bundle)
    monkeypatch.setattr(w, 'collect_context', lambda *_a, **_k: bundle)
    facts, artifact = w.execute(pre, gate_reader(pre, private=True),
                                runtime_factory=lambda: pytest.fail('private model run'),
                                source_factory=lambda *_: pytest.fail('private checkout'))
    assert facts == terminal_facts('UNAVAILABLE', 'repository-unavailable') and artifact is None


def test_dispatch_validates_source_through_github_and_rejects_push_origin():
    api = SourceAPI()
    pre = api.preflight(trigger='workflow_dispatch')
    assert pre.source.run_id == '42' and pre.identity.workflow_run_id == 100
    api.run['event'] = 'push'
    with pytest.raises(w.WorkflowError): api.preflight(trigger='workflow_dispatch')
    api.run['event'] = 'pull_request'
    api.run['workflow_id'] = 9
    with pytest.raises(w.WorkflowError): api.preflight(trigger='workflow_dispatch')
