import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

const enabled = process.env.AI_ORCHESTRATION_REAL_SERVICES === "1";
const root = process.cwd();
const evidenceRoot = resolve(
  root,
  process.env.AI_ORCHESTRATION_EVIDENCE_DIR
    ?? "test-results/ai-orchestration-v2/screenshots",
);
const viewports = [
  { width: 390, height: 844, label: "390x844" },
  { width: 1024, height: 768, label: "1024x768" },
  { width: 1440, height: 900, label: "1440x900" },
] as const;

test.skip(!enabled, "requires the isolated local real-service harness");
test.describe.configure({ mode: "serial" });

async function runWorker(page: Page, taskId: string, unavailable = false) {
  const response = await page.request.post(`/api/v1/testing/tasks/${taskId}/run`, {
    data: { ai_mode: unavailable ? "unavailable" : "fixture" },
  });
  expect(response.status()).toBe(200);
  return await response.json() as { error_code: string | null; status: string };
}

async function inspectTask(page: Page, taskId: string) {
  const response = await page.request.get(
    `/api/v1/testing/tasks/${taskId}/inspection`,
  );
  expect(response.status()).toBe(200);
  return await response.json() as Record<string, unknown>;
}

function ownerHash(ownerUserId: string) {
  return createHash("sha256").update(ownerUserId).digest("hex");
}

function redactOwnerIds(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value, (key, item) => (
    key === "owner_user_id" && typeof item === "string" ? ownerHash(item) : item
  )));
}

async function capture(page: Page, directory: string, name: string) {
  const metrics = await page.evaluate(() => ({
    client_width: document.documentElement.clientWidth,
    horizontal_overflow:
      document.documentElement.scrollWidth > document.documentElement.clientWidth,
    runtime_error: document.body.innerText.includes("Runtime Error"),
    scroll_width: document.documentElement.scrollWidth,
    visible_internal_markers: [
      "experience",
      "fact_candidate_edit",
      "proved",
      "underexpressed",
      "needs_confirmation",
      "real_gap",
      "analyze_intake_answer",
      "generate_intake_draft",
      "parse_job",
      "match_resume_to_job",
      "/sections/",
    ].filter((value) => document.body.innerText.includes(value)),
  }));
  expect(metrics.horizontal_overflow).toBe(false);
  expect(metrics.runtime_error).toBe(false);
  expect(metrics.visible_internal_markers).toEqual([]);
  await page.screenshot({
    fullPage: true,
    path: resolve(directory, `${name}.png`),
  });
  return metrics;
}

async function register(page: Page, nonce: string) {
  const email = `browser-${nonce}@example.com`;
  const started = await page.request.post("/api/v1/auth/email/start", {
    data: { email },
  });
  expect(started.status()).toBe(202);
  const registered = await page.request.post("/api/v1/auth/password/register", {
    data: {
      email,
      code: "123456",
      password: "browser-contract-password-2026",
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
    },
  });
  expect(registered.status()).toBe(200);
  return await registered.json() as { user_id: string };
}

