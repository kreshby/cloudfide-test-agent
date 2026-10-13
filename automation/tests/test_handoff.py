"""Strict current-run fact handoff, no network or untrusted code execution."""

from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from automation.test_agent import handoff as h
from automation.test_agent.provider import canonical, digest
from automation.test_agent.reporting import (AgentRun, DeliveryState, MAX_ADVISORY_BYTES,
                                            ReportingError, inert_text, parse_marker)
from automation.tests.t06_fixtures import fixture
from automation.tests.test_workflow import pre_for


def payload(root):
    bundle, result, verification = fixture(root)
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    return pre, raw


def read(pre, raw, expected_digest=None, **changes):
    params = dict(expected_identity=pre.identity, expected_source_ci=pre.source_ci, expected_trusted_sha="8" * 40)
    params.update(changes)
    return h.decode(raw, expected_digest or digest(raw), **params)


def test_canonical_deterministic_bytes_and_safe_selected_facts(tmp_path):
    pre, raw = payload(tmp_path)
    assert raw == payload(tmp_path)[1] == canonical(json.loads(raw))
    value = read(pre, raw)
    assert len(raw) < h.MAX_HANDOFF_BYTES and value["schema_version"] == 1
    assert b"/private/" not in raw and str(tmp_path).encode() not in raw
    assert b"SHOULD_NOT_RENDER" not in raw and b"system" not in raw and b"interpretation" not in raw
    assert b"raw_response" not in raw and b"prompt" not in raw and b"stdout\"" not in raw
    rendered = h.report(value, artifact_id=700)
    assert rendered.status == "VERIFIED" and rendered.delivery.state == DeliveryState.AVAILABLE
    assert "Source CI run: 42" in rendered.body and "Agent workflow run: 100" in rendered.body
    assert h.successful(rendered)
    unresolved = h.report(value)
    assert unresolved.status == "VERIFIED" and unresolved.delivery.state == DeliveryState.UNAVAILABLE
    assert "verdict is unchanged" in unresolved.body and not h.successful(unresolved)


@pytest.mark.parametrize("mutation", ["schema", "unknown", "digest", "oversized", "duplicate", "noncanonical",
                                      "source", "head", "policy", "run", "source-run", "attempt", "ci", "code", "mixed", "null"])
def test_invalid_envelopes_are_rejected(tmp_path, mutation):
    pre, raw = payload(tmp_path)
    value = json.loads(raw)
    original_digest = digest(raw)
    if mutation == "digest":
        with pytest.raises(h.HandoffError):
            read(pre, raw, "0" * 64)
        return
    if mutation == "oversized":
        raw = b"x" * (h.MAX_HANDOFF_BYTES + 1)
    elif mutation == "duplicate":
        raw = raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1)
    elif mutation == "noncanonical":
        raw = json.dumps(value, indent=2).encode()
    else:
        if mutation == "schema": value["schema_version"] = True
        elif mutation == "unknown": value["comment"] = "model-controlled"
        elif mutation == "source": value["source"]["run_id"] = "100"
        elif mutation == "head": value["identity"]["head_sha"] = "0" * 40
        elif mutation == "policy": value["identity"]["policy_sha"] = "0" * 40
        elif mutation == "run": value["identity"]["workflow_run_id"] = 101
        elif mutation == "source-run": value["identity"]["source_ci_run_id"] = 100
        elif mutation == "attempt": value["identity"]["run_attempt"] = 2
        elif mutation == "ci": value["source_ci"]["workflow_id"] = 9
        elif mutation == "code": value["trusted_workflow_sha"] = "9" * 40
        elif mutation == "mixed":
            value["identity"]["workflow_run_id"], value["identity"]["source_ci_run_id"] = 42, 100
        elif mutation == "null": value["identity"]["head_repository"] = None
        raw = canonical(value)
    with pytest.raises(h.HandoffError):
        read(pre, raw)  # Recomputed checksum cannot defeat expected identity/schema.
    if mutation not in {"null"}:
        with pytest.raises(h.HandoffError):
            read(pre, raw, original_digest)


