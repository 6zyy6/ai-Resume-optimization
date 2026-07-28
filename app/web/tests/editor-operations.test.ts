import { describe, expect, it } from "vitest";

import { mergeBullets, moveItem, splitBullet } from "../features/editor/editor-operations";

describe("structured editor operations", () => {
  it("reorders modules deterministically", () => {
    expect(moveItem(["教育", "项目", "技能"], 1, 0)).toEqual(["项目", "教育", "技能"]);
  });

  it("splits and merges bullet text without losing content", () => {
    const split = splitBullet(["完成访谈；整理结论。"], 0);
    expect(split).toEqual(["完成访谈", "整理结论"]);
    expect(mergeBullets(split, 0)).toEqual(["完成访谈；整理结论"]);
  });

  it("leaves invalid operations unchanged", () => {
    const source = ["只有一条"];
    expect(moveItem(source, 2, 0)).toBe(source);
    expect(splitBullet(source, 0)).toBe(source);
    expect(mergeBullets(source, 0)).toBe(source);
  });
});
