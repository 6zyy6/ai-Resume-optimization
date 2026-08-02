import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import FactsPage from "../app/facts/page";
import HomePage from "../app/home/page";
import ResumesPage from "../app/resumes/page";
import SettingsPage from "../app/settings/page";
import TasksPage from "../app/tasks/page";
import { ProtectedBoundary } from "../features/session/ProtectedBoundary";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/facts",
  useRouter: () => ({ replace }),
}));

afterEach(() => {
  cleanup();
  replace.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

describe("V2 protected session boundary", () => {
  it("does not render protected content before GET /v1/me succeeds", async () => {
    let resolveRequest: ((value: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => {
      resolveRequest = resolve;
    })));

    render(<ProtectedBoundary><div>私有内容</div></ProtectedBoundary>);

    expect(screen.queryByText("私有内容")).not.toBeInTheDocument();
    expect(screen.getByText("正在验证登录状态")).toBeInTheDocument();
    resolveRequest?.(jsonResponse({
      consent_versions: {},
      masked_email: "3***@qq.com",
      user_id: "usr_unique",
      identity_type: "email",
    }));
    expect(await screen.findByText("私有内容")).toBeInTheDocument();
  });

  it("does not start business resource requests before the session check", () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);

    render(<ProtectedBoundary><HomePage /></ProtectedBoundary>);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toMatch(/\/v1\/me$/);
    expect(screen.queryByText("把下一步做完。")).not.toBeInTheDocument();
  });

  it("redirects a missing session to a safe login return path", async () => {
    window.history.replaceState({}, "", "/facts?status=confirmed");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: {
        code: "AUTH_REQUIRED",
        details: {},
        message: "Authentication required",
        request_id: "req_test",
      },
    }, 401)));

    render(<ProtectedBoundary><div>私有内容</div></ProtectedBoundary>);

    await waitFor(() => expect(replace).toHaveBeenCalledWith(
      "/login?returnTo=%2Ffacts%3Fstatus%3Dconfirmed",
    ));
    expect(screen.queryByText("私有内容")).not.toBeInTheDocument();
  });
});