@pytest.mark.parametrize("location,key,value", [
    ("facts", "raw_stdout", "::warning::injected"), ("facts", "model_response", "raw provider text"),
    ("facts", "reason", "/host/private"), ("facts", "required_edits", False),
    ("facts", "status", "BASELINE_FAILED"), ("facts", "context_sha256", None),
    ("command", "stdout", "dummy-token"), ("command", "source_sha", "9" * 40),
    ("command", "image_id", "model-selected"), ("command", "passed", True),
    ("counts", "collected", True), ("host", "source", {}), ("host", "status", "unavailable"),
])
def test_untrusted_fact_injection_and_contradictions_fail(tmp_path, location, key, value):
    pre, raw = payload(tmp_path)
    obj = json.loads(raw)
    f = obj["facts"]
    targets = {"facts": f, "command": f["verification"]["commands"][0],
               "counts": f["verification"]["commands"][1]["counts"], "host": f["verification"]["host_result"]}
    if location == "command" and key == "passed":
        targets[location]["exit_code"] = 1
    targets[location][key] = value
    with pytest.raises(h.HandoffError):
        read(pre, canonical(obj))


def test_absolute_report_reference_and_tampered_skill_rejected(tmp_path):
    pre, raw = payload(tmp_path)
    value = json.loads(raw)
    value["facts"]["verification"]["commands"][1]["reports"][0][0] = "/private/report.json"
    with pytest.raises(h.HandoffError):
        read(pre, canonical(value))
    value = json.loads(raw)
    value["facts"]["skills"][0]["loaded_content_sha256"] = "0" * 64
    with pytest.raises(h.HandoffError):
        read(pre, canonical(value))


@pytest.mark.parametrize("receipt", [True, 0, -1, "https://evil", "700", 2**63])
def test_artifact_receipt_is_only_a_bounded_trusted_integer(tmp_path, receipt):
    pre, raw = payload(tmp_path)
    with pytest.raises((ReportingError, h.HandoffError)):
        h.report(read(pre, raw), artifact_id=receipt)


def test_terminal_envelope_is_distinct_and_cannot_contain_execution_facts(tmp_path):
    bundle, _, _ = fixture(tmp_path)
    pre = pre_for(bundle)
    facts = h.terminal_facts("UNSUPPORTED", "docs-only")
    raw = h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    prepared = h.report(read(pre, raw))
    assert prepared.status == "UNSUPPORTED" and h.successful(prepared)
    assert "no execution evidence" in prepared.body and "VERIFIED" not in prepared.body
    with pytest.raises(h.HandoffError):
        h.report(read(pre, raw), artifact_id=700)
    facts["verification"] = {"host_result": "fake"}
    with pytest.raises(h.HandoffError):
        h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)


def test_no_tests_needed_neutral_without_invented_verifier(tmp_path):
    bundle, result, verification = fixture(tmp_path, case="already-covered")
    pre = pre_for(bundle)
    assert verification is None
    facts = h.execution_facts(bundle, result, None, AgentRun(100, 1), patch_prepared=False)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    prepared = h.report(read(pre, raw))
    assert prepared.status == "NO_TESTS_NEEDED" and prepared.delivery.state == DeliveryState.NOT_REQUIRED
    assert h.successful(prepared) and "no execution evidence" in prepared.body


def test_invalid_unicode_advisory_text_is_rejected_before_handoff(tmp_path):
    bundle, result, verification = fixture(tmp_path)
    decision = result.proposal.decisions[0]
    result = replace(result, proposal=replace(result.proposal, decisions=(replace(decision,
                     plan=replace(decision.plan, rationale='\u202e DUMMY_RAW_MODEL_TEXT')),)))
    with pytest.raises(h.HandoffError, match='advisory text rejected') as caught:
        h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    assert 'DUMMY_RAW_MODEL_TEXT' not in str(caught.value)


def test_agent_review_failure_preserves_actual_host_verdict(tmp_path):
    from automation.test_agent.agent import State
    bundle, result, verification = fixture(tmp_path)
    result = replace(result, state=State.FAILED, failure='feedback unavailable')
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    prepared = h.report(read(pre, raw), artifact_id=700)
    assert prepared.status == 'AGENT_FAILED' and 'Host verification: **VERIFIED**' in prepared.body
    assert not h.successful(prepared)


def test_advisory_action_cannot_contradict_accepted_edit_facts(tmp_path):
    pre, raw = payload(tmp_path)
    value = json.loads(raw)
    value['facts']['decisions'][0]['action'] = 'no-tests-needed'
    with pytest.raises(h.HandoffError):
        read(pre, canonical(value))


