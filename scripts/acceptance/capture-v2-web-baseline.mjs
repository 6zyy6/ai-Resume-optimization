import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { chromium } from "@playwright/test";

const root = resolve(import.meta.dirname, "../..");
const outputDir = resolve(
  root,
  process.argv[2]
    ?? "docs/superpowers/specs/ai-resume-assistant-v2/evidence/current-baseline",
);
const baseUrl = process.env.WEB_BASE_URL ?? "http://127.0.0.1:3000";
const routes = [
  { path: "/home", slug: "home" },
  { path: "/create", slug: "create" },
  { path: "/resumes", slug: "resumes" },
  { path: "/facts", slug: "facts" },
  { path: "/tasks", slug: "tasks" },
  { path: "/settings", slug: "settings" },
];
const viewports = [
  { height: 844, label: "390x844", width: 390 },
  { height: 900, label: "1440x900", width: 1440 },
];

function git(...args) {
  return execFileSync("git", args, { cwd: root, encoding: "utf8" }).trim();
}

async function sha256(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

await mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const browserVersion = browser.version();
const captures = [];

try {
  for (const viewport of viewports) {
    const viewportDir = resolve(outputDir, viewport.label);
    await mkdir(viewportDir, { recursive: true });
    const context = await browser.newContext({
      deviceScaleFactor: 1,
      viewport: { height: viewport.height, width: viewport.width },
    });

    for (const route of routes) {
      const page = await context.newPage();
      const requests = [];
      page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.pathname.startsWith("/api/") || url.port === "8000") {
          requests.push(`${request.method()} ${url.pathname}${url.search}`);
        }
      });
      const response = await page.goto(`${baseUrl}${route.path}`, {
        waitUntil: "domcontentloaded",
      });
      await page.waitForTimeout(750);
      const metrics = await page.evaluate(() => {
        const clickable = [
          ...document.querySelectorAll(
            "a, button, [role='button'], input[type='submit']",
          ),
        ];
        return {
          body_text_prefix: document.body.innerText.slice(0, 500),
          h1: document.querySelector("h1")?.textContent?.trim() ?? null,
          horizontal_overflow:
            document.documentElement.scrollWidth
            > document.documentElement.clientWidth,
          runtime_error_overlay: [
            ...document.querySelectorAll("nextjs-portal"),
          ].some((portal) => (
            portal.shadowRoot?.textContent?.includes("Runtime Error") ?? false
          )) || document.body.innerText.includes("Runtime Error"),
          wrapped_clickable_count: clickable.filter(
            (element) => element.scrollHeight > element.clientHeight + 1,
          ).length,
        };
      });
      const screenshotPath = resolve(viewportDir, `${route.slug}.png`);
      await page.screenshot({ fullPage: true, path: screenshotPath });
      captures.push({
        api_requests: requests,
        cookies: (await context.cookies(baseUrl)).map((cookie) => cookie.name),
        http_status: response?.status() ?? null,
        metrics,
        path: route.path,
        screenshot: screenshotPath.slice(outputDir.length + 1),
        screenshot_sha256: await sha256(screenshotPath),
        viewport: viewport.label,
      });
      await page.close();
    }
    await context.close();
  }
} finally {
  await browser.close();
}

const sourceFiles = [
  "app/web/app/home/page.tsx",
  "app/web/app/create/page.tsx",
  "app/web/app/resumes/page.tsx",
  "app/web/app/facts/page.tsx",
  "app/web/app/tasks/page.tsx",
  "app/web/app/settings/page.tsx",
];
const sourceHashes = Object.fromEntries(
  await Promise.all(
    sourceFiles.map(async (file) => [
      file,
      await sha256(resolve(root, file)),
    ]),
  ),
);
const status = git("status", "--porcelain");
const manifest = {
  browser: `Chrome ${browserVersion}`,
  captures,
  commit_sha: git("rev-parse", "HEAD"),
  created_at: new Date().toISOString(),
  evidence_status: status ? "BASELINE_DIRTY_WORKTREE" : "BASELINE_CLEAN_WORKTREE",
  source_hashes: sourceHashes,
  web_base_url: baseUrl,
  working_tree_changes: status ? status.split("\n") : [],
};
await writeFile(
  resolve(outputDir, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
);
console.log(`Captured ${captures.length} screenshots in ${outputDir}`);
