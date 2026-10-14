# frontend-unit-tests v1.3.0
Use Vitest, exact host test_path/operation/language, allowed_package_roots and
captured exports/signatures. Prefer plain exported objects/functions for data/API;
indirect behavior tests are allowed. Use meaningful assertions; no real network.

CREATE (including CREATE repair): return only the structured response required by
the schema: subject_imports, package_imports, test_body plus binding and target. Do not return a complete file or write repository-local import declarations.
subject_imports must use available_subject_exports; package_imports must use
allowed_package_roots. The host constructs all imports and file bytes,
using exactly required_subject_import. test_body contains assertions/test code only:
no import/export/require/dynamic import/eval/Function. Use ordinary quoted strings,
no templates.

Illustrative buildKey/StatusBadge: replace with actual captured exports.
Pure .test.ts pattern, illustrative CREATE fields:
subject_imports: ["buildKey"]
package_imports: [{"module":"vitest","names":["describe","expect","it"]}]
test_body:
```ts
describe('buildKey', () => {
  it('includes the selected value', () => {
    expect(buildKey('high')).toEqual(['items', 'high'])
  })
})
```
For .test.ts, JSX is forbidden: no React rendering, fragments or provider wrappers.
React/RTL .test.tsx pattern (JSX permitted): request render/screen from allowed
@testing-library/react in package_imports; body may use
render(<StatusBadge status="done" />) and expect(screen.getByText('done')).toBeInTheDocument().
Use existing mocks and isolated QueryClient.

UPDATE only: return complete updated code as data under the existing UPDATE schema.
Resolve relative imports from test_path; direct selected_source_path imports
must use exactly required_subject_import. Preserve unrelated meaningful tests;
change only cases justified by changed behavior. Echo the exact original SHA-256.
Never weaken expectations or add skip/only/todo/xfail, conditional or snapshot-only
tests. Never execute code or claim it passed.
