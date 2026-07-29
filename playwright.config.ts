import { defineConfig, devices } from "@playwright/test";

const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: "./tests/e2e-web",
  use: {
    baseURL: externalBaseUrl ?? "http://127.0.0.1:3100",
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: externalBaseUrl ? undefined : {
    command: "pnpm --filter @resume/web dev --port 3100",
    reuseExistingServer: true,
    url: "http://127.0.0.1:3100",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
