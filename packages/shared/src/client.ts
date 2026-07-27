export type FetchCompatible = (url: string, init: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  baseUrl: string;
  request: FetchCompatible;
}

export function createApiClient({ baseUrl, request }: ApiClientOptions) {
  return {
    async post<TBody, TResponse = unknown>(
      path: string,
      body: TBody,
      idempotencyKey: string,
    ): Promise<TResponse> {
      const headers = new Headers({ "Content-Type": "application/json" });
      headers.set("Idempotency-Key", idempotencyKey);
      headers.set("X-Trace-Id", `tr_${crypto.randomUUID()}`);
      const response = await request(`${baseUrl.replace(/\/$/, "")}${path}`, {
        body: JSON.stringify(body),
        headers,
        method: "POST",
      });
      return response.json() as Promise<TResponse>;
    },
  };
}

export function isInternalReturnTo(value: string): boolean {
  return value.startsWith("/") && !value.startsWith("//") && !value.includes("\\");
}
