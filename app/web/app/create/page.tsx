"use client";

import { ApiError } from "@resume/shared/client";
import type { components } from "@resume/shared/schema";
import { TaskTerminalError, waitForTask } from "@resume/shared/workflows";
import { useRouter } from "next/navigation";
import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { StatusTag } from "../../components/ui/StatusTag";
import { createWebApiClient } from "../../features/api/client";
import { factKindLabel } from "../../features/presentation/business-labels";

type IntakeSession = components["schemas"]["IntakeSessionResponse"];
type IntakeFact = components["schemas"]["IntakeFactSummary"];
type IntakeFactCandidate = components["schemas"]["IntakeFactCandidateResponse"];
type SessionUser = components["schemas"]["MeResponse"];
type ViewState = "loading" | "ready" | "saving" | "analyzing" | "confirming" | "drafting" | "error";

const reasonLabels = {
  ambiguous_role: "需要说明你本人承担的角色",
  conflict: "已有信息存在冲突，需要你澄清",
  missing_unit: "结果缺少单位或可核实范围",
} as const;

function messageFor(error: unknown): string {
  if (error instanceof TaskTerminalError) {
    return error.status === "cancelled"
      ? "任务已取消。已保存的输入仍然保留。"
      : "任务没有完成。已保存的输入仍然保留，可以重试或手工继续。";
  }
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
  const [editingCandidateId, setEditingCandidateId] = useState("");
  const [candidateEdits, setCandidateEdits] = useState<Record<string, string>>({});
  const [draftFallbackAvailable, setDraftFallbackAvailable] = useState(false);
  const [draftStatusUncertain, setDraftStatusUncertain] = useState(false);
  const draftAllowsFallback = useRef(true);
  const openedResume = useRef("");
  const resumedTask = useRef("");
  const resumedAnalysisTask = useRef("");
  const answerOperation = useRef({ fingerprint: "", key: "" });
  const draftOperation = useRef({ fingerprint: "", key: "" });
  const factOperationKeys = useRef<Record<string, string>>({});

  const openCompletedDraft = async (current: IntakeSession, allowFallback = true) => {
    if (!current.task_id) return;
    const api = createWebApiClient();
    draftAllowsFallback.current = allowFallback;
    setState("drafting");
    setError("");
    setDraftFallbackAvailable(false);
    setDraftStatusUncertain(false);
    try {
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${current.task_id}`),
        current.task_id,
      );
    } catch (requestError) {
      if (requestError instanceof TaskTerminalError) {
        try {
          const latest = await api.get<IntakeSession>(`/v1/intake-sessions/${current.id}`);
          setSession(latest);
          setDraftFallbackAvailable(allowFallback && requestError.status === "failed");
          setDraftStatusUncertain(false);
        } catch (reloadError) {
          setDraftStatusUncertain(true);
          setError(messageFor(reloadError));
          setState("error");
          return;
        }
      } else {
        setDraftStatusUncertain(true);
      }
      setError(messageFor(requestError));
      setState("error");
      return;
    }
    try {
      const completed = await api.get<IntakeSession>(`/v1/intake-sessions/${current.id}`);
      setSession(completed);
      if (!completed.resume_id) throw new Error("Draft task completed without a resume");
      openedResume.current = completed.resume_id;
      router.push(`/resumes/${completed.resume_id}/edit`);
    } catch (requestError) {
      setDraftStatusUncertain(true);
      setError(messageFor(requestError));
      setState("error");
    }
  };

  const applySession = (current: IntakeSession) => {
    setSession(current);
    setState(["queued", "running"].includes(current.analysis_status) ? "analyzing" : "ready");
    setError("");
    setDraftStatusUncertain(false);
    window.history.replaceState({}, "", `/create?session=${encodeURIComponent(current.id)}`);
  };

  const pollAnswerAnalysis = async (current: IntakeSession) => {
    if (!current.analysis_task_id) return;
    setSession(current);
    setState("analyzing");
    setError("");
    try {
      const api = createWebApiClient();
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(
          `/v1/tasks/${current.analysis_task_id}`,
        ),
        current.analysis_task_id,
      );
      applySession(await api.get<IntakeSession>(`/v1/intake-sessions/${current.id}`));
    } catch (requestError) {
      const latest = await createWebApiClient().get<IntakeSession>(
        `/v1/intake-sessions/${current.id}`,
      ).catch(() => null);
      if (latest && !["queued", "running"].includes(latest.analysis_status)) {
        applySession(latest);
        return;
      }
      if (latest) setSession(latest);
      setError(messageFor(requestError));
      setState("error");
    }
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
      session?.status === "completed"
      && session.resume_id
      && openedResume.current !== session.resume_id
    ) {
      openedResume.current = session.resume_id;
      router.push(`/resumes/${session.resume_id}/edit`);
      return;
    }
    if (
      session
      && ["active", "drafting"].includes(session.status)
      && session.task_id
      && resumedTask.current !== session.task_id
    ) {
      resumedTask.current = session.task_id;
      void openCompletedDraft(session);
    }
  }, [session]);

  useEffect(() => {
    if (
      session?.analysis_task_id
      && ["queued", "running"].includes(session.analysis_status)
      && resumedAnalysisTask.current !== session.analysis_task_id
    ) {
      resumedAnalysisTask.current = session.analysis_task_id;
      void pollAnswerAnalysis(session);
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
      if (
        updated.analysis_task_id
        && ["queued", "running"].includes(updated.analysis_status)
      ) {
        resumedAnalysisTask.current = updated.analysis_task_id;
        await pollAnswerAnalysis(updated);
      }
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  const decideCandidate = async (
    candidate: IntakeFactCandidate,
    decision: "accept" | "edit" | "reject",
  ) => {
    if (!session || state === "confirming") return;
    const editedValue = (candidateEdits[candidate.id] ?? candidate.value).trim();
    if (decision === "edit" && (!editedValue || editedValue === candidate.value)) {
      setEditingCandidateId(candidate.id);
      setError("请先修改候选内容，再保存编辑。");
      return;
    }
    setState("confirming");
    setError("");
    try {
      const body: components["schemas"]["FactCandidateDecisionRequest"] = {
        base_version: session.version,
        decision,
        value: decision === "edit" ? editedValue : null,
      };
      const operation = `${candidate.id}:${JSON.stringify(body)}`;
      factOperationKeys.current[operation] ||= crypto.randomUUID();
      const result = await createWebApiClient().post<
        typeof body,
        components["schemas"]["FactCandidateDecisionResponse"]
      >(
        `/v1/intake-sessions/${session.id}/fact-candidates/${candidate.id}/decision`,
        body,
        factOperationKeys.current[operation],
      );
      delete factOperationKeys.current[operation];
      const remaining = session.fact_candidates.filter((item) => (
        item.id !== candidate.id && item.status === "pending"
      ));
      setSession({
        ...session,
        analysis_status: remaining.length === 0 ? "completed" : "waiting_for_confirmation",
        current_question: result.current_question,
        fact_candidates: session.fact_candidates.map((item) => (
          item.id === candidate.id ? { ...item, status: result.status } : item
        )),
        fact_summaries: result.fact_summary
          ? [...session.fact_summaries, result.fact_summary]
          : session.fact_summaries,
        version: result.session_version,
      });
      setEditingCandidateId("");
      setState("ready");
    } catch (requestError) {
      setError(messageFor(requestError));
      setState("error");
    }
  };

  const recoverAnalysis = async (action: "retry" | "continue") => {
    if (!session || state === "saving") return;
    setState("saving");
    setError("");
    try {
      const body: components["schemas"]["IntakeAnalysisActionRequest"] = {
        base_version: session.version,
      };
      const operation = `analysis:${action}:${session.version}`;
      factOperationKeys.current[operation] ||= crypto.randomUUID();
      const updated = await createWebApiClient().post<typeof body, IntakeSession>(
        `/v1/intake-sessions/${session.id}/analysis/${action}`,
        body,
        factOperationKeys.current[operation],
      );
      delete factOperationKeys.current[operation];
      applySession(updated);
      if (
        updated.analysis_task_id
        && ["queued", "running"].includes(updated.analysis_status)
      ) {
        resumedAnalysisTask.current = updated.analysis_task_id;
        await pollAnswerAnalysis(updated);
      }
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

  const createDraft = async (
    generationMode: components["schemas"]["IntakeDraftRequest"]["generation_mode"] = "model",
  ) => {
    if (!session || state === "drafting" || draftStatusUncertain) return;
    setState("drafting");
    setError("");
    setDraftFallbackAvailable(false);
    setDraftStatusUncertain(false);
    try {
      const body: components["schemas"]["IntakeDraftRequest"] = {
        base_version: session.version,
        title: title.trim(),
        generation_mode: generationMode,
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
      await openCompletedDraft(draftingSession, generationMode === "model");
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
  const analyzing = ["queued", "running"].includes(session.analysis_status);
  const reviewingCandidates = session.analysis_status === "waiting_for_confirmation";
  const analysisFailed = session.analysis_status === "failed";
  const analysisPollingFailed = analyzing && state === "error";
  const draftFailed = draftFallbackAvailable;
  const busy = ["saving", "analyzing", "confirming", "drafting", "loading"].includes(state);
  const question = session.current_question;
  const reason = question?.reason ? reasonLabels[question.reason] : null;

  return (
    <Page
      actions={<Button disabled={busy} onClick={() => void loadSession(true)} variant="quiet">重新开始</Button>}
      eyebrow={`已完成 ${session.completed_count} 题 · 预计还剩 ${session.remaining_estimate} 题`}
      status={{
        label: draftFailed
          ? "草稿生成失败"
          : draftStatusUncertain
            ? "草稿状态暂时无法确认"
          : session.status === "drafting"
          ? "正在生成草稿"
          : analysisPollingFailed
            ? "整理状态暂时无法确认"
          : analyzing
            ? "正在整理这段经历"
            : reviewingCandidates
              ? "请确认候选事实"
              : analysisFailed
                ? "经历整理失败"
                : "回答已保存",
        tone: draftFailed || draftStatusUncertain || analysisPollingFailed
          ? "error"
          : session.status === "drafting" || analyzing || reviewingCandidates
          ? "pending"
          : analysisFailed
            ? "error"
            : "success",
      }}
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
          {draftStatusUncertain ? (
            <div role="alert">
              <h2>草稿状态暂时无法确认</h2>
              <p>{error}</p>
              <Button
                onClick={() => void openCompletedDraft(session, draftAllowsFallback.current)}
                variant="secondary"
              >
                重新检查草稿状态
              </Button>
            </div>
          ) : analysisPollingFailed ? (
            <div role="alert">
              <h2>整理状态暂时无法确认</h2>
              <p>{error}</p>
              <Button
                onClick={() => void pollAnswerAnalysis(session)}
                variant="secondary"
              >
                重新检查整理进度
              </Button>
            </div>
          ) : analyzing ? (
            <div aria-live="polite" role="status">
              <h2>正在整理这段经历</h2>
              <p>回答已经保存。整理完成后会显示候选事实和原回答出处。</p>
            </div>
          ) : analysisFailed ? (
            <div role="alert">
              <h2>这段经历暂时没有整理完成</h2>
              <p>原回答已经保存。你可以用同一输入重试，也可以使用规则问题继续。</p>
              <div className="button-row">
                <Button
                  disabled={busy}
                  onClick={() => void recoverAnalysis("retry")}
                  state={state === "saving" ? "loading" : "default"}
                  variant="secondary"
                >
                  重试整理
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => void recoverAnalysis("continue")}
                  variant="quiet"
                >
                  继续回答下一题
                </Button>
              </div>
              {error ? <p className="auth-error">{error}</p> : null}
            </div>
          ) : reviewingCandidates ? (
            <div>
              <h2>确认这段经历中的事实</h2>
              <p>只有你接受或编辑后的候选，才会进入事实库。</p>
              <ol className="intake-facts">
                {session.fact_candidates.filter((candidate) => candidate.status === "pending").map((candidate, index) => (
                  <li className="intake-fact" key={candidate.id}>
                    <span>{factKindLabel(candidate.kind)}</span>
                    <p>{candidate.value}</p>
                    <p><strong>原回答出处：</strong>{candidate.source_excerpt}</p>
                    <p className="resource-id">字符范围 {candidate.source_start}–{candidate.source_end}</p>
                    {editingCandidateId === candidate.id ? (
                      <Field
                        label={`编辑候选 ${index + 1}`}
                        multiline
                        name={`candidate-${candidate.id}`}
                        onChange={(event: ChangeEvent<HTMLTextAreaElement>) => {
                          const value = event.currentTarget.value;
                          setCandidateEdits((current) => ({
                            ...current,
                            [candidate.id]: value,
                          }));
                        }}
                        value={candidateEdits[candidate.id] ?? candidate.value}
                      />
                    ) : null}
                    <div className="button-row">
                      {candidate.decision_mode === "accept_or_edit" ? (
                        <Button
                          aria-label={`接受候选 ${index + 1}`}
                          disabled={busy}
                          onClick={() => void decideCandidate(candidate, "accept")}
                          variant="secondary"
                        >
                          接受候选
                        </Button>
                      ) : null}
                      <Button
                        aria-label={`${editingCandidateId === candidate.id ? "保存编辑候选" : "编辑候选"} ${index + 1}`}
                        disabled={busy}
                        onClick={() => {
                          if (editingCandidateId === candidate.id) void decideCandidate(candidate, "edit");
                          else setEditingCandidateId(candidate.id);
                        }}
                        variant="secondary"
                      >
                        {editingCandidateId === candidate.id ? "保存编辑" : "编辑候选"}
                      </Button>
                      <Button
                        aria-label={`拒绝候选 ${index + 1}`}
                        disabled={busy}
                        onClick={() => void decideCandidate(candidate, "reject")}
                        variant="quiet"
                      >
                        不采用
                      </Button>
                    </div>
                  </li>
                ))}
              </ol>
              {error ? <p className="auth-error" role="alert">{error}</p> : null}
            </div>
          ) : question ? (
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
                <span>{factKindLabel(fact.kind)}</span>
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
            disabled={busy || draftStatusUncertain || confirmedCount < 2 || !title.trim()}
            onClick={() => void createDraft()}
            state={state === "drafting" ? "loading" : "default"}
          >
            生成基础简历
          </Button>
          {draftFallbackAvailable ? (
            <Button
              disabled={state === "drafting" || !title.trim()}
              onClick={() => void createDraft("rule_fallback")}
              state={state === "drafting" ? "loading" : "default"}
              variant="secondary"
            >
              使用事实原文创建基础草稿
            </Button>
          ) : null}
          {confirmedCount < 2 ? <p>至少确认两条有来源的事实后才可生成。</p> : null}
          {error && !question && !draftStatusUncertain ? <p role="alert">{error}</p> : null}
        </aside>
      </div>
    </Page>
  );
}
