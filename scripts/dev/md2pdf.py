"""Render a Markdown report to a styled A4 PDF (reportlab).

    python scripts/dev/md2pdf.py docs/FINAL_REPORT.md Final_Report.pdf

Supports: # / ## / ### headings, paragraphs, - bullets, 1. numbered items, pipe tables,
``` code blocks, ![alt](relative/image.png), <!-- pagebreak -->, **bold**, *italic*, `code`.
"""
import os
import re
import sys

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

src, out = sys.argv[1], sys.argv[2]
footer_text = sys.argv[3] if len(sys.argv) > 3 else "MLSD Mini Project 01 - Motor Claim Intake & Triage"
SRC_DIR = os.path.dirname(os.path.abspath(src))
WIDTH = A4[0] - 36 * mm
ACCENT, INK, MUTE = colors.HexColor("#1f4e8c"), colors.HexColor("#1d2330"), colors.HexColor("#5b6475")
LINE, HEAD_BG, CODE_BG = colors.HexColor("#c9d1dd"), colors.HexColor("#e8eef7"), colors.HexColor("#f4f6f9")

ss = getSampleStyleSheet()
base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=9.8, leading=13.8,
                      textColor=INK, spaceAfter=5)
title = ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22,
                       textColor=ACCENT, spaceAfter=4)
subtitle = ParagraphStyle("sub", parent=base, textColor=MUTE, spaceAfter=10)
h2 = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=12.8, leading=16, textColor=ACCENT,
                    spaceBefore=10, spaceAfter=5, keepWithNext=1)
h3 = ParagraphStyle("h3", parent=base, fontName="Helvetica-Bold", fontSize=10.6, leading=14, spaceBefore=6,
                    spaceAfter=3, keepWithNext=1)
cell = ParagraphStyle("cell", parent=base, fontSize=8.6, leading=11.2, spaceAfter=0)
cellh = ParagraphStyle("cellh", parent=cell, fontName="Helvetica-Bold")
bullet = ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=3, spaceAfter=2)
numbered = ParagraphStyle("numbered", parent=base, leftIndent=16, bulletIndent=2, spaceAfter=4)
code = ParagraphStyle("code", parent=base, fontName="Courier", fontSize=7.9, leading=10)


def latin(t: str) -> str:
    t = t.replace("→", "->").replace("≤", "<=").replace("≥", ">=").replace("≈", "~")
    t = t.replace("–", "-").replace("—", "-").replace("₹", "Rs ")
    return t.encode("latin-1", "replace").decode("latin-1")


def inline(t: str) -> str:
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"`([^`]+)`", r'<font face="Courier" size="8.6">\1</font>', t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return latin(t)


def make_table(rows):
    n = len(rows[0])
    lens = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(n)]
    weights = [min(max(x, 6), 90) for x in lens]
    total = sum(weights)
    cols = [WIDTH * w / total for w in weights]
    data = [[Paragraph(inline(c), cellh) for c in rows[0]]] + \
           [[Paragraph(inline(r[i] if i < len(r) else ""), cell) for i in range(n)] for r in rows[1:]]
    t = Table(data, colWidths=cols, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def boxed(flow, bg=None):
    t = Table([[flow]], colWidths=[WIDTH])
    style = [("BOX", (0, 0), (-1, -1), 0.5, LINE)]
    if bg:
        style += [("BACKGROUND", (0, 0), (-1, -1), bg), ("LEFTPADDING", (0, 0), (-1, -1), 6),
                  ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    else:
        style += [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                  ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]
    t.setStyle(TableStyle(style))
    return t


def image(rel):
    path = os.path.join(SRC_DIR, rel)
    w, h = PILImage.open(path).size
    width = WIDTH
    height = h * width / w
    max_h = A4[1] * 0.55
    if height > max_h:
        width, height = width * max_h / height, max_h
    return boxed(Image(path, width=width, height=height))


lines = open(src, encoding="utf-8").read().splitlines()
story, i, first = [], 0, True
BLOCK_START = re.compile(r"^(#|\||```|\s*[-*] |\d+\. |!\[|<!--)")
while i < len(lines):
    ln = lines[i]
    if ln.strip() == "<!-- pagebreak -->":
        story.append(PageBreak())
    elif ln.startswith("```"):
        j, buf = i + 1, []
        while j < len(lines) and not lines[j].startswith("```"):
            buf.append(lines[j])
            j += 1
        story += [boxed(Preformatted(latin("\n".join(buf)), code), CODE_BG), Spacer(1, 6)]
        i = j
    elif m := re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", ln):
        story += [image(m.group(2)), Spacer(1, 8)]
    elif ln.startswith("|"):
        rows = []
        while i < len(lines) and lines[i].startswith("|"):
            if not re.match(r"^\|[\s\-:|]+\|$", lines[i]):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
            i += 1
        story += [make_table(rows), Spacer(1, 7)]
        continue
    elif ln.startswith("# "):
        story.append(Paragraph(inline(ln[2:]), title))
        if first and i + 2 < len(lines) and lines[i + 2].strip():
            story.append(Paragraph(inline(lines[i + 2]), subtitle))
            i += 2
        first = False
    elif ln.startswith("## "):
        story.append(Paragraph(inline(ln[3:]), h2))
    elif ln.startswith("### "):
        story.append(Paragraph(inline(ln[4:]), h3))
    elif re.match(r"^\d+\. ", ln):
        num, text = ln.split(". ", 1)
        while i + 1 < len(lines) and lines[i + 1].startswith("   ") and not re.match(r"^\s*\d+\. ", lines[i + 1]):
            i += 1
            text += " " + lines[i].strip()
        story.append(Paragraph(inline(text), numbered, bulletText=num + "."))
    elif re.match(r"^\s*[-*] ", ln):
        text = re.sub(r"^\s*[-*] ", "", ln)
        while i + 1 < len(lines) and lines[i + 1].startswith("  ") and not re.match(r"^\s*[-*] ", lines[i + 1]):
            i += 1
            text += " " + lines[i].strip()
        story.append(Paragraph(inline(text), bullet, bulletText="•"))
    elif ln.strip():
        text = ln
        while i + 1 < len(lines) and lines[i + 1].strip() and not BLOCK_START.match(lines[i + 1]):
            i += 1
            text += " " + lines[i].strip()
        story.append(Paragraph(inline(text), base))
    i += 1


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTE)
    canvas.drawString(18 * mm, 10 * mm, footer_text)
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                  title=os.path.splitext(os.path.basename(out))[0].replace("_", " ")).build(
    story, onFirstPage=footer, onLaterPages=footer)
print("wrote", out)
