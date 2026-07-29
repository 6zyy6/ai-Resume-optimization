import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { buildManifest } from "../../scripts/acceptance/build-manifest.mjs";
import { ACCEPTANCE_IDS, checkCoverage } from "../../scripts/acceptance/check-coverage.mjs";
import { scanText } from "../../scripts/acceptance/check-sensitive-data.mjs";

const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");

async function fixture() {
  const parent = await mkdtemp(join(tmpdir(), "acceptance-"));
  const releaseId = "20260729000000-aaaaaaaa";
  const releaseDir = join(parent, releaseId);
  const commandDir = join(releaseDir, "commands");
  await mkdir(commandDir, { recursive: true });
  const rawLog = join(commandDir, "test.log");
  await writeFile(rawLog, "12 tests passed\n");
  await writeFile(join(commandDir, "test.json"), JSON.stringify({
    command: "pnpm test",
    started_at: "2026-07-29T00:00:00.000Z",
    ended_at: "2026-07-29T00:01:00.000Z",
    exit_code: 0,
    raw_log: rawLog,
    raw_log_sha256: hash("12 tests passed\n"),
  }));
  const blockerPath = join(releaseDir, "external-blockers.txt");
  await writeFile(blockerPath, "BLOCKED: real device unavailable\n");
  const passEvidence = {
    "MP-09": { path: "commands/test.log", sha256: hash("12 tests passed\n") },
  };
  const manifest = await buildManifest({
    releaseDir,
    releaseId,
    commitSha: "a".repeat(40),
    blockerPath,
    passEvidence,
  });
  return { manifest, releaseDir };
}

describe("acceptance manifest", () => {
  it("contains all 146 unique acceptance IDs exactly once", async () => {
    const { manifest } = await fixture();
    expect(ACCEPTANCE_IDS).toHaveLength(146);
    expect(checkCoverage(manifest.acceptance_items)).toEqual({
      valid: true,
      unknown: [],
      missing: [],
      duplicates: [],
    });
  });

  it("uses only PASS, FAIL or BLOCKED and keeps the candidate commit", async () => {
    const { manifest } = await fixture();
    expect(new Set(manifest.acceptance_items.map((item) => item.status)))
      .toEqual(new Set(["PASS", "BLOCKED"]));
    expect(manifest.commit_sha).toBe("a".repeat(40));
    expect(manifest.acceptance_status).toBe("BLOCKED");
  });

  it("gives every PASS an existing evidence file with a matching SHA-256", async () => {
    const { manifest, releaseDir } = await fixture();
    for (const item of manifest.acceptance_items.filter((candidate) => candidate.status === "PASS")) {
      expect(item.evidence.length).toBeGreaterThan(0);
      for (const evidence of item.evidence) {
        const bytes = await readFile(join(releaseDir, evidence.path));
        expect(hash(bytes)).toBe(evidence.sha256);
      }
    }
  });

  it("records command, timestamps, exit code, output and output hash", async () => {
    const { manifest, releaseDir } = await fixture();
    expect(manifest.commands).toHaveLength(1);
    const command = manifest.commands[0];
    expect(command).toEqual(expect.objectContaining({
      command: "pnpm test",
      started_at: expect.stringContaining("T"),
      ended_at: expect.stringContaining("T"),
      exit_code: 0,
      raw_log: "commands/test.log",
      raw_log_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    }));
    expect(hash(await readFile(join(releaseDir, command.raw_log)))).toBe(command.raw_log_sha256);
  });

  it("detects sensitive fixtures without returning their value", () => {
    const providerKey = ["sk", "proj", "abcdefghijklmnop"].join("-");
    const findings = scanText(`student@example.com 13800138000 11010519491231002X ${providerKey}`);
    expect(findings.map((item) => item.type)).toEqual(expect.arrayContaining([
      "email", "china_mobile", "china_id", "provider_key",
    ]));
    expect(findings.every((item) => item.preview.startsWith("<redacted:"))).toBe(true);
    expect(JSON.stringify(findings)).not.toContain("student@example.com");
  });
});
