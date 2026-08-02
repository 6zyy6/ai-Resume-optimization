from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.modules.resumes.quality import high_risk_terms, supports_high_risk_entities


@dataclass(frozen=True)
class DraftClaim:
    text: str
    fact_refs: tuple[str, ...]
    claim_order: int


@dataclass(frozen=True)
class ConfirmedFactProjection:
    id: str
    value: str
    status: str
    source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class SupportedClaim:
    text: str
    fact_refs: tuple[str, ...]
    claim_order: int
    start: int
    end: int


@dataclass(frozen=True)
class FactPolicyIssue:
    code: str
    claim_order: int
    message: str


@dataclass(frozen=True)
class FactPolicyResult:
    supported_claims: tuple[SupportedClaim, ...]
    issues: tuple[FactPolicyIssue, ...]


def fact_policy_check(
    text: str,
    claims: Iterable[DraftClaim],
    confirmed_fact_projection: Iterable[ConfirmedFactProjection],
) -> FactPolicyResult:
    facts = {fact.id: fact for fact in confirmed_fact_projection}
    supported: list[SupportedClaim] = []
    issues: list[FactPolicyIssue] = []
    cursor = 0
    seen_orders: set[int] = set()

    for claim in sorted(claims, key=lambda item: item.claim_order):
        if claim.claim_order in seen_orders:
            issues.append(
                FactPolicyIssue(
                    "CLAIM_ORDER_DUPLICATE",
                    claim.claim_order,
                    "Atomic claim order must be unique",
                )
            )
            continue
        seen_orders.add(claim.claim_order)

        start = text.find(claim.text, cursor)
        if not claim.text or start < 0:
            issues.append(
                FactPolicyIssue(
                    "CLAIM_RANGE_INVALID",
                    claim.claim_order,
                    "Atomic claim text must map to an exact non-overlapping range",
                )
            )
            continue
        end = start + len(claim.text)
        cursor = end

        refs = tuple(dict.fromkeys(claim.fact_refs))
        if not refs:
            issues.append(
                FactPolicyIssue(
                    "CLAIM_FACT_REFERENCE_REQUIRED",
                    claim.claim_order,
                    "Atomic claim requires at least one fact reference",
                )
            )
            continue
        referenced = [facts.get(fact_id) for fact_id in refs]
        if any(fact is None for fact in referenced):
            issues.append(
                FactPolicyIssue(
                    "CLAIM_FACT_UNKNOWN",
                    claim.claim_order,
                    "Atomic claim references an unknown fact",
                )
            )
            continue
        evidence_facts = [fact for fact in referenced if fact is not None]
        if any(fact.status != "confirmed" for fact in evidence_facts):
            issues.append(
                FactPolicyIssue(
                    "CLAIM_FACT_NOT_CONFIRMED",
                    claim.claim_order,
                    "Atomic claim references a fact that is not confirmed",
                )
            )
            continue
        if any(not fact.source_hashes for fact in evidence_facts):
            issues.append(
                FactPolicyIssue(
                    "CLAIM_FACT_SOURCE_REQUIRED",
                    claim.claim_order,
                    "Atomic claim references a fact without source evidence",
                )
            )
            continue

        evidence = " ".join(fact.value for fact in evidence_facts)
        claim_terms = high_risk_terms(claim.text)
        exact_match = any(
            claim.text.strip().casefold() == fact.value.strip().casefold()
            for fact in evidence_facts
        )
        if not exact_match and (
            not supports_high_risk_entities(claim.text, evidence)
            or (claim_terms and not claim_terms <= high_risk_terms(evidence))
            or not claim_terms
        ):
            issues.append(
                FactPolicyIssue(
                    "CLAIM_FACT_MISMATCH",
                    claim.claim_order,
                    "Atomic claim is not supported by its fact evidence",
                )
            )
            continue

        supported.append(
            SupportedClaim(
                text=claim.text,
                fact_refs=refs,
                claim_order=claim.claim_order,
                start=start,
                end=end,
            )
        )

    return FactPolicyResult(tuple(supported), tuple(issues))
