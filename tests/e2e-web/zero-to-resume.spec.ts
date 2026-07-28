import { expect, type Page, test } from "@playwright/test";
import { installStrictFixtureApi } from "./fixture-api";

async function assertLayout(page: Page) {
  const result = await page.evaluate(() => {
    const heading = document.querySelector("h1");
    const controls = [...document.querySelectorAll(".button, .command-nav a, .statement-footer a, .command-pill")];
    return {
      headingClipped: heading ? heading.scrollWidth > heading.clientWidth : false,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      wrappedControls: controls.filter((node) => node.scrollWidth > node.clientWidth).length,
    };
  });
  expect(result).toEqual({ headingClipped: false, overflow: false, wrappedControls: 0 });
}

test("completes zero-to-resume through API-backed save, matching, suggestion and export", async ({ page }) => {
  const requests: string[] = [];
  await installStrictFixtureApi(page, requests);
  await page.goto("/");
  await page.getByRole("link", { name: /我还没有简历/ }).click();
  for (let index = 0; index < 6; index += 1) {
    await page.getByLabel("你的回答").fill(`第 ${index + 1} 步真实回答`);
    await page.getByRole("button", { name: "确认并继续" }).click();
  }
  await expect(page).toHaveURL(/resumes\/resume_r01\/edit/);
  const saveRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().includes("/api/v1/resumes/resume_r01/versions"));
  await page.getByLabel("要点 1").fill("完成访谈；整理结论。");
  await saveRequest;
  await expect(page.getByRole("status", { name: "已保存" })).toBeVisible();
  await page.getByRole("button", { name: "拆分要点" }).click();
  await expect(page.getByLabel("要点 2")).toHaveValue("整理结论");
  await page.getByRole("button", { name: "合并要点" }).click();
  await expect(page.getByLabel("要点 1")).toHaveValue("完成访谈；整理结论");
  await page.getByRole("button", { name: "新增要点" }).click();
  await expect(page.getByLabel("要点 2")).toBeVisible();
  await page.getByRole("button", { name: "删除末条" }).click();
  await expect(page.getByLabel("要点 2")).toHaveCount(0);
  await page.getByRole("button", { name: "项目移到顶部" }).click();
  await expect(page.locator(".editor-rail nav a").first()).toHaveText("项目经历");
  await page.getByRole("button", { name: /撤销/ }).click();
  await expect(page.locator(".editor-rail nav a").first()).toHaveText("教育经历");
  await page.getByRole("link", { name: "继续岗位匹配" }).click();
  await page.getByLabel("JD 原文").fill("负责用户研究和内容策划");
  await page.getByRole("button", { name: "解析岗位要求" }).click();
  await page.getByRole("button", { name: "确认并开始匹配" }).click();
  await page.getByRole("button", { name: "逐条处理建议" }).click();
  await page.getByRole("button", { name: "接受" }).click();
  await expect(page.getByRole("status", { name: "已接受" })).toBeVisible();
  await page.getByRole("link", { name: "进入导出" }).click();
  await page.getByRole("button", { name: "下载 PDF" }).click();
  await expect(page.getByRole("button", { name: "下载已准备" })).toBeVisible();
  expect(requests).toEqual(expect.arrayContaining([
    "POST /api/v1/facts",
    "POST /api/v1/resumes",
    "POST /api/v1/resumes/resume_r01/versions",
    "POST /api/v1/jobs",
    "POST /api/v1/match-analyses",
    "POST /api/v1/jobs/job_j01/parse",
    "GET /api/v1/match-analyses/analysis_a01/suggestions",
    "POST /api/v1/suggestions/suggestion_s01/accept",
    "POST /api/v1/exports",
    "GET /api/v1/exports/export_e01",
  ]));
});

test("a 409 exposes conflict choices and stops automatic overwrite", async ({ page }) => {
  let patches = 0;
  await page.route("**/api/v1/resumes/resume_r01/versions", async (route) => {
    patches += 1;
    await route.fulfill({
      contentType: "application/json",
      json: { error: { code: "VERSION_CONFLICT", details: {}, message: "Version changed", request_id: "req_conflict" } },
      status: 409,
    });
  });
  await page.goto("/resumes/resume_r01/edit");
  await page.getByLabel("要点 1").fill("本地未同步内容");
  await expect(page.locator("section.conflict-panel")).toContainText("自动保存已停止");
  await expect(page.getByRole("button", { name: "保留本地" })).toBeVisible();
  await page.waitForTimeout(1_000);
  expect(patches).toBe(1);
});

for (const width of [320, 375, 390, 414, 768, 1024, 1440]) {
  test(`zero-flow critical pages preserve layout at ${width}px`, async ({ page }) => {
    await installStrictFixtureApi(page, []);
    await page.setViewportSize({ width, height: width >= 1024 ? 900 : 844 });
    for (const path of [
      "/create",
      "/resumes/resume_r01/edit",
      "/jobs/job_j01/match?analysis=analysis_a01&version=version_v01",
      "/exports/new?version=version_v01",
    ]) {
      await page.goto(path);
      await assertLayout(page);
    }
  });
}
