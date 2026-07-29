"use client";

import type { components } from "@resume/shared/schema";
import Link from "next/link";
import { useRef, useState } from "react";

import { EmptyState, Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { StatusTag } from "../../components/ui/StatusTag";
import { apiBrowserUrl, createWebApiClient } from "../../features/api/client";
import { useApiResource } from "../../features/api/useApiResource";

export default function TasksPage() {
  const tasks = useApiResource<components["schemas"]["TaskListResponse"]>("/v1/tasks?limit=50");
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const cancelKeys = useRef<Record<string, string>>({});

  async function cancel(taskId: string) {
    setCancelling(taskId);
    try {
      cancelKeys.current[taskId] ||= crypto.randomUUID();
      await createWebApiClient().post(`/v1/tasks/${taskId}/cancel`, {}, cancelKeys.current[taskId]);
      delete cancelKeys.current[taskId];
      tasks.reload();
    } finally {
      setCancelling(null);
    }
  }

  async function downloadPrivateData(taskId: string) {
    setDownloading(taskId);
    try {
      const artifact = await createWebApiClient().get<components["schemas"]["PrivacyExportResponse"]>(
        `/v1/me/data-exports/${taskId}`,
      );
      const anchor = document.createElement("a");
      anchor.href = apiBrowserUrl(artifact.download_url);
      anchor.download = "account-data.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      setDownloading(null);
    }
  }

  function resultHref(task: components["schemas"]["TaskResponse"]): string | null {
    if (!task.result_ref || task.status !== "succeeded") return null;
    if (task.type === "generate_intake_draft") return `/resumes/${task.result_ref}/edit`;
    if (task.type === "parse_resume_import") return `/imports/${task.result_ref}/confirm`;
    if (task.type === "parse_job") return `/jobs/new?job=${encodeURIComponent(task.result_ref)}`;
    if (task.type === "render_resume_export") return `/exports/${task.result_ref}`;
    return null;
  }

  return (
    <Page eyebrow="异步任务" title="任务中心">
      {tasks.status === "loading" ? <section className="panel" role="status">正在读取任务…</section> : null}
      {tasks.status === "error" ? <section className="panel" role="alert"><p>{tasks.error}</p><Button onClick={tasks.reload} variant="secondary">重试</Button></section> : null}
      {tasks.status === "ready" && tasks.data.items.length === 0 ? <EmptyState action="返回工作台" href="/home" text="当前没有任务。解析、生成、匹配、导出和隐私请求会显示在这里。" /> : null}
      {tasks.status === "ready" && tasks.data.items.length > 0 ? (
        <section className="resume-list">
          {tasks.data.items.map((task) => (
            <article className="resume-row" key={task.id}>
              <div>
                <StatusTag tone={task.status === "failed" ? "error" : task.status === "succeeded" ? "success" : task.status === "cancelled" ? "info" : "pending"}>{task.status} · {task.stage}</StatusTag>
                <h2>{task.type}</h2>
                <p>{task.progress}%{task.error_code ? ` · ${task.error_code}` : ""}</p>
              </div>
              {["queued", "running", "waiting_for_user"].includes(task.status) ? (
                <Button disabled={cancelling === task.id} onClick={() => void cancel(task.id)} state={cancelling === task.id ? "loading" : "default"} variant="quiet">取消任务</Button>
              ) : null}
              {resultHref(task) ? <Link href={resultHref(task) ?? "/tasks"}>打开结果</Link> : null}
              {task.type === "data_export" && task.status === "succeeded" ? (
                <Button
                  disabled={downloading === task.id}
                  onClick={() => void downloadPrivateData(task.id)}
                  state={downloading === task.id ? "loading" : "default"}
                  variant="secondary"
                >
                  下载数据副本
                </Button>
              ) : null}
            </article>
          ))}
        </section>
      ) : null}
    </Page>
  );
}
