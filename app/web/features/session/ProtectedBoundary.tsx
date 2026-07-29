"use client";

import { ApiError } from "@resume/shared/client";
import type { components } from "@resume/shared/schema";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { createWebApiClient } from "../api/client";

export type SessionUser = components["schemas"]["MeResponse"];

const PUBLIC_PATHS = new Set(["/", "/login", "/register", "/version"]);

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname) || pathname.startsWith("/legal/");
}

export function ProtectedBoundary({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [state, setState] = useState<"checking" | "error">("checking");
  const [readyPath, setReadyPath] = useState("");

  useEffect(() => {
    if (isPublicPath(pathname)) return;
    let active = true;
    setState("checking");
    createWebApiClient().get<SessionUser>("/v1/me").then(
      () => {
        if (active) setReadyPath(pathname);
      },
      (error: unknown) => {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          const returnTo = `${pathname}${window.location.search}${window.location.hash}`;
          router.replace(`/login?returnTo=${encodeURIComponent(returnTo)}`);
          return;
        }
        setState("error");
      },
    );
    return () => {
      active = false;
    };
  }, [pathname, router]);

  if (isPublicPath(pathname)) return children;
  if (state === "checking" && readyPath !== pathname) {
    return <main className="session-state" role="status">正在验证登录状态</main>;
  }
  if (state === "error") {
    return (
      <main className="session-state" role="alert">
        <h1>暂时无法验证登录状态</h1>
        <p>请检查网络后刷新页面。你的已保存数据不会丢失。</p>
      </main>
    );
  }
  return children;
}
