"use client";

import { ApiError } from "@resume/shared/client";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { useRouter } from "next/navigation";
import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { StatusTag } from "../../components/ui/StatusTag";
import { createWebApiClient } from "../../features/api/client";

type IntakeSession = components["schemas"]["IntakeSessionResponse"];
type IntakeFact = components["schemas"]["IntakeFactSummary"];
type SessionUser = components["schemas"]["MeResponse"];
type ViewState = "loading" | "ready" | "saving" | "confirming" | "drafting" | "error";

const reasonLabels = {
  ambiguous_role: "需要说明你本人承担的角色",
  conflict: "已有信息存在冲突，需要你澄清",
  missing_unit: "结果缺少单位或可核实范围",
} as const;

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "INTAKE_VERSION_CONFLICT") return "会话已在其他页面更新，请读取云端最新进度后继续。";
    if (error.code === "INTAKE_FACTS_NOT_READY") return "请先确认至少两条有来源的经历事实。";
    if (error.code === "TASK_FAILED") return "草稿生成失败，已保存的回答仍在，可以重新生成。";
    return error.message || "请求没有完成，请重试。";
  }
  return "网络请求没有完成，请检查连接后重试。";
}

export default function CreatePage() {
  const router = useRouter();
  const [session, setSession] = useState<IntakeSession | null>(null);
  const [answer, setAnswer] = useState("");
  const [title, setTitle] = useState("我的基础简历");
  const [state, setState] = useState<ViewState>("loading");
  const [error, setError] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const resumedTask = useRef("");
  const answerOperation = useRef({ fingerprint: "", key: "" });
  const draftOperation = useRef({ fingerprint: "", key: "" });
  const factOperationKeys = useRef<Record<string, string>>({});

  const openCompletedDraft = async (current: IntakeSession) => {
    if (!current.task_id) return;
    const api = createWebApiClient();
    setState("drafting");
    try {
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${current.task_id}`),
        current.task_id,
      );
      const completed = await api.get<IntakeSession>(`/v1/intake-sessions/${current.id}`);
      setSession(completed);
      if (!completed.resume_id) throw new Error("Draft task completed without a resume");
      router.push(`/resumes/${completed.resume_id}/edit`);
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  const applySession = (current: IntakeSession) => {
    setSession(current);
    setState("ready");
    setError("");
    window.history.replaceState({}, "", `/create?session=${encodeURIComponent(current.id)}`);
  };

  const loadSession = async (restart = false) => {
    setState("loading");
    setError("");
    try {
      const api = createWebApiClient();
      const me = await api.get<SessionUser>("/v1/me");
      const existingId = restart
        ? null
        : new URLSearchParams(window.location.search).get("session");
      const current = existingId
        ? await api.get<IntakeSession>(`/v1/intake-sessions/${existingId}`)
        : await api.post<components["schemas"]["IntakeStartRequest"], IntakeSession>(
            "/v1/intake-sessions",
            { restart },
            crypto.randomUUID(),
          );
      setOwnerId(me.user_id);
      setAnswer("");
      applySession(current);
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  useEffect(() => {
    void loadSession();
  }, []);

  useEffect(() => {
    if (
      session?.status === "drafting"
      && session.task_id
      && resumedTask.current !== session.task_id
    ) {
      resumedTask.current = session.task_id;
      void openCompletedDraft(session);
    }
  }, [session]);

  const answerDraftKey = session?.current_question && ownerId
    ? `intake-answer:${ownerId}:${session.id}:${session.current_question.id}`
    : "";

  useEffect(() => {
    if (!answerDraftKey || typeof localStorage === "undefined") return;
    const raw = localStorage.getItem(answerDraftKey);
    if (!raw) return;
    try {
      const draft = JSON.parse(raw) as {
        answer?: string;
        expires_at?: number;
        owner_id?: string;
        question_id?: string;
        session_id?: string;
      };
      if (
        draft.owner_id === ownerId
        && draft.session_id === session?.id
        && draft.question_id === session?.current_question?.id
        && typeof draft.answer === "string"
        && typeof draft.expires_at === "number"
        && draft.expires_at > Date.now()
      ) {
        setAnswer(draft.answer);
      } else {
        localStorage.removeItem(answerDraftKey);
      }
    } catch {
      localStorage.removeItem(answerDraftKey);
    }
  }, [answerDraftKey, ownerId, session?.current_question?.id, session?.id]);

  useEffect(() => {
    if (!answerDraftKey || !answer || typeof localStorage === "undefined") return;
    localStorage.setItem(answerDraftKey, JSON.stringify({
      answer,
      expires_at: Date.now() + 24 * 60 * 60 * 1000,
      owner_id: ownerId,
      question_id: session?.current_question?.id,
      session_id: session?.id,
    }));
  }, [answer, answerDraftKey, ownerId, session?.current_question?.id, session?.id]);

  const submitAnswer = async (skipped: boolean) => {
    const question = session?.current_question;
    if (!session || !question || state === "saving") return;
    const value = answer.trim();
    if (!skipped && !value) {
      setError("请填写回答，或者选择“跳过此题”。");
      return;
    }
    setState("saving");
    setError("");
    try {
      const body: components["schemas"]["IntakeAnswerRequest"] = {
        answer: skipped ? null : value,
        base_version: session.version,
        question_id: question.id,
        skipped,
      };
      const fingerprint = JSON.stringify(body);
      if (answerOperation.current.fingerprint !== fingerprint) {
        answerOperation.current = { fingerprint, key: crypto.randomUUID() };
      }
      const updated = await createWebApiClient().post<typeof body, IntakeSession>(
        `/v1/intake-sessions/${session.id}/answers`,
        body,
        answerOperation.current.key,
      );
      answerOperation.current = { fingerprint: "", key: "" };
      if (answerDraftKey && typeof localStorage !== "undefined") {
        localStorage.removeItem(answerDraftKey);
      }
      setAnswer("");
      applySession(updated);
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  const setFactStatus = async (fact: IntakeFact, next: "confirm" | "reject") => {
    if (!session || state === "confirming") return;
    setState("confirming");
    setError("");
    try {
      const operation = `${fact.id}:${next}`;
      factOperationKeys.current[operation] ||= crypto.randomUUID();
      const updated = await createWebApiClient().post<{}, components["schemas"]["FactResponse"]>(
        `/v1/facts/${fact.id}/${next}`,
        {},
        factOperationKeys.current[operation],
      );
      delete factOperationKeys.current[operation];
      setSession({
        ...session,
        fact_summaries: session.fact_summaries.map((item) => (
          item.id === updated.id ? { ...item, status: updated.status } : item
        )),
      });
      setState("ready");
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  const createDraft = async () => {
    if (!session || state === "drafting") return;
    setState("drafting");
    setError("");
    try {
      const body: components["schemas"]["IntakeDraftRequest"] = {
        base_version: session.version,
        title: title.trim(),
        generation_mode: "model",
      };
      const fingerprint = JSON.stringify(body);
      if (draftOperation.current.fingerprint !== fingerprint) {
        draftOperation.current = { fingerprint, key: crypto.randomUUID() };
      }
      const queued = await createWebApiClient().post<
        typeof body,
        components["schemas"]["IntakeDraftResponse"]
      >(
        `/v1/intake-sessions/${session.id}/drafts`,
        body,
        draftOperation.current.key,
      );
      draftOperation.current = { fingerprint: "", key: "" };
      const draftingSession: IntakeSession = {
        ...session,
        status: "drafting",
        task_id: queued.task_id,
        version: queued.version,
      };
      resumedTask.current = queued.task_id;
      setSession(draftingSession);
      await openCompletedDraft(draftingSession);
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  if (!session) {
    return (
      <Page eyebrow="经历雷达" title="从真实经历开始">
        <section className="panel" role={state === "error" ? "alert" : "status"}>
          <p>{state === "error" ? error : "正在恢复你的创建进度…"}</p>
          {state === "error" ? <Button onClick={() => void loadSession()} variant="secondary">重试</Button> : null}
        </section>
      </Page>
    );
  }

  const confirmedCount = session.fact_summaries.filter((fact) => fact.status === "confirmed").length;
  const busy = ["saving", "confirming", "drafting", "loading"].includes(state);
  const question = session.current_question;
  const reason = question?.reason ? reasonLabels[question.reason] : null;

  return (
    <Page
      actions={<Button disabled={busy} onClick={() => void loadSession(true)} variant="quiet">重新开始</Button>}
      eyebrow={`已完成 ${session.completed_count} 题 · 预计还剩 ${session.remaining_estimate} 题`}
      status={{ label: session.status === "drafting" ? "正在生成草稿" : "回答已保存", tone: session.status === "drafting" ? "pending" : "success" }}
      title="从真实经历开始"
    >
      <div className="editor-grid wizard-grid">
        <aside className="editor-rail" aria-label="创建进度">
          <p className="eyebrow">会话进度</p>
          <strong>{session.completed_count} 题已保存</strong>
          <p>刷新页面会从最后一次成功保存的位置继续。重新开始不会删除已确认事实。</p>
          <p className="resource-id">{session.id}</p>
        </aside>

        <section className="editor-main">
          {question ? (
            <>
              <p className="eyebrow">{question.type === "short_answer" ? "简短回答" : "经历深挖"}</p>
              <h2>{question.prompt}</h2>
              {reason ? <p className="rule-strip">{reason}</p> : null}
              <Field
                disabled={busy}
                error={error || undefined}
                helper={`${answer.length} / ${question.type === "short_answer" ? 300 : 1000} 字${answer ? " · 未同步" : ""}`}
                label="你的回答"
                maxLength={question.type === "short_answer" ? 300 : 1000}
                multiline
                name="create-answer"
                onChange={(event: ChangeEvent<HTMLTextAreaElement>) => {
                  setAnswer(event.currentTarget.value);
                  if (error) setError("");
                }}
                placeholder="只写你真实做过、能在面试中说明的内容"
                value={answer}
              />
              <div className="button-row">
                <Button disabled={busy || !answer.trim()} onClick={() => void submitAnswer(false)} state={state === "saving" ? "loading" : "default"}>
                  保存并继续
                </Button>
                <Button disabled={busy} onClick={() => void submitAnswer(true)} variant="quiet">跳过此题</Button>
                {state === "error" ? <Button onClick={() => void loadSession()} variant="secondary">读取云端最新进度</Button> : null}
              </div>
            </>
          ) : (
            <div role="status">
              <h2>问题已完成</h2>
              <p>请确认事实后生成基础简历。</p>
            </div>
          )}
        </section>

        <aside className="editor-context">
          <header>
            <div>
              <p className="eyebrow">事实与来源</p>
              <h2>已确认 {confirmedCount} / {session.fact_summaries.length}</h2>
            </div>
          </header>
          {session.fact_summaries.length === 0 ? <p>回答有效经历后，待确认事实会出现在这里。</p> : null}
          <ol className="intake-facts">
            {session.fact_summaries.map((fact, index) => (
              <li className="intake-fact" key={fact.id}>
                <span>{fact.kind}</span>
                <p>{fact.value}</p>
                <StatusTag tone={fact.status === "confirmed" ? "success" : fact.status === "rejected" ? "error" : "pending"}>
                  {fact.status === "confirmed" ? "已确认" : fact.status === "rejected" ? "不采用" : "需要你确认"}
                </StatusTag>
                {fact.status === "unconfirmed" ? (
                  <div className="button-row">
                    <Button
                      aria-label={`确认事实 ${index + 1}`}
                      disabled={busy}
                      onClick={() => void setFactStatus(fact, "confirm")}
                      variant="secondary"
                    >
                      确认
                    </Button>
                    <Button
                      aria-label={`不采用事实 ${index + 1}`}
                      disabled={busy}
                      onClick={() => void setFactStatus(fact, "reject")}
                      variant="quiet"
                    >
                      不采用
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
          <Field
            disabled={busy}
            label="简历名称"
            maxLength={255}
            name="draft-title"
            onChange={(event: ChangeEvent<HTMLInputElement>) => setTitle(event.currentTarget.value)}
            value={title}
          />
          <Button
            disabled={busy || confirmedCount < 2 || !title.trim()}
            onClick={() => void createDraft()}
            state={state === "drafting" ? "loading" : "default"}
          >
            生成基础简历
          </Button>
          {confirmedCount < 2 ? <p>至少确认两条有来源的事实后才可生成。</p> : null}
          {error && !question ? <p role="alert">{error}</p> : null}
        </aside>
      </div>
    </Page>
  );
}
