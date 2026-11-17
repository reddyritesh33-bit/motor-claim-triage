"""Structure-aware chunking of policy wordings (L1).

Why not fixed-size windows: a policy wording defines terms in one place, states
cover in another and restricts it in a third. Fixed windows straddle those
boundaries and lose the heading that tells a reader (or a retriever) what the
passage is. Instead we:

1. strip running headers/footers (product name, UIN, page x of y);
2. split on the document's own SECTION headings, then on sub-headings (add-on
   names, "Conditions:", "Exclusions:", "CLAIM PROCEDURE" sub-parts);
3. pack whole numbered items / bullets into chunks of ~60-260 words, never
   splitting an item, so an exclusion stays with its own sub-clauses;
4. give every chunk a stable clause id (DOC-SECTION-NN) and a contextual header
   "Document > Section > Sub-heading" that is prepended to the embedded text;
5. extract cross-references (quoted defined terms such as 'Limitations as to
   Use') so the agent's find_modifiers tool can tell it which provisions the
   retrieved text depends on but has not yet retrieved.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

from pypdf import PdfReader

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

HEADER_FOOTER = [
    re.compile(r"^Motor Private Car\s*[–-]\s*Package Policy (Wording|Prospectus)\s*$", re.I),
    re.compile(r"^UIN-?\s*IRDAN\S+\s+(Page\s+\d+\s+of\s+\d+|\d+)\s*$", re.I),
]
SECTION_RE = re.compile(r"^SECTION\s+([IVX]+)\s*[:\-–]\s*(.+)$", re.I)
ADDON_RE = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Za-z0-9 '’&/()+-]{2,60}?)\s*:\s*(UIN.*)?$")
NAMED_SUBHEAD_RE = re.compile(
    r"^(Admissibility of Claims.*|List of Documents|Claim Procedure|Loss Of Or Damage To The Vehicle Insured|"
    r"Personal Accident Cover For Owner-Driver|Liability To Third Parties Claim Process|THIRD PARTY CLAIMS|"
    r"IMMEDIATE ACTION AFTER ACCIDENT:?|What does this Policy cover\?|Coverages available:|"
    r"ADDITIONAL DEDUCTIBLE:?|Sum Insured, Insured.s Declared Value \(IDV\)|SUM INSURED.*|"
    r"AVOIDANCE OF CERTAIN TERMS AND RIGHT OF RECOVERY|APPLICATION OF LIMITS OF INDEMNITY|"
    r"\d\s+CANCELLATION|Reasons for delay which can be taken into consideration:|Escalation Matrix|"
    r"Policy Servicing.*|Turn-around time.*)\s*$",
    re.I,
)
ITEM_START_RE = re.compile(r"^(\d{1,2}[.)]?\s|[a-z][.)]\s|\(?[ivx]{1,4}[.)]\s|[•➢▪\-]\s?)", re.I)
QUOTED_TERM_RE = re.compile(r"[‘'\"“]([A-Z][A-Za-z’' ]{3,40}?)[’'\"”]")
KNOWN_TERMS = ["Limitations as to Use", "Driver's Clause", "geographical area", "deductible", "Insured's Declared Value",
               "Constructive Total Loss", "Schedule"]

MIN_WORDS, TARGET_WORDS, MAX_WORDS = 60, 180, 260


@dataclass
class Chunk:
    clause_id: str
    doc_id: str
    doc_title: str
    doc_version: str
    uin: str
    section: str
    subsection: str
    page_start: int
    page_end: int
    text: str
    header: str
    refs: list[str] = field(default_factory=list)
    sha: str = ""

    @property
    def embed_text(self) -> str:
        return f"{self.header}\n{self.text}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["embed_text"] = self.embed_text
        return d


def _normalise(s: str) -> str:
    s = s.replace("’", "’").replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬃ", "ffi")
    s = s.replace("�", "")
    return re.sub(r"[ \t]+", " ", s).strip()


def read_lines(pdf_path: str) -> list[tuple[int, str]]:
    reader = PdfReader(pdf_path)
    out: list[tuple[int, str]] = []
    for pno, page in enumerate(reader.pages, start=1):
        for raw in (page.extract_text() or "").split("\n"):
            line = _normalise(raw)
            if not line or any(p.match(line) for p in HEADER_FOOTER):
                continue
            out.append((pno, line))
    return out


def _is_caps_heading(line: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", line)
    return (len(line) <= 70 and len(letters) >= 6 and letters.isupper() and "%" not in line
            and not re.match(r"^[A-Z]\d", line))


def _section_code(roman: str | None) -> str:
    return f"S{ROMAN.get(roman.upper(), 0)}" if roman else "S0"


def _refs(text: str) -> list[str]:
    found = {m.group(1).strip() for m in QUOTED_TERM_RE.finditer(text)}
    low = text.lower()
    for term in KNOWN_TERMS:
        if term.lower().replace("’", "'") in low.replace("’", "'"):
            found.add(term)
    return sorted(found)


def chunk_document(pdf_path: str, doc: dict) -> list[Chunk]:
    """doc: manifest entry with id, code, title, version, uin."""
    lines = read_lines(pdf_path)
    blocks: list[dict] = []  # each: section, roman, subsection, lines[(page, text)]
    cur = {"section": "Preamble", "roman": None, "subsection": "", "lines": []}

    def flush():
        if cur["lines"]:
            blocks.append(dict(cur))

    for page, line in lines:
        m = SECTION_RE.match(line)
        if m and len(line) < 110:
            flush()
            cur = {"section": f"Section {m.group(1).upper()}: {m.group(2).strip().title()}",
                   "roman": m.group(1).upper(), "subsection": "", "lines": [(page, line)]}
            continue
        a = ADDON_RE.match(line)
        is_addon = bool(a and cur["roman"] == "IV" and len(line) < 120)
        if is_addon or NAMED_SUBHEAD_RE.match(line) or (_is_caps_heading(line) and cur["lines"]):
            flush()
            sub = a.group(2).strip() if is_addon else line.rstrip(":").strip()
            cur = {"section": cur["section"], "roman": cur["roman"], "subsection": sub, "lines": [(page, line)]}
            continue
        cur["lines"].append((page, line))
    flush()

    # merge tiny heading-only blocks into the following block of the same section
    merged: list[dict] = []
    for b in blocks:
        words = sum(len(t.split()) for _, t in b["lines"])
        if merged and words < 25 and merged[-1]["section"] == b["section"] and \
                sum(len(t.split()) for _, t in merged[-1]["lines"]) < MIN_WORDS:
            merged[-1]["lines"].extend(b["lines"])
            continue
        merged.append({**b, "lines": list(b["lines"])})

    chunks: list[Chunk] = []
    counters: dict[str, int] = {}
    for b in merged:
        # group lines into items (an item starts at a numbered/bulleted line)
        items: list[list[tuple[int, str]]] = []
        for page, line in b["lines"]:
            if not items or ITEM_START_RE.match(line):
                items.append([(page, line)])
            else:
                items[-1].append((page, line))
        packs: list[list[tuple[int, str]]] = []
        cur_pack: list[tuple[int, str]] = []
        for item in items:
            item_words = sum(len(t.split()) for _, t in item)
            pack_words = sum(len(t.split()) for _, t in cur_pack)
            if cur_pack and pack_words >= TARGET_WORDS or (cur_pack and pack_words + item_words > MAX_WORDS):
                packs.append(cur_pack)
                cur_pack = []
            cur_pack.extend(item)
        if cur_pack:
            if packs and sum(len(t.split()) for _, t in cur_pack) < MIN_WORDS // 2:
                packs[-1].extend(cur_pack)
            else:
                packs.append(cur_pack)

        sec_code = _section_code(b["roman"])
        for pack in packs:
            counters[sec_code] = counters.get(sec_code, 0) + 1
            text = "\n".join(t for _, t in pack)
            header = f"[{doc['title']} > {b['section']}" + (f" > {b['subsection']}" if b["subsection"] else "") + "]"
            cid = f"{doc['code']}-{sec_code}-{counters[sec_code]:03d}"
            chunks.append(Chunk(
                clause_id=cid, doc_id=doc["id"], doc_title=doc["title"], doc_version=doc["version"],
                uin=doc.get("uin", ""), section=b["section"], subsection=b["subsection"],
                page_start=pack[0][0], page_end=pack[-1][0], text=text, header=header, refs=_refs(text),
                sha=hashlib.sha256((header + "\n" + text).encode()).hexdigest()[:16],
            ))
    return chunks
