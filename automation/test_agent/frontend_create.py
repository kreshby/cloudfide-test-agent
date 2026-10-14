"""Host-owned imports for frontend creates; no discovery, execution or resolver.

The small lexical subset deliberately rejects ambiguous literals. It is not a
TypeScript parser: the isolated compiler remains authoritative for syntax/types.
"""

import re

from .import_contract import frontend_import_contract
from .policy import PolicyError
from .subjects import captured_frontend_subjects


MAX_SYMBOLS = 64
MAX_NAME = 128
MAX_BODY_BYTES = 16 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_RESERVED = set("await break case catch class const continue debugger default delete do else enum "
                "export extends false finally for function if implements import in instanceof "
                "interface let new null package private protected public return static super "
                "switch this throw true try typeof var void while with yield eval arguments".split())


def _name(value):
    return (type(value) is str and len(value) <= MAX_NAME
            and _IDENTIFIER.fullmatch(value) is not None and value not in _RESERVED)


def _tokens(text, maximum, *, for_body=False):
    """Skip comments/quoted literals, retain delimiters and ASCII identifiers.

    Templates, escaped identifiers, regex/division and non-ASCII code are outside
    this conservative subset. Ordinary JSX closing tags are recognized only as
    opaque tokens. None of this text is executed or loaded as a module.
    """
    if (type(text) is not str or not text.strip() or len(text.encode()) > maximum
            or any(ord(c) < 32 and c not in '\n\r\t' for c in text)):
        raise PolicyError('unsupported frontend structured text')
    tokens, i = [], 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith('//', i):
            end = text.find('\n', i + 2)
            i = len(text) if end < 0 else end + 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                raise PolicyError('unterminated frontend comment')
            i = end + 2
        elif c in "'\"":
            quote = c
            start = i
            i += 1
            while i < len(text) and text[i] != quote:
                if text[i] in '\n\r':
                    raise PolicyError('unsupported frontend literal')
                if text[i] == '\\':
                    i += 1
                i += 1
            if i >= len(text):
                raise PolicyError('unterminated frontend literal')
            i += 1
            if for_body and (text[start + 1:i - 1].startswith(('./', '../'))
                    or re.search(r'\b(?:import|require|eval|Function)\b|\\(?:u|x|[0-7])', text[start:i])):
                # Computed loader properties and encoded loader strings must not
                # conceal the operations rejected below. Ordinary assertion
                # strings remain data; this deliberately conservative check
                # does not attempt JavaScript constant folding. Relative module
                # literals are excluded even for package-provided mock loaders.
                raise PolicyError('test body loader literal rejected')
            tokens.append('<literal>')
        elif text.startswith('</', i) and (closing := re.match(r'</[A-Za-z][A-Za-z0-9.]*\s*>', text[i:])):
            tokens.append('<jsx-close>')
            i += len(closing.group())
        elif text.startswith('/>', i):
            tokens.append('<jsx-self-close>')
            i += 2
        elif c in '`\\/' or ord(c) > 127:
            raise PolicyError('ambiguous frontend lexical form')
        elif match := _IDENTIFIER.match(text, i):
            tokens.append(match.group())
            i = match.end()
        else:
            tokens.append(c)
            i += 1
    return tokens


def direct_named_exports(content):
    tokens = _tokens(content, 4096)
    stack, names = [], set()
    pairs = {')': '(', ']': '[', '}': '{'}
    for i, token in enumerate(tokens):
        if token == '<' and not stack:
            # Top-level JSX text can contain declaration-looking words. Generic
            # top-level headers/comparisons are also outside this narrow subset.
            raise PolicyError('ambiguous top-level frontend syntax')
        if token == 'export' and not stack and (i == 0 or tokens[i - 1] != '.'):
            tail = tokens[i + 1:i + 5]
            # Unsupported export lists, defaults, modifiers and aliases supply
            # no authority. Only the first simple variable binding is admitted.
            if len(tail) >= 3 and _name(tail[1]):
                kind, name, following = tail[:3]
                supported = (kind in {'const', 'let'} and following in {'=', ':'}
                    or kind == 'function' and following in {'(', '<'}
                    or kind in {'class', 'interface'} and following in {'{', '<', 'extends', 'implements'}
                    or kind == 'type' and following in {'=', '<'}
                    or kind == 'enum' and following == '{')
                if supported:
                    names.add(name)
        if token in {'(', '[', '{'}:
            stack.append(token)
        elif token in pairs:
            if not stack or stack.pop() != pairs[token]:
                raise PolicyError('unbalanced frontend source delimiters')
    if stack or not names or len(names) > MAX_SYMBOLS:
        raise PolicyError('no bounded supported frontend exports')
    return sorted(names)


def frontend_create_contract(bundle, path, selected_source):
    contract = frontend_import_contract(bundle, path, selected_source)
    entries = [e for e in bundle.sources if e.path == selected_source
               and e.source_sha == bundle.source.head_sha]
    if (selected_source not in captured_frontend_subjects(bundle) or len(entries) != 1
            or entries[0].category != 'source' or entries[0].state != 'available'
            or entries[0].content is None):
        raise PolicyError('frontend create subject evidence unavailable')
    return {**contract, 'available_subject_exports': direct_named_exports(entries[0].content)}


def _names(value, allowed=None):
    if (type(value) is not list or not 1 <= len(value) <= MAX_SYMBOLS
            or any(not _name(n) for n in value) or len(set(value)) != len(value)):
        raise PolicyError('invalid structured import names')
    if allowed is not None and any(n not in allowed for n in value):
        raise PolicyError('subject export outside captured allowlist')
    return sorted(value)


def assemble_frontend_create(value, contract):
    """Independently validate every model field, then emit deterministic bytes."""
    if type(value) is not dict or set(value) != {'subject_imports', 'package_imports', 'test_body'}:
        raise PolicyError('frontend create fields rejected')
    subjects = _names(value['subject_imports'], contract['available_subject_exports'])
    packages = value['package_imports']
    if type(packages) is not list or len(packages) > 64:
        raise PolicyError('structured package import bound')
    imports, locals_used = {}, set(subjects)
    for item in packages:
        if type(item) is not dict or set(item) != {'module', 'names'}:
            raise PolicyError('structured package fields rejected')
        module = item['module']
        if type(module) is not str or module not in contract['allowed_package_roots']:
            raise PolicyError('package outside captured allowlist')
        names = _names(item['names'])
        if module in imports or locals_used.intersection(names):
            raise PolicyError('duplicate structured import binding')
        imports[module] = names
        locals_used.update(names)
    body = value['test_body']
    tokens = _tokens(body, MAX_BODY_BYTES, for_body=True)
    if any(t in {'import', 'export', 'require', 'eval', 'Function'} for t in tokens) or '```' in body:
        raise PolicyError('test body import bypass rejected')
    lines = ["import { " + ', '.join(subjects) + " } from '" + contract['required_subject_import'] + "'"]
    lines += ["import { " + ', '.join(imports[module]) + " } from '" + module + "'" for module in sorted(imports)]
    return '\n'.join(lines) + '\n\n' + body.strip() + '\n'
