"use client";

import type { components } from "@resume/shared/schema";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { Field } from "../../../components/ui/Field";
import { StatusTag } from "../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../features/api/client";
import { useApiResource } from "../../../features/api/useApiResource";
import { EvidenceList } from "../../../features/facts/EvidenceList";

interface DecisionResponse {
  decision_id: string;
  status: string;
  suggestion_id: string;
  version_id: string;
}

export default function SuggestionsPage() {
  const { analysisId } = useParams<{ analysisId: string }>();
  const suggestions = useApiResource<components["schemas"]["SuggestionListResponse"]>(
    `/v1/match-analyses/${analysisId}/suggestions`,
  );
  const [selectedId, setSelectedId] = useState("");
  const [decision, setDecision] = useState("待处理");
  const [versionId, setVersionId] = useState("");
  const [editedText, setEditedText] = useState("");
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [requestedMissing, setRequestedMissing] = useState(false);
  const decisionOperation = useRef({ fingerprint: "", key: "" });

  const selected = suggestions.status === "ready" && !requestedMissing
    ? suggestions.data.items.find((item) => item.id === selectedId) ?? suggestions.data.items[0]
    : null;

  useEffect(() => {
    if (suggestions.status !== "ready" || suggestions.data.items.length === 0) return;
    const requested = new URLSearchParams(window.location.search).get("suggestion");
    const requestedItem = requested
      ? suggestions.data.items.find((item) => item.id === requested)
      : null;
    if (requested && !requestedItem) {
      setRequestedMissing(true);
      setError("指定的建议不存在或不属于当前分析。");
      return;
    }
    setRequestedMissing(false);
    const next = requestedItem ?? suggestions.data.items[0];
    setSelectedId(next.id);
    setEditedText(next.suggested_text);
    setDecision(next.status === "pending" ? "待处理" : next.status);
  }, [suggestions.data, suggestions.status]);

  async function decide(action: "accept" | "edit" | "ignore" | "revert", label: string) {
    if (!selected || running) return;
    if (action === "edit" && !editing) {
      setEditing(true);
      return;
    }
    setRunning(true);
    setError("");
    try {
      const body = action === "edit" ? { text: editedText } : {};
      const fingerprint = JSON.stringify({ action, body, suggestion_id: selected.id });
      if (decisionOperation.current.fingerprint !== fingerprint) {
        decisionOperation.current = { fingerprint, key: crypto.randomUUID() };
      }
      const result = await createWebApiClient().post<typeof body, DecisionResponse>(
        `/v1/suggestions/${selected.id}/${action}`,
        body,
        decisionOperation.current.key,
      );
      decisionOperation.current = { fingerprint: "", key: "" };
      setDecision(label);
      setVersionId(result.version_id);
      setEditing(false);
    } catch {
      setError("建议操作失败。原简历版本没有被覆盖，请重试。");
    } finally {
      setRunning(false);
    }
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable) return;
      const action = {
        a: ["accept", "已接受"],
        e: ["edit", "已编辑"],
        i: ["ignore", "已忽略"],
        z: ["revert", "已撤销"],
      } as const;
      const choice = action[event.key.toLowerCase() as keyof typeof action];
      if (choice) void decide(choice[0], choice[1]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <Page
      eyebrow="逐条建议 · A 接受 · E 编辑 · I 忽略 · Z 撤销"
      status={{ label: running ? "正在保存" : decision, tone: error ? "error" : decision === "待处理" ? "pending" : "success" }}
      title="确认每一处修改"
    >
      {suggestions.status === "loading" ? <section className="panel" role="status">正在读取建议…</section> : null}
      {suggestions.status === "error" ? <section className="panel" role="alert"><p>{suggestions.error}</p><Button onClick={suggestions.reload} variant="secondary">重试</Button></section> : null}
      {suggestions.status === "ready" && suggestions.data.items.length === 0 ? <section className="panel"><p>这次匹配没有生成可处理建议。</p><Link href="/resumes">返回我的简历</Link></section> : null}
      {requestedMissing ? <section className="panel" role="alert"><p>{error}</p><Link href={`/suggestions/${analysisId}`}>查看此分析的建议</Link></section> : null}
      {selected ? (
        <div className="suggestion-layout">
          <section className="panel panel--wide">
            <p className="eyebrow">JD 原要求</p>
            <h2>{selected.requirement_text ?? "未关联具体岗位要求"}</h2>
            <dl className="comparison">
              <div><dt>当前原文</dt><dd>{selected.original_text}</dd></div>
              <div>
                <dt>建议文本</dt>
                <dd>
                  {editing ? (
                    <Field
                      label="编辑后的文本"
                      multiline
                      name="suggestion-edit"
                      onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setEditedText(event.currentTarget.value)}
                      value={editedText}
                    />
                  ) : selected.suggested_text}
                </dd>
              </div>
            </dl>
            <p><strong>修改理由：</strong>{selected.reason}</p>
            <StatusTag tone={selected.risk_flags.length > 0 ? "pending" : "success"}>
              {selected.risk_flags.length > 0 ? `风险：${selected.risk_flags.join("、")}` : "没有额外风险标记"}
            </StatusTag>
            <div className="button-row">
              <Button disabled={running} onClick={() => void decide("accept", "已接受")} state={running ? "loading" : "default"}>接受</Button>
              <Button disabled={running} onClick={() => void decide("edit", "已编辑")} variant="secondary">{editing ? "保存编辑" : "编辑"}</Button>
              <Button disabled={running} onClick={() => void decide("ignore", "已忽略")} variant="quiet">忽略</Button>
              <Button disabled={running} onClick={() => void decide("revert", "已撤销")} variant="quiet">撤销</Button>
            </div>
            {error ? <p className="auth-error" role="alert">{error}</p> : null}
            {versionId ? <Link className="button button--primary" href={`/exports/new?version=${versionId}`}>进入导出</Link> : null}
          </section>
          <aside className="panel"><h2>事实引用</h2>
            <EvidenceList factIds={selected.fact_refs} />
            <p className="resource-id">目标路径：{selected.target_path}</p>
          </aside>
        </div>
      ) : null}
    </Page>
  );
}
