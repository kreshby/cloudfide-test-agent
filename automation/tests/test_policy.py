"""Public proposal validation: paths, integrity, host limits and weakening."""

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from automation.test_agent.contracts import ContractError, PolicyDecision, SourceIdentity
from automation.test_agent.patches import assess_proposal, prepare_changes
from automation.test_agent.policy import (
    MAX_EDITED_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, PolicyError, TrustedContext,
)


FRONTEND = "frontend/src/example.test.ts"
BACKEND = "backend/tests/test_example.py"
JS = "it('works', () => { expect(1).toBe(1) })\n"
PY = "def test_works():\n    assert 1 == 1\n"


@pytest.fixture
def identity():
    return SourceIdentity("owner/repository", 12, "a" * 40, "b" * 40,
                          "c" * 40, "d" * 40, "run-12")


@pytest.fixture
def context(tmp_path, identity):
    return TrustedContext(tmp_path, identity, frozenset({"frontend/src/example.ts",
                          "backend/app/example.py"}), frozenset({FRONTEND, BACKEND}))


def edit(path=FRONTEND, content=None, operation="create", original=None):
    target = "backend" if path.startswith("backend/") else "frontend"
    return {"target": target, "path": path, "operation": operation,
            "original_sha256": original,
            "content": content if content is not None else (JS if target == "frontend" else PY)}


def proposal(context, edits):
    decisions = []
    for target in ("frontend", "backend"):
        selected = [item for item in edits if item["target"] == target]
        if not selected:
            continue
        decisions.append({
            "target": target,
            "action": "update" if any(item["operation"] == "update" for item in selected) else "generate",
            "plan": {
                "changed_behavior": "Expected value changes", "existing_test_evidence": [],
                "rationale": "Cover the changed value",
                "intended_test_files": [item["path"] for item in selected], "skill_ids": [],
            },
        })
    return {"schema_version": 1, "source": asdict(context.source),
            "decisions": decisions, "edits": edits}


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o644)
    return target


def update(path, old, new):
    return edit(path, new, "update", hashlib.sha256(old.encode()).hexdigest())


@pytest.mark.parametrize("path", [FRONTEND, "frontend/src/components/example.test.tsx",
                                       BACKEND, "backend/tests/features/test_example.py"])
def test_create_allowed_test_families_without_writing(context, path):
    context = replace(context, permitted_test_paths=frozenset({path}))
    changes = prepare_changes(proposal(context, [edit(path)]), context)
    assert changes.edits[0].path == path
    assert changes.edits[0].mode == 0o644
    assert changes.edits[0].original_sha256 is None
    assert not (context.repository_root / path).exists()


@pytest.mark.parametrize("path,old,new", [
    (FRONTEND, JS, JS.replace("toBe(1)", "toBe(2)")),
    (BACKEND, PY, PY.replace("1 == 1", "1 == 2")),
])
def test_matching_hash_allows_legitimate_assertion_update(context, path, old, new):
    target = write(context.repository_root, path, old)
    changes = prepare_changes(proposal(context, [update(path, old, new)]), context)
    assert changes.edits[0].content == new.encode()
    assert target.read_text() == old


@pytest.mark.parametrize("path,old,new", [
    (FRONTEND, "it('works', () => { expect(1).toBe(1); expect(2).toBe(2) })\n", JS),
    (BACKEND, "def test_works():\n    assert 1 == 1\n    assert 2 == 2\n", PY),
])
def test_obsolete_assertion_can_be_removed_while_test_still_asserts(context, path, old, new):
    write(context.repository_root, path, old)
    assert prepare_changes(proposal(context, [update(path, old, new)]), context).edits[0].content == new.encode()


def test_both_stacks_and_deterministic_host_change_representation(context):
    edits = [edit(FRONTEND), edit(BACKEND)]
    first = prepare_changes(proposal(context, edits), context)
    second = prepare_changes(json.dumps(proposal(context, list(reversed(edits)))), context)
    assert first.to_bytes() == second.to_bytes()
    result = json.loads(first.to_bytes())
    assert result["changed_files"] == sorted(context.changed_files)
    assert result["permitted_test_paths"] == sorted(context.permitted_test_paths)
    assert [item["path"] for item in result["edits"]] == [BACKEND, FRONTEND]
    assert result["edits"][0]["byte_size"] == len(PY.encode())
    assert result["edits"][0]["new_sha256"] == hashlib.sha256(PY.encode()).hexdigest()


