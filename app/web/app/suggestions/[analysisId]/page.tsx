"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { StatusTag } from "../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../features/api/client";
export default function SuggestionsPage() {
  const [decision,setDecision]=useState("待处理");
  const [versionId,setVersionId]=useState("");
  async function decide(action:"accept"|"edit"|"ignore"|"revert", label:string){
    const suggestionId = new URLSearchParams(window.location.search).get("suggestion");
    if (!suggestionId) throw new Error("缺少建议");
    const body = action === "edit" ? { text: "设计访谈提纲并整理访谈结论，用于课程产品方案迭代。" } : {};
    await createWebApiClient().post(`/v1/suggestions/${suggestionId}/${action}`, body, crypto.randomUUID());
    setDecision(label);
  }
  useEffect(()=>{setVersionId(new URLSearchParams(window.location.search).get("version") ?? "");const onKey=(event:KeyboardEvent)=>{const target=event.target as HTMLElement; if(["INPUT","TEXTAREA","SELECT"].includes(target.tagName)||target.isContentEditable)return; const actions={a:["accept","已接受"],e:["edit","编辑中"],i:["ignore","已忽略"],z:["revert","已撤销"]} as const; const action=actions[event.key.toLowerCase() as keyof typeof actions]; if(action)void decide(action[0],action[1]);}; window.addEventListener("keydown",onKey);return()=>window.removeEventListener("keydown",onKey);},[]);
  return <Page eyebrow="建议 1 / 4 · A 接受 · E 编辑 · I 忽略 · Z 撤销" status={{label:decision,tone:decision==="待处理"?"pending":"success"}} title="逐条确认，不批量接受"><div className="suggestion-layout"><section className="panel panel--wide"><p className="eyebrow">JD 原要求</p><h2>能够独立完成用户访谈与结论整理</h2><dl className="comparison"><div><dt>当前原文</dt><dd>负责课程项目的用户调研。</dd></div><div><dt>建议文本</dt><dd>设计访谈提纲并整理访谈结论，用于课程产品方案迭代。</dd></div></dl><p><strong>修改理由：</strong>补充真实动作，不增加未经确认的数字。</p><StatusTag tone="success">风险：低 · 已有事实覆盖</StatusTag><div className="button-row"><Button onClick={()=>void decide("accept","已接受")}>接受</Button><Button onClick={()=>void decide("edit","编辑中")} variant="secondary">编辑</Button><Button onClick={()=>void decide("ignore","已忽略")} variant="quiet">忽略</Button><Button onClick={()=>void decide("revert","已撤销")} variant="quiet">撤销</Button></div>{decision !== "待处理" && versionId ? <Link className="button button--primary" href={`/exports/new?version=${versionId}`}>进入导出</Link> : null}</section><aside className="panel"><h2>事实引用</h2><p>课程产品设计 · 用户确认回答</p><p>来源短语：设计访谈提纲、整理访谈结论</p></aside></div></Page>;
}
