"""Narrow sticky-comment adapter. No live caller or workflow is installed here."""

from dataclasses import dataclass
from enum import StrEnum
import http.client
import re
import socket
import ssl
import threading
import time
from typing import Protocol
from urllib.parse import urlencode

from .github import CollectionError, Response, integer, repository, sha
from .provider import canonical, strict_json
from .reporting import (MARKER, MARKER_FAMILY, MAX_COMMENT_BYTES, PatchDelivery, PreparedReport,
                        PublicationIdentity, ReportingError, parse_marker)


API_HOST = "api.github.com"
TIMEOUT_SECONDS = 10
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESPONSE_TOTAL_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 7  # Two PR reads, at most four pages, one write.
MAX_PAGES = 4
PAGE_SIZE = 30
MAX_COMMENTS = 100
MAX_REMOTE_COMMENT_BYTES = 64 * 1024


class PublicationError(ValueError):
    """Fixed errors, never token values, remote bodies or transport exceptions."""

    def __init__(self, message, *, phase="publisher-report-unavailable"):
        super().__init__(message)
        self.phase = phase


def _phase(category, operation, *args):
    try:
        return operation(*args)
    except PublicationError as error:
        raise PublicationError(str(error), phase=category) from None
    except Exception:
        raise PublicationError(category, phase=category) from None


def _integer(value):
    try:
        return integer(value, 1)
    except CollectionError:
        raise PublicationError("invalid publisher integer") from None


@dataclass(frozen=True)
class PublisherAuthor:
    id: int
    login: str
    type: str = "Bot"

    def __post_init__(self):
        _integer(self.id)
        if (type(self.login) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,100}(?:\[bot\])?", self.login) is None
                or type(self.type) is not str or self.type not in {"Bot", "User"}):
            raise PublicationError("invalid expected publisher author")


@dataclass(frozen=True)
class PullRequestSnapshot:
    repository: str
    repository_id: int
    number: int
    state: str
    base_sha: str
    head_sha: str
    head_repository: str | None = None
    head_repository_id: int | None = None


@dataclass(frozen=True)
class IssueComment:
    id: int
    author: PublisherAuthor
    body: str


class PublisherTransport(Protocol):
    def request(self, method: str, path: str, query: tuple, body: bytes) -> Response: ...


def _operation(repo, method, path, query, body):
    """Reject unsupported operations at the concrete transport as well as adapter."""
    if type(method) is not str or type(path) is not str or type(body) is not bytes:
        raise PublicationError("unsupported publisher operation")
    prefix = "/repos/" + repo
    tail = path[len(prefix):] if path.startswith(prefix + "/") else ""
    is_pr = re.fullmatch(r"/pulls/[1-9][0-9]{0,18}", tail) is not None
    is_list = re.fullmatch(r"/issues/[1-9][0-9]{0,18}/comments", tail) is not None
    is_comment = re.fullmatch(r"/issues/comments/[1-9][0-9]{0,18}", tail) is not None
    if not ((method == "GET" and (is_pr or is_list))
            or (method == "POST" and is_list) or (method == "PATCH" and is_comment)):
        raise PublicationError("unsupported publisher endpoint or method")
    _integer(int(tail.split("/")[-2] if is_list else tail.split("/")[-1]))
    if method == "GET" and is_list:
        if (type(query) is not tuple or len(query) != 2
                or any(type(pair) is not tuple or len(pair) != 2 for pair in query)
                or tuple(key for key, _ in query) != ("per_page", "page")
                or query[0][1] != str(PAGE_SIZE) or type(query[1][1]) is not str
                or query[1][1] not in {str(i) for i in range(1, MAX_PAGES + 1)}):
            raise PublicationError("unsupported publisher pagination")
    elif query != ():
        raise PublicationError("unsupported publisher query")
    if len(body) > MAX_REQUEST_BYTES or (method == "GET" and body):
        raise PublicationError("publisher request budget exhausted")
    if method != "GET":
        try:
            value = strict_json(body.decode(), MAX_REQUEST_BYTES)
            if type(value) is not dict or set(value) != {"body"} or canonical(value) != body:
                raise ValueError()
            _comment_body(value["body"], incoming=True)
            identity = parse_marker(value["body"])
            if identity.repository != repo or (method == "POST" and identity.pr_number != int(tail.split("/")[-2])):
                raise ValueError()
        except Exception:
            raise PublicationError("invalid publisher write body") from None
    return 201 if method == "POST" else 200


def _comment_body(body, *, incoming=False):
    if type(body) is not str:
        raise PublicationError("invalid comment body")
    try:
        size = len(body.encode())
    except UnicodeError:
        raise PublicationError("invalid comment encoding") from None
    if size > (MAX_COMMENT_BYTES if incoming else MAX_REMOTE_COMMENT_BYTES):
        raise PublicationError("comment byte limit exceeded")
    if incoming:
        try:
            parse_marker(body)
        except ReportingError:
            raise PublicationError("invalid incoming sticky marker") from None


