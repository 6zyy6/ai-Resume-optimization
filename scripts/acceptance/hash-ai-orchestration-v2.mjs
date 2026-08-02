import { createHash } from "node:crypto";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { relative, resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const evidenceDir = resolve(
  root,
  process.argv[2]
    ?? "docs/superpowers/specs/ai-resume-assistant-v2/evidence/ai-orchestration-v2",
);
const expectedViewports = new Map([
  ["390x844", { height: 844, width: 390 }],
  ["1024x768", { height: 768, width: 1024 }],
  ["1440x900", { height: 900, width: 1440 }],
]);

async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  }));
  return nested.flat().sort();
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

const metadata = JSON.parse(await readFile(resolve(evidenceDir, "run-metadata.json"), "utf8"));
if (metadata.exit_code !== 0) throw new Error("Playwright evidence run did not pass");
const identities = JSON.parse(await readFile(resolve(evidenceDir, "service-identities.json"), "utf8"));
for (const service of ["web", "api", "pi"]) {
  if (identities[service].commit_sha !== metadata.source_commit) {
    throw new Error(`${service} identity does not match the tested source commit`);
  }
}

const reports = [];
const screenshots = [];
for (const [label, viewport] of expectedViewports) {
  const reportPath = resolve(evidenceDir, label, "api-db-report.json");
  const report = JSON.parse(await readFile(reportPath, "utf8"));
  if (report.captures.length !== 9) throw new Error(`${label} must contain nine state captures`);
  if (report.page_errors.length || report.server_errors.length) {
    throw new Error(`${label} contains a page error or API 5xx`);
  }
  if (report.broker_status !== "BLOCKED_NO_REDIS") {
    throw new Error(`${label} must disclose the missing Redis/Celery topology`);
  }
  if (report.captures.some(({ metrics }) => (
    metrics.horizontal_overflow
    || metrics.runtime_error
    || metrics.visible_internal_markers.length
  ))) {
    throw new Error(`${label} contains an invalid rendered state`);
  }
  reports.push({ path: relative(evidenceDir, reportPath), viewport });

  const imageFiles = (await files(resolve(evidenceDir, label)))
    .filter((path) => path.endsWith(".png"));
  if (imageFiles.length !== 9) throw new Error(`${label} must contain exactly nine screenshots`);
  for (const path of imageFiles) {
    const bytes = await readFile(path);
    const width = bytes.readUInt32BE(16);
    const height = bytes.readUInt32BE(20);
    if (width !== viewport.width || height < viewport.height) {
      throw new Error(`${relative(evidenceDir, path)} has unexpected dimensions ${width}x${height}`);
    }
    screenshots.push({
      height,
      path: relative(evidenceDir, path),
      sha256: sha256(bytes),
      viewport,
      width,
    });
  }
}
if (screenshots.length !== 27) throw new Error("Expected exactly 27 screenshots");

const manifestFiles = [];
for (const path of await files(evidenceDir)) {
  if (path.endsWith("manifest.json")) continue;
  const bytes = await readFile(path);
  manifestFiles.push({
    bytes: (await stat(path)).size,
    path: relative(evidenceDir, path),
    sha256: sha256(bytes),
  });
}
const manifest = {
  checks: {
    api_5xx_count: 0,
    horizontal_overflow_count: 0,
    internal_marker_count: 0,
    page_error_count: 0,
    screenshot_count: screenshots.length,
    state_count_per_viewport: 9,
  },
  created_at: new Date().toISOString(),
  evidence_status: "PASS",
  external_gates: metadata.external_gates,
  files: manifestFiles,
  note: "PASS is limited to deterministic local HTTP orchestration evidence; every external gate remains BLOCKED.",
  reports,
  screenshots,
  source_commit: metadata.source_commit,
  tested_environment: metadata.environment,
};
await writeFile(
  resolve(evidenceDir, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
);
console.log(`Hashed ${screenshots.length} AI orchestration screenshots for ${metadata.source_commit}`);