def test_validated_advisory_plan_recommendations_and_both_skill_hashes_survive(tmp_path):
    bundle, result, verification = fixture(tmp_path)
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    value = read(pre, raw)
    for fact, decision in zip(value['facts']['decisions'], result.proposal.decisions):
        assert set(fact) == {'target', 'action', 'changed_behavior', 'rationale'}
        assert fact['changed_behavior'] == decision.plan.changed_behavior
        assert fact['rationale'] == decision.plan.rationale
    assert value['facts']['recommendations'] == list(result.recommendations) == ['accept']
    body = h.report(value, artifact_id=700).body
    for decision in result.proposal.decisions:
        assert 'Changed behavior: ' + inert_text(decision.plan.changed_behavior) in body
        assert 'Rationale: ' + inert_text(decision.plan.rationale) in body
    assert 'Review recommendations (advisory): accept' in body
    for skill in value['facts']['skills']:
        expected = (f"Loaded skill: {inert_text(skill['skill_id'])} {inert_text(skill['version'])} ({skill['target']}); "
                    f"content SHA-256 `{skill['content_sha256']}`; loaded SHA-256 `{skill['loaded_content_sha256']}`")
        assert expected in body
    assert 'Host verification: **VERIFIED**' in body


@pytest.mark.parametrize('field', ['changed_behavior', 'rationale'])
@pytest.mark.parametrize('text', [
    '@octocat @org/team', '<b>HTML</b><script>alert(1)</script>', '[click](javascript:alert(1))',
    '`code` ```fence', '# heading | table |', '<!-- taskly-unit-test-agent-report:v2 -->',
])
def test_advisory_markdown_html_and_mentions_stay_inert_after_handoff(tmp_path, field, text):
    bundle, result, verification = fixture(tmp_path)
    decision = result.proposal.decisions[0]
    result = replace(result, proposal=replace(result.proposal, decisions=(replace(
        decision, plan=replace(decision.plan, **{field: text})),)))
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    value = read(pre, raw)
    assert value['facts']['decisions'][0][field] == text
    prepared = h.report(value)
    assert inert_text(text, MAX_ADVISORY_BYTES) in prepared.body
    assert text not in prepared.body
    assert '@' not in prepared.body and '<b>' not in prepared.body and '<script>' not in prepared.body
    assert 'javascript:' not in prepared.body and '```' not in prepared.body
    assert '<!--' not in prepared.body.split('\n', 1)[1]
    assert parse_marker(prepared.body) == prepared.identity
    assert prepared.status == 'VERIFIED'


@pytest.mark.parametrize('field', ['changed_behavior', 'rationale'])
@pytest.mark.parametrize('text', [
    None, 1, True, [], '', '   ', 'x' * 4097, '\x00', '\n', '\t', '\x1b[31m', '\x7f',
    '\x85', '\u202e', '\u200b', '\u2028', '\u2029', '\ud800',
])
def test_invalid_advisory_type_contract_length_and_characters_fail_closed(tmp_path, field, text):
    pre, raw = payload(tmp_path)
    value = json.loads(raw)
    value['facts']['decisions'][0][field] = text
    # A freshly recomputed digest cannot bypass the nested text contract.
    invalid = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    with pytest.raises(h.HandoffError):
        read(pre, invalid)
    with pytest.raises(h.HandoffError):
        h.report(value)


@pytest.mark.parametrize('field', ['changed_behavior', 'rationale'])
@pytest.mark.parametrize('text', ['x' * 4096, '😀' * 4096, '<>@' * 1365])
def test_contract_valid_long_advisory_is_retained_but_display_clips_to_t06_budget(tmp_path, field, text):
    bundle, result, verification = fixture(tmp_path)
    decision = result.proposal.decisions[0]
    result = replace(result, proposal=replace(result.proposal, decisions=(replace(
        decision, plan=replace(decision.plan, **{field: text})),)))
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    value = read(pre, raw)
    assert value['facts']['decisions'][0][field] == text
    assert len(raw) <= h.MAX_HANDOFF_BYTES == 192 * 1024
    rendered = inert_text(text, MAX_ADVISORY_BYTES)
    assert rendered.endswith(' [clipped]') and len(rendered.encode()) <= MAX_ADVISORY_BYTES
    assert rendered in h.report(value).body


