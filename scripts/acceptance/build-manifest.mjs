import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { relative } from "node:path";
import { ACCEPTANCE_IDS, checkCoverage } from "./check-coverage.mjs";

const sha = (value) => createHash("sha256").update(value).digest("hex");

export async function buildManifest({ releaseDir, releaseId, commitSha, blockerPath, passEvidence = {} }) {
  const commandDir = `${releaseDir}/commands`;
  const metadataFiles = (await readdir(commandDir)).filter((file) => file.endsWith(".json")).sort();
  const commands = [];
  for (const file of metadataFiles) {
    const value = JSON.parse(await readFile(`${commandDir}/${file}`, "utf8"));
    value.raw_log = relative(releaseDir, value.raw_log);
    commands.push(value);
  }
  const blocker = await readFile(blockerPath);
  const blockerEvidence = { path: relative(releaseDir, blockerPath), sha256: sha(blocker) };
  const acceptanceItems = ACCEPTANCE_IDS.map((id) => {
    const evidence = passEvidence[id];
    return evidence
      ? { id, status: "PASS", evidence: [evidence] }
      : { id, status: "BLOCKED", evidence: [blockerEvidence] };
  });
  const coverage = checkCoverage(acceptanceItems);
  if (!coverage.valid) throw new Error(`acceptance coverage invalid: ${JSON.stringify(coverage)}`);
  const blockedDigest = `sha256:${sha("BLOCKED: container image was not built in this environment")}`;
  const manifest = {
    release_id: releaseId,
    commit_sha: commitSha,
    created_at: new Date().toISOString(),
    scope: ["local-verification"],
    acceptance_status: "BLOCKED",
    web_image_digest: blockedDigest,
    api_image_digest: blockedDigest,
    pi_image_digest: blockedDigest,
    worker_image_digest: blockedDigest,
    miniprogram_build_version: commitSha.slice(0, 12),
    database_schema_version: "0006",
    prompt_version: "resume-workflow-v1",
    workflow_version: "1",
    model_route_version: "environment-controlled",
    template_version: "clear-standard,modern-whitespace",
    test_environment: `${process.platform}-${process.arch}; external evidence unavailable`,
    executor: process.env.USER ?? "local-codex",
    reviewer: "independent-review-pending",
    open_severity_counts: { sev1: 0, sev2: 0, sev3: 0, sev4: 0 },
    commands,
    acceptance_items: acceptanceItems,
  };
  await writeFile(`${releaseDir}/manifest.json`, `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}
