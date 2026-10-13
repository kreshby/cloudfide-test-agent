"""Artifact allowlist, exact originals, byte budgets and deterministic diff data."""

from dataclasses import replace
import hashlib
import json
import os

import pytest

from automation.test_agent.artifacts import (CLASSIFICATIONS, ENTRY_LIMITS, MAX_BUNDLE_BYTES,
                                           ArtifactEntry, ArtifactError, PatchPreparationState,
                                           PatchUnavailable, UploadSpec, build_bundle, build_patch,
                                           check_total, upload_spec, validate_entries)
from automation.test_agent.reporting import prepare_report
from automation.tests.t06_fixtures import fixture


def test_create_patch_is_exact_deterministic_utf8_data(tmp_path):
    bundle, result, report = fixture(tmp_path)
    patch = build_patch(bundle, result, tmp_path, report)
    edit = result.policy.changes.edits[0]
    expected = (f"--- /dev/null\n+++ b/{edit.path}\n@@ -0,0 +1,11 @@\n" +
                "".join("+" + line + "\n" for line in edit.content.decode().splitlines())).encode()
    assert patch == expected
    assert patch == build_patch(bundle, result, tmp_path, report)
    assert str(tmp_path).encode() not in patch
    assert not (tmp_path / edit.path).exists()


def test_update_diff_rechecks_exact_head_original_and_never_writes(tmp_path):
    bundle, result, report = fixture(tmp_path, "backend-update")
    edit = result.policy.changes.edits[0]
    original = (tmp_path / edit.path).read_bytes()
    patch = build_patch(bundle, result, tmp_path, report)
    assert patch == build_patch(bundle, result, tmp_path, report)
    assert patch.startswith(f"--- a/{edit.path}\n+++ b/{edit.path}\n".encode())
    assert b"-def tasks(limit: int = 0):" not in patch  # No application files are included.
    assert b"+    assert response.status_code == 422" in patch
    assert b"+    assert response.json()['detail'][0]['loc'] == ['query', 'limit']" in patch
    assert (tmp_path / edit.path).read_bytes() == original
    assert str(tmp_path).encode() not in patch
    (tmp_path / edit.path).write_bytes(original + b"# changed\n")
    with pytest.raises(PatchUnavailable, match="revalidation"):
        build_patch(bundle, result, tmp_path, report)
    output = build_bundle(bundle, result, tmp_path, report)
    assert output.patch_preparation == PatchPreparationState.UNAVAILABLE
    assert "generated-tests.patch" not in output.upload.paths
    assert prepare_report(bundle, result, report).status == "VERIFIED"


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "directory", "ancestor-symlink", "mode"])
def test_unsafe_originals_cannot_become_patch_artifacts(tmp_path, attack):
    bundle, result, report = fixture(tmp_path, "backend-update")
    path = tmp_path / result.policy.changes.edits[0].path
    original = path.read_bytes()
    outside = tmp_path / "outside"
    outside.write_bytes(original)
    path.unlink()
    if attack == "symlink":
        path.symlink_to(outside)
    elif attack == "hardlink":
        os.link(outside, path)
    elif attack == "fifo":
        os.mkfifo(path)
    elif attack == "directory":
        path.mkdir()
    elif attack == "mode":
        path.write_bytes(original)
        path.chmod(0o755)
    else:
        path.parent.rmdir()
        path.parent.symlink_to(outside)
    with pytest.raises(PatchUnavailable):
        build_patch(bundle, result, tmp_path, report)


