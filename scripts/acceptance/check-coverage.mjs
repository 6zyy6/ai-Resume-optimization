export const GROUP_COUNTS = {
  ENG: 10, FLOW: 14, WEB: 10, MP: 10, AI: 14, FILE: 12, DATA: 10,
  PERF: 12, SEC: 14, OBS: 12, UX: 8, USER: 10, OPS: 10,
};

export const ACCEPTANCE_IDS = Object.entries(GROUP_COUNTS).flatMap(([group, count]) =>
  Array.from({ length: count }, (_, index) => `${group}-${String(index + 1).padStart(2, "0")}`),
);

export function checkCoverage(items) {
  const ids = items.map((item) => item.id);
  const unknown = ids.filter((id) => !ACCEPTANCE_IDS.includes(id));
  const missing = ACCEPTANCE_IDS.filter((id) => !ids.includes(id));
  const duplicates = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
  return { valid: !unknown.length && !missing.length && !duplicates.length && ids.length === 146, unknown, missing, duplicates };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const manifest = JSON.parse(await (await import("node:fs/promises")).readFile(process.argv[2], "utf8"));
  const result = checkCoverage(manifest.acceptance_items);
  console.log(JSON.stringify(result));
  if (!result.valid) process.exitCode = 1;
}
