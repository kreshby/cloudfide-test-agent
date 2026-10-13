"""Pinned public PR #2 evidence; finite HTTP fixtures never access GitHub."""

from dataclasses import asdict
import http.client
import json
from pathlib import Path
import socket

from automation.test_agent.__main__ import load_host_bundle
from automation.test_agent.github import Response
from automation.test_agent.provider import canonical, digest
from automation.test_agent import workflow as w


BASE = "e43bdccff33da0d5e598ef522819883148aafa59"
HEAD = "f4a754bc6f0b28e587eb4b8e0d9d6724d96a22bf"
CONTEXT_HASH = "143e2d0ef67a09e71d5cf2484cd27bd92ff42b92a97aa3577bd74e0f1b500e84"
HANDOFF_HASH = "26f3bd1e0076fc0bdc9ab32d7ea7f607e910086efd629c08b418b43096e6f126"
FIXTURES = Path(__file__).parent / "fixtures"
FRONTEND_TEST = "frontend/src/features/todos/api/todos.test.ts"
BACKEND_TEST = "backend/tests/features/todos/test_router.py"


def live_bundle():
    return load_host_bundle((FIXTURES / "t07b2-context.json").read_bytes(), CONTEXT_HASH)


def live_handoff():
    raw = (FIXTURES / "t07b2-handoff.json").read_bytes()
    assert digest(raw) == HANDOFF_HASH
    return raw


class LiveReadAPI:
    """The immutable live source and in-progress current run, for real preflight."""

    def __init__(self):
        value = json.loads(live_handoff())
        self.identity = value["identity"]
        self.repo = {"id": self.identity["repository_id"], "full_name": w.REPOSITORY, "private": False}
        self.pr = {"number": 2, "state": "open", "changed_files": 11,
                   "base": {"sha": BASE, "repo": self.repo}, "head": {"sha": HEAD, "repo": self.repo}}
        self.ci = {**value["source_ci"], "id": value["source_ci"]["run_id"],
                   "repository": self.repo, "head_repository": self.repo}
        self.agent = {"id": self.identity["workflow_run_id"], "run_attempt": 1, "workflow_id": 383151448,
                      "repository": self.repo, "head_repository": self.repo, "name": w.AGENT_NAME,
                      "path": w.AGENT_PATH, "event": "workflow_run", "status": "in_progress",
                      "head_sha": BASE, "head_branch": "main"}
        self.calls = []

    def get(self, path, query=()):
        self.calls.append((path, query))
        endpoint = path.removeprefix("/repos/" + w.REPOSITORY)
        objects = {"": self.repo, "/actions/workflows/ci.yml": {
            "id": self.ci["workflow_id"], "name": w.CI_NAME, "path": w.CI_PATH, "state": "active"},
            "/actions/workflows/test-agent.yml": {
                "id": 383151448, "name": w.AGENT_NAME, "path": w.AGENT_PATH, "state": "active"},
            "/actions/runs/38397187600": self.ci, "/actions/runs/38397271649": self.agent,
            "/pulls/2": self.pr, f"/commits/{HEAD}/pulls": [self.pr]}
        if endpoint == f"/compare/{BASE}...{HEAD}":
            files = [{"filename": f.path, "sha": f.blob_sha, "status": f.status,
                      "additions": f.additions, "deletions": f.deletions, "changes": f.changes,
                      "patch": f.patch} for f in live_bundle().changed_files]
            return Response(200, canonical({"base_commit": {"sha": BASE}, "merge_base_commit": {"sha": BASE},
                                           "total_commits": 1, "commits": [{"sha": HEAD}],
                                           "status": "ahead", "files": files}))
        assert endpoint in objects
        return Response(200, canonical(objects[endpoint]))


