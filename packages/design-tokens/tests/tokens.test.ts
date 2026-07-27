import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { tokens } from "../src/tokens";

describe("design tokens", () => {
  it("exports the approved colors", () => {
    expect(tokens.color).toEqual({
      brand600: "#4F46E5",
      brand700: "#4338CA",
      ink950: "#111827",
      ink700: "#374151",
      ink500: "#6B7280",
      surface0: "#FFFFFF",
      surface50: "#F8FAFC",
      line200: "#E5E7EB",
      fact600: "#059669",
      pending600: "#D97706",
      risk600: "#DC2626",
      gap600: "#7C3AED",
    });
  });

  it("exposes the CSS entry point through the package export map", () => {
    const resolved = execFileSync(
      process.execPath,
      [
        "--input-type=module",
        "--eval",
        "console.log(import.meta.resolve('@resume/design-tokens/tokens.css'))",
      ],
      { cwd: process.cwd(), encoding: "utf8" },
    );

    expect(resolved.trim()).toMatch(/tokens\.css$/);
  });
});
