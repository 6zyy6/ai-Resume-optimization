export type TaskProgress = {
  status: string;
  error_code?: string | null;
};

export type ClaimEvidence = {
  bullet_id: string;
  start: number;
  end: number;
  fact_refs: string[];
};

export class TaskTerminalError extends Error {
  constructor(
    public readonly taskId: string,
    public readonly status: string,
    public readonly errorCode?: string | null,
  ) {
    super(errorCode ?? `Task ${taskId} ${status}`);
    this.name = "TaskTerminalError";
  }
}

export function claimEvidenceForText(bulletId: string, text: string, factId: string): ClaimEvidence[] {
  return text ? [{ bullet_id: bulletId, start: 0, end: text.length, fact_refs: [factId] }] : [];
}

export async function waitForTask<T extends TaskProgress>(
  readTask: () => Promise<T>,
  taskId: string,
  { intervalMs = 500, timeoutMs = 120_000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() <= deadline) {
    const task = await readTask();
    if (task.status === "succeeded") return task;
    if (task.status === "failed" || task.status === "cancelled") {
      throw new TaskTerminalError(taskId, task.status, task.error_code);
    }
    if (intervalMs) await new Promise<void>((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`Task ${taskId} timed out`);
}
