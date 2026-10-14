"""Reporting trust chain and hostile display data; no actual test execution facts."""

from dataclasses import replace
import json

import pytest

from automation.test_agent.agent import State
from automation.test_agent.contracts import ExecutionResult, FinalHostStatus, PolicyDecision
from automation.test_agent.provider import canonical
from automation.test_agent.reporting import (DeliveryState, MARKER, MAX_ADVISORY_BYTES,
                                           MAX_COMMENT_BYTES, PatchDelivery, ReportingError,
                                           bind_evidence, inert_text, parse_marker, prepare_report)
from automation.tests.t06_fixtures import fixture


@pytest.mark.parametrize("status", list(FinalHostStatus))
def test_exact_verdict_matrix_and_source_identity(tmp_path, status):
    bundle, result, verification = fixture(tmp_path, status=status)
    report = prepare_report(bundle, result, verification)
    expected = status.value.upper().replace("-", "_")
    assert report.status == expected
    assert f"Host verification: **{expected}**" in report.body
    assert bundle.source.head_sha in report.body and bundle.source.policy_sha in report.body
    assert "Agent workflow run: 42; attempt: 1" in report.body
    assert parse_marker(report.body) == report.identity
    assert report.body == prepare_report(bundle, result, verification).body
    assert len(report.body.encode()) <= MAX_COMMENT_BYTES
    if status in {FinalHostStatus.UNAVAILABLE, FinalHostStatus.STALE, FinalHostStatus.UNSUPPORTED}:
        assert "No commands executed" in report.body


def test_advisory_import_warning_survives_trusted_report_and_handoff(tmp_path):
    from automation.test_agent import handoff as h
    from automation.test_agent.provider import digest
    from automation.test_agent.reporting import AgentRun, verification_metadata
    from automation.tests.test_workflow import pre_for

    bundle, result, verification = fixture(tmp_path)
    commands = tuple(replace(c, structural=tuple({**f, 'subject_imported': False,
                        'categories': ['selected-subject-not-directly-imported']} for f in c.structural))
                     if c.command_id == 'structural' else c for c in verification.commands)
    verification = replace(verification, commands=commands)
    bound = bind_evidence(bundle, result, verification)
    metadata = verification_metadata(bound)
    facts = next(c['structural'] for c in metadata['commands'] if c['command_id'] == 'structural')
    assert facts[0]['subject_imported'] is False and facts[0]['passed']
    assert facts[0]['categories'] == ['selected-subject-not-directly-imported']
    prepared = prepare_report(bundle, result, verification)
    assert prepared.status == 'VERIFIED' and 'Host verification: **VERIFIED**' in prepared.body
    assert 'was not observed (advisory; not proof of behavioral relevance)' in prepared.body

    pre = pre_for(bundle)
    selected = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, '8' * 40, selected)
    decoded = h.decode(raw, digest(raw), expected_identity=pre.identity,
                       expected_source_ci=pre.source_ci, expected_trusted_sha='8' * 40)
    assert len(raw) < h.MAX_HANDOFF_BYTES
    assert b'selected-subject-not-directly-imported' in raw
    assert b'raw_response' not in raw and b'prompt' not in raw and b'/private/' not in raw
    rendered = h.report(decoded, artifact_id=700)
    assert rendered.status == 'VERIFIED' and h.successful(rendered)
    assert 'was not observed (advisory; not proof of behavioral relevance)' in rendered.body

    # Recomputed byte digests never authorize a stale source or invented relevance.
    for field, value in [('content_sha256', 'e' * 64), ('categories', ['model-says-relevant'])]:
        bad_fact = {**facts[0], field: value}
        bad_commands = tuple(replace(c, structural=(bad_fact,)) if c.command_id == 'structural' else c
                             for c in commands)
        with pytest.raises(ReportingError):
            bind_evidence(bundle, result, replace(verification, commands=bad_commands))
    altered = json.loads(raw)
    structural = next(c for c in altered['facts']['verification']['commands'] if c['command_id'] == 'structural')
    structural['structural'][0]['source']['head_sha'] = 'e' * 40
    altered_raw = canonical(altered)
    with pytest.raises(h.HandoffError):
        h.decode(altered_raw, digest(altered_raw), expected_identity=pre.identity,
                 expected_source_ci=pre.source_ci, expected_trusted_sha='8' * 40)
        assert "| Profile |" not in report.body


