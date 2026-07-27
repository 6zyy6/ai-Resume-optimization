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
            elif any(reference not in confirmed for reference in refs):
                issues.append(QualityIssue("BULLET_FACT_NOT_CONFIRMED", path, "Bullet references an unconfirmed fact"))
            else:
                claims = _claims(bullet["text"])
                if len(refs) < len(claims):
                    issues.append(QualityIssue("BULLET_CLAIM_NOT_COVERED", path, "Every atomic claim requires evidence"))
                for index, claim in enumerate(claims):
                    if index >= len(refs):
                        continue
                    evidence = confirmed[refs[index]]
                    claim_numbers = set(re.findall(r"(?<!\d)\d+(?:\.\d+)?%?(?!\d)", claim[0]))
                    evidence_numbers = set(re.findall(r"(?<!\d)\d+(?:\.\d+)?%?(?!\d)", evidence))
                    if claim_numbers - evidence_numbers:
                        issues.append(QualityIssue("BULLET_NEW_NUMBER", f"{path}.claims.{index}", "Bullet introduces an unsupported number"))
                    elif not claim_numbers and not _shares_textual_evidence(claim[0], evidence):
                        issues.append(QualityIssue("BULLET_CLAIM_NOT_COVERED", f"{path}.claims.{index}", "Bullet claim is unrelated to its fact"))
    return issues


def claim_ranges(text: str) -> list[tuple[int, int]]:
    return [(start, end) for _, start, end in _claims(text)]


def _claims(text: str) -> list[tuple[str, int, int]]:
    claims: list[tuple[str, int, int]] = []
    start = 0
    for match in re.finditer(r"[;；,，.。]", text):
        segment = text[start:match.start()].strip()
        if segment:
            offset = start + len(text[start:match.start()]) - len(text[start:match.start()].lstrip())
            claims.append((segment, offset, match.start()))
        start = match.end()
    segment = text[start:].strip()
    if segment:
        offset = start + len(text[start:].rstrip()) - len(text[start:].strip())
        claims.append((segment, offset, len(text)))
    return claims


def _shares_textual_evidence(claim: str, evidence: str) -> bool:
    claim_terms = {term.lower() for term in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", claim)}
    evidence_terms = {term.lower() for term in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", evidence)}
    return bool(claim_terms & evidence_terms)
