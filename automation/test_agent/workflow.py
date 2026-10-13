"""Trusted-main T07 entry points. Event/PR/model data never supplies commands.

Only prepare and publish are CLI operations. Each receives platform-owned run
metadata. Dispatch accepts one bounded source CI run ID, never SHA/URL overrides.
"""

from dataclasses import asdict, dataclass, replace
import os
from pathlib import Path
import re
import signal
import tempfile
import time

from .agent import State, UnitTestAgent
from .artifacts import PatchPreparationState, build_bundle, validate_entries
from .context import _compare, _source, _test, collect_context
from .contracts import PolicyDecision, SourceIdentity
from .github import CollectionError, GitHubHTTPTransport, GitHubReader, integer, object_value, repository, sha
from .handoff import (FILE_NAME, MAX_HANDOFF_BYTES, decode, encode, execution_facts,
                      report, successful, terminal_facts)
from .provider import digest, strict_json
from .publisher import (GitHubPublisherAdapter, GitHubPublisherHTTPTransport, PublicationState,
                        PublicationError, PublisherAuthor, publish)
from .reporting import AgentRun, PublicationIdentity, bind_evidence
from .runtime import LocalRuntime, Process, RuntimeUnavailable
from .verification import Executor, GIT, Verifier, checked, safe_read


REPOSITORY = "kreshby/cloudfide-test-agent"
CI_NAME = "Ordinary CI"
CI_PATH = ".github/workflows/ci.yml"
AGENT_NAME = "Automatic unit test agent"
AGENT_PATH = ".github/workflows/test-agent.yml"
ORIGIN = "https://github.com/" + REPOSITORY + ".git"
EXPECTED_AUTHOR = PublisherAuthor(41898282, "github-actions[bot]")
TOTAL_SECONDS = 2640
CHECKOUT_SECONDS = 120
MAX_EVENT_BYTES = 1024 * 1024
CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "neutral", "skipped", "action_required",
               "startup_failure", "stale"}


class WorkflowError(ValueError):
    """Fixed integration error, no exception text from data or subprocesses."""


def run_id(value):
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise WorkflowError("bounded run ID required")
    return integer(int(value), 1)


def select_source(event_name, event):
    object_value(event)
    if event_name == "workflow_run":
        return integer(object_value(event.get("workflow_run")).get("id"), 1)
    if event_name == "workflow_dispatch":
        inputs = object_value(event.get("inputs"))
        if set(inputs) != {"ordinary_ci_run_id"}:
            raise WorkflowError("dispatch accepts only Ordinary CI run ID")
        return run_id(inputs["ordinary_ci_run_id"])
    raise WorkflowError("unsupported integration trigger")


def _repo(raw, expected_id=None, *, public=False):
    obj = object_value(raw)
    if repository(obj.get("full_name")) != REPOSITORY or (expected_id is not None and obj.get("id") != expected_id):
        raise WorkflowError("exact repository identity required")
    identifier = integer(obj.get("id"), 1)
    if public and obj.get("private") is not False:
        raise WorkflowError("public repository required")
    return identifier


def _workflow(raw, name, path):
    obj = object_value(raw)
    if obj.get("name") != name or obj.get("path") != path or obj.get("state") != "active":
        raise WorkflowError("exact active workflow required")
    return integer(obj.get("id"), 1)


def validate_agent_run(reader, publication, trusted_sha, repository_id, event_name):
    workflow_id = _workflow(reader.agent_workflow(), AGENT_NAME, AGENT_PATH)
    run = reader.workflow_run(publication.run_id)
    if (integer(run.get("id"), 1), integer(run.get("run_attempt"), 1), integer(run.get("workflow_id"), 1)) != (
        publication.run_id, publication.run_attempt, workflow_id):
        raise WorkflowError("current agent run identity mismatch")
    _repo(run.get("repository"), repository_id)
    _repo(run.get("head_repository"), repository_id)
    if (run.get("name") != AGENT_NAME or run.get("path") != AGENT_PATH or run.get("event") != event_name
            or run.get("status") not in {"queued", "in_progress"} or sha(run.get("head_sha")) != trusted_sha
            or run.get("head_branch") != "main"):
        raise WorkflowError("agent must execute the trusted main workflow commit")


@dataclass(frozen=True)
class Preflight:
    source: SourceIdentity
    source_ci: dict
    identity: PublicationIdentity
    terminal_reason: str | None