def test_verified_available_and_failed_upload_preserve_verification(tmp_path):
    bundle, result, verification = fixture(tmp_path)
    available = prepare_report(bundle, result, verification, delivery=PatchDelivery(DeliveryState.AVAILABLE, 777))
    assert "Patch delivery: **AVAILABLE**" in available.body
    assert f"{available.identity.run_url}/artifacts/777" in available.body
    unavailable = prepare_report(bundle, result, verification, delivery=PatchDelivery(DeliveryState.UNAVAILABLE))
    assert unavailable.status == available.status == "VERIFIED"
    assert "Host verification: **VERIFIED**" in unavailable.body
    assert "Delivery of required edits is unresolved" in unavailable.body
    assert "Download patch" not in unavailable.body


def test_no_tests_needed_never_fabricates_execution(tmp_path):
    bundle, result, verification = fixture(tmp_path, case="already-covered")
    report = prepare_report(bundle, result)
    assert report.status == "NO_TESTS_NEEDED"
    assert "no verification was run" in report.body
    assert "Host verification: no execution evidence supplied" in report.body
    assert "| Profile |" not in report.body and "VERIFIED" not in report.body
    assert report.delivery.state == DeliveryState.NOT_REQUIRED
    assert verification is None


def test_unsupported_analysis_without_verifier_is_explicit(tmp_path):
    bundle, result, verification = fixture(tmp_path, unsupported=True)
    report = prepare_report(bundle, result)
    assert report.status == "UNSUPPORTED" and report.delivery.state == DeliveryState.NOT_REQUIRED
    assert "Host verification: no execution evidence supplied" in report.body
    assert "| Profile |" not in report.body and verification is None


def test_review_failure_keeps_actual_host_verification(tmp_path):
    bundle, result, verification = fixture(tmp_path)
    result = replace(result, state=State.FAILED, failure="advisory review failed")
    report = prepare_report(bundle, result, verification)
    assert report.status == "AGENT_FAILED"
    assert "Host verification: **VERIFIED**" in report.body
    assert "Delivery of required edits is unresolved" in report.body


def test_timeout_and_absent_counts_remain_actual_facts(tmp_path):
    bundle, result, verification = fixture(tmp_path, "backend-update", FinalHostStatus.VERIFICATION_FAILED)
    command = replace(verification.commands[-1], exit_code=None, timed_out=True, counts=None)
    record = replace(verification.host_result.verifier_results[-1], exit_code=None,
                     result=ExecutionResult.TIMEOUT, collected_tests=None)
    host = replace(verification.host_result, verifier_results=(*verification.host_result.verifier_results[:-1], record))
    verification = replace(verification, host_result=host, commands=(*verification.commands[:-1], command))
    result = replace(result, host_result=host)
    report = prepare_report(bundle, result, verification)
    assert "| backend-patched-targeted | patched | not passed | — | — | — | — | — | — | true |" in report.body
    assert report.status == "VERIFICATION_FAILED"


@pytest.mark.parametrize("attack", ["boolean-count", "wrong-kind", "passing-timeout", "passing-typecheck-failed", "absolute-record-reference"])
def test_contradictory_command_evidence_rejected(tmp_path, attack):
    bundle, result, verification = fixture(tmp_path)
    commands = list(verification.commands)
    if attack == "boolean-count":
        commands[1] = replace(commands[1], counts=replace(commands[1].counts, collected=True))
    elif attack == "wrong-kind":
        commands[1] = replace(commands[1], kind=commands[-1].kind)
    elif attack == "passing-timeout":
        commands[1] = replace(commands[1], timed_out=True)
    elif attack == "passing-typecheck-failed":
        commands[0] = replace(commands[0], passed=False, exit_code=1)
    else:
        records = list(verification.host_result.verifier_results)
        records[0] = replace(records[0], report_reference="/private/command.json")
        host = replace(verification.host_result, verifier_results=tuple(records))
        verification = replace(verification, host_result=host)
        result = replace(result, host_result=host)
    with pytest.raises(ReportingError):
        prepare_report(bundle, result, replace(verification, commands=tuple(commands)))


