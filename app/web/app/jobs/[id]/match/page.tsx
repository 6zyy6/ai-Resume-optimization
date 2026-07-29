"use client";

import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { createWebApiClient } from "../../../../features/api/client";
import { EvidenceList } from "../../../../features/facts/EvidenceList";

const categories = [
  ["已有证据", "proved"],
  ["表达不足", "underexpressed"],
  ["需要确认", "needs_confirmation"],
  ["真实缺口", "real_gap"],
] as const;

export default function MatchPage() {
  const { id: jobId } = useParams<{ id: string }>();
  const router = useRouter();
  const [analysis, setAnalysis] = useState<components["schemas"]["MatchResponse"] | null>(null);
  const [job, setJob] = useState<components["schemas"]["JobResponse"] | null>(null);
  const [message, setMessage] = useState("正在读取匹配任务状态。");
  const [error, setError] = useState(false);

  useEffect(() => {
    const analysisId = new URLSearchParams(window.location.search).get("analysis");
    if (!analysisId) {
      setError(true);
      setMessage("缺少匹配分析 ID，请从岗位确认页重新开始。");
      return;
    }
    let active = true;
    const load = async () => {
      try {
        const api = createWebApiClient();
        let loaded = await api.get<components["schemas"]["MatchResponse"]>(`/v1/match-analyses/${analysisId}`);
        if (loaded.status !== "succeeded") {
          if (!loaded.task_id) throw new Error("匹配任务未创建");
          await waitForTask(
            () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${loaded.task_id}`),
            loaded.task_id,
          );
          loaded = await api.get<components["schemas"]["MatchResponse"]>(`/v1/match-analyses/${analysisId}`);
        }
        const loadedJob = await api.get<components["schemas"]["JobResponse"]>(`/v1/jobs/${jobId}`);
        if (!active) return;
        setAnalysis(loaded);
        setJob(loadedJob);
        setMessage("匹配已完成，可查看每条岗位要求和事实引用。");
      } catch {
        if (active) {
          setError(true);
          setMessage("匹配尚未完成或已失败；已保存的简历版本不会丢失。");
        }
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [jobId]);

  async function openSuggestions() {
    if (!analysis) return;
    try {
      const versionId = new URLSearchParams(window.location.search).get("version") ?? analysis.resume_version_id;
      const suggestions = await createWebApiClient().get<components["schemas"]["SuggestionListResponse"]>(
        `/v1/match-analyses/${analysis.id}/suggestions`,
      );
      const suggestionId = suggestions.items[0]?.id;
      if (!suggestionId) {
        setError(true);
        setMessage("这次匹配没有可处理建议。");
        return;
      }
      router.push(`/suggestions/${analysis.id}?suggestion=${suggestionId}&version=${versionId}`);
    } catch {
      setError(true);
      setMessage("建议读取失败，请稍后重试。");
    }
  }

  const requirements = new Map((job?.requirements ?? []).map((item) => [item.id, item]));
  return (
    <Page
      actions={<Button disabled={!analysis || error} onClick={() => void openSuggestions()}>逐条处理建议</Button>}
      eyebrow="匹配报告 · 不使用 ATS 总分"
      status={{ label: message, tone: error ? "error" : analysis ? "success" : "pending" }}
      title="岗位要求与事实证据"
    >
      {analysis ? (
        <section className="match-grid">
          {categories.map(([label, key]) => {
            const items = analysis.items.filter((item) => item.category === key);
            return (
              <article className="match-card" key={key}>
                <span>{key}</span>
                <strong>{items.length}</strong>
                <h2>{label}</h2>
                {items.length === 0 ? <p>没有此类要求。</p> : items.map((item) => (
                  <details key={item.id} open>
                    <summary>{requirements.get(item.requirement_id)?.text ?? `岗位要求 ${item.requirement_id}`}</summary>
                    <EvidenceList factIds={item.evidence_refs} />
                  </details>
                ))}
              </article>
            );
          })}
        </section>
      ) : <section className="panel" role={error ? "alert" : "status"}>{message}</section>}
    </Page>
  );
}