def _pr(raw, repository_id):
    obj = object_value(raw)
    if obj.get("state") not in {"open", "closed"}:
        raise WorkflowError("PR state rejected")
    number = integer(obj.get("number"), 1)
    base, head = object_value(obj.get("base")), object_value(obj.get("head"))
    _repo(base.get("repo"), repository_id)
    head_repo = object_value(head.get("repo"))
    return (number, obj["state"], sha(base.get("sha")), sha(head.get("sha")),
            repository(head_repo.get("full_name")), integer(head_repo.get("id"), 1))


def preflight(reader, source_run_id, publication, policy_sha, *, expected_repository_id,
              event_snapshot=None, trigger="workflow_run"):
    """Only exact API association grants a publication target; forks get no T03."""
    if trigger not in {"workflow_run", "workflow_dispatch"}:
        raise WorkflowError("unsupported preflight trigger")
    repository_id = _repo(reader.repository_metadata(), expected_repository_id, public=True)
    if reader.repository != REPOSITORY:
        raise WorkflowError("configured repository mismatch")
    workflow_id = _workflow(reader.ordinary_ci_workflow(), CI_NAME, CI_PATH)
    raw = reader.workflow_run(source_run_id)
    _repo(raw.get("repository"), repository_id)
    if (integer(raw.get("id"), 1) != source_run_id or integer(raw.get("workflow_id"), 1) != workflow_id
            or raw.get("name") != CI_NAME or raw.get("path") != CI_PATH
            or raw.get("status") != "completed" or raw.get("conclusion") not in CONCLUSIONS):
        raise WorkflowError("source Ordinary CI run rejected")
    head = sha(raw.get("head_sha"))
    attempt = integer(raw.get("run_attempt"), 1)
    if event_snapshot is not None:
        for key in ("id", "workflow_id", "run_attempt", "name", "path", "event", "status", "conclusion", "head_sha"):
            if event_snapshot.get(key) != raw.get(key):
                raise WorkflowError("originating event differs from GitHub source run")
        _repo(event_snapshot.get("repository"), repository_id)
    # Ordinary CI also runs on push. This is deliberately not a model failure.
    if raw.get("event") == "push":
        if trigger == "workflow_run":
            return None
        raise WorkflowError("dispatch source must originate from pull_request")
    if raw.get("event") != "pull_request":
        raise WorkflowError("source event rejected")
    candidates, seen = [], set()
    for item in reader.associated_prs(head):
        pr = _pr(item, repository_id)
        if pr[0] in seen:
            raise WorkflowError("duplicate PR association")
        seen.add(pr[0])
        if pr[1] == "open" and pr[3] == head:
            candidates.append(pr)
    if len(candidates) != 1:
        raise WorkflowError("exactly one current open PR required")
    pr = candidates[0]
    if _pr(reader.pull_request(pr[0]), repository_id) != pr:
        raise WorkflowError("stale PR association")
    run_head_repo = object_value(raw.get("head_repository"))
    if (repository(run_head_repo.get("full_name")), integer(run_head_repo.get("id"), 1)) != (pr[4], pr[5]):
        raise WorkflowError("source head repository differs from exact PR")
    merge_base, _ = _compare(reader, pr[2], head)
    if _pr(reader.pull_request(pr[0]), repository_id) != pr or reader.workflow_run(source_run_id) != raw:
        raise WorkflowError("source changed during preflight")
    source = SourceIdentity(REPOSITORY, pr[0], pr[2], merge_base, head, sha(policy_sha), str(source_run_id))
    i = PublicationIdentity(REPOSITORY, pr[0], head, policy_sha, publication.run_id, publication.run_attempt,
                            pr[2], merge_base, repository_id, source_run_id, attempt, pr[4], pr[5])
    ci = {"run_id": source_run_id, "run_attempt": attempt, "workflow_id": workflow_id,
          "repository_id": repository_id, "head_sha": head, "name": CI_NAME, "path": CI_PATH,
          "event": "pull_request", "status": "completed", "conclusion": raw["conclusion"]}
    fork = (pr[4], pr[5]) != (REPOSITORY, repository_id)
    reason = "fork-disabled" if fork else "source-ci-unsuccessful" if raw["conclusion"] != "success" else None
    return Preflight(source, ci, i, reason)


