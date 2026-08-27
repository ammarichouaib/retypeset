"""
retypeset.template_profile -- turn an author's own journal template into a profile.

Why this exists
---------------
Until now a target journal had to exist as `profiles/<id>.json` before anything
could be checked. That is the wrong first step for the common case: the author
has downloaded the publisher's Word template and has no interest in writing
JSON. Everything a profile stores about *presentation* -- page size, margins,
columns, body font and size, line spacing, line numbering, heading numbering --
is already in that file, and most publisher templates additionally carry their
own author instructions in the body text ("The abstract should not exceed 250
words", "3 to 6 keywords"), which is where the *structural* limits come from.

So: read the template, derive a profile, show the author what was read and where
each value came from, let them correct it, and optionally save it as a normal
profile file. The template stays the source of truth for style transplant; the
derived profile only drives the compliance check and the LaTeX route.

Honesty rules, kept deliberately strict
---------------------------------------
* Everything derived here is `verified=False`. It was inferred from a file, not
  read from the publisher's guidelines, so every rule reports as a warning.
* A limit is recorded only when a pattern matched. Nothing is guessed to fill a
  field -- an omitted key falls back to the schema default and produces no
  finding, which is the correct behaviour for "unknown".
* Every derived value carries a one-line evidence string, quoting the sentence
  it came from where mining was involved. The UI shows these; a value the author
  cannot trace is a value they cannot be expected to trust.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from .profile import JournalProfile, writable_profile_dir
from .template_docx import inspect as inspect_template
from .template_docx import TemplateInfo

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCUMENT = "word/document.xml"
_STYLES = "word/styles.xml"


@dataclass
class Derived:
    """A profile inferred from a template, plus the reasoning behind it."""

    profile: JournalProfile
    info: TemplateInfo
    evidence: list[str] = field(default_factory=list)
    text_chars: int = 0

    @property
    def mined_any(self) -> bool:
        return any(e.startswith("mined") for e in self.evidence)


# ---------------------------------------------------------------------------
# Raw reads
# ---------------------------------------------------------------------------

def _document_text(path: Path, limit: int = 400_000) -> str:
    """All visible text of the template, including text boxes.

    Publisher templates put their instructions in ordinary paragraphs, in
    tables, and -- often -- in floating text boxes, which is exactly the content
    Pandoc drops. Reading `w:t` directly picks up all three.
    """
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read(_DOCUMENT)
    except (KeyError, zipfile.BadZipFile):
        return ""
    root = etree.fromstring(xml)
    parts: list[str] = []
    size = 0
    for p in root.iter(f"{{{W}}}p"):
        line = "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))
        if not line.strip():
            continue
        parts.append(line.strip())
        size += len(line)
        if size > limit:
            break
    return "\n".join(parts)


def _line_spacing(path: Path) -> tuple[float, str]:
    """Default line spacing from docDefaults, as a multiple."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read(_STYLES)
    except (KeyError, zipfile.BadZipFile):
        return 0.0, ""
    root = etree.fromstring(xml)
    sp = root.find(f".//{{{W}}}docDefaults//{{{W}}}spacing")
    if sp is None:
        return 0.0, ""
    rule = sp.get(f"{{{W}}}lineRule") or "auto"
    raw = sp.get(f"{{{W}}}line")
    if not raw:
        return 0.0, ""
    try:
        val = int(raw)
    except ValueError:
        return 0.0, ""
    if rule in ("auto", "atLeast", ""):
        return round(val / 240, 2), rule
    return round(val / 240, 2), rule          # exact: twips, still reported as a ratio


