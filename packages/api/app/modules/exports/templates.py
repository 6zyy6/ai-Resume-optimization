from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)
from pypdf import PdfReader


ASSET_DIR = Path(__file__).with_name("assets")
FONT_PATH = ASSET_DIR / "NotoSansSC-Regular.ttf"
FONT_NAME = "NotoSansSC"
TEMPLATES = {"clear-standard", "modern-whitespace"}


class ExportBlocked(ValueError):
    pass


@dataclass(frozen=True)
class RenderedExport:
    pdf: bytes
    snapshot_hash: str
    template_version: str
    download_expires_in: int = 600


def content_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def sanitize_download_name(value: str) -> str:
    stem = Path(value.replace("\\", "/")).name
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w\u3400-\u9fff-]+", "_", stem, flags=re.UNICODE)
    stem = re.sub(r"_+", "_", stem).strip("._-")[:80] or "resume"
    return f"{stem}.pdf"


def canonical_resume_text(snapshot: dict[str, Any]) -> str:
    lines = [str(snapshot.get("title", "")).strip()]
    if snapshot.get("target"):
        lines.append(str(snapshot["target"]).strip())
    for section in snapshot.get("sections", []):
        if section.get("title"):
            lines.append(str(section["title"]).strip())
        lines.extend(
            str(item["text"]).strip()
            for item in section.get("items", [])
            if item.get("text")
        )
    return "\n".join(line for line in lines if line)


def normalized_pdf_text(pdf: bytes) -> str:
    lines: list[str] = []
    for page_number, page in enumerate(PdfReader(BytesIO(pdf)).pages, start=1):
        page_lines = (page.extract_text() or "").splitlines()
        page_number_removed = False
        for raw_line in page_lines:
            line = raw_line.strip()
            if not line:
                continue
            if not page_number_removed and line == str(page_number):
                page_number_removed = True
                continue
            page_number_removed = True
            lines.append(line.removeprefix("• ").strip())
    return "\n".join(lines)


def render_resume_pdf(
    snapshot: dict[str, Any],
    template_version: str,
) -> RenderedExport:
    if template_version not in TEMPLATES:
        raise ExportBlocked("VALIDATION_FAILED: unknown template")
    _assert_exportable(snapshot)
    _register_font()
    output = BytesIO()
    modern = template_version == "modern-whitespace"
    margin = 24 * mm if modern else 18 * mm
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=snapshot.get("title", "Resume"),
        author="AI Resume Assistant",
        creator="AI Resume Assistant",
        pageCompression=1,
    )
    story = _story(snapshot, modern=modern)
    document.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return RenderedExport(
        pdf=output.getvalue(),
        snapshot_hash=content_hash(snapshot),
        template_version=template_version,
    )


def _register_font() -> None:
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        if not FONT_PATH.is_file():
            raise RuntimeError("Bundled Noto Sans SC font is missing")
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))


def _story(snapshot: dict[str, Any], *, modern: bool) -> list:
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ResumeTitle",
        parent=styles["Title"],
        fontName=FONT_NAME,
        fontSize=22 if modern else 20,
        leading=28,
        alignment=TA_CENTER if not modern else 0,
        textColor=HexColor("#132238"),
        spaceAfter=6 * mm,
    )
    target = ParagraphStyle(
        "ResumeTarget",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=10,
        leading=15,
        textColor=HexColor("#52606D"),
        alignment=title.alignment,
        spaceAfter=7 * mm,
    )
    section = ParagraphStyle(
        "ResumeSection",
        parent=styles["Heading2"],
        fontName=FONT_NAME,
        fontSize=13,
        leading=18,
        textColor=HexColor("#1F5A55" if modern else "#1F4B7A"),
        spaceBefore=4 * mm,
        spaceAfter=2 * mm,
        keepWithNext=True,
    )
    bullet = ParagraphStyle(
        "ResumeBullet",
        parent=styles["BodyText"],
        fontName=FONT_NAME,
        fontSize=10.5,
        leading=16 if modern else 15,
        textColor=HexColor("#1D2939"),
        leftIndent=4 * mm,
        firstLineIndent=-3 * mm,
        spaceAfter=1.8 * mm,
        splitLongWords=False,
    )
    story: list = [Paragraph(escape(snapshot.get("title", "")), title)]
    if snapshot.get("target"):
        story.append(Paragraph(escape(str(snapshot["target"])), target))
    for item in snapshot.get("sections", []):
        section_title = Paragraph(escape(str(item.get("title", ""))), section)
        bullets = [
            Paragraph(f"• {escape(str(bullet_item.get('text', '')))}", bullet)
            for bullet_item in item.get("items", [])
            if bullet_item.get("text")
        ]
        if bullets:
            story.append(KeepTogether([section_title, bullets[0]]))
            story.extend(bullets[1:])
        else:
            story.append(section_title)
        story.append(Spacer(1, 1.5 * mm))
    return story


def _assert_exportable(snapshot: dict[str, Any]) -> None:
    for section in snapshot.get("sections", []):
        for item in section.get("items", []):
            flags = set(item.get("risk_flags", []))
            if flags & {"needs_confirmation", "unsupported"}:
                raise ExportBlocked(
                    "EXPORT_BLOCKED_BY_FACTS: pending or unsupported claim"
                )
            if item.get("text") and not item.get("fact_refs"):
                raise ExportBlocked(
                    "EXPORT_BLOCKED_BY_FACTS: every bullet requires evidence"
                )


def _page_number(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont(FONT_NAME, 8)
    canvas.setFillColor(HexColor("#98A2B3"))
    canvas.drawRightString(A4[0] - document.rightMargin, 9 * mm, str(document.page))
    canvas.restoreState()