class HTTPFixture:
    """Real stdlib HTTPResponse/socket EOF semantics with finite JSON receipts.

    Only local socket pairs are used. No remote connection, token, response log,
    model, or generated application test execution is involved.
    """

    def __init__(self, pr, *, close=True, framing="length"):
        self.pr, self.close, self.framing = pr, close, framing
        self.comments, self.operations = [], []
        self.fail_method = None
        self.receipt_author = w.EXPECTED_AUTHOR
        self.extra_length = 0
        self.closed = 0

    def connection(self, host, **kwargs):
        assert host == "api.github.com" and kwargs["timeout"] == 10
        fixture = self

        class Connection:
            def __init__(self):
                self.sock, self.peer = socket.socketpair()
                self.response = None

            def connect(self):
                pass

            def request(self, method, path, *, body, headers):
                assert "Authorization" not in headers
                fixture.operations.append((method, path, body))
                if method == fixture.fail_method:
                    status, data = 403, b"UNTRUSTED_RESPONSE_MUST_NOT_RENDER"
                elif method == "GET" and path.endswith("/pulls/2"):
                    status, data = 200, canonical(fixture.pr)
                elif method == "GET":
                    assert path.endswith("/issues/2/comments?per_page=30&page=1")
                    status, data = 200, canonical(fixture.comments)
                else:
                    comment = {"id": 500, "body": json.loads(body)["body"], "user": asdict(fixture.receipt_author)}
                    if method == "POST":
                        assert path.endswith("/issues/2/comments")
                        assert not fixture.comments
                        fixture.comments.append(comment)
                        status = 201
                    else:
                        assert method == "PATCH" and path.endswith("/issues/comments/500")
                        fixture.comments[:] = [comment]
                        status = 200
                    data = canonical(comment)
                wire = f"HTTP/1.1 {status} Fixture\r\n".encode()
                if fixture.close:
                    wire += b"Connection: close\r\n"
                if fixture.framing == "length":
                    wire += f"Content-Length: {len(data) + fixture.extra_length}\r\n\r\n".encode() + data
                elif fixture.framing == "chunked":
                    wire += b"Transfer-Encoding: chunked\r\n\r\n" + f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n"
                else:
                    assert fixture.close and fixture.framing == "eof"
                    wire += b"\r\n" + data
                self.peer.sendall(wire)
                self.peer.shutdown(socket.SHUT_WR)

            def getresponse(self):
                self.response = http.client.HTTPResponse(self.sock)
                self.response.begin()
                if self.response.will_close:
                    self.sock.close()  # Matches HTTPConnection.getresponse().
                return self.response

            def close(self):
                if self.response is not None:
                    self.response.close()
                self.sock.close()
                self.peer.close()
                fixture.closed += 1

        return Connection()


def publisher_environment(tmp_path, monkeypatch):
    import resource
    api = LiveReadAPI()
    directory = tmp_path / "t07-handoff"
    directory.mkdir()
    (directory / "handoff.json").write_bytes(live_handoff())
    env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "ImageOS": "ubuntu24",
           "GITHUB_REPOSITORY": w.REPOSITORY, "GITHUB_REPOSITORY_ID": str(api.repo["id"]),
           "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "38397271649", "GITHUB_RUN_ATTEMPT": "1",
           "GITHUB_EVENT_NAME": "workflow_run", "RUNNER_TEMP": str(tmp_path),
           "TRUSTED_WORKFLOW_SHA": BASE, "TRUSTED_WORKFLOW_REF": w.REPOSITORY + "/" + w.AGENT_PATH + "@refs/heads/main",
           "SOURCE_CI_RUN_ID": "38397187600", "EXPECTED_PR_NUMBER": "2", "HANDOFF_SHA256": HANDOFF_HASH,
           "FINAL_ARTIFACT_ID": ""}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(resource, "setrlimit", lambda *_: None)
    monkeypatch.setattr(w, "trusted_checkout", lambda *_: live_bundle().source.policy_sha)
    monkeypatch.setattr(w, "GitHubHTTPTransport", lambda *_: api)
    return api
