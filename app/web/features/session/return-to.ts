export function safeReturnTo(value?: string | null): string {
  let decoded = "";
  try {
    decoded = decodeURIComponent(value ?? "");
  } catch {
    return "/home";
  }
  if (!value || !value.startsWith("/") || value.startsWith("//") || decoded.includes("\\") || decoded.startsWith("//")) {
    return "/home";
  }
  try {
    const parsed = new URL(value, "https://resume.local");
    return parsed.origin === "https://resume.local"
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : "/home";
  } catch {
    return "/home";
  }
}
