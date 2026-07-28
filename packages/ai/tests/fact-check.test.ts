import { describe, expect, it } from "vitest";

import { factCheck } from "../src/workflows/fact-check.js";

describe("factCheck", () => {
  it("blocks a number not present in confirmed facts", () => {
    const result = factCheck(
      "将转化率提升 30%",
      [
        {
          id: "fact_1",
          kind: "result",
          value: "改进了转化流程",
          status: "confirmed",
        },
      ],
    );

    expect(result.exportable).toBe(false);
    expect(
      result.claims.some((claim) => claim.status === "unsupported"),
    ).toBe(true);
    expect(result.risk_flags).toContain("unsupported_numeric");
  });

  it("allows an atomic numeric claim with an exact confirmed source", () => {
    const result = factCheck(
      "将转化率提升 30%",
      [
        {
          id: "fact_1",
          kind: "result",
          value: "通过优化漏斗，将转化率提升 30%",
          status: "confirmed",
        },
      ],
    );

    expect(result).toEqual({
      claims: [
        {
          text: "将转化率提升 30%",
          fact_refs: ["fact_1"],
          status: "supported",
        },
      ],
      exportable: true,
      risk_flags: [],
    });
  });

  it("blocks unsupported tools, roles, awards and absolute results", () => {
    const result = factCheck(
      "担任负责人；使用 Kubernetes；获得全国冠军；彻底解决全部故障",
      [
        {
          id: "fact_1",
          kind: "action",
          value: "参与服务稳定性改进",
          status: "confirmed",
        },
      ],
    );

    expect(result.exportable).toBe(false);
    expect(result.claims.every(({ status }) => status !== "supported")).toBe(
      true,
    );
    expect(result.risk_flags).toEqual(
      expect.arrayContaining([
        "unsupported_role",
        "unsupported_tool",
        "unsupported_award",
        "absolute_claim",
      ]),
    );
  });

  it("marks related but insufficient evidence as needs_confirmation", () => {
    const result = factCheck(
      "负责支付产品的整体战略",
      [
        {
          id: "fact_1",
          kind: "action",
          value: "参与支付产品需求分析",
          status: "confirmed",
        },
      ],
    );

    expect(result.exportable).toBe(false);
    expect(result.claims[0]?.status).toBe("needs_confirmation");
  });
});
