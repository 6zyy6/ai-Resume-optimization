import type { Page, Route } from "@playwright/test";

type Json = Record<string, unknown>;

function hasExactKeys(body: Json, keys: string[]) {
  return Object.keys(body).sort().join(",") === [...keys].sort().join(",");
}

function containsForbiddenPlaceholder(value: unknown): boolean {
  if (typeof value === "string") return value.toLowerCase().includes("demo");
  if (Array.isArray(value)) return value.some(containsForbiddenPlaceholder);
  return value !== null && typeof value === "object"
    ? Object.values(value).some(containsForbiddenPlaceholder)
    : false;
}

async function reply(route: Route, status: number, json: Json) {
  await route.fulfill({ contentType: "application/json", json, status });
}

export async function installStrictFixtureApi(page: Page, requests: string[]) {
  let uploaded = false;

  await page.route("https://upload.fixture/file_f01", async (route) => {
    const request = route.request();
    requests.push(`${request.method()} ${request.url()}`);
    if (request.method() !== "PUT" || !request.postDataBuffer()?.length) {
      return reply(route, 422, { error: { code: "UPLOAD_REQUIRED", details: {}, message: "Expected file bytes", request_id: "req_fixture" } });
    }
    uploaded = true;
    await route.fulfill({ status: 200 });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const key = `${request.method()} ${path}`;
    requests.push(key);
    const body = request.postDataJSON() as Json | null;
    const valid = (keys: string[]) => body !== null && !containsForbiddenPlaceholder(body) && hasExactKeys(body, keys);

    if (key === "POST /api/v1/facts" && valid(["kind", "sources", "status", "value"])) return reply(route, 201, { id: "fact_f01" });
    if (key === "POST /api/v1/resumes" && valid(["kind", "title"])) return reply(route, 201, { id: "resume_r01", version: 1 });
    if (key === "POST /api/v1/resumes/resume_r01/versions" && valid(["base_version", "claim_evidence", "snapshot"])) {
      return reply(route, 201, { id: "version_v01" });
    }
    if (key === "POST /api/v1/files/upload-tokens" && valid(["display_name", "mime", "purpose", "sha256", "size"])) {
      const sha256 = body?.sha256;
      if (typeof sha256 !== "string" || !/^[a-f0-9]{64}$/.test(sha256)) return reply(route, 422, { error: { code: "INVALID_HASH", details: {}, message: "Invalid SHA-256", request_id: "req_fixture" } });
      return reply(route, 201, { expires_in: 900, file_id: "file_f01", status: "pending", upload_url: "https://upload.fixture/file_f01" });
    }
    if (key === "POST /api/v1/files/file_f01/confirm-upload" && valid([]) && uploaded) return reply(route, 200, { id: "file_f01", status: "uploaded" });
    if (key === "POST /api/v1/imports" && valid(["file_id"]) && body?.file_id === "file_f01") return reply(route, 202, { id: "import_i01", status: "queued" });
    if (key === "POST /api/v1/imports/import_i01/confirm" && valid(["facts"])) return reply(route, 201, { fact_ids: ["fact_f01"], id: "import_i01", status: "confirmed" });
    if (key === "POST /api/v1/jobs" && valid(["raw", "title"])) return reply(route, 201, { id: "job_j01", status: "created", title: "目标岗位" });
    if (key === "POST /api/v1/jobs/job_j01/parse" && valid([])) return reply(route, 202, { id: "job_j01", status: "queued" });
    if (key === "POST /api/v1/match-analyses" && valid(["job_id", "resume_version_id"])
      && body?.job_id === "job_j01" && body?.resume_version_id === "version_v01") {
      return reply(route, 202, { id: "analysis_a01", status: "queued" });
    }
    if (key === "GET /api/v1/match-analyses/analysis_a01/suggestions") return reply(route, 200, { items: [{ id: "suggestion_s01" }] });
    if (/^POST \/api\/v1\/suggestions\/suggestion_s01\/(accept|ignore|revert)$/.test(key) && valid([])) return reply(route, 201, { status: "accepted" });
    if (key === "POST /api/v1/suggestions/suggestion_s01/edit" && valid(["text"])) return reply(route, 201, { status: "edited" });
    if (key === "POST /api/v1/exports" && valid(["resume_version_id", "template_version"]) && body?.resume_version_id === "version_v01") {
      return reply(route, 202, { id: "export_e01", status: "queued" });
    }
    if (key === "GET /api/v1/exports/export_e01") return reply(route, 200, { id: "export_e01", status: "succeeded" });

    if (path.startsWith("/api/v1/")) {
      return reply(route, body === null ? 404 : 422, { error: { code: "FIXTURE_REJECTED", details: { key }, message: "Unknown path or invalid body", request_id: "req_fixture" } });
    }
    return reply(route, 404, { error: { code: "NOT_FOUND", details: {}, message: "Not found", request_id: "req_fixture" } });
  });
}
