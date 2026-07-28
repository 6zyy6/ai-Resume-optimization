"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { type ChangeEvent, useReducer } from "react";
import type { components } from "@resume/shared/schema";

import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { Field } from "../../../../components/ui/Field";
import { StatusTag } from "../../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../../features/api/client";
import { createEditorState, editorReducer } from "../../../../features/editor/editor-reducer";
import { useAutoSave } from "../../../../features/editor/use-auto-save";

const statusLabels = { conflict: "版本冲突", error: "保存失败", offline: "未同步", saved: "已保存", saving: "保存中" };

export default function EditorPage() {
  const { id } = useParams<{ id: string }>();
  const [editor, dispatch] = useReducer(editorReducer, createEditorState("负责课程项目的用户调研，整理访谈结论并完成方案汇报。", 1));
  const projectIndex = editor.snapshot.modules.findIndex((module) => module.id === "project");
  const project = editor.snapshot.modules[projectIndex];
  const save = useAutoSave({
    baseVersion: editor.baseVersion,
    content: editor.content,
    dirty: editor.dirty,
    save: async ({ baseVersion }) => {
      const snapshot: components["schemas"]["ResumeSnapshot"] = {
        schema_version: "1",
        sections: editor.snapshot.modules.map((module) => ({
          id: module.id,
          items: module.bullets.map((text, index) => ({ fact_refs: [], id: `${module.id}-${index}`, text })),
          title: module.title,
          type: module.id,
        })),
        target: null,
        title: "产品运营实习",
      };
      const body: components["schemas"]["VersionCreate"] = { base_version: baseVersion, claim_evidence: [], snapshot };
      const version = await createWebApiClient().post<typeof body, components["schemas"]["ResumeVersionResponse"]>(
        `/v1/resumes/${id}/versions`, body, crypto.randomUUID(),
      );
      return { id: version.id, version: baseVersion + 1 };
    },
  });
  const tone = save.state === "saved" ? "success" : save.state === "conflict" || save.state === "error" ? "error" : "pending";
  return (
    <Page eyebrow="结构化编辑器" status={{ label: statusLabels[save.state], tone }} title="产品运营实习 · 基础版">
      <div className="editor-grid">
        <aside className="editor-rail"><h2>模块</h2><nav>{editor.snapshot.modules.map((module) => <a href={module.id === "project" ? "#project" : "#"} key={module.id}>{module.title}</a>)}</nav><hr /><p>完整性：需要补充时间范围</p><Link href={`/resumes/${id}/versions`}>历史版本</Link></aside>
        <section className="editor-main" id="project">
          <header><div><p className="eyebrow">项目经历</p><h2>课程产品设计</h2></div><Button onClick={() => dispatch({ type: "moveModule", from: projectIndex, to: 0 })} variant="quiet">项目移到顶部</Button></header>
          <Field label="角色" name="role" defaultValue="项目负责人" />
          <Field label="时间" name="date" defaultValue="2025.03—2025.06" />
          {project.bullets.map((bullet, index) => <Field key={index} label={`要点 ${index + 1}`} multiline name={`bullet-${index}`} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => dispatch({ type: "editBullet", moduleIndex: projectIndex, bulletIndex: index, content: event.currentTarget.value })} value={bullet} />)}
          <div className="button-row"><Button onClick={() => dispatch({ type: "undo" })} variant="secondary">撤销（{editor.history.length}/20）</Button><Button onClick={() => dispatch({ type: "splitBullet", moduleIndex: projectIndex, bulletIndex: 0 })} variant="quiet">拆分要点</Button><Button onClick={() => dispatch({ type: "mergeBullet", moduleIndex: projectIndex, bulletIndex: 0 })} variant="quiet">合并要点</Button><Button onClick={() => dispatch({ type: "addBullet", moduleIndex: projectIndex })} variant="quiet">新增要点</Button><Button onClick={() => dispatch({ type: "deleteBullet", moduleIndex: projectIndex, bulletIndex: project.bullets.length - 1 })} variant="quiet">删除末条</Button></div>
        </section>
        <aside className="editor-context"><h2>质量与事实</h2><StatusTag tone="pending">1 项需检查</StatusTag><article className="audit-card"><strong>“负责”缺少具体动作</strong><p>建议补充你实际完成的访谈、整理或交付行为。</p><a href="#project">定位问题</a></article><article className="audit-card"><strong>事实来源</strong><p>课程产品设计 · 用户回答 · 已确认</p></article>{save.resourceId ? <Link className="button button--primary" href={`/jobs/new?version=${save.resourceId}`}>继续岗位匹配</Link> : <Button disabled>保存后继续</Button>}<Link href={`/exports/new?version=${save.resourceId ?? ""}`}>独立预览</Link></aside>
      </div>
      {save.state === "conflict" ? <section className="conflict-panel" role="alert"><h2>检测到云端新版本</h2><p>自动保存已停止。请选择保留本地、使用云端或逐字段合并。</p><div className="button-row"><Button>保留本地</Button><Button variant="secondary">使用云端</Button><Button variant="quiet">逐字段合并</Button></div></section> : null}
    </Page>
  );
}
