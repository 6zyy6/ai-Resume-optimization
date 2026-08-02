import { spawn, spawnSync } from "node:child_process";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const python = resolve(root, ".venv/bin/python");
const runtimeDir = process.env.AI_ORCHESTRATION_RUNTIME_DIR;
const databaseUrl = process.env.AI_ORCHESTRATION_DATABASE_URL;
const apiUrl = process.env.AI_ORCHESTRATION_API_URL ?? "http://127.0.0.1:8310";
const piUrl = process.env.AI_ORCHESTRATION_PI_URL ?? "http://127.0.0.1:8311";
const webUrl = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3310";
const token = "local-ai-orchestration-fixture-token";
const sourceCommit = process.env.APP_COMMIT_SHA ?? "local-evidence";
const evidenceDir = process.env.AI_ORCHESTRATION_EVIDENCE_DIR;

const safeRuntimePrefix = resolve(tmpdir(), "ai-orchestration-v2-");
if (
  !runtimeDir
  || !databaseUrl
  || !resolve(runtimeDir).startsWith(safeRuntimePrefix)
) {
  throw new Error("AI orchestration runtime must use an isolated temporary directory");
}
await rm(runtimeDir, { force: true, recursive: true });
await mkdir(runtimeDir, { recursive: true });

function checked(command, args, env = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, ...env },
    stdio: "inherit",
  });
  if (result.status !== 0) {
    throw new Error(`${command} failed with exit ${result.status ?? "unknown"}`);
  }
}

checked("pnpm", ["--filter", "@resume/ai", "build"]);
checked("pnpm", ["--filter", "@resume/web", "build"], {
  APP_COMMIT_SHA: sourceCommit,
  WEB_API_PROXY_TARGET: apiUrl,
});
checked(
  python,
  ["-m", "alembic", "-c", "packages/api/alembic.ini", "upgrade", "head"],
  { DATABASE_URL: databaseUrl },
);

const children = [];
function start(name, command, args, env = {}) {
  const child = spawn(command, args, {
    cwd: root,
    env: { ...process.env, ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => process.stdout.write(`[${name}] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[${name}] ${chunk}`));
  child.once("exit", (code, signal) => {
    if (!stopping) {
      console.error(`[services] service=${name} status=failed error_code=${signal ? `signal_${signal}` : `exit_${code}`}`);
      void shutdown(1);
    }
  });
  children.push(child);
}

const piPort = new URL(piUrl).port;
const apiPort = new URL(apiUrl).port;
const webPort = new URL(webUrl).port;
start("ai", process.execPath, ["tests/contract/fixtures/pi-server.mjs"], {
  AI_PORT: piPort,
  AI_SERVICE_TOKEN: token,
  APP_COMMIT_SHA: sourceCommit,
});
start(
  "api",
  python,
  [
    "-m",
    "uvicorn",
    "ai_orchestration_fastapi:app",
    "--app-dir",
    "tests/contract/fixtures",
    "--host",
    "127.0.0.1",
    "--port",
    apiPort,
  ],
  {
    AI_INTERNAL_URL: piUrl,
    AI_SERVICE_TOKEN: token,
    APP_COMMIT_SHA: sourceCommit,
    CONTRACT_DATABASE_URL: databaseUrl,
    CONTRACT_WEB_ORIGIN: webUrl,
    PYTHONPATH: resolve(root, "packages/api"),
  },
);
start(
  "web",
  "pnpm",
  ["--filter", "@resume/web", "exec", "next", "start", "--port", webPort],
  {
    APP_COMMIT_SHA: sourceCommit,
    WEB_API_PROXY_TARGET: apiUrl,
  },
);

async function ready(url, service, expected = {}) {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        const body = await response.json();
        if (
          Object.entries(expected).every(([key, value]) => body[key] === value)
        ) return body;
      }
    } catch {
      // Startup connection errors are expected until the service binds.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`${service} readiness timed out`);
}

let stopping = false;
async function shutdown(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of [...children].reverse()) {
    if (child.exitCode === null) child.kill("SIGTERM");
  }
  setTimeout(() => {
    for (const child of children) {
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    process.exit(exitCode);
  }, 2_000).unref();
}

process.on("SIGINT", () => void shutdown());
process.on("SIGTERM", () => void shutdown());
process.on("SIGHUP", () => void shutdown());

const identities = await Promise.all([
  ready(`${piUrl}/internal/v1/health/ready`, "ai-ready", { status: "ready" }),
  ready(`${piUrl}/internal/v1/version`, "ai-version", { service: "ai", commit_sha: sourceCommit }),
  ready(`${apiUrl}/v1/health/ready`, "api-ready", { status: "ready" }),
  ready(`${apiUrl}/v1/version`, "api-version", { service: "api", commit_sha: sourceCommit }),
  ready(`${webUrl}/version`, "web-version", { service: "web", commit_sha: sourceCommit }),
]);
if (evidenceDir) {
  await mkdir(evidenceDir, { recursive: true });
  await writeFile(
    resolve(evidenceDir, "service-identities.json"),
    `${JSON.stringify({
      api: identities[3],
      broker_status: "BLOCKED_NO_REDIS",
      commit_sha: sourceCommit,
      pi: identities[1],
      web: identities[4],
    }, null, 2)}\n`,
  );
}
console.log("[services] status=ready service=web,api,ai broker=blocked_no_redis");
await new Promise(() => {});
