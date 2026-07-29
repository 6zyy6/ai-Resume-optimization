"use client";
import { type ChangeEvent, useState } from "react";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { useRouter } from "next/navigation";
import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { Field } from "../../../components/ui/Field";
import { createWebApiClient } from "../../../features/api/client";
export default function NewJobPage() {
  const [parsed, setParsed] = useState(false);
  const [jd, setJd] = useState("");
  const [jobId, setJobId] = useState("");
  const router = useRouter();
  async function parseJob() {
    const body: components["schemas"]["JobCreate"] = { raw: jd, title: "目标岗位" };
    const job = await createWebApiClient().post<typeof body, components["schemas"]["JobResponse"]>("/v1/jobs", body, crypto.randomUUID());
    const api = createWebApiClient();
    const parsing = await api.post<{}, components["schemas"]["JobResponse"]>(`/v1/jobs/${job.id}/parse`, {}, crypto.randomUUID());
    if (!parsing.task_id) throw new Error("岗位解析任务未创建");
    await waitForTask(() => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${parsing.task_id}`), parsing.task_id);
    setJobId(job.id);
    setParsed(true);
  }
  async function matchJob() {
    const resumeVersionId = new URLSearchParams(window.location.search).get("version");
    if (!resumeVersionId) throw new Error("缺少简历版本");
    const body: components["schemas"]["MatchCreate"] = { job_id: jobId, resume_version_id: resumeVersionId };
    const api = createWebApiClient();
    const analysis = await api.post<typeof body, components["schemas"]["MatchResponse"]>("/v1/match-analyses", body, crypto.randomUUID());
    if (!analysis.task_id) throw new Error("匹配任务未创建");
    await waitForTask(() => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${analysis.task_id}`), analysis.task_id);
    router.push(`/jobs/${jobId}/match?analysis=${analysis.id}&version=${resumeVersionId}`);
  }
  return <Page eyebrow="岗位信息" title="粘贴并确认目标岗位"><div className="split-layout"><section className="panel panel--wide"><Field label="JD 原文" maxLength={20_000} multiline name="jd" onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setJd(event.currentTarget.value)} placeholder="粘贴岗位职责和任职要求" value={jd} /><Button onClick={() => void parseJob()}>解析岗位要求</Button></section><aside className="panel"><h2>解析结果</h2>{parsed ? <><ul className="list"><li><strong>用户研究与分析</strong><span>重要 · 可编辑</span></li><li><strong>内容策划能力</strong><span>重要 · 可编辑</span></li></ul><Button onClick={() => void matchJob()}>确认并开始匹配</Button></> : <p>解析后逐条确认，不使用不可解释的总分。</p>}</aside></div></Page>;
}
