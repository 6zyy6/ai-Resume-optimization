"use client";

import type { components } from "@resume/shared/schema";
import Link from "next/link";

import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { StatusTag } from "../../components/ui/StatusTag";
import { useApiResource } from "../../features/api/useApiResource";

export default function HomePage() {
  const resumes = useApiResource<components["schemas"]["ResumeListResponse"]>("/v1/resumes?limit=3");
  const tasks = useApiResource<components["schemas"]["TaskListResponse"]>("/v1/tasks?limit=5");
  const usage = useApiResource<components["schemas"]["UsageResponse"]>("/v1/me/usage");
  const nextHref = resumes.status === "ready" && resumes.data.items.length > 0
    ? `/resumes/${resumes.data.items[0].id}/edit`
    : "/create";
  const nextLabel = resumes.status === "ready" && resumes.data.items.length > 0
    ? `继续编辑 ${resumes.data.items[0].title}`
    : "开始梳理经历";

  return (
    <Page eyebrow="今天的工作台" title="把下一步做完。">
      <section className="action-grid">
        <Link className="action-card action-card--accent" href={nextHref}><span>下一步</span><strong>{nextLabel}</strong><b>继续 →</b></Link>
        <Link className="action-card" href="/imports/new/confirm"><span>已有文件</span><strong>导入已有简历</strong><b>选择文件 →</b></Link>
      </section>
      <section className="dashboard-grid">
        <div className="panel panel--wide"><header><h2>最近简历</h2><Link href="/resumes">查看全部</Link></header>
          {resumes.status === "loading" ? <p role="status">正在加载简历…</p> : null}
          {resumes.status === "error" ? <div role="alert"><p>{resumes.error}</p><Button onClick={resumes.reload} variant="secondary">重试简历列表</Button></div> : null}
          {resumes.status === "ready" && resumes.data.items.length === 0 ? <p>尚未创建简历。先梳理经历，或导入现有文件。</p> : null}
          {resumes.status === "ready" && resumes.data.items.length > 0 ? (
            <ul className="list">
              {resumes.data.items.map((resume) => (
                <li key={resume.id}><Link href={`/resumes/${resume.id}/edit`}>{resume.title}</Link><span>v{resume.version}</span></li>
              ))}
            </ul>
          ) : null}
        </div>
        <div className="panel"><h2>任务</h2>
          {tasks.status === "loading" ? <p role="status">正在加载任务…</p> : null}
          {tasks.status === "error" ? <div role="alert"><p>{tasks.error}</p><Button onClick={tasks.reload} variant="secondary">重试任务</Button></div> : null}
          {tasks.status === "ready" && tasks.data.items.length === 0 ? <p>当前没有进行中或失败任务。</p> : null}
          {tasks.status === "ready" ? tasks.data.items.map((task) => (
            <p key={task.id}><StatusTag tone={task.status === "failed" ? "error" : task.status === "succeeded" ? "success" : "pending"}>{task.type} · {task.status}</StatusTag> {task.progress}%</p>
          )) : null}
          <Link href="/tasks">查看任务</Link>
        </div>
        <div className="panel"><h2>今日 AI 用量</h2>
          {usage.status === "loading" ? <p role="status">正在加载用量…</p> : null}
          {usage.status === "error" ? <div role="alert"><p>{usage.error}</p><Button onClick={usage.reload} variant="secondary">重试用量</Button></div> : null}
          {usage.status === "ready" ? <p className="usage">{usage.data.ai_tasks_used} / {usage.data.ai_tasks_limit}</p> : null}
          <Link href="/settings">账户设置</Link>
        </div>
      </section>
    </Page>
  );
}
