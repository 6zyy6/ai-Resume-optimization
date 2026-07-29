"use client";

import type { components } from "@resume/shared/schema";
import { type ChangeEvent, useRef, useState } from "react";

import { EmptyState, Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { StatusTag } from "../../components/ui/StatusTag";
import { createWebApiClient } from "../../features/api/client";
import { useApiResource } from "../../features/api/useApiResource";

export default function FactsPage() {
  const facts = useApiResource<components["schemas"]["FactListResponse"]>("/v1/facts?limit=50");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [sources, setSources] = useState<Record<string, components["schemas"]["SourceInput"][]>>({});
  const [busy, setBusy] = useState("");
  const operationKeys = useRef<Record<string, string>>({});

  async function setStatus(factId: string, action: "confirm" | "reject") {
    const operation = `${factId}:${action}`;
    operationKeys.current[operation] ||= crypto.randomUUID();
    setBusy(operation);
    try {
      await createWebApiClient().post(`/v1/facts/${factId}/${action}`, {}, operationKeys.current[operation]);
      delete operationKeys.current[operation];
      facts.reload();
    } finally {
      setBusy("");
    }
  }

  async function toggleSources(factId: string) {
    if (sources[factId]) {
      setSources((current) => {
        const next = { ...current };
        delete next[factId];
        return next;
      });
      return;
    }
    setBusy(`${factId}:sources`);
    try {
      const loaded = await createWebApiClient().get<components["schemas"]["FactSourcesResponse"]>(`/v1/facts/${factId}/sources`);
      setSources((current) => ({ ...current, [factId]: loaded.items }));
    } finally {
      setBusy("");
    }
  }

  const visibleFacts = facts.status === "ready"
    ? facts.data.items.filter((fact) => (
        (filter === "all" || fact.status === filter)
        && `${fact.kind} ${fact.value}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
      ))
    : [];

  return (
    <Page actions={<a className="button button--secondary" href="/create">从回答新增</a>} eyebrow="经历事实库" title="面试时能解释的内容">
      {facts.status === "loading" ? <section className="panel" role="status">正在读取事实…</section> : null}
      {facts.status === "error" ? <section className="panel" role="alert"><p>{facts.error}</p><Button onClick={facts.reload} variant="secondary">重试</Button></section> : null}
      {facts.status === "ready" && facts.data.items.length === 0 ? <EmptyState action="开始梳理经历" href="/create" text="还没有经历事实。回答问题后，确认过且有来源的内容会出现在这里。" /> : null}
      {facts.status === "ready" && facts.data.items.length > 0 ? (
        <section className="panel filter-row">
          <Field label="搜索事实" name="fact-search" onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.currentTarget.value)} value={query} />
          <label className="field">
            <span className="field__label">状态</span>
            <select className="field__control" onChange={(event) => setFilter(event.currentTarget.value)} value={filter}>
              <option value="all">全部</option>
              <option value="confirmed">已确认</option>
              <option value="unconfirmed">待确认</option>
              <option value="rejected">不采用</option>
            </select>
          </label>
        </section>
      ) : null}
      {facts.status === "ready" && facts.data.items.length > 0 && visibleFacts.length === 0 ? <section className="panel">没有符合当前筛选的事实。</section> : null}
      {facts.status === "ready" ? visibleFacts.map((fact) => (
        <article className="resume-row" key={fact.id}>
          <div>
            <StatusTag tone={fact.status === "confirmed" ? "success" : fact.status === "rejected" ? "error" : "pending"}>
              {fact.status} · {fact.source_ids.length > 0 ? `${fact.source_ids.length} 个来源` : "无来源"}
            </StatusTag>
            <h2>{fact.kind}</h2>
            <p>{fact.value}</p>
            {sources[fact.id]?.map((source, index) => (
              <blockquote className="audit-card" key={`${source.source_type}:${index}`}>
                <strong>{source.source_type}</strong>
                <p>{source.content}</p>
              </blockquote>
            ))}
          </div>
          <div className="button-row">
            <Button disabled={busy === `${fact.id}:sources`} onClick={() => void toggleSources(fact.id)} variant="quiet">
              {sources[fact.id] ? "收起来源" : "查看来源"}
            </Button>
            {fact.status === "unconfirmed" ? (
              <>
                <Button disabled={Boolean(busy)} onClick={() => void setStatus(fact.id, "confirm")} variant="secondary">确认</Button>
                <Button disabled={Boolean(busy)} onClick={() => void setStatus(fact.id, "reject")} variant="quiet">不采用</Button>
              </>
            ) : null}
            <span className="resource-id">#{fact.id.slice(-8)}</span>
          </div>
        </article>
      )) : null}
    </Page>
  );
}
