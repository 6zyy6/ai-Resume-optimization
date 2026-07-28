import { describe, expect, it } from "vitest";
import { createWebApiClient } from "../features/api/client";

describe("Web API client", () => {
  it("routes requests through an injected fixture transport", async () => {
    const calls: string[] = [];
    const client = createWebApiClient({
      baseUrl: "fixture://resume-api",
      transport: async (url) => {
        calls.push(url);
        return new Response(JSON.stringify({ id: "res_fixture" }));
      },
    });

    await expect(client.get<{ id: string }>("/v1/resumes/res_fixture")).resolves.toEqual({
      id: "res_fixture",
    });
    expect(calls).toEqual(["fixture://resume-api/v1/resumes/res_fixture"]);
  });
});
