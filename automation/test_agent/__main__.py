"""Trusted stdin/stdout orchestration. No application writes or execution."""

import argparse
from dataclasses import asdict, fields
import os
import re
import stat
import sys

from .agent import AgentError, State, UnitTestAgent
from .context import (MAX_TOTAL_CONTEXT_BYTES, CandidateTest, ChangedFile,
                      ContextBundle, Evidence, WorkflowIdentity)
from .contracts import (ContractError, ExecutionKind, ExecutionResult, FinalHostStatus,
                        HostResult, SourceIdentity, VerifierResult)
from .provider import (FakeProvider, LocalLlamaProvider, ProviderError,
                       canonical, digest, strict_json)


MAX_INPUT_BYTES = 128 * 1024


def _object(value, kind):
    if type(value) is not dict or set(value) != {f.name for f in fields(kind)}:
        raise AgentError("invalid trusted input fields")
    return value.copy()


def _read(path, maximum):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AgentError("trusted input must be a regular non-linked file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise AgentError("trusted input byte limit")
        return raw
    except OSError:
        raise AgentError("trusted input unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def load_host_bundle(raw: bytes, expected_sha256: str):
    """Only host-produced T03 serialization. Digest must arrive over a trusted channel.

    This authenticates a caller-pinned byte snapshot, not the origin of a file.
    Never pass PR attachments or model-produced contexts/digests to this interface.
    """
    if (type(expected_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or digest(raw) != expected_sha256):
        raise AgentError("trusted context digest mismatch")
    obj = _object(strict_json(raw.decode("utf-8"), MAX_TOTAL_CONTEXT_BYTES), ContextBundle)
    obj["source"] = SourceIdentity.from_mapping(obj["source"])
    obj["workflow"] = WorkflowIdentity(**_object(obj["workflow"], WorkflowIdentity))
    for name, kind in (("changed_files", ChangedFile), ("sources", Evidence),
                       ("existing_tests", Evidence), ("fixtures", Evidence),
                       ("configuration", Evidence), ("candidate_tests", CandidateTest)):
        if type(obj[name]) is not list:
            raise AgentError("trusted evidence array required")
        values = []
        for value in obj[name]:
            item = _object(value, kind)
            if "reasons" in item:
                if type(item["reasons"]) is not list:
                    raise AgentError("trusted reasons array required")
                item["reasons"] = tuple(item["reasons"])
            values.append(kind(**item))
        obj[name] = tuple(values)
    if type(obj["markers"]) is not list:
        raise AgentError("trusted markers array required")
    obj["markers"] = tuple(obj["markers"])
    for name in ("limits", "usage"):
        if type(obj[name]) is not dict:
            raise AgentError("trusted budget object required")
        obj[name] = tuple(sorted(obj[name].items()))
    bundle = ContextBundle(**obj)
    if (bundle.schema_version != 1 or bundle.workflow.head_sha != bundle.source.head_sha
            or bundle.to_bytes() != raw):
        raise AgentError("trusted canonical context or source mismatch")
    return bundle


def load_host_feedback(value, proposal_sha256):
    # Only the orchestrating host may write this line. Never accept provider JSON here.
    if type(value) is not dict or set(value) != {"proposal_sha256", "host_result"}:
        raise AgentError("trusted feedback envelope required")
    if value["proposal_sha256"] != proposal_sha256:
        raise AgentError("trusted feedback proposal mismatch")
    obj = _object(value["host_result"], HostResult)
    obj["source"] = SourceIdentity.from_mapping(obj["source"])
    obj["status"] = FinalHostStatus(obj["status"])
    if type(obj["verifier_results"]) is not list or len(obj["verifier_results"]) > 8:
        raise AgentError("trusted verification array limit")
    records = []
    for value in obj["verifier_results"]:
        item = _object(value, VerifierResult)
        item["kind"] = ExecutionKind(item["kind"])
        item["result"] = ExecutionResult(item["result"])
        records.append(VerifierResult(**item))
    obj["verifier_results"] = tuple(records)
    return HostResult(**obj)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Advisory test proposal; no writes, execution, or model startup")
    parser.add_argument("--context", required=True, help="trusted canonical T03 context file")
    parser.add_argument("--context-sha256", required=True, help="digest supplied by the trusted collector")
    parser.add_argument("--repository-root", required=True, help="host-prepared exact-head checkout for T02 reads")
    parser.add_argument("--provider", choices=("fake", "local"), required=True)
    parser.add_argument("--transcript", help="fake only: JSON array of raw stage response strings")
    parser.add_argument("--port", type=int, help="local only: host-owned loopback port (default 8765)")
    parser.add_argument("--resume-stdin", action="store_true", help="after draft output, consume one trusted host feedback line")
    args = parser.parse_args(argv)
    if args.provider == "fake" and (not args.transcript or args.port is not None):
        parser.error("fake requires --transcript and forbids --port")
    if args.provider == "local" and args.transcript is not None:
        parser.error("local forbids --transcript")
    try:
        from pathlib import Path
        bundle = load_host_bundle(_read(args.context, MAX_TOTAL_CONTEXT_BYTES), args.context_sha256)
        if args.provider == "fake":
            responses = strict_json(_read(args.transcript, MAX_INPUT_BYTES).decode(), MAX_INPUT_BYTES)
            if type(responses) is not list:
                raise AgentError("fake transcript array required")
            provider = FakeProvider(tuple(responses))
        else:
            provider = LocalLlamaProvider(8765 if args.port is None else args.port)
        agent = UnitTestAgent(bundle, Path(args.repository_root), provider)
        result = agent.run()
        sys.stdout.write(result.to_bytes().decode() + "\n")
        sys.stdout.flush()
        if args.resume_stdin and result.state == State.AWAITING_VERIFICATION:
            line = sys.stdin.buffer.readline(MAX_INPUT_BYTES + 1)
            if not line.endswith(b"\n") or len(line) > MAX_INPUT_BYTES:
                raise AgentError("trusted feedback line missing or oversized")
            feedback = load_host_feedback(strict_json(line.decode(), MAX_INPUT_BYTES),
                                          digest(canonical(asdict(result.proposal))))
            result = agent.resume(feedback)
            sys.stdout.write(result.to_bytes().decode() + "\n")
        return 1 if result.state == State.FAILED else 0
    except (AgentError, ContractError, ProviderError, OSError, ValueError, TypeError, KeyError):
        # No traceback, absolute input paths, remote messages, or raw output in errors.
        sys.stderr.write("test-agent: trusted input or provider rejected\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
