"""Real-socket regression of the exact LocalLlamaProvider production path."""

import http.client
import socket
import time

import pytest

from automation.test_agent.provider import LocalLlamaProvider, MAX_HTTP_BYTES, ProviderError
from automation.tests.t07b3_fixtures import WireAPI


def test_stdlib_final_length_read_closes_detached_socket():
    reader, writer = socket.socketpair()
    response = http.client.HTTPResponse(reader)
    try:
        writer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        response.begin()
        assert response.will_close
        reader.close()  # HTTPConnection.getresponse() does this, retaining response.fp.
        reader.settimeout(1)
        assert response.read1(4096) == b"{}"
        assert response.isclosed()
        with pytest.raises(OSError) as caught:
            reader.settimeout(1)
        assert caught.value.errno == 9
    finally:
        response.close()
        reader.close()
        writer.close()


@pytest.mark.parametrize("endpoint", ["/apply-template", "/tokenize", "/completion"])
@pytest.mark.parametrize("close,framing", [(True, "length"), (False, "length"), (True, "chunked"), (True, "eof")])
def test_real_provider_endpoint_eof(endpoint, close, framing):
    api = WireAPI(lambda *_: (200, {"ok": True}), close=close, framing=framing)
    with api.serve() as port:
        assert LocalLlamaProvider(port)._post(endpoint, {}, time.monotonic() + 1) == {"ok": True}
    assert [(m, p) for m, p, _ in api.operations] == [("POST", endpoint)]


@pytest.mark.parametrize("endpoint", ["/apply-template", "/tokenize", "/completion"])
@pytest.mark.parametrize("fault", ["truncated-length", "oversized", "timeout", "malformed"])
def test_real_provider_rejects_incomplete_or_unbounded_response(endpoint, fault):
    data = b"PRIVATE_REMOTE_RESPONSE" if fault == "malformed" else b"{}"
    if fault == "oversized":
        data = b"x" * (MAX_HTTP_BYTES + 1)
    api = WireAPI(lambda *_: (200, data), extra_length=int(fault == "truncated-length"),
                  delay=.15 if fault == "timeout" else 0)
    with api.serve() as port:
        with pytest.raises(ProviderError) as caught:
            LocalLlamaProvider(port)._post(endpoint, {}, time.monotonic() + (.03 if fault == "timeout" else 1))
    assert "PRIVATE_REMOTE_RESPONSE" not in str(caught.value)
    assert len(api.operations) == 1
