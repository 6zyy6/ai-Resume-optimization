import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e-web",
  use: {
    baseURL: "http://127.0.0.1:3100",
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "pnpm --filter @resume/web dev --port 3100",
    reuseExistingServer: true,
    url: "http://127.0.0.1:3100",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
