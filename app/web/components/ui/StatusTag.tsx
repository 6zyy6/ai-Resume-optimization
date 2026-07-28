import type { ReactNode } from "react";

const icons = { error: "!", info: "i", pending: "…", success: "✓" } as const;

export function StatusTag({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: keyof typeof icons;
}) {
  return (
    <span
      aria-label={typeof children === "string" ? children : undefined}
      className={`status status--${tone}`}
      role="status"
    >
      <span aria-hidden="true">{icons[tone]}</span>
      <span>{children}</span>
    </span>
  );
}