class GitHubPublisherHTTPTransport:
    """Injected auth, fixed HTTPS host/repository, deadline, no redirects/proxies."""

    def __init__(self, expected_repository, token=None):
        try:
            self.repository = repository(expected_repository)
        except CollectionError:
            raise PublicationError("invalid publisher repository") from None
        if token is not None and (type(token) is not str or not 1 <= len(token) <= 4096
                                  or re.fullmatch(r"[!-~]+", token) is None):
            raise PublicationError("invalid injected publisher authentication")
        self._token = token

    def request(self, method, path, query=(), body=b""):
        expected = _operation(self.repository, method, path, query, body)
        headers = {"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trusted-t06-publisher"}
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        connection = http.client.HTTPSConnection(API_HOST, timeout=TIMEOUT_SECONDS,
                                                 context=ssl.create_default_context())
        deadline = time.monotonic() + TIMEOUT_SECONDS
        expired = threading.Event()
        timer = None

        def remaining():
            seconds = deadline - time.monotonic()
            if expired.is_set() or seconds <= 0:
                raise PublicationError("publisher request deadline exceeded")
            return seconds

        try:
            connection.connect()
            sock = connection.sock
            # Retain the actual socket: HTTPConnection may detach it for a
            # Connection: close response while its file is still being read.
            def expire():
                expired.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                    sock.close()
                except OSError:
                    pass
            timer = threading.Timer(remaining(), expire)
            timer.daemon = True
            timer.start()
            sock.settimeout(remaining())
            connection.request(method, path + ("?" + urlencode(query) if query else ""),
                               body=body or None, headers=headers)
            sock.settimeout(remaining())
            response = connection.getresponse()
            if response.status == 404 and method == "GET" and "/pulls/" in path:
                return Response(404, b"")
            if response.status != expected:
                raise PublicationError("GitHub publisher request failed")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise PublicationError("unsupported publisher response encoding")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or len(length) > 8 or int(length) > MAX_RESPONSE_BYTES):
                raise PublicationError("publisher response byte limit exceeded")
            data = bytearray()
            while True:
                # HTTPResponse closes its file after the last Content-Length
                # byte (or terminal chunk). With Connection: close that also
                # closes the captured socket; do not touch it again at EOF.
                if response.isclosed():
                    break
                sock.settimeout(remaining())
                chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise PublicationError("publisher response byte limit exceeded")
            link = response.getheader("Link", "")
            remaining()
            if length is not None and len(data) != int(length):
                raise PublicationError("publisher response length mismatch")
            if len(data) > MAX_RESPONSE_BYTES or len(link) > 8192:
                raise PublicationError("publisher response byte limit exceeded")
            return Response(response.status, bytes(data), re.search(r';\s*rel="next"(?:\s*,|\s*$)', link) is not None)
        except PublicationError:
            raise
        except Exception:
            raise PublicationError("GitHub publisher transport unavailable") from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()


class GitHubPublisherAdapter:
    """One publication's bounded operations; never a generic URL/method client."""

    def __init__(self, expected_repository, transport: PublisherTransport):
        try:
            self.repository = repository(expected_repository)
        except CollectionError:
            raise PublicationError("invalid publisher repository") from None
        self._prefix, self._transport = "/repos/" + self.repository, transport
        self.requests = self.response_bytes = 0

    def _json(self, method, endpoint, query=(), body=b""):
        expected = _operation(self.repository, method, self._prefix + endpoint, query, body)
        if self.requests >= MAX_REQUESTS:
            raise PublicationError("publisher request budget exhausted")
        self.requests += 1
        try:
            response = self._transport.request(method, self._prefix + endpoint, query, body)
        except Exception:
            raise PublicationError("publisher transport failed") from None
        if (type(response) is not Response or type(response.status) is not int
                or type(response.body) is not bytes or type(response.has_next) is not bool):
            raise PublicationError("invalid publisher response")
        self.response_bytes += len(response.body)
        if len(response.body) > MAX_RESPONSE_BYTES or self.response_bytes > MAX_RESPONSE_TOTAL_BYTES:
            raise PublicationError("publisher response budget exhausted")
        if response.status == 404 and method == "GET" and endpoint.startswith("/pulls/") and not response.has_next:
            return None, False
        if response.status != expected:
            raise PublicationError("publisher request failed")
        try:
            return strict_json(response.body.decode(), MAX_RESPONSE_BYTES), response.has_next
        except Exception:
            raise PublicationError("invalid publisher JSON") from None

    def pull_request(self, number):
        number = _integer(number)
        obj, paginated = self._json("GET", f"/pulls/{number}")
        if obj is None:
            return None
        try:
            if paginated or type(obj) is not dict or obj["number"] != number or obj["state"] not in {"open", "closed"}:
                raise ValueError()
            integer(obj["number"], 1)
            base, head = obj["base"], obj["head"]
            if repository(base["repo"]["full_name"]) != self.repository:
                raise ValueError()
            integer(base["repo"]["id"], 1)
            repository(head["repo"]["full_name"])
            integer(head["repo"]["id"], 1)
            return PullRequestSnapshot(self.repository, base["repo"]["id"], number,
                                       obj["state"], sha(base["sha"]), sha(head["sha"]),
                                       head["repo"]["full_name"], head["repo"]["id"])
        except Exception:
            raise PublicationError("invalid publisher PR identity") from None

    def comments(self, number):
        number = _integer(number)
        found, ids = [], set()
        for page in range(1, MAX_PAGES + 1):
            value, more = self._json("GET", f"/issues/{number}/comments",
                                     (("per_page", str(PAGE_SIZE)), ("page", str(page))))
            if type(value) is not list or len(value) > PAGE_SIZE or (more and not value):
                raise PublicationError("invalid publisher comment page")
            for item in value:
                comment = self._comment(item)
                if comment.id in ids:
                    raise PublicationError("duplicate comment identity")
                ids.add(comment.id)
                found.append(comment)
            if len(found) > MAX_COMMENTS:
                raise PublicationError("publisher comment budget exhausted")
            if not more:
                return tuple(found)
        raise PublicationError("publisher page budget exhausted")

    @staticmethod
    def _comment(obj):
        try:
            author = obj["user"]
            result = IssueComment(_integer(obj["id"]), PublisherAuthor(author["id"], author["login"], author["type"]), obj["body"])
            _comment_body(result.body)
            return result
        except Exception:
            raise PublicationError("invalid publisher comment metadata") from None

    def create_comment(self, number, body):
        value, more = self._json("POST", f"/issues/{_integer(number)}/comments", body=canonical({"body": body}))
        if more:
            raise PublicationError("unexpected write pagination")
        return self._comment(value)

    def update_comment(self, comment_id, body):
        value, more = self._json("PATCH", f"/issues/comments/{_integer(comment_id)}", body=canonical({"body": body}))
        if more:
            raise PublicationError("unexpected write pagination")
        result = self._comment(value)
        if result.id != comment_id:
            raise PublicationError("write comment identity mismatch")
        return result


