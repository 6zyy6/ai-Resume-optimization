"use client";

import { ApiError } from "@resume/shared/client";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { useParams, useRouter } from "next/navigation";
import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { Field } from "../../../../components/ui/Field";
import { StatusTag } from "../../../../components/ui/StatusTag";
import { apiBrowserUrl, createWebApiClient } from "../../../../features/api/client";

type DraftFact = components["schemas"]["ImportedFactInput"] & { id: string };

function toDraftFacts(items: components["schemas"]["ImportResponse"]["draft_facts"]): DraftFact[] {
  return items.flatMap((item, index) => {
    const kind = typeof item.kind === "string" ? item.kind : "";
    const value = typeof item.value === "string" ? item.value : "";
    return kind && value ? [{ draft_index: index, id: `draft-${index}`, kind, value }] : [];
  });
}

function finalizedPath(resource: components["schemas"]["ImportResponse"]): string | null {
  return resource.status === "confirmed" && resource.resume_id && resource.version_id
    ? `/resumes/${resource.resume_id}/edit?version=${resource.version_id}`
    : null;
}

export default function ImportConfirmPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [resource, setResource] = useState<components["schemas"]["ImportResponse"] | null>(null);
  const [draftFacts, setDraftFacts] = useState<DraftFact[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [fallbackText, setFallbackText] = useState("");
  const [resumeTitle, setResumeTitle] = useState("导入基础简历");
  const [state, setState] = useState<"idle" | "loading" | "uploading" | "parsing" | "saving" | "error">(
    id === "new" ? "idle" : "loading",
  );
  const [message, setMessage] = useState("");
  const operationKeys = useRef<Record<string, string>>({});

  useEffect(() => {
    if (id === "new") return;
    let active = true;
    createWebApiClient().get<components["schemas"]["ImportResponse"]>(`/v1/imports/${id}`).then(
      (imported) => {
        if (!active) return;
        const destination = finalizedPath(imported);
        if (destination) {
          router.push(destination);
          return;
        }
        setResource(imported);
        setDraftFacts(toDraftFacts(imported.draft_facts));
        setState("idle");
      },
      () => {
        if (active) {
          setMessage("无法读取导入结果，请从任务中心重试。");
          setState("error");
        }
      },
    );
    return () => {
      active = false;
    };
  }, [id]);

  async function uploadAndParse() {
    if (!file || state === "uploading" || state === "parsing") return;
    const api = createWebApiClient();
    setState("uploading");
    setMessage("正在上传文件…");
    try {
      const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
      const tokenBody: components["schemas"]["UploadTokenRequest"] = {
        display_name: file.name,
        mime: file.type || "text/plain",
        purpose: "resume_import",
        sha256,
        size: file.size,
      };
      operationKeys.current.uploadToken ||= crypto.randomUUID();
      const token = await api.post<typeof tokenBody, components["schemas"]["UploadTokenResponse"]>(
        "/v1/files/upload-tokens",
        tokenBody,
        operationKeys.current.uploadToken,
      );
      const uploaded = await fetch(apiBrowserUrl(token.upload_url), { body: file, method: "PUT" });
      if (!uploaded.ok) throw new Error("upload failed");
      operationKeys.current.confirmUpload ||= crypto.randomUUID();
      await api.post(`/v1/files/${token.file_id}/confirm-upload`, {}, operationKeys.current.confirmUpload);
      const importBody: components["schemas"]["ImportCreate"] = { file_id: token.file_id };
      operationKeys.current.createImport ||= crypto.randomUUID();
      const imported = await api.post<typeof importBody, components["schemas"]["ImportResponse"]>(
        "/v1/imports",
        importBody,
        operationKeys.current.createImport,
      );
      if (!imported.task_id) throw new Error("missing task");
      setState("parsing");
      setMessage("上传完成，正在解析文件…");
      await waitForTask(
        () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${imported.task_id}`),
        imported.task_id,
      );
      const parsed = await api.get<components["schemas"]["ImportResponse"]>(`/v1/imports/${imported.id}`);
      setResource(parsed);
      setDraftFacts(toDraftFacts(parsed.draft_facts));
      setResumeTitle(file.name.replace(/\.(pdf|docx|txt)$/i, "") || "导入基础简历");
      setState("idle");
      setMessage(parsed.status === "needs_paste" ? "文件无法解析，请粘贴文本继续。" : "解析完成，请逐条确认。");
      window.history.replaceState({}, "", `/imports/${parsed.id}/confirm`);
      operationKeys.current = {};
    } catch {
      setState("error");
      setMessage("上传或解析失败。你可以更换文件，或粘贴文本继续。");
    }
  }

  function updateFact(index: number, patch: Partial<DraftFact>) {
    setDraftFacts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  }

  function usePastedText() {
    const lines = fallbackText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    setDraftFacts(lines.map((value, index) => ({ draft_index: null, id: `paste-${index}`, kind: "resume_text", value })));
    setMessage(`已从粘贴文本中保留 ${lines.length} 条，请逐条确认。`);
  }

  async function deleteSource() {
    if (!resource) {
      setFile(null);
      return;
    }
    setState("saving");
    try {
      operationKeys.current.deleteSource ||= crypto.randomUUID();
      await createWebApiClient().delete(`/v1/files/${resource.file_id}`, operationKeys.current.deleteSource);
      delete operationKeys.current.deleteSource;
      setResource(null);
      setDraftFacts([]);
      setFile(null);
      setMessage("源文件已删除。");
      setState("idle");
    } catch {
      setMessage("删除失败，请稍后重试。");
      setState("error");
    }
  }

  async function confirmImport() {
    if (!resource || draftFacts.length === 0 || state === "saving") return;
    setState("saving");
    setMessage("正在保存事实与首个简历版本…");
    try {
      const api = createWebApiClient();
      const facts = draftFacts.map(({ draft_index, kind, value }) => ({
        draft_index,
        kind: kind.trim(),
        value: value.trim(),
      })).filter((fact) => fact.kind && fact.value);
      const confirmBody: components["schemas"]["ImportConfirm"] = {
        facts,
        title: resumeTitle.trim(),
      };
      operationKeys.current.finalize ||= crypto.randomUUID();
      const confirmed = await api.post<typeof confirmBody, components["schemas"]["ImportResponse"]>(
        `/v1/imports/${resource.id}/confirm`,
        confirmBody,
        operationKeys.current.finalize,
      );
      if (!confirmed.resume_id || !confirmed.version_id) throw new Error("finalize result missing");
      delete operationKeys.current.finalize;
      router.push(`/resumes/${confirmed.resume_id}/edit?version=${confirmed.version_id}`);
    } catch (error) {
      if (error instanceof ApiError && error.code === "IMPORT_ALREADY_FINALIZED") {
        try {
          const finalized = await createWebApiClient().get<components["schemas"]["ImportResponse"]>(
            `/v1/imports/${resource.id}`,
          );
          const destination = finalizedPath(finalized);
          if (destination) {
            delete operationKeys.current.finalize;
            router.push(destination);
            return;
          }
        } catch {
          // Keep the existing draft and retry key when recovery is uncertain.
        }
      }
      setMessage("保存失败。已解析的字段仍保留在页面中，请重试。");
      setState("error");
    }
  }

  return (
    <Page
      eyebrow="导入并确认"
      status={message ? { label: message, tone: state === "error" ? "error" : state === "idle" ? "success" : "pending" } : undefined}
      title={resource ? "逐条核对解析结果" : "上传已有简历"}
    >
      <aside className="rule-strip">支持 PDF、DOCX、TXT · 最大 10 MiB · PDF 最多 10 页 · 不支持扫描件 OCR</aside>
      {!resource ? (
        <section className="panel">
          <label className="field">
            <span className="field__label">选择简历文件</span>
            <input
              accept=".pdf,.docx,.txt"
              aria-label="选择简历文件"
              disabled={state === "uploading" || state === "parsing"}
              onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
              type="file"
            />
            <span className="field__message">{file?.name || "尚未选择文件"}</span>
          </label>
          <div className="button-row">
            <Button disabled={!file || state === "uploading" || state === "parsing"} onClick={() => void uploadAndParse()} state={state === "uploading" || state === "parsing" ? "loading" : "default"}>上传并解析</Button>
            {file ? <Button onClick={() => setFile(null)} variant="quiet">移除文件</Button> : null}
          </div>
        </section>
      ) : null}

      {state === "loading" ? <section className="panel" role="status">正在读取解析结果…</section> : null}
      {resource ? (
        <div className="split-layout">
          <section className="panel panel--wide">
            <header><h2>解析字段</h2><StatusTag tone={draftFacts.length > 0 ? "pending" : "info"}>{draftFacts.length} 条待确认</StatusTag></header>
            {draftFacts.length === 0 ? <p>没有识别到可确认字段。请粘贴文本，或删除后更换文件。</p> : null}
            <div className="settings-list">
              {draftFacts.map((fact, index) => (
                <article className="audit-card" key={fact.id}>
                  <Field label={`字段 ${index + 1} 类型`} name={`kind-${index}`} onChange={(event: ChangeEvent<HTMLInputElement>) => updateFact(index, { kind: event.currentTarget.value })} value={fact.kind} />
                  <Field label={`字段 ${index + 1} 内容`} multiline name={`value-${index}`} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => updateFact(index, { value: event.currentTarget.value })} value={fact.value} />
                  <Button onClick={() => setDraftFacts((items) => items.filter((_, itemIndex) => itemIndex !== index))} variant="quiet">不采用这条</Button>
                </article>
              ))}
            </div>
            <Field label="基础简历名称" name="resume-title" onChange={(event: ChangeEvent<HTMLInputElement>) => setResumeTitle(event.currentTarget.value)} value={resumeTitle} />
          </section>
          <aside className="panel">
            <h2>解析失败时</h2>
            <Field label="粘贴简历文本" multiline name="fallback-text" onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setFallbackText(event.currentTarget.value)} value={fallbackText} />
            <Button disabled={!fallbackText.trim()} onClick={usePastedText} variant="secondary">使用粘贴文本</Button>
            <Button onClick={() => void deleteSource()} state={state === "error" ? "error" : "default"} variant="quiet">删除源文件</Button>
          </aside>
        </div>
      ) : null}
      {resource && draftFacts.length > 0 ? <div className="sticky-action"><Button disabled={state === "saving"} onClick={() => void confirmImport()} state={state === "saving" ? "loading" : state === "error" ? "error" : "default"}>确认并创建基础简历</Button></div> : null}
      {state === "error" ? <p className="auth-error" role="alert">{message}</p> : null}
    </Page>
  );
}
