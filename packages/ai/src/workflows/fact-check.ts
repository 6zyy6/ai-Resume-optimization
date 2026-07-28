import type {
  ConfirmedFact,
  FactCheckOutput,
} from "../contracts.js";

const RISK_RULES = [
  {
    flag: "unsupported_role",
    pattern: /(?:担任|任职|晋升为|负责人|总监|经理|主管|leader)/i,
  },
  {
    flag: "unsupported_tool",
    pattern:
      /(?:Kubernetes|Docker|SQL|Python|Java|React|Vue|Figma|Tableau|Power\s?BI)/i,
  },
  {
    flag: "unsupported_award",
    pattern: /(?:获奖|冠军|金奖|银奖|一等奖|二等奖|award)/i,
  },
  {
    flag: "absolute_claim",
    pattern: /(?:全部|完全|彻底|从不|始终|百分之百|100%)/i,
  },
] as const;

function normalize(value: string): string {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s，,。.;；:：、"'“”‘’()（）]/g, "");
}

function numbers(value: string): string[] {
  return value.match(/\d+(?:\.\d+)?%?/g) ?? [];
}

function isDirectlySupported(claim: string, fact: ConfirmedFact): boolean {
  const normalizedClaim = normalize(claim);
  const normalizedFact = normalize(fact.value);
  if (
    !normalizedClaim ||
    (!normalizedFact.includes(normalizedClaim) &&
      !normalizedClaim.includes(normalizedFact))
  ) {
    return false;
  }
  return numbers(claim).every((value) => numbers(fact.value).includes(value));
}

function isRelated(claim: string, fact: ConfirmedFact): boolean {
  const normalizedClaim = normalize(claim);
  const normalizedFact = normalize(fact.value);
  for (let index = 0; index < normalizedClaim.length - 1; index += 1) {
    if (normalizedFact.includes(normalizedClaim.slice(index, index + 2))) {
      return true;
    }
  }
  return false;
}

export function factCheck(
  text: string,
  facts: ConfirmedFact[],
): FactCheckOutput {
  const confirmedFacts = facts.filter(({ status }) => status === "confirmed");
  const atomicClaims = text
    .split(/[。；;\n]+/)
    .map((claim) => claim.trim())
    .filter(Boolean);
  const riskFlags = new Set<string>();

  const claims = atomicClaims.map((claim) => {
    const directFacts = confirmedFacts.filter((fact) =>
      isDirectlySupported(claim, fact),
    );
    if (directFacts.length > 0) {
      return {
        text: claim,
        fact_refs: directFacts.map(({ id }) => id),
        status: "supported" as const,
      };
    }

    const relatedFacts = confirmedFacts.filter((fact) =>
      isRelated(claim, fact),
    );
    const claimRiskFlags: string[] = RISK_RULES
      .filter(({ pattern }) => pattern.test(claim))
      .map(({ flag }) => flag);
    if (
      numbers(claim).some(
        (value) =>
          !confirmedFacts.some((fact) => numbers(fact.value).includes(value)),
      )
    ) {
      claimRiskFlags.push("unsupported_numeric");
    }
    for (const flag of claimRiskFlags) {
      riskFlags.add(flag);
    }

    return {
      text: claim,
      fact_refs: relatedFacts.map(({ id }) => id),
      status:
        claimRiskFlags.length > 0 || relatedFacts.length === 0
          ? "unsupported" as const
          : "needs_confirmation" as const,
    };
  });

  return {
    claims,
    exportable:
      claims.length > 0 &&
      claims.every(({ status }) => status === "supported"),
    risk_flags: [...riskFlags],
  };
}
