"""Read adapter bounds and fixed destination, using only fake network objects."""

import base64
from copy import deepcopy
import json

import pytest

from automation.test_agent import github
from automation.test_agent.github import (
    API_HOST, CollectionError, GitHubHTTPTransport, GitHubReader, MAX_PAGES,
    MAX_REQUESTS, MAX_RESPONSE_BYTES, MAX_RESPONSE_TOTAL_BYTES, Response, TIMEOUT_SECONDS,
)
from automation.tests.t03_fixtures import HEAD, REPOSITORY, RUN, FixtureTransport


class StaticTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, path, query):
        self.calls.append((path, query))
        return self.response


@pytest.mark.parametrize("body", [b"not-json", b"\xff", b'{"id":1,"id":2}',
                                  b"[" * 2000 + b"]" * 2000,
                                  b" " * (MAX_RESPONSE_BYTES + 1)])
def test_invalid_or_unbounded_json_fails_without_echo(body):
    reader = GitHubReader(REPOSITORY, StaticTransport(Response(200, body)))
    with pytest.raises(CollectionError) as caught:
        reader.workflow_run(RUN)
    assert len(str(caught.value)) < 100


@pytest.mark.parametrize("response", [Response(302, b"secret"), Response(403, b"secret"),
                                      Response(200, "not bytes"), Response(200, b"{}", "yes"), {}])
def test_invalid_response_envelopes_rejected(response):
    with pytest.raises(CollectionError):
        GitHubReader(REPOSITORY, StaticTransport(response)).workflow_run(RUN)


def test_page_budget_exhaustion_never_silently_returns_partial_files():
    transport = StaticTransport(Response(200, b'[{}]', True))
    reader = GitHubReader(REPOSITORY, transport)
    with pytest.raises(CollectionError, match="page budget"):
        reader.changed_files(12)
    assert [dict(query)["page"] for _, query in transport.calls] == [str(n) for n in range(1, MAX_PAGES + 1)]


def test_association_later_page_is_seen():
    fixture = FixtureTransport()
    closed = deepcopy(fixture.pr)
    closed["state"] = "closed"
    fixture.associations = [dict(closed, number=index + 100) for index in range(30)] + [fixture.pr]
    assert fixture.collect().source.pr_number == 12
    assert any("/pulls" in path and dict(query).get("page") == "2" for path, query in fixture.calls)


def test_request_and_aggregate_response_budgets_at_and_over_limit():
    transport = StaticTransport(Response(200, b"{}"))
    reader = GitHubReader(REPOSITORY, transport)
    reader.requests = MAX_REQUESTS - 1
    assert reader.workflow_run(RUN) == {}
    with pytest.raises(CollectionError, match="request budget"):
        reader.workflow_run(RUN)
    assert len(transport.calls) == 1
    reader = GitHubReader(REPOSITORY, transport)
    reader.response_bytes = MAX_RESPONSE_TOTAL_BYTES - 2
    assert reader.workflow_run(RUN) == {}
    with pytest.raises(CollectionError, match="response budget"):
        reader.workflow_run(RUN)


def test_response_limit_exact_size_accepted():
    body = b"{}" + b" " * (MAX_RESPONSE_BYTES - 2)
    assert GitHubReader(REPOSITORY, StaticTransport(Response(200, body))).workflow_run(RUN) == {}


def test_transport_exception_does_not_expose_secrets_or_chain():
    class Failing:
        def get(self, path, query):
            raise RuntimeError("Authorization: Bearer injected-sensitive-placeholder")
    with pytest.raises(CollectionError) as caught:
        GitHubReader(REPOSITORY, Failing()).workflow_run(RUN)
    assert str(caught.value) == "GitHub transport failed"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("method,args", [
    ("workflow_run", [True]), ("pull_request", ["12/../../secrets"]),
    ("associated_prs", ["https://evil.invalid"]), ("compare", [HEAD, "main"]),
    ("tree_at", ["HEAD"]), ("blob", [HEAD, 100, 99]),
])
def test_endpoint_identifiers_validated_before_transport(method, args):
    transport = StaticTransport(Response(200, b"{}"))
    reader = GitHubReader(REPOSITORY, transport)
    with pytest.raises(CollectionError):
        getattr(reader, method)(*args)
    assert transport.calls == []


@pytest.mark.parametrize("repository", ["https://evil.invalid/a", "owner/..", "../repo", "owner/r%2fp"])
def test_repository_identifiers_cannot_select_destinations(repository):
    with pytest.raises(CollectionError):
        GitHubReader(repository, StaticTransport(Response(200, b"{}")))


