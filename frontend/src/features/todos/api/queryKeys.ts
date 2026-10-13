export const todoKeys = {
  all: ['todos'] as const,
  list: ['todos', 'list'] as const,
  priorityLists: ['todos', 'list', 'priority'] as const,
  create: ['todos', 'create'] as const,
  update: ['todos', 'update'] as const,
  delete: ['todos', 'delete'] as const,
}