@pytest.mark.parametrize("path", [
    "frontend/src/example.ts", "backend/app/example.py", ".github/workflows/ci.yml",
    "automation/test_agent/policy.py", "frontend/package.json", "frontend/package-lock.json",
    "backend/requirements.txt", "backend/pyproject.toml", "pytest.ini", "AGENTS.md",
    "SOLUTION.md", "docs/test_policy.py", "frontend/Dockerfile", "compose.yaml",
    "frontend/vite.config.ts", "frontend/tsconfig.json", "skills/example.test.ts",
    "frontend/src/example.spec.ts", "backend/tests/example_test.py",
])
def test_every_non_allowlisted_family_rejected(context, path):
    with pytest.raises(PolicyError, match="allowlist"):
        prepare_changes(proposal(context, [edit(path)]), context)


@pytest.mark.parametrize("path", [
    "../frontend/src/example.test.ts", "frontend/src/../src/example.test.ts",
    "frontend/src/nested/../../src/example.test.ts", "frontend/src/./example.test.ts",
    "/frontend/src/example.test.ts", "C:/frontend/src/example.test.ts",
    "C:frontend/src/example.test.ts", "C:\\frontend\\src\\example.test.ts",
    "frontend\\src\\..\\src\\example.test.ts", "\\\\server\\share\\example.test.ts",
    "//server/frontend/src/example.test.ts", "frontend//src/example.test.ts",
    "frontend/src/example.test.ts/", "frontend/src/%2e%2e/example.test.ts",
    "frontend/src/\u2024\u2024/example.test.ts", "frontend/src/example.test.ts ",
    "frontend/src/example.test.ts.", "frontend/src/example.test.ts:stream",
    "frontend/src/CON.test.ts", "frontend/src/example\x00.test.ts",
])
def test_traversal_absolute_and_ambiguous_paths_rejected(context, path):
    with pytest.raises((ContractError, PolicyError)):
        prepare_changes(proposal(context, [edit(path)]), context)


def test_repository_escape_does_not_read_or_write_external_target(context, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.test.ts")
    outside.write_text(JS)
    try:
        data = proposal(context, [edit(str(outside))])
        with pytest.raises(PolicyError):
            prepare_changes(data, context)
        assert outside.read_text() == JS
    finally:
        outside.unlink()


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("kind", ["file", "parent", "dangling", "internal"])
def test_symlinks_in_allowed_paths_rejected(context, tmp_path, operation, kind):
    root = context.repository_root
    target = root / FRONTEND
    target.parent.mkdir(parents=True)
    destination = root / "external"
    destination.mkdir()
    if kind == "parent":
        target.parent.rmdir()
        target.parent.symlink_to(destination, target_is_directory=True)
        (destination / "example.test.ts").write_text(JS)
    else:
        destination = destination / "test.ts"
        if kind != "dangling":
            destination.write_text(JS)
        target.symlink_to(destination if kind != "internal" else root / BACKEND)
    item = edit(operation=operation, original=hashlib.sha256(JS.encode()).hexdigest()
                if operation == "update" else None)
    with pytest.raises(PolicyError):
        prepare_changes(proposal(context, [item]), context)


def test_symlink_to_outside_repository_is_rejected(context, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.test.ts")
    outside.write_text(JS)
    target = context.repository_root / FRONTEND
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    try:
        with pytest.raises(PolicyError):
            prepare_changes(proposal(context, [update(FRONTEND, JS, JS)]), context)
        assert outside.read_text() == JS
    finally:
        outside.unlink()


@pytest.mark.parametrize("kind", ["directory", "fifo", "executable", "setuid", "hardlink"])
def test_unsupported_file_types_modes_and_hardlinks_rejected(context, kind):
    target = context.repository_root / FRONTEND
    target.parent.mkdir(parents=True)
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_text(JS)
        target.chmod(0o755 if kind == "executable" else 0o4644 if kind == "setuid" else 0o644)
        if kind == "hardlink":
            os.link(target, target.parent / "linked.test.ts")
    with pytest.raises(PolicyError):
        prepare_changes(proposal(context, [update(FRONTEND, JS, JS)]), context)


@pytest.mark.parametrize("operation", ["delete", "chmod", "rename", "shell", "unknown", None])
def test_unsupported_operations_rejected(context, operation):
    with pytest.raises(ContractError):
        prepare_changes(proposal(context, [edit(operation=operation)]), context)


def test_create_cannot_overwrite_and_update_cannot_create(context):
    target = write(context.repository_root, FRONTEND, JS)
    with pytest.raises(PolicyError, match="overwrite"):
        prepare_changes(proposal(context, [edit()]), context)
    assert target.read_text() == JS
    with pytest.raises(PolicyError, match="existing"):
        prepare_changes(proposal(context, [update(BACKEND, PY, PY)]), context)
    assert not (context.repository_root / BACKEND).exists()


def test_create_original_hash_and_model_file_modes_rejected(context):
    with pytest.raises(ContractError):
        prepare_changes(proposal(context, [edit(original="a" * 64)]), context)
    for field in ("mode", "file_mode", "chmod", "byte_size"):
        item = edit()
        item[field] = 0o755
        with pytest.raises(ContractError):
            prepare_changes(proposal(context, [item]), context)


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o664])
def test_supported_update_mode_is_preserved_by_host(context, mode):
    target = write(context.repository_root, FRONTEND, JS)
    target.chmod(mode)
    changes = prepare_changes(proposal(context, [update(FRONTEND, JS, JS)]), context)
    assert changes.edits[0].mode == mode