def _has_line_numbers(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read(_DOCUMENT)
    except (KeyError, zipfile.BadZipFile):
        return False
    return etree.fromstring(xml).find(f".//{{{W}}}lnNumType") is not None


# ---------------------------------------------------------------------------
# Mining the template's own author instructions
# ---------------------------------------------------------------------------

_NUM = r"(\d{1,4})"
_CAP = r"(?:not\s+exceed|no\s+more\s+than|no\s+longer\s+than|maximum\s+of|"       \
       r"limited\s+to|at\s+most|within|up\s+to|less\s+than|fewer\s+than)"

_ABSTRACT_PATTERNS = [
    # The window handed to these patterns is already scoped to a few sentences
    # around the word "abstract", so they may cross sentence boundaries: several
    # templates put the heading, the instruction and the number on three
    # consecutive lines.
    rf"abstract[^\n]{{0,200}}?between\s+{_NUM}\s*(?:to|-|--|–|and)\s*{_NUM}\s*words",
    rf"abstract[^\n]{{0,200}}?{_CAP}[^\n]{{0,25}}?{_NUM}\s*words",
    rf"abstract[^\n]{{0,200}}?{_NUM}\s*words\s*(?:max\.?|maximum|or\s+less)",
    rf"abstract[^.\n]{{0,60}}?of\s+{_NUM}\s*words\s+or\s+less",
    rf"{_NUM}\s*words?\s*(?:maximum|max\.?)[^.\n]{{0,40}}abstract",
]
_KEYWORD_RANGE = [
    rf"{_NUM}\s*(?:to|-|--|–)\s*{_NUM}\s*key\s?words",
    # "a minimum of three to a maximum of six keywords" -- spelled numbers are
    # normalised to digits before matching, so this reaches the same rule.
    rf"minimum\s+of\s+{_NUM}[^.\n]{{0,40}}?maximum\s+of\s+{_NUM}\s*key\s?words",
    rf"{_NUM}\s*(?:to|-|--|–)[^.\n]{{0,30}}?{_NUM}\s*key\s?words",
    rf"key\s?words[^.\n]{{0,40}}?{_NUM}\s*(?:to|-|--|–)\s*{_NUM}",
]
_KEYWORD_MAX = [
    rf"key\s?words[^.\n]{{0,60}}?{_CAP}[^.\n]{{0,15}}?{_NUM}",
    rf"{_CAP}\s*{_NUM}\s*key\s?words",
]
_HIGHLIGHT_RANGE = [
    rf"highlights[^.\n]{{0,80}}?{_NUM}\s*(?:to|-|--|–)\s*{_NUM}\s*"
    r"(?:bullet|point|item)",
]
_HIGHLIGHT_CHARS = [
    rf"highlights?[^.\n]{{0,120}}?{_NUM}\s*characters",
    rf"{_NUM}\s*characters[^.\n]{{0,60}}?(?:including\s+spaces)",
]
_TITLE_CHARS = [rf"title[^.\n]{{0,80}}?{_CAP}[^.\n]{{0,15}}?{_NUM}\s*characters"]
_DPI = [rf"{_NUM}\s*dpi[^.\n]{{0,60}}?(?:halftone|photograph|colour|color)",
        rf"(?:halftone|photograph|colour|color)[^.\n]{{0,60}}?{_NUM}\s*dpi"]
_WORDS_TOTAL = [rf"manuscript[^.\n]{{0,80}}?{_CAP}[^.\n]{{0,20}}?{_NUM}\s*words",
                rf"{_CAP}\s*{_NUM}\s*words\s+in\s+(?:total|length)"]


_WORD_NUMBERS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12",
}
_WORD_NUMBER_RE = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.I)


def _digits(text: str) -> str:
    """Spell out numbers as digits so one set of patterns covers both.

    Author guidelines mix the two freely inside a single sentence -- "a minimum
    of three to a maximum of six keywords" -- and writing every rule twice is
    how one of the two variants silently stops being matched.
    """
    return _WORD_NUMBER_RE.sub(lambda m: _WORD_NUMBERS[m.group(1).lower()], text)


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip())
    return out


def _first_match(sentences: list[str], patterns: list[str], *,
                 context: int = 0) -> tuple[tuple[int, ...], str] | None:
    """First (numbers, quoted sentence) match, searching sentence by sentence.

    Sentence scope matters: searching the whole document lets "abstract" in one
    paragraph pair with "250 words" three paragraphs later and produce a limit
    nobody wrote.

    `context` widens that window by N preceding sentences, which is needed for
    the templates that put the keyword in a heading and the number in the line
    below it -- "Abstract" / "(250 words max.)". One sentence of lookback is
    enough for that idiom and still far short of document scope.
    """
    windows = list(sentences)
    if context:
        windows = [" ".join(sentences[max(0, i - context):i + 1])
                   for i in range(len(sentences))]
    for s in windows:
        low = _digits(s.lower())
        for pat in patterns:
            m = re.search(pat, low, re.I)
            if m:
                try:
                    nums = tuple(int(g) for g in m.groups() if g and g.isdigit())
                except ValueError:
                    continue
                if nums:
                    return nums, s[:200]
    return None


