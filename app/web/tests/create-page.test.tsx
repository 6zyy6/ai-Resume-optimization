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
  answered_question_ids: [],
  completed_count: 0,
  current_question: {
    id: "experience_radar",
    prompt: "服务端问题：最近完成了什么真实任务？",
    reason: null,
    type: "deep_answer",
  },
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
