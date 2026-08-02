import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CreatePage from "../app/create/page";

const push = vi.fn();
const replace = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/create",
  useRouter: () => ({ push, replace }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

const me = {
  consent_versions: {},
  identity_type: "email",
  masked_email: "3***@qq.com",
  user_id: "usr_create",
};

const firstSession = {
  analysis_status: "idle",
  analysis_task_id: null,
  answered_question_ids: [],
  completed_count: 0,
  current_question: {
    id: "experience_radar",
    prompt: "服务端问题：最近完成了什么真实任务？",
    reason: null,
    type: "deep_answer",
  },
  fact_candidates: [],
  fact_summaries: [],
  id: "intake_unique",
  remaining_estimate: 8,
  resume_id: null,
  skipped_question_ids: [],
  status: "active",
  task_id: null,
  version: 0,
};

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => [...values.keys()][index] ?? null,
    get length() {
      return values.size;
    },
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  });
  push.mockReset();
  replace.mockReset();
  window.localStorage.clear();
  window.history.replaceState({}, "", "/create");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("V2 persisted intake", () => {
  it("polls a queued answer analysis, reloads the candidate source, and accepts it as a confirmed fact", async () => {
    let finishAnalysis: ((response: Response) => void) | undefined;
    const queuedSession = {
      ...firstSession,
      analysis_status: "queued",
      analysis_task_id: "task_answer_analysis",
      answered_question_ids: ["experience_radar"],
      completed_count: 1,
      version: 1,
    };
    const candidateSession = {
      ...queuedSession,
      analysis_status: "waiting_for_confirmation",
      current_question: null,
      fact_candidates: [{
        ai_run_id: "run_answer_analysis",
        decision_mode: "accept_or_edit",
        id: "candidate_real",
        intake_answer_id: "answer_real",
        kind: "project",
        source_end: 12,
        source_excerpt: "我组织了校园招聘活动",
        source_hash: "a".repeat(64),
        source_start: 0,
        status: "pending",
        value: "组织校园招聘活动",
      }],
    };
    let sessionReads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") {
        return jsonResponse(firstSession, 201);
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique/answers")) {
        return jsonResponse(queuedSession, 202);
      }
      if (path.endsWith("/v1/tasks/task_answer_analysis")) {
        return new Promise<Response>((resolve) => {
          finishAnalysis = resolve;
        });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        sessionReads += 1;
        return jsonResponse(candidateSession);
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique/fact-candidates/candidate_real/decision")) {
        return jsonResponse({
          candidate_id: "candidate_real",
          current_question: {
            id: "project_role",
            prompt: "你亲自完成了哪部分？",
            reason: "ambiguous_role",
            type: "short_answer",
          },
          fact_summary: {
            id: "fact_confirmed",
            kind: "project",
            status: "confirmed",
            value: "组织校园招聘活动",
          },
          session_version: 2,
          status: "accepted",
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CreatePage />);
    fireEvent.change(await screen.findByRole("textbox", { name: "你的回答" }), {
      target: { value: "我组织了校园招聘活动" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存并继续" }));

    expect(await screen.findByRole("heading", { name: "正在整理这段经历" })).toBeInTheDocument();
    finishAnalysis?.(jsonResponse({
      cancellation_requested: false,
      error_code: null,
      id: "task_answer_analysis",
      progress: 100,
      result_ref: "intake_unique",
      stage: "completed",
      status: "succeeded",
      trace_id: "trace_answer_analysis",
      type: "analyze_intake_answer",
    }));
    expect(await screen.findByText("我组织了校园招聘活动")).toBeInTheDocument();
    expect(screen.getByText("项目事实")).toBeInTheDocument();
    expect(screen.queryByText("project")).not.toBeInTheDocument();
    expect(sessionReads).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "接受候选 1" }));

    expect(await screen.findByText("已确认 1 / 1")).toBeInTheDocument();
    expect(screen.getByText("组织校园招聘活动")).toBeInTheDocument();
    const decisionCall = fetchMock.mock.calls.find(([url]) => (
      String(url).endsWith("/fact-candidates/candidate_real/decision")
    ));
    expect(JSON.parse(String(decisionCall?.[1]?.body))).toEqual({
      base_version: 1,
      decision: "accept",
      value: null,
    });
  });

  it("offers retry and rule continuation without losing a failed answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") {
        return jsonResponse({
          ...firstSession,
          analysis_status: "failed",
          analysis_task_id: "task_analysis_failed",
          current_question: null,
          version: 1,
        }, 200);
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);

    expect(await screen.findByRole("button", { name: "重试整理" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "继续回答下一题" })).toBeEnabled();
  });

  it("makes the fact-text draft fallback an explicit user decision", async () => {
    const readySession = {
      ...firstSession,
      analysis_status: "completed",
      completed_count: 2,
      current_question: null,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "confirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "confirmed", value: "真实结果二" },
      ],
      version: 2,
    };
    const draftBodies: Array<{ base_version: number; generation_mode: string }> = [];
    let sessionReads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") {
        return jsonResponse(readySession, 201);
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique/drafts") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        draftBodies.push(body);
        const fallback = body.generation_mode === "rule_fallback";
        return jsonResponse({
          session_id: "intake_unique",
          status: "queued",
          task_id: fallback ? "task_draft_fallback" : "task_draft_model",
          version: fallback ? 4 : 3,
        }, 202);
      }
      if (path.endsWith("/v1/tasks/task_draft_model")) {
        return jsonResponse({
          cancellation_requested: false,
          error_code: "MODEL_FAILED",
          id: "task_draft_model",
          progress: 50,
          result_ref: null,
          stage: "compose_resume_draft",
          status: "failed",
          trace_id: "trace_draft_model",
          type: "generate_intake_draft",
        });
      }
      if (path.endsWith("/v1/tasks/task_draft_fallback")) {
        return jsonResponse({
          cancellation_requested: false,
          error_code: null,
          id: "task_draft_fallback",
          progress: 100,
          result_ref: "resume_fallback",
          stage: "completed",
          status: "succeeded",
          trace_id: "trace_draft_fallback",
          type: "generate_intake_draft",
        });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        sessionReads += 1;
        return jsonResponse(sessionReads === 1
          ? {
              ...readySession,
              status: "active",
              task_id: "task_draft_model",
              version: 4,
            }
          : {
              ...readySession,
              resume_id: "resume_fallback",
              status: "completed",
              task_id: "task_draft_fallback",
              version: 5,
            });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "生成基础简历" }));
    fireEvent.click(await screen.findByRole("button", {
      name: "使用事实原文创建基础草稿",
    }));

    await waitFor(() => expect(draftBodies).toHaveLength(2));
    expect(draftBodies[1]).toMatchObject({
      base_version: 4,
      generation_mode: "rule_fallback",
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/resumes/resume_fallback/edit"));
  });

  it("does not offer a draft fallback for an uncertain task network error", async () => {
    const readySession = {
      ...firstSession,
      analysis_status: "completed",
      current_question: null,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "confirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "confirmed", value: "真实结果二" },
      ],
      version: 2,
    };
    let draftPosts = 0;
    let taskReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") return jsonResponse(readySession, 201);
      if (path.endsWith("/v1/intake-sessions/intake_unique/drafts") && init?.method === "POST") {
        draftPosts += 1;
        return jsonResponse({ session_id: "intake_unique", status: "queued", task_id: "task_draft_uncertain", version: 3 }, 202);
      }
      if (path.endsWith("/v1/tasks/task_draft_uncertain")) {
        taskReads += 1;
        if (taskReads === 1) throw new TypeError("temporary network failure");
        return jsonResponse({ error_code: null, id: "task_draft_uncertain", status: "succeeded" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        return jsonResponse({
          ...readySession,
          resume_id: "resume_after_task_retry",
          status: "completed",
          task_id: "task_draft_uncertain",
          version: 4,
        });
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "生成基础简历" }));

    expect(await screen.findByText(/网络请求没有完成/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "使用事实原文创建基础草稿" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成基础简历" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "重新检查草稿状态" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/resumes/resume_after_task_retry/edit"));
    expect(taskReads).toBe(2);
    expect(draftPosts).toBe(1);
  });

  it("does not offer a fallback when the draft task succeeded but session reload failed", async () => {
    const readySession = {
      ...firstSession,
      analysis_status: "completed",
      current_question: null,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "confirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "confirmed", value: "真实结果二" },
      ],
      version: 2,
    };
    let sessionReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") return jsonResponse(readySession, 201);
      if (path.endsWith("/v1/intake-sessions/intake_unique/drafts") && init?.method === "POST") {
        return jsonResponse({ session_id: "intake_unique", status: "queued", task_id: "task_draft_success", version: 3 }, 202);
      }
      if (path.endsWith("/v1/tasks/task_draft_success")) {
        return jsonResponse({ error_code: null, id: "task_draft_success", status: "succeeded" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        sessionReads += 1;
        if (sessionReads === 1) throw new TypeError("session reload failed");
        return jsonResponse({
          ...readySession,
          resume_id: "resume_after_session_retry",
          status: "completed",
          task_id: "task_draft_success",
          version: 4,
        });
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "生成基础简历" }));

    expect(await screen.findByText(/网络请求没有完成/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "使用事实原文创建基础草稿" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成基础简历" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "重新检查草稿状态" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/resumes/resume_after_session_retry/edit"));
    expect(sessionReads).toBe(2);
  });

  it("allows a new draft with the latest version and a new key after cancellation is confirmed", async () => {
    const readySession = {
      ...firstSession,
      analysis_status: "completed",
      current_question: null,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "confirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "confirmed", value: "真实结果二" },
      ],
      version: 2,
    };
    const draftBodies: Array<{ base_version: number }> = [];
    const draftKeys: string[] = [];
    let sessionReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") return jsonResponse(readySession, 201);
      if (path.endsWith("/v1/intake-sessions/intake_unique/drafts") && init?.method === "POST") {
        draftBodies.push(JSON.parse(String(init.body)));
        draftKeys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        const retry = draftBodies.length === 2;
        return jsonResponse({
          session_id: "intake_unique",
          status: "queued",
          task_id: retry ? "task_after_cancel" : "task_cancelled",
          version: retry ? 5 : 3,
        }, 202);
      }
      if (path.endsWith("/v1/tasks/task_cancelled")) {
        return jsonResponse({ error_code: "CANCELLED", id: "task_cancelled", status: "cancelled" });
      }
      if (path.endsWith("/v1/tasks/task_after_cancel")) {
        return jsonResponse({ error_code: "MODEL_FAILED", id: "task_after_cancel", status: "failed" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        sessionReads += 1;
        return jsonResponse({
          ...readySession,
          status: "active",
          task_id: sessionReads === 1 ? "task_cancelled" : "task_after_cancel",
          version: sessionReads === 1 ? 4 : 6,
        });
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "生成基础简历" }));

    expect(await screen.findByText(/任务已取消/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新检查草稿状态" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "使用事实原文创建基础草稿" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成基础简历" }));

    await waitFor(() => expect(draftBodies).toHaveLength(2));
    expect(draftBodies[1]).toMatchObject({ base_version: 4 });
    expect(draftKeys[0]).not.toBe("");
    expect(draftKeys[1]).not.toBe(draftKeys[0]);
  });

  it("allows normal generation after a rule fallback has terminally failed", async () => {
    const readySession = {
      ...firstSession,
      analysis_status: "completed",
      current_question: null,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "confirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "confirmed", value: "真实结果二" },
      ],
      version: 2,
    };
    const draftBodies: Array<{ base_version: number; generation_mode: string }> = [];
    const draftKeys: string[] = [];
    let sessionReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") return jsonResponse(readySession, 201);
      if (path.endsWith("/v1/intake-sessions/intake_unique/drafts") && init?.method === "POST") {
        draftBodies.push(JSON.parse(String(init.body)));
        draftKeys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
        return jsonResponse({
          session_id: "intake_unique",
          status: "queued",
          task_id: `task_terminal_failure_${draftBodies.length}`,
          version: draftBodies.length * 2 + 1,
        }, 202);
      }
      if (path.includes("/v1/tasks/task_terminal_failure_")) {
        return jsonResponse({ error_code: "MODEL_FAILED", id: path.split("/").at(-1), status: "failed" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        sessionReads += 1;
        return jsonResponse({
          ...readySession,
          status: "active",
          task_id: `task_terminal_failure_${sessionReads}`,
          version: sessionReads * 2 + 2,
        });
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "生成基础简历" }));
    fireEvent.click(await screen.findByRole("button", { name: "使用事实原文创建基础草稿" }));

    expect(await screen.findByText(/任务没有完成/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新检查草稿状态" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "使用事实原文创建基础草稿" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成基础简历" }));

    await waitFor(() => expect(draftBodies).toHaveLength(3));
    expect(draftBodies[2]).toMatchObject({ base_version: 6, generation_mode: "model" });
    expect(new Set(draftKeys).size).toBe(3);
  });

  it("opens a completed draft restored from the session URL", async () => {
    window.history.replaceState({}, "", "/create?session=intake_unique");
    const completed = {
      ...firstSession,
      analysis_status: "completed",
      current_question: null,
      resume_id: "resume_completed_refresh",
      status: "completed",
      task_id: "task_completed_refresh",
      version: 4,
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions/intake_unique")) return jsonResponse(completed);
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);

    await waitFor(() => expect(push).toHaveBeenCalledWith("/resumes/resume_completed_refresh/edit"));
  });

  it("recovers fallback availability from an active session with a persisted failed draft task", async () => {
    window.history.replaceState({}, "", "/create?session=intake_unique");
    const restored = {
      ...firstSession,
      analysis_status: "completed",
      current_question: null,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "confirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "confirmed", value: "真实结果二" },
      ],
      status: "active",
      task_id: "task_draft_failed_refresh",
      version: 4,
    };
    let sessionReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions/intake_unique")) {
        sessionReads += 1;
        return jsonResponse(restored);
      }
      if (path.endsWith("/v1/tasks/task_draft_failed_refresh")) {
        return jsonResponse({ error_code: "MODEL_FAILED", id: "task_draft_failed_refresh", status: "failed" });
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);

    expect(await screen.findByRole("button", { name: "使用事实原文创建基础草稿" })).toBeEnabled();
    expect(sessionReads).toBe(2);
  });

  it("shows a same-page recovery action after an uncertain answer-analysis poll", async () => {
    const queued = {
      ...firstSession,
      analysis_status: "queued",
      analysis_task_id: "task_analysis_network",
      current_question: null,
      version: 1,
    };
    const recovered = {
      ...queued,
      analysis_status: "completed",
      current_question: {
        id: "project_role",
        prompt: "恢复后的服务端问题",
        reason: null,
        type: "short_answer",
      },
    };
    let taskReads = 0;
    let sessionReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions") && init?.method === "POST") return jsonResponse(queued, 201);
      if (path.endsWith("/v1/tasks/task_analysis_network")) {
        taskReads += 1;
        if (taskReads === 1) throw new TypeError("temporary task network failure");
        return jsonResponse({ error_code: null, id: "task_analysis_network", status: "succeeded" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && init?.method === "GET") {
        sessionReads += 1;
        return jsonResponse(sessionReads === 1 ? queued : recovered);
      }
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);

    fireEvent.click(await screen.findByRole("button", { name: "重新检查整理进度" }));
    expect(await screen.findByRole("heading", { name: "恢复后的服务端问题" })).toBeInTheDocument();
    expect(taskReads).toBe(2);
  });

  it("starts a server session and renders the server-owned question", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions")) return jsonResponse(firstSession, 201);
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);

    expect(await screen.findByRole("heading", {
      name: "服务端问题：最近完成了什么真实任务？",
    })).toBeInTheDocument();
    expect(screen.getByText(/已完成 0 题/)).toBeInTheDocument();
    expect(window.location.search).toBe("?session=intake_unique");
  });

  it("submits the current answer with base_version and advances only from the response", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions")) return jsonResponse(firstSession, 201);
      if (path.endsWith("/v1/intake-sessions/intake_unique/answers")) {
        return jsonResponse({
          ...firstSession,
          answered_question_ids: ["experience_radar"],
          completed_count: 1,
          current_question: {
            id: "project_role",
            prompt: "服务端追问：你亲自完成了哪部分？",
            reason: "ambiguous_role",
            type: "short_answer",
          },
          fact_summaries: [{
            id: "fact_intake",
            kind: "experience",
            status: "unconfirmed",
            value: "组织了一次校园活动",
          }],
          remaining_estimate: 7,
          version: 1,
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CreatePage />);
    fireEvent.change(await screen.findByRole("textbox", { name: "你的回答" }), {
      target: { value: "组织了一次校园活动" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存并继续" }));

    expect(await screen.findByRole("heading", {
      name: "服务端追问：你亲自完成了哪部分？",
    })).toBeInTheDocument();
    const answerCall = fetchMock.mock.calls.find(([url]) => (
      String(url).endsWith("/v1/intake-sessions/intake_unique/answers")
    ));
    expect(JSON.parse(String(answerCall?.[1]?.body))).toEqual({
      answer: "组织了一次校园活动",
      base_version: 0,
      question_id: "experience_radar",
      skipped: false,
    });
    expect(screen.getByText("组织了一次校园活动")).toBeInTheDocument();
  });

  it("restores an owner-scoped unsent answer from this device", async () => {
    window.localStorage.setItem(
      "intake-answer:usr_create:intake_unique:experience_radar",
      JSON.stringify({
        answer: "刷新前尚未提交的真实回答",
        expires_at: Date.now() + 60_000,
        owner_id: "usr_create",
        question_id: "experience_radar",
        session_id: "intake_unique",
      }),
    );
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions")) return jsonResponse(firstSession, 201);
      return jsonResponse({}, 404);
    }));

    render(<CreatePage />);

    expect(await screen.findByDisplayValue("刷新前尚未提交的真实回答")).toBeInTheDocument();
  });

  it("records a skip without creating a fact", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions")) return jsonResponse(firstSession, 201);
      if (path.endsWith("/v1/intake-sessions/intake_unique/answers")) {
        return jsonResponse({
          ...firstSession,
          answered_question_ids: ["experience_radar"],
          completed_count: 1,
          current_question: {
            id: "course_probe",
            prompt: "服务端追问：还有其他经历吗？",
            reason: null,
            type: "deep_answer",
          },
          remaining_estimate: 7,
          skipped_question_ids: ["experience_radar"],
          version: 1,
        });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "跳过此题" }));

    expect(await screen.findByRole("heading", {
      name: "服务端追问：还有其他经历吗？",
    })).toBeInTheDocument();
    const answerCall = fetchMock.mock.calls.find(([url]) => (
      String(url).endsWith("/v1/intake-sessions/intake_unique/answers")
    ));
    expect(JSON.parse(String(answerCall?.[1]?.body))).toEqual({
      answer: null,
      base_version: 0,
      question_id: "experience_radar",
      skipped: true,
    });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/v1/facts"))).toBe(false);
  });

  it("confirms two sourced facts and opens the resume created by the draft task", async () => {
    const readySession = {
      ...firstSession,
      completed_count: 2,
      fact_summaries: [
        { id: "fact_one", kind: "experience", status: "unconfirmed", value: "真实经历一" },
        { id: "fact_two", kind: "result", status: "unconfirmed", value: "真实结果二" },
      ],
      version: 2,
    };
    let completed = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/v1/me")) return jsonResponse(me);
      if (path.endsWith("/v1/intake-sessions")) return jsonResponse(readySession, 201);
      if (path.endsWith("/v1/facts/fact_one/confirm")) {
        return jsonResponse({ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_one", kind: "experience", source_ids: ["src_one"], status: "confirmed", value: "真实经历一" });
      }
      if (path.endsWith("/v1/facts/fact_two/confirm")) {
        return jsonResponse({ confirmed_at: "2026-07-30T00:00:00Z", id: "fact_two", kind: "result", source_ids: ["src_two"], status: "confirmed", value: "真实结果二" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique/drafts") && init?.method === "POST") {
        return jsonResponse({ session_id: "intake_unique", status: "queued", task_id: "task_draft", version: 3 }, 202);
      }
      if (path.endsWith("/v1/tasks/task_draft")) {
        completed = true;
        return jsonResponse({ cancellation_requested: false, error_code: null, id: "task_draft", progress: 100, result_ref: "resume_created", stage: "completed", status: "succeeded", trace_id: "trace_draft", type: "generate_intake_draft" });
      }
      if (path.endsWith("/v1/intake-sessions/intake_unique") && completed) {
        return jsonResponse({ ...readySession, current_question: null, resume_id: "resume_created", status: "completed", task_id: "task_draft", version: 3 });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CreatePage />);
    fireEvent.click(await screen.findByRole("button", { name: "确认事实 1" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "确认事实 2" })).toBeEnabled());
    fireEvent.click(await screen.findByRole("button", { name: "确认事实 2" }));
    await waitFor(() => expect(screen.getByText("已确认 2 / 2")).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox", { name: "简历名称" }), {
      target: { value: "我的真实基础简历" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成基础简历" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/resumes/resume_created/edit"));
    const draftCall = fetchMock.mock.calls.find(([url]) => (
      String(url).endsWith("/v1/intake-sessions/intake_unique/drafts")
    ));
    expect(JSON.parse(String(draftCall?.[1]?.body))).toEqual({
      base_version: 2,
      generation_mode: "model",
      title: "我的真实基础简历",
    });
  });
});
