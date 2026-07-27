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
                claims = [claim.strip() for claim in re.split(r"[;；]", bullet["text"]) if claim.strip()]
                if len(refs) < len(claims):
                    issues.append(QualityIssue("BULLET_CLAIM_NOT_COVERED", path, "Every atomic claim requires evidence"))
                evidence = " ".join(confirmed[reference] for reference in refs)
                numbers = re.findall(r"\d+(?:\.\d+)?%?", bullet["text"])
                if any(number not in evidence for number in numbers):
                    issues.append(QualityIssue("BULLET_NEW_NUMBER", path, "Bullet introduces an unsupported number"))
    return issues
