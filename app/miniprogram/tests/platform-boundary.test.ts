import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(import.meta.dirname, "..", "src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : /\.(ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

describe("mini program platform boundary", () => {
  it("keeps WeChat globals inside platform adapters", () => {
    const violations = sourceFiles(root)
      .filter((file) => !relative(root, file).startsWith("platform/"))
      .filter((file) => /\bwx\./.test(readFileSync(file, "utf8")));
    expect(violations).toEqual([]);
  });

  it("starts WeChat login only from an active button handler", () => {
    const page = readFileSync(join(root, "pages/me/index.tsx"), "utf8");
    expect(page).toContain('onClick={() => void login()}');
    expect(page).toContain("loginFromUserAction");
    expect(readFileSync(join(root, "platform/auth.ts"), "utf8")).toContain("Taro.login()");
  });

  it("adds operation keys to all shared-client writes", () => {
    const adapter = readFileSync(join(root, "platform/request.ts"), "utf8");
    expect(adapter).toContain("operationKey: string");
    expect(adapter).toContain("api[method]<TBody, TResponse>(path, body, operationKey)");
    for (const file of sourceFiles(root).filter((path) => !path.includes("/platform/"))) {
      const source = readFileSync(file, "utf8");
      expect(source).not.toMatch(/\bapi\.(post|put|patch|delete)\s*\(/);
    }
  });
});
