import { expect, type Page, test } from "@playwright/test";
import { installStrictFixtureApi } from "./fixture-api";

test("completes import, confirmation, JD, suggestion and export through the adapter", async ({ page }) => {
  const requests: string[] = [];
  await installStrictFixtureApi(page, requests);
  await page.goto("/");
  await page.getByRole("link", { name: /我已经有简历/ }).click();
  await page.getByLabel("选择简历文件").setInputFiles({ buffer: Buffer.from("resume"), mimeType: "text/plain", name: "resume.txt" });
  await page.getByRole("button", { name: "确认并进入岗位信息" }).click();
  await page.getByLabel("JD 原文").fill("需要用户研究和内容策划能力");
  await page.getByRole("button", { name: "解析岗位要求" }).click();
  await page.getByRole("button", { name: "确认并开始匹配" }).click();
  await expect(page.getByText("真实缺口")).toBeVisible();
  await page.getByRole("button", { name: "逐条处理建议" }).click();
  await expect(page.getByRole("heading", { name: "逐条确认，不批量接受" })).toBeVisible();
  await page.locator("body").click({ position: { x: 8, y: 8 } });
  await page.keyboard.press("KeyA");
  await expect(page.getByRole("status", { name: "已接受" })).toBeVisible();
  await page.getByRole("link", { name: "进入导出" }).click();
  await page.getByRole("button", { name: "下载 PDF" }).click();
  await expect(page.getByRole("button", { name: "下载已准备" })).toBeVisible();
  expect(requests).toEqual(expect.arrayContaining([
    "POST /api/v1/files/upload-tokens",
    "PUT https://upload.fixture/file_f01",
    "POST /api/v1/files/file_f01/confirm-upload",
    "POST /api/v1/imports",
    "POST /api/v1/imports/import_i01/confirm",
    "POST /api/v1/resumes/resume_r01/versions",
    "POST /api/v1/jobs",
    "POST /api/v1/jobs/job_j01/parse",
    "POST /api/v1/match-analyses",
    "GET /api/v1/match-analyses/analysis_a01/suggestions",
    "POST /api/v1/suggestions/suggestion_s01/accept",
    "POST /api/v1/exports",
    "GET /api/v1/exports/export_e01",
  ]));
});

test("strict fixture rejects unknown paths and invalid bodies", async ({ page }) => {
  await installStrictFixtureApi(page, []);
  await page.goto("/");
  const statuses = await page.evaluate(async () => {
    const unknown = await fetch("/api/v1/not-real");
    const invalid = await fetch("/api/v1/jobs", {
      body: JSON.stringify({ content: "wrong schema" }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });
    return [unknown.status, invalid.status];
  });
  expect(statuses).toEqual([404, 422]);
});

for (const width of [320, 375, 390, 414, 768, 1024, 1440]) {
  test(`optimize-flow critical pages preserve layout at ${width}px`, async ({ page }) => {
    await installStrictFixtureApi(page, []);
    await page.setViewportSize({ width, height: width >= 1024 ? 900 : 844 });
    for (const path of [
      "/imports/new/confirm",
      "/jobs/new?version=version_v01",
      "/suggestions/analysis_a01?suggestion=suggestion_s01&version=version_v01",
      "/exports/new?version=version_v01",
    ]) {
      await page.goto(path);
      const result = await page.evaluate(() => ({
        headingClipped: [...document.querySelectorAll("h1")].some((node) => node.scrollWidth > node.clientWidth),
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        wrappedControls: [...document.querySelectorAll(".button, .command-nav a, .statement-footer a, .command-pill")]
          .filter((node) => node.scrollWidth > node.clientWidth).length,
      }));
      expect(result).toEqual({ headingClipped: false, overflow: false, wrappedControls: 0 });
    }
  });
}
