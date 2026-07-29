import Link from "next/link";
import type { ReactNode } from "react";

const links = [
  ["/home", "工作台"],
  ["/resumes", "我的简历"],
  ["/facts", "经历事实"],
  ["/tasks", "任务中心"],
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link className="wordmark" href="/home">简历证据台</Link>
        <nav aria-label="主导航" className="command-nav">
          {links.map(([href, label]) => <Link href={href} key={href}>{label}</Link>)}
          <Link className="command-pill" href="/settings">账号与数据</Link>
        </nav>
      </header>
      {children}
      <footer className="statement-footer">
        <p>每一句经历，都应该经得起面试追问。</p>
        <Link href="/settings">隐私与数据</Link>
      </footer>
    </div>
  );
}
