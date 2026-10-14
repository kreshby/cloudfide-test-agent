"""Sanitized deterministic GitHub responses, including a divergent base commit.

Only data is returned by this fake transport. Contents are never installed as code.
The same fixture generates the external handoff sample during review preparation.
"""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from automation.test_agent.context import collect_context
from automation.test_agent.github import GitHubReader, PAGE_SIZE, Response


REPOSITORY = "example/taskly"
BASE = "a" * 40
MERGE_BASE = "b" * 40
HEAD = "c" * 40
POLICY = "d" * 40
RUN = 42
WORKFLOW = 7
FRONTEND = "frontend/src/features/todos/components/TodoItem.tsx"
FRONTEND_TEST = "frontend/src/features/todos/components/TodoItem.test.tsx"
BACKEND = "backend/app/features/todos/router.py"
BACKEND_TEST = "backend/tests/features/todos/test_router.py"
FRONTEND_TEXT = "export const TodoItem = () => 'updated task'\n"
BACKEND_TEXT = "def route_value():\n    return 'updated task'\n"


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def changed_file(path, data, status="modified", previous=None, patch=None):
    result = {"filename": path, "status": status, "sha": blob_sha(data),
              "additions": 1, "deletions": 1, "changes": 2,
              "patch": patch if patch is not None else "@@ -1 +1 @@\n-old\n+new"}
    if previous is not None:
        result["previous_filename"] = previous
    return result


class FixtureTransport:
    def __init__(self):
        self.calls = []
        self.repo = {"id": 101, "full_name": REPOSITORY}
        self.run = {"id": RUN, "workflow_id": WORKFLOW, "run_attempt": 1,
                    "head_sha": HEAD, "event": "pull_request",
                    "repository": deepcopy(self.repo), "head_repository": deepcopy(self.repo)}
        self.pr = {"number": 12, "state": "open", "changed_files": 2,
                   "base": {"sha": BASE, "repo": deepcopy(self.repo)},
                   "head": {"sha": HEAD, "repo": deepcopy(self.repo)}}
        self.associations = [deepcopy(self.pr)]
        self.head = {}
        self.previous = {}
        self.blobs = {}
        self.overrides = {}
        self.add(FRONTEND, FRONTEND_TEXT)
        self.add(BACKEND, BACKEND_TEXT)
        self.add(FRONTEND_TEST, "import { TodoItem } from './TodoItem'\n"
                 "import { todoFixture } from '../../../test/todoFixture'\n"
                 "it('renders', () => expect(TodoItem()).toBe('updated task'))\n")
        self.add(BACKEND_TEST, "from app.features.todos.router import route_value\n"
                 "def test_route():\n    assert route_value() == 'updated task'\n")
        self.add("frontend/src/other/Unrelated.test.ts", "it('other', () => expect(2).toBe(2))\n")
        self.add("backend/tests/core/test_unrelated.py", "def test_other():\n    assert 2 == 2\n")
        self.add("frontend/vite.config.ts", "export default { test: { environment: 'jsdom' } }\n")
        self.add("frontend/package.json", (Path(__file__).resolve().parents[2] / 'frontend/package.json').read_text())
        self.add("frontend/src/test/setup.ts", "import '@testing-library/jest-dom/vitest'\n")
        self.add("frontend/src/test/todoFixture.ts", "export const todoFixture = { id: 1, title: 'demo' }\n")
        self.add("backend/pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        self.add("backend/tests/conftest.py", "from tests.mocks import make_repository_mock\n")
        self.add("backend/tests/mocks.py", "def make_repository_mock():\n    return None\n")
        self.add("pytest.ini", "[pytest]\ntestpaths = backend/tests\n")
        self.add("backend/.env", "SANITIZED_SECRET_MUST_NOT_BE_FETCHED=yes\n")
        self.add(FRONTEND, "export const TodoItem = () => 'old task'\n", historical=True)
        self.add(BACKEND, "def route_value():\n    return 'old task'\n", historical=True)
        self.files = [changed_file(FRONTEND, FRONTEND_TEXT.encode()),
                      changed_file(BACKEND, BACKEND_TEXT.encode())]
        # Present only on the divergent base branch, never on the PR's side.
        self.base_only_file = "backend/app/unrelated_base_commit.py"

    def add(self, path, content, *, historical=False, mode="100644"):
        data = content.encode() if type(content) is str else content
        digest = blob_sha(data)
        self.blobs[digest] = {"sha": digest, "size": len(data), "encoding": "base64",
                              "content": base64.b64encode(data).decode()}
        (self.previous if historical else self.head)[path] = {
            "path": path, "type": "blob", "mode": mode, "sha": digest, "size": len(data)}
        return digest

    def page(self, items, query):
        page = int(dict(query)["page"])
        start = (page - 1) * PAGE_SIZE
        return self.response(items[start:start + PAGE_SIZE], start + PAGE_SIZE < len(items))

    @staticmethod
    def response(data, has_next=False):
        return Response(200, json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), has_next)

    def get(self, path, query=()):
        self.calls.append((path, query))
        if (path, query) in self.overrides:
            value = self.overrides[path, query]
            return value() if callable(value) else value
        prefix = "/repos/" + REPOSITORY
        assert path.startswith(prefix), "only expected repository endpoints are reachable"
        endpoint = path[len(prefix):]
        if endpoint == f"/actions/runs/{RUN}":
            return self.response(self.run)
        if endpoint == f"/commits/{HEAD}/pulls":
            return self.page(self.associations, query)
        if endpoint == "/pulls/12":
            return self.response(self.pr)
        if endpoint == "/pulls/12/files":
            return self.page(self.files, query)
        if endpoint in (f"/compare/{BASE}...{HEAD}", f"/compare/{MERGE_BASE}...{HEAD}"):
            base = endpoint.split("/")[-1].split("...")[0]
            return self.response({"base_commit": {"sha": base}, "merge_base_commit": {"sha": MERGE_BASE},
                                  "status": "diverged" if base == BASE else "ahead",
                                  "total_commits": 1, "commits": [{"sha": HEAD}], "files": self.files})
        if endpoint.startswith("/git/commits/"):
            commit = endpoint.split("/")[-1]
            assert commit in (HEAD, MERGE_BASE)
            return self.response({"sha": commit, "tree": {"sha": "e" * 40 if commit == HEAD else "f" * 40}})
        if endpoint.startswith("/git/trees/"):
            digest = endpoint.split("/")[-1]
            assert query == (("recursive", "1"),)
            inventory = self.head if digest == "e" * 40 else self.previous
            return self.response({"sha": digest, "tree": list(inventory.values()), "truncated": False})
        if endpoint.startswith("/git/blobs/"):
            return self.response(self.blobs[endpoint.split("/")[-1]])
        raise AssertionError("unexpected fixture read operation")

    def collect(self, **overrides):
        arguments = dict(run_id=RUN, workflow_id=WORKFLOW, expected_head_sha=HEAD, policy_sha=POLICY)
        arguments.update(overrides)
        return collect_context(GitHubReader(REPOSITORY, self), **arguments)

    def set_files(self, files):
        self.files = files
        self.pr["changed_files"] = len(files)
        self.associations = [deepcopy(self.pr)]


def sample_bytes():
    return FixtureTransport().collect().to_bytes()
