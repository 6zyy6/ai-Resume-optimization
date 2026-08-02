import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { localDevCommands, runDevSupervisor } from "../dev-supervisor.mjs";

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

test("local development starts only the web and required backend processes", () => {
  const commands = localDevCommands("/workspace");

  assert.deepEqual(
    commands.map(({ name }) => name),
    ["web", "api", "ai", "dispatcher", "worker"],
  );
  assert.equal(commands.some(({ args }) => args.includes("@resume/miniprogram")), false);
  assert.equal(
    commands.find(({ name }) => name === "api").args.includes("app.local_main:app"),
    true,
  );
});

test("AI orchestration acceptance builds workspace dependencies before services", async () => {
  const source = await readFile(
    new URL("../acceptance/start-ai-orchestration-v2.mjs", import.meta.url),
    "utf8",
  );
  const buildFilters = [...source.matchAll(
    /checked\("pnpm", \["--filter", "([^"]+)", "build"\]/g,
  )].map((match) => match[1]);

  assert.deepEqual(buildFilters.slice(0, 4), [
    "@resume/design-tokens",
    "@resume/shared",
    "@resume/ai",
    "@resume/web",
  ]);
});

test("returns a child failure and terminates its sibling", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dev-supervisor-"));
  const marker = join(directory, "terminated");

  try {
    const messages = [];
    const exitCode = await runDevSupervisor([
      {
        name: "web",
        command: process.execPath,
        args: [
          "-e",
          "process.on('SIGTERM', () => { require('node:fs').writeFileSync(process.argv[1], 'terminated'); process.exit(0); }); setInterval(() => {}, 1000);",
          marker,
        ],
      },
      {
        name: "ai",
        command: process.execPath,
        args: ["-e", "setTimeout(() => process.exit(7), 100)"],
      },
    ], { log: (message) => messages.push(message) });

    assert.equal(exitCode, 7);
    assert.equal(await readFile(marker, "utf8"), "terminated");
    assert.equal(
      messages.some((message) => (
        message.includes("service=ai")
        && message.includes("error_code=process_exit_7")
      )),
      true,
    );
    assert.equal(messages.some((message) => message.includes(marker)), false);
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("reports readiness only after web, API, and Pi are all ready", async () => {
  const messages = [];
  const readinessChecks = new Map();
  let aiReady = false;
  let webIdentityReady = false;
  const commands = ["web", "api", "ai", "dispatcher", "worker"].map((name) => ({
    name,
    command: process.execPath,
    args: ["-e", "setTimeout(() => process.exit(0), 250)"],
    ...(name === "web" || name === "api" || name === "ai"
      ? {
          expectedService: name,
          expectedStatus: "ready",
          identityUrl: `http://${name}.local/version`,
          readyUrl: `http://${name}.local/ready`,
        }
      : {}),
  }));
  const run = runDevSupervisor(commands, {
    fetch: async (url) => {
      const service = new URL(String(url)).hostname.split(".")[0];
      readinessChecks.set(service, (readinessChecks.get(service) ?? 0) + 1);
      const identity = String(url).endsWith("/version");
      return new Response(JSON.stringify(identity
        ? { service: service === "web" && !webIdentityReady ? "api" : service }
        : { status: "ready" }), {
        headers: { "Content-Type": "application/json" },
        status: service === "ai" && !aiReady && !identity ? 503 : 200,
      });
    },
    intervalMs: 5,
    log: (message) => messages.push(message),
    timeoutMs: 200,
  });

  await delay(30);
  assert.equal(messages.some((message) => message.includes("status=ready")), false);
  aiReady = true;
  await delay(30);
  assert.equal(messages.some((message) => message.includes("status=ready")), false);
  webIdentityReady = true;

  assert.equal(await run, 0);
  assert.equal(
    messages.filter((message) => message.includes("status=ready")).length,
    1,
  );
  assert.deepEqual([...readinessChecks.keys()].sort(), ["ai", "api", "web"]);
});

test("does not report ready when a service exits during its readiness probe", async () => {
  const messages = [];
  const exitCode = await runDevSupervisor([{
    name: "api",
    command: process.execPath,
    args: ["-e", "setTimeout(() => process.exit(9), 20)"],
    expectedService: "api",
    readyUrl: "http://api.local/ready",
  }], {
    fetch: async () => {
      await delay(60);
      return new Response(JSON.stringify({ service: "api", status: "ready" }), {
        headers: { "Content-Type": "application/json" },
      });
    },
    intervalMs: 25,
    log: (message) => messages.push(message),
    timeoutMs: 200,
  });

  await delay(80);
  assert.equal(exitCode, 9);
  assert.equal(messages.some((message) => message.includes("status=ready")), false);
});

test("fails safely when readiness times out", async () => {
  const messages = [];
  const exitCode = await runDevSupervisor([{
    name: "api",
    command: process.execPath,
    args: ["-e", "setInterval(() => {}, 1000)"],
    expectedService: "api",
    identityUrl: "http://api.local/version",
    readyUrl: "http://api.local/ready",
  }], {
    fetch: async () => new Response(JSON.stringify({ status: "not_ready" }), {
      headers: { "Content-Type": "application/json" },
      status: 503,
    }),
    intervalMs: 5,
    log: (message) => messages.push(message),
    timeoutMs: 25,
  });

  assert.equal(exitCode, 1);
  assert.equal(
    messages.includes("[dev] service=supervisor status=failed error_code=readiness_timeout"),
    true,
  );
});