@pytest.mark.parametrize("original", [None, "", "z" * 64, "a" * 63, "A" * 64, True])
def test_malformed_update_hash_rejected(context, original):
    with pytest.raises(ContractError):
        prepare_changes(proposal(context, [edit(operation="update", original=original)]), context)


def test_stale_content_rejected_with_file_preserved(context):
    target = write(context.repository_root, FRONTEND, JS + "// changed since context\n")
    with pytest.raises(PolicyError, match="stale"):
        prepare_changes(proposal(context, [update(FRONTEND, JS, JS)]), context)
    assert "changed since context" in target.read_text()


@pytest.mark.parametrize("field", ["repository", "pr_number", "base_sha", "merge_base_sha",
                                        "head_sha", "policy_sha", "run_id"])
def test_source_binding_rejects_stale_or_replaced_identity(context, field):
    data = proposal(context, [edit()])
    data["source"][field] = {"repository": "other/repo", "pr_number": 13,
                             "run_id": "other-run"}.get(field, "e" * 40)
    with pytest.raises(PolicyError, match="identity"):
        prepare_changes(data, context)


def sized_content(size):
    assert size >= len(JS.encode())
    return JS + " " * (size - len(JS.encode()))


@pytest.mark.parametrize("size", [MAX_FILE_BYTES - 1, MAX_FILE_BYTES, MAX_FILE_BYTES + 1])
def test_per_file_byte_limit_below_at_and_above(context, size):
    data = proposal(context, [edit(content=sized_content(size))])
    result = assess_proposal(data, context)
    assert result.decision == (PolicyDecision.ACCEPTED if size <= MAX_FILE_BYTES else PolicyDecision.REJECTED)


@pytest.mark.parametrize("count", [MAX_EDITED_FILES - 1, MAX_EDITED_FILES, MAX_EDITED_FILES + 1])
def test_edit_count_limit_below_at_and_above(context, count):
    paths = [f"frontend/src/example{index}.test.ts" for index in range(count)]
    context = replace(context, permitted_test_paths=frozenset(paths))
    result = assess_proposal(proposal(context, [edit(path) for path in paths]), context)
    assert result.decision == (PolicyDecision.ACCEPTED if count <= MAX_EDITED_FILES else PolicyDecision.REJECTED)


