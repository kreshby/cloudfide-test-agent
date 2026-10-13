"""Sticky comment tests use finite fakes exclusively, including concrete HTTP."""

from dataclasses import replace
import json
import traceback

import pytest

from automation.test_agent.github import Response
from automation.test_agent.provider import canonical
from automation.test_agent.publisher import (API_HOST, MAX_COMMENT_BYTES, MAX_REQUESTS,
                                           MAX_RESPONSE_BYTES, MAX_RESPONSE_TOTAL_BYTES,
                                           GitHubPublisherAdapter, GitHubPublisherHTTPTransport,
                                           PublicationError, PublicationState, PublisherAuthor, publish)
from automation.test_agent.reporting import prepare_report
from automation.tests.t06_fixtures import AUTHOR, FakeTransport, fixture


def report(root, *, run=42, attempt=1):
    bundle, result, verification = fixture(root, run=run, attempt=attempt)
    return prepare_report(bundle, result, verification)


def send(prepared, transport):
    return publish(prepared, GitHubPublisherAdapter(prepared.identity.repository, transport), AUTHOR)


def test_create_rerun_updates_same_id_without_duplicate(tmp_path):
    first = report(tmp_path)
    transport = FakeTransport(first.identity)
    assert send(first, transport).state == PublicationState.CREATED
    assert transport.pr_reads == 2 and transport.writes == 1
    second = report(tmp_path, attempt=2)
    outcome = send(second, transport)
    assert outcome.state == PublicationState.UPDATED and outcome.comment_id == 500
    assert len(transport.comment_values) == 1 and transport.writes == 2
    assert send(second, transport).state == PublicationState.IDEMPOTENT
    assert transport.writes == 2
    assert [method for method, _, _, _ in transport.operations if method != "GET"] == ["POST", "PATCH"]


def test_foreign_marker_and_author_identity_cannot_hijack(tmp_path):
    prepared = report(tmp_path)
    for foreign in (PublisherAuthor(124, AUTHOR.login), PublisherAuthor(AUTHOR.id, "other[bot]"),
                    PublisherAuthor(AUTHOR.id, AUTHOR.login, "User")):
        transport = FakeTransport(prepared.identity, [FakeTransport.comment(9, prepared.body, foreign)])
        assert send(prepared, transport).state == PublicationState.CREATED
        assert transport.comment_values[0]["body"] == prepared.body
        assert len(transport.comment_values) == 2


def test_duplicate_owned_markers_fail_closed(tmp_path):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity, [FakeTransport.comment(9, prepared.body), FakeTransport.comment(10, prepared.body)])
    with pytest.raises(PublicationError, match="duplicate publisher sticky"):
        send(prepared, transport)
    assert transport.writes == 0


@pytest.mark.parametrize("read", [1, 2])
@pytest.mark.parametrize("field", ["head", "base", "closed", "missing", "repository", "number", "repo-id"])
def test_stale_identity_before_or_during_lookup_never_writes(tmp_path, read, field):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity)
    def mutate(t):
        if t.pr_reads != read:
            return
        if field in {"head", "base"}:
            t.pr[field]["sha"] = "9" * 40
        elif field == "closed":
            t.pr["state"] = "closed"
        elif field == "missing":
            t.pr = None
        elif field == "repository":
            t.pr["base"]["repo"]["full_name"] = "wrong/repo"
        elif field == "number":
            t.pr["number"] += 1
        else:
            t.pr["base"]["repo"]["id"] += 1
    transport.before_pr = mutate
    if field in {"repository", "number"}:
        with pytest.raises(PublicationError):
            send(prepared, transport)
    else:
        assert send(prepared, transport).state == PublicationState.STALE
    assert transport.writes == 0


@pytest.mark.parametrize("incoming,existing", [((41, 1), (42, 1)), ((42, 1), (42, 2))])
def test_old_run_or_attempt_cannot_overwrite(tmp_path, incoming, existing):
    old, new = report(tmp_path, run=incoming[0], attempt=incoming[1]), report(tmp_path, run=existing[0], attempt=existing[1])
    transport = FakeTransport(old.identity, [FakeTransport.comment(9, new.body)])
    assert send(old, transport).state == PublicationState.SUPERSEDED
    assert transport.writes == 0 and transport.comment_values[0]["body"] == new.body


