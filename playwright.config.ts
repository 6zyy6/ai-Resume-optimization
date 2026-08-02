import { defineConfig, devices } from "@playwright/test";
import { join } from "node:path";
import { tmpdir } from "node:os";

const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;
const realServices = process.env.AI_ORCHESTRATION_REAL_SERVICES === "1";
if (realServices) {
  const runtimeDir = process.env.AI_ORCHESTRATION_RUNTIME_DIR
    ?? join(tmpdir(), `ai-orchestration-v2-${process.pid}`);
  process.env.AI_ORCHESTRATION_RUNTIME_DIR = runtimeDir;
  process.env.AI_ORCHESTRATION_DATABASE_URL ??=
    `sqlite+aiosqlite:///${join(runtimeDir, "contract.db")}`;
  process.env.AI_ORCHESTRATION_API_URL ??= "http://127.0.0.1:8310";
  process.env.AI_ORCHESTRATION_PI_URL ??= "http://127.0.0.1:8311";
  process.env.PLAYWRIGHT_BASE_URL ??= "http://127.0.0.1:3310";
}
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3100";

export default defineConfig({
  testDir: "./tests",
  use: {
    baseURL,
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: externalBaseUrl
    ? undefined
    : realServices
      ? {
          command: "node scripts/acceptance/start-ai-orchestration-v2.mjs",
          reuseExistingServer: false,
          timeout: 120_000,
          url: `${baseURL}/version`,
        }
      : {
          command: "pnpm --filter @resume/web dev --port 3100",
          reuseExistingServer: true,
          url: "http://127.0.0.1:3100",
        },
  projects: realServices
    ? [{
        name: "ai-orchestration-real",
        testMatch: /e2e\/ai-orchestration-v2\.spec\.ts/,
        use: { ...devices["Desktop Chrome"] },
      }]
    : [{
        name: "fixture-chromium",
        testMatch: /e2e-web\/.*\.spec\.ts/,
        use: { ...devices["Desktop Chrome"] },
      }],
});
