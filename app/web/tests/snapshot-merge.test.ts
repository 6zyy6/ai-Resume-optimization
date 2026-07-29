import type { components } from "@resume/shared/schema";
import { describe, expect, it } from "vitest";

import {
  mergeResumeSnapshots,
  resumeSnapshotConflicts,
  type SnapshotMergeChoice,
} from "../features/editor/snapshot-merge";

type ResumeSnapshot = components["schemas"]["ResumeSnapshot"];

function bullet(id: string, text: string, fact = id) {
  return { fact_refs: [fact], id, text };
}

function snapshots(): { cloud: ResumeSnapshot; local: ResumeSnapshot } {
  return {
    cloud: {
      schema_version: "1",
      sections: [
        {
          id: "a",
          items: [
            bullet("a1", "云端修改的第一条", "fact-cloud"),
            bullet("a2", "第二条"),
            bullet("a3", "云端保留、但本机删除"),
          ],
          title: "云端项目经历",
          type: "project",
        },
        {
          id: "b",
          items: [bullet("b1", "共同内容")],
          title: "教育经历",
          type: "education",
        },
        {
          id: "c",
          items: [bullet("c1", "云端模块")],
          title: "云端新增模块",
          type: "experience",
        },
      ],
      target: "云端目标",
      title: "云端标题",
    },
    local: {
      schema_version: "1",
      sections: [
        {
          id: "b",
          items: [bullet("b1", "共同内容")],
          title: "教育经历",
          type: "education",
        },
        {
          id: "a",
          items: [
            bullet("a2", "第二条"),
            bullet("a1", "本机修改的第一条", "fact-local"),
          ],
          title: "本机项目标题",
          type: "experience",
        },
        {
          id: "d",
          items: [bullet("d1", "本机模块")],
          title: "本机新增模块",
          type: "skills",
        },
      ],
      target: "本机目标",
      title: "本机标题",
    },
  };
}

describe("resume snapshot conflict merge", () => {
  it("reports every top-level and structural difference that needs a decision", () => {
    const { cloud, local } = snapshots();

    expect(resumeSnapshotConflicts(local, cloud).map((conflict) => conflict.id)).toEqual([
      "snapshot:title",
      "snapshot:target",
      "sections:presence:c",
      "sections:presence:d",
      "sections:order",
      "section:a:title",
      "section:a:type",
      "bullets:a:presence:a3",
      "bullets:a:order",
      "bullet:a:a1:text",
    ]);
  });

  it("merges title, target, section metadata/order/deletion and bullet text/order/deletion without loss", () => {
    const { cloud, local } = snapshots();
    const choices: Record<string, SnapshotMergeChoice> = {
      "bullet:a:a1:text": "cloud",
      "bullets:a:order": "local",
      "bullets:a:presence:a3": "local",
      "section:a:title": "cloud",
      "section:a:type": "cloud",
      "sections:order": "local",
      "sections:presence:c": "local",
      "sections:presence:d": "local",
      "snapshot:target": "local",
      "snapshot:title": "cloud",
    };

    const merged = mergeResumeSnapshots(local, cloud, choices);

    expect(merged.title).toBe("云端标题");
    expect(merged.target).toBe("本机目标");
    expect(merged.sections.map((section) => section.id)).toEqual(["b", "a", "d"]);
    expect(merged.sections.find((section) => section.id === "c")).toBeUndefined();
    expect(merged.sections.find((section) => section.id === "d")?.items[0].text).toBe("本机模块");

    const project = merged.sections.find((section) => section.id === "a");
    expect(project?.title).toBe("云端项目经历");
    expect(project?.type).toBe("project");
    expect(project?.items.map((item) => item.id)).toEqual(["a2", "a1"]);
    expect(project?.items.find((item) => item.id === "a3")).toBeUndefined();
    expect(project?.items.find((item) => item.id === "a1")).toEqual(
      bullet("a1", "云端修改的第一条", "fact-cloud"),
    );
  });

  it("refuses to merge while any conflict has no explicit decision", () => {
    const { cloud, local } = snapshots();

    expect(() => mergeResumeSnapshots(local, cloud, {})).toThrow(
      "Missing merge choice for snapshot:title",
    );
  });

  it("requires an explicit choice when equal text has different fact evidence", () => {
    const { cloud, local } = snapshots();
    cloud.sections = local.sections.map((section) => ({
      ...section,
      items: section.items.map((item) => ({ ...item, fact_refs: [...item.fact_refs] })),
    }));
    cloud.title = local.title;
    cloud.target = local.target;
    const cloudBullet = cloud.sections[1].items.find((item) => item.id === "a1");
    if (!cloudBullet) throw new Error("missing test bullet");
    cloudBullet.fact_refs = ["fact-cloud-only"];

    const conflicts = resumeSnapshotConflicts(local, cloud);
    expect(conflicts.map((conflict) => conflict.id)).toEqual(["bullet:a:a1:fact_refs"]);
    expect(mergeResumeSnapshots(local, cloud, {
      "bullet:a:a1:fact_refs": "local",
    }).sections[1].items.find((item) => item.id === "a1")?.fact_refs).toEqual(["fact-local"]);
  });
});
