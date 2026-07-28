export type ApiTransport = (url: string, init: RequestInit) => Promise<Response>;
export type FetchCompatible = ApiTransport;

export interface ApiClientOptions {
  baseUrl: string;
  request: ApiTransport;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
  details: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string;
  readonly status: number;

  constructor(status: number, error: ApiErrorBody) {
    super(error.message);
    this.name = "ApiError";
    this.status = status;
    this.code = error.code;
    this.requestId = error.request_id;
    this.details = error.details;
  }
}

type Body = Record<string, unknown> | readonly unknown[] | unknown;

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (!value || typeof value !== "object") return false;
  const error = value as Partial<ApiErrorBody>;
  return (
    typeof error.code === "string" &&
    typeof error.message === "string" &&
    typeof error.request_id === "string" &&
    !!error.details &&
    typeof error.details === "object"
  );
}

export function createApiClient({ baseUrl, request }: ApiClientOptions) {
  const root = baseUrl.replace(/\/$/, "");

  async function send<TResponse>(
    method: string,
    path: string,
    body?: Body,
    idempotencyKey?: string,
  ): Promise<TResponse> {
    const headers = new Headers();
    headers.set("X-Trace-Id", `tr_${crypto.randomUUID()}`);
    if (body !== undefined) headers.set("Content-Type", "application/json");
    if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);

    const response = await request(`${root}${path}`, {
      body: body === undefined ? undefined : JSON.stringify(body),
      headers,
      method,
    });
    const payload = response.status === 204 ? undefined : await response.json();
    if (!response.ok) {
      const candidate =
        payload && typeof payload === "object" && "error" in payload
          ? (payload as { error?: unknown }).error
          : undefined;
      const error = isApiErrorBody(candidate)
        ? candidate
        : {
            code: "HTTP_ERROR",
            details: {},
            message: response.statusText || `Request failed with status ${response.status}`,
            request_id: response.headers.get("X-Request-Id") ?? "",
          };
      throw new ApiError(response.status, error);
    }
    return payload as TResponse;
  }

  return {
    get<TResponse = unknown>(path: string): Promise<TResponse> {
      return send("GET", path);
    },
    post<TBody, TResponse = unknown>(
      path: string,
      body: TBody,
      idempotencyKey: string,
    ): Promise<TResponse> {
      return send("POST", path, body, idempotencyKey);
    },
    patch<TBody, TResponse = unknown>(
      path: string,
      body: TBody,
      idempotencyKey: string,
    ): Promise<TResponse> {
      return send("PATCH", path, body, idempotencyKey);
    },
    put<TBody, TResponse = unknown>(
      path: string,
      body: TBody,
      idempotencyKey: string,
    ): Promise<TResponse> {
      return send("PUT", path, body, idempotencyKey);
    },
    delete<TResponse = unknown>(path: string, idempotencyKey: string): Promise<TResponse> {
      return send("DELETE", path, undefined, idempotencyKey);
    },
  };
}

export function isInternalReturnTo(value: string): boolean {
  return value.startsWith("/") && !value.startsWith("//") && !value.includes("\\");
}
