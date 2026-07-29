import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { buildManifest } from "./build-manifest.mjs";
import { recordCommand } from "./record-command.mjs";

const root = new URL("../..", import.meta.url).pathname;
const commitSha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
const releaseId = `${new Date().toISOString().replaceAll(/[-:.TZ]/g, "").slice(0, 14)}-${commitSha.slice(0, 8)}`;
const releaseDir = join(root, "artifacts", "acceptance", releaseId);
const commandDir = join(releaseDir, "commands");
await mkdir(commandDir, { recursive: true });

const commands = [
  { name: "install", command: "pnpm", args: ["install"] },
  { name: "lint", command: "pnpm", args: ["lint"] },
  { name: "test", command: "pnpm", args: ["test"] },
  { name: "build", command: "pnpm", args: ["build"] },
  { name: "contract", command: "pnpm", args: ["exec", "vitest", "run", "tests/contract/deployment.test.ts"] },
  { name: "miniprogram", command: "pnpm", args: ["--filter", "@resume/miniprogram", "test"] },
  { name: "manifest-tests", command: "pnpm", args: ["exec", "vitest", "run", "tests/acceptance/manifest.test.ts"] },
];
const results = [];
for (const entry of commands) {
  results.push(await recordCommand({ ...entry, cwd: root, outputDir: commandDir }));
}

const blockerPath = join(releaseDir, "external-blockers.txt");
await writeFile(blockerPath, [
  "BLOCKED external evidence:",
  "- Docker CLI/container-runtime validation unavailable.",
  "- Tencent Cloud, production TLS/network, COS, alert delivery and backup restore not executed.",
  "- Safari/Edge matrix and WeChat developer tools/real-device evidence unavailable.",
  "- Real provider billing reconciliation and 30-student validation unavailable.",
  "- This release is NOT READY while any P0 item remains BLOCKED.",
  "",
].join("\n"));

const passEvidence = {};

const sensitive = await recordCommand({
  name: "sensitive-scan",
  command: "node",
  args: ["scripts/acceptance/check-sensitive-data.mjs", `--output=${join(releaseDir, "sensitive-scan.json")}`, ...results.map((item) => item.raw_log)],
  cwd: root,
  outputDir: commandDir,
});
results.push(sensitive);
await buildManifest({ releaseDir, releaseId, commitSha, blockerPath, passEvidence });
if (sensitive.exit_code !== 0 || results.some((result) => result.exit_code !== 0)) process.exitCode = 1;
console.log(`Acceptance manifest: ${join(releaseDir, "manifest.json")}`);
