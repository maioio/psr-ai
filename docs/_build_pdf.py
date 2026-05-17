"""Build ARCHITECTURE_AND_COMMANDS.pdf from the markdown source.

Uses ReportLab Platypus - no native library dependencies.
Run: python docs/_build_pdf.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

BLUE_DARK   = colors.HexColor("#0969da")
BLUE_MID    = colors.HexColor("#0550ae")
GREY_LIGHT  = colors.HexColor("#f6f8fa")
GREY_BORDER = colors.HexColor("#d0d7de")
GREY_TEXT   = colors.HexColor("#57606a")
BLACK       = colors.HexColor("#1a1a1a")
WHITE       = colors.white
CODE_FG     = colors.HexColor("#24292f")
TH_BG       = colors.HexColor("#0969da")
TR_EVEN     = colors.HexColor("#f6f8fa")


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _build_styles():
    base = getSampleStyleSheet()

    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    normal_font   = "Helvetica"
    bold_font     = "Helvetica-Bold"
    code_font     = "Courier"

    return {
        "title": S("psrai_title",
            fontName=bold_font, fontSize=22, leading=28,
            textColor=BLACK, spaceAfter=6, spaceBefore=0,
            alignment=TA_LEFT,
        ),
        "subtitle": S("psrai_subtitle",
            fontName=normal_font, fontSize=10, leading=14,
            textColor=GREY_TEXT, spaceAfter=18,
        ),
        "h2": S("psrai_h2",
            fontName=bold_font, fontSize=14, leading=18,
            textColor=BLUE_DARK, spaceBefore=22, spaceAfter=6,
            keepWithNext=1,
            borderPad=(0, 0, 4, 0),
        ),
        "h3": S("psrai_h3",
            fontName=bold_font, fontSize=11, leading=15,
            textColor=BLACK, spaceBefore=14, spaceAfter=4,
            keepWithNext=1,
        ),
        "h4": S("psrai_h4",
            fontName=bold_font, fontSize=10, leading=14,
            textColor=GREY_TEXT, spaceBefore=10, spaceAfter=3,
            keepWithNext=1,
        ),
        "body": S("psrai_body",
            fontName=normal_font, fontSize=10, leading=15,
            textColor=BLACK, spaceAfter=6,
            alignment=TA_JUSTIFY,
        ),
        "bullet": S("psrai_bullet",
            fontName=normal_font, fontSize=10, leading=14,
            textColor=BLACK, spaceAfter=2,
            leftIndent=14, bulletIndent=0,
            alignment=TA_LEFT,
        ),
        "code_inline": S("psrai_code_inline",
            fontName=code_font, fontSize=8.5,
            textColor=BLUE_MID,
        ),
        "code_block": S("psrai_code_block",
            fontName=code_font, fontSize=8, leading=12,
            textColor=CODE_FG, backColor=GREY_LIGHT,
            borderColor=GREY_BORDER, borderWidth=0.5,
            borderPad=10,
            spaceAfter=10, spaceBefore=6,
        ),
        "toc_h2": S("psrai_toc_h2",
            fontName=normal_font, fontSize=10, leading=14,
            textColor=BLUE_DARK, leftIndent=0, spaceAfter=1,
        ),
        "toc_h3": S("psrai_toc_h3",
            fontName=normal_font, fontSize=9, leading=13,
            textColor=BLACK, leftIndent=14, spaceAfter=1,
        ),
        "footer": S("psrai_footer",
            fontName=normal_font, fontSize=8,
            textColor=GREY_TEXT, alignment=TA_CENTER,
        ),
        "th": S("psrai_th",
            fontName=bold_font, fontSize=9, leading=12,
            textColor=WHITE, alignment=TA_LEFT,
        ),
        "td": S("psrai_td",
            fontName=normal_font, fontSize=9, leading=13,
            textColor=BLACK, alignment=TA_LEFT,
        ),
        "td_code": S("psrai_td_code",
            fontName=code_font, fontSize=8, leading=12,
            textColor=BLUE_MID, alignment=TA_LEFT,
        ),
    }


# ---------------------------------------------------------------------------
# Header / footer canvas
# ---------------------------------------------------------------------------

class _DocCanvas:
    def __init__(self, styles):
        self._styles = styles

    def __call__(self, canvas, doc):
        canvas.saveState()
        w, h = A4

        # Top rule
        canvas.setStrokeColor(GREY_BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(2.0 * cm, h - 1.4 * cm, w - 2.0 * cm, h - 1.4 * cm)

        # Header text
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY_TEXT)
        canvas.drawString(2.0 * cm, h - 1.2 * cm, "PSR.ai v1.0.0")
        canvas.drawRightString(w - 2.0 * cm, h - 1.2 * cm,
                               "Architecture and Command Reference")

        # Bottom rule
        canvas.line(2.0 * cm, 1.3 * cm, w - 2.0 * cm, 1.3 * cm)

        # Page number
        canvas.drawCentredString(w / 2, 0.8 * cm, f"{doc.page}")

        canvas.restoreState()


# ---------------------------------------------------------------------------
# Inline markup: `code` and **bold**
# ---------------------------------------------------------------------------

def _apply_inline(text: str, style) -> Paragraph:
    """Escape XML, then restore backtick and bold markers as ReportLab tags."""
    # Escape XML special chars first
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Backtick code spans
    text = re.sub(
        r"`([^`]+)`",
        r'<font name="Courier" color="#0550ae" size="9">\1</font>',
        text,
    )
    # **bold**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return Paragraph(text, style)


# ---------------------------------------------------------------------------
# Table parsing
# ---------------------------------------------------------------------------

def _parse_md_table(lines: list[str], styles: dict) -> Table | None:
    rows = []
    for line in lines:
        if re.match(r"^\s*\|[-| :]+\|\s*$", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return None

    col_count = max(len(r) for r in rows)
    # Pad short rows
    rows = [r + [""] * (col_count - len(r)) for r in rows]

    # Detect header (first row) - style it differently
    table_data = []
    for ri, row in enumerate(rows):
        styled_row = []
        for ci, cell in enumerate(row):
            s = styles["th"] if ri == 0 else styles["td"]
            # Use td_code style for cells that look like code (start/end with backtick or contain /)
            if ri > 0 and (cell.startswith("`") or cell.startswith("pip")):
                s = styles["td_code"]
                cell = cell.strip("`")
            styled_row.append(_apply_inline(cell, s))
        table_data.append(styled_row)

    # Column widths: distribute evenly
    page_w = A4[0] - 4.5 * cm
    col_w = page_w / col_count

    tbl = Table(table_data, colWidths=[col_w] * col_count, repeatRows=1)
    tbl.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), TH_BG),
        ("TEXTCOLOR",  (0, 0), (-1, 0), WHITE),
        # Alternating rows
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, TR_EVEN]),
        # Grid
        ("GRID",       (0, 0), (-1, -1), 0.4, GREY_BORDER),
        ("LINEBELOW",  (0, 0), (-1, 0), 1.0, BLUE_DARK),
        # Padding
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        # Vertical align
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Markdown parser -> ReportLab flowables
# ---------------------------------------------------------------------------

def md_to_flowables(md_text: str, styles: dict) -> list:
    lines = md_text.splitlines()
    flowables = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # ---- Blank line
        if not line.strip():
            i += 1
            continue

        # ---- Headings
        if line.startswith("# ") and not line.startswith("## "):
            title_line = line[2:].strip()
            # Split on first newline that has metadata (Version/Python/etc.)
            flowables.append(Paragraph(title_line, styles["title"]))
            flowables.append(HRFlowable(width="100%", thickness=2,
                                         color=BLUE_DARK, spaceAfter=4))
            i += 1
            continue

        if line.startswith("## "):
            text = line[3:].strip()
            # Strip anchor: remove leading number prefix for display
            flowables.append(Spacer(1, 2 * mm))
            flowables.append(Paragraph(text, styles["h2"]))
            flowables.append(HRFlowable(width="100%", thickness=0.8,
                                         color=GREY_BORDER, spaceAfter=4))
            i += 1
            continue

        if line.startswith("### "):
            flowables.append(Paragraph(line[4:].strip(), styles["h3"]))
            i += 1
            continue

        if line.startswith("#### "):
            flowables.append(Paragraph(line[5:].strip(), styles["h4"]))
            i += 1
            continue

        # ---- Horizontal rule
        if re.match(r"^---+$", line.strip()):
            flowables.append(Spacer(1, 3 * mm))
            flowables.append(HRFlowable(width="100%", thickness=0.5,
                                         color=GREY_BORDER))
            flowables.append(Spacer(1, 3 * mm))
            i += 1
            continue

        # ---- Code block (fenced ```)
        if line.strip().startswith("```"):
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_text = "\n".join(code_lines)
            flowables.append(Preformatted(code_text, styles["code_block"]))
            continue

        # ---- Table (starts with |)
        if line.startswith("|"):
            tbl_lines = []
            while i < n and lines[i].startswith("|"):
                tbl_lines.append(lines[i])
                i += 1
            tbl = _parse_md_table(tbl_lines, styles)
            if tbl:
                flowables.append(Spacer(1, 2 * mm))
                flowables.append(tbl)
                flowables.append(Spacer(1, 3 * mm))
            continue

        # ---- Bullet list
        if re.match(r"^[-*] ", line):
            bullets = []
            while i < n and re.match(r"^[-*] ", lines[i]):
                bullets.append(lines[i][2:].strip())
                i += 1
            for b in bullets:
                p = _apply_inline(b, styles["bullet"])
                # Wrap in a bullet paragraph
                bp = Paragraph(
                    f'<bullet>&bull;</bullet> {p.text}',
                    ParagraphStyle(
                        "bullet_real",
                        parent=styles["bullet"],
                        bulletFontName="Helvetica",
                        bulletFontSize=10,
                    ),
                )
                flowables.append(bp)
            flowables.append(Spacer(1, 2 * mm))
            continue

        # ---- Numbered list
        if re.match(r"^\d+\. ", line):
            items = []
            while i < n and re.match(r"^\d+\. ", lines[i]):
                m = re.match(r"^(\d+)\. (.+)$", lines[i])
                if m:
                    items.append((m.group(1), m.group(2)))
                i += 1
            for num, text in items:
                p = _apply_inline(f"{num}. {text}", styles["bullet"])
                flowables.append(p)
            flowables.append(Spacer(1, 2 * mm))
            continue

        # ---- Metadata lines directly after title (bold key: value pairs like **Version:** 1.0.0)
        if line.startswith("**") and ":**" in line:
            flowables.append(_apply_inline(line.strip(), styles["subtitle"]))
            i += 1
            continue

        # ---- Regular paragraph (may span multiple non-blank lines)
        para_lines = []
        while i < n and lines[i].strip() and not lines[i].startswith("#") \
              and not lines[i].startswith("|") and not lines[i].startswith("```") \
              and not re.match(r"^[-*] ", lines[i]) \
              and not re.match(r"^\d+\. ", lines[i]) \
              and not re.match(r"^---+$", lines[i].strip()):
            para_lines.append(lines[i].strip())
            i += 1

        full = " ".join(para_lines).strip()
        if full:
            flowables.append(_apply_inline(full, styles["body"]))
            flowables.append(Spacer(1, 1 * mm))

    return flowables


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(src: Path, dst: Path) -> None:
    styles = _build_styles()
    md_text = src.read_text(encoding="utf-8")

    doc = SimpleDocTemplate(
        str(dst),
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2.0 * cm,
        bottomMargin=2.0 * cm,
        title="PSR.ai v1.0.0 - Architecture and Command Reference",
        author="PSR.ai",
        subject="Architecture, CLI reference, agent safety, distribution extras",
    )

    canvas_fn = _DocCanvas(styles)
    flowables = md_to_flowables(md_text, styles)

    doc.build(flowables, onFirstPage=canvas_fn, onLaterPages=canvas_fn)
    print(f"Written : {dst}")
    print(f"Size    : {dst.stat().st_size:,} bytes")
    pages_approx = dst.stat().st_size // 3000
    print(f"~Pages  : {pages_approx}+")


if __name__ == "__main__":
    root = Path(__file__).parent
    src  = root / "ARCHITECTURE_AND_COMMANDS.md"
    dst  = root / "ARCHITECTURE_AND_COMMANDS.pdf"
    build(src, dst)
