import Link from "next/link";
import type { ReactNode } from "react";

import { AppShell } from "./AppShell";
import { StatusTag } from "./ui/StatusTag";

export function Page({
  actions,
  children,
  eyebrow,
  status,
  title,
}: {
  actions?: ReactNode;
  children: ReactNode;
  eyebrow: string;
  status?: { label: string; tone: "error" | "info" | "pending" | "success" };
  title: string;
}) {
  return (
    <AppShell>
      <main className="page">
        <header className="page-head">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <h1>{title}</h1>
          </div>
          <div className="page-head__actions">
            {status ? <StatusTag tone={status.tone}>{status.label}</StatusTag> : null}
            {actions}
          </div>
        </header>
        {children}
      </main>
    </AppShell>
  );
}

export function EmptyState({ action, href, text }: { action: string; href: string; text: string }) {
  return (
    <section className="empty-state">
      <span aria-hidden="true">□</span>
      <p>{text}</p>
      <Link className="button button--secondary" href={href}>{action}</Link>
    </section>
  );
}
