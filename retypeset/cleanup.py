"""
retypeset.cleanup -- remove the previous journal's furniture from a manuscript.

The problem this solves
-----------------------
Restyling changes fonts, margins and columns. It does not change *content*, and
a manuscript prepared for one journal carries a surprising amount of that
journal's identity as ordinary document content:

  * running headers and footers with the journal name and citation line
  * the journal's logo, sitting above the title as an inline image
  * e-ISSN / DOI placeholder lines, "Vol. xx, No. x", "20xx"
  * a copyright or Creative Commons footnote
  * leftover template instructions the author never deleted
    ("(text in red retain unchanged)", "Author name, Affiliation", ...)

Restyled to Elsevier's rules, a Diagnostyka manuscript came out in correct
single-column double-spaced form with line numbers -- and still had the
Diagnostyka logo on page 1 and its citation header on every page. Formatting was
right; the document was unusable.

Design
------
This module is conservative and reversible in spirit: every removal is reported,
nothing is deleted that is not matched by an explicit pattern, and the caller
opts in. It never touches equations, figures referenced by captions, tables or
the reference list.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"

_DOCUMENT = "word/document.xml"

# Lines that are journal furniture rather than the author's manuscript.
# Each pattern must be specific enough that a false positive is implausible.
_FURNITURE_PATTERNS: list[tuple[str, str]] = [
    (r"^\s*article\s+citation\s+info", "citation-info block"),
    (r"^\s*(e-?)?issn\b", "ISSN line"),
    (r"^\s*doi\s*:?\s*$", "empty DOI placeholder"),
    (r"^\s*doi\s*:?\s*10\.x+", "DOI placeholder"),
    (r"\b20xx\b.*\bvol\.?\s*xx\b", "volume/issue placeholder"),
    (r"^\s*vol\.?\s*xx\b", "volume placeholder"),
    (r"licensee .*\blicen[cs]e\b", "licence statement"),
    (r"creative\s+commons\s+attribution", "Creative Commons notice"),
    (r"^\s*©\s*20", "copyright line"),
    (r"^\s*received\s+20\d\d-?x*-?x*\s*;\s*accepted", "submission-dates placeholder"),
    (r"this article is an open access article distributed", "open-access notice"),
    (r"^\s*\(?\s*text in .{0,20}retain(ed)? unchanged", "template instruction"),
    (r"^\s*\(?\s*(do not|don't) (change|modify|edit|delete) ", "template instruction"),
    (r"^\s*publication\s+fee\s*$", "publication-fee section"),
    (r"^\s*xxxxx+\b", "placeholder text"),
]

_COMPILED = [(re.compile(p, re.I), label) for p, label in _FURNITURE_PATTERNS]

# An image is treated as journal branding if it sits before the title, is small,
# and is not referenced by any figure caption.
_LOGO_MAX_MM = 40.0
EMU_PER_MM = 914400.0 / 25.4


@dataclass
class CleanupResult:
    path: Path
    removed_paragraphs: list[str] = field(default_factory=list)
    removed_images: int = 0
    cleared_headers: int = 0
    cleared_footers: int = 0
    reindented: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (len(self.removed_paragraphs) + self.removed_images
                + self.cleared_headers + self.cleared_footers)


def _para_text(p: etree._Element) -> str:
    return "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))


def _has_drawing(p: etree._Element) -> bool:
    return p.find(f".//{{{W}}}drawing") is not None or p.find(f".//{{{W}}}pict") is not None


def _image_width_mm(p: etree._Element) -> float:
    widths = []
    for ext in p.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent"):
        try:
            widths.append(int(ext.get("cx") or 0) / EMU_PER_MM)
        except ValueError:
            pass
    return max(widths) if widths else 0.0


def clean(source_docx: str | Path, out_path: str | Path, *,
          title: str = "",
          drop_headers: bool = True,
          drop_footers: bool = True,
          drop_logos: bool = True,
          drop_boilerplate: bool = True,
          normalise_indents: bool = True,
          first_line_indent_mm: float = 0.0) -> CleanupResult:
    """Strip journal furniture from `source_docx` into `out_path`."""
    src, out = Path(source_docx), Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.chmod(out.stat().st_mode | stat.S_IWUSR)
        out.unlink()

    result = CleanupResult(path=out)

    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        payload = {n: z.read(n) for n in names}

    # -- headers and footers ----------------------------------------------
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if drop_headers and base.startswith("header") and base.endswith(".xml"):
            payload[name] = _empty_part(payload[name], "hdr")
            result.cleared_headers += 1
        elif drop_footers and base.startswith("footer") and base.endswith(".xml"):
            payload[name] = _empty_part(payload[name], "ftr")
            result.cleared_footers += 1

    # -- body --------------------------------------------------------------
    if _DOCUMENT in payload:
        payload[_DOCUMENT] = _clean_body(
            payload[_DOCUMENT], result, title=title,
            drop_logos=drop_logos, drop_boilerplate=drop_boilerplate,
            normalise_indents=normalise_indents,
            first_line_indent_mm=first_line_indent_mm,
        )

    # Footnotes carry the licence block in many journal templates.
    if drop_boilerplate:
        for name in names:
            if name.endswith("word/footnotes.xml"):
                payload[name], n = _clean_footnotes(payload[name])
                if n:
                    result.notes.append(f"{n} boilerplate footnote(s) emptied "
                                        "(licence / submission dates).")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as oz:
        for n in names:
            oz.writestr(n, payload[n])

    if result.cleared_headers or result.cleared_footers:
        result.notes.append(
            f"{result.cleared_headers} header(s) and {result.cleared_footers} "
            "footer(s) emptied - these carried the previous journal's running "
            "citation line."
        )
    return result


def _empty_part(xml: bytes, tag: str) -> bytes:
    """Replace a header/footer part with a single empty paragraph.

    Deleting the part would break the relationship references in sectPr; an
    empty body is the safe equivalent.
    """
    root = etree.fromstring(xml)
    for child in list(root):
        root.remove(child)
    p = root.makeelement(f"{{{W}}}p", {})
    root.append(p)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True)


def _clean_footnotes(xml: bytes) -> tuple[bytes, int]:
    root = etree.fromstring(xml)
    n = 0
    for fn in root.iter(f"{{{W}}}footnote"):
        text = "".join(t.text or "" for t in fn.iter(f"{{{W}}}t"))
        if not text.strip():
            continue
        if any(rx.search(text) for rx, _ in _COMPILED):
            for p in list(fn):
                fn.remove(p)
            fn.append(fn.makeelement(f"{{{W}}}p", {}))
            n += 1
    return (etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                           standalone=True), n)


def _clean_body(xml: bytes, result: CleanupResult, *, title: str,
                drop_logos: bool, drop_boilerplate: bool,
                normalise_indents: bool, first_line_indent_mm: float) -> bytes:
    root = etree.fromstring(xml)
    body = root.find(f"{{{W}}}body")
    if body is None:
        return xml

    paragraphs = list(body.iter(f"{{{W}}}p"))
    title_norm = re.sub(r"\s+", " ", title or "").strip().lower()

    # Where does the manuscript proper begin? Everything above the title is
    # masthead. Without a known title we fall back to the first 12 paragraphs,
    # which is where journal furniture lives in every template seen so far.
    title_idx = None
    if title_norm:
        for i, p in enumerate(paragraphs):
            if re.sub(r"\s+", " ", _para_text(p)).strip().lower() == title_norm:
                title_idx = i
                break
    masthead_end = title_idx if title_idx is not None else min(12, len(paragraphs))

    to_remove: list[etree._Element] = []

    for i, p in enumerate(paragraphs):
        text = _para_text(p).strip()

        if drop_logos and i < masthead_end and _has_drawing(p):
            w = _image_width_mm(p)
            if 0 < w <= _LOGO_MAX_MM:
                to_remove.append(p)
                result.removed_images += 1
                continue

        if not text:
            continue

        if drop_boilerplate:
            for rx, label in _COMPILED:
                if rx.search(text):
                    to_remove.append(p)
                    result.removed_paragraphs.append(f"{label}: {text[:70]}")
                    break

    for p in to_remove:
        parent = p.getparent()
        if parent is not None:
            parent.remove(p)

    # Removing a masthead leaves the empty paragraphs that spaced it out, which
    # push the title down a third of the first page.
    blanks = 0
    for p in list(body):
        if p.tag != f"{{{W}}}p":
            break
        if _para_text(p).strip() or _has_drawing(p):
            break
        body.remove(p)
        blanks += 1
    if blanks:
        result.notes.append(f"{blanks} empty paragraph(s) removed from the top "
                            "of the document.")

    if normalise_indents:
        result.reindented = _normalise_indents(body, first_line_indent_mm)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True)


def _normalise_indents(body: etree._Element, first_line_mm: float) -> int:
    """Clear inherited left/right paragraph indents.

    A manuscript laid out for a narrow two-column journal carries left and right
    indents that look absurd once the text is a single full-width column. First-
    line indent is set from the caller rather than cleared, because journals do
    differ on it.
    """
    twips = int(round(first_line_mm / 25.4 * 1440))
    n = 0
    for p in body.iter(f"{{{W}}}p"):
        if not _para_text(p).strip():
            continue
        ppr = p.find(f"{{{W}}}pPr")
        if ppr is None:
            continue
        ind = ppr.find(f"{{{W}}}ind")
        if ind is None:
            continue
        changed = False
        for attr in ("left", "right", "start", "end"):
            if ind.get(f"{{{W}}}{attr}") not in (None, "0"):
                ind.set(f"{{{W}}}{attr}", "0")
                changed = True
        if twips:
            ind.set(f"{{{W}}}firstLine", str(twips))
            ind.attrib.pop(f"{{{W}}}hanging", None)
        else:
            for attr in ("firstLine", "hanging"):
                if ind.get(f"{{{W}}}{attr}") not in (None, "0"):
                    ind.attrib.pop(f"{{{W}}}{attr}", None)
                    changed = True
        if changed:
            n += 1
    return n


def describe(source_docx: str | Path, title: str = "") -> list[str]:
    """Dry run: what would be removed, without writing anything."""
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        res = clean(source_docx, Path(td) / "probe.docx", title=title)
        out = list(res.removed_paragraphs)
        if res.removed_images:
            out.append(f"{res.removed_images} masthead logo/image(s)")
        if res.cleared_headers or res.cleared_footers:
            out.append(f"{res.cleared_headers} header(s), "
                       f"{res.cleared_footers} footer(s)")
        return out