@pytest.mark.parametrize("total", [MAX_TOTAL_BYTES - 1, MAX_TOTAL_BYTES, MAX_TOTAL_BYTES + 1])
def test_total_byte_limit_below_at_and_above(context, total):
    # Five modest files exercise the real constants, with each below 64 KiB.
    paths = [f"frontend/src/example{index}.test.ts" for index in range(5)]
    context = replace(context, permitted_test_paths=frozenset(paths))
    sizes = [total // 5 + (index < total % 5) for index in range(5)]
    result = assess_proposal(proposal(context, [edit(path, sized_content(size))
                             for path, size in zip(paths, sizes)]), context)
    assert result.decision == (PolicyDecision.ACCEPTED if total <= MAX_TOTAL_BYTES else PolicyDecision.REJECTED)


def test_byte_limit_counts_utf8_bytes_not_characters(context):
    content = JS + "é" * ((MAX_FILE_BYTES - len(JS.encode())) // 2 + 1)
    assert len(content) < MAX_FILE_BYTES < len(content.encode())
    with pytest.raises(PolicyError, match="byte limit"):
        prepare_changes(proposal(context, [edit(content=content)]), context)


def test_host_context_cannot_be_enlarged_by_model(context):
    path = "frontend/src/unrelated.test.ts"
    with pytest.raises(PolicyError, match="context boundary"):
        prepare_changes(proposal(context, [edit(path)]), context)
    assert context.changed_files == frozenset({"frontend/src/example.ts", "backend/app/example.py"})
    approved = replace(context, permitted_test_paths=context.permitted_test_paths | {path})
    assert prepare_changes(proposal(approved, [edit(path)]), approved).changed_files == tuple(sorted(context.changed_files))


@pytest.mark.parametrize("kwargs", [
    {"changed_files": ["frontend/src/example.ts"]},
    {"changed_files": frozenset({"../escape"})},
    {"permitted_test_paths": frozenset({"backend/app/example.py"})},
    {"repository_root": Path("relative")},
])
def test_invalid_host_context_fails_closed(context, kwargs):
    with pytest.raises(PolicyError):
        replace(context, **kwargs)


def test_symlink_repository_root_and_untrusted_context_are_rejected(context):
    link = context.repository_root / "root-link"
    link.symlink_to(context.repository_root, target_is_directory=True)
    with pytest.raises(PolicyError):
        replace(context, repository_root=link)
    with pytest.raises(PolicyError):
        prepare_changes(proposal(context, [edit()]), {"repository_root": context.repository_root})


@pytest.mark.parametrize("mechanism", [
    "it.skip", "it.only", "test.skip", "test.only", "describe.skip", "describe.only",
    "test.skipIf(true)", "test.runIf(false)", "test.todo", "test.concurrent.only",
    "test['skip']", "fit", "xit", "fdescribe", "xdescribe", "xtest",
    "test.each([1]).only", "test.concurrent.sequential.only", "test.fails",
])
def test_new_frontend_skip_and_only_variants_rejected(context, mechanism):
    new = JS + f"{mechanism}('hidden', () => expect(1).toBe(1))\n"
    with pytest.raises(PolicyError, match="skip/only/xfail"):
        prepare_changes(proposal(context, [edit(content=new)]), context)


@pytest.mark.parametrize("mechanism", [
    "@pytest.mark.skip", "@pytest.mark.skipif(True)", "pytest.xfail('later')",
    "@pytest.mark.xfail", "pytest.skip('later')", "@unittest.skip('later')",
    "@unittest.skipIf(True, 'later')", "self.skipTest('later')", "raise unittest.SkipTest",
    "@skip", "@skipif(True)", "@xfail", "@mark.skip", "skip('later')",
])
def test_new_backend_skip_and_xfail_variants_rejected(context, mechanism):
    with pytest.raises(PolicyError, match="skip/only/xfail"):
        prepare_changes(proposal(context, [edit(BACKEND, PY + mechanism + "\n")]), context)


@pytest.mark.parametrize("mechanism", [
    "import pytest\npytest.importorskip('optional_dependency')\n",
    "import unittest\n@unittest.expectedFailure\n",
])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_new_backend_import_skip_and_expected_failure_rejected(context, mechanism, operation):
    if operation == "update":
        write(context.repository_root, BACKEND, PY)
        item = update(BACKEND, PY, mechanism + PY)
    else:
        item = edit(BACKEND, mechanism + PY)
    result = assess_proposal(proposal(context, [item]), context)
    assert result.decision == PolicyDecision.REJECTED
    assert result.reason == "new skip/only/xfail mechanism"
    assert result.changes is None


@pytest.mark.parametrize("runner", ["test", "it"])
@pytest.mark.parametrize("modifier", ["skip", "only", "todo", "fails", "skipIf", "runIf"])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_frontend_each_weakening_modifiers_rejected_consistently(context, runner, modifier, operation):
    new = JS + f"{runner}.each([1]).{modifier}('hidden', () => expect(1).toBe(1))\n"
    if operation == "update":
        write(context.repository_root, FRONTEND, JS)
        item = update(FRONTEND, JS, new)
    else:
        item = edit(content=new)
    result = assess_proposal(proposal(context, [item]), context)
    assert result.decision == PolicyDecision.REJECTED
    assert result.reason == "new skip/only/xfail mechanism"
    assert result.changes is None


@pytest.mark.parametrize("chain", [
    "test.each([1])['fails']", "it.each([1])['skip']", "test.each([1])['todo']",
    "it.concurrent.each([1]).only", "test.each([1]).sequential.fails",
])
def test_frontend_each_bracket_and_qualifier_forms_rejected(context, chain):
    result = assess_proposal(proposal(context, [edit(content=JS +
                             f"{chain}('hidden', () => expect(1).toBe(1))\n")]), context)
    assert result.decision == PolicyDecision.REJECTED
    assert result.reason == "new skip/only/xfail mechanism"


@pytest.mark.parametrize("path,prefix,body", [
    (BACKEND, "import pytest\npytest.importorskip('optional_dependency')\n", PY),
    (BACKEND, "import unittest\n@unittest.expectedFailure\n", PY),
    (FRONTEND, "test.each([1]).fails('old', () => expect(1).toBe(1))\n", JS),
    (FRONTEND, "it.each([1]).fails('old', () => expect(1).toBe(1))\n", JS),
])
def test_preexisting_reviewed_mechanisms_allow_assertion_update(context, path, prefix, body):
    old = prefix + body
    new = prefix + (body.replace("1 == 1", "1 == 2") if path == BACKEND
                    else body.replace("toBe(1)", "toBe(2)"))
    target = write(context.repository_root, path, old)
    result = assess_proposal(proposal(context, [update(path, old, new)]), context)
    assert result.decision == PolicyDecision.ACCEPTED
    assert result.changes.edits[0].content == new.encode()
    assert target.read_text() == old


@pytest.mark.parametrize("path,old,new", [
    (FRONTEND, JS + "it.skip('old', () => expect(1).toBe(1))\n",
     JS.replace("toBe(1)", "toBe(2)") + "it.skip('old', () => expect(1).toBe(1))\n"),
    (BACKEND, "@pytest.mark.skip\n" + PY, "@pytest.mark.skip\n" + PY.replace("1 == 1", "1 == 2")),
    (FRONTEND, JS + "// already mentions test.only\n", JS + "// already mentions test.only\n// harmless\n"),
])
def test_preexisting_weakening_text_does_not_block_legitimate_updates(context, path, old, new):
    write(context.repository_root, path, old)
    assert prepare_changes(proposal(context, [update(path, old, new)]), context).edits[0].content == new.encode()


@pytest.mark.parametrize("path,new", [
    (FRONTEND, ""), (FRONTEND, "// removed\n"), (FRONTEND, "it('empty', () => {})\n"),
    (BACKEND, ""), (BACKEND, "def test_empty():\n    pass\n"),
])
def test_obvious_emptying_or_assertion_removal_rejected(context, path, new):
    old = JS if path == FRONTEND else PY
    write(context.repository_root, path, old)
    with pytest.raises(PolicyError):
        prepare_changes(proposal(context, [update(path, old, new)]), context)


@pytest.mark.parametrize("path,old,new", [
    (FRONTEND, JS + "it('second', () => expect(2).toBe(2))\n", JS),
    (BACKEND, PY + "def test_second():\n    assert 2 == 2\n", PY),
])
def test_obvious_test_declaration_removal_rejected(context, path, old, new):
    write(context.repository_root, path, old)
    with pytest.raises(PolicyError, match="removal"):
        prepare_changes(proposal(context, [update(path, old, new)]), context)


@pytest.mark.parametrize("content", [123, "\ud800", JS + "\x00"])
def test_non_text_or_binary_content_rejected(context, content):
    with pytest.raises((ContractError, PolicyError)):
        prepare_changes(proposal(context, [edit(content=content)]), context)


def test_plan_and_edit_alignment_and_duplicate_edits(context):
    data = proposal(context, [edit()])
    data["decisions"][0]["plan"]["intended_test_files"] = []
    with pytest.raises(PolicyError, match="match"):
        prepare_changes(data, context)
    data = proposal(context, [edit()])
    data["edits"].append(edit())
    with pytest.raises(PolicyError, match="duplicate"):
        prepare_changes(data, context)


def test_policy_acceptance_never_implies_verification(context):
    accepted = assess_proposal(proposal(context, [edit()]), context)
    rejected = assess_proposal({"verified": True}, context)
    assert accepted.decision == PolicyDecision.ACCEPTED and accepted.changes is not None
    assert rejected.decision == PolicyDecision.REJECTED and rejected.changes is None
    assert not hasattr(accepted, "status") and not hasattr(accepted, "verified")


@pytest.mark.parametrize("action", ["no-tests-needed", "unsupported"])
def test_non_edit_decisions_accepted_without_trusted_success(context, action):
    data = proposal(context, [edit()])
    data["edits"] = []
    data["decisions"][0]["action"] = action
    data["decisions"][0]["plan"]["intended_test_files"] = []
    result = assess_proposal(data, context)
    assert result.decision == PolicyDecision.ACCEPTED
    assert result.changes.edits == ()


@pytest.mark.parametrize("action", ["no-tests-needed", "unsupported"])
def test_non_edit_decision_cannot_supply_edits(context, action):
    data = proposal(context, [edit()])
    data["decisions"][0]["action"] = action
    with pytest.raises(PolicyError):
        prepare_changes(data, context)


@pytest.mark.parametrize("path,old,new_path", [
    (FRONTEND, JS, "frontend/src/supplemental.test.tsx"),
    (BACKEND, PY, "backend/tests/test_supplemental.py"),
])
def test_update_decision_can_add_supplemental_tests(context, path, old, new_path):
    write(context.repository_root, path, old)
    context = replace(context, permitted_test_paths=context.permitted_test_paths | {new_path})
    result = assess_proposal(proposal(context, [update(path, old, old), edit(new_path)]), context)
    assert result.decision == PolicyDecision.ACCEPTED
    assert {item.operation for item in result.changes.edits} == {"create", "update"}


@pytest.mark.parametrize("path", [FRONTEND, BACKEND])
def test_update_decision_with_only_creates_rejected(context, path):
    data = proposal(context, [edit(path)])
    data["decisions"][0]["action"] = "update"
    result = assess_proposal(data, context)
    assert result.decision == PolicyDecision.REJECTED
    assert result.reason == "update decision requires at least one update operation"
    assert result.changes is None


def test_update_decision_requires_update_for_its_own_target(context):
    write(context.repository_root, FRONTEND, JS)
    data = proposal(context, [update(FRONTEND, JS, JS), edit(BACKEND)])
    data["decisions"][1]["action"] = "update"
    result = assess_proposal(data, context)
    assert result.decision == PolicyDecision.REJECTED
    assert result.reason == "update decision requires at least one update operation"


@pytest.mark.parametrize("path,old", [(FRONTEND, JS), (BACKEND, PY)])
def test_generate_decision_cannot_supply_updates(context, path, old):
    write(context.repository_root, path, old)
    data = proposal(context, [update(path, old, old)])
    data["decisions"][0]["action"] = "generate"
    result = assess_proposal(data, context)
    assert result.decision == PolicyDecision.REJECTED
    assert result.reason == "generate decision requires create operations"


def test_existing_binary_and_oversized_test_reads_rejected(context):
    target = write(context.repository_root, FRONTEND, JS)
    for old in (b"\xff", b"\x00", b" " * (MAX_FILE_BYTES + 1)):
        target.write_bytes(old)
        item = edit(operation="update", original=hashlib.sha256(old).hexdigest())
        with pytest.raises(PolicyError):
            prepare_changes(proposal(context, [item]), context)
