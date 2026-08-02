"use client";

import type { components } from "@resume/shared/schema";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Page } from "../../../components/Page";
import { Button } from "../../../components/ui/Button";
import { Field } from "../../../components/ui/Field";
import { StatusTag } from "../../../components/ui/StatusTag";
import { createWebApiClient } from "../../../features/api/client";
import { useApiResource } from "../../../features/api/useApiResource";
import { EvidenceList } from "../../../features/facts/EvidenceList";
import { resumeTargetLabel, riskFlagLabel } from "../../../features/presentation/business-labels";

type Suggestion = components["schemas"]["SuggestionResponse"];
type SuggestionStatus = Suggestion["status"];

const suggestionStatuses = new Set<SuggestionStatus>([
  "pending",
  "accepted",
  "edited",
  "ignored",
  "reverted",
  "blocked",
]);

function statusLabel(status: SuggestionStatus | null) {
  if (status === "accepted") return "已接受";
  if (status === "edited") return "已编辑";
  if (status === "ignored") return "已忽略";
  if (status === "reverted") return "已撤销";
  if (status === "blocked") return "等待补充事实";
  return "待处理";
}

export default function SuggestionsPage() {
  const { analysisId } = useParams<{ analysisId: string }>();
  const router = useRouter();
  const suggestions = useApiResource<components["schemas"]["SuggestionListResponse"]>(
    `/v1/match-analyses/${analysisId}/suggestions`,
  );
  const [selectedId, setSelectedId] = useState("");
  const [decisionStatuses, setDecisionStatuses] = useState<Record<string, SuggestionStatus>>({});
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
  const selectedStatus = selected
    ? decisionStatuses[selected.id] ?? selected.status
    : null;
  const selectedIndex = suggestions.status === "ready" && selected
    ? suggestions.data.items.findIndex((item) => item.id === selected.id)
    : -1;

  function selectSuggestion(index: number) {
    if (suggestions.status !== "ready") return;
    const item = suggestions.data.items[index];
    if (!item) return;
    setSelectedId(item.id);
    setEditedText(item.suggested_text);
    setEditing(false);
    setError("");
    setRequestedMissing(false);
    const query = new URLSearchParams(window.location.search);
    query.set("suggestion", item.id);
    window.history.replaceState({}, "", `/suggestions/${analysisId}?${query.toString()}`);
  }

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
  }, [suggestions.data, suggestions.status]);

  async function decide(action: "accept" | "edit" | "ignore" | "revert") {
    if (!selected || !selectedStatus || running) return;
    const allowed = selectedStatus === "pending"
      ? ["accept", "edit", "ignore"]
      : selectedStatus === "blocked"
        ? ["ignore"]
        : ["accepted", "edited", "ignored"].includes(selectedStatus)
          ? ["revert"]
          : [];
    if (!allowed.includes(action)) return;
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
      const result = await createWebApiClient().post<typeof body, components["schemas"]["DecisionResponse"]>(
        `/v1/suggestions/${selected.id}/${action}`,
        body,
        decisionOperation.current.key,
      );
      if (
        result.suggestion_id !== selected.id
        || !suggestionStatuses.has(result.status as SuggestionStatus)
      ) {
        throw new Error("Suggestion decision returned an invalid state");
      }
      decisionOperation.current = { fingerprint: "", key: "" };
      setDecisionStatuses((current) => ({
        ...current,
        [selected.id]: result.status as SuggestionStatus,
      }));
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
      const key = event.key.toLowerCase();
      if (selectedStatus === "pending" && key === "a") void decide("accept");
      else if (selectedStatus === "pending" && key === "e") void decide("edit");
      else if (["pending", "blocked"].includes(selectedStatus ?? "") && key === "i") void decide("ignore");
      else if (["accepted", "edited", "ignored"].includes(selectedStatus ?? "") && key === "z") void decide("revert");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <Page
      eyebrow="逐条建议 · A 接受 · E 编辑 · I 忽略 · Z 撤销"
      status={{
        label: running ? "正在保存" : statusLabel(selectedStatus),
        tone: error ? "error" : ["pending", "blocked"].includes(selectedStatus ?? "") ? "pending" : "success",
      }}
      title="确认每一处修改"
    >
      {suggestions.status === "loading" ? <section className="panel" role="status">正在读取建议…</section> : null}
      {suggestions.status === "error" ? <section className="panel" role="alert"><p>{suggestions.error}</p><Button onClick={suggestions.reload} variant="secondary">重试</Button></section> : null}
      {suggestions.status === "ready" && suggestions.data.items.length === 0 ? <section className="panel"><p>这次匹配没有生成可处理建议。</p><Link href="/resumes">返回我的简历</Link></section> : null}
      {requestedMissing ? <section className="panel" role="alert"><p>{error}</p><Link href={`/suggestions/${analysisId}`}>查看此分析的建议</Link></section> : null}
      {selected ? (
        <>
          <nav className="button-row" aria-label="建议导航">
            <Button
              disabled={selectedIndex <= 0}
              onClick={() => selectSuggestion(selectedIndex - 1)}
              variant="secondary"
            >
              上一条建议
            </Button>
            <span className="resource-id">{selectedIndex + 1} / {suggestions.status === "ready" ? suggestions.data.items.length : 0}</span>
            <Button
              disabled={suggestions.status !== "ready" || selectedIndex >= suggestions.data.items.length - 1}
              onClick={() => selectSuggestion(selectedIndex + 1)}
              variant="secondary"
            >
              下一条建议
            </Button>
          </nav>
          <div className="suggestion-layout">
            <section className="panel panel--wide">
            <p className="eyebrow">JD 原要求</p>
            <h2>{selected.requirement_text ?? "未关联具体岗位要求"}</h2>
            {selected.generation_mode ? (
              <StatusTag tone="info">
                {selected.generation_mode === "rule_fallback" ? "基础解析" : "模型建议"}
              </StatusTag>
            ) : null}
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
              {selected.risk_flags.length > 0 ? `风险：${selected.risk_flags.map(riskFlagLabel).join("、")}` : "没有额外风险标记"}
            </StatusTag>
            <div className="button-row">
              {selectedStatus === "pending" ? (
                <>
                  <Button disabled={running} onClick={() => void decide("accept")} state={running ? "loading" : "default"}>接受建议</Button>
                  <Button disabled={running} onClick={() => void decide("edit")} variant="secondary">{editing ? "保存编辑" : "编辑后接受"}</Button>
                  <Button disabled={running} onClick={() => void decide("ignore")} variant="quiet">忽略建议</Button>
                </>
              ) : selectedStatus === "blocked" ? (
                <>
                <Button disabled={running} onClick={() => router.push("/create")} variant="secondary">补充事实</Button>
                  <Button disabled={running} onClick={() => void decide("ignore")} variant="quiet">忽略建议</Button>
                </>
              ) : ["accepted", "edited", "ignored"].includes(selectedStatus ?? "") ? (
                <Button disabled={running} onClick={() => void decide("revert")} variant="quiet">撤销</Button>
              ) : (
                <span className="resource-id">此建议没有可执行操作</span>
              )}
            </div>
            {error ? <p className="auth-error" role="alert">{error}</p> : null}
            {versionId ? <Link className="button button--primary" href={`/exports/new?version=${versionId}`}>进入导出</Link> : null}
            </section>
            <aside className="panel"><h2>事实引用</h2>
              <EvidenceList factIds={selected.fact_refs} />
              <p className="resource-id">修改位置：{resumeTargetLabel(selected.target_path)}</p>
            </aside>
          </div>
        </>
      ) : null}
    </Page>
  );
}
