"use client";

import type { components } from "@resume/shared/schema";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type ChangeEvent, useEffect, useMemo, useRef, useState } from "react";

import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { Field } from "../../../../components/ui/Field";
import { StatusTag } from "../../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../../features/api/client";
import {
  mergeResumeSnapshots,
  resumeSnapshotConflicts,
  type SnapshotMergeChoice,
} from "../../../../features/editor/snapshot-merge";
import { resumeSectionTypeLabel } from "../../../../features/presentation/business-labels";
import { useAutoSave } from "../../../../features/editor/use-auto-save";

type Resume = components["schemas"]["ResumeResponse"];
type ResumeSnapshot = components["schemas"]["ResumeSnapshot"];
type ResumeVersion = components["schemas"]["ResumeVersionResponse"];
type QualityIssue = components["schemas"]["QualityIssueResponse"];
type Fact = components["schemas"]["FactResponse"];
type SessionUser = components["schemas"]["MeResponse"];

const statusLabels = {
  conflict: "版本冲突",
  error: "保存失败",
  offline: "未同步",
  saved: "已保存",
  saving: "保存中",
};

function isResumeSnapshot(value: Record<string, unknown>): value is ResumeSnapshot {
  return value.schema_version === "1"
    && typeof value.title === "string"
    && Array.isArray(value.sections);
}

function cloneSnapshot(snapshot: ResumeSnapshot): ResumeSnapshot {
  return {
    ...snapshot,
    sections: snapshot.sections.map((section) => ({
      ...section,
      items: section.items.map((item) => ({ ...item, fact_refs: [...item.fact_refs] })),
    })),
  };
}

function claimEvidence(snapshot: ResumeSnapshot) {
  return snapshot.sections.flatMap((section) => section.items.flatMap((item) => (
    item.text
      ? [{
          bullet_id: item.id,
          end: item.text.length,
          fact_refs: item.fact_refs,
          start: 0,
        }]
      : []
  )));
}

export default function EditorPage() {
  const { id } = useParams<{ id: string }>();
  const [resume, setResume] = useState<Resume | null>(null);
  const [version, setVersion] = useState<ResumeVersion | null>(null);
  const [issues, setIssues] = useState<QualityIssue[]>([]);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [ownerId, setOwnerId] = useState("");
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">("loading");

  useEffect(() => {
    let active = true;
    const api = createWebApiClient();
    Promise.all([
      api.get<Resume>(`/v1/resumes/${id}`),
      api.get<components["schemas"]["ResumeVersionsResponse"]>(`/v1/resumes/${id}/versions?limit=1`),
      api.post<{}, components["schemas"]["QualityCheckResponse"]>(`/v1/resumes/${id}/quality-checks`, {}, crypto.randomUUID()),
      api.get<components["schemas"]["FactListResponse"]>("/v1/facts?limit=100"),
      api.get<SessionUser>("/v1/me"),
    ]).then(
      ([loadedResume, versions, quality, loadedFacts, me]) => {
        if (!active) return;
        setResume(loadedResume);
        setIssues(quality.issues);
        setFacts(loadedFacts.items.filter((fact) => (
          fact.status === "confirmed" && fact.source_ids.length > 0
        )));
        setOwnerId(me.user_id);
        if (versions.items.length === 0) {
          setState("empty");
          return;
        }
        setVersion(versions.items[0]);
        setState("ready");
      },
      () => {
        if (active) setState("error");
      },
    );
    return () => {
      active = false;
    };
  }, [id]);

  if (state === "loading") {
    return <Page eyebrow="结构化编辑器" title="读取简历"><section className="panel" role="status">正在读取最新版本…</section></Page>;
  }
  if (state === "error" || !resume) {
    return <Page eyebrow="结构化编辑器" title="无法打开简历"><section className="panel" role="alert"><p>简历读取失败，请从“我的简历”重新进入。</p><Link href="/resumes">返回我的简历</Link></section></Page>;
  }
  if (state === "empty" || !version || !isResumeSnapshot(version.snapshot)) {
    return <Page eyebrow="结构化编辑器" title={resume.title}><section className="panel"><p>这份简历还没有可编辑版本。请先完成经历梳理或导入确认。</p><Link href="/create">开始梳理经历</Link></section></Page>;
  }
  return (
    <LoadedEditor
      facts={facts}
      initialSnapshot={version.snapshot}
      initialVersionId={version.id}
      issues={issues}
      ownerId={ownerId}
      resume={resume}
    />
  );
}

