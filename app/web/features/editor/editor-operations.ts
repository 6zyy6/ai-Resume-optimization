export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from < 0 || to < 0 || from >= items.length || to >= items.length || from === to) return items;
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

export function splitBullet(bullets: string[], index: number): string[] {
  const value = bullets[index];
  if (value === undefined) return bullets;
  const parts = value.split(/[；。]/).map((part) => part.trim()).filter(Boolean);
  if (parts.length < 2) return bullets;
  return [...bullets.slice(0, index), ...parts, ...bullets.slice(index + 1)];
}

export function mergeBullets(bullets: string[], index: number): string[] {
  if (index < 0 || index >= bullets.length - 1) return bullets;
  const merged = `${bullets[index]}；${bullets[index + 1]}`;
  return [...bullets.slice(0, index), merged, ...bullets.slice(index + 2)];
}
