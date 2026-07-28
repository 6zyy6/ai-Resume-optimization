import { mergeBullets, moveItem, splitBullet } from "./editor-operations";

export type SaveState = "offline" | "saving" | "saved" | "error" | "conflict";

export interface EditorModule {
  id: string;
  title: string;
  bullets: string[];
}

export interface EditorSnapshot {
  modules: EditorModule[];
}

export interface EditorState {
  baseVersion: number;
  content: string;
  dirty: boolean;
  history: EditorSnapshot[];
  saveState: SaveState;
  serverContent?: string;
  snapshot: EditorSnapshot;
}

export type EditorAction =
  | { type: "edit"; content: string }
  | { type: "editBullet"; moduleIndex: number; bulletIndex: number; content: string }
  | { type: "splitBullet"; moduleIndex: number; bulletIndex: number }
  | { type: "mergeBullet"; moduleIndex: number; bulletIndex: number }
  | { type: "moveModule"; from: number; to: number }
  | { type: "addBullet"; moduleIndex: number }
  | { type: "deleteBullet"; moduleIndex: number; bulletIndex: number }
  | { type: "undo" }
  | { type: "saving" }
  | { type: "saved"; version: number }
  | { type: "offline" }
  | { type: "error" }
  | { type: "conflict"; serverContent: string };

function cloneSnapshot(snapshot: EditorSnapshot): EditorSnapshot {
  return { modules: snapshot.modules.map((module) => ({ ...module, bullets: [...module.bullets] })) };
}

function serialize(snapshot: EditorSnapshot): string {
  return JSON.stringify(snapshot);
}

function withSnapshot(state: EditorState, snapshot: EditorSnapshot): EditorState {
  if (serialize(snapshot) === state.content) return state;
  return {
    ...state,
    content: serialize(snapshot),
    dirty: true,
    history: [...state.history, cloneSnapshot(state.snapshot)].slice(-20),
    snapshot,
  };
}

export function createEditorState(content = "", baseVersion = 1): EditorState {
  const snapshot = {
    modules: [
      { bullets: ["华东大学 · 本科"], id: "education", title: "教育经历" },
      { bullets: [content], id: "project", title: "项目经历" },
      { bullets: ["用户研究"], id: "skills", title: "技能" },
    ],
  };
  return { baseVersion, content: serialize(snapshot), dirty: false, history: [], saveState: "saved", snapshot };
}

export function editorReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case "edit":
      return editorReducer(state, { type: "editBullet", moduleIndex: 1, bulletIndex: 0, content: action.content });
    case "editBullet": {
      const snapshot = cloneSnapshot(state.snapshot);
      const module = snapshot.modules[action.moduleIndex];
      if (!module || module.bullets[action.bulletIndex] === undefined) return state;
      module.bullets[action.bulletIndex] = action.content;
      return withSnapshot(state, snapshot);
    }
    case "splitBullet": {
      const snapshot = cloneSnapshot(state.snapshot);
      const module = snapshot.modules[action.moduleIndex];
      if (!module) return state;
      module.bullets = splitBullet(module.bullets, action.bulletIndex);
      return withSnapshot(state, snapshot);
    }
    case "mergeBullet": {
      const snapshot = cloneSnapshot(state.snapshot);
      const module = snapshot.modules[action.moduleIndex];
      if (!module) return state;
      module.bullets = mergeBullets(module.bullets, action.bulletIndex);
      return withSnapshot(state, snapshot);
    }
    case "moveModule": {
      const snapshot = cloneSnapshot(state.snapshot);
      snapshot.modules = moveItem(snapshot.modules, action.from, action.to);
      return withSnapshot(state, snapshot);
    }
    case "addBullet": {
      const snapshot = cloneSnapshot(state.snapshot);
      const module = snapshot.modules[action.moduleIndex];
      if (!module) return state;
      module.bullets.push("");
      return withSnapshot(state, snapshot);
    }
    case "deleteBullet": {
      const snapshot = cloneSnapshot(state.snapshot);
      const module = snapshot.modules[action.moduleIndex];
      if (!module || module.bullets.length <= 1) return state;
      module.bullets.splice(action.bulletIndex, 1);
      return withSnapshot(state, snapshot);
    }
    case "undo": {
      const previous = state.history.at(-1);
      if (!previous) return state;
      return {
        ...state,
        content: serialize(previous),
        dirty: true,
        history: state.history.slice(0, -1),
        snapshot: cloneSnapshot(previous),
      };
    }
    case "saving":
      return { ...state, saveState: "saving" };
    case "saved":
      return { ...state, baseVersion: action.version, dirty: false, saveState: "saved" };
    case "offline":
      return { ...state, dirty: true, saveState: "offline" };
    case "error":
      return { ...state, dirty: true, saveState: "error" };
    case "conflict":
      return { ...state, dirty: true, saveState: "conflict", serverContent: action.serverContent };
  }
}
