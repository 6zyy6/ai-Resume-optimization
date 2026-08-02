"use client";

import type { components } from "@resume/shared/schema";
import { useEffect, useState } from "react";

import { createWebApiClient } from "../api/client";
import { sourceTypeLabel } from "../presentation/business-labels";

type Evidence = {
  fact: components["schemas"]["FactResponse"];
  sources: components["schemas"]["SourceResponse"][];
};

export function EvidenceList({ factIds }: { factIds: string[] }) {
  const [items, setItems] = useState<Evidence[]>([]);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "error">(
    factIds.length > 0 ? "loading" : "idle",
  );
  const identity = factIds.join(",");

  useEffect(() => {
    const requestedFactIds = identity ? identity.split(",") : [];
    if (requestedFactIds.length === 0) {
      setItems([]);
      setState("idle");
      return;
    }
    let active = true;
    setState("loading");
    const api = createWebApiClient();
    Promise.all(requestedFactIds.map(async (factId) => {
      const [fact, sources] = await Promise.all([
        api.get<components["schemas"]["FactResponse"]>(`/v1/facts/${factId}`),
        api.get<components["schemas"]["FactSourcesResponse"]>(`/v1/facts/${factId}/sources`),
      ]);
      return { fact, sources: sources.items };
    })).then(
      (loaded) => {
        if (!active) return;
        setItems(loaded);
        setState("ready");
      },
      () => {
        if (active) setState("error");
      },
    );
    return () => {
      active = false;
    };
  }, [identity]);

  if (state === "idle") return <p>没有事实引用。</p>;
  if (state === "loading") return <p role="status">正在读取事实与来源…</p>;
  if (state === "error") return <p role="alert">事实来源读取失败，不能据此做决定。</p>;
  return (
    <ul className="module-list">
      {items.map(({ fact, sources }) => (
        <li key={fact.id}>
          <div>
            <strong>{fact.value}</strong>
            {sources.map((source, index) => (
              <p key={`${source.source_type}:${index}`}>{sourceTypeLabel(source.source_type)}：{source.content}</p>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}
