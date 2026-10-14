import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { todoFixture } from '../../../test/todoFixture'
import { todoKeys } from '../api/queryKeys'
import { todosApi } from '../api/todos'
import type { Todo, TodoInput } from '../types'
import { useCreateTodoMutation } from './useCreateTodoMutation'

vi.mock('../api/todos', () => ({ todosApi: { create: vi.fn() } }))

const input: TodoInput = {
  title: 'Create a task',
  description: 'Test the API boundary',
  priority: 'high',
  due_date: '2026-10-15',
  completed: false,
}
const savedTodo: Todo = { ...todoFixture, ...input }
const existingTodo: Todo = { ...todoFixture, id: 8, title: 'Existing task' }

let queryClient: QueryClient

beforeEach(() => {
  vi.clearAllMocks()
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  vi.mocked(todosApi.create).mockResolvedValue(savedTodo)
})

afterEach(() => queryClient.clear())

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

it('calls todosApi.create with the supplied input', async () => {
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })

  await act(async () => {
    await expect(result.current.mutateAsync(input)).resolves.toEqual(savedTodo)
  })

  expect(todosApi.create).toHaveBeenCalledExactlyOnceWith(input)
})

it('cancels an in-flight todo-list query and caches the saved todo on success', async () => {
  queryClient.setQueryData(todoKeys.list, [existingTodo])
  let listSignal: AbortSignal | undefined
  let resolveList!: (todos: Todo[]) => void
  const staleResponse = new Promise<Todo[]>((resolve) => {
    resolveList = resolve
  })
  const listQuery = queryClient.fetchQuery({
    queryKey: todoKeys.list,
    queryFn: ({ signal }) => {
      listSignal = signal
      return staleResponse
    },
  })
  expect(queryClient.getQueryState(todoKeys.list)?.fetchStatus).toBe('fetching')
  expect(listSignal?.aborted).toBe(false)
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })

  await act(async () => {
    await result.current.mutateAsync(input)
  })

  expect(listSignal?.aborted).toBe(true)
  expect(queryClient.getQueryState(todoKeys.list)?.fetchStatus).toBe('idle')
  resolveList([existingTodo])
  await staleResponse
  await listQuery
  expect(queryClient.getQueryData(todoKeys.list)).toEqual([
    savedTodo,
    existingTodo,
  ])
})

it('replaces a cached todo with the same id without leaving a duplicate', async () => {
  queryClient.setQueryData(todoKeys.list, [
    existingTodo,
    { ...savedTodo, title: 'Unconfirmed title' },
  ])
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })

  await act(async () => {
    await result.current.mutateAsync(input)
  })

  expect(queryClient.getQueryData(todoKeys.list)).toEqual([
    savedTodo,
    existingTodo,
  ])
})