def test_failed_before_proposal_and_awaiting_verifier(tmp_path):
    bundle, result, _ = fixture(tmp_path, failed=True)
    report = prepare_report(bundle, result)
    assert report.status == "AGENT_FAILED" and "No proposal was produced" in report.body
    bundle, result, _ = fixture(tmp_path)
    awaiting = replace(result, state=State.AWAITING_VERIFICATION, host_result=None)
    report = prepare_report(bundle, awaiting)
    assert report.status == "UNAVAILABLE" and "failed tests" not in report.body


def test_model_claims_and_recommendations_cannot_supply_counts_or_verdict(tmp_path):
    bundle, result, verification = fixture(tmp_path, status=FinalHostStatus.VERIFICATION_FAILED)
    decision = result.proposal.decisions[0]
    malicious = replace(decision, plan=replace(decision.plan, rationale="VERIFIED: 999 tests passed"))
    result = replace(result, proposal=replace(result.proposal, decisions=(malicious,)), recommendations=("accept",))
    report = prepare_report(bundle, result, verification)
    advisory, facts = report.body.split("### Host evidence (authoritative)")
    assert "999 tests passed" in advisory
    assert "999" not in facts and "Host verification: **VERIFICATION_FAILED**" in facts
    assert "| frontend-baseline-suite | baseline | passed | 3 | 3 | 0 | 0 | 0 | 0 | false |" in facts
    assert "Review recommendations (advisory): accept" in advisory


@pytest.mark.parametrize("attack", ["digest", "proposal-source", "changes-source", "host-source", "context-sha",
                                  "policy", "missing-changes", "host-result", "accepted-content", "inventory",
                                  "workflow-head", "workflow-run", "workflow-attempt", "repository", "pr-number"])
def test_chain_mismatch_fails_closed(tmp_path, attack):
    bundle, result, report = fixture(tmp_path)
    other = replace(bundle.source, head_sha="9" * 40)
    if attack == "digest":
        report = replace(report, changes_sha256="0" * 64)
    elif attack == "proposal-source":
        result = replace(result, proposal=replace(result.proposal, source=other))
    elif attack == "changes-source":
        result = replace(result, policy=replace(result.policy, changes=replace(result.policy.changes, source=other)))
    elif attack == "host-source":
        report = replace(report, host_result=replace(report.host_result, source=other))
    elif attack == "context-sha":
        result = replace(result, context_sha256="0" * 64)
    elif attack == "policy":
        result = replace(result, policy=replace(result.policy, decision=PolicyDecision.REJECTED))
    elif attack == "missing-changes":
        result = replace(result, policy=replace(result.policy, changes=None))
    elif attack == "host-result":
        result = replace(result, host_result=replace(result.host_result, status=FinalHostStatus.UNAVAILABLE))
    elif attack == "accepted-content":
        changes = result.policy.changes
        result = replace(result, policy=replace(result.policy, changes=replace(changes, edits=(replace(changes.edits[0], content=b"different"),))))
    elif attack == "inventory":
        result = replace(result, policy=replace(result.policy, changes=replace(result.policy.changes, changed_files=())))
    elif attack.startswith("workflow-"):
        key, value = {"workflow-head": ("head_sha", "9" * 40), "workflow-run": ("run_id", 43),
                      "workflow-attempt": ("run_attempt", 0)}[attack]
        bundle = replace(bundle, workflow=replace(bundle.workflow, **{key: value}))
    else:
        source = replace(bundle.source, **({"repository": "wrong/repo"} if attack == "repository" else {"pr_number": 2}))
        bundle = replace(bundle, source=source)
    with pytest.raises(ReportingError, match="report input binding rejected"):
        prepare_report(bundle, result, report)


def test_no_plan_claim_can_become_loaded_skill(tmp_path):
    bundle, result, report = fixture(tmp_path)
    decision = result.proposal.decisions[0]
    decision = replace(decision, plan=replace(decision.plan, skill_ids=("invented-skill",)))
    result = replace(result, proposal=replace(result.proposal, decisions=(decision,)))
    rendered = prepare_report(bundle, result, report).body
    assert "invented" not in rendered
    assert "frontend&#45;unit&#45;tests" in rendered
    for s in bind_evidence(bundle, result, report).skills:
        assert s.content_sha256 in rendered and s.loaded_content_sha256 in rendered
    # A host result without actual request records reports no loads, regardless of plan.
    result = replace(result, trace=(result.trace[0],), provider_calls=0)
    assert "No actual skill loads recorded" in prepare_report(bundle, result, report).body