for (const viewport of viewports) {
  test(`${viewport.label} captures nine AI orchestration states without API interception`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize(viewport);
    const directory = resolve(evidenceRoot, viewport.label);
    await mkdir(directory, { recursive: true });
    const nonce = `${viewport.width}-${Date.now()}`;
    const user = await register(page, nonce);
    const apiResponses: Array<Record<string, unknown>> = [];
    const pageErrors: string[] = [];
    const serverErrors: Array<{ status: number; url: string }> = [];
    const tasks: Array<{ owner_user_id: string; task_id: string }> = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith("/api/v1/") && response.status() >= 500) {
        serverErrors.push({ status: response.status(), url: url.pathname });
      }
    });

    await page.goto("/create");
    await expect(page.getByLabel("你的回答")).toBeVisible();
    await page.getByLabel("你的回答").fill("负责用户调研。完成产品原型。");
    const answerResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && /\/api\/v1\/intake-sessions\/[^/]+\/answers$/.test(new URL(response.url()).pathname)
    ));
    await page.getByRole("button", { name: "保存并继续" }).click();
    const answerResponse = await answerResponsePromise;
    const answered = await answerResponse.json() as {
      analysis_task_id: string;
      id: string;
    };
    apiResponses.push({ operation: "answer", status: answerResponse.status(), body: answered });
    tasks.push({ owner_user_id: user.user_id, task_id: answered.analysis_task_id });
    await expect(page.getByRole("heading", { name: "正在整理这段经历" })).toBeVisible();
    const captures = [{
      name: "01-create-analysis",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "01-create-analysis"),
    }];

    expect((await runWorker(page, answered.analysis_task_id)).status).toBe("succeeded");
    await expect(page.getByRole("heading", { name: "确认这段经历中的事实" }))
      .toBeVisible();
    captures.push({
      name: "02-candidate-confirmation",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "02-candidate-confirmation"),
    });

    for (let index = 0; index < 2; index += 1) {
      await page.getByRole("button", { name: "编辑候选 1", exact: true }).click();
      const field = page.locator("textarea[name^='candidate-']");
      await field.fill(`${await field.inputValue()}（已确认）`);
      await page.getByRole("button", { name: "保存编辑候选 1", exact: true }).click();
    }
    const draftResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && /\/api\/v1\/intake-sessions\/[^/]+\/drafts$/.test(new URL(response.url()).pathname)
    ));
    await page.getByRole("button", { name: "生成基础简历" }).click();
    const draftResponse = await draftResponsePromise;
    const drafted = await draftResponse.json() as { task_id: string };
    apiResponses.push({ operation: "draft", status: draftResponse.status(), body: drafted });
    tasks.push({ owner_user_id: user.user_id, task_id: drafted.task_id });
    expect((await runWorker(page, drafted.task_id)).status).toBe("succeeded");
    await page.waitForURL(/\/resumes\/resume_[^/]+\/edit$/);
    await expect(page.getByText("1 个事实来源").first()).toBeVisible();
    captures.push({
      name: "03-model-draft-provenance",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "03-model-draft-provenance"),
    });
    const resumeId = new URL(page.url()).pathname.split("/")[2]!;
    const versionsResponse = await page.request.get(
      `/api/v1/resumes/${resumeId}/versions?limit=20`,
    );
    const versions = await versionsResponse.json() as {
      items: Array<{ id: string; generation_mode: string }>;
    };
    const resumeVersionId = versions.items[0].id;
    expect(versions.items[0].generation_mode).toBe("model");

    await page.goto(`/jobs/new?version=${resumeVersionId}`);
    await page.getByLabel("岗位名称").fill("产品实习生");
    await page.getByLabel("公司（可选）").fill("浏览器合同公司");
    await page.getByLabel("JD 原文").fill("负责用户调研\n熟悉产品原型");
    const parseResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && /\/api\/v1\/jobs\/[^/]+\/parse$/.test(new URL(response.url()).pathname)
    ));
    await page.getByRole("button", { name: "解析岗位要求" }).click();
    const parseResponse = await parseResponsePromise;
    const parsing = await parseResponse.json() as { id: string; task_id: string };
    apiResponses.push({ operation: "parse", status: parseResponse.status(), body: parsing });
    tasks.push({ owner_user_id: user.user_id, task_id: parsing.task_id });
    expect((await runWorker(page, parsing.task_id)).status).toBe("succeeded");
    await expect(page.getByText("解析完成，请确认每条要求。")).toBeVisible();
    await expect(page.getByText("模型解析").first()).toBeVisible();
    captures.push({
      name: "04-jd-provenance",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "04-jd-provenance"),
    });

    await page.getByRole("button", { name: "确认全部要求" }).click();
    await expect(page.getByText("岗位要求已确认，可以开始匹配。")).toBeVisible();
    const matchResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/match-analyses"
    ));
    await page.getByRole("button", { name: "开始匹配" }).click();
    const matchResponse = await matchResponsePromise;
    const matching = await matchResponse.json() as { id: string; task_id: string };
    apiResponses.push({ operation: "match", status: matchResponse.status(), body: matching });
    tasks.push({ owner_user_id: user.user_id, task_id: matching.task_id });
    expect((await runWorker(page, matching.task_id)).status).toBe("succeeded");
    await page.waitForURL(/\/jobs\/job_[^/]+\/match/);
    await expect(page.getByText("模型匹配")).toBeVisible();
    captures.push({
      name: "05-match-categories",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "05-match-categories"),
    });

    await page.getByRole("button", { name: "逐条处理建议" }).click();
    await page.waitForURL(/\/suggestions\/match_/);
    await expect(page.getByText("待处理", { exact: true })).toBeVisible();
    captures.push({
      name: "06-pending-suggestion",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "06-pending-suggestion"),
    });
    await page.getByRole("button", { name: "下一条建议" }).click();
    await expect(page.getByText("等待补充事实", { exact: true })).toBeVisible();
    captures.push({
      name: "07-blocked-suggestion",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "07-blocked-suggestion"),
    });

    await page.goto("/tasks");
    await expect(page.getByText("已完成 · 处理完成").first()).toBeVisible();
    captures.push({
      name: "08-task-success",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "08-task-success"),
    });

    const primaryAssertions = await Promise.all(tasks.map(async ({ owner_user_id, task_id }) => ({
      owner_scope_hash: ownerHash(owner_user_id),
      task_id,
      state: redactOwnerIds(await inspectTask(page, task_id)),
    })));
    const restartedResponse = await page.request.post("/api/v1/intake-sessions", {
      data: { restart: true },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    expect(restartedResponse.status()).toBe(201);
    const restarted = await restartedResponse.json() as { id: string };
    await page.goto(`/create?session=${restarted.id}`);
    await expect(page.getByLabel("你的回答")).toBeVisible();
    await page.getByLabel("你的回答").fill("组织校园活动并完成复盘。");
    const failureResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && /\/api\/v1\/intake-sessions\/[^/]+\/answers$/.test(new URL(response.url()).pathname)
    ));
    await page.getByRole("button", { name: "保存并继续" }).click();
    const failureResponse = await failureResponsePromise;
    const failureAnswer = await failureResponse.json() as { analysis_task_id: string };
    tasks.push({
      owner_user_id: user.user_id,
      task_id: failureAnswer.analysis_task_id,
    });
    const failedWorker = await runWorker(page, failureAnswer.analysis_task_id, true);
    expect(failedWorker.status).toBe("failed");
    await expect(page.getByRole("heading", { name: "这段经历暂时没有整理完成" }))
      .toBeVisible();
    await expect(page.getByRole("button", { name: "重试整理" })).toBeVisible();
    captures.push({
      name: "09-recoverable-failure",
      route: new URL(page.url()).pathname,
      metrics: await capture(page, directory, "09-recoverable-failure"),
    });

    const dbAssertions = [
      ...primaryAssertions,
      {
        owner_scope_hash: ownerHash(user.user_id),
        task_id: failureAnswer.analysis_task_id,
        state: redactOwnerIds(
          await inspectTask(page, failureAnswer.analysis_task_id),
        ),
      },
    ];
    await writeFile(
      resolve(directory, "api-db-report.json"),
      `${JSON.stringify({
        api_responses: apiResponses,
        broker_status: "BLOCKED_NO_REDIS",
        captures,
        owner_scope_hash: ownerHash(user.user_id),
        page_errors: pageErrors,
        server_errors: serverErrors,
        task_assertions: dbAssertions,
        viewport,
      }, null, 2)}\n`,
    );
    expect(captures).toHaveLength(9);
    expect(pageErrors).toEqual([]);
    expect(serverErrors).toEqual([]);
    await page.close({ runBeforeUnload: false });
  });
}
