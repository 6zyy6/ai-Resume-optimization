import { getStored, removeStored, setStored } from "../platform/storage";

export const MAX_DRAFT_BYTES = 200 * 1024;
export const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const PREFIX = "resume-draft:";

export interface LocalDraft<T = unknown> {
  resumeId: string;
  updatedAt: number;
  value: T;
}

type Hook = () => Promise<void>;
const flushHooks = new Set<Hook>();
const refreshHooks = new Set<Hook>();

export function encodedBytes(value: unknown): number {
  let bytes = 0;
  for (const character of JSON.stringify(value)) {
    const code = character.codePointAt(0) ?? 0;
    bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
  }
  return bytes;
}

export async function saveDraft<T>(draft: LocalDraft<T>): Promise<void> {
  if (encodedBytes(draft) > MAX_DRAFT_BYTES) {
    throw new Error("草稿超过 200KB，请精简内容后再保存。");
  }
  await setStored(`${PREFIX}${draft.resumeId}`, draft);
}

export async function loadDraft<T>(resumeId: string, now = Date.now()): Promise<LocalDraft<T> | undefined> {
  const draft = await getStored<LocalDraft<T>>(`${PREFIX}${resumeId}`);
  if (!draft) return undefined;
  if (now - draft.updatedAt > DRAFT_TTL_MS) {
    await removeDraft(resumeId);
    return undefined;
  }
  return draft;
}

export function removeDraft(resumeId: string): Promise<void> {
  return removeStored(`${PREFIX}${resumeId}`);
}

export async function syncDraft<T>(draft: LocalDraft<T>, sync: (draft: LocalDraft<T>) => Promise<void>) {
  await sync(draft);
  await removeDraft(draft.resumeId);
}

export function registerLifecycleHooks(hooks: { flush?: Hook; refresh?: Hook }): () => void {
  if (hooks.flush) flushHooks.add(hooks.flush);
  if (hooks.refresh) refreshHooks.add(hooks.refresh);
  return () => {
    if (hooks.flush) flushHooks.delete(hooks.flush);
    if (hooks.refresh) refreshHooks.delete(hooks.refresh);
  };
}

export async function flushRegisteredDrafts(): Promise<void> {
  await Promise.all([...flushHooks].map((hook) => hook()));
}

export async function refreshRegisteredResources(): Promise<void> {
  await Promise.all([...refreshHooks].map((hook) => hook()));
}
