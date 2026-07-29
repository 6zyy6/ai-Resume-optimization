import type { components } from "@resume/shared/schema";

type ResumeBullet = components["schemas"]["ResumeBullet"];
type ResumeSection = components["schemas"]["ResumeSection"];
type ResumeSnapshot = components["schemas"]["ResumeSnapshot"];

export type SnapshotMergeChoice = "cloud" | "local";

export interface SnapshotMergeConflict {
  cloud: string;
  id: string;
  label: string;
  local: string;
}

function cloneBullet(item: ResumeBullet): ResumeBullet {
  return { ...item, fact_refs: [...item.fact_refs] };
}

function cloneSection(section: ResumeSection): ResumeSection {
  return { ...section, items: section.items.map(cloneBullet) };
}

function text(value: string | null): string {
  return value?.trim() ? value : "（空）";
}

function orderText(ids: string[], labels: Map<string, string>): string {
  return ids.map((id) => labels.get(id) ?? id).join(" → ") || "（空）";
}

function presenceText(present: boolean, label: string): string {
  return present ? `保留：${label}` : `删除：${label}`;
}

function sameOrder(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

export function resumeSnapshotConflicts(
  local: ResumeSnapshot,
  cloud: ResumeSnapshot,
): SnapshotMergeConflict[] {
  const conflicts: SnapshotMergeConflict[] = [];
  if (local.title !== cloud.title) {
    conflicts.push({
      cloud: text(cloud.title),
      id: "snapshot:title",
      label: "简历标题",
      local: text(local.title),
    });
  }
  if (local.target !== cloud.target) {
    conflicts.push({
      cloud: text(cloud.target),
      id: "snapshot:target",
      label: "求职目标",
      local: text(local.target),
    });
  }

  const localSections = new Map(local.sections.map((section) => [section.id, section]));
  const cloudSections = new Map(cloud.sections.map((section) => [section.id, section]));
  const sectionLabels = new Map([
    ...cloud.sections.map((section) => [section.id, section.title] as const),
    ...local.sections.map((section) => [section.id, section.title] as const),
  ]);
  const sectionIds = [
    ...cloud.sections.map((section) => section.id),
    ...local.sections.map((section) => section.id).filter((id) => !cloudSections.has(id)),
  ];

  for (const id of sectionIds) {
    const localSection = localSections.get(id);
    const cloudSection = cloudSections.get(id);
    if (Boolean(localSection) === Boolean(cloudSection)) continue;
    const label = localSection?.title ?? cloudSection?.title ?? id;
    conflicts.push({
      cloud: presenceText(Boolean(cloudSection), label),
      id: `sections:presence:${id}`,
      label: `模块“${label}”是否保留`,
      local: presenceText(Boolean(localSection), label),
    });
  }

  const localSectionOrder = local.sections.map((section) => section.id);
  const cloudSectionOrder = cloud.sections.map((section) => section.id);
  if (!sameOrder(localSectionOrder, cloudSectionOrder)) {
    conflicts.push({
      cloud: orderText(cloudSectionOrder, sectionLabels),
      id: "sections:order",
      label: "模块顺序",
      local: orderText(localSectionOrder, sectionLabels),
    });
  }

  for (const localSection of local.sections) {
    const cloudSection = cloudSections.get(localSection.id);
    if (!cloudSection) continue;
    if (localSection.title !== cloudSection.title) {
      conflicts.push({
        cloud: text(cloudSection.title),
        id: `section:${localSection.id}:title`,
        label: `模块“${localSection.title}”的标题`,
        local: text(localSection.title),
      });
    }
    if (localSection.type !== cloudSection.type) {
      conflicts.push({
        cloud: text(cloudSection.type),
        id: `section:${localSection.id}:type`,
        label: `模块“${localSection.title}”的类型`,
        local: text(localSection.type),
      });
    }

    const localBullets = new Map(localSection.items.map((item) => [item.id, item]));
    const cloudBullets = new Map(cloudSection.items.map((item) => [item.id, item]));
    const bulletLabels = new Map([
      ...cloudSection.items.map((item) => [item.id, item.text] as const),
      ...localSection.items.map((item) => [item.id, item.text] as const),
    ]);
    const bulletIds = [
      ...cloudSection.items.map((item) => item.id),
      ...localSection.items.map((item) => item.id).filter((id) => !cloudBullets.has(id)),
    ];
    for (const id of bulletIds) {
      const localBullet = localBullets.get(id);
      const cloudBullet = cloudBullets.get(id);
      if (Boolean(localBullet) === Boolean(cloudBullet)) continue;
      const label = localBullet?.text ?? cloudBullet?.text ?? id;
      conflicts.push({
        cloud: presenceText(Boolean(cloudBullet), label),
        id: `bullets:${localSection.id}:presence:${id}`,
        label: `要点“${label}”是否保留`,
        local: presenceText(Boolean(localBullet), label),
      });
    }

    const localBulletOrder = localSection.items.map((item) => item.id);
    const cloudBulletOrder = cloudSection.items.map((item) => item.id);
    if (!sameOrder(localBulletOrder, cloudBulletOrder)) {
      conflicts.push({
        cloud: orderText(cloudBulletOrder, bulletLabels),
        id: `bullets:${localSection.id}:order`,
        label: `模块“${localSection.title}”的要点顺序`,
        local: orderText(localBulletOrder, bulletLabels),
      });
    }

    for (const localBullet of localSection.items) {
      const cloudBullet = cloudBullets.get(localBullet.id);
      if (!cloudBullet) continue;
      if (localBullet.text !== cloudBullet.text) {
        conflicts.push({
          cloud: text(cloudBullet.text),
          id: `bullet:${localSection.id}:${localBullet.id}:text`,
          label: `模块“${localSection.title}”中的要点文字`,
          local: text(localBullet.text),
        });
      } else if (!sameOrder(localBullet.fact_refs, cloudBullet.fact_refs)) {
        conflicts.push({
          cloud: orderText(cloudBullet.fact_refs, new Map()),
          id: `bullet:${localSection.id}:${localBullet.id}:fact_refs`,
          label: `模块“${localSection.title}”中的要点事实来源`,
          local: orderText(localBullet.fact_refs, new Map()),
        });
      }
    }
  }
  return conflicts;
}

function chosen(
  choices: Record<string, SnapshotMergeChoice>,
  id: string,
): SnapshotMergeChoice {
  const choice = choices[id];
  if (!choice) throw new Error(`Missing merge choice for ${id}`);
  return choice;
}

function reconcileOrder<T>(
  retained: Map<string, T>,
  preferred: string[],
  secondary: string[],
): T[] {
  const ordered = preferred.filter((id) => retained.has(id));
  for (const id of secondary) {
    if (!retained.has(id) || ordered.includes(id)) continue;
    const secondaryIndex = secondary.indexOf(id);
    const following = secondary.slice(secondaryIndex + 1).find((candidate) => ordered.includes(candidate));
    if (following) {
      ordered.splice(ordered.indexOf(following), 0, id);
      continue;
    }
    const preceding = secondary.slice(0, secondaryIndex).reverse().find((candidate) => ordered.includes(candidate));
    if (preceding) {
      ordered.splice(ordered.lastIndexOf(preceding) + 1, 0, id);
    } else {
      ordered.push(id);
    }
  }
  return ordered.map((id) => retained.get(id) as T);
}

function mergeSection(
  local: ResumeSection,
  cloud: ResumeSection,
  choices: Record<string, SnapshotMergeChoice>,
): ResumeSection {
  const sectionId = local.id;
  const title = local.title === cloud.title
    ? cloud.title
    : chosen(choices, `section:${sectionId}:title`) === "local" ? local.title : cloud.title;
  const type = local.type === cloud.type
    ? cloud.type
    : chosen(choices, `section:${sectionId}:type`) === "local" ? local.type : cloud.type;
  const localBullets = new Map(local.items.map((item) => [item.id, item]));
  const cloudBullets = new Map(cloud.items.map((item) => [item.id, item]));
  const retained = new Map<string, ResumeBullet>();
  const bulletIds = new Set([...localBullets.keys(), ...cloudBullets.keys()]);

  for (const id of bulletIds) {
    const localBullet = localBullets.get(id);
    const cloudBullet = cloudBullets.get(id);
    if (localBullet && cloudBullet) {
      const source = localBullet.text !== cloudBullet.text
        ? chosen(choices, `bullet:${sectionId}:${id}:text`) === "local" ? localBullet : cloudBullet
        : !sameOrder(localBullet.fact_refs, cloudBullet.fact_refs)
          ? chosen(choices, `bullet:${sectionId}:${id}:fact_refs`) === "local" ? localBullet : cloudBullet
          : cloudBullet;
      retained.set(id, cloneBullet(source));
      continue;
    }
    const choice = chosen(choices, `bullets:${sectionId}:presence:${id}`);
    const source = choice === "local" ? localBullet : cloudBullet;
    if (source) retained.set(id, cloneBullet(source));
  }

  const localOrder = local.items.map((item) => item.id);
  const cloudOrder = cloud.items.map((item) => item.id);
  const orderChoice = sameOrder(localOrder, cloudOrder)
    ? "cloud"
    : chosen(choices, `bullets:${sectionId}:order`);
  return {
    id: sectionId,
    items: reconcileOrder(
      retained,
      orderChoice === "local" ? localOrder : cloudOrder,
      orderChoice === "local" ? cloudOrder : localOrder,
    ),
    title,
    type,
  };
}

export function mergeResumeSnapshots(
  local: ResumeSnapshot,
  cloud: ResumeSnapshot,
  choices: Record<string, SnapshotMergeChoice>,
): ResumeSnapshot {
  for (const conflict of resumeSnapshotConflicts(local, cloud)) chosen(choices, conflict.id);

  const title = local.title === cloud.title
    ? cloud.title
    : chosen(choices, "snapshot:title") === "local" ? local.title : cloud.title;
  const target = local.target === cloud.target
    ? cloud.target
    : chosen(choices, "snapshot:target") === "local" ? local.target : cloud.target;
  const localSections = new Map(local.sections.map((section) => [section.id, section]));
  const cloudSections = new Map(cloud.sections.map((section) => [section.id, section]));
  const retained = new Map<string, ResumeSection>();
  const sectionIds = new Set([...localSections.keys(), ...cloudSections.keys()]);

  for (const id of sectionIds) {
    const localSection = localSections.get(id);
    const cloudSection = cloudSections.get(id);
    if (localSection && cloudSection) {
      retained.set(id, mergeSection(localSection, cloudSection, choices));
      continue;
    }
    const choice = chosen(choices, `sections:presence:${id}`);
    const source = choice === "local" ? localSection : cloudSection;
    if (source) retained.set(id, cloneSection(source));
  }

  const localOrder = local.sections.map((section) => section.id);
  const cloudOrder = cloud.sections.map((section) => section.id);
  const orderChoice = sameOrder(localOrder, cloudOrder)
    ? "cloud"
    : chosen(choices, "sections:order");
  return {
    schema_version: "1",
    sections: reconcileOrder(
      retained,
      orderChoice === "local" ? localOrder : cloudOrder,
      orderChoice === "local" ? cloudOrder : localOrder,
    ),
    target,
    title,
  };
}