def trusted_checkout(root, trusted_sha, workflow_ref, *, executor=None):
    if workflow_ref != REPOSITORY + "/" + AGENT_PATH + "@refs/heads/main":
        raise WorkflowError("trusted main workflow ref required")
    sha(trusted_sha)
    executor = executor or Executor()
    def git(*args):
        return checked(executor, (GIT, "-C", str(root), *args)).strip().decode("ascii")
    if root != Path(__file__).resolve().parents[2] or git("rev-parse", "HEAD") != trusted_sha:
        raise WorkflowError("trusted checkout commit mismatch")
    if git("diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD"):
        raise WorkflowError("trusted checkout must be clean")
    policy, _ = safe_read(root, "automation/test_agent/policy.py", maximum=64 * 1024)
    # Git blob identity of the installed policy, not a PR-supplied policy SHA.
    import hashlib
    policy_sha = hashlib.sha1(b"blob " + str(len(policy)).encode() + b"\0" + policy).hexdigest()
    if git("rev-parse", "HEAD:automation/test_agent/policy.py") != policy_sha:
        raise WorkflowError("trusted policy bytes mismatch")
    return policy_sha


class SourceCheckout:
    """Public, exact-SHA fetch with no token, helper, hooks or PR-selected URL."""
    def __init__(self, source):
        if source.repository != REPOSITORY:
            raise WorkflowError("source repository rejected")
        source.__post_init__()
        self.source, self.temporary, self.root = source, None, None

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="t07-source-")
        work = Path(self.temporary.name)
        self.root = work / "source"
        self.root.mkdir()
        deadline = time.monotonic() + CHECKOUT_SECONDS
        source = self.source
        # Finite exact operations, not a general shell or arbitrary argv runner.
        prefix = (GIT, "-c", "credential.helper=", "-c", "http.followRedirects=false",
                  "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                  "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                  "-C", str(self.root))
        operations = [("init",), ("remote", "add", "origin", ORIGIN),
                      ("fetch", "--no-tags", "origin", source.head_sha, source.base_sha),
                      ("checkout", "--detach", source.head_sha), ("rev-parse", "HEAD"),
                      ("config", "--get", "remote.origin.url"),
                      ("merge-base", source.base_sha, source.head_sha),
                      ("status", "--porcelain", "--untracked-files=all")]
        observed = []
        try:
            for operation in operations:
                process = Process([*prefix, *operation], work)
                try:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise WorkflowError("source checkout deadline exhausted")
                    code = process.process.wait(timeout=timeout)
                finally:
                    process.stop()
                if code != 0 or any(process.truncated.values()):
                    raise WorkflowError("source checkout operation unavailable")
                observed.append(bytes(process.stdout).strip().decode("utf-8"))
            if observed[-4:] != [source.head_sha, ORIGIN, source.merge_base_sha, ""]:
                raise WorkflowError("exact source checkout binding rejected")
            return self.root
        except BaseException:
            self.temporary.cleanup()
            self.temporary = None
            raise WorkflowError("source checkout unavailable") from None

    def __exit__(self, *_):
        if self.temporary is not None:
            self.temporary.cleanup()


