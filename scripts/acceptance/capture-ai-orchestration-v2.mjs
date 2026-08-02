import { spawnSync } from "node:child_process";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const evidenceDir = resolve(
  root,
  process.argv[2]
    ?? "docs/superpowers/specs/ai-resume-assistant-v2/evidence/ai-orchestration-v2",
);
const sourceCommit = spawnSync("git", ["rev-parse", "HEAD"], {
  cwd: root,
  encoding: "utf8",
}).stdout.trim();
const worktreeStatus = spawnSync("git", ["status", "--porcelain"], {
  cwd: root,
  encoding: "utf8",
}).stdout.trim();

if (!sourceCommit || worktreeStatus) {
  throw new Error("AI orchestration evidence requires a clean immutable source commit");
}
if (process.env.APP_COMMIT_SHA && process.env.APP_COMMIT_SHA !== sourceCommit) {
  throw new Error("APP_COMMIT_SHA must match the checked-out source commit");
}
if (!evidenceDir.includes("ai-orchestration-v2")) {
  throw new Error("Evidence output must be scoped to ai-orchestration-v2");
}
try {
  await stat(evidenceDir);
  throw new Error(`Evidence output already exists: ${evidenceDir}`);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
await mkdir(evidenceDir, { recursive: true });

const args = [
  "exec",
  "playwright",
  "test",
  "--project=ai-orchestration-real",
  "--reporter=line",
];
const command = `AI_ORCHESTRATION_REAL_SERVICES=1 APP_COMMIT_SHA=${sourceCommit} pnpm ${args.join(" ")}`;
const startedAt = new Date().toISOString();
const result = spawnSync("pnpm", args, {
  cwd: root,
  encoding: "utf8",
  env: {
    ...process.env,
    AI_ORCHESTRATION_EVIDENCE_DIR: evidenceDir,
    AI_ORCHESTRATION_REAL_SERVICES: "1",
    APP_COMMIT_SHA: sourceCommit,
  },
});
const endedAt = new Date().toISOString();
const commandLog = [
  `$ ${command}`,
  result.stdout,
  result.stderr,
].filter(Boolean).join("\n");
await writeFile(resolve(evidenceDir, "command.log"), `${commandLog}\n`);
let browser = null;
let buildId = null;
if (result.status === 0) {
  const reports = await Promise.all(
    ["390x844", "1024x768", "1440x900"].map(async (viewport) => JSON.parse(
      await readFile(resolve(evidenceDir, viewport, "api-db-report.json"), "utf8"),
    )),
  );
  const browsers = new Set(reports.map((report) => report.browser));
  const buildIds = new Set(reports.map((report) => report.build_id));
  if (browsers.size !== 1 || buildIds.size !== 1) {
    throw new Error("All viewport reports must use one browser and build ID");
  }
  [browser] = browsers;
  [buildId] = buildIds;
}
await writeFile(
  resolve(evidenceDir, "run-metadata.json"),
  `${JSON.stringify({
    browser,
    build_id: buildId,
    command,
    commit_sha: sourceCommit,
    ended_at: endedAt,
    environment: "Next.js production build/start + FastAPI + SQLite + in-process worker operation + TCP Pi deterministic fixture",
    exit_code: result.status ?? 1,
    external_gates: {
      cloud_deployment: "BLOCKED",
      external_browser_matrix: "BLOCKED",
      real_model_accuracy: "BLOCKED",
      real_redis_dispatcher_celery: "BLOCKED",
      user_study: "BLOCKED",
    },
    source_commit: sourceCommit,
    started_at: startedAt,
  }, null, 2)}\n`,
);
process.stdout.write(result.stdout ?? "");
process.stderr.write(result.stderr ?? "");
if (result.error) throw result.error;
if (result.status !== 0) process.exitCode = result.status ?? 1;
else console.log(`Captured AI orchestration evidence in ${evidenceDir}`);
