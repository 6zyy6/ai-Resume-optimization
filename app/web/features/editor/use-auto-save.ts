"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type AutoSaveState = "offline" | "saving" | "saved" | "error" | "conflict";

interface SaveInput {
  baseVersion: number;
  content: string;
}

interface SaveResult {
  id?: string;
  version: number;
}

interface AutoSaveOptions extends SaveInput {
  dirty: boolean;
  online?: boolean;
  save: (input: SaveInput) => Promise<SaveResult>;
}

export function useAutoSave({
  baseVersion,
  content,
  dirty,
  online = typeof navigator === "undefined" ? true : navigator.onLine,
  save,
}: AutoSaveOptions) {
  const [state, setState] = useState<AutoSaveState>(online ? "saved" : "offline");
  const [currentVersion, setCurrentVersion] = useState(baseVersion);
  const [resourceId, setResourceId] = useState<string | null>(null);
  const [isDirty, setDirty] = useState(dirty);
  const conflictRef = useRef(false);
  const savingRef = useRef(false);
  const lastSavedContentRef = useRef(dirty ? null : content);
  const latestRef = useRef({ content, dirty, online, save });
  latestRef.current = { content, dirty, online, save };

  useEffect(() => {
    setDirty(dirty && content !== lastSavedContentRef.current);
  }, [dirty, content]);
  useEffect(() => {
    if (!online) setState("offline");
  }, [online]);

  const persist = useCallback(async () => {
    const latest = latestRef.current;
    if (
      !latest.online ||
      conflictRef.current ||
      savingRef.current ||
      latest.content === lastSavedContentRef.current
    ) return;
    savingRef.current = true;
    setState("saving");
    try {
      const result = await latest.save({ baseVersion: currentVersion, content: latest.content });
      lastSavedContentRef.current = latest.content;
      setCurrentVersion(result.version);
      if (result.id) setResourceId(result.id);
      setDirty(false);
      setState("saved");
    } catch (error) {
      const failure = error as { status?: number };
      if (failure.status === 409) {
        conflictRef.current = true;
        setState("conflict");
      } else {
        setState("error");
      }
      setDirty(true);
    } finally {
      savingRef.current = false;
    }
  }, [currentVersion]);

  useEffect(() => {
    if (!dirty || !online || conflictRef.current) return;
    const timer = window.setTimeout(() => void persist(), 800);
    return () => window.clearTimeout(timer);
  }, [content, dirty, online, persist]);

  useEffect(() => {
    const timer = window.setInterval(() => void persist(), 15_000);
    return () => window.clearInterval(timer);
  }, [persist]);

  useEffect(() => {
    const beforeUnload = () => void persist();
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [persist]);

  return { baseVersion: currentVersion, dirty: isDirty, resourceId, saveNow: persist, state };
}
