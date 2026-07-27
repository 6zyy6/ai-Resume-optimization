import { describe, expect, it } from "vitest";
import { createApiClient, isInternalReturnTo } from "../src/index";

describe("shared client", () => {
  it("rejects an external return URL", () => {
    expect(isInternalReturnTo("/resumes")).toBe(true);
    expect(isInternalReturnTo("https://evil.example")).toBe(false);
    expect(isInternalReturnTo("//evil.example")).toBe(false);
    expect(isInternalReturnTo("javascript:alert(1)")).toBe(false);
  });

  it("adds trace and idempotency headers to writes", async () => {
    const calls: RequestInit[] = [];
    const client = createApiClient({
      baseUrl: "https://api.example.test/v1",
      request: async (_url, init) => {
        calls.push(init);
        return new Response(JSON.stringify({ request_id: "req_1" }), { status: 201 });
      },
    });
    await client.post("/facts", { kind: "action", value: "完成调研" }, "idem_1");
    expect(new Headers(calls[0].headers).get("Idempotency-Key")).toBe("idem_1");
    expect(new Headers(calls[0].headers).get("X-Trace-Id")).toMatch(/^tr_/);
  });
});
