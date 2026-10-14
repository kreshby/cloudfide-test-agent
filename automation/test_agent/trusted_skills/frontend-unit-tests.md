# frontend-unit-tests v1.1.0
Use Vitest and project conventions. Directly test the bound selected_source_path
using its captured current exports/signatures. Use only the exact permitted test
path, operation and language contract. New tests must statically import the
selected source through a relative path. RTL availability does not justify
drifting to a neighboring page/component.

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
React rendering requiring JSX belongs in .test.tsx. TSX permission never permits
changing the subject. Helpers/providers are allowed only when required by that
subject. Use existing mocks, isolated QueryClient when relevant; no real network.

Updates: preserve unrelated meaningful cases; add/update only behavior justified
by the changed selected source. Echo the supplied original SHA-256. Never weaken
expectations or add skip/only/todo/xfail, conditional or snapshot-only tests.
Return complete code as data; never execute it or claim it passed.
