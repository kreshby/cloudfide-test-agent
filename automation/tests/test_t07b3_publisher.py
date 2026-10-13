"""Concrete publisher transport + adapter over finite localhost sockets."""

from dataclasses import asdict
import http.client
import json

import pytest

from automation.test_agent import publisher as p
from automation.test_agent.handoff import report
from automation.test_agent.workflow import EXPECTED_AUTHOR, REPOSITORY
from automation.tests.t07b2_fixtures import LiveReadAPI, live_handoff
from automation.tests.t07b3_fixtures import WireAPI


def setup(monkeypatch, *, fault=None, close=True, framing="length", patch=False):
    prepared = report(json.loads(live_handoff()))
    pr = LiveReadAPI().pr
    writes = []
    def reply(method, path, body):
        if method == "GET":
            if "/pulls/" in path:
                return 200, pr
            return 200, ([{"id": 500, "body": prepared.body.replace("No accepted", "Previously accepted"),
                           "user": asdict(EXPECTED_AUTHOR)}] if patch else [])
        writes.append(method)
        if fault == "disconnect":
            return None, b""
        if type(fault) is int:
            return fault, b"PRIVATE_REMOTE_RESPONSE"
        receipt = {"id": 500, "body": json.loads(body)["body"], "user": asdict(EXPECTED_AUTHOR)}
        if fault == "json":
            return 201, b"PRIVATE_REMOTE_RESPONSE"
        if fault == "author":
            receipt["user"]["id"] = 7
        if fault == "body":
            receipt["body"] = "PRIVATE_REMOTE_RESPONSE"
        if fault == "id":
            receipt["id"] = 0
        if fault == "patch-id":
            receipt["id"] = 501
        return (200 if method == "PATCH" else 201), receipt
    api = WireAPI(reply, close=close, framing=framing)
    # Route only the trusted fixed host to a local socket. Production request,
    # HTTPResponse, response loop, adapter validation and publication are real.
    factory = http.client.HTTPConnection
    local_port = []
    def local(host, *, timeout, context):
        assert host == "api.github.com"
        return factory("127.0.0.1", local_port[0], timeout=timeout)
    monkeypatch.setattr(p.http.client, "HTTPSConnection", local)
    if fault == "truncated":
        # Truncate only the write receipt, after the read phases succeeded.
        original = api.reply
        def truncated(method, path, body):
            value = original(method, path, body)
            api.extra_length = int(method != "GET")
            return value
        api.reply = truncated
    if fault == "timeout":
        monkeypatch.setattr(p, "TIMEOUT_SECONDS", .04)
        original = api.reply
        def delayed(method, path, body):
            value = original(method, path, body)
            api.delay = .15 if method != "GET" else 0
            return value
        api.reply = delayed
    if patch:
        from automation.test_agent.handoff import report as render
        value = json.loads(live_handoff())
        value["identity"]["run_attempt"] = 2
        prepared = render(value)
    # Older sticky comment for a PATCH test uses attempt one.
    if patch:
        old = report(json.loads(live_handoff()))
        original = api.reply
        def older(method, path, body):
            if method == "GET" and "/comments?" in path:
                return 200, [{"id": 500, "body": old.body, "user": asdict(EXPECTED_AUTHOR)}]
            return original(method, path, body)
        api.reply = older
    return api, prepared, writes, local_port


@pytest.mark.parametrize("close,framing", [(True, "length"), (False, "length"), (True, "chunked"), (True, "eof")])
def test_post_201_real_socket_receipt(monkeypatch, close, framing):
    api, prepared, writes, local_port = setup(monkeypatch, close=close, framing=framing)
    with api.serve() as server_port:
        local_port.append(server_port)
        adapter = p.GitHubPublisherAdapter(REPOSITORY, p.GitHubPublisherHTTPTransport(REPOSITORY))
        result = p.publish(prepared, adapter, EXPECTED_AUTHOR)
    assert result.state == p.PublicationState.CREATED and result.comment_id == 500
    assert writes == ["POST"]
    assert [m for m, _, _ in api.operations] == ["GET", "GET", "GET", "POST"]


@pytest.mark.parametrize("fault,category", [
    (401, "http-401"), (403, "http-403"), (404, "http-404"), (422, "http-422"), (500, "http-5xx"),
    (302, "http-other"), ("json", "json"), ("author", "receipt"), ("body", "receipt"),
    ("id", "receipt"), ("patch-id", "receipt"), ("truncated", "transport"), ("timeout", "timeout"),
    ("disconnect", "transport"),
])
def test_real_socket_write_failure_has_fixed_category_and_no_retry(monkeypatch, fault, category):
    api, prepared, writes, local_port = setup(monkeypatch, fault=fault, patch=fault == "patch-id")
    with api.serve() as server_port:
        local_port.append(server_port)
        adapter = p.GitHubPublisherAdapter(REPOSITORY, p.GitHubPublisherHTTPTransport(REPOSITORY))
        with pytest.raises(p.PublicationError) as caught:
            p.publish(prepared, adapter, EXPECTED_AUTHOR)
    assert caught.value.phase == "publisher-write-" + category
    assert "PRIVATE_REMOTE_RESPONSE" not in str(caught.value)
    assert len(writes) == 1
