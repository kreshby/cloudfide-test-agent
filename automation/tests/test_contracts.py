"""Behavior of the public versioned model boundary and separate host types."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from automation.test_agent.contracts import (
    ContractError, ExecutionKind, ExecutionResult, FinalHostStatus, HostResult,
    MAX_PAYLOAD_BYTES, ModelAction, ModelProposal, ProposedEdit, SkillUseRecord,
    SourceIdentity, Target, TargetDecision, TestPlan as PlanContract, VerifierResult,
)


def source():
    return SourceIdentity("owner/repository", 12, "a" * 40, "b" * 40,
                          "c" * 40, "d" * 40, "run-12")


def decision(target="frontend", action="no-tests-needed", files=()):
    return {
        "target": target, "action": action,
        "plan": {
            "changed_behavior": "Validation accepts the documented value",
            "existing_test_evidence": ["Existing validation test covers the value"],
            "rationale": "Preserve the documented behavior",
            "intended_test_files": list(files), "skill_ids": [],
        },
    }


def payload():
    return {"schema_version": 1, "source": asdict(source()),
            "decisions": [decision(), decision("backend", "unsupported")], "edits": []}


def test_both_targets_and_non_edit_actions_coexist():
    result = ModelProposal.from_payload(json.dumps(payload()))
    assert [(item.target, item.action) for item in result.decisions] == [
        (Target.FRONTEND, ModelAction.NO_TESTS_NEEDED),
        (Target.BACKEND, ModelAction.UNSUPPORTED),
    ]
    assert result.source == source()
    assert result.decisions[0].plan.skill_ids == ()


@pytest.mark.parametrize("field", [
    "verified", "final_status", "verifier_success", "host_status", "trusted_result",
    "changed_files", "permitted_test_paths", "repository_root", "max_files",
    "max_file_bytes", "max_total_bytes", "command", "permissions", "network",
    "skill_uses", "byte_size", "unexpected",
])
@pytest.mark.parametrize("location", ["proposal", "source", "decision", "plan", "edit"])
def test_unknown_fields_cannot_inject_host_state_or_policy(field, location):
    data = payload()
    data["edits"] = [{"target": "frontend", "path": "frontend/src/new.test.ts",
                      "operation": "create", "original_sha256": None,
                      "content": "it('works', () => expect(1).toBe(1))\n"}]
    selected = {
        "proposal": data, "source": data["source"], "decision": data["decisions"][0],
        "plan": data["decisions"][0]["plan"], "edit": data["edits"][0],
    }[location]
    selected[field] = True
    with pytest.raises(ContractError):
        ModelProposal.from_payload(data)


@pytest.mark.parametrize("data", [
    {"verified": True}, {}, [], None, "not JSON",
    '{"schema_version":1,"schema_version":1}',
    '{"source":{"head_sha":"a","head_sha":"b"}}',
])
def test_malformed_payloads_and_duplicate_json_fields_fail(data):
    with pytest.raises(ContractError):
        ModelProposal.from_payload(data)


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 2), ("schema_version", "1"),
    ("decisions", []), ("decisions", {}), ("edits", {}),
])
def test_malformed_top_level_schema(key, value):
    data = payload()
    data[key] = value
    with pytest.raises(ContractError):
        ModelProposal.from_payload(data)


@pytest.mark.parametrize("key,value", [
    ("repository", "https://example.com/owner/repo"), ("pr_number", True),
    ("pr_number", 0), ("head_sha", "z" * 40), ("base_sha", "A" * 40),
    ("merge_base_sha", "a" * 39), ("policy_sha", "a" * 64),
    ("run_id", ""), ("schema_version", False),
])
def test_invalid_source_identity(key, value):
    data = payload()
    data["source"][key] = value
    with pytest.raises(ContractError):
        ModelProposal.from_payload(data)


def test_optional_identity_and_skill_contract_without_fabricated_usage():
    data = asdict(source())
    data.update(pr_number=None, run_id=None)
    assert SourceIdentity.from_mapping(data).pr_number is None
    record = SkillUseRecord("validation", "v1", "a" * 64, "b" * 64, Target.BACKEND)
    assert record.target == Target.BACKEND
    assert payload()["decisions"][0]["plan"]["skill_ids"] == []
    with pytest.raises(ContractError):
        SkillUseRecord("validation", "v1", "invalid", "b" * 64, Target.BACKEND)


@pytest.mark.parametrize("mutation", ["duplicate-target", "invalid-target", "invalid-action",
                                          "bad-plan", "duplicate-files"])
def test_invalid_decisions(mutation):
    data = payload()
    if mutation == "duplicate-target":
        data["decisions"][1] = deepcopy(data["decisions"][0])
    elif mutation == "invalid-target":
        data["decisions"][0]["target"] = "both"
    elif mutation == "invalid-action":
        data["decisions"][0]["action"] = "verified"
    elif mutation == "bad-plan":
        data["decisions"][0]["plan"]["rationale"] = 1
    else:
        data["decisions"][0]["plan"]["intended_test_files"] = ["a", "a"]
    with pytest.raises(ContractError):
        ModelProposal.from_payload(data)


def verifier(kind=ExecutionKind.BASELINE, **overrides):
    values = dict(profile_id="backend-unit", command_id="pytest-unit",
                  kind=kind, result=ExecutionResult.PASSED, collected_tests=3,
                  exit_code=0, report_reference="evidence/report.txt", output_bytes=20)
    values.update(overrides)
    return VerifierResult(**values)


def test_verifier_and_final_states_are_host_owned_and_distinct():
    results = (verifier(), verifier(ExecutionKind.PATCHED))
    result = HostResult(source(), FinalHostStatus.VERIFIED, results)
    assert result.status == FinalHostStatus.VERIFIED
    assert results[0].kind == ExecutionKind.BASELINE
    assert set(FinalHostStatus) == {
        FinalHostStatus.VERIFIED, FinalHostStatus.VERIFICATION_FAILED,
        FinalHostStatus.BASELINE_FAILED, FinalHostStatus.UNAVAILABLE,
        FinalHostStatus.STALE, FinalHostStatus.UNSUPPORTED,
    }
    with pytest.raises(ContractError):
        HostResult(source(), FinalHostStatus.VERIFIED)
    with pytest.raises(ContractError):
        HostResult(source(), FinalHostStatus.VERIFIED, (verifier(),))


@pytest.mark.parametrize("overrides", [
    {"exit_code": 1}, {"collected_tests": 0}, {"collected_tests": True},
    {"result": "passed"}, {"kind": "baseline"}, {"output_bytes": 65537},
    {"output_bytes": -1}, {"report_reference": None},
    {"result": ExecutionResult.FAILED, "exit_code": 0},
    {"result": ExecutionResult.NOT_RUN},
])
def test_inconsistent_or_unbounded_verifier_evidence_rejected(overrides):
    with pytest.raises(ContractError):
        verifier(**overrides)


def test_not_run_verifier_contract_records_no_execution():
    result = verifier(result=ExecutionResult.NOT_RUN, collected_tests=None,
                      exit_code=None, output_bytes=0, report_reference=None)
    assert result.result == ExecutionResult.NOT_RUN


def test_typed_construction_cannot_bypass_contract_validation():
    with pytest.raises(ContractError):
        ProposedEdit(Target.FRONTEND, "frontend/src/test.test.ts", "delete", None, "")
    with pytest.raises(ContractError):
        PlanContract("behavior", [], "reason", (), ())
    with pytest.raises(ContractError):
        TargetDecision("frontend", ModelAction.GENERATE,
                       PlanContract("behavior", (), "reason", (), ()))
    with pytest.raises(ContractError):
        ModelProposal(True, source(), (), ())
    with pytest.raises(ContractError):
        ModelProposal.from_payload(ModelProposal.from_payload(payload()))


def test_model_envelope_resource_bounds():
    with pytest.raises(ContractError):
        ModelProposal.from_payload(" " * (MAX_PAYLOAD_BYTES + 1))
    data = payload()
    data["edits"] = [{}] * 33
    with pytest.raises(ContractError):
        ModelProposal.from_payload(data)
    with pytest.raises(ContractError):
        ModelProposal.from_payload("[" * 2000 + "]" * 2000)
