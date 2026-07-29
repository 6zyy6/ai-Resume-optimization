import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(import.meta.dirname, "..", "src");
const read = (path: string) => readFileSync(join(root, path), "utf8");

describe("mobile resume flow", () => {
  it("ships four tabs and every planned workflow page", () => {
    const config = read("app.config.ts");
    for (const page of ["pages/home/index", "pages/resumes/index", "pages/facts/index", "pages/me/index"]) {
      expect(config).toContain(page);
    }
    for (const page of ["editor", "preview", "import", "job", "match", "suggestions", "privacy"]) {
      expect(config).toContain(`"${page}"`);
    }
  });

  it("flushes on hide and refreshes on show", () => {
    const app = read("app.tsx");
    expect(app).toContain("useDidHide");
    expect(app).toContain("flushRegisteredDrafts");
    expect(app).toContain("useDidShow");
    expect(app).toContain("refreshRegisteredResources");
  });

  it("does not report save success when the platform capability is unavailable", () => {
    const files = read("platform/files.ts");
    expect(files).toContain('typeof Taro.saveFile !== "function"');
    expect(files).toContain("saved: false");
    expect(files).toContain("alternative:");
  });

  it("declares all eight Hallmark action states and 44px touch controls", () => {
    const component = read("components/ui/PrimaryAction.tsx");
    const styles = read("app.scss");
    for (const state of ["default", "hover", "focus", "active", "disabled", "loading", "error", "success"]) {
      expect(component).toContain(`"${state}"`);
      expect(styles).toContain(`primary-action--${state}`);
    }
    expect(styles).toMatch(/min-height:\s*44px/);
    expect(styles).toContain("env(safe-area-inset-bottom)");
  });

  it("uses button controls for suggestion sorting and full-screen source views", () => {
    const page = read("subpackages/optimize/suggestions.tsx");
    expect(page).toContain("sort-button");
    expect(page).toContain("source-sheet");
    expect(page).not.toContain("<Picker");
  });
});
