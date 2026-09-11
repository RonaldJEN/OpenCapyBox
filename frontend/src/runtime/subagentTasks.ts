import type { SubagentTask } from '../types';

const statusRank: Record<SubagentTask['status'], number> = {
  requested: 0, running: 1, completed: 2, failed: 2, cancelled: 2,
};

/** Identity is the edge, never a tool name, description, or list position. */
export function mergeSubagentTasks(
  preferred: SubagentTask[] | undefined,
  fallback: SubagentTask[] | undefined,
): SubagentTask[] | undefined {
  if (!preferred && !fallback) return undefined;
  const tasks = new Map((fallback || []).map((task) => [task.edge_id, task]));
  for (const task of preferred || []) {
    const previous = tasks.get(task.edge_id);
    if (!previous) {
      tasks.set(task.edge_id, task);
      continue;
    }
    // The graph snapshot may be newer than its Round's SSE watermark. A later
    // replayed envelope must not undo progress already observed in that graph.
    const previousCompleted = Date.parse(previous.completed_at || '');
    const incomingCompleted = Date.parse(task.completed_at || '');
    const keepProgress = statusRank[previous.status] > statusRank[task.status]
      || (statusRank[previous.status] === 2 && statusRank[task.status] === 2
        && Number.isFinite(previousCompleted)
        && (!Number.isFinite(incomingCompleted) || previousCompleted > incomingCompleted));
    tasks.set(task.edge_id, {
      ...previous,
      ...task,
      child_run_id: task.child_run_id ?? previous.child_run_id,
      started_at: task.started_at ?? previous.started_at,
      status: keepProgress ? previous.status : task.status,
      completed_at: keepProgress ? previous.completed_at : task.completed_at ?? previous.completed_at,
    });
  }
  return [...tasks.values()];
}

export function isSubagentTask(value: unknown): value is SubagentTask {
  if (!value || typeof value !== 'object') return false;
  const task = value as SubagentTask;
  return typeof task.edge_id === 'string' && Boolean(task.edge_id)
    && typeof task.parent_run_id === 'string' && Boolean(task.parent_run_id)
    && Object.prototype.hasOwnProperty.call(statusRank, task.status);
}
