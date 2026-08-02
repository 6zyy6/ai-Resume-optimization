from dataclasses import dataclass
import re
from typing import Any, Iterable


_ENGLISH_STOPWORDS = {
    "achieved",
    "and",
    "created",
    "delivered",
    "developed",
    "enhanced",
    "for",
    "from",
    "improved",
    "increased",
    "into",
    "managed",
    "optimized",
    "processed",
    "reduced",
    "resolved",
    "handled",
    "supported",
    "the",
    "through",
    "using",
    "via",
    "with",
}
_CHINESE_STOPWORDS = {
    "将",
    "同比",
    "以及",
    "使用",
    "优化",
    "减少",
    "协助",
    "参与",
    "完成",
    "实现",
    "支持",
    "改进",
    "提升",
    "提高",
    "推动",
    "管理",
    "负责",
    "进行",
    "通过",
    "降低",
}

_RESPONSIBILITY_LEVELS = (
    (
        2,
        (
            r"\b(?:drove|led|managed|owned|oversaw)\b",
            r"\b(?:was\s+)?responsible\s+for\b",
            r"主导|牵头|推动|管理|负责",
        ),
    ),
    (
        1,
        (
            r"\b(?:assisted|contributed|helped|participated|supported)\b",
            r"协助|参与|支持",
        ),
    ),
)
_RESPONSIBILITY_MARKERS = tuple(
    pattern
    for _, patterns in _RESPONSIBILITY_LEVELS
    for pattern in patterns
)


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
                elif not responsibility_claim_supported(claim[0], [evidence]):
                    issues.append(
                        QualityIssue(
                            "BULLET_RESPONSIBILITY_STRENGTH_UNSUPPORTED",
                            f"{path}.claims.{index}",
                            "Bullet claims stronger responsibility than its fact evidence",
                        )
                    )
                elif not supports_high_risk_entities(claim[0], evidence) or (
                    not _numbers(claim[0])
                    and not _high_risk_terms(claim[0]) <= _high_risk_terms(evidence)
                ):
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


def supports_high_risk_entities(claim: str, evidence: str) -> bool:
    claim_numbers = _numbers(claim)
    if claim_numbers - _numbers(evidence):
        return False
    if not claim_numbers:
        return True
    claim_signatures = _numeric_signatures(claim)
    evidence_signatures = _numeric_signatures(evidence)
    return all(
        claim_signatures.get(number)
        and claim_signatures[number] & evidence_signatures.get(number, set())
        for number in claim_numbers
    )


def high_risk_terms(text: str) -> set[str]:
    return _high_risk_terms(text)


def responsibility_strength(text: str) -> int:
    normalized = text.casefold()
    return max(
        (
            level
            for level, patterns in _RESPONSIBILITY_LEVELS
            if any(re.search(pattern, normalized) for pattern in patterns)
        ),
        default=0,
    )


def responsibility_equivalent(left: str, right: str) -> bool:
    return any(
        _responsibility_subject_equivalent(left_subject, right_subject)
        for left_subject, _ in _responsibility_fragments(left)
        for right_subject, _ in _responsibility_fragments(right)
    )


def responsibility_claim_supported(
    claim: str,
    evidence_values: Iterable[str],
) -> bool:
    claim_fragments = _responsibility_fragments(claim)
    responsibility_fragments = [
        (subject, strength)
        for subject, strength in claim_fragments
        if strength > 0
    ]
    if not responsibility_fragments:
        return True
    evidence_fragments = [
        fragment
        for evidence in evidence_values
        for fragment in _responsibility_fragments(evidence)
    ]
    for subject, strength in responsibility_fragments:
        equivalent_strengths = [
            evidence_strength
            for evidence_subject, evidence_strength in evidence_fragments
            if _responsibility_subject_equivalent(subject, evidence_subject)
        ]
        if equivalent_strengths and max(equivalent_strengths) < strength:
            return False
    return True


def _responsibility_fragments(text: str) -> list[tuple[str, int]]:
    fragments: list[tuple[str, int]] = []
    inherited_strength = 0
    for raw in re.split(
        r"[、，,;；。/&＆／]|\s+and\s+|以及",
        text.casefold(),
    ):
        local_strength = responsibility_strength(raw)
        if local_strength:
            inherited_strength = local_strength
        subject = _without_responsibility_markers(raw).strip()
        if not subject:
            continue
        fragments.append((subject, local_strength or inherited_strength))
    return fragments


