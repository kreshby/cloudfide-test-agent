"""Fixed host categories survive failures; arbitrary error text stays private."""

from dataclasses import replace

import pytest

from automation.test_agent import handoff as h
from automation.test_agent.agent import State, UnitTestAgent
from automation.test_agent.diagnostics import Diagnostic
from automation.test_agent.provider import ProviderError, canonical
from automation.test_agent.reporting import AgentRun
from automation.tests.t04_fixtures import bundle_for, install_originals
from automation.tests.t06_fixtures import fixture
from automation.tests.test_workflow import pre_for


PRIVATE = "secret /private/path raw model text Authorization: dummy"


@pytest.mark.parametrize("category", list(Diagnostic))
def test_safe_agent_failure_roundtrips_and_renders_fixed_explanation(tmp_path, category):
    bundle, result, verification = fixture(tmp_path)
    failed = replace(result, state=State.FAILED, failure=PRIVATE, diagnostic=category)
    pre = pre_for(bundle)
    facts = h.execution_facts(bundle, failed, verification, AgentRun(100, 1), patch_prepared=True)
    assert facts["reason"] == str(category)
    raw = h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)
    value = h.decode(raw, h.digest(raw), expected_identity=pre.identity,
                     expected_source_ci=pre.source_ci, expected_trusted_sha="8" * 40)
    prepared = h.report(value, artifact_id=700)
    assert prepared.status == "AGENT_FAILED"
    assert h.DIAGNOSTIC_REASONS[category] in prepared.body
    assert PRIVATE.encode() not in raw and PRIVATE not in prepared.body
    assert "Host verification: **VERIFIED**" in prepared.body


@pytest.mark.parametrize("value", [PRIVATE, "provider-json", {}, 7, None])
def test_untrusted_internal_diagnostic_is_replaced_with_fixed_fallback(tmp_path, value):
    bundle, result, verification = fixture(tmp_path)
    failed = replace(result, state=State.FAILED, failure=PRIVATE, diagnostic=value)
    facts = h.execution_facts(bundle, failed, verification, AgentRun(100, 1), patch_prepared=True)
    assert facts["reason"] == "agent-contract-failed"
    assert PRIVATE not in canonical(facts).decode()


@pytest.mark.parametrize("reason", [PRIVATE, "provider-json\n", "", {}, ["provider-json"], 7])
def test_handoff_rejects_nonallowlisted_diagnostics(tmp_path, reason):
    bundle, result, verification = fixture(tmp_path)
    failed = replace(result, state=State.FAILED, diagnostic=Diagnostic.PROVIDER_JSON)
    facts = h.execution_facts(bundle, failed, verification, AgentRun(100, 1), patch_prepared=True)
    facts["reason"] = reason
    pre = pre_for(bundle)
    with pytest.raises(h.HandoffError):
        h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)


@pytest.mark.parametrize("category", [Diagnostic.PROVIDER_HTTP, Diagnostic.PROVIDER_TIMEOUT,
                                      Diagnostic.PROVIDER_JSON, Diagnostic.PROVIDER_TRUNCATED,
                                      Diagnostic.PROVIDER_TOKEN_BUDGET])
def test_provider_exception_text_cannot_enter_agent_trace(tmp_path, category):
    bundle = bundle_for("frontend-generate")
    install_originals(bundle, tmp_path)
    class FailedProvider:
        def infer(self, *_):
            raise ProviderError(PRIVATE, diagnostic=category)
    result = UnitTestAgent(bundle, tmp_path, FailedProvider()).run()
    assert result.state == State.FAILED and result.diagnostic == category
    assert result.failure == "provider inference failed"
    assert PRIVATE not in result.to_bytes().decode()


def test_arbitrary_provider_exception_has_fixed_category(tmp_path):
    bundle = bundle_for("frontend-generate")
    class FailedProvider:
        def infer(self, *_):
            raise RuntimeError(PRIVATE)
    result = UnitTestAgent(bundle, tmp_path, FailedProvider()).run()
    assert result.diagnostic == Diagnostic.PROVIDER_HTTP
    assert PRIVATE not in result.to_bytes().decode()


def test_success_cannot_claim_failure_diagnostic(tmp_path):
    bundle, result, verification = fixture(tmp_path)
    facts = h.execution_facts(bundle, result, verification, AgentRun(100, 1), patch_prepared=True)
    facts["reason"] = "provider-json"
    pre = pre_for(bundle)
    with pytest.raises(h.HandoffError):
        h.encode(pre.identity, pre.source, pre.source_ci, "8" * 40, facts)


@pytest.mark.parametrize("phase", [PRIVATE, {}, [PRIVATE], None])
def test_publisher_boundary_rejects_arbitrary_exception_phase(monkeypatch, phase):
    from automation.test_agent import workflow as w
    from automation.test_agent.publisher import PublicationError
    def fail(_):
        raise PublicationError(PRIVATE, phase=phase)
    monkeypatch.setattr(w, "_main", fail)
    with pytest.raises(w.WorkflowError) as caught:
        w.main("publish")
    assert str(caught.value) == "publisher-preflight-unavailable"


def test_direct_report_and_final_artifact_include_only_safe_category(tmp_path):
    from automation.test_agent.artifacts import build_bundle
    from automation.test_agent.reporting import prepare_report
    import json
    bundle, result, verification = fixture(tmp_path)
    failed = replace(result, state=State.FAILED, failure=PRIVATE, diagnostic=Diagnostic.PROVIDER_JSON)
    prepared = prepare_report(bundle, failed, verification, publication=AgentRun(100, 1))
    assert "`provider-json`" in prepared.body and PRIVATE not in prepared.body
    artifact = build_bundle(bundle, failed, tmp_path, verification, publication=AgentRun(100, 1))
    metadata = json.loads(next(e.data for e in artifact.entries if e.name == "report-metadata.json"))
    assert metadata["diagnostic"] == "provider-json"
    assert PRIVATE not in canonical(metadata).decode()
