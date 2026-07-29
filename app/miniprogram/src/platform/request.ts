import Taro from "@tarojs/taro";
import { createApiClient, type ApiTransport } from "@resume/shared/client-runtime";

const API_BASE_URL = process.env.TARO_APP_API_BASE_URL ?? "http://localhost:8000";

type HeaderMap = Record<string, string>;

function responseHeaders(headers: HeaderMap) {
  return {
    get(name: string) {
      const key = Object.keys(headers).find((candidate) => candidate.toLowerCase() === name.toLowerCase());
      return key ? headers[key] : null;
    },
  };
}

export const taroTransport: ApiTransport = async (url, init) => {
  const headers = { ...(init.headers as HeaderMap | undefined) };
  const result = await Taro.request({
    url,
    method: init.method as "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
    data: init.body ? JSON.parse(String(init.body)) : undefined,
    header: headers,
    credentials: "include",
  });
  return {
    ok: result.statusCode >= 200 && result.statusCode < 300,
    status: result.statusCode,
    statusText: String(result.errMsg ?? ""),
    headers: responseHeaders(result.header as HeaderMap),
    json: async () => result.data,
  } as Response;
};

export const api = createApiClient({ baseUrl: API_BASE_URL, request: taroTransport });

export function newIdempotencyKey(scope: string): string {
  return `${scope}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

export function write<TBody, TResponse>(
  method: "post" | "patch" | "put",
  path: string,
  body: TBody,
  operationKey: string,
): Promise<TResponse> {
  return api[method]<TBody, TResponse>(path, body, operationKey);
}
