import { useQuery } from '@tanstack/react-query'
import { todoKeys } from '../api/queryKeys'
import { todosApi } from '../api/todos'
import type { Priority } from '../types'

export function useTodosQuery(priority?: Priority) {
  return useQuery({
    queryKey: priority ? [...todoKeys.priorityLists, priority] : todoKeys.list,
    queryFn: ({ signal }) => todosApi.list(signal, priority),
  })
}
