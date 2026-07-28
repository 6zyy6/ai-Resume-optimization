import { describe, expect, it } from "vitest";
import { ApiError, createApiClient, isInternalReturnTo } from "../src/index";

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

  it("supports every public HTTP method through the injected transport", async () => {
    const calls: Array<{ init: RequestInit; url: string }> = [];
    const client = createApiClient({
      baseUrl: "https://api.example.test/",
      request: async (url, init) => {
        calls.push({ init, url });
        return new Response(JSON.stringify({ ok: true }));
      },
    });

    await client.get("/facts");
    await client.patch("/facts/fct_1", { value: "更新" }, "patch_1");
    await client.put("/facts/fct_1", { value: "替换" }, "put_1");
    await client.delete("/facts/fct_1", "delete_1");

    expect(calls.map(({ init }) => init.method)).toEqual(["GET", "PATCH", "PUT", "DELETE"]);
    expect(calls.map(({ url }) => url)).toEqual([
      "https://api.example.test/facts",
      "https://api.example.test/facts/fct_1",
      "https://api.example.test/facts/fct_1",
      "https://api.example.test/facts/fct_1",
    ]);
    expect(new Headers(calls[0].init.headers).has("Idempotency-Key")).toBe(false);
    expect(new Headers(calls[1].init.headers).get("Idempotency-Key")).toBe("patch_1");
    expect(new Headers(calls[2].init.headers).get("Idempotency-Key")).toBe("put_1");
    expect(new Headers(calls[3].init.headers).get("Idempotency-Key")).toBe("delete_1");
  });

  it("throws the same typed error for every non-success response", async () => {
    const client = createApiClient({
      baseUrl: "https://api.example.test",
      request: async () =>
        new Response(
          JSON.stringify({
            error: {
              code: "VERSION_CONFLICT",
              details: { current_version: 4 },
              message: "版本已更新",
              request_id: "req_conflict",
            },
          }),
          { status: 409 },
        ),
    });

    const error = await client.get("/resumes/res_1").catch((failure: unknown) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: "VERSION_CONFLICT",
      details: { current_version: 4 },
      message: "版本已更新",
      requestId: "req_conflict",
      status: 409,
    });
  });

  it("accepts empty successful responses", async () => {
    const client = createApiClient({
      baseUrl: "https://api.example.test",
      request: async () => new Response(null, { status: 204 }),
    });

    await expect(client.delete("/facts/fct_1", "delete_1")).resolves.toBeUndefined();
  });
});