class PublicationState(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    IDEMPOTENT = "idempotent"
    STALE = "stale"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class PublicationResult:
    state: PublicationState
    comment_id: int | None = None


def _current(snapshot, identity):
    return (type(snapshot) is PullRequestSnapshot and snapshot.state == "open"
            and snapshot.repository == identity.repository and snapshot.repository_id == identity.repository_id
            and snapshot.number == identity.pr_number and snapshot.head_sha == identity.head_sha
            and snapshot.base_sha == identity.base_sha
            and (snapshot.head_repository or snapshot.repository) == identity.head_repository
            and (snapshot.head_repository_id or snapshot.repository_id) == identity.head_repository_id)


def publish(report, adapter: GitHubPublisherAdapter, author: PublisherAuthor):
    """Two PR reads bracket discovery. There is no delete or automatic retry."""
    if type(report) is not PreparedReport or type(author) is not PublisherAuthor:
        raise PublicationError("trusted report and publisher author required")
    author.__post_init__()
    _comment_body(report.body, incoming=True)
    if type(report.identity) is not PublicationIdentity or parse_marker(report.body) != report.identity:
        raise PublicationError("incoming publication identity mismatch")
    report.identity.__post_init__()
    if type(report.delivery) is not PatchDelivery:
        raise PublicationError("invalid report delivery")
    report.delivery.__post_init__()
    if adapter.repository != report.identity.repository:
        raise PublicationError("publisher repository mismatch")
    i = report.identity
    if not _current(_phase("publisher-read-unavailable", adapter.pull_request, i.pr_number), i):
        return PublicationResult(PublicationState.STALE)
    matching = []
    for comment in _phase("publisher-read-unavailable", adapter.comments, i.pr_number):
        if comment.author != author or (MARKER not in comment.body and MARKER_FAMILY + "v1" not in comment.body):
            continue
        try:
            old = parse_marker(comment.body)
        except ReportingError:
            raise PublicationError("malformed publisher sticky marker") from None
        if (old.repository, old.pr_number, old.repository_id) != (i.repository, i.pr_number, i.repository_id):
            raise PublicationError("sticky comment scope mismatch")
        matching.append((comment, old))
    if len(matching) > 1:
        raise PublicationError("duplicate publisher sticky comments")
    if matching:
        comment, old = matching[0]
        if old.order > i.order:
            return PublicationResult(PublicationState.SUPERSEDED, comment.id)
        if old.order == i.order:
            if old != i or comment.body != report.body:
                raise PublicationError("conflicting publication for same run and attempt")
            return PublicationResult(PublicationState.IDEMPOTENT, comment.id)
    # Immediately before the only POST/PATCH: never write after a source change.
    if not _current(_phase("publisher-read-unavailable", adapter.pull_request, i.pr_number), i):
        return PublicationResult(PublicationState.STALE)
    result = (_phase("publisher-write-unavailable", adapter.update_comment, matching[0][0].id, report.body) if matching
              else _phase("publisher-write-unavailable", adapter.create_comment, i.pr_number, report.body))
    if result.author != author or result.body != report.body:
        raise PublicationError("publisher write receipt mismatch", phase="publisher-write-unavailable")
    return PublicationResult(PublicationState.UPDATED if matching else PublicationState.CREATED, result.id)
