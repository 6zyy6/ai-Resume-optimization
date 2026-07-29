"use client";

import type { components } from "@resume/shared/schema";
import Link from "next/link";

import { EmptyState, Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { StatusTag } from "../../components/ui/StatusTag";
import { useApiResource } from "../../features/api/useApiResource";

export default function ResumesPage() {
  const resumes = useApiResource<components["schemas"]["ResumeListResponse"]>("/v1/resumes?limit=20");
  return (
    <Page actions={<Link className="button button--primary" href="/create">新建简历</Link>} eyebrow="我的简历" title="基础版本与岗位版本">
      {resumes.status === "loading" ? <section className="panel" role="status">正在读取你的简历…</section> : null}
      {resumes.status === "error" ? <section className="panel" role="alert"><p>{resumes.error}</p><Button onClick={resumes.reload} variant="secondary">重试</Button></section> : null}
      {resumes.status === "ready" && resumes.data.items.length === 0 ? <EmptyState action="开始梳理经历" href="/create" text="尚未创建简历。基础简历保存通用经历，岗位版本用于定向投递。" /> : null}
      {resumes.status === "ready" && resumes.data.items.length > 0 ? (
        <section className="resume-list">
          {resumes.data.items.map((resume) => (
            <article className="resume-row" key={resume.id}>
              <div>
                <StatusTag tone={resume.kind === "base" ? "success" : "info"}>{resume.kind === "base" ? "基础简历" : "岗位版本"}</StatusTag>
                <h2>{resume.title}</h2>
                <p>当前资源版本：v{resume.version}{resume.base_resume_id ? " · 基础版本保持独立" : ""}</p>
              </div>
              <div className="button-row">
                <Link href={`/resumes/${resume.id}/edit`}>编辑</Link>
                <Link href={`/resumes/${resume.id}/versions`}>历史版本</Link>
              </div>
            </article>
          ))}
        </section>
      ) : null}
    </Page>
  );
}
