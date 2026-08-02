"use client";

import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { useRouter } from "next/navigation";
import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { Field } from "../../../components/ui/Field";
import { StatusTag } from "../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../features/api/client";

type Requirement = components["schemas"]["RequirementResponse"];
type Job = components["schemas"]["JobResponse"];
type SessionUser = components["schemas"]["MeResponse"];

function confidenceLabel(band: Requirement["confidence_band"] | undefined) {
  if (band === "high") return "高置信";
  if (band === "medium") return "中置信";
  if (band === "low") return "低置信";
  return null;
}

function generationLabel(mode: Requirement["generation_mode"] | undefined) {
  if (mode === "model") return "模型解析";
  if (mode === "rule_fallback") return "基础解析";
  return null;
}

export default function NewJobPage() {
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [jd, setJd] = useState("");
  const [jobId, setJobId] = useState("");
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [ownerId, setOwnerId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [state, setState] = useState<"idle" | "parsing" | "confirming" | "matching" | "error">("idle");
  const [message, setMessage] = useState("");
  const createKey = useRef("");
  const parseKey = useRef("");
  const matchKey = useRef("");
  const requirementKeys = useRef<Record<string, string>>({});
  const router = useRouter();

  useEffect(() => {
    let active = true;
    const query = new URLSearchParams(window.location.search);
    const restoredVersion = query.get("version") ?? "";
    const restoredJob = query.get("job") ?? "";
    setVersionId(restoredVersion);
    const api = createWebApiClient();
    Promise.all([
      api.get<SessionUser>("/v1/me"),
      restoredJob ? api.get<Job>(`/v1/jobs/${restoredJob}`) : Promise.resolve(null),
    ]).then(
      async ([me, job]) => {
        if (!active) return;
        setOwnerId(me.user_id);
        if (job) {
          setJobId(job.id);
          setTitle(job.title);
          setCompany(job.company ?? "");
          setJd(job.raw ?? "");
          setRequirements(job.requirements ?? []);
          if (job.status === "queued" && job.task_id) {
            setState("parsing");
            setMessage("已恢复岗位解析任务，正在继续等待…");
            const api = createWebApiClient();
            try {
              await waitForTask(
                () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${job.task_id}`),
                job.task_id,
              );
              const parsed = await api.get<Job>(`/v1/jobs/${job.id}`);
              if (!active) return;
              setRequirements(parsed.requirements ?? []);
              setState("idle");
              setMessage(parsed.requirements?.length ? "解析完成，请确认每条要求。" : "没有解析到岗位要求，请修改原文后重试。");
            } catch {
              if (!active) return;
              setState("error");
              setMessage("岗位解析任务暂时无法恢复。岗位原文和任务记录仍保留，请稍后重试。");
            }
          } else {
            setMessage(job.requirements?.length ? "已恢复岗位要求，请继续确认。" : "已恢复岗位，继续解析即可。");
          }
        }
      },
      () => {
        if (active) {
          setState("error");
          setMessage("岗位恢复失败。已输入的本机草稿仍会保留。");
        }
      },
    );
    return () => {
      active = false;
    };
  }, []);

  const draftKey = ownerId && versionId ? `job-draft:${ownerId}:${versionId}` : "";

  useEffect(() => {
    if (!draftKey || typeof localStorage === "undefined") return;
    const raw = localStorage.getItem(draftKey);
    if (!raw) return;
    try {
      const draft = JSON.parse(raw) as {
        company?: string;
        expires_at?: number;
        jd?: string;
        owner_id?: string;
        title?: string;
        version_id?: string;
      };
      if (
        draft.owner_id === ownerId
        && draft.version_id === versionId
        && typeof draft.expires_at === "number"
        && draft.expires_at > Date.now()
      ) {
        setTitle((current) => current || draft.title || "");
        setCompany((current) => current || draft.company || "");
        setJd(draft.jd || "");
      } else {
        localStorage.removeItem(draftKey);
      }
    } catch {
      localStorage.removeItem(draftKey);
    }
  }, [draftKey, ownerId, versionId]);

  useEffect(() => {
    if (!draftKey || (!title && !company && !jd) || typeof localStorage === "undefined") return;
    localStorage.setItem(draftKey, JSON.stringify({
      company,
      expires_at: Date.now() + 24 * 60 * 60 * 1000,
      jd,
      owner_id: ownerId,
      title,
      version_id: versionId,
    }));
  }, [company, draftKey, jd, ownerId, title, versionId]);

  async function parseJob() {
    if (!title.trim() || !jd.trim() || ["parsing", "confirming", "matching"].includes(state)) return;
    setState("parsing");
    setMessage("正在解析岗位要求…");
    let currentJobId = jobId;
    let parsingTaskId = "";
    try {
      const api = createWebApiClient();
      if (!currentJobId) {
        const body: components["schemas"]["JobCreate"] = {
          company: company.trim() || null,
          raw: jd,
          title,
        };
        createKey.current ||= crypto.randomUUID();
        const job = await api.post<typeof body, Job>("/v1/jobs", body, createKey.current);
        currentJobId = job.id;
        setJobId(job.id);
        const query = new URLSearchParams(window.location.search);
        query.set("job", job.id);
        window.history.replaceState({}, "", `/jobs/new?${query.toString()}`);
      }
      parseKey.current ||= crypto.randomUUID();
      const parsing = await api.post<{}, components["schemas"]["JobResponse"]>(
        `/v1/jobs/${currentJobId}/parse`,
        {},
        parseKey.current,
      );
      if (!parsing.task_id) throw new Error("岗位解析任务未创建");
      parsingTaskId = parsing.task_id;
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${parsing.task_id}`),
        parsing.task_id,
      );
      const parsed = await api.get<Job>(`/v1/jobs/${currentJobId}`);
      setJobId(parsed.id);
      setRequirements(parsed.requirements ?? []);
      setState("idle");
      setMessage(parsed.requirements?.length ? "解析完成，请确认每条要求。" : "没有解析到岗位要求，请修改原文后重试。");
    } catch {
      const terminal = parsingTaskId
        ? await createWebApiClient().get<components["schemas"]["TaskResponse"]>(
            `/v1/tasks/${parsingTaskId}`,
          ).catch(() => null)
        : null;
      if (terminal && ["failed", "cancelled"].includes(terminal.status)) parseKey.current = "";
      setState("error");
      setMessage("岗位解析失败。JD 原文仍保留，请重试。");
    }
  }

  function updateRequirement(index: number, patch: Partial<Requirement>) {
    setRequirements((items) => items.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...patch, confirmed: false } : item
    )));
  }

  async function confirmAll() {
    if (!jobId || requirements.length === 0 || state === "confirming") return;
    setState("confirming");
    setMessage("正在保存确认结果…");
    try {
      const api = createWebApiClient();
      const confirmed = await Promise.all(requirements.map((requirement) => {
        const body: components["schemas"]["RequirementUpdate"] = {
          confirmed: true,
          priority: requirement.priority,
          text: requirement.text,
          type: requirement.type as components["schemas"]["RequirementUpdate"]["type"],
        };
        requirementKeys.current[requirement.id] ||= crypto.randomUUID();
        return api.patch<typeof body, Requirement>(
          `/v1/jobs/${jobId}/requirements/${requirement.id}`,
          body,
          requirementKeys.current[requirement.id],
        );
      }));
      setRequirements(confirmed);
      setState("idle");
      setMessage("岗位要求已确认，可以开始匹配。");
    } catch {
      setState("error");
      setMessage("确认失败。已编辑的要求仍保留在页面中。");
    }
  }

  async function matchJob() {
    if (!versionId || !jobId || requirements.some((item) => !item.confirmed)) return;
    setState("matching");
    setMessage("正在创建匹配分析…");
    try {
      const body: components["schemas"]["MatchCreate"] = {
        job_id: jobId,
        resume_version_id: versionId,
      };
      const api = createWebApiClient();
      matchKey.current ||= crypto.randomUUID();
      const analysis = await api.post<typeof body, components["schemas"]["MatchResponse"]>(
        "/v1/match-analyses",
        body,
        matchKey.current,
      );
      if (!analysis.task_id) throw new Error("匹配任务未创建");
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${analysis.task_id}`),
        analysis.task_id,
      );
      if (typeof localStorage !== "undefined" && draftKey) localStorage.removeItem(draftKey);
      router.push(`/jobs/${jobId}/match?analysis=${analysis.id}&version=${versionId}`);
    } catch {
      setState("error");
      setMessage("匹配失败。岗位要求和简历版本均已保留。");
    }
  }

  const allConfirmed = requirements.length > 0 && requirements.every((item) => item.confirmed);
  const busy = ["parsing", "confirming", "matching"].includes(state);
  return (
    <Page
      eyebrow="岗位信息"
      status={message ? { label: message, tone: state === "error" ? "error" : busy ? "pending" : "success" } : undefined}
      title="粘贴并确认目标岗位"
    >
      <div className="split-layout">
        <section className="panel panel--wide">
          <Field label="岗位名称" name="job-title" onChange={(event: ChangeEvent<HTMLInputElement>) => setTitle(event.currentTarget.value)} required value={title} />
          <Field label="公司（可选）" name="job-company" onChange={(event: ChangeEvent<HTMLInputElement>) => setCompany(event.currentTarget.value)} value={company} />
          <Field label="JD 原文" maxLength={20_000} multiline name="jd" onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setJd(event.currentTarget.value)} placeholder="粘贴岗位职责和任职要求" value={jd} />
          <Button disabled={busy || !title.trim() || !jd.trim()} onClick={() => void parseJob()} state={state === "parsing" ? "loading" : state === "error" ? "error" : "default"}>解析岗位要求</Button>
        </section>
        <aside className="panel">
          <h2>解析结果</h2>
          {requirements.length === 0 ? <p>解析后逐条确认，不使用不可解释的总分。</p> : (
            <>
              <div className="resume-list">
                {requirements.map((requirement, index) => (
                  <article className="audit-card" key={requirement.id}>
                    <Field label={`岗位要求 ${index + 1}`} multiline name={`requirement-${index}`} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => updateRequirement(index, { text: event.currentTarget.value })} value={requirement.text} />
                    <label className="field"><span className="field__label">类型</span>
                      <select className="field__control" onChange={(event) => updateRequirement(index, { type: event.currentTarget.value as Requirement["type"] })} value={requirement.type}>
                        <option value="must_have">必须</option><option value="preferred">加分</option><option value="responsibility">职责</option><option value="other">其他</option>
                      </select>
                    </label>
                    <div className="button-row" aria-label="解析依据">
                      {confidenceLabel(requirement.confidence_band) ? (
                        <StatusTag tone={requirement.confidence_band === "low" ? "pending" : "info"}>
                          {confidenceLabel(requirement.confidence_band)}
                        </StatusTag>
                      ) : null}
                      {generationLabel(requirement.generation_mode) ? (
                        <StatusTag tone="info">{generationLabel(requirement.generation_mode)}</StatusTag>
                      ) : null}
                      {requirement.source_range ? (
                        <span className="resource-id">原文字符 {requirement.source_range.start}–{requirement.source_range.end}</span>
                      ) : null}
                    </div>
                    <StatusTag tone={requirement.confirmed ? "success" : "pending"}>{requirement.confirmed ? "已确认" : "待确认"}</StatusTag>
                  </article>
                ))}
              </div>
              {!allConfirmed ? <Button disabled={busy} onClick={() => void confirmAll()} state={state === "confirming" ? "loading" : "default"}>确认全部要求</Button> : <Button disabled={busy} onClick={() => void matchJob()} state={state === "matching" ? "loading" : "default"}>开始匹配</Button>}
            </>
          )}
        </aside>
      </div>
      {state === "error" ? <p className="auth-error" role="alert">{message}</p> : null}
    </Page>
  );
}