@pytest.mark.parametrize("attack", ["original-source", "blob", "missing-evidence", "content", "size"])
def test_missing_or_unbound_exact_original_is_unavailable(tmp_path, attack):
    bundle, result, report = fixture(tmp_path, "backend-update")
    old = bundle.existing_tests[0]
    updates = {"original-source": {"source_sha": "9" * 40}, "blob": {"blob_sha": "e" * 40},
               "content": {"content": "different original"}, "size": {"observed_bytes": 1}}
    altered = replace(bundle, existing_tests=() if attack == "missing-evidence" else (replace(old, **updates[attack]),))
    # Bind the synthetic host result to the altered context; T06 still checks originals independently.
    trace = list(result.trace)
    initial = json.loads(trace[0])
    initial["context_sha256"] = altered.sha256
    from automation.test_agent.provider import canonical
    trace[0] = canonical(initial).decode()
    result = replace(result, context_sha256=altered.sha256, trace=tuple(trace))
    with pytest.raises(PatchUnavailable):
        build_patch(altered, result, tmp_path, report)


def test_create_target_existing_or_symlink_ancestor_is_unavailable(tmp_path):
    bundle, result, report = fixture(tmp_path)
    path = tmp_path / result.policy.changes.edits[0].path
    (tmp_path / "frontend").symlink_to(tmp_path / "outside")
    with pytest.raises(PatchUnavailable):
        build_patch(bundle, result, tmp_path, report)
    (tmp_path / "frontend").unlink()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing")
    assert build_bundle(bundle, result, tmp_path, report).patch_preparation == PatchPreparationState.UNAVAILABLE


@pytest.mark.parametrize("path", ["../escape", "/tmp/test_new.py", "backend/tests/../../test_escape.py", "backend/app/tasks.py", "frontend/src/unknown.test.ts"])
def test_traversal_and_unaccepted_paths_rejected(tmp_path, path):
    bundle, result, report = fixture(tmp_path)
    edit = result.policy.changes.edits[0]
    changes = replace(result.policy.changes, edits=(replace(edit, path=path),))
    result = replace(result, policy=replace(result.policy, changes=changes))
    with pytest.raises(ArtifactError, match="input binding rejected"):
        build_patch(bundle, result, tmp_path, report)


@pytest.mark.parametrize("directory", ["node_modules", "models", ".env", "weights.gguf", ".ssh", "credentials"])
def test_sensitive_or_bulk_paths_excluded_even_inside_test_family(tmp_path, directory):
    from automation.test_agent.agent import UnitTestAgent
    from automation.test_agent.provider import FakeProvider
    from automation.tests.t04_fixtures import transcript
    bundle, _, _ = fixture(tmp_path)
    candidate = replace(bundle.candidate_tests[0], path=f"frontend/src/{directory}/x.test.tsx")
    bundle = replace(bundle, candidate_tests=(candidate,))
    result = UnitTestAgent(bundle, tmp_path, FakeProvider(transcript(bundle))).run()
    assert result.policy.changes is not None
    with pytest.raises(PatchUnavailable, match="bulk test path excluded"):
        build_patch(bundle, result, tmp_path)


def test_update_patch_exact_bytes(tmp_path):
    bundle, result, report = fixture(tmp_path, "backend-update")
    path = result.policy.changes.edits[0].path
    assert build_patch(bundle, result, tmp_path, report).decode() == (
        f"--- a/{path}\n+++ b/{path}\n@@ -4,4 +4,5 @@\n"
        " \n def test_zero_limit():\n"
        "     response = client.get('/tasks?limit=0')\n"
        "-    assert response.status_code == 200\n"
        "+    assert response.status_code == 422\n"
        "+    assert response.json()['detail'][0]['loc'] == ['query', 'limit']\n"
    )


