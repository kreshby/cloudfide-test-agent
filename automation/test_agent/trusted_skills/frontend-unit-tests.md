# frontend-unit-tests v1.2.0
Use Vitest and the exact host test_path, operation and language contract.
Do not invent npm packages; use only allowed_package_roots. Relative imports must
resolve from test_path. A direct selected_source_path import must use exactly
required_subject_import. Use captured exports/signatures. Indirect behavior tests
are allowed. Prefer testing a plain exported object/function over hook machinery
when the selected source is plain data/API.

Example: some/path/queryKeys.test.ts importing some/path/queryKeys.ts uses
"./queryKeys", never "../queryKeys".

Illustrative identifiers below: replace them with actual captured subject exports;
never invent buildKey or StatusBadge in the real test.

Pure .test.ts pattern (TypeScript, not TSX):
```ts
import { describe, expect, it } from 'vitest'
import { buildKey } from './buildKey'
describe('buildKey', () => {
  it('includes the selected value', () => {
    expect(buildKey('high')).toEqual(['items', 'high'])
  })
})
```
For .test.ts, JSX is forbidden: never emit <Component />, JSX fragments or JSX
provider wrappers. Use pure TypeScript; no React rendering or component wrapper.

React/RTL .test.tsx pattern (JSX permitted):
```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusBadge } from './StatusBadge'
describe('StatusBadge', () => {
  it('renders the selected status', () => {
    render(<StatusBadge status="done" />)
    expect(screen.getByText('done')).toBeInTheDocument()
  })
})
```
React JSX belongs in .test.tsx. Use existing mocks and isolated QueryClient when
relevant; no real network.

Updates: preserve unrelated meaningful cases; add/update only behavior justified
by the changed selected source. Echo the supplied original SHA-256. Never weaken
expectations or add skip/only/todo/xfail, conditional or snapshot-only tests.
Return complete code as data; never execute it or claim it passed.
