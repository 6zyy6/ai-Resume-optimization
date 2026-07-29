"use client";

import type { components } from "@resume/shared/schema";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { StatusTag } from "../../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../../features/api/client";

export default function VersionsPage() {
  const { id } = useParams<{ id: string }>();
  const [resume, setResume] = useState<components["schemas"]["ResumeResponse"] | null>(null);
  const [versions, setVersions] = useState<components["schemas"]["ResumeVersionResponse"][]>([]);
  const [selected, setSelected] = useState<components["schemas"]["ResumeVersionResponse"] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    const api = createWebApiClient();
    Promise.all([
      api.get<components["schemas"]["ResumeResponse"]>(`/v1/resumes/${id}`),
      api.get<components["schemas"]["ResumeVersionsResponse"]>(`/v1/resumes/${id}/versions?limit=20`),
    ]).then(
      ([loadedResume, loadedVersions]) => {
        if (!active) return;
        setResume(loadedResume);
        setVersions(loadedVersions.items);
        setState("ready");
      },
      () => {
        if (active) {
          setMessage("版本历史读取失败，请稍后重试。");
          setState("error");
        }
      },
    );
    return () => {
      active = false;
    };
  }, [id]);

  async function restore(versionId: string) {
    if (!resume || state === "saving") return;
    setState("saving");
    setMessage("正在创建恢复版本…");
    try {
      const body: components["schemas"]["RestoreRequest"] = { base_version: resume.version };
      const restored = await createWebApiClient().post<typeof body, components["schemas"]["ResumeVersionResponse"]>(
        `/v1/resumes/${id}/versions/${versionId}/restore`,
        body,
        crypto.randomUUID(),
      );
      setVersions((items) => [restored, ...items]);
      setResume({ ...resume, version: resume.version + 1 });
      setMessage(`已创建恢复版本 ${restored.id}，历史快照没有被覆盖。`);
      setState("ready");
    } catch {
      setMessage("恢复失败，可能有其他设备创建了新版本。请刷新后比较。");
      setState("error");
    }
  }

  return (
    <Page
      eyebrow="不可变历史"
      status={message ? { label: message, tone: state === "error" ? "error" : state === "saving" ? "pending" : "success" } : undefined}
      title={resume ? `${resume.title} · 版本记录` : "版本记录"}
    >
      {state === "loading" ? <section className="panel" role="status">正在读取版本历史…</section> : null}
      {state === "error" && versions.length === 0 ? <section className="panel" role="alert">{message}</section> : null}
      {state !== "loading" && versions.length === 0 ? <section className="panel"><p>这份简历还没有版本。</p></section> : null}
      {versions.length > 0 ? (
        <ol className="timeline">
          {versions.map((version, index) => (
            <li key={version.id}>
              <StatusTag tone={index === 0 ? "success" : "info"}>{index === 0 ? "当前版本" : "历史版本"}</StatusTag>
              <div>
                <h2>{version.operation} · {version.id}</h2>
                <p>{new Date(version.created_at).toLocaleString("zh-CN")} · hash {version.snapshot_hash.slice(0, 12)}{version.parent_version_id ? ` · 父版本 ${version.parent_version_id}` : ""}</p>
              </div>
              <div className="button-row">
                <Button onClick={() => setSelected(version)} variant="secondary">查看</Button>
                {index > 0 ? <Button disabled={state === "saving"} onClick={() => void restore(version.id)} state={state === "saving" ? "loading" : "default"} variant="quiet">恢复为新版本</Button> : null}
              </div>
            </li>
          ))}
        </ol>
      ) : null}
      {selected ? (
        <section className="panel">
          <header><h2>只读快照 · {selected.id}</h2><Button onClick={() => setSelected(null)} variant="quiet">关闭</Button></header>
          <pre className="snapshot-view">{JSON.stringify(selected.snapshot, null, 2)}</pre>
        </section>
      ) : null}
    </Page>
  );
}