def test_bundle_is_deterministic_with_exact_manifest_hashes_and_sizes(tmp_path):
    bundle, result, report = fixture(tmp_path)
    first, second = build_bundle(bundle, result, tmp_path, report), build_bundle(bundle, result, tmp_path, report)
    assert first == second and first.patch_preparation == PatchPreparationState.PREPARED
    assert first.upload.paths == tuple(sorted(ENTRY_LIMITS))
    by_name = {e.name: e for e in first.entries}
    manifest = json.loads(by_name["manifest.json"].data)
    assert manifest["schema_version"] == 1 and manifest["artifact_logical_name"] == "test-agent-final"
    assert manifest["source_head_sha"] == bundle.source.head_sha and manifest["policy_sha"] == bundle.source.policy_sha
    assert (manifest["run_id"], manifest["run_attempt"]) == (42, 1)
    assert [entry["relative_path"] for entry in manifest["entries"]] == sorted(set(ENTRY_LIMITS) - {"manifest.json"})
    for item in manifest["entries"]:
        entry = by_name[item["relative_path"]]
        assert item["byte_size"] == len(entry.data)
        assert item["sha256"] == hashlib.sha256(entry.data).hexdigest()
        assert item["classification"] == entry.classification
    metadata = json.loads(by_name["report-metadata.json"].data)
    assert metadata["patch_preparation"] == "PREPARED" and metadata["patch_delivery"] == "UNAVAILABLE"
    verification = json.loads(by_name["verification.json"].data)
    assert verification["status"] == "verified"
    assert sum(len(e.data) for e in first.entries) < MAX_BUNDLE_BYTES
    for entry in first.entries:
        assert len(entry.data) <= ENTRY_LIMITS[entry.name]
        assert str(tmp_path).encode() not in entry.data and b"/private/" not in entry.data
        assert b"SHOULD_NOT_RENDER" not in entry.data


def test_non_edit_bundle_has_no_patch_or_execution_and_fixed_retention(tmp_path):
    bundle, result, _ = fixture(tmp_path, "already-covered")
    output = build_bundle(bundle, result, tmp_path)
    assert output.patch_preparation == PatchPreparationState.NOT_REQUIRED
    assert "generated-tests.patch" not in output.upload.paths
    value = json.loads(next(e.data for e in output.entries if e.name == "verification.json"))
    assert value["commands"] == [] and value["status"] is None
    assert output.upload.retention_days == 7
    assert upload_spec(output.entries, classification="intermediate").retention_days == 1


@pytest.mark.parametrize("classification,retention", [("final", 1), ("final", 8), ("final", True), ("intermediate", 7), ("intermediate", 0)])
def test_other_retention_values_rejected(classification, retention):
    with pytest.raises(ArtifactError, match="upload specification"):
        UploadSpec(classification, "test-agent-" + classification, ("manifest.json",), retention)


@pytest.mark.parametrize("name", ["other.json", ".env", "credentials.json", "node_modules/a", "model.gguf", "Dockerfile", ".git/config", ".github/workflows/publish.yml", "/host/log", "*.json", "directory/"])
def test_arbitrary_sensitive_and_bulk_paths_cannot_be_added(name):
    with pytest.raises(ArtifactError, match="allowlist"):
        ArtifactEntry(name, b"data", "host-report-metadata")
    with pytest.raises(ArtifactError):
        UploadSpec("final", "test-agent-final", (name,), 7)


@pytest.mark.parametrize("name", list(ENTRY_LIMITS))
def test_every_entry_overflow_rejected(name):
    with pytest.raises(ArtifactError, match="entry byte limit"):
        ArtifactEntry(name, b"x" * (ENTRY_LIMITS[name] + 1), CLASSIFICATIONS[name])


def test_total_ten_mib_and_tampered_envelope_rejected_before_upload():
    check_total((b"x" * MAX_BUNDLE_BYTES,))
    with pytest.raises(ArtifactError, match="total byte limit"):
        check_total((b"x" * MAX_BUNDLE_BYTES, b"x"))
    entry = ArtifactEntry("manifest.json", b"{}", "manifest")
    object.__setattr__(entry, "data", b"x" * (MAX_BUNDLE_BYTES + 1))
    with pytest.raises(ArtifactError, match="total byte limit"):
        validate_entries((entry,))
    with pytest.raises(ArtifactError):
        ArtifactEntry("manifest.json", b"\xff", "manifest")


def test_missing_newline_diff_record_is_conventional_data():
    from automation.test_agent.artifacts import _diff
    assert _diff("old", "new", "backend/tests/test_x.py", False) == (
        b"--- a/backend/tests/test_x.py\n+++ b/backend/tests/test_x.py\n@@ -1 +1 @@\n"
        b"-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n")
