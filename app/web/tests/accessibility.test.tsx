import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "../components/ui/Button";
import { Field } from "../components/ui/Field";
import { StatusTag } from "../components/ui/StatusTag";
import { safeReturnTo } from "../features/session/return-to";

describe("shared UI accessibility", () => {
  it("keeps an accessible name in all eight interactive states", () => {
    const states = ["default", "hover", "focus", "active", "disabled", "loading", "error", "success"] as const;
    render(<>{states.map((state) => <Button key={state} state={state}>保存简历</Button>)}</>);
    expect(screen.getAllByRole("button", { name: "保存简历" })).toHaveLength(8);
  });

  it("keeps field names stable across all eight states", () => {
    const states = ["default", "hover", "focus", "active", "disabled", "loading", "error", "success"] as const;
    render(<>{states.map((state) => <Field key={state} label="经历标题" name={`title-${state}`} state={state} />)}</>);
    expect(screen.getAllByLabelText("经历标题")).toHaveLength(8);
    expect(screen.getAllByLabelText("经历标题").every((field) => field.getAttribute("data-state"))).toBe(true);
  });

  it("associates fields and describes statuses with icon and text", () => {
    render(
      <>
        <Field label="目标岗位" name="job" />
        <StatusTag tone="error">保存失败</StatusTag>
      </>,
    );
    expect(screen.getByRole("textbox", { name: "目标岗位" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "保存失败" })).toBeInTheDocument();
  });

  it("rejects malicious or external return URLs", () => {
    for (const value of [
      "https://evil.example", "http://evil.example", "//evil.example", "///evil.example",
      "/\\evil", "\\\\evil", "javascript:alert(1)", "data:text/html,evil",
      "mailto:test@example.com", "ftp://evil.example", "evil.example", "",
      " /home", "\n/home", "https:%2f%2fevil.example", "/%5cevil",
      "blob:https://evil.example/id", "file:///etc/passwd", "vbscript:evil", "about:blank",
    ]) {
      expect(safeReturnTo(value)).toBe("/home");
    }
    expect(safeReturnTo("/resumes/r_1/edit?tab=facts")).toBe("/resumes/r_1/edit?tab=facts");
  });
});
