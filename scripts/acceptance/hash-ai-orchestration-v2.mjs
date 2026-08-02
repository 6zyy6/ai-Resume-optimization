import { createHash } from "node:crypto";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { basename, relative, resolve } from "node:path";

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
const expectedStates = new Map([
  ["01-create-analysis", /^\/create$/],
  ["02-candidate-confirmation", /^\/create$/],
  ["03-model-draft-provenance", /^\/resumes\/resume_[^/]+\/edit$/],
  ["04-jd-provenance", /^\/jobs\/new$/],
  ["05-match-categories", /^\/jobs\/job_[^/]+\/match$/],
  ["06-pending-suggestion", /^\/suggestions\/match_[^/]+$/],
  ["07-blocked-suggestion", /^\/suggestions\/match_[^/]+$/],
  ["08-task-success-result", /^\/tasks$/],
  ["09-recoverable-failure", /^\/create$/],
]);
const expectedWorkflows = [
  "analyze_intake_answer",
  "compose_resume_draft",
  "generate_suggestions_batch",
  "match_resume_to_jd",
  "parse_jd",
];
const sha256Pattern = /^[a-f0-9]{64}$/;

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

function sameValues(actual, expected) {
  return actual.length === expected.length
    && actual.every((value, index) => value === expected[index]);
}

const metadata = JSON.parse(await readFile(resolve(evidenceDir, "run-metadata.json"), "utf8"));
if (metadata.exit_code !== 0) throw new Error("Playwright evidence run did not pass");
for (const field of [
  "browser",
  "build_id",
  "command",
  "commit_sha",
  "ended_at",
  "environment",
  "source_commit",
  "started_at",
]) {
  if (typeof metadata[field] !== "string" || !metadata[field]) {
    throw new Error(`Run metadata is missing ${field}`);
  }
}
if (metadata.commit_sha !== metadata.source_commit) {
  throw new Error("Run metadata commit does not match the source commit");
}
if (metadata.build_id !== metadata.source_commit) {
  throw new Error("Local evidence build ID must equal the immutable source commit");
}
if (
  !Number.isFinite(Date.parse(metadata.started_at))
  || !Number.isFinite(Date.parse(metadata.ended_at))
  || Date.parse(metadata.ended_at) < Date.parse(metadata.started_at)
) {
  throw new Error("Run metadata has invalid UTC timestamps");
}
const identities = JSON.parse(await readFile(resolve(evidenceDir, "service-identities.json"), "utf8"));
for (const service of ["web", "api", "pi"]) {
  if (identities[service].commit_sha !== metadata.source_commit) {
    throw new Error(`${service} identity does not match the tested source commit`);
  }
}

