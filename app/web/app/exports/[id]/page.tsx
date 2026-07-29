"use client";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { StatusTag } from "../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../features/api/client";
export default function ExportPage(){
  const [zoom,setZoom]=useState("适宽");
  const [downloaded,setDownloaded]=useState(false);
  const [status, setStatus] = useState("尚未执行事实检查");
  async function download(){
    const versionId = new URLSearchParams(window.location.search).get("version");
    if (!versionId) throw new Error("缺少简历版本");
    const body: components["schemas"]["ExportCreate"] = { resume_version_id: versionId, template_version: "clear-standard" };
    const api = createWebApiClient();
    const exported = await api.post<typeof body, components["schemas"]["ExportResponse"]>("/v1/exports", body, crypto.randomUUID());
    if (!exported.task_id) throw new Error("导出任务未创建");
    setStatus("正在检查事实并生成 PDF");
    await waitForTask(() => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${exported.task_id}`), exported.task_id);
    const ready = await api.get<components["schemas"]["ExportResponse"]>(`/v1/exports/${exported.id}`);
    if (!ready.download_url) throw new Error("导出文件尚未就绪");
    window.location.assign(ready.download_url);
    setStatus("事实检查通过，PDF 已生成");
    setDownloaded(true);
  }
  return <Page actions={<Button onClick={()=>void download()} state={downloaded?"success":"default"}>{downloaded?"下载已准备":"下载 PDF"}</Button>} eyebrow="导出结果" status={{label:status,tone:downloaded?"success":"pending"}} title="导出简历"><div className="preview-layout"><aside className="panel"><h2>模板与缩放</h2><label className="field"><span className="field__label">模板</span><select className="field__control"><option>清晰标准</option><option>现代留白</option></select><span className="field__message">切换模板不会修改内容</span></label><div className="button-row">{["100%","适宽","整页"].map(x=><Button key={x} onClick={()=>setZoom(x)} variant={zoom===x?"secondary":"quiet"}>{x}</Button>)}</div><StatusTag tone={downloaded?"success":"pending"}>{status}</StatusTag></aside><article className="a4-preview"><p>生成完成后将在这里提供与 PDF 一致的预览。</p></article></div></Page>
}