function LoadedEditor({
  facts,
  initialSnapshot,
  initialVersionId,
  issues,
  ownerId,
  resume,
}: {
  facts: Fact[];
  initialSnapshot: ResumeSnapshot;
  initialVersionId: string;
  issues: QualityIssue[];
  ownerId: string;
  resume: Resume;
}) {
  const [snapshot, setSnapshot] = useState(() => cloneSnapshot(initialSnapshot));
  const [dirty, setDirty] = useState(false);
  const [selectedFacts, setSelectedFacts] = useState<Record<string, string>>({});
  const [history, setHistory] = useState<ResumeSnapshot[]>([]);
  const [manualMessage, setManualMessage] = useState("");
  const [cloudSnapshot, setCloudSnapshot] = useState<ResumeSnapshot | null>(null);
  const [mergeChoices, setMergeChoices] = useState<Record<string, SnapshotMergeChoice>>({});
  const [conflictBusy, setConflictBusy] = useState(false);
  const saveOperation = useRef({ content: "", key: "" });
  const conflictOperation = useRef({ content: "", key: "" });
  const draftBaseVersion = useRef(resume.version);
  const content = useMemo(() => JSON.stringify(snapshot), [snapshot]);
  const unsupported = snapshot.sections.flatMap((section) => section.items).filter(
    (item) => item.text.trim() && item.fact_refs.length === 0,
  );
  const storageKey = `resume-draft:${ownerId}:${resume.id}`;

  useEffect(() => {
    if (typeof localStorage === "undefined") return;
    const raw = localStorage.getItem(storageKey);
    if (!raw) return;
    try {
      const draft = JSON.parse(raw) as {
        base_version?: number;
        expires_at?: number;
        owner_id?: string;
        resource_id?: string;
        snapshot?: Record<string, unknown>;
      };
      if (
        draft.owner_id === ownerId
        && draft.resource_id === resume.id
        && draft.base_version === resume.version
        && typeof draft.expires_at === "number"
        && draft.expires_at > Date.now()
        && draft.snapshot
        && isResumeSnapshot(draft.snapshot)
      ) {
        setSnapshot(cloneSnapshot(draft.snapshot));
        setDirty(true);
        setManualMessage("已恢复这台设备上尚未同步的草稿。");
      } else {
        localStorage.removeItem(storageKey);
      }
    } catch {
      localStorage.removeItem(storageKey);
    }
  }, [ownerId, resume.id, resume.version, storageKey]);

  useEffect(() => {
    if (!dirty || typeof localStorage === "undefined") return;
    localStorage.setItem(storageKey, JSON.stringify({
      base_version: draftBaseVersion.current,
      expires_at: Date.now() + 24 * 60 * 60 * 1000,
      owner_id: ownerId,
      resource_id: resume.id,
      snapshot,
    }));
  }, [dirty, ownerId, resume.id, resume.version, snapshot, storageKey]);

  const save = useAutoSave({
    baseVersion: resume.version,
    content,
    dirty: dirty && unsupported.length === 0,
    save: async ({ baseVersion, content: currentContent }) => {
      const body: components["schemas"]["VersionCreate"] = {
        base_version: baseVersion,
        claim_evidence: claimEvidence(snapshot),
        snapshot,
      };
      if (saveOperation.current.content !== currentContent) {
        saveOperation.current = { content: currentContent, key: crypto.randomUUID() };
      }
      const saved = await createWebApiClient().post<typeof body, ResumeVersion>(
        `/v1/resumes/${resume.id}/versions`,
        body,
        saveOperation.current.key,
      );
      saveOperation.current = { content: "", key: "" };
      const nextVersion = baseVersion + 1;
      draftBaseVersion.current = nextVersion;
      if (typeof localStorage !== "undefined") localStorage.removeItem(storageKey);
      setDirty(false);
      return { id: saved.id, version: nextVersion };
    },
  });

  function mutate(change: (next: ResumeSnapshot) => void) {
    setHistory((items) => [...items, cloneSnapshot(snapshot)].slice(-20));
    setSnapshot((current) => {
      const next = cloneSnapshot(current);
      change(next);
      return next;
    });
    setDirty(true);
    setManualMessage("");
  }

  function editBullet(sectionIndex: number, itemIndex: number, value: string) {
    mutate((next) => {
      const item = next.sections[sectionIndex]?.items[itemIndex];
      if (!item) return;
      item.text = value;
      item.fact_refs = [];
    });
  }

  function moveSection(sectionIndex: number, direction: -1 | 1) {
    mutate((next) => {
      const target = sectionIndex + direction;
      if (target < 0 || target >= next.sections.length) return;
      [next.sections[sectionIndex], next.sections[target]] = [
        next.sections[target],
        next.sections[sectionIndex],
      ];
    });
  }

  function moveBullet(sectionIndex: number, itemIndex: number, direction: -1 | 1) {
    mutate((next) => {
      const items = next.sections[sectionIndex]?.items;
      const target = itemIndex + direction;
      if (!items || target < 0 || target >= items.length) return;
      [items[itemIndex], items[target]] = [items[target], items[itemIndex]];
    });
  }

  function splitBullet(sectionIndex: number, itemIndex: number) {
    const text = snapshot.sections[sectionIndex]?.items[itemIndex]?.text ?? "";
    const candidates = ["\n", "；", "。"].flatMap((separator) => {
      const index = text.indexOf(separator);
      return index > 0 && index < text.length - 1 ? [index + separator.length] : [];
    });
    const splitAt = candidates.sort((left, right) => (
      Math.abs(left - text.length / 2) - Math.abs(right - text.length / 2)
    ))[0];
    if (!splitAt) {
      setManualMessage("要点中需要有换行、分号或句号，才能安全拆分。");
      return;
    }
    mutate((next) => {
      const items = next.sections[sectionIndex].items;
      const current = items[itemIndex];
      items.splice(
        itemIndex,
        1,
        { ...current, fact_refs: [], text: text.slice(0, splitAt).trim() },
        { fact_refs: [], id: crypto.randomUUID(), text: text.slice(splitAt).trim() },
      );
    });
  }

  function mergeWithPrevious(sectionIndex: number, itemIndex: number) {
    if (itemIndex === 0) return;
    mutate((next) => {
      const items = next.sections[sectionIndex].items;
      const previous = items[itemIndex - 1];
      const current = items[itemIndex];
      previous.text = `${previous.text.replace(/[；。]\s*$/, "")}；${current.text}`;
      previous.fact_refs = [...new Set([...previous.fact_refs, ...current.fact_refs])];
      items.splice(itemIndex, 1);
    });
  }

  function linkFact(sectionIndex: number, itemIndex: number) {
    const item = snapshot.sections[sectionIndex]?.items[itemIndex];
    const factId = item ? selectedFacts[item.id] : "";
    if (!item?.text.trim() || !factId) return;
    mutate((next) => {
      next.sections[sectionIndex].items[itemIndex].fact_refs = [factId];
    });
    setManualMessage("已关联既有确认事实；服务端会再次检查声明与证据是否匹配。");
  }

  function undo() {
    const previous = history.at(-1);
    if (!previous) return;
    setSnapshot(cloneSnapshot(previous));
    setHistory((items) => items.slice(0, -1));
    setDirty(true);
  }

  async function saveConflictSnapshot(candidate: ResumeSnapshot) {
    setConflictBusy(true);
    setManualMessage("");
    try {
      const api = createWebApiClient();
      const latestResume = await api.get<Resume>(`/v1/resumes/${resume.id}`);
      const body: components["schemas"]["VersionCreate"] = {
        base_version: latestResume.version,
        claim_evidence: claimEvidence(candidate),
        snapshot: candidate,
      };
      const fingerprint = JSON.stringify(body);
      if (conflictOperation.current.content !== fingerprint) {
        conflictOperation.current = { content: fingerprint, key: crypto.randomUUID() };
      }
      await api.post<typeof body, ResumeVersion>(
        `/v1/resumes/${resume.id}/versions`,
        body,
        conflictOperation.current.key,
      );
      if (typeof localStorage !== "undefined") localStorage.removeItem(storageKey);
      window.location.reload();
    } catch {
      setManualMessage("冲突处理未保存，本机草稿仍在。请检查事实关联后重试。");
      setConflictBusy(false);
    }
  }

  async function startFieldMerge() {
    setConflictBusy(true);
    setManualMessage("");
    try {
      const versions = await createWebApiClient().get<components["schemas"]["ResumeVersionsResponse"]>(
        `/v1/resumes/${resume.id}/versions?limit=1`,
      );
      const latest = versions.items[0]?.snapshot;
      if (!latest || !isResumeSnapshot(latest)) throw new Error("missing latest snapshot");
      setCloudSnapshot(cloneSnapshot(latest));
      setMergeChoices({});
    } catch {
      setManualMessage("无法读取云端版本，本机草稿仍在。");
    } finally {
      setConflictBusy(false);
    }
  }

  const mergeConflicts = cloudSnapshot
    ? resumeSnapshotConflicts(snapshot, cloudSnapshot)
    : [];

  function buildMergedSnapshot(): ResumeSnapshot {
    if (!cloudSnapshot) return cloneSnapshot(snapshot);
    return mergeResumeSnapshots(snapshot, cloudSnapshot, mergeChoices);
  }

  const effectiveVersionId = save.resourceId ?? initialVersionId;
  const status = unsupported.length > 0
    ? { label: `${unsupported.length} 条待确认来源`, tone: "pending" as const }
    : { label: statusLabels[save.state], tone: save.state === "saved" ? "success" as const : save.state === "error" || save.state === "conflict" ? "error" as const : "pending" as const };

  return (
    <Page eyebrow="结构化编辑器" status={status} title={`${snapshot.title} · ${resume.kind === "base" ? "基础版" : "岗位版"}`}>
      <div className="editor-grid">
        <aside className="editor-rail">
          <h2>模块</h2>
          <nav>{snapshot.sections.map((section) => <a href={`#${section.id}`} key={section.id}>{section.title}</a>)}</nav>
          <hr />
          <p>{issues.length > 0 ? `${issues.length} 项质量问题` : "质量检查通过"}</p>
          <Link href={`/resumes/${resume.id}/versions`}>历史版本</Link>
        </aside>
        <div className="resume-list">
          {snapshot.sections.map((section, sectionIndex) => (
            <section className="editor-main" id={section.id} key={section.id}>
              <header>
                <div><p className="eyebrow">{resumeSectionTypeLabel(section.type)}</p><h2>{section.title}</h2></div>
                <div className="button-row">
                  <Button disabled={sectionIndex === 0} onClick={() => moveSection(sectionIndex, -1)} variant="quiet">模块上移</Button>
                  <Button disabled={sectionIndex === snapshot.sections.length - 1} onClick={() => moveSection(sectionIndex, 1)} variant="quiet">模块下移</Button>
                  <Button onClick={() => mutate((next) => { next.sections.splice(sectionIndex, 1); })} variant="quiet">删除模块</Button>
                </div>
              </header>
              <Field
                label="模块标题"
                name={`${section.id}-title`}
                onChange={(event: ChangeEvent<HTMLInputElement>) => mutate((next) => {
                  next.sections[sectionIndex].title = event.currentTarget.value;
                })}
                value={section.title}
              />
              {section.items.map((item, itemIndex) => (
                <div className="audit-card" key={item.id}>
                  <Field
                    label={`要点 ${itemIndex + 1}`}
                    multiline
                    name={`${section.id}-${item.id}`}
                    onChange={(event: ChangeEvent<HTMLTextAreaElement>) => editBullet(sectionIndex, itemIndex, event.currentTarget.value)}
                    value={item.text}
                  />
                  <div className="button-row">
                    <StatusTag tone={item.fact_refs.length > 0 ? "success" : "pending"}>
                      {item.fact_refs.length > 0 ? `${item.fact_refs.length} 个事实来源` : "修改后需确认来源"}
                    </StatusTag>
                    {item.text.trim() && item.fact_refs.length === 0 ? (
                      <>
                        <label className="field fact-picker">
                          <span className="field__label">关联已有确认事实</span>
                          <select
                            className="field__control"
                            onChange={(event) => setSelectedFacts((current) => ({
                              ...current,
                              [item.id]: event.currentTarget.value,
                            }))}
                            value={selectedFacts[item.id] ?? ""}
                          >
                            <option value="">选择有来源的事实</option>
                            {facts.map((fact) => (
                              <option key={fact.id} value={fact.id}>{fact.value}</option>
                            ))}
                          </select>
                        </label>
                        <Button
                          disabled={!selectedFacts[item.id]}
                          onClick={() => linkFact(sectionIndex, itemIndex)}
                          variant="secondary"
                        >
                          关联事实
                        </Button>
                      </>
                    ) : null}
                    <Button disabled={itemIndex === 0} onClick={() => moveBullet(sectionIndex, itemIndex, -1)} variant="quiet">上移</Button>
                    <Button disabled={itemIndex === section.items.length - 1} onClick={() => moveBullet(sectionIndex, itemIndex, 1)} variant="quiet">下移</Button>
                    <Button onClick={() => splitBullet(sectionIndex, itemIndex)} variant="quiet">拆分</Button>
                    <Button disabled={itemIndex === 0} onClick={() => mergeWithPrevious(sectionIndex, itemIndex)} variant="quiet">并入上一条</Button>
                    <Button onClick={() => mutate((next) => { next.sections[sectionIndex].items.splice(itemIndex, 1); })} variant="quiet">删除要点</Button>
                  </div>
                </div>
              ))}
              <Button onClick={() => mutate((next) => { next.sections[sectionIndex].items.push({ fact_refs: [], id: crypto.randomUUID(), text: "" }); })} variant="quiet">新增要点</Button>
            </section>
          ))}
          <Button
            onClick={() => mutate((next) => {
              next.sections.push({
                id: crypto.randomUUID(),
                items: [],
                title: "新模块",
                type: "experience",
              });
            })}
            variant="secondary"
          >
            新增模块
          </Button>
        </div>
        <aside className="editor-context">
          <h2>质量与事实</h2>
          <StatusTag tone={issues.length > 0 || unsupported.length > 0 ? "pending" : "success"}>
            {issues.length + unsupported.length} 项需处理
          </StatusTag>
          {issues.map((issue) => <article className="audit-card" key={`${issue.code}:${issue.path}`}><strong>{issue.code}</strong><p>{issue.message}</p><a href={`#${issue.path.split(".")[0]}`}>定位问题</a></article>)}
          {unsupported.length > 0 ? <article className="audit-card"><strong>修改尚未写入正式版本</strong><p>请关联事实库中已有来源且已确认的事实。当前文字不能给自己充当证据。</p><Link href="/facts">打开事实库</Link></article> : null}
          <Button disabled={history.length === 0} onClick={undo} variant="secondary">撤销（{history.length}/20）</Button>
          <Link className="button button--primary" href={`/jobs/new?version=${effectiveVersionId}`}>继续岗位匹配</Link>
          <Link href={`/exports/new?version=${effectiveVersionId}`}>独立预览</Link>
        </aside>
      </div>
      {manualMessage ? <p className="panel" role="status">{manualMessage}</p> : null}
      {save.state === "conflict" ? (
        <section className="conflict-panel" role="alert">
          <h2>检测到云端新版本</h2>
          <p>自动保存已停止。本机草稿仍保留。请选择覆盖、放弃或逐字段合并，不会静默覆盖。</p>
          <div className="button-row">
            <Button disabled={conflictBusy} onClick={() => void saveConflictSnapshot(snapshot)}>保留本机并创建新版本</Button>
            <Button disabled={conflictBusy} onClick={() => window.location.reload()} variant="secondary">使用云端版本</Button>
            <Button disabled={conflictBusy} onClick={() => void startFieldMerge()} variant="quiet">逐字段合并</Button>
          </div>
          {cloudSnapshot ? (
            <div className="resume-list">
              {mergeConflicts.length === 0 ? <p>没有同一字段的文字冲突；将同时保留双方新增内容。</p> : null}
              {mergeConflicts.map((conflict, index) => (
                <fieldset className="audit-card" key={conflict.id}>
                  <legend>冲突字段 {index + 1}：{conflict.label}</legend>
                  <label className="check-row">
                    <input checked={mergeChoices[conflict.id] === "local"} name={`merge-${conflict.id}`} onChange={() => setMergeChoices((current) => ({ ...current, [conflict.id]: "local" }))} type="radio" />
                    <span><strong>本机</strong><br />{conflict.local}</span>
                  </label>
                  <label className="check-row">
                    <input checked={mergeChoices[conflict.id] === "cloud"} name={`merge-${conflict.id}`} onChange={() => setMergeChoices((current) => ({ ...current, [conflict.id]: "cloud" }))} type="radio" />
                    <span><strong>云端</strong><br />{conflict.cloud}</span>
                  </label>
                </fieldset>
              ))}
              <Button
                disabled={conflictBusy || mergeConflicts.some((item) => !mergeChoices[item.id])}
                onClick={() => void saveConflictSnapshot(buildMergedSnapshot())}
                variant="secondary"
              >
                保存合并结果
              </Button>
            </div>
          ) : null}
        </section>
      ) : null}
    </Page>
  );
}