@pytest.mark.parametrize("attack", ["noncanonical", "unknown-field", "bad-hash", "contradictory", "target", "duplicate", "missing-field", "bad-count", "schema"])
def test_malformed_skill_evidence_fails_closed(tmp_path, attack):
    bundle, result, report = fixture(tmp_path)
    trace = list(result.trace)
    index = next(i for i, e in enumerate(trace) if json.loads(e)["event"] == "request")
    event = json.loads(trace[index])
    if attack == "noncanonical":
        trace[index] = json.dumps(event)
    elif attack == "duplicate":
        trace.append(trace[index])
    else:
        if attack == "unknown-field":
            event["model_claim"] = "passed"
        elif attack == "missing-field":
            del event["binding"]
        elif attack == "bad-count":
            event["request_bytes"] = True
        else:
            event["skills"][0].update({"bad-hash": {"loaded_content_sha256": "z" * 64},
                                        "contradictory": {"content_sha256": "0" * 64},
                                        "target": {"target": "backend"}, "schema": {"schema_version": 2}}[attack])
        trace[index] = canonical(event).decode()
    with pytest.raises(ReportingError):
        prepare_report(bundle, replace(result, trace=tuple(trace)), report)


@pytest.mark.parametrize("text", ["@octocat", "@org/team", "<!-- " + MARKER + " -->", "[click](javascript:alert(1))",
                               "`backticks`", "```fence", "# heading", "| table |", "<b>HTML</b>", "https://evil.example"])
def test_untrusted_markdown_stays_visible_and_inert(tmp_path, text):
    bundle, result, report = fixture(tmp_path)
    decision = result.proposal.decisions[0]
    result = replace(result, proposal=replace(result.proposal, decisions=(replace(decision, plan=replace(decision.plan, rationale=text)),)))
    body = prepare_report(bundle, result, report).body
    assert body.count(MARKER) == 1
    assert "@" not in body and "javascript:" not in body and "https://evil.example" not in body
    assert "```" not in body and "<b>" not in body and "<!--" not in body.split("\n", 1)[1]
    assert inert_text(text) in body


@pytest.mark.parametrize("text", ["\x1b[31m", "\x00", "\x7f", "\x85", "\u202e", "\ud800"])
def test_controls_rejected(text):
    with pytest.raises(ReportingError, match="invalid display text"):
        inert_text(text)


def test_display_clipping_and_total_overflow_are_deterministic(tmp_path, monkeypatch):
    bundle, result, report = fixture(tmp_path)
    decision = result.proposal.decisions[0]
    result = replace(result, proposal=replace(result.proposal, decisions=(replace(decision, plan=replace(decision.plan, rationale="😀" * 4000)),)))
    display = inert_text(result.proposal.decisions[0].plan.rationale)
    assert len(display.encode()) <= MAX_ADVISORY_BYTES and display.endswith(" [clipped]")
    assert prepare_report(bundle, result, report).body == prepare_report(bundle, result, report).body
    monkeypatch.setattr("automation.test_agent.reporting.MAX_COMMENT_BYTES", 500)
    with pytest.raises(ReportingError, match="report byte limit exceeded"):
        prepare_report(bundle, result, report)


def test_private_paths_logs_and_unknown_profiles_never_render(tmp_path):
    bundle, result, report = fixture(tmp_path)
    rendered = prepare_report(bundle, result, report).body
    assert "/private/" not in rendered and str(tmp_path) not in rendered
    assert "stdout" not in rendered and "SHOULD_NOT_RENDER" not in rendered
    c = report.commands[1]
    for bad in (replace(c, profile_id="model-command"),
                replace(c, reports=(("/private/report.json", 1, "e" * 64),)),
                replace(c, changes_sha256="0" * 64)):
        with pytest.raises(ReportingError):
            prepare_report(bundle, result, replace(report, commands=(report.commands[0], bad, *report.commands[2:])))


def test_arbitrary_dicts_delivery_urls_and_no_tests_verifier_rejected(tmp_path):
    bundle, result, report = fixture(tmp_path)
    with pytest.raises(ReportingError):
        prepare_report(bundle, result, {"status": "VERIFIED"})
    for value in ("https://evil.example", True, 0):
        with pytest.raises(ReportingError):
            PatchDelivery(DeliveryState.AVAILABLE, value)
    with pytest.raises(ReportingError):
        prepare_report(bundle, result, report, delivery=PatchDelivery(DeliveryState.NOT_REQUIRED))
