"""Synthetic host evidence and finite publisher transport, with no network/code execution."""

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from automation.test_agent.agent import UnitTestAgent
from automation.test_agent.contracts import (ExecutionKind, ExecutionResult, FinalHostStatus,
                                           HostResult, VerifierResult)
from automation.test_agent.github import Response
from automation.test_agent.provider import FakeProvider, canonical, digest
from automation.test_agent.publisher import PublisherAuthor
from automation.test_agent.verification import CommandEvidence, Counts, VerificationReport, structural_contracts
from automation.verifier.profiles import PROFILES
from automation.tests.t04_fixtures import (bundle_for, decisions_for, install_originals, transcript)


AUTHOR = PublisherAuthor(123, "test-agent[bot]")


def fixture(root, case="frontend-generate", status=FinalHostStatus.VERIFIED, *, run=42, attempt=1, failed=False, unsupported=False):
    bundle = bundle_for(case)
    bundle = replace(bundle, source=replace(bundle.source, run_id=str(run)),
                     workflow=replace(bundle.workflow, run_id=run, run_attempt=attempt))
    tests = []
    for e in bundle.existing_tests:
        data = e.content.encode()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        tests.append(replace(e, blob_sha=blob, observed_bytes=len(data)))
    bundle = replace(bundle, existing_tests=tuple(tests))
    install_originals(bundle, root)
    if failed:
        result = UnitTestAgent(bundle, root, FakeProvider(("malformed",))).run()
        return bundle, result, None
    decisions = decisions_for(bundle, {"frontend": "unsupported"}) if unsupported else None
    initial = UnitTestAgent(bundle, root, FakeProvider(transcript(bundle, decisions))).run()
    if not initial.proposal.edits:
        return bundle, initial, None
    changes_hash = digest(initial.policy.changes.to_bytes())
    commands, records = [], []
    if status not in {FinalHostStatus.UNAVAILABLE, FinalHostStatus.STALE, FinalHostStatus.UNSUPPORTED}:
        stacks = {e.target.value for e in initial.policy.changes.edits}
        steps = [s for s in PROFILES if PROFILES[s].stack in stacks]
        steps.sort(key=lambda s: PROFILES[s].kind != "baseline")
        for index, step in enumerate(steps):
            p = PROFILES[step]
            broken = (status == FinalHostStatus.BASELINE_FAILED and p.kind == "baseline" or
                      status == FinalHostStatus.VERIFICATION_FAILED and p.kind == "patched")
            n = 1 if p.targeted else 3 if p.kind == "baseline" else 4
            path = next(e.path for e in initial.policy.changes.edits if e.target.value == p.stack)
            counts = None if p.command_id in {"tsc", "structural"} else Counts(n, 0 if broken else n, n if broken else 0, 0, 0, ((path, n),))
            refs = () if counts is None else ((f"reports/{index}/tests.json" if p.stack == "frontend" else f"reports/{index}/tests.xml", 25, "e" * 64),)
            c = CommandEvidence(step, p.command_id, ExecutionKind(p.kind), "sha256:" + "f" * 64,
                                bundle.source.head_sha, changes_hash, 0.25, 1 if broken else 0,
                                False, 12, 0, "a" * 64, digest(b""), refs, counts,
                                "/private/log/SHOULD_NOT_RENDER" if broken else None, not broken)
            if p.command_id == "structural":
                facts = tuple({**contract, "validator": "typescript-structural-v1", "syntax_diagnostics": 1 if broken else 0,
                               "jsx_nodes": 0, "subject_imported": True,
                               "categories": ["syntax-invalid"] if broken else [], "passed": not broken}
                              for contract in structural_contracts(bundle.source, initial.policy.changes))
                c = replace(c, structural=facts)
            commands.append(c)
            if counts:
                records.append(VerifierResult(step, p.command_id, c.kind,
                                              ExecutionResult.FAILED if broken else ExecutionResult.PASSED,
                                              n, c.exit_code, f"commands/{index}.json", 12))
            if broken:
                break
    report = VerificationReport(HostResult(bundle.source, status, tuple(records)), changes_hash,
                                None, tuple(commands), "/private/reason/SHOULD_NOT_RENDER", Path("/private/reports"))
    agent = UnitTestAgent(bundle, root, FakeProvider(transcript(bundle, feedback=report.host_result)))
    agent.run()
    return bundle, agent.resume(report.host_result), report


class FakeTransport:
    def __init__(self, identity, comments=()):
        self.identity = identity
        self.comment_values = list(deepcopy(comments))
        repo = {"id": identity.repository_id, "full_name": identity.repository}
        self.pr = {"number": identity.pr_number, "state": "open",
                   "base": {"sha": identity.base_sha, "repo": repo},
                   "head": {"sha": identity.head_sha, "repo": repo}}
        self.pr_reads = self.calls = self.writes = 0
        self.operations = []
        self.before_pr = lambda _: None
        self.override = None

    @staticmethod
    def comment(id, body, author=AUTHOR):
        return {"id": id, "body": body, "user": asdict(author)}

    def request(self, method, path, query=(), body=b""):
        self.calls += 1
        self.operations.append((method, path, query, body))
        if self.override is not None:
            value = self.override(method, path, query, body)
            if value is not None:
                return value
        if method == "GET" and "/pulls/" in path:
            self.pr_reads += 1
            self.before_pr(self)
            return Response(200, canonical(self.pr))
        if method == "GET":
            page = int(dict(query)["page"])
            start = (page - 1) * 30
            return Response(200, canonical(self.comment_values[start:start + 30]), start + 30 < len(self.comment_values))
        self.writes += 1
        text = json.loads(body)["body"]
        if method == "POST":
            comment = self.comment(500, text)
            self.comment_values.append(comment)
            return Response(201, canonical(comment))
        comment = next(c for c in self.comment_values if c["id"] == int(path.split("/")[-1]))
        comment["body"] = text
        return Response(200, canonical(comment))