def execute(pre, reader, *, source_factory=SourceCheckout, runtime_factory=LocalRuntime,
            agent_factory=UnitTestAgent, verifier_factory=Verifier):
    """Trusted injectable seams for offline tests; none is configured by data."""
    if pre.terminal_reason:
        status = "UNSUPPORTED" if pre.terminal_reason == "fork-disabled" else "UNAVAILABLE"
        return terminal_facts(status, pre.terminal_reason), None
    try:
        bundle = collect_context(reader, run_id=pre.source_ci["run_id"], workflow_id=pre.source_ci["workflow_id"],
                                 expected_head_sha=pre.source.head_sha, policy_sha=pre.source.policy_sha)
        if bundle.source != pre.source or asdict(bundle.workflow) != {k: pre.source_ci[k] for k in asdict(bundle.workflow)}:
            raise WorkflowError("collected source differs from preflight")
    except Exception:
        return terminal_facts("UNAVAILABLE", "context-unavailable"), None
    if not any(_source(f.path) or _test(f.path) for f in bundle.changed_files):
        return terminal_facts("UNSUPPORTED", "docs-only"), None
    try:
        # Context collection can take time; refresh the public/$0 gate directly
        # before checkout/model work, rather than relying on the old event flag.
        _repo(reader.repository_metadata(), pre.identity.repository_id, public=True)
    except Exception:
        return terminal_facts("UNAVAILABLE", "repository-unavailable"), None
    publication = AgentRun(pre.identity.workflow_run_id, pre.identity.run_attempt)
    verification = None
    artifact = None
    try:
        with source_factory(pre.source) as root:
            try:
                with runtime_factory() as runtime:
                    agent = agent_factory(bundle, root, runtime.provider)
                    result = agent.run()
                    if result.state == State.AWAITING_VERIFICATION:
                        if result.policy is None or result.policy.decision != PolicyDecision.ACCEPTED:
                            return terminal_facts("AGENT_FAILED", "policy-rejected"), None
                        runtime.stop()  # Release model RAM before image build/containers.
                        profiles = tuple(t + "-v1" for t in ("frontend", "backend") if any(
                            f.path.startswith(t + "/") for f in bundle.changed_files) or any(
                            e.target.value == t for e in result.policy.changes.edits))
                        try:
                            verification = verifier_factory().verify(bundle.source, root, result.policy.changes,
                                                                      profile_ids=profiles)
                            bind_evidence(bundle, result, verification, publication=publication)
                        except Exception:
                            verification = None  # No fabricated HostResult for an unavailable verifier.
                        if verification is not None:
                            try:
                                runtime.start()
                                result = agent.resume(verification.host_result)
                            except Exception:
                                result = replace(result, state=State.FAILED, failure="verification feedback unavailable")
                    elif result.state == State.FAILED and result.policy is not None and result.policy.decision == PolicyDecision.REJECTED:
                        return terminal_facts("AGENT_FAILED", "policy-rejected"), None
            except Exception:
                # If evidence exists, keep it even when feedback runtime cleanup fails.
                if verification is None:
                    return terminal_facts("UNAVAILABLE", "runtime-unavailable"), None
                result = replace(result, state=State.FAILED, failure="runtime cleanup unavailable")
            try:
                artifact = build_bundle(bundle, result, root, verification, publication=publication)
            except Exception:
                artifact = None
            try:
                facts = execution_facts(bundle, result, verification, publication,
                                        patch_prepared=bool(artifact and artifact.patch_preparation == PatchPreparationState.PREPARED))
                return facts, artifact
            except Exception:
                return terminal_facts("AGENT_FAILED", "agent-contract-failed"), None
    except Exception:
        return terminal_facts("UNAVAILABLE", "checkout-unavailable"), None
    finally:
        if verification is not None and verification.output_directory.exists():
            # T05 owns this directory; it never arrives from JSON or PR metadata.
            import shutil
            shutil.rmtree(verification.output_directory)


