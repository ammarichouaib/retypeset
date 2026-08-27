"""
retypeset.audit -- fidelity verification.

The parser's own `issues` list reports what it *knows* it did badly. This module
answers the harder question: did anything silently vanish? It does so by
counting primitives directly in the OOXML (the ground truth) and comparing
against the IR.

Any renderer built on top of this must never be trusted until this report is
clean, because a missing equation in a submitted manuscript is unrecoverable.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .ir import Manuscript, SectionRole

_NS_MATH = re.compile(r"<m:oMath[\s>]")
_NS_MATH_PARA = re.compile(r"<m:oMathPara[\s>]")
_TBL = re.compile(r"<w:tbl>")
_DRAWING = re.compile(r"<w:drawing[\s>]")
_PICT = re.compile(r"<w:pict[\s>]")
_OLE = re.compile(r"<o:OLEObject[\s>]")
_MATHTYPE = re.compile(r"Equation\.(DSMT4|3)")
_PARA = re.compile(r"<w:p[\s>]")
_FOOTNOTE_REF = re.compile(r"<w:footnoteReference[\s>]")
_CSL_FIELD = re.compile(r"CSL_CITATION|EN\.CITE|ZOTERO_ITEM|Mendeley")


def _docx_ground_truth(path: str | Path) -> dict:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        doc = z.read("word/document.xml").decode("utf-8", "ignore")
        try:
            rels_xml = z.read("word/_rels/document.xml.rels").decode("utf-8", "ignore")
        except KeyError:
            rels_xml = ""
        media = [n for n in names if n.startswith("word/media/")]

    # Authoritative image count: <pic:pic> elements, i.e. actual pictures.
    #
    # Two wrong ways to count this, both of which we tried:
    #   * <w:drawing> over-reports -- a drawing may be a chart, a shape or an
    #     empty grouping container with no picture in it.
    #   * relationship references over-report -- Word 2016+ attaches an SVG
    #     original to a raster fallback, so one picture consumes two rels and
    #     the parser looks as though it lost images it never had.
    pictures = len(re.findall(r"<pic:pic[\s>]", doc)) + len(re.findall(r"<v:imagedata[\s>]", doc))
    image_rids = set(re.findall(r'Id="(rId\d+)"[^>]*Target="[^"]*media/', rels_xml))
    svg_rels = len(re.findall(r"<asvg:svgBlip", doc))

    return {
        "image_references": pictures,
        "media_relationships": len(image_rids),
        "svg_vector_originals": svg_rels,
        "omml_inline": len(_NS_MATH.findall(doc)),
        "omml_display": len(_NS_MATH_PARA.findall(doc)),
        "tables": len(_TBL.findall(doc)),
        "drawings": len(_DRAWING.findall(doc)),
        "pict": len(_PICT.findall(doc)),
        "ole_objects": len(_OLE.findall(doc)),
        "mathtype_objects": len(_MATHTYPE.findall(doc)),
        "paragraphs": len(_PARA.findall(doc)),
        "footnote_refs": len(_FOOTNOTE_REF.findall(doc)),
        "citation_fields": len(_CSL_FIELD.findall(doc)),
        "media_files": len(media),
        "media_names": [Path(n).name for n in media],
    }


def audit(ms: Manuscript, docx_path: str | Path) -> dict:
    gt = _docx_ground_truth(docx_path)

    # Inline math must be counted recursively: it hides in table cells and list
    # items, and an earlier version of this check walked only top-level section
    # blocks -- which made a manuscript with 69 equations inside numbering
    # tables look as though half its mathematics had been lost.
    ir_math = len(ms.equations) + _count_inline_math(ms)

    # Tables the parser deliberately reclassified are accounted for, not lost:
    # equation-numbering layouts became display equations, and empty layout
    # tables were removed. Both must be added back before comparing with the
    # OOXML count, or the audit reports a loss it caused on purpose.
    eq_tables = sum(1 for i in ms.issues if i.code == "equation_table")
    dropped_layout = 0
    for i in ms.issues:
        if i.code == "layout_table_dropped":
            m = re.match(r"\s*(\d+)", i.message)
            if m:
                dropped_layout += int(m.group(1))
    checks = [
        _check("Equations (OMML)", gt["omml_inline"], ir_math,
               "Every OMML node must survive as either a display Equation or an "
               "inline math run."),
        _check("Tables", gt["tables"], len(ms.tables) + eq_tables + dropped_layout,
               "Includes equation-numbering layouts (stored as display "
               "equations) and empty layout tables (removed)."),
        _check("Embedded images", gt["image_references"],
               sum(len(f.files) for f in ms.figures),
               "Counted as <pic:pic> elements. retypeset.oox is the authoritative "
               "source; Pandoc under-reports."),
    ]

    blocking = []
    if gt["mathtype_objects"]:
        blocking.append(
            f"{gt['mathtype_objects']} MathType/Equation-3.0 OLE object(s) detected. "
            "Pandoc cannot convert these to LaTeX - they are pictures. Convert them "
            "in Word (MathType > Convert Equations > Office Math) before parsing."
        )
    if gt["citation_fields"] == 0 and ms.references:
        blocking.append(
            "Bibliography is hand-typed (no Zotero/Mendeley/EndNote field codes). "
            "Automatic reference restyling will rely on regex parsing; re-linking "
            "the bibliography in a reference manager is strongly recommended."
        )
    bad_fmt = [f.id for f in ms.figures if f.needs_conversion]
    if bad_fmt:
        blocking.append(
            f"{len(bad_fmt)} figure(s) in EMF/WMF/TIFF: {', '.join(bad_fmt[:8])}. "
            "These cannot be placed by pdfLaTeX and must be converted."
        )

    coverage = {
        "has_title": bool(ms.meta.title),
        "has_authors": bool(ms.meta.authors),
        "has_affiliations": bool(ms.meta.affiliations),
        "has_abstract": bool(ms.meta.abstract_raw),
        "has_keywords": bool(ms.meta.keywords),
        "has_references": bool(ms.references),
        "roles_resolved": sum(
            1 for s in ms.body if s.role is not SectionRole.UNKNOWN
        ),
        "roles_total": len(ms.body),
    }

    return {
        "ground_truth": gt,
        "checks": checks,
        "blocking": blocking,
        "coverage": coverage,
        "stats": ms.stats,
        "ready_to_render": not blocking and all(c["ok"] for c in checks),
    }


def _count_inline_math(ms: Manuscript) -> int:
    """Inline math runs anywhere in the document, including nested containers."""
    total = 0

    def in_block(b) -> int:
        n = 0
        if b.paragraph:
            n += sum(1 for x in b.paragraph.inlines if x.kind == "math")
        if b.list_block:
            for item in b.list_block.items:
                for sub in item:
                    n += in_block(sub)
        for sub in b.quote_blocks:
            n += in_block(sub)
        return n

    for s in ms.iter_sections():
        for b in s.blocks:
            total += in_block(b)
    for t in ms.tables:
        for row in t.grid:
            for cell in row:
                for b in cell.blocks:
                    total += in_block(b)
    return total


def _check(name: str, expected: int, got: int, note: str = "") -> dict:
    ok = got >= expected if expected else True
    return {
        "name": name, "source": expected, "ir": got, "ok": ok,
        "delta": got - expected, "note": note,
    }


def format_report(report: dict, ms: Manuscript) -> str:
    L: list[str] = []
    add = L.append

    add("=" * 74)
    add(f"FIDELITY AUDIT - {ms.meta.source_file}")
    add("=" * 74)

    add("\n-- Retention (source OOXML vs. parsed IR) " + "-" * 31)
    add(f"{'item':<26}{'source':>8}{'IR':>8}{'delta':>8}   status")
    for c in report["checks"]:
        add(f"{c['name']:<26}{c['source']:>8}{c['ir']:>8}{c['delta']:>+8}   "
            f"{'OK' if c['ok'] else 'LOSS'}")

    s = report["stats"]
    add("\n-- Parsed content " + "-" * 55)
    add(f"words {s['words']:<8} paragraphs {s['paragraphs']:<6} sections {s['sections']:<5} "
        f"(top level {s['top_level_sections']})")
    add(f"equations {s['equations']:<5} figures {s['figures']:<8} tables {s['tables']:<7} "
        f"references {s['references']}")

    cov = report["coverage"]
    add("\n-- Front matter " + "-" * 57)
    for k in ("has_title", "has_authors", "has_affiliations", "has_abstract",
              "has_keywords", "has_references"):
        add(f"  [{'x' if cov[k] else ' '}] {k[4:]}")
    add(f"  section roles resolved: {cov['roles_resolved']}/{cov['roles_total']}")

    add("\n-- Section tree " + "-" * 57)
    for sec in ms.body:
        title = (sec.title_raw or "(untitled preamble)")[:52]
        add(f"  {'  ' * (sec.level - 1)}L{sec.level} {title:<54} -> {sec.role.value}")
        for ch in sec.children[:12]:
            add(f"    {'  ' * ch.level}L{ch.level} {(ch.title_raw or '')[:48]:<50} -> {ch.role.value}")

    if report["blocking"]:
        add("\n-- BLOCKING (must be fixed before rendering) " + "-" * 28)
        for b in report["blocking"]:
            add(f"  ! {b}")

    errs = [i for i in ms.issues if i.severity == "error"]
    warns = [i for i in ms.issues if i.severity == "warning"]
    infos = [i for i in ms.issues if i.severity == "info"]
    for label, items in (("ERRORS", errs), ("WARNINGS", warns)):
        if items:
            add(f"\n-- {label} " + "-" * (69 - len(label)))
            seen: set[str] = set()
            for i in items:
                key = i.code + i.message[:60]
                if key in seen:
                    continue
                seen.add(key)
                add(f"  [{i.code}] {i.message}")
    if infos:
        add(f"\n-- INFO ({len(infos)}) " + "-" * 56)
        seen = set()
        for i in infos:
            if i.code in seen:
                continue
            seen.add(i.code)
            n = sum(1 for x in infos if x.code == i.code)
            add(f"  [{i.code}] x{n}: {i.message}")

    add("\n" + "=" * 74)
    add("VERDICT: " + ("ready to render"
                       if report["ready_to_render"]
                       else "NOT ready - resolve blocking items above"))
    add("=" * 74)
    return "\n".join(L)
