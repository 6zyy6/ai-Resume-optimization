import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAutoSave } from "../features/editor/use-auto-save";

describe("useAutoSave", () => {
  beforeEach(() => vi.useFakeTimers());

  it("debounces for 800ms, advances the version and does not repeat an unchanged save", async () => {
    const save = vi.fn().mockResolvedValue({ version: 2 });
    const { rerender, result } = renderHook(
      ({ content }) => useAutoSave({ baseVersion: 1, content, dirty: true, save }),
      { initialProps: { content: "一" } },
    );
    rerender({ content: "二" });
    await act(async () => vi.advanceTimersByTimeAsync(799));
    expect(save).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(save).toHaveBeenCalledWith({ baseVersion: 1, content: "二" });
    expect(result.current).toMatchObject({ baseVersion: 2, dirty: false, state: "saved" });
    save.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(save).not.toHaveBeenCalled();
  });

  it("performs the 15-second fallback while continuous edits postpone debounce", async () => {
    const save = vi.fn().mockResolvedValue({ version: 2 });
    const { rerender } = renderHook(
      ({ content }) => useAutoSave({ baseVersion: 1, content, dirty: true, save }),
      { initialProps: { content: "版本 0" } },
    );
    for (let index = 1; index <= 21; index += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(700));
      rerender({ content: `版本 ${index}` });
    }
    expect(save).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(save).toHaveBeenCalledWith({ baseVersion: 1, content: "版本 21" });
  });

  it("retains dirty content on failure and stops after a 409 conflict", async () => {
    const save = vi.fn().mockRejectedValue({ status: 409, serverContent: "云端" });
    const { result } = renderHook(() =>
      useAutoSave({ baseVersion: 1, content: "本地", dirty: true, save }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(800));
    expect(result.current).toMatchObject({ state: "conflict", dirty: true });
    save.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(save).not.toHaveBeenCalled();
  });

  it("uses local offline state without pretending the draft is saved", () => {
    const { result } = renderHook(() =>
      useAutoSave({
        baseVersion: 1,
        content: "本地",
        dirty: true,
        online: false,
        save: vi.fn(),
      }),
    );
    expect(result.current).toMatchObject({ state: "offline", dirty: true });
  });
});
