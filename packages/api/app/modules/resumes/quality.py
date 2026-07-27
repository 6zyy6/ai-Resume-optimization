from dataclasses import dataclass
import re
from typing import Any, Iterable


@dataclass(frozen=True)
class QualityIssue:
    code: str
    path: str
    message: str


def check_exportable(snapshot: dict[str, Any], facts: Iterable[Any]) -> list[QualityIssue]:
    confirmed = {
        (fact.id if hasattr(fact, "id") else fact["id"]): (
            fact.value_encrypted if hasattr(fact, "value_encrypted") else fact.get("value", "")
        )
        for fact in facts
        if (fact.status if hasattr(fact, "status") else fact["status"]) == "confirmed"
    }
    issues: list[QualityIssue] = []
    for section_index, section in enumerate(snapshot.get("sections", [])):
        for bullet_index, bullet in enumerate(section.get("items", [])):
            if not bullet.get("text"):
                continue
            refs = bullet.get("fact_refs", [])
            path = f"sections.{section_index}.items.{bullet_index}"
            if not refs:
                issues.append(QualityIssue("BULLET_FACT_REFERENCE_REQUIRED", path, "Every bullet claim requires a confirmed fact"))
                continue
            claims = _claims(bullet["text"])
            if len(refs) != len(claims) or len(set(refs)) != len(refs):
                issues.append(QualityIssue("BULLET_FACT_CARDINALITY_MISMATCH", path, "Each atomic claim requires exactly one fact reference"))
                continue
            if any(reference not in confirmed for reference in refs):
                issues.append(QualityIssue("BULLET_FACT_NOT_CONFIRMED", path, "Bullet references an unconfirmed fact"))
                continue
            for index, claim in enumerate(claims):
                evidence = confirmed[refs[index]]
                claim_numbers = _numbers(claim[0])
                evidence_numbers = _numbers(evidence)
                if claim_numbers - evidence_numbers:
                    issues.append(QualityIssue("BULLET_NEW_NUMBER", f"{path}.claims.{index}", "Bullet introduces an unsupported number"))
                elif not _shares_textual_evidence(claim[0], evidence):
                    issues.append(QualityIssue("BULLET_CLAIM_NOT_COVERED", f"{path}.claims.{index}", "Bullet claim is unrelated to its fact"))
    return issues


def claim_ranges(text: str) -> list[tuple[int, int]]:
    return [(start, end) for _, start, end in _claims(text)]


def _claims(text: str) -> list[tuple[str, int, int]]:
    claims: list[tuple[str, int, int]] = []
    start = 0
    for match in re.finditer(r"[;；，。]|(?<!\d)[,.]|[,.](?!\d)", text):
        raw = text[start:match.start()]
        segment = raw.strip()
        if segment:
            offset = start + len(raw) - len(raw.lstrip())
            claims.append((segment, offset, start + len(raw.rstrip())))
        start = match.end()
    raw = text[start:]
    segment = raw.strip()
    if segment:
        offset = start + len(raw) - len(raw.lstrip())
        claims.append((segment, offset, start + len(raw.rstrip())))
    return claims


def _numbers(text: str) -> set[str]:
    tokens = re.findall(
        r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\d.,])",
        text,
    )
    return {token.replace(",", "") for token in tokens}


def _shares_textual_evidence(claim: str, evidence: str) -> bool:
    claim_terms = {term.lower() for term in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", claim)}
    evidence_terms = {term.lower() for term in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", evidence)}
    return bool(claim_terms & evidence_terms)