def _reference_style(text: str) -> tuple[str, str] | None:
    """numeric vs author-year, decided by what the template's own examples use."""
    numeric = len(re.findall(r"\[\s*\d{1,3}\s*(?:[,-]\s*\d{1,3}\s*)*\]", text))
    author_year = len(re.findall(r"\([A-Z][A-Za-zÀ-ſ'\-]+"
                                 r"(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z]+))?"
                                 r",?\s+(?:19|20)\d{2}[a-z]?\)", text))
    superscript = len(re.findall(r"references?[^.\n]{0,80}superscript", text, re.I))
    if superscript:
        return "numeric-superscript", "template mentions superscript citations"
    if numeric >= 3 and numeric > author_year:
        return "numeric", f"{numeric} bracketed citation example(s) in the template"
    if author_year >= 3 and author_year > numeric:
        return "author-year", f"{author_year} (Author, year) example(s) in the template"
    return None


_ROMAN_HEAD = re.compile(r"^\s*(I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+\S")
_ARABIC_HEAD = re.compile(r"^\s*\d+(\.\d+)*\.?\s+\S")


def _heading_numbering(text: str) -> tuple[str, str] | None:
    roman = sum(1 for line in text.splitlines() if _ROMAN_HEAD.match(line))
    arabic = sum(1 for line in text.splitlines() if _ARABIC_HEAD.match(line))
    if roman >= 2 and roman >= arabic:
        return "roman", f"{roman} Roman-numbered heading(s) in the template"
    if arabic >= 2:
        return "arabic", f"{arabic} Arabic-numbered heading(s) in the template"
    return None


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "uploaded_template"


