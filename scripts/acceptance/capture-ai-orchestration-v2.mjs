import { spawnSync } from "node:child_process";
import { mkdir, stat, writeFile } from "node:fs/promises";
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
  `$ AI_ORCHESTRATION_REAL_SERVICES=1 APP_COMMIT_SHA=${sourceCommit} pnpm ${args.join(" ")}`,
  result.stdout,
  result.stderr,
].filter(Boolean).join("\n");
await writeFile(resolve(evidenceDir, "command.log"), `${commandLog}\n`);
await writeFile(
  resolve(evidenceDir, "run-metadata.json"),
  `${JSON.stringify({
    command: `pnpm ${args.join(" ")}`,
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
