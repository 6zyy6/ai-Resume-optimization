import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

export const patterns = {
  email: /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,
  china_mobile: /(?<!\d)1[3-9]\d{9}(?!\d)/g,
  china_id: /(?<!\d)\d{17}[\dXx](?!\d)/g,
  provider_key: /\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,})\b/g,
  private_key: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g,
  bearer_token: /\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*\b/gi,
};

export function scanText(text, enabled = Object.keys(patterns)) {
  return enabled.flatMap((name) => {
    const expression = patterns[name];
    expression.lastIndex = 0;
    return [...text.matchAll(expression)].map((match) => ({
      type: name,
      index: match.index ?? -1,
      preview: `<redacted:${match[0].length}>`,
    }));
  });
}

export async function scanFiles(files, enabled) {
  const findings = [];
  for (const file of files) {
    const text = await readFile(file, "utf8").catch(() => "");
    for (const finding of scanText(text, enabled)) findings.push({ file, ...finding });
  }
  return findings;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const outputArg = process.argv.find((arg) => arg.startsWith("--output="));
  const artifactArgs = process.argv
    .slice(2)
    .filter((arg) => !arg.startsWith("--"));
  const tracked = execFileSync("git", ["ls-files", "-z"], { encoding: "utf8" })
    .split("\0")
    .filter(Boolean)
    .map((file) => resolve(file));
  const repositoryFindings = await scanFiles(tracked, ["provider_key", "private_key", "bearer_token"]);
  const artifactFindings = await scanFiles(artifactArgs.map((file) => resolve(file)));
  const report = {
    scanned_tracked_files: tracked.length,
    scanned_artifact_files: artifactArgs.length,
    findings: [...repositoryFindings, ...artifactFindings],
  };
  if (outputArg) await writeFile(resolve(outputArg.slice(9)), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report));
  if (report.findings.length) process.exitCode = 1;
}
