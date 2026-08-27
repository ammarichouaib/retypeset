"""
retypeset.template_docx -- apply the publisher's own Word template to a manuscript.

Why this beats a rule-based restyler
------------------------------------
A profile can express "Times New Roman 10 pt, two columns, 25 mm margins". It
cannot express what an IEEE or Elsevier template actually encodes: the exact
title block, the author/affiliation blocks, the abstract's bold-italic run
style, Roman section numbering, caption styles, the theme fonts, the numbering
definitions. Those live in the template's `styles.xml`, `theme1.xml` and
`numbering.xml`, and no reasonable number of profile fields will reproduce them.

Publishers already ship these files. So the highest-fidelity path is not to
describe the format but to *transplant* it: take the template's style
definitions and page setup, merge them into the author's document, then point
each paragraph at the matching template style.

What is transplanted
    word/styles.xml      merged: template definitions win, extra local styles kept
    word/theme/theme1.xml    replaced (theme fonts and colours)
    word/numbering.xml   replaced when the template has one
    sectPr               page size, margins and column layout

What is NOT touched
    Every equation, image, table, footnote and field code in the body. They are
    left exactly as the author wrote them, which is the whole reason this route
    exists rather than a rebuild.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .ir import Manuscript, SectionRole

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}

_STYLES = "word/styles.xml"
_THEME = "word/theme/theme1.xml"
_NUMBERING = "word/numbering.xml"
_DOCUMENT = "word/document.xml"

# Template style names we know how to target, by IR concept. Word templates use
# wildly different ids but fairly consistent *names*, so we match on name.
_ROLE_STYLE_HINTS: dict[SectionRole, tuple[str, ...]] = {
    SectionRole.ABSTRACT: ("abstract", "papersubtitle", "abstract text"),
    SectionRole.KEYWORDS: ("keywords", "index terms", "keyword"),
    SectionRole.REFERENCES: ("references", "reference", "bibliography"),
    SectionRole.ACKNOWLEDGEMENTS: ("acknowledgment", "acknowledgement"),
}
_TITLE_HINTS = ("title", "papertitle", "paper title", "titre")
_AUTHOR_HINTS = ("author", "authors", "paperauthor", "author name")
_CAPTION_HINTS = ("caption", "figure caption", "table caption", "légende")
_HEADING_HINTS = ("heading {n}", "heading{n}", "titre {n}")


@dataclass
class TemplateInfo:
    path: Path
    style_names: list[str] = field(default_factory=list)
    heading_styles: list[str] = field(default_factory=list)
    default_font: str = ""
    default_size_pt: float = 0.0
    page_size: str = ""
    margins_mm: dict[str, float] = field(default_factory=dict)
    columns: list[int] = field(default_factory=list)
    has_numbering: bool = False
    has_theme: bool = False

    @property
    def summary(self) -> str:
        cols = "/".join(str(c) for c in self.columns) or "?"
        return (f"{len(self.style_names)} styles, default "
                f"{self.default_font or '?'} {self.default_size_pt or '?'} pt, "
                f"{self.page_size or '?'}, {cols} column(s)")


@dataclass
class ApplyResult:
    path: Path
    styles_merged: int = 0
    paragraphs_mapped: int = 0
    notes: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def _read(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _twips_to_mm(v: str | None) -> float:
    try:
        return round(int(v) / 1440 * 25.4, 1)
    except (TypeError, ValueError):
        return 0.0


def inspect(template_path: str | Path) -> TemplateInfo:
    """Read a .docx/.dotx template and report what it will contribute."""
    p = Path(template_path)
    info = TemplateInfo(path=p)

    with zipfile.ZipFile(p) as z:
        styles_xml = _read(z, _STYLES)
        doc_xml = _read(z, _DOCUMENT)
        info.has_theme = _read(z, _THEME) is not None
        info.has_numbering = _read(z, _NUMBERING) is not None

    if styles_xml:
        root = etree.fromstring(styles_xml)
        for s in root.findall(f"{{{W}}}style"):
            nm = s.find(f"{{{W}}}name")
            name = nm.get(f"{{{W}}}val") if nm is not None else s.get(f"{{{W}}}styleId")
            if name:
                info.style_names.append(name)
                if re.match(r"^heading\s*\d$", name.strip(), re.I):
                    info.heading_styles.append(name)
        dd = root.find(f"{{{W}}}docDefaults")
        if dd is not None:
            rf = dd.find(f".//{{{W}}}rFonts")
            if rf is not None:
                info.default_font = rf.get(f"{{{W}}}ascii") or ""
            sz = dd.find(f".//{{{W}}}sz")
            if sz is not None:
                try:
                    info.default_size_pt = int(sz.get(f"{{{W}}}val")) / 2
                except (TypeError, ValueError):
                    pass

    if doc_xml:
        root = etree.fromstring(doc_xml)
        for sect in root.iter(f"{{{W}}}sectPr"):
            pg = sect.find(f"{{{W}}}pgSz")
            if pg is not None and not info.page_size:
                w = pg.get(f"{{{W}}}w")
                info.page_size = "A4" if w == "11906" else (
                    "Letter" if w == "12240" else f"{_twips_to_mm(w):g} mm wide")
            mar = sect.find(f"{{{W}}}pgMar")
            if mar is not None and not info.margins_mm:
                info.margins_mm = {
                    k: _twips_to_mm(mar.get(f"{{{W}}}{k}"))
                    for k in ("top", "bottom", "left", "right")
                }
            cols = sect.find(f"{{{W}}}cols")
            n = 1
            if cols is not None:
                try:
                    n = int(cols.get(f"{{{W}}}num") or 1)
                except ValueError:
                    n = 1
            info.columns.append(n)
    return info


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class TemplateApplier:
    def __init__(self, source_docx: str | Path, template_docx: str | Path,
                 ms: Manuscript | None = None):
        self.src = Path(source_docx)
        self.tpl = Path(template_docx)
        self.ms = ms
        self.notes: list[str] = []
        self.unsupported: list[str] = []
        self._merged = 0
        self._mapped = 0
        self._source_for_body = self.src

    def apply(self, out_path: str | Path, *, take_page_setup: bool = True,
              map_paragraphs: bool = True,
              strip_furniture: bool = True) -> ApplyResult:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.chmod(out.stat().st_mode | stat.S_IWUSR)
            out.unlink()

        source = self.src
        tmp_clean: Path | None = None
        if strip_furniture:
            # Adopting a new journal's styles does not remove the old journal's
            # logo, ISSN line or running citation header - those are content.
            import tempfile  # noqa: PLC0415

            from . import cleanup  # noqa: PLC0415

            tmp_clean = Path(tempfile.mkdtemp()) / "clean.docx"
            res = cleanup.clean(
                self.src, tmp_clean,
                title=self.ms.meta.title if self.ms else "",
            )
            source = tmp_clean
            self.notes.extend(res.notes)
            if res.removed_paragraphs:
                self.notes.append(
                    f"{len(res.removed_paragraphs)} boilerplate paragraph(s) "
                    "removed from the previous journal's template.")
            if res.removed_images:
                self.notes.append(f"{res.removed_images} masthead image(s) removed.")

        self._source_for_body = source

        with zipfile.ZipFile(self.tpl) as tz:
            tpl_styles = _read(tz, _STYLES)
            tpl_theme = _read(tz, _THEME)
            tpl_numbering = _read(tz, _NUMBERING)
            tpl_doc = _read(tz, _DOCUMENT)

        if not tpl_styles:
            raise ValueError(f"{self.tpl.name} has no word/styles.xml - "
                             "it does not look like a Word template.")

        tpl_sectpr = self._last_sectpr(tpl_doc) if (take_page_setup and tpl_doc) else None

        # Rewrite the package part by part. python-docx cannot replace whole
        # parts, so we operate on the zip directly.
        with zipfile.ZipFile(self._source_for_body) as sz:
            names = sz.namelist()
            payload: dict[str, bytes] = {n: sz.read(n) for n in names}

        payload[_STYLES], tpl_names = self._merge_styles(
            payload.get(_STYLES), tpl_styles)
        if tpl_theme:
            payload[_THEME] = tpl_theme
            self.notes.append("Theme fonts and colours taken from the template.")
        if tpl_numbering:
            payload[_NUMBERING] = tpl_numbering
            self.notes.append("List and heading numbering taken from the template.")

        doc_xml = payload.get(_DOCUMENT)
        if doc_xml:
            payload[_DOCUMENT] = self._rewrite_document(
                doc_xml, tpl_sectpr, tpl_names if map_paragraphs else {}
            )

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as oz:
            for n in names:
                oz.writestr(n, payload[n])
            for extra in (_THEME, _NUMBERING):
                if extra not in names and extra in payload:
                    oz.writestr(extra, payload[extra])

        return ApplyResult(out, self._merged, self._mapped, self.notes,
                           self.unsupported)

    # -- styles ------------------------------------------------------------

    @staticmethod
    def _style_name(s: etree._Element) -> str:
        nm = s.find(f"{{{W}}}name")
        val = nm.get(f"{{{W}}}val") if nm is not None else None
        return (val or s.get(f"{{{W}}}styleId") or "").strip().lower()

    def _merge_styles(self, target_xml: bytes | None,
                      tpl_xml: bytes) -> tuple[bytes, dict[str, str]]:
        """Template definitions win; styles unique to the manuscript survive.

        Matching is by style **name**, not styleId. Word only guarantees that
        ids are unique within a document, and documents produced by non-English
        Word or by converters use opaque ids -- one real manuscript here used
        `a`, `1`, `2`, ... so an id-keyed merge matched nothing and the
        template's `Normal` was appended as a second, unused style while the
        body kept its original formatting.

        When a match is found the template's definition is adopted but the
        *manuscript's* styleId is kept, because every paragraph in the body
        already points at that id. References between styles (basedOn, next,
        link) are remapped into the manuscript's id space for the same reason.

        Returns the merged XML and a name -> final-styleId map for paragraph
        mapping.
        """
        import copy  # noqa: PLC0415

        tpl_root = etree.fromstring(tpl_xml)
        tpl_styles = tpl_root.findall(f"{{{W}}}style")

        if not target_xml:
            self._merged = len(tpl_styles)
            return (etree.tostring(tpl_root, xml_declaration=True,
                                   encoding="UTF-8", standalone=True),
                    {self._style_name(s): s.get(f"{{{W}}}styleId")
                     for s in tpl_styles})

        root = etree.fromstring(target_xml)
        tgt_styles = root.findall(f"{{{W}}}style")
        tgt_by_name = {self._style_name(s): s for s in tgt_styles}
        tgt_ids = {s.get(f"{{{W}}}styleId") for s in tgt_styles}

        # docDefaults carries the document-wide font and size.
        tpl_dd = tpl_root.find(f"{{{W}}}docDefaults")
        if tpl_dd is not None:
            old_dd = root.find(f"{{{W}}}docDefaults")
            if old_dd is not None:
                root.replace(old_dd, copy.deepcopy(tpl_dd))
            else:
                root.insert(0, copy.deepcopy(tpl_dd))
            self.notes.append("Document defaults (font, size, spacing) taken "
                              "from the template.")

        # Pass 1: decide the final id for every template style.
        id_map: dict[str, str] = {}
        for s in tpl_styles:
            tpl_id = s.get(f"{{{W}}}styleId")
            match = tgt_by_name.get(self._style_name(s))
            if match is not None:
                id_map[tpl_id] = match.get(f"{{{W}}}styleId")
            else:
                new_id = tpl_id
                while new_id in tgt_ids and new_id not in id_map.values():
                    new_id = f"{new_id}X"     # avoid colliding with a different style
                id_map[tpl_id] = new_id

        # Pass 2: install them, remapping inter-style references.
        added = replaced = 0
        for s in tpl_styles:
            tpl_id = s.get(f"{{{W}}}styleId")
            final_id = id_map[tpl_id]
            new = copy.deepcopy(s)
            new.set(f"{{{W}}}styleId", final_id)
            for tag in ("basedOn", "next", "link"):
                el = new.find(f"{{{W}}}{tag}")
                if el is None:
                    continue
                ref = el.get(f"{{{W}}}val")
                if ref in id_map:
                    el.set(f"{{{W}}}val", id_map[ref])
                else:
                    new.remove(el)      # dangling reference: drop it

            match = tgt_by_name.get(self._style_name(s))
            if match is not None:
                # Preserve the manuscript's default-style flag.
                if match.get(f"{{{W}}}default"):
                    new.set(f"{{{W}}}default", match.get(f"{{{W}}}default"))
                root.replace(match, new)
                replaced += 1
            else:
                root.append(new)
                added += 1

        self._merged = added + replaced
        self.notes.append(
            f"{replaced} style(s) overridden by the template, {added} added, "
            f"{len(tgt_styles) - replaced} manuscript-only style(s) kept."
        )
        if replaced == 0 and tgt_styles:
            self.unsupported.append(
                "No style in the manuscript matched one in the template by name, "
                "so the template could only add styles. Body text keeps its "
                "current look apart from the document defaults."
            )

        name_to_id = {self._style_name(s): id_map[s.get(f"{{{W}}}styleId")]
                      for s in tpl_styles}
        return (etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                               standalone=True),
                name_to_id)

    # -- document ----------------------------------------------------------

    @staticmethod
    def _last_sectpr(doc_xml: bytes) -> etree._Element | None:
        root = etree.fromstring(doc_xml)
        body = root.find(f"{{{W}}}body")
        if body is None:
            return None
        sects = list(body.iter(f"{{{W}}}sectPr"))
        return sects[-1] if sects else None

    def _rewrite_document(self, doc_xml: bytes, tpl_sectpr: etree._Element | None,
                          tpl_names: dict[str, str]) -> bytes:
        root = etree.fromstring(doc_xml)
        body = root.find(f"{{{W}}}body")
        if body is None:
            return doc_xml

        if tpl_sectpr is not None:
            self._copy_page_setup(body, tpl_sectpr)
        if tpl_names and self.ms is not None:
            self._map_paragraphs(body, tpl_names)

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                              standalone=True)

    def _copy_page_setup(self, body: etree._Element,
                         tpl_sectpr: etree._Element) -> None:
        """Copy page size, margins and column layout from the template.

        Column counts are copied only when the manuscript is uniformly
        single-column. If it already varies, the author has a title block
        spanning the page and a multi-column body, and overwriting that
        collapses the title into one column.
        """
        want_cols = tpl_sectpr.find(f"{{{W}}}cols")
        sects = list(body.iter(f"{{{W}}}sectPr"))
        counts = []
        for s in sects:
            c = s.find(f"{{{W}}}cols")
            try:
                counts.append(int(c.get(f"{{{W}}}num")) if c is not None
                              and c.get(f"{{{W}}}num") else 1)
            except ValueError:
                counts.append(1)
        uniform = len(set(counts)) <= 1

        import copy  # noqa: PLC0415

        for s in sects:
            for tag in ("pgSz", "pgMar"):
                tpl_el = tpl_sectpr.find(f"{{{W}}}{tag}")
                if tpl_el is None:
                    continue
                old = s.find(f"{{{W}}}{tag}")
                new = copy.deepcopy(tpl_el)
                if old is not None:
                    s.replace(old, new)
                else:
                    s.append(new)
            if uniform and want_cols is not None:
                old = s.find(f"{{{W}}}cols")
                new = copy.deepcopy(want_cols)
                if old is not None:
                    s.replace(old, new)
                else:
                    s.append(new)

        self.notes.append("Page size and margins taken from the template.")
        if uniform and want_cols is not None:
            self.notes.append(
                f"Column layout taken from the template "
                f"({want_cols.get(f'{{{W}}}num') or 1} column(s))."
            )
        elif not uniform:
            self.notes.append(
                "Column counts left alone: the manuscript already has a "
                "full-width title block and a multi-column body."
            )

    def _map_paragraphs(self, body: etree._Element,
                        tpl_names: dict[str, str]) -> None:
        """Point headings, captions and known sections at the template's styles."""
        def find_style(*hints: str) -> str | None:
            for h in hints:
                if h in tpl_names:
                    return tpl_names[h]
            for h in hints:
                for name, sid in tpl_names.items():
                    if h in name:
                        return sid
            return None

        headings = {}
        for sec in self.ms.iter_sections():
            if sec.title_raw:
                headings[_norm(sec.title_raw)] = sec

        caption_sid = find_style(*_CAPTION_HINTS)
        title_sid = find_style(*_TITLE_HINTS)
        role_sid = {
            role: find_style(*hints)
            for role, hints in _ROLE_STYLE_HINTS.items()
        }
        heading_sid = {
            n: find_style(f"heading {n}", f"heading{n}", f"titre {n}")
            for n in range(1, 5)
        }

        title_text = _norm(self.ms.meta.title)
        seen_title = False

        for p in body.iter(f"{{{W}}}p"):
            text = _norm("".join(t.text or "" for t in p.iter(f"{{{W}}}t")))
            if not text:
                continue

            sid = None
            if not seen_title and title_sid and text == title_text:
                sid, seen_title = title_sid, True
            elif text in headings:
                lvl = min(max(headings[text].level, 1), 4)
                sid = heading_sid.get(lvl)
                role = headings[text].role
                if role in role_sid and role_sid[role]:
                    sid = role_sid[role]
            elif re.match(r"^\s*(fig(ure)?|tab(le)?)\s*\.?\s*[A-Z]?\d+", text, re.I):
                sid = caption_sid

            if not sid:
                continue

            ppr = p.find(f"{{{W}}}pPr")
            if ppr is None:
                ppr = p.makeelement(f"{{{W}}}pPr", {})
                p.insert(0, ppr)
            style = ppr.find(f"{{{W}}}pStyle")
            if style is None:
                style = ppr.makeelement(f"{{{W}}}pStyle", {})
                ppr.insert(0, style)
            style.set(f"{{{W}}}val", sid)
            self._mapped += 1

        missing = [k for k, v in
                   {"Title": title_sid, "Caption": caption_sid,
                    "Heading 1": heading_sid.get(1)}.items() if not v]
        if missing:
            self.unsupported.append(
                "The template does not define: " + ", ".join(missing)
                + ". Those elements keep their existing formatting."
            )
        self.notes.append(f"{self._mapped} paragraph(s) mapped onto template styles.")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def apply_template(source_docx: str | Path, template_docx: str | Path,
                   out_path: str | Path, ms: Manuscript | None = None,
                   *, take_page_setup: bool = True,
                   map_paragraphs: bool = True,
                   strip_furniture: bool = True) -> ApplyResult:
    return TemplateApplier(source_docx, template_docx, ms).apply(
        out_path, take_page_setup=take_page_setup,
        map_paragraphs=map_paragraphs, strip_furniture=strip_furniture,
    )