def test_equal_run_attempt_only_identical_publication_is_idempotent(tmp_path):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity, [FakeTransport.comment(9, prepared.body)])
    assert send(prepared, transport).state == PublicationState.IDEMPOTENT
    transport.comment_values[0]["body"] += "conflicting facts\n"
    with pytest.raises(PublicationError, match="conflicting publication"):
        send(prepared, transport)
    assert transport.writes == 0
    transport.comment_values[0]["body"] = prepared.body.replace("policy=" + prepared.identity.policy_sha, "policy=" + "9" * 40)
    with pytest.raises(PublicationError, match="conflicting publication"):
        send(prepared, transport)


@pytest.mark.parametrize("mutation", [lambda b: b.replace("run=42", "run=042"),
                                     lambda b: b.replace("attempt=1", "attempt=0"),
                                     lambda b: "prefix\n" + b,
                                     lambda b: b.replace("head=", "head_sha="),
                                     lambda b: b + b.split("\n", 1)[0],
                                     lambda b: b.replace("run=42", "run=9999999999999999999")])
def test_malformed_owned_marker_cannot_select_a_comment(tmp_path, mutation):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity, [FakeTransport.comment(9, mutation(prepared.body))])
    with pytest.raises(PublicationError, match="malformed publisher sticky marker"):
        send(prepared, transport)
    assert transport.writes == 0


def test_similar_marker_does_not_select_comment(tmp_path):
    prepared = report(tmp_path)
    other = prepared.body.replace("taskly-unit-test-agent-report:v2", "taskly-unit-test-agent-report:other")
    transport = FakeTransport(prepared.identity, [FakeTransport.comment(9, other)])
    assert send(prepared, transport).state == PublicationState.CREATED
    assert transport.comment_values[0]["body"] == other


def test_oversized_incoming_rejected_before_any_transport_call(tmp_path):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity)
    with pytest.raises(PublicationError, match="comment byte limit"):
        send(replace(prepared, body=prepared.body + "x" * MAX_COMMENT_BYTES), transport)
    assert transport.calls == transport.writes == 0


@pytest.mark.parametrize("mode", ["pages", "count", "duplicate", "empty-next", "page-size"])
def test_comment_and_pagination_budgets_fail_closed(tmp_path, mode):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity)
    foreign = PublisherAuthor(200, "foreign")
    if mode == "count":
        transport.comment_values = [FakeTransport.comment(i + 1, "ordinary", foreign) for i in range(101)]
    elif mode == "duplicate":
        transport.comment_values = [FakeTransport.comment(1, "ordinary"), FakeTransport.comment(1, "ordinary")]
    else:
        def override(method, path, query, body):
            if "/pulls/" in path:
                return None
            page = int(dict(query)["page"])
            n = 0 if mode == "empty-next" else 31 if mode == "page-size" else 1
            return Response(200, canonical([FakeTransport.comment(page * 100 + i, "ordinary", foreign) for i in range(n)]), True)
        transport.override = override
    with pytest.raises(PublicationError):
        send(prepared, transport)
    assert transport.writes == 0 and transport.calls <= 5


@pytest.mark.parametrize("mode", ["single-response", "total-response", "requests", "duplicate-json", "invalid-json"])
def test_transport_and_json_budgets_fail_closed(tmp_path, mode):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity)
    adapter = GitHubPublisherAdapter(prepared.identity.repository, transport)
    if mode == "total-response":
        adapter.response_bytes = MAX_RESPONSE_TOTAL_BYTES
    elif mode == "requests":
        adapter.requests = MAX_REQUESTS
    else:
        body = {"single-response": b"x" * (MAX_RESPONSE_BYTES + 1),
                "duplicate-json": b'{"a":1,"a":2}', "invalid-json": b'{"a":NaN}'}[mode]
        transport.override = lambda *_: Response(200, body)
    with pytest.raises(PublicationError):
        publish(prepared, adapter, AUTHOR)
    assert transport.writes == 0


