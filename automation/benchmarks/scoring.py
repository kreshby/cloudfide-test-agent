"""Deterministic host scoring. No repair, execution, writing model edits, or judge LLM."""

import ast
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import statistics
import tempfile

from automation.test_agent.contracts import ContractError, ModelProposal, PolicyDecision
from automation.test_agent.patches import assess_proposal
from automation.test_agent.policy import TrustedContext


MAX_RESPONSE_BYTES = 24 * 1024
FORBIDDEN_FIELDS = frozenset({"status", "host_result", "verified", "verifier_results",
                            "permissions", "commands", "limits", "context", "provider"})


@dataclass(frozen=True)
class Inference:
    raw: str = ""
    failure: str | None = None
    wall_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_per_second: float | None = None


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON field")
        data[key] = value
    return data


def _forbidden(value):
    if isinstance(value, dict):
        return bool(FORBIDDEN_FIELDS.intersection(value)) or any(_forbidden(v) for v in value.values())
    return isinstance(value, list) and any(_forbidden(v) for v in value)


def _python_behavior(content, behavior):
    """Parse syntax as data; require TestClient(app), actual request and response assert."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return False, False
    module = "app.tasks" if behavior == "zero-limit-422" else "app.greeting"
    imported = any(isinstance(n, ast.ImportFrom) and n.module == module and
                   any(a.name == "app" and a.asname is None for a in n.names) for n in tree.body)
    clients = {n.targets[0].id for n in ast.walk(tree) if isinstance(n, ast.Assign) and
               len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and
               isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and
               n.value.func.id == "TestClient" and n.value.args and
               isinstance(n.value.args[0], ast.Name) and n.value.args[0].id == "app"}
    endpoint = "/tasks?limit=0" if behavior == "zero-limit-422" else "/greeting?name=Ada"
    substantive = matched = False
    for test in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")):
        responses = set()
        # Only statements inside tests, excluding comments, docstrings and nested definitions.
        for node in test.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                call = node.value
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and
                        call.func.attr == "get" and isinstance(call.func.value, ast.Name) and
                        call.func.value.id in clients and call.args and
                        isinstance(call.args[0], ast.Constant) and call.args[0].value == endpoint):
                    responses.add(node.targets[0].id)
            if not isinstance(node, ast.Assert):
                continue
            comparison = node.test
            if not (isinstance(comparison, ast.Compare) and len(comparison.ops) == 1 and
                    isinstance(comparison.ops[0], ast.Eq) and len(comparison.comparators) == 1):
                continue
            left, right = comparison.left, comparison.comparators[0]
            if (isinstance(left, ast.Attribute) and left.attr == "status_code" and
                    isinstance(left.value, ast.Name) and left.value.id in responses and
                    isinstance(right, ast.Constant)):
                substantive = imported
                if behavior == "zero-limit-422" and right.value == 422:
                    matched = imported
            if (behavior == "greeting-ada" and isinstance(left, ast.Call) and
                    isinstance(left.func, ast.Attribute) and left.func.attr == "json" and
                    isinstance(left.func.value, ast.Name) and left.func.value.id in responses):
                try:
                    matched = matched or (imported and ast.literal_eval(right) == {"message": "Hello Ada"})
                    substantive = substantive or matched
                except (ValueError, TypeError):
                    pass
    return substantive, matched


def _frontend_behavior(content, behavior):
    # Conservative lexical rubric: comments cannot supply evidence. This is not a verifier.
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", content, flags=re.S)
    component = "SubmitButton" if behavior == "button-trim" else "TaskCount"
    imported = re.search(rf"import\s*\{{[^}}]*\b{component}\b[^}}]*\}}\s*from\s*['\"]\./{component}['\"]", code)
    substantive = bool(imported and re.search(r"\b(?:it|test)\s*\(", code) and
                       re.search(rf"\brender\s*\(\s*<{component}\b", code) and
                       re.search(r"expect\s*\(\s*screen\.", code))
    if behavior == "button-trim":
        matched = all(re.search(pattern, code) for pattern in (
            r"name\s*=\s*(?:['\"]\s+['\"]|\{\s*['\"]\s+['\"]\s*\})",
            r"name\s*=\s*(?:['\"]Alice['\"]|\{\s*['\"]Alice['\"]\s*\})",
            r"expect\s*\(\s*screen\.getByRole\s*\(\s*['\"]button['\"][^;]{0,250}\)\s*\)\s*\.toBeDisabled\s*\(\s*\)",
            r"expect\s*\(\s*screen\.getByRole\s*\(\s*['\"]button['\"][^;]{0,250}\)\s*\)\s*\.toBeEnabled\s*\(\s*\)"))
    else:
        matched = all(re.search(pattern, code) for pattern in (
            r"count\s*=\s*\{\s*0\s*\}", r"count\s*=\s*\{\s*2\s*\}",
            r"expect\s*\(\s*screen\.queryByText\s*\(\s*['\"]0 tasks['\"]\s*\)\s*\)\s*\.toBeNull\s*\(\s*\)",
            r"expect\s*\(\s*screen\.getByText\s*\(\s*['\"]2 tasks['\"]\s*\)\s*\)\s*\.toBeInTheDocument\s*\(\s*\)"))
    return substantive, bool(substantive and matched)


def score(scenario, inference):
    encoded = inference.raw.encode("utf-8", errors="replace")
    truncated = len(encoded) > MAX_RESPONSE_BYTES
    raw = encoded[:MAX_RESPONSE_BYTES].decode("utf-8", errors="ignore")
    failure = inference.failure or ("response-byte-limit" if truncated else None)
    gates = {name: False for name in ("response_produced", "raw_json_valid", "contract_valid",
                                     "policy_accepted", "correct_decisions", "correct_operations",
                                     "expected_paths", "no_forbidden_fields", "substantive_tests",
                                     "behavior_evidence")}
    result = {"id": scenario.id, "failure": failure, "raw_response": raw,
              "response_bytes": len(encoded), "response_truncated": truncated,
              "inference_wall_seconds": inference.wall_seconds,
              "prompt_tokens": inference.prompt_tokens, "completion_tokens": inference.completion_tokens,
              "completion_tokens_per_second": inference.tokens_per_second,
              "policy_rejection_reason": None, "contract_rejection_reason": None,
              "targets": [], "gates": gates}
    gates["response_produced"] = bool(raw.strip()) and failure is None
    proposal = None
    if failure is None:
        try:
            payload = json.loads(raw, object_pairs_hook=_unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            gates["raw_json_valid"] = type(payload) is dict
            gates["no_forbidden_fields"] = gates["raw_json_valid"] and not _forbidden(payload)
        except (ValueError, RecursionError):
            pass
        try:
            proposal = ModelProposal.from_payload(raw)
            gates["contract_valid"] = True
        except ContractError as error:
            result["contract_rejection_reason"] = str(error)
        if proposal is not None:
            with tempfile.TemporaryDirectory(prefix="p01-policy-") as temporary:
                root = Path(temporary).resolve()
                # Only trusted fixture originals are written. No proposed content is written.
                for path, content in scenario.existing_tests:
                    original = root / path
                    original.parent.mkdir(parents=True, exist_ok=True)
                    original.write_text(content, encoding="utf-8")
                    original.chmod(0o644)
                context = TrustedContext(root, scenario.source,
                                         frozenset(path for path, _, _ in scenario.sources),
                                         frozenset(scenario.candidates))
                assessment = assess_proposal(raw, context)
                gates["policy_accepted"] = assessment.decision == PolicyDecision.ACCEPTED
                result["policy_rejection_reason"] = assessment.reason
            actual = {str(d.target): d for d in proposal.decisions}
            for expected in scenario.expected:
                decision = actual.get(expected.target)
                edits = [e for e in proposal.edits if e.target == expected.target]
                correct = decision is not None and decision.action == expected.action
                paths = {e.path for e in edits} == ({expected.path} if expected.path else set())
                operations = ([str(e.operation) for e in edits] == [expected.operation]
                              if expected.operation else not edits)
                if expected.path:
                    substantive, behavior = False, False
                    if len(edits) == 1 and paths:
                        check = _frontend_behavior if expected.target == "frontend" else _python_behavior
                        substantive, behavior = check(edits[0].content, expected.behavior)
                else:
                    substantive = correct and not edits
                    plan = decision.plan if decision else None
                    text = (plan.changed_behavior + " " + plan.rationale).lower() if plan else ""
                    terms = ("negative", "zero") if expected.behavior == "clamp" else ("health", "ok")
                    behavior = bool(substantive and plan and expected.evidence_path in
                                    plan.existing_test_evidence and all(term in text for term in terms))
                result["targets"].append({"target": expected.target, "decision_matched": correct,
                                          "path_matched": paths, "operation_matched": operations,
                                          "substantive_test": substantive, "behavior_matched": behavior})
            exact_targets = set(actual) == {e.target for e in scenario.expected}
            for gate, field in (("correct_decisions", "decision_matched"),
                                ("correct_operations", "operation_matched"),
                                ("expected_paths", "path_matched"),
                                ("substantive_tests", "substantive_test"),
                                ("behavior_evidence", "behavior_matched")):
                gates[gate] = exact_targets and all(t[field] for t in result["targets"])
    result["score"] = sum(gates.values())
    result["maximum_score"] = len(gates)
    result["full_quality"] = all(gates.values())
    return result


def aggregate(cases):
    """All five cases remain in the denominator, including unavailable/timeouts."""
    count = len(cases)
    latency = [c["inference_wall_seconds"] for c in cases if c["inference_wall_seconds"] is not None]
    rates = [c["completion_tokens_per_second"] for c in cases if c["completion_tokens_per_second"] is not None]
    return {
        "cases": count, "score": sum(c["score"] for c in cases),
        "maximum_score": sum(c["maximum_score"] for c in cases),
        "quality_fraction": sum(c["score"] for c in cases) / (10 * count) if count else 0,
        **{key + "_rate": sum(c["gates"][key] for c in cases) / count if count else 0
           for key in ("raw_json_valid", "contract_valid", "policy_accepted", "correct_decisions")},
        "full_quality_cases": sum(c["full_quality"] for c in cases),
        "median_inference_seconds": statistics.median(latency) if latency else None,
        "total_inference_seconds": math.fsum(latency) if latency else None,
        "median_completion_tokens_per_second": statistics.median(rates) if rates else None,
        "failures": sum(c["failure"] is not None for c in cases),
    }
