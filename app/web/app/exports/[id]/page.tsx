"use client";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { StatusTag } from "../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../features/api/client";
export default function ExportPage(){
  const [zoom,setZoom]=useState("适宽");
  const [downloaded,setDownloaded]=useState(false);
  async function download(){
    const versionId = new URLSearchParams(window.location.search).get("version");
    if (!versionId) throw new Error("缺少简历版本");
    const body: components["schemas"]["ExportCreate"] = { resume_version_id: versionId, template_version: "clear-standard" };
    const exported = await createWebApiClient().post<typeof body, components["schemas"]["ExportResponse"]>("/v1/exports", body, crypto.randomUUID());
    await createWebApiClient().get(`/v1/exports/${exported.id}`);
    setDownloaded(true);
  }
  return <Page actions={<Button onClick={()=>void download()} state={downloaded?"success":"default"}>{downloaded?"下载已准备":"下载 PDF"}</Button>} eyebrow="导出结果" status={{label:"事实检查通过",tone:"success"}} title="产品运营实习 · 岗位版"><div className="preview-layout"><aside className="panel"><h2>模板与缩放</h2><label className="field"><span className="field__label">模板</span><select className="field__control"><option>清晰标准</option><option>现代留白</option></select><span className="field__message">切换模板不会修改内容</span></label><div className="button-row">{["100%","适宽","整页"].map(x=><Button key={x} onClick={()=>setZoom(x)} variant={zoom===x?"secondary":"quiet"}>{x}</Button>)}</div><StatusTag tone="success">完整性检查通过</StatusTag></aside><article className="a4-preview"><h2>张同学</h2><p>产品运营实习</p><hr/><h3>项目经历</h3><p>设计访谈提纲并整理访谈结论，用于课程产品方案迭代。</p><span className="page-break">第 1 页</span></article></div></Page>
}
