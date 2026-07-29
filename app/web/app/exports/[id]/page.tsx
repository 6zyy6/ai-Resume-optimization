"use client";

import { ApiError } from "@resume/shared/client";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { StatusTag } from "../../../components/ui/StatusTag";
import { apiBrowserUrl, createWebApiClient } from "../../../features/api/client";

type ExportResource = components["schemas"]["ExportResponse"];
type TemplateVersion = "clear-standard" | "modern-whitespace";
type ExportState = "idle" | "loading" | "running" | "ready" | "error";

function exportError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "EXPORT_FACT_CHECK_FAILED") {
      return "存在待确认、无来源或证据不完整的声明，不能导出。请返回编辑器查看事实检查。";
    }
    if (error.code === "RESOURCE_NOT_FOUND") return "导出记录不存在或已过期。";
    return error.message || "导出请求没有完成。";
  }
  return "导出请求没有完成；版本与模板选择已保留，可以重试。";
}

export default function ExportPage() {
  const params = useParams<{ id: string }>();
  const [zoom, setZoom] = useState("适宽");
  const [template, setTemplate] = useState<TemplateVersion>("clear-standard");
  const [resource, setResource] = useState<ExportResource | null>(null);
  const [state, setState] = useState<ExportState>(params.id === "new" ? "idle" : "loading");
  const [message, setMessage] = useState(
    params.id === "new" ? "尚未执行事实检查" : "正在读取导出记录",
  );
  const createKey = useRef("");

  const finishExport = async (current: ExportResource) => {
    const api = createWebApiClient();
    if (current.task_id && current.status !== "succeeded") {
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${current.task_id}`),
        current.task_id,
      );
    }
    return api.get<ExportResource>(`/v1/exports/${current.id}`);
  };

  useEffect(() => {
    if (params.id === "new") return;
    let active = true;
    setState("loading");
    const api = createWebApiClient();
    api.get<ExportResource>(`/v1/exports/${params.id}`).then(
      async (loaded) => {
        if (!active) return;
        setResource(loaded);
        if (loaded.template_version === "clear-standard" || loaded.template_version === "modern-whitespace") {
          setTemplate(loaded.template_version);
        }
        if (loaded.status === "succeeded") {
          setState("ready");
          setMessage("事实检查通过，PDF 已生成");
          return;
        }
        if (loaded.status === "failed") {
          setState("error");
          setMessage("上次导出失败，可以使用相同版本重新生成。");
          return;
        }
        setState("running");
        setMessage(`导出状态：${loaded.status}`);
        try {
          const ready = await finishExport(loaded);
          if (!active) return;
          setResource(ready);
          if (ready.status === "succeeded" && ready.download_url) {
            setState("ready");
            setMessage("事实检查通过，PDF 已生成");
          } else {
            setState("error");
            setMessage(`导出未成功：${ready.status}`);
          }
        } catch (error) {
          if (!active) return;
          const latest = await api.get<ExportResource>(`/v1/exports/${loaded.id}`).catch(() => loaded);
          setResource(latest);
          setState("error");
          setMessage(exportError(error));
        }
      },
      (error: unknown) => {
        if (!active) return;
        setState("error");
        setMessage(exportError(error));
      },
    );
    return () => {
      active = false;
    };
  }, [params.id]);

  const generate = async () => {
    if (state === "running") return;
    const versionId = (
      new URLSearchParams(window.location.search).get("version")
      || resource?.resume_version_id
    );
    if (!versionId) {
      setState("error");
      setMessage("缺少简历版本，无法创建导出。");
      return;
    }
    setState("running");
    setMessage("正在执行事实检查并生成 PDF");
    let queuedResource: ExportResource | null = null;
    try {
      const body: components["schemas"]["ExportCreate"] = {
        resume_version_id: versionId,
        template_version: template,
      };
      createKey.current ||= crypto.randomUUID();
      const queued = await createWebApiClient().post<typeof body, ExportResource>(
        "/v1/exports",
        body,
        createKey.current,
      );
      queuedResource = queued;
      setResource(queued);
      window.history.replaceState({}, "", `/exports/${queued.id}?version=${encodeURIComponent(versionId)}`);
      const ready = await finishExport(queued);
      if (!ready.download_url || ready.status !== "succeeded") {
        throw new Error("Export completed without a downloadable resource");
      }
      setResource(ready);
      setState("ready");
      setMessage("事实检查通过，PDF 已生成");
      createKey.current = "";
    } catch (error) {
      if (queuedResource?.task_id) {
        const api = createWebApiClient();
        const terminal = await api.get<components["schemas"]["TaskResponse"]>(
          `/v1/tasks/${queuedResource.task_id}`,
        ).catch(() => null);
        if (terminal && ["failed", "cancelled"].includes(terminal.status)) {
          const failedResource = queuedResource;
          const latest = await api.get<ExportResource>(
            `/v1/exports/${failedResource.id}`,
          ).catch(() => failedResource);
          setResource(latest);
          if (latest.status === "failed") createKey.current = "";
        }
      }
      setState("error");
      setMessage(exportError(error));
    }
  };

  const refresh = async () => {
    if (!resource) {
      await generate();
      return;
    }
    setState("running");
    setMessage("正在刷新导出状态");
    try {
      const ready = await finishExport(resource);
      setResource(ready);
      setState(ready.status === "succeeded" ? "ready" : "running");
      setMessage(ready.status === "succeeded" ? "事实检查通过，PDF 已生成" : `导出状态：${ready.status}`);
    } catch (error) {
      const latest = await createWebApiClient().get<ExportResource>(
        `/v1/exports/${resource.id}`,
      ).catch(() => null);
      if (latest) {
        setResource(latest);
        if (latest.status === "failed") createKey.current = "";
      }
      setState("error");
      setMessage(exportError(error));
    }
  };

  const browserUrl = resource?.download_url ? apiBrowserUrl(resource.download_url) : null;
  const tone = state === "ready" ? "success" : state === "error" ? "error" : "pending";
  const actions = state === "ready" && browserUrl ? (
    <a className="button button--primary" href={browserUrl}>下载 PDF</a>
  ) : (
    <Button
      disabled={state === "loading" || state === "running"}
      onClick={() => void (
        state === "error" && resource && resource.status !== "failed"
          ? refresh()
          : generate()
      )}
      state={state === "running" ? "loading" : state === "error" ? "error" : "default"}
    >
      {state === "error" && resource?.status === "failed" ? "重新生成 PDF" : state === "error" && resource ? "继续检查" : "生成 PDF"}
    </Button>
  );

  return (
    <Page
      actions={actions}
      eyebrow="预览与导出"
      status={{ label: message, tone }}
      title="导出简历"
    >
      <div className="preview-layout">
        <aside className="panel">
          <h2>模板与缩放</h2>
          <div className="field">
            <label className="field__label" htmlFor="export-template">模板</label>
            <select
              className="field__control"
              disabled={params.id !== "new" || state === "running"}
              id="export-template"
              onChange={(event) => setTemplate(event.currentTarget.value as TemplateVersion)}
              value={template}
            >
              <option value="clear-standard">清晰标准</option>
              <option value="modern-whitespace">现代留白</option>
            </select>
            <span className="field__message">模板版本：{template}。切换模板不会修改简历内容。</span>
          </div>
          <div className="button-row">
            {["100%", "适宽", "整页"].map((item) => (
              <Button key={item} onClick={() => setZoom(item)} variant={zoom === item ? "secondary" : "quiet"}>
                {item}
              </Button>
            ))}
          </div>
          <StatusTag tone={tone}>{message}</StatusTag>
          {resource ? (
            <dl className="comparison">
              <div><dt>导出记录</dt><dd className="resource-id">{resource.id}</dd></div>
              <div><dt>文件名</dt><dd>{resource.download_name}</dd></div>
              <div><dt>内容哈希</dt><dd className="resource-id">{resource.content_hash}</dd></div>
            </dl>
          ) : null}
          {state === "error" ? <p role="alert">{message}</p> : null}
        </aside>
        <article className={`a4-preview export-preview export-preview--${zoom}`}>
          {browserUrl ? (
            <iframe className="pdf-frame" src={browserUrl} title="PDF 预览" />
          ) : (
            <div role={state === "running" || state === "loading" ? "status" : undefined}>
              <h2>{state === "error" ? "暂时无法预览" : "PDF 预览"}</h2>
              <p>{state === "running" || state === "loading" ? "生成完成后，这里会显示同一份 PDF 资源。" : "选择模板并生成后，可以在这里核对最终文件。"}</p>
            </div>
          )}
        </article>
      </div>
    </Page>
  );
}
