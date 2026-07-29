import { createApiClient, type ApiTransport } from "@resume/shared/client";

export interface WebApiClientOptions {
  baseUrl?: string;
  transport?: ApiTransport;
}

export function apiBrowserUrl(url: string): string {
  return url.startsWith("/v1/") ? `/api${url}` : url;
}

export function createWebApiClient({
  baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api",
  transport = globalThis.fetch.bind(globalThis),
}: WebApiClientOptions = {}) {
  return createApiClient({ baseUrl, request: transport });
}
