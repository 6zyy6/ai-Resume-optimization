import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "../..");
const python = join(root, ".venv/bin/python");

async function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        reject(new Error("failed to allocate local port"));
        return;
      }
      server.close(() => resolvePort(address.port));
    });
  });
}

async function waitForReady(url: string, process: ChildProcess): Promise<void> {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      throw new Error(`contract service exited before readiness: ${process.exitCode}`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // The real subprocess owns readiness; connection refusal is expected during startup.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 50));
  }
  throw new Error(`contract service readiness timed out: ${url}`);
}

async function stop(process: ChildProcess | undefined): Promise<void> {
  if (!process || process.exitCode !== null) return;
  process.kill("SIGTERM");
  await new Promise<void>((resolveClose) => {
    process.once("close", () => resolveClose());
    setTimeout(() => {
      if (process.exitCode === null) process.kill("SIGKILL");
      resolveClose();
    }, 2_000).unref();
  });
}

describe("AI orchestration V2.1 real HTTP contract", () => {
  let api: ChildProcess | undefined;
  let apiUrl = "";
  let databaseUrl = "";
  let directory = "";
  let pi: ChildProcess | undefined;
  let piUrl = "";
  const token = "contract-fixture-service-token";

  beforeAll(async () => {
    directory = await mkdtemp(join(tmpdir(), "ai-contract-v2-"));
    const build = spawnSync("pnpm", ["--filter", "@resume/ai", "build"], {
      cwd: root,
      encoding: "utf8",
    });
    expect(build.status, build.stderr || build.stdout).toBe(0);

    const piPort = await freePort();
    const apiPort = await freePort();
    piUrl = `http://127.0.0.1:${piPort}`;
    databaseUrl = `sqlite+aiosqlite:///${join(directory, "contract.db")}`;
    const migration = spawnSync(
      python,
      ["-m", "alembic", "-c", "packages/api/alembic.ini", "upgrade", "head"],
      {
        cwd: root,
        encoding: "utf8",
        env: { ...process.env, DATABASE_URL: databaseUrl },
      },
    );
    expect(migration.status, migration.stderr || migration.stdout).toBe(0);

    pi = spawn(process.execPath, ["tests/contract/fixtures/pi-server.mjs"], {
      cwd: root,
      env: {
        ...process.env,
        AI_PORT: String(piPort),
        AI_SERVICE_TOKEN: token,
        APP_COMMIT_SHA: "contract-source",
      },
      stdio: "ignore",
    });
    await waitForReady(`${piUrl}/internal/v1/health/ready`, pi);

    apiUrl = `http://127.0.0.1:${apiPort}`;
    api = spawn(python, [
      "-m",
      "uvicorn",
      "ai_orchestration_fastapi:app",
      "--app-dir",
      "tests/contract/fixtures",
      "--host",
      "127.0.0.1",
      "--port",
      String(apiPort),
    ], {
      cwd: root,
      env: {
        ...process.env,
        PYTHONPATH: join(root, "packages/api"),
        AI_INTERNAL_URL: piUrl,
        AI_SERVICE_TOKEN: token,
        CONTRACT_DATABASE_URL: databaseUrl,
      },
      stdio: "ignore",
    });
    await waitForReady(`${apiUrl}/v1/health/ready`, api);
  }, 20_000);

  afterAll(async () => {
    await stop(api);
    await stop(pi);
    if (directory) await rm(directory, { force: true, recursive: true });
  });

  it("drives all five workflows from public FastAPI operations through a real worker operation to TCP Pi", async () => {
    const email = `contract-${Date.now()}@example.com`;
    const startEmail = await fetch(`${apiUrl}/v1/auth/email/start`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email }),
    });
    expect(startEmail.status).toBe(202);
    const registration = await fetch(`${apiUrl}/v1/auth/password/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        email,
        code: "123456",
        password: "contract-password-2026",
        consents: [
          {
            document_type: "user_agreement",
            document_version: "2026-07-27",
            decision: "accepted",
          },
          {
            document_type: "privacy_policy",
            document_version: "2026-07-27",
            decision: "accepted",
          },
        ],
      }),
    });
    expect(registration.status).toBe(200);
    const ownerUserId = (await registration.json() as { user_id: string }).user_id;
    const cookie = registration.headers.get("set-cookie")?.split(";", 1)[0];
    expect(cookie).toMatch(/^session=/);
    const headers = {
      "content-type": "application/json",
      cookie: cookie!,
    };
    const runTask = (taskId: string) => {
      const worker = spawnSync(
        python,
        ["tests/contract/fixtures/run_worker.py", ownerUserId, taskId],
        {
          cwd: root,
          encoding: "utf8",
          env: {
            ...process.env,
            PYTHONPATH: join(root, "packages/api"),
            DATABASE_URL: databaseUrl,
            AI_INTERNAL_URL: piUrl,
            AI_SERVICE_TOKEN: token,
            STORAGE_BACKEND: "memory",
          },
        },
      );
      expect(worker.status, worker.stderr || worker.stdout).toBe(0);
      expect(JSON.parse(worker.stdout)).toMatchObject({ status: "succeeded" });
    };

    const intakeResponse = await fetch(`${apiUrl}/v1/intake-sessions`, {
      method: "POST",
      headers: { ...headers, "Idempotency-Key": "contract-intake-start" },
      body: JSON.stringify({ restart: false }),
    });
    expect(intakeResponse.status).toBe(201);
    const intake = await intakeResponse.json() as {
      current_question: { id: string };
      id: string;
      version: number;
    };
    const answerResponse = await fetch(
      `${apiUrl}/v1/intake-sessions/${intake.id}/answers`,
      {
        method: "POST",
        headers: { ...headers, "Idempotency-Key": "contract-intake-answer" },
        body: JSON.stringify({
          question_id: intake.current_question.id,
          answer: "负责用户调研。完成产品原型。",
          skipped: false,
          base_version: intake.version,
        }),
      },
    );
    expect(answerResponse.status).toBe(202);
    const answered = await answerResponse.json() as { analysis_task_id: string };
    expect(answered.analysis_task_id).toMatch(/^tsk_/);

    runTask(answered.analysis_task_id);

    const completed = await fetch(`${apiUrl}/v1/intake-sessions/${intake.id}`, {
      headers: { cookie: cookie! },
    });
    expect(completed.status).toBe(200);
    const completedIntake = await completed.json() as {
      analysis_status: string;
      fact_candidates: Array<{
        ai_run_id: string;
        id: string;
        status: string;
        value: string;
      }>;
      resume_id: string | null;
      version: number;
    };
    expect(completedIntake.analysis_status).toBe("waiting_for_confirmation");
    expect(completedIntake.fact_candidates).toHaveLength(2);
    expect(completedIntake.fact_candidates.every(({ status }) => status === "pending"))
      .toBe(true);

    const inspection = spawnSync(
      python,
      [
        "tests/contract/fixtures/assert_state.py",
        databaseUrl,
        ownerUserId,
        answered.analysis_task_id,
      ],
      {
        cwd: root,
        encoding: "utf8",
        env: { ...process.env, PYTHONPATH: join(root, "packages/api") },
      },
    );
    expect(inspection.status, inspection.stderr || inspection.stdout).toBe(0);
    const state = JSON.parse(inspection.stdout) as Record<string, unknown>;
    expect(state).toMatchObject({
      task_status: "succeeded",
      outbox_exists: true,
      outbox_dispatched: false,
      outbox_owner_matches: true,
      orphan_trace_count: 0,
    });
    expect(state.trace_count).toBeGreaterThan(0);

    let intakeVersion = completedIntake.version;
    for (const [index, candidate] of completedIntake.fact_candidates.entries()) {
      const decision = await fetch(
        `${apiUrl}/v1/intake-sessions/${intake.id}/fact-candidates/${candidate.id}/decision`,
        {
          method: "POST",
          headers: {
            ...headers,
            "Idempotency-Key": `contract-candidate-${index}`,
          },
          body: JSON.stringify({
            decision: "edit",
            value: `${candidate.value}（已确认）`,
            base_version: intakeVersion,
          }),
        },
      );
      const decisionBody = await decision.json() as {
        error?: { code: string };
        session_version?: number;
      };
      expect(decision.status, JSON.stringify(decisionBody)).toBe(200);
      intakeVersion = decisionBody.session_version!;
    }

    const draftResponse = await fetch(
      `${apiUrl}/v1/intake-sessions/${intake.id}/drafts`,
      {
        method: "POST",
        headers: { ...headers, "Idempotency-Key": "contract-draft" },
        body: JSON.stringify({
          base_version: intakeVersion,
          title: "AI 合同基础简历",
          generation_mode: "model",
        }),
      },
    );
    expect(draftResponse.status).toBe(202);
    const draftTaskId = (await draftResponse.json() as { task_id: string }).task_id;
    runTask(draftTaskId);
    const draftedIntakeResponse = await fetch(
      `${apiUrl}/v1/intake-sessions/${intake.id}`,
      { headers: { cookie: cookie! } },
    );
    const draftedIntake = await draftedIntakeResponse.json() as {
      resume_id: string;
      status: string;
    };
    expect(draftedIntake).toMatchObject({ status: "completed" });
    expect(draftedIntake.resume_id).toMatch(/^resume_/);
    const versionsResponse = await fetch(
      `${apiUrl}/v1/resumes/${draftedIntake.resume_id}/versions`,
      { headers: { cookie: cookie! } },
    );
    expect(versionsResponse.status).toBe(200);
    const versions = await versionsResponse.json() as {
      items: Array<{ generation_mode: string; id: string }>;
    };
    expect(versions.items[0].generation_mode).toBe("model");
    const resumeVersionId = versions.items[0].id;

    const jobResponse = await fetch(`${apiUrl}/v1/jobs`, {
      method: "POST",
      headers: { ...headers, "Idempotency-Key": "contract-job" },
      body: JSON.stringify({
        title: "产品实习生",
        company: "合同测试公司",
        raw: "负责用户调研\n熟悉产品原型",
      }),
    });
    expect(jobResponse.status).toBe(201);
    const jobId = (await jobResponse.json() as { id: string }).id;
    const parseResponse = await fetch(`${apiUrl}/v1/jobs/${jobId}/parse`, {
      method: "POST",
      headers: { ...headers, "Idempotency-Key": "contract-job-parse" },
    });
    expect(parseResponse.status).toBe(202);
    const parseTaskId = (await parseResponse.json() as { task_id: string }).task_id;
    runTask(parseTaskId);
    const parsedJobResponse = await fetch(`${apiUrl}/v1/jobs/${jobId}`, {
      headers: { cookie: cookie! },
    });
    const parsedJob = await parsedJobResponse.json() as {
      requirements: Array<{ confirmed: boolean; id: string }>;
      status: string;
    };
    expect(parsedJob.status).toBe("parsed");
    expect(parsedJob.requirements).toHaveLength(2);
    for (const [index, requirement] of parsedJob.requirements.entries()) {
      const confirmation = await fetch(
        `${apiUrl}/v1/jobs/${jobId}/requirements/${requirement.id}`,
        {
          method: "PATCH",
          headers: {
            ...headers,
            "Idempotency-Key": `contract-requirement-${index}`,
          },
          body: JSON.stringify({ confirmed: true }),
        },
      );
      expect(confirmation.status).toBe(200);
    }

    const matchResponse = await fetch(`${apiUrl}/v1/match-analyses`, {
      method: "POST",
      headers: { ...headers, "Idempotency-Key": "contract-match" },
      body: JSON.stringify({ resume_version_id: resumeVersionId, job_id: jobId }),
    });
    expect(matchResponse.status).toBe(202);
    const queuedMatch = await matchResponse.json() as { id: string; task_id: string };
    runTask(queuedMatch.task_id);
    const completedMatchResponse = await fetch(
      `${apiUrl}/v1/match-analyses/${queuedMatch.id}`,
      { headers: { cookie: cookie! } },
    );
    const completedMatch = await completedMatchResponse.json() as {
      items: Array<{ category: string }>;
      status: string;
    };
    expect(completedMatch.status).toBe("succeeded");
    expect(completedMatch.items).toHaveLength(2);
    expect(completedMatch.items.some(({ category }) => category === "underexpressed"))
      .toBe(true);
    const suggestionsResponse = await fetch(
      `${apiUrl}/v1/match-analyses/${queuedMatch.id}/suggestions`,
      { headers: { cookie: cookie! } },
    );
    expect(suggestionsResponse.status).toBe(200);
    const suggestions = await suggestionsResponse.json() as {
      items: Array<{ status: string }>;
    };
    expect(suggestions.items).toHaveLength(2);
    expect(suggestions.items[0].status).toBe("pending");
    expect(suggestions.items[1].status).toBe("blocked");

    const taskWorkflowExpectations = [
      [answered.analysis_task_id, ["analyze_intake_answer"]],
      [draftTaskId, ["compose_resume_draft"]],
      [parseTaskId, ["parse_jd"]],
      [queuedMatch.task_id, ["match_resume_to_jd", "generate_suggestions_batch"]],
    ] as const;
    for (const [taskId, expectedWorkflows] of taskWorkflowExpectations) {
      const result = spawnSync(
        python,
        ["tests/contract/fixtures/assert_state.py", databaseUrl, ownerUserId, taskId],
        {
          cwd: root,
          encoding: "utf8",
          env: { ...process.env, PYTHONPATH: join(root, "packages/api") },
        },
      );
      expect(result.status, result.stderr || result.stdout).toBe(0);
      const taskState = JSON.parse(result.stdout) as {
        outbox_dispatched: boolean;
        outbox_exists: boolean;
        outbox_owner_matches: boolean;
        orphan_trace_count: number;
        runs: Array<{
          input_hash: string;
          owner_user_id: string;
          prompt_template_version: string;
          receipt_hash: string;
          result_ref: string;
          status: string;
          task_id: string;
          trace_id: string;
          trace_sequence: number[];
          trace_types: string[];
          workflow_type: string;
          workflow_version: string;
        }>;
        task_event_sequences: number[];
        task_event_stages: string[];
        task_status: string;
        task_trace_id: string;
        trace_count: number;
      };
      expect(taskState.task_status).toBe("succeeded");
      expect(taskState.runs.map(({ workflow_type }) => workflow_type))
        .toEqual(expectedWorkflows);
      expect(taskState.trace_count).toBeGreaterThan(0);
      expect(taskState.outbox_exists).toBe(true);
      expect(taskState.outbox_dispatched).toBe(false);
      expect(taskState.outbox_owner_matches).toBe(true);
      expect(taskState.orphan_trace_count).toBe(0);
      expect(taskState.task_event_sequences).toEqual(
        taskState.task_event_sequences.map((_, index) => index + 1),
      );
      expect(taskState.task_event_stages.at(-1)).toBe("succeeded");
      for (const run of taskState.runs) {
        expect(run).toMatchObject({
          owner_user_id: ownerUserId,
          status: "succeeded",
          task_id: taskId,
          trace_id: taskState.task_trace_id,
          workflow_version: "2",
        });
        expect(run.prompt_template_version).toMatch(/@2$/);
        expect(run.input_hash).toMatch(/^[a-f0-9]{64}$/);
        expect(run.receipt_hash).toMatch(/^[a-f0-9]{64}$/);
        expect(run.result_ref).toBeTruthy();
        expect(run.trace_sequence).toEqual(
          run.trace_sequence.map((_, index) => index + 1),
        );
        expect(run.trace_types[0]).toBe("run_queued");
        expect(run.trace_types.at(-1)).toBe("run_succeeded");
      }
    }
  });
});