def test_injected_exception_token_is_not_exposed(tmp_path):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity)
    def bad(*_):
        raise RuntimeError("DUMMY_T06_TOKEN_ONLY")
    transport.override = bad
    with pytest.raises(PublicationError) as caught:
        send(prepared, transport)
    assert "DUMMY_T06_TOKEN_ONLY" not in str(caught.value) and caught.value.__suppress_context__
    assert "DUMMY_T06_TOKEN_ONLY" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("method,path,query,body", [
    ("DELETE", "/repos/example/taskly/issues/comments/1", (), b""),
    ("GET", "https://evil.example", (), b""),
    ("GET", "/repos/example/taskly/graphql", (), b""),
    ("GET", "/repos/wrong/repo/pulls/1", (), b""),
    ("POST", "/repos/example/taskly/pulls/1", (), b"{}"),
    ("GET", "/repos/example/taskly/pulls/1", (("url", "https://evil.example"),), b""),
    ("GET", "/repos/example/taskly/issues/1/comments", (("per_page", "30"), ("page", "5")), b""),
    ("GET", "/repos/example/taskly/issues/1/comments", ("malformed", ("page", "1")), b""),
    ("PATCH", "/repos/example/taskly/issues/comments/1", (), b'{"body":"raw"}'),
])
def test_unsupported_operations_rejected_before_network(monkeypatch, method, path, query, body):
    monkeypatch.setattr("http.client.HTTPSConnection", lambda *_a, **_k: pytest.fail("network attempted"))
    transport = GitHubPublisherHTTPTransport("example/taskly", "DUMMY_T06_TOKEN_ONLY")
    with pytest.raises(PublicationError):
        transport.request(method, path, query, body)


def test_concrete_transport_fixed_host_auth_and_errors_are_offline(monkeypatch, tmp_path):
    prepared = report(tmp_path)
    captured = {}
    class Remote:
        status = 200
        headers = {}
        data = canonical({"number": 1})
        def getheader(self, name, default=None):
            return self.headers.get(name, default)
        def read1(self, count):
            assert 0 < count <= 4096
            data, self.data = self.data[:count], self.data[count:]
            return data
        def isclosed(self):
            return not self.data
    remote = Remote()
    class Socket:
        def settimeout(self, timeout):
            assert 0 < timeout <= 10
        def shutdown(self, how):
            pass
        def close(self):
            pass
    class Connection:
        sock = Socket()
        def __init__(self, host, **kwargs):
            assert host == API_HOST and kwargs["timeout"] == 10
        def request(self, method, path, **kwargs):
            captured.update(method=method, path=path, **kwargs)
        def connect(self):
            pass
        def getresponse(self):
            return remote
        def close(self):
            captured["closed"] = True
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    transport = GitHubPublisherHTTPTransport(prepared.identity.repository, "DUMMY_T06_TOKEN_ONLY")
    path = f"/repos/{prepared.identity.repository}/pulls/{prepared.identity.pr_number}"
    assert transport.request("GET", path).status == 200
    assert captured["headers"]["Authorization"] == "Bearer DUMMY_T06_TOKEN_ONLY"
    for status, headers, data in [(302, {"Location": "https://github.com"}, b""),
                                   (500, {}, b"DUMMY_T06_TOKEN_ONLY"),
                                   (200, {"Content-Encoding": "gzip"}, b""),
                                   (200, {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b""),
                                   (200, {}, b"x" * (MAX_RESPONSE_BYTES + 1))]:
        remote.status, remote.headers, remote.data = status, headers, data
        with pytest.raises(PublicationError) as caught:
            transport.request("GET", path)
        assert "DUMMY_T06_TOKEN_ONLY" not in str(caught.value)
    assert captured["closed"]


def test_missing_pr_and_write_receipt_identity(tmp_path):
    prepared = report(tmp_path)
    transport = FakeTransport(prepared.identity)
    transport.override = lambda *_: Response(404, b"")
    assert send(prepared, transport).state == PublicationState.STALE
    assert transport.writes == 0
    transport = FakeTransport(prepared.identity)
    def wrong_author(method, path, query, body):
        if method == "POST":
            return Response(201, canonical(FakeTransport.comment(500, json.loads(body)["body"], PublisherAuthor(456, "foreign"))))
    transport.override = wrong_author
    with pytest.raises(PublicationError, match="receipt mismatch"):
        send(prepared, transport)


def test_http_deadline_closes_captured_socket_without_network(monkeypatch):
    events = []
    class Socket:
        def shutdown(self, _):
            events.append("shutdown")
        def close(self):
            events.append("socket-close")
        def settimeout(self, _):
            pass
    class Connection:
        sock = Socket()
        def __init__(self, *_a, **_k):
            pass
        def connect(self):
            pass
        def close(self):
            events.append("connection-close")
        def request(self, *_a, **_k):
            pytest.fail("expired operation attempted")
    class Timer:
        def __init__(self, _, callback):
            self.callback = callback
        def start(self):
            self.callback()
        def cancel(self):
            events.append("cancel")
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    monkeypatch.setattr("automation.test_agent.publisher.threading.Timer", Timer)
    with pytest.raises(PublicationError, match="deadline"):
        GitHubPublisherHTTPTransport("example/taskly").request("GET", "/repos/example/taskly/pulls/1")
    assert events == ["shutdown", "socket-close", "cancel", "connection-close"]
