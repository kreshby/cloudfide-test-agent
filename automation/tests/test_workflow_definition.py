"""Review fixed workflow structure using stdlib, without adding a YAML stack."""

from pathlib import Path
import re


PATH = Path('.github/workflows/test-agent.yml')


def workflow():
    return PATH.read_text()


def jobs():
    body = workflow().split('\njobs:\n', 1)[1]
    parts = re.split(r'^  ([a-z_]+):\n', body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def mapping(body, key, indent):
    pattern = ' ' * indent + key + ':\n'
    block = body.split(pattern, 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith(' ' * (indent + 2)):
            break
        if line.strip():
            lines.append(line.strip())
    return lines


def test_only_workflow_run_and_bounded_trusted_dispatch_triggers():
    text = workflow()
    trigger = text.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
    assert re.findall(r'^  ([a-z_]+):', trigger, re.M) == ['workflow_run', 'workflow_dispatch']
    assert 'workflows: [Ordinary CI]' in trigger and 'types: [completed]' in trigger
    assert 'ordinary_ci_run_id:' in trigger and 'type: string' in trigger
    assert 'pull_request_target' not in text and 'push:' not in text
    assert 'head_sha:' not in trigger and 'policy_sha:' not in trigger
    benchmark = Path('.github/workflows/model-benchmark.yml').read_text()
    assert benchmark.split('\non:\n', 1)[1].split('\npermissions:', 1)[0].strip() == 'workflow_dispatch:'


def test_trust_zones_minimal_job_permissions_and_public_runner_guard():
    text = workflow()
    assert re.findall(r'^permissions: (.+)$', text, re.M) == ['{}']
    graph = jobs()
    assert set(graph) == {'prepare', 'publisher'}
    assert 'needs: prepare' in graph['publisher']
    assert mapping(graph['prepare'], 'permissions', 4) == ['actions: read', 'contents: read', 'pull-requests: read']
    assert mapping(graph['publisher'], 'permissions', 4) == ['contents: read', 'actions: read', 'pull-requests: write', 'issues: write']
    assert 'contents: write' not in text and 'actions: write' not in text
    assert text.count('pull-requests: write') == 1
    assert text.count('issues: write') == 1
    for body in graph.values():
        assert re.findall(r'^    runs-on: (.+)$', body, re.M) == ['ubuntu-24.04']
        assert "github.repository == 'kreshby/cloudfide-test-agent'" in body
        assert '!github.event.repository.private' in body
        assert "github.ref == 'refs/heads/main'" in body
        assert re.search(r'^    timeout-minutes: [1-9][0-9]*$', body, re.M)
    assert 'timeout-minutes: 45' in graph['prepare'] and 'timeout-minutes: 5' in graph['publisher']


def test_all_action_versions_are_verified_immutable_pins():
    pins = re.findall(r'^\s+uses: (\S+)', workflow(), re.M)
    assert set(pins) == {
        'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9',
        'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333',
    }
    assert all(re.fullmatch(r'actions/[a-z-]+@[0-9a-f]{40}', p) for p in pins)
    assert 'cache@' not in workflow() and 'setup-python' not in workflow()


def test_publisher_serializes_only_validated_repository_pr_without_cancellation():
    publisher = jobs()['publisher']
    assert mapping(publisher, 'concurrency', 4) == [
        'group: test-agent-publisher-${{ needs.prepare.outputs.repository_id }}-${{ needs.prepare.outputs.pr_number }}',
        'cancel-in-progress: false',
    ]
    assert "needs.prepare.outputs.publish == 'true'" in publisher
    assert "needs.prepare.result == 'success'" in publisher
    assert 'github.head_ref' not in publisher


def test_exact_trusted_code_and_current_run_handoff_not_source_ci_artifacts():
    text = workflow()
    assert text.count('ref: ${{ github.workflow_sha }}') == 2
    assert text.count('persist-credentials: false') == 2
    assert text.count('path: trusted') == 2
    publisher = jobs()['publisher']
    assert 'name: test-agent-handoff-${{ github.run_id }}-${{ github.run_attempt }}' in publisher
    assert 'run-id:' not in publisher and 'pattern:' not in publisher
    assert 'HANDOFF_SHA256: ${{ needs.prepare.outputs.handoff_sha256 }}' in publisher
    assert 'EXPECTED_PR_NUMBER: ${{ needs.prepare.outputs.pr_number }}' in publisher
    assert 'FINAL_ARTIFACT_ID: ${{ needs.prepare.outputs.artifact_id }}' in publisher
    assert 'python3 -m automation.test_agent.workflow publish' in publisher


def test_explicit_small_upload_allowlist_retention_and_receipt_failure_path():
    text = workflow()
    assert re.findall(r'retention-days: (\d+)', text) == ['7', '1']
    paths = re.findall(r'\$\{\{ runner.temp \}\}/t07-evidence/[^\s]+', text)
    assert set(paths) == {'${{ runner.temp }}/t07-evidence/final/' + name for name in (
        'manifest.json', 'verification.json', 'report-metadata.json', 'generated-tests.patch')
    } | {'${{ runner.temp }}/t07-evidence/handoff.json'}
    assert 'continue-on-error: true' in jobs()['prepare']
    assert "os.environ['UPLOAD_OUTCOME'] == 'success'" in text
    assert "os.environ['PATCH_PREPARED'] == 'true'" in text
    assert 'UPLOAD_ARTIFACT_ID: ${{ steps.final.outputs.artifact-id }}' in text
    assert 'include-hidden-files: true' not in text
    assert 'node_modules' not in text and '.gguf' not in text and 'docker' not in text.lower()
