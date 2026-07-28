import { describe, expect, it } from "vitest";

import { createEditorState, editorReducer } from "../features/editor/editor-reducer";

describe("editor reducer", () => {
  it("retains exactly the latest 20 undoable edits and underflows safely", () => {
    let state = createEditorState("初稿");
    for (let index = 1; index <= 25; index += 1) {
      state = editorReducer(state, { type: "edit", content: `版本 ${index}` });
    }
    expect(state.history).toHaveLength(20);
    for (let index = 0; index < 25; index += 1) {
      state = editorReducer(state, { type: "undo" });
    }
    expect(state.snapshot.modules[1].bullets[0]).toBe("版本 5");
    expect(state.history).toHaveLength(0);
  });

  it("marks edits dirty and records a successful server version", () => {
    let state = editorReducer(createEditorState("初稿", 2), { type: "edit", content: "新稿" });
    expect(state.dirty).toBe(true);
    state = editorReducer(state, { type: "saved", version: 3 });
    expect(state).toMatchObject({ dirty: false, saveState: "saved", baseVersion: 3 });
  });

  it("keeps the dirty draft and stops saves after a conflict", () => {
    let state = editorReducer(createEditorState("本地"), { type: "edit", content: "未同步内容" });
    state = editorReducer(state, { type: "conflict", serverContent: "云端内容" });
    expect(state).toMatchObject({
      dirty: true,
      saveState: "conflict",
      serverContent: "云端内容",
    });
    expect(state.snapshot.modules[1].bullets[0]).toBe("未同步内容");
  });

  it("uses one snapshot for split, merge, add, delete, move and undo", () => {
    let state = editorReducer(createEditorState("完成访谈；整理结论。"), { type: "splitBullet", moduleIndex: 1, bulletIndex: 0 });
    expect(state.snapshot.modules[1].bullets).toEqual(["完成访谈", "整理结论"]);
    state = editorReducer(state, { type: "mergeBullet", moduleIndex: 1, bulletIndex: 0 });
    state = editorReducer(state, { type: "addBullet", moduleIndex: 1 });
    expect(state.snapshot.modules[1].bullets).toEqual(["完成访谈；整理结论", ""]);
    state = editorReducer(state, { type: "deleteBullet", moduleIndex: 1, bulletIndex: 1 });
    state = editorReducer(state, { type: "moveModule", from: 1, to: 0 });
    expect(state.snapshot.modules[0].id).toBe("project");
    state = editorReducer(state, { type: "undo" });
    expect(state.snapshot.modules[1].id).toBe("project");
    expect(JSON.parse(state.content)).toEqual(state.snapshot);
  });
});
