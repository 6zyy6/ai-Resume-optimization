import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "@resume/design-tokens/tokens.css";
import "./globals.css";

export const metadata: Metadata = {
  description: "从真实经历出发，创建和优化可验证的大学生简历。",
  title: { default: "简历证据台", template: "%s · 简历证据台" },
};

export const viewport: Viewport = {
  initialScale: 1,
  viewportFit: "cover",
  width: "device-width",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
