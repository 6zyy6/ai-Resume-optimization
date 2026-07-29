"use client";

import { ApiError } from "@resume/shared/client";
import { useCallback, useEffect, useState } from "react";

import { createWebApiClient } from "./client";

export type ResourceState<T> =
  | { data: null; error: null; status: "loading" }
  | { data: null; error: string; status: "error" }
  | { data: T; error: null; status: "ready" };

function resourceErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "你没有权限查看这项内容。";
    if (error.status === 404) return "这项内容不存在或已被删除。";
    if (error.status === 429) return "请求过于频繁，请稍后重试。";
    if (error.status >= 500) return "服务暂时不可用，已保存的数据不会丢失。";
  }
  return "加载失败，请检查网络后重试。";
}

export function useApiResource<T>(path: string): ResourceState<T> & { reload: () => void } {
  const [state, setState] = useState<ResourceState<T>>({
    data: null,
    error: null,
    status: "loading",
  });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => {
    setState({ data: null, error: null, status: "loading" });
    setAttempt((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    createWebApiClient().get<T>(path).then(
      (data) => {
        if (active) setState({ data, error: null, status: "ready" });
      },
      (error: unknown) => {
        if (active) {
          setState({ data: null, error: resourceErrorMessage(error), status: "error" });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [attempt, path]);

  return { ...state, reload };
}