def derive(template_path: str | Path, *, profile_id: str = "",
           journal: str = "", publisher: str = "",
           base: JournalProfile | None = None,
           mine_text: bool = True) -> Derived:
    """Infer a JournalProfile from a .docx/.dotx template.

    `base` seeds the result -- pass the publisher's generic profile when the
    author knows the publisher, and the template only overrides what it actually
    proves. Without a base, unproven fields stay at schema defaults.
    """
    path = Path(template_path)
    info = inspect_template(path)
    ev: list[str] = []

    data: dict[str, Any] = (base.model_dump() if base else {})
    data.update({
        "id": profile_id or _slug(path.stem),
        "journal": journal or path.stem.replace("_", " ").strip(),
        "publisher": publisher or (base.publisher if base else "from template"),
        "verified": False,
    })
    if base:
        data["template_family"] = base.template_family
    docx = dict(data.get("docx") or {})
    structure = dict(data.get("structure") or {})
    figures = dict(data.get("figures") or {})
    references = dict(data.get("references") or {})
    sources = dict(data.get("sources") or {})

    # ---------------- presentation: read, never guessed -------------------
    docx["template_file"] = str(path)

    if info.default_font:
        docx["body_font"] = info.default_font
        ev.append(f"read · body font `{info.default_font}` from docDefaults")
    if info.default_size_pt:
        docx["body_size_pt"] = float(info.default_size_pt)
        ev.append(f"read · body size {info.default_size_pt:g} pt from docDefaults")

    page = (info.page_size or "").lower()
    if page.startswith("a4"):
        docx["page_size"] = "a4"
        ev.append("read · A4 page size from sectPr")
    elif page.startswith("letter"):
        docx["page_size"] = "letter"
        ev.append("read · US Letter page size from sectPr")
    elif info.page_size:
        # IEEE and several society templates use a trimmed custom sheet. The
        # profile schema only knows A4 and Letter, so record the fact rather
        # than rounding it to whichever is closer and stating it as read.
        ev.append(f"read · non-standard page size ({info.page_size}) — kept at "
                  f"`{docx.get('page_size', 'a4')}` because the profile schema "
                  "has no custom size; the template route reproduces it exactly")

    if info.margins_mm and all(info.margins_mm.get(k) for k in
                               ("top", "bottom", "left", "right")):
        docx["margins_mm"] = {k: float(v) for k, v in info.margins_mm.items()}
        ev.append("read · margins " + ", ".join(
            f"{k} {v:g} mm" for k, v in info.margins_mm.items()))

    if info.columns:
        # A template's first section is often a single-column title block over a
        # two-column body. The body layout is the one that matters, so take the
        # most frequent value rather than the first.
        cols = max(set(info.columns), key=info.columns.count)
        docx["columns"] = int(cols)
        ev.append(f"read · {cols} column(s) (section values {info.columns})")

    spacing, rule = _line_spacing(path)
    if spacing and rule in ("auto", "atLeast"):
        docx["line_spacing"] = spacing
        ev.append(f"read · line spacing {spacing:g}× from docDefaults")

    if _has_line_numbers(path):
        docx["line_numbers"] = True
        ev.append("read · continuous line numbering is on in the template")

    # ---------------- structure: mined from the instructions ---------------
    text = _document_text(path) if mine_text else ""
    sents = _sentences(text)

    if sents:
        hit = _first_match(sents, _ABSTRACT_PATTERNS, context=3)
        if hit and 30 <= hit[0][-1] <= 2000:
            structure["abstract_max_words"] = hit[0][-1]
            ev.append(f"mined · abstract ≤ {hit[0][-1]} words — “{hit[1]}”")
            sources["structure.abstract_max_words"] = f"template: {path.name}"

        hit = _first_match(sents, _KEYWORD_RANGE, context=1)
        if hit and len(hit[0]) >= 2 and 1 <= hit[0][0] < hit[0][1] <= 20:
            structure["keywords_min"], structure["keywords_max"] = hit[0][0], hit[0][1]
            ev.append(f"mined · {hit[0][0]}–{hit[0][1]} keywords — “{hit[1]}”")
        else:
            hit = _first_match(sents, _KEYWORD_MAX, context=1)
            if hit and 1 <= hit[0][-1] <= 20:
                structure["keywords_max"] = hit[0][-1]
                ev.append(f"mined · at most {hit[0][-1]} keywords — “{hit[1]}”")

        hit = _first_match(sents, _HIGHLIGHT_RANGE, context=1)
        if hit and len(hit[0]) >= 2 and 1 <= hit[0][0] < hit[0][1] <= 10:
            structure["highlights_required"] = True
            structure["highlights_min"], structure["highlights_max"] = hit[0][0], hit[0][1]
            ev.append(f"mined · {hit[0][0]}–{hit[0][1]} highlights — “{hit[1]}”")
            hit2 = _first_match(sents, _HIGHLIGHT_CHARS)
            if hit2 and 40 <= hit2[0][-1] <= 200:
                structure["highlights_max_chars"] = hit2[0][-1]
                ev.append(f"mined · highlights ≤ {hit2[0][-1]} characters")

        hit = _first_match(sents, _TITLE_CHARS)
        if hit and 20 <= hit[0][-1] <= 500:
            structure["title_max_chars"] = hit[0][-1]
            ev.append(f"mined · title ≤ {hit[0][-1]} characters — “{hit[1]}”")

        hit = _first_match(sents, _WORDS_TOTAL)
        if hit and 500 <= hit[0][-1] <= 30000:
            structure["manuscript_max_words"] = hit[0][-1]
            ev.append(f"mined · manuscript ≤ {hit[0][-1]} words — “{hit[1]}”")

        hit = _first_match(sents, _DPI)
        if hit and 72 <= hit[0][-1] <= 1200:
            figures["dpi_halftone"] = hit[0][-1]
            ev.append(f"mined · {hit[0][-1]} dpi for halftone artwork — “{hit[1]}”")

        style = _reference_style(text)
        if style:
            references["style"] = style[0]
            ev.append(f"mined · reference style `{style[0]}` — {style[1]}")

        numbering = _heading_numbering(text)
        if numbering:
            docx["heading_numbering"] = numbering[0]
            ev.append(f"mined · {numbering[0]} heading numbering — {numbering[1]}")

    data["docx"] = docx
    data["structure"] = structure
    data["figures"] = figures
    data["references"] = references
    data["sources"] = sources
    data["notes"] = (
        f"Derived from the uploaded template `{path.name}`"
        + (f", seeded from the {base.label} profile" if base else "")
        + ". Presentation values were read from the file; structural limits, "
        "where present, were mined from the template's own author instructions. "
        "Nothing here came from the publisher's guidelines, so `verified` stays "
        "false and every rule reports as a warning rather than a failure."
    )

    prof = JournalProfile.model_validate(data)
    return Derived(profile=prof, info=info, evidence=ev, text_chars=len(text))


def save(profile: JournalProfile, directory: str | Path | None = None,
         *, overwrite: bool = False) -> Path:
    """Write a derived profile to disk so it survives the session.

    Defaults to the shipped `profiles/` directory when that is writable, and to
    the per-user directory when it is not -- which is the normal case for the
    packaged Windows build installed under `Program Files`.
    """
    d = Path(directory) if directory else writable_profile_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{profile.id}.json"
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path.name} already exists")
    path.write_text(profile.model_dump_json(indent=2, exclude_none=False) + "\n",
                    encoding="utf-8")
    from .profile import load_profiles
    load_profiles.cache_clear()
    return path
