"""Bounded import guidance derived exclusively from exact-head manifest evidence."""

import re

from .policy import PolicyError
from .provider import strict_json
from .subjects import required_subject_import


MAX_PACKAGE_ROOTS = 64
MAX_PACKAGE_BYTES = 2048
MAX_MANIFEST_BYTES = 16 * 1024
_ROOT = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")


def package_roots(content):
    try:
        value = strict_json(content, MAX_MANIFEST_BYTES)
        if type(value) is not dict:
            raise ValueError()
        roots = set()
        for key in ('dependencies', 'devDependencies', 'peerDependencies'):
            group = value.get(key, {})
            if type(group) is not dict:
                raise ValueError()
            for name, version in group.items():
                if (type(name) is not str or len(name) > 128 or _ROOT.fullmatch(name) is None
                        or type(version) is not str or not version):
                    raise ValueError()
                roots.add(name)
        if len(roots) > MAX_PACKAGE_ROOTS or sum(len(p.encode()) for p in roots) > MAX_PACKAGE_BYTES:
            raise ValueError()
        return sorted(roots)
    except (ValueError, TypeError, UnicodeError):
        raise PolicyError('invalid captured frontend package manifest') from None


def captured_package_roots(bundle):
    entries = [e for e in bundle.configuration if e.path == 'frontend/package.json']
    # Historical contexts without a captured manifest cannot authorize packages.
    if not entries:
        return []
    if (len(entries) != 1 or entries[0].source_sha != bundle.source.head_sha
            or entries[0].category != 'configuration' or entries[0].state != 'available'
            or entries[0].content is None):
        raise PolicyError('frontend package manifest evidence unavailable')
    return package_roots(entries[0].content)


def frontend_import_contract(bundle, path, selected_source):
    return {'test_path': path, 'selected_source_path': selected_source,
            'required_subject_import': required_subject_import(path, selected_source),
            'allowed_package_roots': captured_package_roots(bundle)}