describe("V2 pages render only API-owned business data", () => {
  it("renders independent home resources and their real values", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_home", identity_type: "email" });
      }
      if (path.endsWith("/v1/resumes?limit=3")) {
        return jsonResponse({ items: [{ id: "resume_nonce", kind: "base", title: "唯一简历甲", version: 2, base_resume_id: null, job_description_id: null }] });
      }
      if (path.endsWith("/v1/tasks?limit=5")) {
        return jsonResponse({ items: [{ id: "task_nonce", type: "resume_draft", status: "running", stage: "writing", progress: 42, trace_id: "tr_1", error_code: null, result_ref: null, cancellation_requested: false }], next_cursor: null });
      }
      if (path.endsWith("/v1/me/usage")) {
        return jsonResponse({ ai_concurrent_limit: 2, ai_tasks_limit: 20, ai_tasks_running: 1, ai_tasks_used: 7, cost_state: "normal", global_cost_cny: "3.00", global_cost_limit_cny: "100.00" });
      }
      return jsonResponse({}, 404);
    }));

    render(<HomePage />);

    expect(await screen.findByText("唯一简历甲")).toBeInTheDocument();
    expect(await screen.findByText(/7\s*\/\s*20/)).toBeInTheDocument();
    expect(await screen.findByText(/42%/)).toBeInTheDocument();
  });

  it("renders resume, fact and task nonces from list APIs", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({ consent_versions: {}, masked_email: "3***@qq.com", user_id: "usr_lists", identity_type: "email" });
      }
      if (path.endsWith("/v1/resumes?limit=20")) {
        return jsonResponse({ items: [{ id: "resume_only", kind: "job_targeted", title: "唯一岗位版本乙", version: 4, base_resume_id: "base_1", job_description_id: "job_1" }] });
      }
      if (path.endsWith("/v1/facts?limit=50")) {
        return jsonResponse({ items: [{ id: "fact_only", kind: "project", value: "唯一事实丙", status: "confirmed", source_ids: ["src_1"], confirmed_at: "2026-07-30T00:00:00Z" }] });
      }
      if (path.endsWith("/v1/tasks?limit=50")) {
        return jsonResponse({ items: [{ id: "task_only", type: "parse_resume_import", status: "failed", stage: "parse", progress: 18, trace_id: "tr_2", error_code: "PARSE_FAILED", result_ref: null, cancellation_requested: false }], next_cursor: null });
      }
      return jsonResponse({}, 404);
    }));

    const resumeView = render(<ResumesPage />);
    expect(await screen.findByText("唯一岗位版本乙")).toBeInTheDocument();
    expect(screen.queryByText("产品运营实习")).not.toBeInTheDocument();
    resumeView.unmount();

    const factView = render(<FactsPage />);
    expect(await screen.findByText("唯一事实丙")).toBeInTheDocument();
    expect(screen.queryByText("课程产品设计")).not.toBeInTheDocument();
    factView.unmount();

    render(<TasksPage />);
    expect(await screen.findByRole("heading", { name: "解析简历文件" })).toBeInTheDocument();
    expect(screen.getByText("任务失败 · 解析文件")).toBeInTheDocument();
    expect(screen.queryByText(/parse_resume_import|PARSE_FAILED/)).not.toBeInTheDocument();
    expect(screen.queryByText("内容运营岗位匹配")).not.toBeInTheDocument();
  });

  it("routes real task types and downloads a completed private data export", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/tasks?limit=50")) {
        return jsonResponse({ items: [
          { cancellation_requested: false, error_code: null, id: "task_import", progress: 100, result_ref: "import_real", stage: "succeeded", status: "succeeded", trace_id: "tr_import", type: "parse_resume_import" },
          { cancellation_requested: false, error_code: null, id: "task_export", progress: 100, result_ref: "export_real", stage: "succeeded", status: "succeeded", trace_id: "tr_export", type: "render_resume_export" },
          { cancellation_requested: false, error_code: null, id: "task_privacy", progress: 100, result_ref: "file_private", stage: "succeeded", status: "succeeded", trace_id: "tr_private", type: "data_export" },
        ], next_cursor: null });
      }
      if (path.endsWith("/v1/me/data-exports/task_privacy")) {
        return jsonResponse({
          download_expires_in: 900,
          download_url: "/v1/storage/download/privacy/data.json?signature=real",
          file_id: "file_private",
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    let clickedHref = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function click() {
      clickedHref = this.getAttribute("href") ?? "";
    });

    render(<TasksPage />);

    const resultLinks = await screen.findAllByRole("link", { name: "打开结果" });
    expect(resultLinks[0]).toHaveAttribute("href", "/imports/import_real/confirm");
    expect(resultLinks[1]).toHaveAttribute("href", "/exports/export_real");
    fireEvent.click(screen.getByRole("button", { name: "下载数据副本" }));
    await waitFor(() => expect(clickedHref).toBe(
      "/api/v1/storage/download/privacy/data.json?signature=real",
    ));
  });

  it("routes completed answer analysis and match tasks without guessing private resource ids", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/tasks?limit=50")) {
        return jsonResponse({ items: [
          { cancellation_requested: false, error_code: null, id: "task_answer", progress: 100, result_ref: "answer_private_id", stage: "completed", status: "succeeded", trace_id: "tr_answer", type: "analyze_intake_answer" },
          { cancellation_requested: false, error_code: null, id: "task_match", progress: 100, result_ref: "analysis_public_id", stage: "completed", status: "succeeded", trace_id: "tr_match", type: "match_resume_to_job" },
        ], next_cursor: null });
      }
      return jsonResponse({}, 404);
    }));

    render(<TasksPage />);

    const resultLinks = await screen.findAllByRole("link", { name: "打开结果" });
    expect(resultLinks[0]).toHaveAttribute("href", "/create");
    expect(resultLinks[1]).toHaveAttribute("href", "/suggestions/analysis_public_id");
    expect(resultLinks[0]).not.toHaveAttribute("href", expect.stringContaining("answer_private_id"));
  });

  it("renders account and usage data and executes privacy writes through the API", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) {
        return jsonResponse({
          consent_versions: { privacy_policy: "2026-07-27" },
          masked_email: "3***@qq.com",
          user_id: "usr_settings",
          identity_type: "email",
        });
      }
      if (path.endsWith("/v1/me/usage")) {
        return jsonResponse({ ai_concurrent_limit: 2, ai_tasks_limit: 20, ai_tasks_running: 0, ai_tasks_used: 6, cost_state: "normal", global_cost_cny: "2.00", global_cost_limit_cny: "100.00" });
      }
      return jsonResponse({}, 404);
    }));

    render(<SettingsPage />);

    expect(await screen.findByText("3***@qq.com")).toBeInTheDocument();
    expect(await screen.findByText(/6\s*\/\s*20/)).toBeInTheDocument();
  });
});
