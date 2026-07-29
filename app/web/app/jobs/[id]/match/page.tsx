"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { createWebApiClient } from "../../../../features/api/client";
export default function MatchPage() {
  const router = useRouter();
  const [groups, setGroups] = useState<string[][]>([]);
  const [ready, setReady] = useState(false);
  const [message, setMessage] = useState("正在读取匹配任务状态。");
  useEffect(() => {
    const analysisId = new URLSearchParams(window.location.search).get("analysis");
    if (!analysisId) return;
    const load = async () => {
      try {
        const api = createWebApiClient();
        let analysis = await api.get<components["schemas"]["MatchResponse"]>(`/v1/match-analyses/${analysisId}`);
        if (analysis.status !== "succeeded") {
          if (!analysis.task_id) throw new Error("匹配任务未创建");
          await waitForTask(() => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${analysis.task_id}`), analysis.task_id);
          analysis = await api.get<components["schemas"]["MatchResponse"]>(`/v1/match-analyses/${analysisId}`);
        }
        const counts = new Map<string, number>();
        for (const item of analysis.items) counts.set(item.category, (counts.get(item.category) ?? 0) + 1);
        setGroups([["已有证据", "proved"], ["表达不足", "underexpressed"], ["需要确认", "needs_confirmation"], ["真实缺口", "real_gap"]]
          .map(([label, key]) => [label, key, String(counts.get(key) ?? 0)]));
        setReady(true);
        setMessage("匹配已完成，可逐条查看建议。");
      } catch {
        setMessage("匹配尚未完成或已失败；已保存的简历版本不会丢失。");
      }
    };
    void load();
  }, []);
  async function openSuggestions(){
    const analysisId = new URLSearchParams(window.location.search).get("analysis");
    if (!analysisId) throw new Error("缺少匹配分析");
    const versionId = new URLSearchParams(window.location.search).get("version") ?? "";
    const suggestions = await createWebApiClient().get<{ items: { id: string }[] }>(`/v1/match-analyses/${analysisId}/suggestions`);
    const suggestionId = suggestions.items[0]?.id;
    if (!suggestionId) throw new Error("没有可处理建议");
    router.push(`/suggestions/${analysisId}?suggestion=${suggestionId}&version=${versionId}`);
  }
  return <Page actions={<Button disabled={!ready} onClick={() => void openSuggestions()}>逐条处理建议</Button>} eyebrow="匹配报告 · 不使用 ATS 总分" status={{ label: message, tone: ready ? "success" : "pending" }} title="岗位要求与事实证据"><section className="match-grid">{groups.map(([label,key,count])=><article className="match-card" key={key}><span>{key}</span><strong>{count}</strong><h2>{label}</h2><details><summary>查看要求与证据</summary><p>完成后将展示服务端返回的岗位要求和事实链接。</p></details></article>)}</section></Page>;
}