@pytest.mark.parametrize('mutation', ['missing-behavior', 'missing-rationale', 'raw-response', 'edit-content'])
def test_advisory_decision_exact_schema_excludes_raw_and_edit_fields(tmp_path, mutation):
    pre, raw = payload(tmp_path)
    value = json.loads(raw)
    decision = value['facts']['decisions'][0]
    if mutation == 'missing-behavior': del decision['changed_behavior']
    elif mutation == 'missing-rationale': del decision['rationale']
    elif mutation == 'raw-response': decision['raw_response'] = 'DUMMY_PROVIDER_RESPONSE'
    else: decision['content'] = 'DUMMY_EDIT_CONTENT'
    with pytest.raises(h.HandoffError): read(pre, canonical(value))


def test_raw_provider_response_prompt_trace_and_edit_contents_are_not_transported(tmp_path):
    from automation.test_agent.agent import UNIT_TEST_AGENT
    from automation.tests.t04_fixtures import transcript
    bundle, result, verification = fixture(tmp_path)
    trace = canonical({'event': 'response', 'raw_response': 'DUMMY_RAW_PROVIDER_RESPONSE',
                       'prompt': 'DUMMY_RAW_PROVIDER_PROMPT', 'text': 'DUMMY_RAW_TRACE_TEXT'}).decode()
    result = replace(result, trace=(*result.trace, trace))
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    value = read(pre, raw)
    for forbidden in ('DUMMY_RAW_PROVIDER_RESPONSE', 'DUMMY_RAW_PROVIDER_PROMPT', 'DUMMY_RAW_TRACE_TEXT', UNIT_TEST_AGENT):
        assert forbidden.encode() not in raw
    for response in transcript(bundle, feedback=verification.host_result):
        assert response not in raw.decode()
    for edit in result.proposal.edits:
        assert edit.content not in raw.decode()
    assert all(set(e) == {'target', 'path', 'operation'} for e in value['facts']['edits'])
    assert set(value['facts']['decisions'][0]) == {'target', 'action', 'changed_behavior', 'rationale'}


@pytest.mark.parametrize('reason', ['docs-only', 'fork-disabled', 'source-ci-unsuccessful'])
def test_preagent_terminal_envelopes_never_invent_advisory_or_execution_facts(tmp_path, reason):
    bundle, _, _ = fixture(tmp_path)
    pre = pre_for(bundle)
    if reason == 'fork-disabled':
        pre = replace(pre, identity=replace(pre.identity, head_repository='contributor/taskly', head_repository_id=202))
    elif reason == 'source-ci-unsuccessful':
        pre = replace(pre, source_ci={**pre.source_ci, 'conclusion': 'failure'})
    facts = h.terminal_facts('UNAVAILABLE' if reason == 'source-ci-unsuccessful' else 'UNSUPPORTED', reason)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    value = read(pre, raw)
    assert value['facts']['decisions'] == value['facts']['recommendations'] == value['facts']['skills'] == []
    assert value['facts']['verification'] is None and value['facts']['edits'] == []
    assert 'changed_behavior' not in raw.decode() and 'rationale' not in raw.decode()
    body = h.report(value).body
    assert 'Changed behavior:' not in body and 'Rationale:' not in body and 'Review recommendations' not in body
    assert 'no execution evidence' in body
    value['facts']['decisions'] = [{'target': 'frontend', 'action': 'unsupported',
                                  'changed_behavior': 'Invented behavior', 'rationale': 'Invented rationale'}]
    with pytest.raises(h.HandoffError): read(pre, canonical(value))
    value = json.loads(raw)
    value['facts']['recommendations'] = ['accept']
    with pytest.raises(h.HandoffError): read(pre, canonical(value))


@pytest.mark.parametrize('recommendations', [(), ('reject',), ('unavailable',), ('reject', 'unavailable')])
def test_advisory_recommendations_render_without_overriding_host_verification(tmp_path, recommendations):
    bundle, result, verification = fixture(tmp_path)
    result = replace(result, recommendations=recommendations)
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, facts)
    prepared = h.report(read(pre, raw), artifact_id=700)
    if recommendations:
        assert 'Review recommendations (advisory): ' + ', '.join(recommendations) in prepared.body
    else:
        assert 'Review recommendations (advisory)' not in prepared.body
    assert prepared.status == 'VERIFIED' and 'Host verification: **VERIFIED**' in prepared.body
