import { describe, expect, it } from "vitest";

import { MODEL_WORKFLOW_TYPES } from "../src/contracts.js";
import { createModelRouterFromEnv } from "../src/model-router.js";

describe("model routing from environment", () => {
  it("routes every text workflow to the configured DeepSeek models", () => {
    const router = createModelRouterFromEnv({
      AI_TEXT_PROVIDER: "deepseek",
      AI_TEXT_MODEL: "deepseek-v4-flash",
      AI_TEXT_FALLBACK_MODEL: "deepseek-v4-pro",
      DEEPSEEK_API_KEY: "test-key",
    });

    expect(router.isReady({
      DEEPSEEK_API_KEY: "test-key",
    })).toBe(true);
    for (const workflowType of MODEL_WORKFLOW_TYPES) {
      expect(router.getRoute(workflowType)?.primary).toEqual({
        provider: "deepseek",
        model: "deepseek-v4-flash",
        approved_data_policy: true,
      });
      expect(router.getRoute(workflowType)?.fallback).toEqual({
        provider: "deepseek",
        model: "deepseek-v4-pro",
        approved_data_policy: true,
      });
      expect(router.getRoute(workflowType)?.enabled).toBe(true);
    }
  });

  it("does not report ready when the configured provider key is absent", () => {
    const router = createModelRouterFromEnv({
      AI_TEXT_PROVIDER: "deepseek",
      AI_TEXT_MODEL: "deepseek-v4-flash",
    });

    expect(router.isReady({})).toBe(false);
  });
});
