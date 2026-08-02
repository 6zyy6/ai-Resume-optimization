import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EditorPage from "../app/resumes/[id]/edit/page";
import ExportPage from "../app/exports/[id]/page";
import ImportConfirmPage from "../app/imports/[id]/confirm/page";
import MatchPage from "../app/jobs/[id]/match/page";
import NewJobPage from "../app/jobs/new/page";
import VersionsPage from "../app/resumes/[id]/versions/page";
import SuggestionsPage from "../app/suggestions/[analysisId]/page";

const push = vi.fn();
const replace = vi.fn();
let params: Record<string, string> = {};

vi.mock("next/navigation", () => ({
  useParams: () => params,
  usePathname: () => "/workflow",
  useRouter: () => ({ push, replace }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

beforeEach(() => {
  push.mockReset();
  replace.mockReset();
  params = {};
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("V2 real workflow pages", () => {
  it("initializes the editor from the latest server snapshot without creating self-proving facts", async () => {
    params = { id: "resume_unique" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_editor", identity_type: "email" });
      }
      if (path.endsWith("/v1/resumes/resume_unique")) {
        return jsonResponse({ id: "resume_unique", kind: "base", title: "唯一编辑器标题", version: 3, base_resume_id: null, job_description_id: null });
      }
      if (path.endsWith("/v1/resumes/resume_unique/versions?limit=1")) {
        return jsonResponse({
          items: [{
            created_at: "2026-07-30T00:00:00Z",
            id: "rver_unique",
            operation: "save",
            parent_version_id: null,
            resume_id: "resume_unique",
            snapshot: {
              schema_version: "1",
              sections: [{
                id: "project",
                items: [{ fact_refs: ["fact_source"], id: "bullet_unique", text: "唯一服务端经历内容" }],
                title: "项目经历",
                type: "EXPERIENCE",
              }],
              target: null,
              title: "唯一编辑器标题",
            },
            snapshot_hash: "hash_unique",
          }],
          next_cursor: null,
        });
      }
      if (path.endsWith("/v1/resumes/resume_unique/quality-checks")) {
        return jsonResponse({
          issues: [{
            code: "CLAIM_EVIDENCE_FACT_MISMATCH",
            message: "Claim evidence does not support high-risk claim entities",
            path: "claim_evidence",
          }, {
            code: "UNEXPECTED_INTERNAL_QUALITY_CODE",
            message: "Unexpected internal quality message",
            path: "sections.0",
          }],
        });
      }
      if (path.endsWith("/v1/facts?limit=100")) {
        return jsonResponse({
          items: [{
            confirmed_at: "2026-07-30T00:00:01Z",
            id: "fact_source",
            kind: "project",
            source_ids: ["src_explicit"],
            status: "confirmed",
            value: "用户明确修改后的内容",
          }],
          next_cursor: null,
        });
      }
      if (init?.method === "POST" && path.endsWith("/v1/resumes/resume_unique/versions")) {
        return jsonResponse({
          created_at: "2026-07-30T00:00:01Z",
          id: "rver_saved",
          operation: "save",
          parent_version_id: "rver_unique",
          resume_id: "resume_unique",
          snapshot: JSON.parse(String(init.body)).snapshot,
          snapshot_hash: "hash_saved",
        }, 201);
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EditorPage />);

    expect(await screen.findByDisplayValue("唯一服务端经历内容")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /唯一编辑器标题/ })).toBeInTheDocument();
    expect(screen.getByText("经历模块")).toBeInTheDocument();
    expect(screen.getByText("事实依据需要核对")).toBeInTheDocument();
    expect(screen.getByText("当前简历表述与已确认事实不完全一致，请检查内容或重新关联事实。")).toBeInTheDocument();
    expect(screen.getByText("简历内容需要核对")).toBeInTheDocument();
    expect(screen.getByText("系统发现一项需要处理的内容，请检查相关简历表述后重试。")).toBeInTheDocument();
    expect(screen.queryByText(/experience/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/CLAIM_EVIDENCE_FACT_MISMATCH/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Claim evidence does not support/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/UNEXPECTED_INTERNAL_QUALITY_CODE/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Unexpected internal quality message/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/课程项目的用户调研/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByDisplayValue("唯一服务端经历内容"), {
      target: { value: "用户明确修改后的内容" },
    });
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/v1/facts"))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/v1/resumes/resume_unique/versions"))).toBe(false);

    fireEvent.change(screen.getByRole("combobox", { name: "关联已有确认事实" }), {
      target: { value: "fact_source" },
    });
    fireEvent.click(screen.getByRole("button", { name: "关联事实" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/v1/resumes/resume_unique/versions"))).toBe(true);
    }, { timeout: 2_000 });
    expect(fetchMock.mock.calls.some(([url, init]) => (
      String(url).endsWith("/v1/facts") && init?.method === "POST"
    ))).toBe(false);
  });

  it("advances the offline draft baseline after autosave and restores the next edit on refresh", async () => {
    params = { id: "resume_draft_baseline" };
    const stored = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      clear: () => stored.clear(),
      getItem: (key: string) => stored.get(key) ?? null,
      key: (index: number) => [...stored.keys()][index] ?? null,
      get length() { return stored.size; },
      removeItem: (key: string) => stored.delete(key),
      setItem: (key: string, value: string) => stored.set(key, value),
    });
    let serverVersion = 3;
    let serverText = "服务端第三版";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_draft", identity_type: "email" });
      }
      if (path.endsWith("/v1/resumes/resume_draft_baseline")) {
        return jsonResponse({ id: "resume_draft_baseline", kind: "base", title: "草稿基线", version: serverVersion, base_resume_id: null, job_description_id: null });
      }
      if (path.endsWith("/v1/resumes/resume_draft_baseline/versions?limit=1")) {
        return jsonResponse({ items: [{
          created_at: "2026-07-30T00:00:00Z",
          id: `rver_${serverVersion}`,
          operation: "save",
          parent_version_id: null,
          resume_id: "resume_draft_baseline",
          snapshot: {
            schema_version: "1",
            sections: [{ id: "project", items: [{ fact_refs: ["fact_draft"], id: "bullet_draft", text: serverText }], title: "项目", type: "project" }],
            target: null,
            title: "草稿基线",
          },
          snapshot_hash: `hash_${serverVersion}`,
        }], next_cursor: null });
      }
      if (path.endsWith("/quality-checks")) return jsonResponse({ issues: [] });
      if (path.endsWith("/v1/facts?limit=100")) {
        return jsonResponse({ items: [{ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_draft", kind: "project", source_ids: ["src_draft"], status: "confirmed", value: "事实" }], next_cursor: null });
      }
      if (path.endsWith("/v1/resumes/resume_draft_baseline/versions") && init?.method === "POST") {
        serverVersion = 4;
        serverText = JSON.parse(String(init.body)).snapshot.sections[0].items[0].text;
        return jsonResponse({ created_at: "2026-07-30T00:01:00Z", id: "rver_4", operation: "save", parent_version_id: "rver_3", resume_id: "resume_draft_baseline", snapshot: JSON.parse(String(init.body)).snapshot, snapshot_hash: "hash_4" }, 201);
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    const first = render(<EditorPage />);
    fireEvent.change(await screen.findByDisplayValue("服务端第三版"), { target: { value: "已保存第四版" } });
    fireEvent.change(screen.getByRole("combobox", { name: "关联已有确认事实" }), { target: { value: "fact_draft" } });
    fireEvent.click(screen.getByRole("button", { name: "关联事实" }));
    await waitFor(() => expect(serverVersion).toBe(4), { timeout: 2_000 });

    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    fireEvent.change(screen.getByDisplayValue("已保存第四版"), { target: { value: "第四版后的离线编辑" } });
    await waitFor(() => {
      const draft = JSON.parse(stored.get("resume-draft:usr_draft:resume_draft_baseline") ?? "{}");
      expect(draft.base_version).toBe(4);
      expect(draft.snapshot.sections[0].items[0].text).toBe("第四版后的离线编辑");
    });
    first.unmount();

    render(<EditorPage />);
    expect(await screen.findByDisplayValue("第四版后的离线编辑")).toBeInTheDocument();
    expect(screen.getByText(/已恢复这台设备/)).toBeInTheDocument();
  });

  it("renders only the draft facts returned by GET /v1/imports/:id", async () => {
    params = { id: "imp_unique" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_import", identity_type: "email" });
      }
      if (path.endsWith("/v1/imports/imp_unique")) {
        return jsonResponse({
          draft_facts: [{ kind: "project", value: "唯一导入事实丁" }],
          fact_ids: [],
          fallback_reason: null,
          file_id: "file_unique",
          id: "imp_unique",
          status: "parsed",
          task_id: "task_import",
        });
      }
      if (path.endsWith("/v1/imports/imp_unique/confirm") && init?.method === "POST") {
        return jsonResponse({
          draft_facts: [{ kind: "project", value: "唯一导入事实丁" }],
          fact_ids: ["fact_imported"],
          fallback_reason: null,
          file_id: "file_unique",
          id: "imp_unique",
          resume_id: "resume_imported",
          status: "confirmed",
          task_id: "task_import",
          version_id: "rver_imported",
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ImportConfirmPage />);

    expect(await screen.findByDisplayValue("唯一导入事实丁")).toBeInTheDocument();
    expect(screen.queryByText("基本信息")).not.toBeInTheDocument();
    expect(screen.queryByText(/待用户确认/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认并创建基础简历" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(
      "/resumes/resume_imported/edit?version=rver_imported",
    ));
    expect(fetchMock.mock.calls.some(([url, init]) => (
      String(url).endsWith("/v1/resumes") && init?.method === "POST"
    ))).toBe(false);
  });

  it("opens an already finalized import instead of rendering a stale confirmation form", async () => {
    params = { id: "imp_confirmed" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/imports/imp_confirmed")) {
        return jsonResponse({
          draft_facts: [{ kind: "project", value: "已确认字段" }],
          fact_ids: ["fact_confirmed"],
          fallback_reason: null,
          file_id: "file_confirmed",
          id: "imp_confirmed",
          resume_id: "resume_confirmed",
          status: "confirmed",
          task_id: "task_confirmed",
          version_id: "rver_confirmed",
        });
      }
      return jsonResponse({}, 404);
    }));

    render(<ImportConfirmPage />);

    await waitFor(() => expect(push).toHaveBeenCalledWith(
      "/resumes/resume_confirmed/edit?version=rver_confirmed",
    ));
    expect(screen.queryByRole("button", { name: "确认并创建基础简历" })).not.toBeInTheDocument();
  });

  it("recovers finalized import ids after an uncertain confirm replay", async () => {
    params = { id: "imp_replay" };
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/imports/imp_replay") && init?.method === "GET") {
        reads += 1;
        return jsonResponse(reads === 1
          ? {
              draft_facts: [{ kind: "project", value: "待确认字段" }],
              fact_ids: [],
              fallback_reason: null,
              file_id: "file_replay",
              id: "imp_replay",
              resume_id: null,
              status: "parsed",
              task_id: "task_replay",
              version_id: null,
            }
          : {
              draft_facts: [{ kind: "project", value: "待确认字段" }],
              fact_ids: ["fact_replay"],
              fallback_reason: null,
              file_id: "file_replay",
              id: "imp_replay",
              resume_id: "resume_replay",
              status: "confirmed",
              task_id: "task_replay",
              version_id: "rver_replay",
            });
      }
      if (path.endsWith("/v1/imports/imp_replay/confirm") && init?.method === "POST") {
        return jsonResponse({
          error: {
            code: "IMPORT_ALREADY_FINALIZED",
            details: {},
            message: "Import has already been finalized",
            request_id: "req_replay",
          },
        }, 409);
      }
      return jsonResponse({}, 404);
    }));

    render(<ImportConfirmPage />);
    fireEvent.click(await screen.findByRole("button", { name: "确认并创建基础简历" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith(
      "/resumes/resume_replay/edit?version=rver_replay",
    ));
  });

  it("renders suggestion fields from the API and routes decisions to the returned version", async () => {
    params = { analysisId: "analysis_unique" };
    window.history.replaceState({}, "", "/suggestions/analysis_unique?suggestion=suggestion_unique");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_suggestion", identity_type: "email" });
      }
      if (path.endsWith("/v1/match-analyses/analysis_unique/suggestions")) {
        return jsonResponse({ items: [{
          fact_refs: ["fact_unique"],
          id: "suggestion_unique",
          original_text: "唯一原文戊",
          reason: "唯一理由己",
          requirement_id: "req_unique",
          requirement_text: "唯一岗位要求庚",
          risk_flags: ["missing_metric"],
          status: "pending",
          suggested_text: "唯一建议辛",
          target_path: "/sections/0/items/0/text",
        }] });
      }
      if (path.endsWith("/v1/facts/fact_unique/sources")) {
        return jsonResponse({ items: [{ content: "唯一来源文本", source_ref: "intake:1", source_type: "question_answer" }] });
      }
      if (path.endsWith("/v1/facts/fact_unique")) {
        return jsonResponse({ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_unique", kind: "project", source_ids: ["source_unique"], status: "confirmed", value: "唯一事实内容" });
      }
      if (path.endsWith("/v1/suggestions/suggestion_unique/accept") && init?.method === "POST") {
        return jsonResponse({ decision_id: "decision_unique", status: "accepted", suggestion_id: "suggestion_unique", version_id: "rver_decision_nonce" }, 201);
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SuggestionsPage />);

    expect(await screen.findByText("唯一岗位要求庚")).toBeInTheDocument();
    expect(screen.getByText("唯一原文戊")).toBeInTheDocument();
    expect(screen.getByText("唯一建议辛")).toBeInTheDocument();
    expect(screen.getByText(/唯一理由己/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "接受建议" }));
    expect(await screen.findByRole("link", { name: "进入导出" })).toHaveAttribute(
      "href",
      "/exports/new?version=rver_decision_nonce",
    );
  });

  it("navigates a suggestion batch and limits blocked items to evidence or ignore", async () => {
    params = { analysisId: "analysis_batch" };
    window.history.replaceState({}, "", "/suggestions/analysis_batch");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/match-analyses/analysis_batch/suggestions")) {
        return jsonResponse({ items: [
          {
            ai_run_id: "run_suggestion",
            fact_links: [{ claim_range: { end: 5, start: 0 }, fact_id: "fact_pending" }],
            fact_refs: ["fact_pending"],
            generation_mode: "model",
            id: "suggestion_pending",
            input_hash: "a".repeat(64),
            original_hash: "b".repeat(64),
            original_text: "第一条原文",
            reason: "第一条理由",
            requirement_id: "req_pending",
            requirement_text: "第一条岗位要求",
            risk_flags: [],
            status: "pending",
            suggested_text: "第一条建议",
            target_path: "/sections/0/items/0/text",
            updated_at: "2026-07-30T08:00:00Z",
            workflow_version: "2",
          },
          {
            ai_run_id: "run_suggestion",
            fact_links: [{ claim_range: { end: 6, start: 0 }, fact_id: "fact_blocked" }],
            fact_refs: ["fact_blocked"],
            generation_mode: "model",
            id: "suggestion_blocked",
            input_hash: "c".repeat(64),
            original_hash: "d".repeat(64),
            original_text: "第二条原文证据",
            reason: "还缺少结果来源",
            requirement_id: "req_blocked",
            requirement_text: "第二条岗位要求证据",
            risk_flags: ["missing_result_source"],
            status: "blocked",
            suggested_text: "补充事实后才可采用的建议",
            target_path: "/sections/0/items/1/text",
            updated_at: "2026-07-30T08:01:00Z",
            workflow_version: "2",
          },
        ] });
      }
      if (path.endsWith("/v1/facts/fact_pending/sources")) return jsonResponse({ items: [] });
      if (path.endsWith("/v1/facts/fact_pending")) {
        return jsonResponse({ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_pending", kind: "project", source_ids: [], status: "confirmed", value: "第一条事实" });
      }
      if (path.endsWith("/v1/facts/fact_blocked/sources")) {
        return jsonResponse({ items: [{ content: "第二条事实来源", source_ref: "intake:2", source_type: "question_answer" }] });
      }
      if (path.endsWith("/v1/facts/fact_blocked")) {
        return jsonResponse({ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_blocked", kind: "result", source_ids: ["source_blocked"], status: "confirmed", value: "第二条事实证据" });
      }
      return jsonResponse({}, 404);
    }));

    render(<SuggestionsPage />);

    expect(await screen.findByText("第一条岗位要求")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一条建议" }));
    expect(await screen.findByText("第二条原文证据")).toBeInTheDocument();
    expect(screen.getByText("第二条岗位要求证据")).toBeInTheDocument();
    expect(screen.getByText("补充事实后才可采用的建议")).toBeInTheDocument();
    expect(screen.getByText(/还缺少结果来源/)).toBeInTheDocument();
    expect(await screen.findByText("第二条事实证据")).toBeInTheDocument();
    expect(screen.getByText("风险：缺少结果来源")).toBeInTheDocument();
    expect(screen.queryByText(/missing_result_source/)).not.toBeInTheDocument();
    expect(screen.getByText("修改位置：简历第 1 个模块 · 第 2 条内容")).toBeInTheDocument();
    expect(screen.queryByText(/\/sections\//)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "接受建议" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑后接受" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "补充事实" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "忽略建议" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "上一条建议" })).toBeEnabled();
  });

  it("renders parsed JD requirements from the server and confirms the real rows", async () => {
    window.history.replaceState({}, "", "/jobs/new?version=rver_job_seed");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_job", identity_type: "email" });
      }
      if (path.endsWith("/v1/jobs") && init?.method === "POST") {
        return jsonResponse({ company: "唯一公司壬", id: "job_unique", requirements: [], status: "draft", task_id: null, title: "唯一岗位癸" }, 201);
      }
      if (path.endsWith("/v1/jobs/job_unique/parse") && init?.method === "POST") {
        return jsonResponse({ company: "唯一公司壬", id: "job_unique", requirements: [], status: "queued", task_id: "task_job", title: "唯一岗位癸" }, 202);
      }
      if (path.endsWith("/v1/tasks/task_job")) {
        return jsonResponse({ cancellation_requested: false, error_code: null, id: "task_job", progress: 100, result_ref: "job_unique", stage: "completed", status: "succeeded", trace_id: "tr_job", type: "parse_job" });
      }
      if (path.endsWith("/v1/jobs/job_unique") && init?.method === "GET") {
        return jsonResponse({ company: "唯一公司壬", id: "job_unique", raw: "岗位正文", requirements: [{ ai_run_id: null, confidence_band: "high", confirmed: false, explicitness: "explicit", generation_mode: "rule_fallback", id: "req_unique", input_hash: "a".repeat(64), priority: 1, source_hash: "b".repeat(64), source_range: { end: 4, start: 0 }, text: "唯一岗位要求子", type: "must_have", workflow_version: "2" }], status: "parsed", task_id: "task_job", title: "唯一岗位癸" });
      }
      if (path.endsWith("/v1/jobs/job_unique/requirements/req_unique") && init?.method === "PATCH") {
        return jsonResponse({ confirmed: true, id: "req_unique", priority: 1, text: "唯一岗位要求子", type: "must_have" });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<NewJobPage />);
    fireEvent.change(await screen.findByRole("textbox", { name: "岗位名称" }), { target: { value: "唯一岗位癸" } });
    fireEvent.change(screen.getByRole("textbox", { name: "公司（可选）" }), { target: { value: "唯一公司壬" } });
    fireEvent.change(screen.getByRole("textbox", { name: "JD 原文" }), { target: { value: "岗位正文" } });
    fireEvent.click(screen.getByRole("button", { name: "解析岗位要求" }));

    expect(await screen.findByDisplayValue("唯一岗位要求子")).toBeInTheDocument();
    expect(screen.getByText("高置信")).toBeInTheDocument();
    expect(screen.getByText("原文字符 0–4")).toBeInTheDocument();
    expect(screen.getByText("基础解析")).toBeInTheDocument();
    expect(screen.queryByText("AI 已完成")).not.toBeInTheDocument();
    expect(screen.queryByText("用户研究与分析")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认全部要求" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => (
      String(url).endsWith("/v1/jobs/job_unique/requirements/req_unique") && init?.method === "PATCH"
    ))).toBe(true));
  });

  it.each(["queued", "processing"] as const)("restores and polls an existing %s job without creating another parse task", async (status) => {
    window.history.replaceState({}, "", "/jobs/new?version=rver_restore&job=job_restore");
    let jobReads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_job", identity_type: "email" });
      }
      if (path.endsWith("/v1/jobs/job_restore") && init?.method === "GET") {
        jobReads += 1;
        return jsonResponse(jobReads === 1
          ? { company: null, id: "job_restore", requirements: [], status, task_id: "task_restore", title: "恢复岗位" }
          : { company: null, id: "job_restore", requirements: [{ confirmed: false, id: "req_restore", priority: 1, text: "恢复后的真实要求", type: "must_have" }], status: "parsed", task_id: "task_restore", title: "恢复岗位" });
      }
      if (path.endsWith("/v1/tasks/task_restore")) {
        return jsonResponse({ cancellation_requested: false, error_code: null, id: "task_restore", progress: 100, result_ref: "job_restore", stage: "succeeded", status: "succeeded", trace_id: "tr_restore", type: "parse_job" });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<NewJobPage />);

    expect(await screen.findByDisplayValue("恢复后的真实要求")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url, init]) => (
      String(url).endsWith("/v1/jobs/job_restore/parse") && init?.method === "POST"
    ))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => (
      String(url).endsWith("/v1/tasks/task_restore")
    ))).toBe(true);
  });

  it("rotates the job parse key only after a confirmed terminal failure", async () => {
    window.history.replaceState({}, "", "/jobs/new?version=rver_retry");
    let parseCalls = 0;
    const keys: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse({ consent_versions: {}, identity_type: "email", masked_email: "3***@qq.com", user_id: "usr_retry" });
      if (path.endsWith("/v1/jobs") && init?.method === "POST") return jsonResponse({ company: null, id: "job_retry", raw: "岗位正文", requirements: [], status: "draft", task_id: null, title: "重试岗位" }, 201);
      if (path.endsWith("/v1/jobs/job_retry/parse") && init?.method === "POST") {
        parseCalls += 1;
        keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        return jsonResponse({ company: null, id: "job_retry", raw: "岗位正文", requirements: [], status: "queued", task_id: `task_retry_${parseCalls}`, title: "重试岗位" }, 202);
      }
      if (path.endsWith("/v1/tasks/task_retry_1")) return jsonResponse({ error_code: "MODEL_FAILED", id: "task_retry_1", status: "failed" });
      if (path.endsWith("/v1/tasks/task_retry_2")) return jsonResponse({ error_code: null, id: "task_retry_2", status: "succeeded" });
      if (path.endsWith("/v1/jobs/job_retry") && init?.method === "GET") return jsonResponse({ company: null, id: "job_retry", raw: "岗位正文", requirements: [{ confirmed: false, id: "req_retry", priority: 1, text: "成功要求", type: "must_have" }], status: "parsed", task_id: "task_retry_2", title: "重试岗位" });
      return jsonResponse({}, 404);
    }));

    render(<NewJobPage />);
    fireEvent.change(await screen.findByRole("textbox", { name: "岗位名称" }), { target: { value: "重试岗位" } });
    fireEvent.change(screen.getByRole("textbox", { name: "JD 原文" }), { target: { value: "岗位正文" } });
    fireEvent.click(screen.getByRole("button", { name: "解析岗位要求" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "解析岗位要求" }));

    expect(await screen.findByDisplayValue("成功要求")).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("rotates the match key only after a confirmed terminal failure", async () => {
    window.history.replaceState({}, "", "/jobs/new?version=rver_match_retry&job=job_match_retry");
    let matchCalls = 0;
    const keys: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse({ consent_versions: {}, identity_type: "email", masked_email: "3***@qq.com", user_id: "usr_match_retry" });
      if (path.endsWith("/v1/jobs/job_match_retry") && init?.method === "GET") {
        return jsonResponse({ company: null, id: "job_match_retry", raw: "岗位正文", requirements: [{ confirmed: true, id: "req_match_retry", priority: 1, text: "已确认岗位要求", type: "must_have" }], status: "parsed", task_id: null, title: "匹配重试岗位" });
      }
      if (path.endsWith("/v1/match-analyses") && init?.method === "POST") {
        matchCalls += 1;
        keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        return jsonResponse({ id: `analysis_match_${matchCalls}`, status: "queued", task_id: `task_match_${matchCalls}` }, 202);
      }
      if (path.endsWith("/v1/tasks/task_match_1")) return jsonResponse({ error_code: "MODEL_FAILED", id: "task_match_1", status: "failed" });
      if (path.endsWith("/v1/tasks/task_match_2")) return jsonResponse({ error_code: null, id: "task_match_2", status: "succeeded" });
      return jsonResponse({}, 404);
    }));

    render(<NewJobPage />);
    fireEvent.click(await screen.findByRole("button", { name: "开始匹配" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "开始匹配" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith(
      "/jobs/job_match_retry/match?analysis=analysis_match_2&version=rver_match_retry",
    ));
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("replays the same match key after an uncertain network failure", async () => {
    window.history.replaceState({}, "", "/jobs/new?version=rver_match_replay&job=job_match_replay");
    let taskReads = 0;
    const keys: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse({ consent_versions: {}, identity_type: "email", masked_email: "3***@qq.com", user_id: "usr_match_replay" });
      if (path.endsWith("/v1/jobs/job_match_replay") && init?.method === "GET") {
        return jsonResponse({ company: null, id: "job_match_replay", raw: "岗位正文", requirements: [{ confirmed: true, id: "req_match_replay", priority: 1, text: "已确认岗位要求", type: "must_have" }], status: "parsed", task_id: null, title: "匹配重放岗位" });
      }
      if (path.endsWith("/v1/match-analyses") && init?.method === "POST") {
        keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        return jsonResponse({ id: "analysis_match_replay", status: "queued", task_id: "task_match_replay" }, 202);
      }
      if (path.endsWith("/v1/tasks/task_match_replay")) {
        taskReads += 1;
        if (taskReads <= 2) throw new TypeError("temporary network failure");
        return jsonResponse({ error_code: null, id: "task_match_replay", status: "succeeded" });
      }
      return jsonResponse({}, 404);
    }));

    render(<NewJobPage />);
    fireEvent.click(await screen.findByRole("button", { name: "开始匹配" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "开始匹配" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith(
      "/jobs/job_match_replay/match?analysis=analysis_match_replay&version=rver_match_replay",
    ));
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("keeps each suggestion's returned decision status while navigating", async () => {
    params = { analysisId: "analysis_decisions" };
    window.history.replaceState({}, "", "/suggestions/analysis_decisions");
    let accepts = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/match-analyses/analysis_decisions/suggestions")) {
        return jsonResponse({ items: [
          { fact_refs: [], generation_mode: "model", id: "suggestion_decide_first", original_text: "第一条待决定原文", reason: "第一条理由", requirement_id: "req_first", requirement_text: "第一条要求", risk_flags: [], status: "pending", suggested_text: "第一条建议", target_path: "/sections/0/items/0/text" },
          { fact_refs: [], generation_mode: "rule_fallback", id: "suggestion_decide_second", original_text: "第二条待决定原文", reason: "第二条理由", requirement_id: "req_second", requirement_text: "第二条要求", risk_flags: [], status: "pending", suggested_text: "第二条建议", target_path: "/sections/0/items/1/text" },
        ] });
      }
      if (path.endsWith("/v1/suggestions/suggestion_decide_first/accept") && init?.method === "POST") {
        accepts += 1;
        return jsonResponse({ decision_id: "decision_first", status: "accepted", suggestion_id: "suggestion_decide_first", version_id: "rver_first" }, 201);
      }
      return jsonResponse({}, 404);
    }));

    render(<SuggestionsPage />);

    expect(await screen.findByText("模型建议")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "接受建议" }));
    expect(await screen.findByRole("button", { name: "撤销" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "接受建议" })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "a" });
    fireEvent.click(screen.getByRole("button", { name: "下一条建议" }));
    expect(await screen.findByText("基础解析")).toBeInTheDocument();
    expect(screen.queryByText("AI 已完成")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "上一条建议" }));
    expect(await screen.findByText("第一条待决定原文")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "撤销" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "接受建议" })).not.toBeInTheDocument();
    expect(accepts).toBe(1);
  });

  it("exposes decision actions only for the current server suggestion status", async () => {
    params = { analysisId: "analysis_status_actions" };
    window.history.replaceState({}, "", "/suggestions/analysis_status_actions");
    const statuses = ["pending", "blocked", "accepted", "edited", "ignored", "reverted"] as const;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/match-analyses/analysis_status_actions/suggestions")) {
        return jsonResponse({ items: statuses.map((status, index) => ({
          fact_refs: [],
          generation_mode: "model",
          id: `suggestion_status_${status}`,
          original_text: `状态原文 ${status}`,
          reason: `状态理由 ${status}`,
          requirement_id: `req_${index}`,
          requirement_text: `状态要求 ${status}`,
          risk_flags: status === "blocked" ? ["missing_source"] : [],
          status,
          suggested_text: `状态建议 ${status}`,
          target_path: `/sections/0/items/${index}/text`,
        })) });
      }
      return jsonResponse({}, 404);
    }));

    render(<SuggestionsPage />);

    expect(await screen.findByText("状态原文 pending")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "接受建议" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "编辑后接受" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "忽略建议" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一条建议" }));
    expect(await screen.findByText("状态原文 blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "补充事实" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "忽略建议" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();

    for (const status of ["accepted", "edited", "ignored"] as const) {
      fireEvent.click(screen.getByRole("button", { name: "下一条建议" }));
      expect(await screen.findByText(`状态原文 ${status}`)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "撤销" })).toBeEnabled();
      expect(screen.queryByRole("button", { name: "接受建议" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "忽略建议" })).not.toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: "下一条建议" }));
    expect(await screen.findByText("状态原文 reverted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "接受建议" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑后接受" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "忽略建议" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });

  it("shows match details by joining analysis items with the real job requirements", async () => {
    params = { id: "job_unique" };
    window.history.replaceState({}, "", "/jobs/job_unique/match?analysis=analysis_unique&version=rver_unique");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_match", identity_type: "email" });
      }
      if (path.endsWith("/v1/match-analyses/analysis_unique")) {
        return jsonResponse({ ai_run_id: "run_match", generation_mode: "model", id: "analysis_unique", input_hash: "c".repeat(64), items: [{ ai_run_id: "run_match", category: "proved", evidence_refs: ["fact_nonce"], generation_mode: "model", id: "match_item", input_hash: "d".repeat(64), reason_code: "direct_evidence", requirement_id: "req_nonce", resume_target_paths: ["/sections/0/items/0/text"], workflow_version: "2" }], job_id: "job_unique", resume_version_id: "rver_unique", status: "succeeded", task_id: "task_match", updated_at: "2026-07-30T08:30:00Z", workflow_version: "2" });
      }
      if (path.endsWith("/v1/jobs/job_unique")) {
        return jsonResponse({ company: null, id: "job_unique", requirements: [{ confirmed: true, id: "req_nonce", priority: 1, text: "唯一匹配要求丑", type: "must_have" }], status: "parsed", task_id: null, title: "目标岗位" });
      }
      if (path.endsWith("/v1/facts/fact_nonce/sources")) {
        return jsonResponse({ items: [{ content: "唯一匹配来源", source_ref: "fact-candidate:2", source_type: "fact_candidate_edit" }] });
      }
      if (path.endsWith("/v1/facts/fact_nonce")) {
        return jsonResponse({ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_nonce", kind: "project", source_ids: ["source_nonce"], status: "confirmed", value: "唯一匹配事实" });
      }
      return jsonResponse({}, 404);
    }));

    render(<MatchPage />);

    expect(await screen.findByText("唯一匹配要求丑")).toBeInTheDocument();
    expect(screen.getByText("模型匹配")).toBeInTheDocument();
    expect(screen.getByText(/2026.*07.*30/)).toBeInTheDocument();
    expect(await screen.findByText("唯一匹配事实")).toBeInTheDocument();
    expect(screen.getByText("候选编辑：唯一匹配来源")).toBeInTheDocument();
    expect(screen.queryByText(/fact_candidate_edit|proved/)).not.toBeInTheDocument();
  });

  it("renders immutable versions from the API and restores through a real write", async () => {
    params = { id: "resume_versions" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_versions", identity_type: "email" });
      }
      if (path.endsWith("/v1/resumes/resume_versions")) {
        return jsonResponse({ base_resume_id: null, id: "resume_versions", job_description_id: null, kind: "base", title: "唯一版本简历", version: 4 });
      }
      if (path.endsWith("/v1/resumes/resume_versions/versions?limit=20")) {
        return jsonResponse({ items: [
          { created_at: "2026-07-30T01:02:04Z", id: "rver_current_nonce", operation: "save", parent_version_id: "rver_history_nonce", resume_id: "resume_versions", snapshot: { schema_version: "1", sections: [], target: null, title: "唯一版本简历" }, snapshot_hash: "abcdef1234567890" },
          { created_at: "2026-07-30T01:02:03Z", id: "rver_history_nonce", operation: "save", parent_version_id: "rver_parent", resume_id: "resume_versions", snapshot: { schema_version: "1", sections: [], target: null, title: "唯一版本简历" }, snapshot_hash: "1234567890abcdef" },
        ], next_cursor: null });
      }
      if (path.endsWith("/v1/resumes/resume_versions/versions/rver_history_nonce/restore") && init?.method === "POST") {
        return jsonResponse({ created_at: "2026-07-30T01:03:03Z", id: "rver_restored_nonce", operation: "restore", parent_version_id: "rver_history_nonce", resume_id: "resume_versions", snapshot: { schema_version: "1", sections: [], target: null, title: "唯一版本简历" }, snapshot_hash: "1234567890abcdef" }, 201);
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<VersionsPage />);

    expect((await screen.findAllByText(/rver_history_nonce/)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "恢复为新版本" }));
    expect((await screen.findAllByText(/rver_restored_nonce/)).length).toBeGreaterThan(0);
  });

  it("creates an export with the selected template and renders the signed preview resource", async () => {
    params = { id: "new" };
    window.history.replaceState({}, "", "/exports/new?version=rver_export_unique");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, identity_type: "email", masked_email: "3***@qq.com", user_id: "usr_export" });
      }
      if (path.endsWith("/v1/exports") && init?.method === "POST") {
        return jsonResponse({
          content_hash: "hash_export",
          download_expires_in: null,
          download_name: "resume.pdf",
          download_url: null,
          id: "export_unique",
          resume_version_id: "rver_export_unique",
          status: "queued",
          task_id: "task_export_unique",
          template_version: "modern-whitespace",
        }, 202);
      }
      if (path.endsWith("/v1/tasks/task_export_unique")) {
        return jsonResponse({ cancellation_requested: false, error_code: null, id: "task_export_unique", progress: 100, result_ref: "export_unique", stage: "completed", status: "succeeded", trace_id: "trace_export", type: "render_resume_export" });
      }
      if (path.endsWith("/v1/exports/export_unique")) {
        return jsonResponse({
          content_hash: "hash_export",
          download_expires_in: 900,
          download_name: "resume.pdf",
          download_url: "/v1/storage/exports/export_unique",
          id: "export_unique",
          resume_version_id: "rver_export_unique",
          status: "succeeded",
          task_id: "task_export_unique",
          template_version: "modern-whitespace",
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ExportPage />);
    fireEvent.change(await screen.findByRole("combobox", { name: "模板" }), {
      target: { value: "modern-whitespace" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成 PDF" }));

    expect(await screen.findByRole("link", { name: "下载 PDF" })).toHaveAttribute(
      "href",
      "/api/v1/storage/exports/export_unique",
    );
    expect(screen.getByTitle("PDF 预览")).toHaveAttribute(
      "src",
      "/api/v1/storage/exports/export_unique",
    );
    const createCall = fetchMock.mock.calls.find(([url, init]) => (
      String(url).endsWith("/v1/exports") && init?.method === "POST"
    ));
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
      resume_version_id: "rver_export_unique",
      template_version: "modern-whitespace",
    });
  });

  it("retries a failed export with the resource resume version when the URL has no version", async () => {
    params = { id: "export_failed" };
    window.history.replaceState({}, "", "/exports/export_failed");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/exports/export_failed") && init?.method === "GET") {
        return jsonResponse({
          content_hash: "failed_hash",
          download_expires_in: null,
          download_name: "failed.pdf",
          download_url: null,
          id: "export_failed",
          resume_version_id: "rver_failed_source",
          status: "failed",
          task_id: "task_failed",
          template_version: "clear-standard",
        });
      }
      if (path.endsWith("/v1/exports") && init?.method === "POST") {
        return jsonResponse({
          content_hash: "retry_hash",
          download_expires_in: null,
          download_name: "retry.pdf",
          download_url: null,
          id: "export_retry",
          resume_version_id: "rver_failed_source",
          status: "queued",
          task_id: "task_retry",
          template_version: "clear-standard",
        }, 202);
      }
      if (path.endsWith("/v1/tasks/task_retry")) {
        return jsonResponse({ cancellation_requested: false, error_code: null, id: "task_retry", progress: 100, result_ref: "export_retry", stage: "succeeded", status: "succeeded", trace_id: "tr_retry", type: "render_resume_export" });
      }
      if (path.endsWith("/v1/exports/export_retry")) {
        return jsonResponse({
          content_hash: "retry_hash",
          download_expires_in: 900,
          download_name: "retry.pdf",
          download_url: "/v1/storage/exports/retry",
          id: "export_retry",
          resume_version_id: "rver_failed_source",
          status: "succeeded",
          task_id: "task_retry",
          template_version: "clear-standard",
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ExportPage />);
    fireEvent.click(await screen.findByRole("button", { name: "重新生成 PDF" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/v1/exports") && init?.method === "POST"
      ));
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        resume_version_id: "rver_failed_source",
      });
    });
  });

  it("resumes polling an existing queued export after refresh", async () => {
    params = { id: "export_existing" };
    window.history.replaceState({}, "", "/exports/export_existing?version=rver_existing");
    let exportReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, identity_type: "email", masked_email: "3***@qq.com", user_id: "usr_export" });
      }
      if (path.endsWith("/v1/tasks/task_existing")) {
        return jsonResponse({ cancellation_requested: false, error_code: null, id: "task_existing", progress: 100, result_ref: "export_existing", stage: "completed", status: "succeeded", trace_id: "trace_existing", type: "render_resume_export" });
      }
      if (path.endsWith("/v1/exports/export_existing")) {
        exportReads += 1;
        return jsonResponse({
          content_hash: "hash_existing",
          download_expires_in: exportReads > 1 ? 900 : null,
          download_name: "existing.pdf",
          download_url: exportReads > 1 ? "/v1/storage/exports/export_existing" : null,
          id: "export_existing",
          resume_version_id: "rver_existing",
          status: exportReads > 1 ? "succeeded" : "queued",
          task_id: "task_existing",
          template_version: "clear-standard",
        });
      }
      return jsonResponse({}, 404);
    }));

    render(<ExportPage />);

    expect(await screen.findByRole("link", { name: "下载 PDF" })).toHaveAttribute(
      "href",
      "/api/v1/storage/exports/export_existing",
    );
    expect(exportReads).toBe(2);
  });

  it("refreshes a terminally failed export and rotates the key before retry", async () => {
    params = { id: "new" };
    window.history.replaceState({}, "", "/exports/new?version=rver_terminal");
    let creates = 0;
    const keys: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/exports") && init?.method === "POST") {
        creates += 1;
        keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        return jsonResponse({ content_hash: `hash_${creates}`, download_expires_in: null, download_name: "resume.pdf", download_url: null, id: `export_${creates}`, resume_version_id: "rver_terminal", status: "queued", task_id: `task_export_${creates}`, template_version: "clear-standard" }, 202);
      }
      if (path.endsWith("/v1/tasks/task_export_1")) return jsonResponse({ error_code: "RENDER_FAILED", id: "task_export_1", status: "failed" });
      if (path.endsWith("/v1/exports/export_1")) return jsonResponse({ content_hash: "hash_1", download_expires_in: null, download_name: "resume.pdf", download_url: null, id: "export_1", resume_version_id: "rver_terminal", status: "failed", task_id: "task_export_1", template_version: "clear-standard" });
      if (path.endsWith("/v1/tasks/task_export_2")) return jsonResponse({ error_code: null, id: "task_export_2", status: "succeeded" });
      if (path.endsWith("/v1/exports/export_2")) return jsonResponse({ content_hash: "hash_2", download_expires_in: 900, download_name: "resume.pdf", download_url: "/v1/storage/exports/export_2", id: "export_2", resume_version_id: "rver_terminal", status: "succeeded", task_id: "task_export_2", template_version: "clear-standard" });
      return jsonResponse({}, 404);
    }));

    render(<ExportPage />);
    fireEvent.click(screen.getByRole("button", { name: "生成 PDF" }));
    fireEvent.click(await screen.findByRole("button", { name: "重新生成 PDF" }));

    expect(await screen.findByRole("link", { name: "下载 PDF" })).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("re-reads the export after refresh confirms a failed task", async () => {
    params = { id: "export_refresh_failure" };
    let taskReads = 0;
    let exportReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/tasks/task_refresh_failure")) {
        taskReads += 1;
        if (taskReads === 1) throw new TypeError("temporary network failure");
        return jsonResponse({ error_code: "RENDER_FAILED", id: "task_refresh_failure", status: "failed" });
      }
      if (path.endsWith("/v1/exports/export_refresh_failure")) {
        exportReads += 1;
        const failed = exportReads >= 3;
        return jsonResponse({ content_hash: "hash", download_expires_in: null, download_name: "resume.pdf", download_url: null, id: "export_refresh_failure", resume_version_id: "rver_refresh", status: failed ? "failed" : "queued", task_id: "task_refresh_failure", template_version: "clear-standard" });
      }
      return jsonResponse({}, 404);
    }));

    render(<ExportPage />);
    fireEvent.click(await screen.findByRole("button", { name: "继续检查" }));

    expect(await screen.findByRole("button", { name: "重新生成 PDF" })).toBeInTheDocument();
    expect(exportReads).toBe(3);
  });
});
