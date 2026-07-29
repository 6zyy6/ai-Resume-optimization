import { beforeEach, describe, expect, it, vi } from "vitest";

const memory = new Map<string, unknown>();
vi.mock("../src/platform/storage", () => ({
  getStored: vi.fn(async (key: string) => memory.get(key)),
  setStored: vi.fn(async (key: string, value: unknown) => void memory.set(key, value)),
  removeStored: vi.fn(async (key: string) => void memory.delete(key)),
}));

import {
  DRAFT_TTL_MS,
  MAX_DRAFT_BYTES,
  loadDraft,
  registerLifecycleHooks,
  saveDraft,
  syncDraft,
  flushRegisteredDrafts,
  refreshRegisteredResources,
} from "../src/features/draft-store";

describe("draft store", () => {
  beforeEach(() => memory.clear());

  it("rejects drafts larger than 200KB", async () => {
    await expect(saveDraft({ resumeId: "r1", updatedAt: 1, value: "x".repeat(MAX_DRAFT_BYTES) }))
      .rejects.toThrow("200KB");
  });

  it("expires drafts after seven days", async () => {
    await saveDraft({ resumeId: "r1", updatedAt: 100, value: { title: "draft" } });
    await expect(loadDraft("r1", 100 + DRAFT_TTL_MS + 1)).resolves.toBeUndefined();
  });

  it("removes the local draft only after successful sync", async () => {
    const draft = { resumeId: "r1", updatedAt: 100, value: { title: "draft" } };
    await saveDraft(draft);
    await expect(syncDraft(draft, async () => { throw new Error("offline"); })).rejects.toThrow();
    await expect(loadDraft("r1", 101)).resolves.toBeDefined();
    await syncDraft(draft, async () => undefined);
    await expect(loadDraft("r1", 101)).resolves.toBeUndefined();
  });

  it("flushes on hide and refreshes on show through registered hooks", async () => {
    const flush = vi.fn(async () => undefined);
    const refresh = vi.fn(async () => undefined);
    const unregister = registerLifecycleHooks({ flush, refresh });
    await flushRegisteredDrafts();
    await refreshRegisteredResources();
    expect(flush).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
    unregister();
  });
});