@pytest.mark.parametrize("mutation", ["sha", "truncated", "duplicate", "unsafe-path", "mode", "ancestor"])
def test_malformed_or_unsafe_tree_fails(mutation):
    fixture = FixtureTransport()
    path = f"/repos/{REPOSITORY}/git/trees/" + "e" * 40
    response = json.loads(fixture.get(path, (("recursive", "1"),)).body)
    if mutation == "sha":
        response["sha"] = HEAD
    elif mutation == "truncated":
        response["truncated"] = True
    elif mutation == "duplicate":
        response["tree"].append(deepcopy(response["tree"][0]))
    elif mutation == "unsafe-path":
        response["tree"][0]["path"] = "../outside"
    elif mutation == "mode":
        response["tree"][0]["mode"] = "777777"
    else:
        response["tree"].append(dict(response["tree"][0], path="frontend", mode="120000"))
    fixture.overrides[path, (("recursive", "1"),)] = fixture.response(response)
    with pytest.raises(CollectionError):
        fixture.collect()


@pytest.mark.parametrize("mutation", ["sha", "size", "encoding", "content", "base64", "oversized"])
def test_blob_bound_to_tree_sha_size_and_encoding(mutation):
    fixture = FixtureTransport()
    digest = fixture.head["frontend/src/features/todos/components/TodoItem.tsx"]["sha"]
    blob = fixture.blobs[digest]
    if mutation == "sha":
        blob["sha"] = HEAD
    elif mutation == "size":
        blob["size"] += 1
    elif mutation == "encoding":
        blob["encoding"] = "url"
    elif mutation == "content":
        blob["content"] = base64.b64encode(b"z" * blob["size"]).decode()
    elif mutation == "base64":
        blob["content"] = "!!!!"
    else:
        blob["content"] = "A" * (github.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(CollectionError):
        fixture.collect()


class FakeHTTPResponse:
    status = 200
    headers = {}
    body = b"{}"

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, maximum):
        self.maximum_read = maximum
        return self.body[:maximum]


def install_fake_https(monkeypatch, response):
    calls = []
    class Connection:
        def __init__(self, host, **kwargs):
            calls.append((host, kwargs))
        def request(self, method, path, *, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return response
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(github.http.client, "HTTPSConnection", Connection)
    return calls


def test_http_fixed_host_get_timeout_bounded_read_and_no_link_url_fetch(monkeypatch):
    response = FakeHTTPResponse()
    response.headers = {"Link": '<https://evil.invalid/secrets?page=2>; rel="next"'}
    calls = install_fake_https(monkeypatch, response)
    transport = GitHubHTTPTransport("sanitized-auth-placeholder")
    result = transport.get(f"/repos/{REPOSITORY}/pulls/12/files", (("page", "1"),))
    assert result.has_next
    assert calls[0][0] == API_HOST and calls[0][1]["timeout"] == TIMEOUT_SECONDS
    assert calls[1][0:2] == ("GET", f"/repos/{REPOSITORY}/pulls/12/files?page=1")
    assert response.maximum_read == MAX_RESPONSE_BYTES + 1
    assert calls[2] == "closed" and len(calls) == 3


@pytest.mark.parametrize("status,headers,body", [
    (302, {"Location": "https://evil.invalid"}, b"token"), (403, {}, b"token"),
    (200, {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b""),
    (200, {"Content-Encoding": "gzip"}, b""), (200, {}, b"x" * (MAX_RESPONSE_BYTES + 1)),
])
def test_http_rejects_redirect_error_encoding_and_oversized_response(monkeypatch, status, headers, body):
    response = FakeHTTPResponse()
    response.status, response.headers, response.body = status, headers, body
    calls = install_fake_https(monkeypatch, response)
    with pytest.raises(CollectionError) as caught:
        GitHubHTTPTransport().get(f"/repos/{REPOSITORY}/actions/runs/{RUN}")
    assert "token" not in str(caught.value)
    assert len(calls) == 3 and calls[-1] == "closed"


@pytest.mark.parametrize("path,query", [
    ("https://evil.invalid", ()), ("//evil.invalid/x", ()),
    (f"/repos/{REPOSITORY}/issues/12/comments", ()),
    (f"/repos/{REPOSITORY}/pulls/12", (("url", "https://evil.invalid"),)),
    (f"/repos/{REPOSITORY}/pulls/12", (("page", "5"),)),
])
def test_http_no_arbitrary_url_or_write_endpoint(monkeypatch, path, query):
    calls = install_fake_https(monkeypatch, FakeHTTPResponse())
    with pytest.raises(CollectionError):
        GitHubHTTPTransport().get(path, query)
    assert calls == []


def test_authentication_is_only_explicit_injection_and_header_safe(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret-must-not-be-used")
    calls = install_fake_https(monkeypatch, FakeHTTPResponse())
    GitHubHTTPTransport().get(f"/repos/{REPOSITORY}/actions/runs/{RUN}")
    assert "Authorization" not in calls[1][2]
    with pytest.raises(CollectionError):
        GitHubHTTPTransport("bad\r\nheader")