const reports = [];
const screenshots = [];
const nonceHashes = new Set();
for (const [label, viewport] of expectedViewports) {
  const reportPath = resolve(evidenceDir, label, "api-db-report.json");
  const report = JSON.parse(await readFile(reportPath, "utf8"));
  if (
    report.viewport?.label !== label
    || report.viewport?.width !== viewport.width
    || report.viewport?.height !== viewport.height
  ) {
    throw new Error(`${label} report viewport does not match its directory`);
  }
  if (report.browser !== metadata.browser || report.build_id !== metadata.build_id) {
    throw new Error(`${label} report browser or build ID does not match run metadata`);
  }
  if (!sha256Pattern.test(report.run_nonce_hash)) {
    throw new Error(`${label} is missing its hashed visible run nonce`);
  }
  nonceHashes.add(report.run_nonce_hash);
  if (
    report.console_errors.length
    || report.page_errors.length
    || report.server_errors.length
  ) {
    throw new Error(`${label} contains a console, page, or API 5xx error`);
  }
  if (report.broker_status !== "BLOCKED_NO_REDIS") {
    throw new Error(`${label} must disclose the missing Redis/Celery topology`);
  }
  const captureNames = report.captures.map(({ name }) => name);
  if (!sameValues(captureNames, [...expectedStates.keys()])) {
    throw new Error(`${label} does not contain the exact nine required states`);
  }
  for (const capture of report.captures) {
    if (!expectedStates.get(capture.name).test(capture.route)) {
      throw new Error(`${label}/${capture.name} has an unexpected route`);
    }
    if (
      capture.metrics.horizontal_overflow
      || capture.metrics.runtime_error
      || capture.metrics.visible_internal_markers.length
      || !sha256Pattern.test(capture.metrics.visible_proof_hash)
      || !["input", "text"].includes(capture.metrics.visible_proof_kind)
    ) {
      throw new Error(`${label}/${capture.name} contains an invalid rendered state`);
    }
  }
  const nonceVisibleStates = report.captures
    .filter(({ metrics }) => metrics.run_nonce_visible)
    .map(({ name }) => name);
  const expectedNonceStates = [...expectedStates.keys()]
    .filter((name) => name !== "08-task-success-result");
  if (!sameValues(nonceVisibleStates, expectedNonceStates)) {
    throw new Error(`${label} must show its unique run nonce in states 01-07 and 09`);
  }

  if (report.task_assertions.length !== 4) {
    throw new Error(`${label} must contain exactly four successful Task assertions`);
  }
  const workflows = [];
  for (const assertion of report.task_assertions) {
    const state = assertion.state;
    if (
      assertion.owner_scope_hash !== report.owner_scope_hash
      || state.task_status !== "succeeded"
      || !state.task_trace_id
      || !state.outbox_exists
      || state.outbox_dispatched
      || !state.outbox_owner_matches
      || state.orphan_trace_count !== 0
      || !Array.isArray(state.task_event_sequences)
      || !state.task_event_sequences.every((value, index) => value === index + 1)
      || state.task_event_stages.at(-1) !== "succeeded"
      || !Array.isArray(state.runs)
      || state.runs.length === 0
    ) {
      throw new Error(`${label}/${assertion.task_id} has an invalid Task owner or terminal state`);
    }
    let traceCount = 0;
    for (const run of state.runs) {
      const sequence = run.trace_sequence;
      if (
        run.owner_user_id !== report.owner_scope_hash
        || run.task_id !== assertion.task_id
        || run.status !== "succeeded"
        || run.trace_id !== state.task_trace_id
        || run.workflow_version !== "2"
        || typeof run.prompt_template_version !== "string"
        || !run.prompt_template_version.endsWith("@2")
        || !sha256Pattern.test(run.input_hash)
        || !sha256Pattern.test(run.receipt_hash)
        || typeof run.result_ref !== "string"
        || !run.result_ref
        || !Array.isArray(sequence)
        || sequence.length === 0
        || !sequence.every((value, index) => value === index + 1)
        || run.trace_types[0] !== "run_queued"
        || run.trace_types.at(-1) !== "run_succeeded"
      ) {
        throw new Error(`${label}/${assertion.task_id} has an invalid AiRun or Trace chain`);
      }
      traceCount += sequence.length;
      workflows.push(run.workflow_type);
    }
    if (state.trace_count !== traceCount) {
      throw new Error(`${label}/${assertion.task_id} trace count does not match its runs`);
    }
  }
  if (!sameValues(workflows.sort(), expectedWorkflows)) {
    throw new Error(`${label} does not prove the five required AI workflows`);
  }
  const taskSuccessCapture = report.captures.find(
    ({ name }) => name === "08-task-success-result",
  );
  const taskSuccessAssertion = report.task_assertions.find(
    ({ task_id: taskId }) => taskId === taskSuccessCapture.evidence_basis?.task_id,
  );
  if (
    taskSuccessCapture.evidence_basis?.task_status !== "succeeded"
    || taskSuccessAssertion?.state?.task_status !== "succeeded"
  ) {
    throw new Error(`${label} task success screenshot is not bound to a succeeded Task`);
  }
  if (
    report.failure_task_assertion?.owner_scope_hash !== report.owner_scope_hash
    || report.failure_task_assertion?.state?.task_status !== "failed"
    || !report.failure_task_assertion.state.task_trace_id
    || report.failure_task_assertion.state.runs.length !== 0
    || report.failure_task_assertion.state.trace_count !== 0
    || !report.failure_task_assertion.state.outbox_exists
    || report.failure_task_assertion.state.outbox_dispatched
    || !report.failure_task_assertion.state.outbox_owner_matches
    || report.failure_task_assertion.state.orphan_trace_count !== 0
    || !report.failure_task_assertion.state.task_event_sequences.every(
      (value, index) => value === index + 1,
    )
    || report.failure_task_assertion.state.task_event_stages.at(-1) !== "failed"
  ) {
    throw new Error(`${label} is missing the recoverable failed Task assertion`);
  }
  reports.push({ path: relative(evidenceDir, reportPath), viewport });

  const imageFiles = (await files(resolve(evidenceDir, label)))
    .filter((path) => path.endsWith(".png"));
  const expectedImageNames = [...expectedStates.keys()].map((name) => `${name}.png`);
  if (!sameValues(imageFiles.map((path) => basename(path)).sort(), expectedImageNames.sort())) {
    throw new Error(`${label} must contain the exact nine required screenshots`);
  }
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
if (nonceHashes.size !== expectedViewports.size) {
  throw new Error("Every viewport run must use a unique visible nonce");
}
if (new Set(screenshots.map(({ sha256: hash }) => hash)).size !== screenshots.length) {
  throw new Error("Every state screenshot must have unique image content");
}

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
  browser: metadata.browser,
  build_id: metadata.build_id,
  checks: {
    api_5xx_count: 0,
    ai_run_count_per_viewport: 5,
    console_error_count: 0,
    horizontal_overflow_count: 0,
    internal_marker_count: 0,
    nonce_visible_state_count_per_viewport: 8,
    page_error_count: 0,
    screenshot_count: screenshots.length,
    state_count_per_viewport: 9,
    successful_task_count_per_viewport: 4,
  },
  command: metadata.command,
  commit_sha: metadata.source_commit,
  created_at: new Date().toISOString(),
  environment: metadata.environment,
  evidence: manifestFiles.map((file) => ({
    kind: file.path.endsWith(".png") ? "screenshot" : "report",
    path: file.path,
    sha256: file.sha256,
  })),
  evidence_status: "PASS",
  exit_code: metadata.exit_code,
  external_gates: metadata.external_gates,
  files: manifestFiles,
  finished_at: metadata.ended_at,
  id: "V2-AI-ORCHESTRATION-LOCAL-HTTP",
  note: "PASS is limited to deterministic local HTTP orchestration evidence; every external gate remains BLOCKED.",
  priority: "P0",
  reports,
  screenshots,
  source_commit: metadata.source_commit,
  started_at: metadata.started_at,
  status: "PASS",
  tested_environment: metadata.environment,
};
await writeFile(
  resolve(evidenceDir, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
);
console.log(`Hashed ${screenshots.length} AI orchestration screenshots for ${metadata.source_commit}`);
