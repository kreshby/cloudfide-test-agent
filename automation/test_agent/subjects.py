"""Host source selection from captured head evidence, never test-name inference."""

import posixpath

from .contracts import Target
from .policy import PolicyError, normalized_path, test_path


def required_subject_import(accepted_test_path, selected_source_path):
    """Derive an extensionless direct import from canonical host-bound paths.

    Validate before POSIX relative-path calculation: normalization must never
    turn an escaping or ambiguous input into an allowed repository path.
    """
    test = test_path(accepted_test_path, Target.FRONTEND)
    source = normalized_path(selected_source_path)
    if not frontend_source(source) or posixpath.basename(source) in {".ts", ".tsx"}:
        raise PolicyError("invalid selected frontend source")
    module = source.rsplit(".", 1)[0]
    relative = posixpath.relpath(module, posixpath.dirname(test))
    return relative if relative.startswith("../") else "./" + relative


def frontend_source(path):
    normalized_path(path)
    parts = path.lower().split("/")
    return (path.startswith("frontend/src/") and path.endswith((".ts", ".tsx"))
            and not path.endswith((".d.ts", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
            and not any(p in {"test", "tests", "__tests__", "fixtures", "__fixtures__",
                              "node_modules", "automation", ".github", "trusted_skills"} for p in parts)
            and not any(p.startswith(("vite.config.", "vitest.config.", "tsconfig", "jest.config.",
                                      ".env")) or p.endswith(".config.ts") for p in parts))


def captured_frontend_subjects(bundle):
    changed = {f.path for f in bundle.changed_files if f.status != "removed"}
    excluded = {e.path for group in (bundle.configuration, bundle.fixtures, bundle.existing_tests)
                for e in group}
    return tuple(sorted({e.path for e in bundle.sources
                         if e.path in changed and e.path not in excluded and frontend_source(e.path)
                         and e.source_sha == bundle.source.head_sha and e.category == "source"
                         and e.state == "available" and e.content is not None
                         and len(e.content.encode("utf-8")) <= 4096}))
