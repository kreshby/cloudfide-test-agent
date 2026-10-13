"""Immutable commands shared by the trusted host and image entrypoint."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Profile:
    stack: str
    kind: str
    command_id: str
    targeted: bool = False
    coverage: bool = False


PROFILES = MappingProxyType({
    "frontend-baseline-typecheck": Profile("frontend", "baseline", "tsc"),
    "frontend-baseline-suite": Profile("frontend", "baseline", "vitest"),
    "frontend-patched-typecheck": Profile("frontend", "patched", "tsc"),
    "frontend-patched-targeted": Profile("frontend", "patched", "vitest", targeted=True),
    "frontend-patched-suite": Profile("frontend", "patched", "vitest", coverage=True),
    "backend-baseline-suite": Profile("backend", "baseline", "pytest"),
    "backend-patched-targeted": Profile("backend", "patched", "pytest", targeted=True),
    "backend-patched-suite": Profile("backend", "patched", "pytest", coverage=True),
})
STACK_PROFILES = MappingProxyType({
    "frontend-v1": tuple(p for p in PROFILES if p.startswith("frontend-")),
    "backend-v1": tuple(p for p in PROFILES if p.startswith("backend-")),
})


def command(profile_id, paths=(), *, excluded_updates=False):
    """Only host-selected IDs and revalidated relative test paths reach here."""
    p = PROFILES[profile_id]
    if p.command_id == "tsc":
        return ("/usr/local/bin/node", "/opt/frontend/node_modules/typescript/bin/tsc",
                "--noEmit", "--project",
                "tsconfig.verifier.json" if excluded_updates else "tsconfig.json")
    if p.command_id == "vitest":
        argv = ("/usr/local/bin/node", "/opt/frontend/node_modules/vitest/vitest.mjs",
                "run", "--config", "vite.config.ts", "--configLoader=runner", "--reporter=json",
                "--outputFile=/reports/tests.json", "--maxWorkers=2")
        if p.coverage:
            argv += ("--coverage", "--coverage.reporter=json-summary",
                     "--coverage.reportsDirectory=/reports/coverage")
        # Vitest arguments are substring filters. Runner moves every other test
        # out of discovery, and host checks the exact report file inventory.
        return argv + tuple(paths)
    argv = ("/usr/local/bin/python3.12", "-I", "-m", "pytest",
            "-c", "pyproject.toml", "-p", "no:cacheprovider", "-p", "verifier_pytest",
            "--junitxml=/reports/tests.xml", "-o", "junit_family=xunit2")
    if p.coverage:
        argv += ("-p", "pytest_cov", "--cov=app", "--cov-branch",
                 "--cov-config=pyproject.toml", "--cov-report=json:/reports/coverage.json")
    return argv + tuple(paths)
