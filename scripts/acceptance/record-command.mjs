import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { basename, join } from "node:path";
import process from "node:process";

export async function recordCommand({ command, args = [], cwd, outputDir, name }) {
  await mkdir(outputDir, { recursive: true });
  const startedAt = new Date().toISOString();
  const child = spawn(command, args, { cwd, env: process.env, shell: false });
  const chunks = [];
  child.stdout.on("data", (chunk) => { chunks.push(chunk); process.stdout.write(chunk); });
  child.stderr.on("data", (chunk) => { chunks.push(chunk); process.stderr.write(chunk); });
  const exitCode = await new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", (code) => resolve(code ?? 1));
  });
  const endedAt = new Date().toISOString();
  const raw = Buffer.concat(chunks);
  const safeName = name ?? basename(command).replaceAll(/[^a-z0-9-]/gi, "-");
  const rawLog = join(outputDir, `${safeName}.log`);
  const metadata = join(outputDir, `${safeName}.json`);
  await writeFile(rawLog, raw);
  const result = {
    command: [command, ...args].join(" "),
    started_at: startedAt,
    ended_at: endedAt,
    exit_code: exitCode,
    raw_log: rawLog,
    raw_log_sha256: createHash("sha256").update(raw).digest("hex"),
  };
  await writeFile(metadata, `${JSON.stringify(result, null, 2)}\n`);
  return { ...result, metadata };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [outputDir, name, command, ...args] = process.argv.slice(2);
  if (!outputDir || !name || !command) {
    throw new Error("usage: record-command.mjs <output-dir> <name> <command> [...args]");
  }
  const result = await recordCommand({ command, args, cwd: process.cwd(), outputDir, name });
  process.exitCode = result.exit_code;
}
