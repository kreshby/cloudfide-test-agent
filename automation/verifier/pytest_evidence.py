"""Image-owned collection hook for pinned pytest; no host execution of tests."""

import json
from pathlib import Path


def pytest_sessionfinish(session, exitstatus):
    files = {}
    for item in session.items:
        path = item.nodeid.split("::", 1)[0]
        files[path] = files.get(path, 0) + 1
    data = json.dumps({"collected": session.testscollected, "files": files},
                      sort_keys=True, separators=(",", ":")).encode()
    if len(data) > 131072:
        raise ValueError("collection evidence limit")
    Path("/reports/collection.json").write_bytes(data)