def _output(**values):
    # Only bounded validated scalar metadata is written to the platform output file.
    for key, value in values.items():
        if key not in {"publish", "repository_id", "pr_number", "source_run_id", "handoff_sha256", "artifact", "patch"} or re.fullmatch(r"[A-Za-z0-9_-]{0,200}", str(value)) is None:
            raise WorkflowError("workflow output rejected")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _main(operation):
    if operation not in {"prepare", "publish"}:
        raise WorkflowError("unsupported host operation")
    if not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("ImageOS") == "ubuntu24" and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and os.environ.get("GITHUB_REF") == "refs/heads/main"):
        raise WorkflowError("public standard trusted-main runner required")
    trusted_root = Path(__file__).resolve().parents[2]
    trusted_sha = sha(os.environ["TRUSTED_WORKFLOW_SHA"])
    policy_sha = trusted_checkout(trusted_root, trusted_sha, os.environ["TRUSTED_WORKFLOW_REF"])
    # P01 inherited host limits also cover compiler, source Git and llama children.
    # Docker daemon/container limits remain T05-owned and unchanged.
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (12 * 1024**3, 12 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (6 * 1024**3, 6 * 1024**3))
    publication = AgentRun(run_id(os.environ["GITHUB_RUN_ID"]), run_id(os.environ["GITHUB_RUN_ATTEMPT"]))
    repository_id = run_id(os.environ["GITHUB_REPOSITORY_ID"])
    event_name = os.environ["GITHUB_EVENT_NAME"]
    def reader():
        return GitHubReader(REPOSITORY, GitHubHTTPTransport(os.environ.get("GITHUB_TOKEN")))
    current = reader()
    _repo(current.repository_metadata(), repository_id, public=True)
    validate_agent_run(current, publication, trusted_sha, repository_id, event_name)
    if operation == "prepare":
        event_bytes, _ = safe_read(Path(os.environ["GITHUB_EVENT_PATH"]).parent,
                                   Path(os.environ["GITHUB_EVENT_PATH"]).name, maximum=MAX_EVENT_BYTES)
        event = strict_json(event_bytes.decode(), MAX_EVENT_BYTES)
        source_id = select_source(event_name, event)
        pre = preflight(current, source_id, publication, policy_sha, expected_repository_id=repository_id,
                        event_snapshot=event["workflow_run"] if event_name == "workflow_run" else None, trigger=event_name)
        if pre is None:
            _output(publish="false", artifact="false")
            return 0
        facts, artifact = execute(pre, reader())
        parent = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        evidence = parent / "t07-evidence"
        evidence.mkdir(mode=0o700)
        if artifact is not None:
            validate_entries(artifact.entries)
            final = evidence / "final"
            final.mkdir(mode=0o700)
            for entry in artifact.entries:
                (final / entry.name).write_bytes(entry.data)
        raw = encode(pre.identity, pre.source, pre.source_ci, trusted_sha, facts)
        (evidence / FILE_NAME).write_bytes(raw)
        _output(publish="true", repository_id=repository_id, pr_number=pre.source.pr_number,
                source_run_id=source_id, handoff_sha256=digest(raw),
                artifact="true" if artifact is not None else "false", patch="true" if facts["patch_prepared"] else "false")
        return 0
    source_id = run_id(os.environ["SOURCE_CI_RUN_ID"])
    pre = preflight(current, source_id, publication, policy_sha, expected_repository_id=repository_id, trigger=event_name)
    if pre is None:
        raise WorkflowError("publisher source is not applicable")
    if pre.identity.pr_number != run_id(os.environ["EXPECTED_PR_NUMBER"]):
        raise WorkflowError("publisher concurrency target mismatch")
    try:
        directory = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True) / "t07-handoff"
        if set(p.name for p in directory.iterdir()) != {FILE_NAME}:
            raise WorkflowError("handoff file allowlist rejected")
        raw, _ = safe_read(directory, FILE_NAME, maximum=MAX_HANDOFF_BYTES)
        value = decode(raw, os.environ["HANDOFF_SHA256"], expected_identity=pre.identity,
                       expected_source_ci=pre.source_ci, expected_trusted_sha=trusted_sha)
    except Exception:
        raise WorkflowError("publisher-handoff-unavailable") from None
    try:
        artifact_id = os.environ.get("FINAL_ARTIFACT_ID", "")
        prepared = report(value, artifact_id=run_id(artifact_id) if artifact_id else None)
    except Exception:
        raise WorkflowError("publisher-report-unavailable") from None
    adapter = GitHubPublisherAdapter(REPOSITORY, GitHubPublisherHTTPTransport(REPOSITORY, os.environ.get("GITHUB_TOKEN")))
    outcome = publish(prepared, adapter, EXPECTED_AUTHOR)
    print("publisher-" + outcome.state.value)
    # A successful comment write cannot make a failed/unresolved pipeline green.
    return int(outcome.state == PublicationState.STALE or not successful(prepared))


PUBLISHER_FAILURES = frozenset({"publisher-preflight-unavailable", "publisher-handoff-unavailable",
                               "publisher-report-unavailable", "publisher-read-unavailable",
                               "publisher-write-unavailable"})


def main(operation):
    try:
        return _main(operation)
    except Exception as error:
        if operation != "publish":
            raise
        # Print only host-owned phase categories at the CLI boundary. Never
        # expose response bodies, arbitrary exception text, tokens, or paths.
        category = (error.phase if type(error) is PublicationError else
                    str(error) if type(error) is WorkflowError else None)
        raise WorkflowError(category if category in PUBLISHER_FAILURES else
                            "publisher-preflight-unavailable") from None


if __name__ == "__main__":
    import sys
    def terminate(*_):
        raise WorkflowError("host deadline or cancellation")
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGALRM, terminate)
    signal.alarm(TOTAL_SECONDS)
    try:
        if len(sys.argv) != 2:
            raise WorkflowError("one fixed operation required")
        code = main(sys.argv[1])
    except Exception as error:
        category = str(error) if type(error) is WorkflowError else None
        print(category if category in PUBLISHER_FAILURES else
              "Trusted test-agent integration unavailable.", file=sys.stderr)
        code = 1
    finally:
        signal.alarm(0)
    sys.exit(code)
