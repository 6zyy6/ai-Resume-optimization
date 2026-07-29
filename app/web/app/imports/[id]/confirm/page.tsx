"use client";

import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { claimEvidenceForText, waitForTask } from "@resume/shared/workflows";
import { useRouter } from "next/navigation";

import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { Field } from "../../../../components/ui/Field";
import { StatusTag } from "../../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../../features/api/client";

const modules = [
  ["基本信息", "confirmed"],
  ["教育经历", "confirmed"],
  ["实习经历", "uncertain"],
  ["项目经历", "missing"],
  ["校园与活动", "missing"],
  ["技能", "uncertain"],
  ["荣誉", "missing"],
] as const;

export default function ImportConfirmPage() {
  const [fallback, setFallback] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const router = useRouter();
  async function confirmImport() {
    if (!file) return;
    const api = createWebApiClient();
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    const tokenBody: components["schemas"]["UploadTokenRequest"] = {
      display_name: file.name,
      mime: file.type || "text/plain",
      purpose: "resume_import",
      sha256,
      size: file.size,
    };
    const token = await api.post<typeof tokenBody, components["schemas"]["UploadTokenResponse"]>(
      "/v1/files/upload-tokens", tokenBody, crypto.randomUUID(),
    );
    const uploaded = await fetch(token.upload_url, { body: file, method: "PUT" });
    if (!uploaded.ok) throw new Error("文件上传失败");
    await api.post(`/v1/files/${token.file_id}/confirm-upload`, {}, crypto.randomUUID());
    const importBody: components["schemas"]["ImportCreate"] = { file_id: token.file_id };
    const imported = await api.post<typeof importBody, components["schemas"]["ImportResponse"]>(
      "/v1/imports", importBody, crypto.randomUUID(),
    );
    if (!imported.task_id) throw new Error("导入任务未创建");
    await waitForTask(
      () => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${imported.task_id}`),
      imported.task_id,
    );
    await api.get<components["schemas"]["ImportResponse"]>(`/v1/imports/${imported.id}`);
    const confirmBody: components["schemas"]["ImportConfirm"] = {
      facts: modules.filter(([, state]) => state !== "missing").map(([kind]) => ({ kind, value: `${kind}：待用户确认` })),
    };
    const confirmed = await api.post<typeof confirmBody, components["schemas"]["ImportResponse"]>(`/v1/imports/${imported.id}/confirm`, confirmBody, crypto.randomUUID());
    const resumeBody: components["schemas"]["ResumeCreate"] = { kind: "base", title: file.name };
    const resume = await api.post<typeof resumeBody, components["schemas"]["ResumeResponse"]>("/v1/resumes", resumeBody, crypto.randomUUID());
    const snapshot: components["schemas"]["ResumeSnapshot"] = {
      schema_version: "1",
      sections: confirmBody.facts?.map((fact, index) => ({
        id: `import-${index}`,
        items: [{ fact_refs: [], id: `import-bullet-${index}`, text: fact.value }],
        title: fact.kind,
        type: fact.kind,
      })) ?? [],
      target: null,
      title: file.name,
    };
    const claim_evidence = snapshot.sections.flatMap((section, index) => {
      const item = section.items[0];
      const factId = confirmed.fact_ids?.[index];
      return factId ? claimEvidenceForText(item.id, item.text, factId) : [];
    });
    const versionBody: components["schemas"]["VersionCreate"] = { base_version: resume.version, claim_evidence, snapshot };
    const version = await api.post<typeof versionBody, components["schemas"]["ResumeVersionResponse"]>(
      `/v1/resumes/${resume.id}/versions`, versionBody, crypto.randomUUID(),
    );
    router.push(`/jobs/new?version=${version.id}`);
  }
  return (
    <Page eyebrow="导入确认" title="先确认解析结果，再写入事实库。">
      <aside className="rule-strip">支持 PDF、DOCX、TXT · 最大 10 MiB · PDF 最多 10 页 · 不支持扫描件 OCR</aside>
      <label className="field"><span className="field__label">选择简历文件</span><input accept=".pdf,.docx,.txt" aria-label="选择简历文件" onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)} type="file" /><span className="field__message">{file?.name || "尚未选择文件"}</span></label>
      <div className="split-layout">
        <section className="panel panel--wide">
          <header><h2>识别模块</h2><StatusTag tone="pending">3 项待确认</StatusTag></header>
          <ul className="module-list">
            {modules.map(([name, state]) => (
              <li key={name}><strong>{name}</strong><StatusTag tone={state === "confirmed" ? "success" : state === "uncertain" ? "pending" : "error"}>{state}</StatusTag><Button variant="quiet">编辑</Button></li>
            ))}
          </ul>
        </section>
        <aside className="panel">
          <h2>解析失败也不会丢文件</h2>
          <p>你可以改为粘贴文本，或立即删除上传文件。</p>
          <Button onClick={() => setFallback(!fallback)} variant="secondary">粘贴文本</Button>
          {fallback ? <Field label="简历文本" multiline name="resume-text" /> : null}
          <Button state="error" variant="quiet">删除文件</Button>
        </aside>
      </div>
      <div className="sticky-action"><Button onClick={() => void confirmImport()}>确认并进入岗位信息</Button></div>
    </Page>
  );
}
