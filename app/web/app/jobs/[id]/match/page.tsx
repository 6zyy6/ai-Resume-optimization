"use client";
import { useRouter } from "next/navigation";
import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { createWebApiClient } from "../../../../features/api/client";
const groups = [["已有证据","proved","3"],["表达不足","underexpressed","2"],["需要确认","needs_confirmation","1"],["真实缺口","real_gap","1"]];
export default function MatchPage() {
  const router = useRouter();
  async function openSuggestions(){
    const analysisId = new URLSearchParams(window.location.search).get("analysis");
    if (!analysisId) throw new Error("缺少匹配分析");
    const versionId = new URLSearchParams(window.location.search).get("version") ?? "";
    const suggestions = await createWebApiClient().get<{ items: { id: string }[] }>(`/v1/match-analyses/${analysisId}/suggestions`);
    const suggestionId = suggestions.items[0]?.id;
    if (!suggestionId) throw new Error("没有可处理建议");
    router.push(`/suggestions/${analysisId}?suggestion=${suggestionId}&version=${versionId}`);
  }
  return <Page actions={<Button onClick={() => void openSuggestions()}>逐条处理建议</Button>} eyebrow="匹配报告 · 不使用 ATS 总分" title="岗位要求与事实证据"><section className="match-grid">{groups.map(([label,key,count])=><article className="match-card" key={key}><span>{key}</span><strong>{count}</strong><h2>{label}</h2><details><summary>查看要求与证据</summary><p>JD 原文：能够独立完成用户访谈与结论整理。</p><p>关联事实：课程产品设计 · 已确认</p><p>下一步：补充具体访谈动作。</p></details></article>)}</section></Page>;
}
