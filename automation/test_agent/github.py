"""Small, bounded GitHub reader. No writes, credential discovery, or URL inputs.

Transport injection is a trusted host capability, never repository/model data.
Each reader has a lifetime request/response budget; use a fresh reader per bundle.
"""

import base64
from dataclasses import dataclass
import hashlib
import http.client
import json
import re
import ssl
from typing import Protocol
from urllib.parse import urlencode


API_HOST = "api.github.com"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESPONSE_TOTAL_BYTES = 8 * 1024 * 1024
MAX_REQUESTS = 160
MAX_PAGES = 4
PAGE_SIZE = 30
MAX_CHANGED_FILES = 100  # Below the compare API's 300-file ceiling.
MAX_ASSOCIATED_PRS = 100
MAX_TREE_ENTRIES = 4000
MAX_COMPARE_COMMITS = 250


class CollectionError(ValueError):
    """Bounded host rule description; never echo remote content or credentials."""


def object_value(value):
    if type(value) is not dict:
        raise CollectionError("malformed metadata object")
    return value


def integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CollectionError("invalid metadata integer")
    return value


def sha(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise CollectionError("invalid exact Git SHA")
    return value


def repository(value):
    if type(value) is not str or len(value) > 200:
        raise CollectionError("invalid repository identity")
    if re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", value) is None:
        raise CollectionError("invalid repository identity")
    if value.split("/")[1] in (".", ".."):
        raise CollectionError("invalid repository identity")
    return value


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    has_next: bool = False


class ReadTransport(Protocol):
    def get(self, path: str, query: tuple[tuple[str, str], ...]) -> Response: ...


# Even the concrete transport accepts only the adapter's fixed endpoint grammar.
_ENDPOINT = re.compile(
    r"/repos/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/(?:"
    r"actions/runs/[1-9][0-9]*|pulls/[1-9][0-9]*(?:/files)?|"
    r"commits/[0-9a-f]{40}/pulls|compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}|"
    r"git/(?:commits|trees|blobs)/[0-9a-f]{40})"
)


class GitHubHTTPTransport:
    """HTTPS GET to one fixed host; redirects/proxies/remote URLs are not used."""

    def __init__(self, token: str | None = None):
        if token is not None and (type(token) is not str or not 1 <= len(token) <= 4096
                                  or re.fullmatch(r"[!-~]+", token) is None):
            raise CollectionError("invalid injected authentication")
        self._token = token

    def get(self, path, query=()):
        if type(path) is not str or _ENDPOINT.fullmatch(path) is None:
            raise CollectionError("unsupported GitHub endpoint")
        repository("/".join(path.split("/")[2:4]))
        # Only integer pagination and recursive-tree switches; no URL-bearing query.
        if type(query) is not tuple or len(query) > 2:
            raise CollectionError("unsupported GitHub query")
        seen = set()
        for pair in query:
            if type(pair) is not tuple or len(pair) != 2:
                raise CollectionError("unsupported GitHub query")
            key, value = pair
            if (type(key) is not str or key not in {"page", "per_page", "recursive"}
                    or key in seen or type(value) is not str
                    or re.fullmatch(r"[1-9][0-9]{0,2}", value) is None):
                raise CollectionError("unsupported GitHub query")
            seen.add(key)
            if int(value) > {"page": MAX_PAGES, "per_page": PAGE_SIZE, "recursive": 1}[key]:
                raise CollectionError("GitHub query exceeds limit")
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trusted-t03-context"}
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        connection = http.client.HTTPSConnection(
            API_HOST, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
        try:
            connection.request("GET", path + ("?" + urlencode(query) if query else ""),
                               headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                # Do not read error bodies or follow Location, including same-host redirects.
                raise CollectionError("GitHub read failed")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise CollectionError("unsupported response encoding")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise CollectionError("GitHub response exceeds limit")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise CollectionError("GitHub response exceeds limit")
            # Link destinations are DATA. Only a next-page indication is consumed.
            link = response.getheader("Link", "")
            if len(link) > 8192:
                raise CollectionError("GitHub pagination header exceeds limit")
            has_next = re.search(r';\s*rel="next"(?:\s*,|\s*$)', link) is not None
            return Response(200, body, has_next)
        except CollectionError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise CollectionError("GitHub transport unavailable") from None
        finally:
            connection.close()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CollectionError("duplicate GitHub JSON field")
        result[key] = value
    return result


class GitHubReader:
    """Expose only T03 reads. Paths are constructed from validated identifiers."""

    def __init__(self, expected_repository: str, transport: ReadTransport):
        self.repository = repository(expected_repository)
        self._prefix = "/repos/" + self.repository
        self._transport = transport
        self.requests = 0
        self.response_bytes = 0

    def _json(self, endpoint, query=()):
        if self.requests >= MAX_REQUESTS:
            raise CollectionError("GitHub request budget exhausted")
        self.requests += 1
        try:
            response = self._transport.get(self._prefix + endpoint, query)
        except Exception:
            # Injected transports must not leak credential-bearing exception text/chains.
            raise CollectionError("GitHub transport failed") from None
        if (type(response) is not Response or type(response.status) is not int
                or response.status != 200 or type(response.body) is not bytes
                or type(response.has_next) is not bool):
            raise CollectionError("invalid GitHub response")
        self.response_bytes += len(response.body)
        if (len(response.body) > MAX_RESPONSE_BYTES
                or self.response_bytes > MAX_RESPONSE_TOTAL_BYTES):
            raise CollectionError("GitHub response budget exhausted")
        try:
            value = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique)
        except (ValueError, UnicodeError, RecursionError):
            raise CollectionError("invalid GitHub JSON") from None
        return value, response.has_next

    def _object(self, endpoint, query=()):
        value, has_next = self._json(endpoint, query)
        if has_next:
            raise CollectionError("unexpected metadata pagination")
        return object_value(value)

    def _pages(self, endpoint, maximum):
        result = []
        for page in range(1, MAX_PAGES + 1):
            value, has_next = self._json(
                endpoint, (("per_page", str(PAGE_SIZE)), ("page", str(page))))
            if type(value) is not list or len(value) > PAGE_SIZE or (has_next and not value):
                raise CollectionError("malformed GitHub page")
            result.extend(value)
            if len(result) > maximum:
                raise CollectionError("GitHub file/candidate budget exhausted")
            if not has_next:
                return result
        raise CollectionError("GitHub page budget exhausted")

    def workflow_run(self, run_id):
        return self._object(f"/actions/runs/{integer(run_id, 1)}")

    def pull_request(self, number):
        return self._object(f"/pulls/{integer(number, 1)}")

    def associated_prs(self, head_sha):
        return self._pages(f"/commits/{sha(head_sha)}/pulls", MAX_ASSOCIATED_PRS)

    def changed_files(self, number):
        return self._pages(f"/pulls/{integer(number, 1)}/files", MAX_CHANGED_FILES)

    def compare(self, base_sha, head_sha):
        return self._object(f"/compare/{sha(base_sha)}...{sha(head_sha)}")

    def tree_at(self, commit_sha):
        commit = self._object(f"/git/commits/{sha(commit_sha)}")
        if sha(commit.get("sha")) != commit_sha:
            raise CollectionError("commit identity mismatch")
        tree_sha = sha(object_value(commit.get("tree")).get("sha"))
        tree = self._object(f"/git/trees/{tree_sha}", (("recursive", "1"),))
        if sha(tree.get("sha")) != tree_sha or tree.get("truncated") is not False:
            raise CollectionError("incomplete or mismatched Git tree")
        entries = tree.get("tree")
        if type(entries) is not list or len(entries) > MAX_TREE_ENTRIES:
            raise CollectionError("Git tree inventory exceeds limit")
        return entries

    def blob(self, blob_sha, expected_size, maximum):
        sha(blob_sha)
        integer(expected_size, 0, maximum)
        obj = self._object(f"/git/blobs/{blob_sha}")
        if (sha(obj.get("sha")) != blob_sha or obj.get("encoding") != "base64"
                or integer(obj.get("size"), 0, maximum) != expected_size):
            raise CollectionError("blob metadata mismatch")
        encoded = obj.get("content")
        if type(encoded) is not str or len(encoded) > ((maximum + 2) // 3) * 4 + maximum // 20 + 8:
            raise CollectionError("blob encoding exceeds limit")
        try:
            content = base64.b64decode(encoded.replace("\n", ""), validate=True)
        except (ValueError, UnicodeError):
            raise CollectionError("invalid blob encoding") from None
        digest = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
        if len(content) != expected_size or digest != blob_sha:
            raise CollectionError("blob content identity mismatch")
        return content