def _responsibility_subject_equivalent(left: str, right: str) -> bool:
    left_terms = _high_risk_terms(left)
    right_terms = _high_risk_terms(right)
    return bool(
        left_terms
        and right_terms
        and (left_terms <= right_terms or right_terms <= left_terms)
    )


def _without_responsibility_markers(text: str) -> str:
    normalized = text.casefold()
    for marker in _RESPONSIBILITY_MARKERS:
        normalized = re.sub(marker, " ", normalized)
    return " ".join(normalized.split())


_NUMBER_TOKEN = r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\d.,])"
_ENGLISH_DIRECTION = {
    "boost": "up",
    "boosted": "up",
    "decrease": "down",
    "decreased": "down",
    "drop": "down",
    "dropped": "down",
    "grew": "up",
    "grow": "up",
    "growth": "up",
    "improve": "up",
    "improved": "up",
    "increase": "up",
    "increased": "up",
    "lower": "down",
    "lowered": "down",
    "reduce": "down",
    "reduced": "down",
}
_ENGLISH_DIRECTION_PATTERN = "|".join(_ENGLISH_DIRECTION)
_CHINESE_DIRECTION = {
    "下降": "down",
    "减少": "down",
    "增长": "up",
    "提高": "up",
    "提升": "up",
    "降低": "down",
}


def _numeric_signatures(text: str) -> dict[str, set[tuple[str, str]]]:
    signatures: dict[str, set[tuple[str, str]]] = {}
    covered: set[tuple[int, int]] = set()
    english_patterns = (
        rf"(?P<verb>{_ENGLISH_DIRECTION_PATTERN})\s+"
        rf"(?P<subject>[A-Za-z][A-Za-z -]*?)\s+(?:by|to|with)\s*"
        rf"(?P<number>{_NUMBER_TOKEN})",
        rf"(?P<subject>[A-Za-z][A-Za-z -]*?)\s+"
        rf"(?P<verb>{_ENGLISH_DIRECTION_PATTERN})\s+(?:by|to|with)\s*"
        rf"(?P<number>{_NUMBER_TOKEN})",
    )
    for pattern in english_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            number = match["number"].replace(",", "")
            subject = " ".join(match["subject"].lower().split())
            direction = _ENGLISH_DIRECTION[match["verb"].lower()]
            signatures.setdefault(number, set()).add((direction, subject))
            covered.add(match.span("number"))
    chinese_patterns = (
        rf"(?:将)?(?P<subject>[\u4e00-\u9fff]+?)(?:同比)?"
        rf"(?P<verb>{'|'.join(_CHINESE_DIRECTION)})"
        rf"(?P<number>{_NUMBER_TOKEN})",
        rf"(?P<verb>{'|'.join(_CHINESE_DIRECTION)})"
        rf"(?P<subject>[\u4e00-\u9fff]+?)"
        rf"(?P<number>{_NUMBER_TOKEN})",
    )
    for pattern in chinese_patterns:
        for match in re.finditer(pattern, text):
            number = match["number"].replace(",", "")
            subject = match["subject"].removeprefix("将").removeprefix("同比")
            direction = _CHINESE_DIRECTION[match["verb"]]
            signatures.setdefault(number, set()).add((direction, subject))
            covered.add(match.span("number"))
    for match in re.finditer(_NUMBER_TOKEN, text):
        if match.span() in covered:
            continue
        number = match.group().replace(",", "")
        after = re.match(r"\s*([A-Za-z]+(?:\s+[A-Za-z]+){0,2})", text[match.end() :])
        if after:
            signatures.setdefault(number, set()).add(
                ("quantity", after.group(1).lower().split()[-1])
            )
            continue
        before = re.search(r"([A-Za-z]+)\s*(?:by|to|with)?\s*$", text[: match.start()], re.IGNORECASE)
        if before:
            signatures.setdefault(number, set()).add(("quantity", before.group(1).lower()))
            continue
        chinese = re.search(r"([\u4e00-\u9fff]+)$", text[: match.start()])
        if chinese:
            signatures.setdefault(number, set()).add(("quantity", chinese.group(1)))
    return signatures


def _high_risk_terms(text: str) -> set[str]:
    english = {
        term
        for raw in re.findall(r"[A-Za-z]+", text)
        if len(term := raw.lower()) >= 3 and term not in _ENGLISH_STOPWORDS
    }
    chinese: set[str] = set()
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        for stopword in _CHINESE_STOPWORDS:
            span = span.replace(stopword, "")
        chinese.update(span[index : index + 2] for index in range(len(span) - 1))
    return english | chinese
