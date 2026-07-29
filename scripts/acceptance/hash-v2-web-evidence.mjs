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
  landing: "/",
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
  const responsiveWidth = path.includes("/responsive/")
    ? Number.parseInt(name.split("-")[0], 10)
    : null;
  const viewport = responsiveWidth
    ? { width: responsiveWidth, height: responsiveWidth <= 414 ? 844 : 900 }
    : name === "landing-viewport-1280x800.png"
      ? { width: 1280, height: 800 }
      : name === "create-390.png"
        ? { width: 390, height: 844 }
        : name === "create-after-skip-1024.png"
          ? { width: 1024, height: 900 }
          : { width: 1440, height: 900 };
  return {
    captured_at: file.mtime.toISOString(),
    capture_mode: name.includes("-viewport-") ? "viewport" : "full-page",
    kind: path.includes("/responsive/") ? "responsive-smoke" : "core-flow",
    height: bytes.readUInt32BE(20),
    path: relative(evidenceDir, path),
    route: scenario ? routes[scenario] : "unknown",
    sha256: createHash("sha256").update(bytes).digest("hex"),
    viewport,
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
