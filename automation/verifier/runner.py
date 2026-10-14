"""Image-owned preparation; never imports application/tests into this process."""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys

# Python -I excludes the script directory; this is the immutable image path.
sys.path.insert(0, "/opt/verifier")
from profiles import PROFILES, command  # noqa: E402


def export(profile_id):
    """After Docker observes tool termination, quiesce all surviving test code.

    Docker archive-copy does not expose tmpfs files on the tested daemon. Read
    bounded reports inside the mount namespace using this immutable exporter.
    The only runnable processes during copying are this exporter and idle PID 1.
    """
    p = PROFILES[profile_id]
    own_pid = os.getpid()
    for _ in range(5):
        running = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal() or int(entry.name) in (1, own_pid):
                continue
            try:
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if state not in ("T", "t", "Z", "X"):
                    running.append(int(entry.name))
                    os.kill(int(entry.name), signal.SIGSTOP)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
        if not running:
            break
    else:
        raise ValueError("test processes did not quiesce")
    names = ["tests.json"] if p.stack == "frontend" else ["tests.xml", "collection.json"]
    if p.coverage:
        names.append("coverage/coverage-summary.json" if p.stack == "frontend" else "coverage.json")
    reports = {}
    for name in names:
        directory = os.open("/reports", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        try:
            parts = name.split("/")
            for part in parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = nxt
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072:
                raise ValueError("unsafe or oversized report")
            data = bytearray()
            while len(data) <= 131072:
                chunk = os.read(fd, min(65536, 131073 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 131072 or len(data) != info.st_size:
                raise ValueError("report size mismatch")
            reports[name] = base64.b64encode(data).decode("ascii")
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)
    sys.stdout.write(json.dumps({"quiesced": True, "reports": reports}, sort_keys=True, separators=(",", ":")))


def main():
    if sys.argv[1:] == ["idle"]:
        while True:
            signal.pause()
    if len(sys.argv) == 3 and sys.argv[1] == "export" and sys.argv[2] in PROFILES:
        export(sys.argv[2])
        return
    profile_id, *paths = sys.argv[1:]
    if profile_id not in PROFILES:
        raise ValueError("unknown fixed profile")
    p = PROFILES[profile_id]
    pattern = (r"src/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.test\.tsx?"
               if p.stack == "frontend" else
               r"tests/(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]+\.py")
    if len(paths) > 256 or len(set(paths)) != len(paths) or any(
        re.fullmatch(pattern, path) is None or any(part in (".", "..") or part.startswith("-")
                                                  for part in path.split("/")) for path in paths
    ):
        raise ValueError("invalid test path")
    if p.command_id not in {"tsc", "structural"} and not paths:
        raise ValueError("explicit nonempty test inventory required")
    shutil.copytree("/input", "/work/repo", symlinks=False)
    root = Path("/work/repo") / p.stack
    if p.stack == "frontend":
        (root / "node_modules").symlink_to("/opt/frontend/node_modules", target_is_directory=True)
        if p.command_id == "vitest":
            allowed = set(paths)
            for file in (root / "src").rglob("*"):
                if file.name.endswith((".test.ts", ".test.tsx")) and str(file.relative_to(root)) not in allowed:
                    file.unlink()
    os.chdir(root)
    # Explicit image-owned environment, no host/container ambient inheritance.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
           "TMPDIR": "/tmp", "LANG": "C.UTF-8", "CI": "true",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    argv = command(profile_id, paths, excluded_updates=(root / "tsconfig.verifier.json").exists())
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No source-controlled exception text is rendered as host control output.
        sys.exit(125)
