import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { relative, resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const evidenceDir = resolve(
  root,
  process.argv[2]
    ?? "docs/superpowers/specs/ai-resume-assistant-v2/evidence/v2-implementation/2026-07-30",
);

const routes = {
  create: "/create",
  export: "/exports/new?version=missing",
  home: "/home",
  import: "/imports/new/confirm",
  job: "/jobs/new",
  login: "/login",
  privacy: "/legal/privacy-policy",
  settings: "/settings",
};

async function pngFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return pngFiles(path);
    return entry.name.endsWith(".png") ? [path] : [];
  }));
  return nested.flat().sort();
}

const screenshots = await Promise.all((await pngFiles(evidenceDir)).map(async (path) => {
  const bytes = await readFile(path);
  const file = await stat(path);
  const name = relative(evidenceDir, path).split("/").at(-1);
  const scenario = Object.keys(routes).find((candidate) => (
    name === `${candidate}.png`
    || name.includes(`-${candidate}.png`)
    || name.startsWith(`${candidate}-`)
  ));
  return {
    captured_at: file.mtime.toISOString(),
    kind: path.includes("/responsive/") ? "responsive-smoke" : "core-flow",
    height: bytes.readUInt32BE(20),
    path: relative(evidenceDir, path),
    route: scenario ? routes[scenario] : "unknown",
    sha256: createHash("sha256").update(bytes).digest("hex"),
    width: bytes.readUInt32BE(16),
  };
}));

const sourceCommit = execFileSync("git", ["rev-parse", "HEAD"], {
  cwd: root,
  encoding: "utf8",
}).trim();

const manifest = {
  browser: process.env.EVIDENCE_BROWSER_VERSION ?? "Chrome (Playwright, system channel)",
  build_id: process.env.EVIDENCE_BUILD_ID ?? sourceCommit,
  created_at: new Date().toISOString(),
  environment: "local FastAPI + Next.js production build; isolated system Chrome",
  note: "These screenshots prove the recorded responsive smoke paths only; they are not the full 58-state acceptance matrix.",
  source_commit: sourceCommit,
  screenshot_count: screenshots.length,
  screenshots,
};

await writeFile(
  resolve(evidenceDir, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
);
console.log(`Hashed ${screenshots.length} screenshots`);
