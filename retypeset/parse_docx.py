"""
retypeset.parse_docx -- deterministic DOCX -> IR parser.

Strategy
--------
Pandoc is the reader. It is the only mature open-source implementation of
OMML -> LaTeX math conversion, and it also gives us tables, media extraction,
footnotes and lists for free. We consume its AST as JSON rather than its
Markdown output, because Markdown is lossy for exactly the things we care about
(cell spans, image identity, math display mode).

Everything after Pandoc is our own normalisation, and it is entirely
rule-based -- no model is called anywhere in this module. That is deliberate:
a parser that produces different output on two runs of the same file cannot be
used for manuscripts.

Known deliberate simplifications, each of which raises a ParseIssue rather than
failing silently:
  * Word BlockQuote artefacts (indented paragraphs) are unwrapped.
  * Equation numbers are recovered from paragraph text, not from Word's
    numbering engine.
  * References are parsed with regex into partial CSL-JSON; `raw` is always kept.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

from . import oox
from .ir import (
    Affiliation,
    Author,
    Block,
    Equation,
    Figure,
    Footnote,
    InlineNode,
    ListBlock,
    Manuscript,
    Metadata,
    Paragraph,
    ParseIssue,
    Provenance,
    Reference,
    Section,
    SectionRole,
    Table,
    TableCell,
)

# ---------------------------------------------------------------------------
# Section-role lexicon
# ---------------------------------------------------------------------------
# Ordered longest-first at match time so that "results and discussion" wins over
# "results". Patterns are matched against the heading title with numbering and
# punctuation stripped and case-folded.

_ROLE_PATTERNS: list[tuple[SectionRole, str]] = [
    (SectionRole.RESULTS_DISCUSSION, r"^results?\s*(and|&|/)\s*discussions?$"),
    (SectionRole.ABSTRACT, r"^(abstract|summary|graphical abstract)$"),
    (SectionRole.KEYWORDS, r"^(key\s*words?|index terms?)$"),
    (SectionRole.HIGHLIGHTS, r"^highlights$"),
    (SectionRole.NOMENCLATURE, r"^(nomenclature|notation|abbreviations?|list of symbols?)$"),
    (SectionRole.INTRODUCTION, r"^(introduction|background|general introduction)$"),
    (SectionRole.RELATED_WORK, r"^(related works?|literature review|state of the art|prior art)$"),
    (SectionRole.THEORY, r"^(theory|theoretical (background|framework|analysis)|mathematical (model|formulation)|modell?ing|system (model|description|modell?ing))$"),
    (SectionRole.METHODS, r"^(methods?|methodolog(y|ies)|materials? and methods?|proposed (method|approach|methodology|algorithm|technique|model)|problem formulation|optimi[sz]ation)$"),
    (SectionRole.EXPERIMENTAL, r"^(experimental( setup| section| procedure| study| validation)?|simulation (setup|environment)|case stud(y|ies)|test bench)$"),
    (SectionRole.RESULTS, r"^(results?|simulation results?|numerical results?|experimental results?|findings)$"),
    (SectionRole.DISCUSSION, r"^(discussions?|analysis and discussion)$"),
    (SectionRole.CONCLUSION, r"^(conclusions?|concluding remarks|conclusions? and (perspectives?|future works?|outlook)|summary and conclusions?)$"),
    (SectionRole.FUTURE_WORK, r"^(future works?|perspectives?|outlook|recommendations?)$"),
    (SectionRole.ACKNOWLEDGEMENTS, r"^acknowledge?ments?$"),
    (SectionRole.FUNDING, r"^(funding|financial support|funding (statement|information))$"),
    (SectionRole.CONFLICT_OF_INTEREST, r"^(conflicts? of interest|declaration of (competing )?interests?|competing interests?|disclosure)$"),
    (SectionRole.AUTHOR_CONTRIBUTIONS, r"^(author (contributions?|statement)|credit authorship contribution statement)$"),
    (SectionRole.DATA_AVAILABILITY, r"^data availability( statement)?$"),
    (SectionRole.ETHICS, r"^(ethics? (approval|statement)|ethical (approval|considerations?)|informed consent)$"),
    (SectionRole.APPENDIX, r"^(appendix|appendices|appendix [a-z0-9]+.*|supplementary (material|information))$"),
    (SectionRole.REFERENCES, r"^(references?|bibliography|literature cited|works cited)$"),
]

_ROLE_RE = [(role, re.compile(pat, re.I)) for role, pat in _ROLE_PATTERNS]

# Leading section numbering: "3", "3.2", "IV.", "A.2", "(3)"
_NUMBERING_RE = re.compile(
    r"^\s*\(?((?:\d+|[IVXLC]+|[A-Z])(?:[.\-]\d+)*)\)?[.)]?\s+", re.U
)

_CAPTION_RE = re.compile(
    r"^\s*(?P<kind>fig(?:ure)?|tab(?:le)?|scheme|chart)\s*\.?\s*"
    r"(?P<num>[A-Z]?\d+(?:\.\d+)?)\s*[.:\-–—]?\s*(?P<rest>.*)$",
    re.I | re.S,
)

# Trailing equation number on a display-equation paragraph: "(3)" / "(A.2)"
_EQNUM_RE = re.compile(r"^\(?\s*([A-Z]?\.?\d+(?:\.\d+)?)\s*\)?$")

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;\"<>]+)", re.I)
_URL_RE = re.compile(r"https?://[^\s,;\"<>]+", re.I)
_YEAR_RE = re.compile(r"\((\d{4}[a-z]?)\)|\b(19|20)\d{2}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b")
# Leading marker of a reference list entry: "[12]", "12.", "12)"
_REF_MARKER_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})\s*[.)])\s+")

_RASTER_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
_VECTOR_EXT = {".emf", ".wmf", ".eps", ".pdf", ".svg"}
# Formats that pdflatex cannot include and that Word renders unpredictably.
_NEEDS_CONVERSION_EXT = {".emf", ".wmf", ".tif", ".tiff", ".bmp", ".gif"}


# ---------------------------------------------------------------------------
# Pandoc invocation
# ---------------------------------------------------------------------------

class PandocError(RuntimeError):
    pass


def _candidate_pandoc_paths() -> Iterable[str]:
    """Places a pandoc binary may live, in order of preference.

    A system install is preferred, but the common failure on Windows is that
    the MSI installs to a per-user directory that is not on PATH in an already
    open terminal. Rather than make the user restart their shell, we look in
    the standard install locations and finally fall back to the copy bundled
    with `pypandoc_binary`, which needs no admin rights.
    """
    if os.environ.get("PANDOC"):
        yield os.environ["PANDOC"]

    found = shutil.which("pandoc")
    if found:
        yield found

    local = os.environ.get("LOCALAPPDATA") or ""
    program_files = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    for base in (
        Path(local) / "Pandoc" if local else None,
        Path(program_files) / "Pandoc",
        Path(r"C:\Program Files (x86)\Pandoc"),
        Path.home() / "AppData" / "Local" / "Pandoc",
        Path("/usr/local/bin"), Path("/usr/bin"), Path("/opt/homebrew/bin"),
    ):
        if base is None:
            continue
        for name in ("pandoc.exe", "pandoc"):
            p = base / name
            if p.exists():
                yield str(p)

    try:
        import pypandoc  # noqa: PLC0415

        p = pypandoc.get_pandoc_path()
        if p:
            yield p
    except Exception:
        pass


_PANDOC_CACHE: str | None = None


def _pandoc_bin() -> str:
    global _PANDOC_CACHE
    if _PANDOC_CACHE:
        return _PANDOC_CACHE
    for cand in _candidate_pandoc_paths():
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True)
            if r.returncode == 0 and "pandoc" in r.stdout.lower():
                _PANDOC_CACHE = cand
                return cand
        except OSError:
            continue
    raise PandocError(
        "pandoc not found.\n"
        "  Easiest fix (no admin rights, no PATH changes):\n"
        "      pip install pypandoc_binary\n"
        "  Or install pandoc from https://pandoc.org/installing.html and open a NEW\n"
        "  terminal so the updated PATH takes effect.\n"
        "  Or point retypeset at an existing binary:\n"
        "      set PANDOC=C:\\Program Files\\Pandoc\\pandoc.exe"
    )


def pandoc_version() -> tuple[int, ...]:
    out = subprocess.run(
        [_pandoc_bin(), "--version"], capture_output=True, text=True, check=True
    ).stdout
    m = re.search(r"pandoc(?:\.exe)?\s+(\d+(?:\.\d+)*)", out)
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def _run_pandoc(docx_path: Path) -> dict[str, Any]:
    """Read the DOCX with Pandoc and return its AST.

    Media is extracted to a throwaway directory: retypeset.oox owns asset extraction
    because Pandoc drops images silently.
    """
    with tempfile.TemporaryDirectory() as td:
        ast_path = Path(td) / "ast.json"
        cmd = [
            _pandoc_bin(),
            "-f", "docx",
            "-t", "json",
            f"--extract-media={Path(td) / 'media'}",
            str(docx_path),
            "-o", str(ast_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise PandocError(f"pandoc failed:\n{proc.stderr.strip()}")
        return json.loads(ast_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AST -> inline nodes
# ---------------------------------------------------------------------------

def _inlines(nodes: Iterable[dict], style: dict | None = None) -> list[InlineNode]:
    """Flatten a Pandoc inline list into IR inline nodes.

    Formatting marks (Strong/Emph/Super/Sub) are pushed down onto leaf text
    nodes rather than kept as a tree; nothing downstream needs the nesting and
    a flat run list is far easier for both renderers to consume.
    """
    style = style or {}
    out: list[InlineNode] = []

    def leaf(text: str, kind: str = "text", **kw) -> None:
        if not text and kind == "text":
            return
        out.append(InlineNode(kind=kind, text=text, **{**style, **kw}))

    for n in nodes:
        t = n.get("t")
        c = n.get("c")
        if t == "Str":
            leaf(c)
        elif t == "Space":
            leaf(" ")
        elif t in ("SoftBreak", "LineBreak"):
            out.append(InlineNode(kind="break"))
        elif t == "Strong":
            out.extend(_inlines(c, {**style, "bold": True}))
        elif t == "Emph":
            out.extend(_inlines(c, {**style, "italic": True}))
        elif t == "Superscript":
            out.extend(_inlines(c, {**style, "superscript": True}))
        elif t == "Subscript":
            out.extend(_inlines(c, {**style, "subscript": True}))
        elif t == "SmallCaps":
            out.extend(_inlines(c, {**style, "smallcaps": True}))
        elif t == "Strikeout" or t == "Underline":
            out.extend(_inlines(c, style))
        elif t == "Code":
            leaf(c[1], code=True)
        elif t == "Math":
            display = c[0]["t"] == "DisplayMath"
            out.append(InlineNode(kind="math", text=c[1], superscript=display))
            # `superscript` is reused as a transient display flag here and is
            # cleared in _promote_display_math; see that function.
        elif t == "Link":
            url = c[2][0] if len(c) > 2 else ""
            inner = _inlines(c[1], style)
            text = "".join(i.text for i in inner)
            out.append(InlineNode(kind="link", text=text, url=url))
        elif t == "Image":
            # Images inside a paragraph are handled at block level; skip here.
            continue
        elif t == "Note":
            out.append(InlineNode(kind="footnote", footnote_id=""))
        elif t == "Span":
            out.extend(_inlines(c[1], style))
        elif t == "Quoted":
            q = "“" if c[0]["t"] == "DoubleQuote" else "‘"
            qe = "”" if c[0]["t"] == "DoubleQuote" else "’"
            leaf(q)
            out.extend(_inlines(c[1], style))
            leaf(qe)
        elif t == "Cite":
            inner = _inlines(c[1], style)
            out.append(InlineNode(kind="cite", text="".join(i.text for i in inner)))
        elif t == "RawInline":
            leaf(c[1])
        elif isinstance(c, list):
            out.extend(_inlines([x for x in c if isinstance(x, dict)], style))
    return out


def _images_in(nodes: Iterable[dict]) -> list[tuple[str, list[dict]]]:
    """Collect (path, caption_inlines) for every Image anywhere in a subtree."""
    found: list[tuple[str, list[dict]]] = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("t") == "Image":
                c = x["c"]
                found.append((c[2][0], c[1]))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(list(nodes))
    return found


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class DocxParser:
    def __init__(self, docx_path: str | Path, media_dir: str | Path | None = None):
        self.docx_path = Path(docx_path)
        self.media_dir = Path(media_dir) if media_dir else self.docx_path.with_suffix("") / "media"
        self.ms = Manuscript()
        self._eq_n = 0
        self._fig_n = 0
        self._tab_n = 0
        self._para_n = 0
        self._pending_footnotes: list[list[Block]] = []
        self.scan: oox.OoxScan | None = None
        # fig_id -> (caption_text, source paragraph index) used for placement
        self._fig_anchor: dict[str, tuple[str, int]] = {}

    # -- public ------------------------------------------------------------

    def parse(self) -> Manuscript:
        # Assets first, straight from the OOXML: Pandoc is known to drop images
        # without warning, so its media output is never authoritative.
        self.scan = oox.scan(self.docx_path)
        self._extract_all_media()
        self._build_figures_from_oox()

        ast = _run_pandoc(self.docx_path)
        self.ms.media_dir = str(self.media_dir)
        self.ms.meta.source_file = self.docx_path.name

        flat = self._flatten(ast["blocks"])
        flat = self._promote_display_math(flat)
        flat = self._place_figures(flat)
        flat = self._attach_captions(flat)
        self._build_sections(flat)
        self._classify_roles()
        self._extract_front_matter(ast)
        self._unwrap_title_wrapper()
        self._extract_references()
        self._audit_media()
        self._check_math_quality()
        self._drop_layout_tables()
        self._check_text_loss()
        self._collect_stats()
        return self.ms

    # -- stage 1: AST -> flat stream ---------------------------------------

    def _flatten(self, blocks: list[dict], depth: int = 0) -> list[dict]:
        """Produce a flat stream of tagged items: heading / block / float.

        Sectioning is deferred to stage 4 because heading *detection* needs the
        whole stream (a bold short paragraph is only a heading if the document
        contains several of them).
        """
        stream: list[dict] = []
        for b in blocks:
            t = b.get("t")
            c = b.get("c")

            if t == "Header":
                lvl, attr, inl = c
                stream.append({
                    "kind": "heading",
                    "level": lvl,
                    "inlines": _inlines(inl),
                    "raw": _plain(inl),
                    "source": "style",
                })

            elif t in ("Para", "Plain"):
                # Images are inventoried from the OOXML, not from Pandoc; here
                # we keep only the paragraph's text (often the caption).
                stream.append(self._para_item(_inlines(c)))

            elif t == "BlockQuote":
                # Word indentation, not a real quotation, in >95% of manuscripts.
                self._issue("info", "blockquote_unwrapped",
                            "Indented paragraph unwrapped from BlockQuote.")
                stream.extend(self._flatten(c, depth + 1))

            elif t == "Div":
                stream.extend(self._flatten(c[1], depth + 1))

            elif t in ("BulletList", "OrderedList"):
                items = c if t == "BulletList" else c[1]
                lb = ListBlock(
                    id=f"list{len(stream)}",
                    ordered=(t == "OrderedList"),
                    items=[self._blocks_from_stream(self._flatten(it, depth + 1)) for it in items],
                )
                stream.append({"kind": "block",
                               "block": Block(kind="list", list_block=lb)})

            elif t == "Table":
                stream.extend(self._make_table_item(b))

            elif t == "CodeBlock":
                stream.append({"kind": "block",
                               "block": Block(kind="code", code_text=c[1],
                                              code_lang=(c[0][1] or [""])[0] if c[0][1] else "")})

            elif t == "DefinitionList":
                for term, defs in c:
                    stream.append(self._para_item(_inlines(term)))
                    for d in defs:
                        stream.extend(self._flatten(d, depth + 1))

            elif t in ("HorizontalRule", "Null"):
                continue

            elif t == "RawBlock":
                stream.append({"kind": "block",
                               "block": Block(kind="raw", raw_text=c[1])})

            elif t == "LineBlock":
                for line in c:
                    stream.append(self._para_item(_inlines(line)))

            else:
                self._issue("warning", "unhandled_block",
                            f"Unhandled Pandoc block type '{t}'; content preserved as raw.")
                stream.append({"kind": "block",
                               "block": Block(kind="raw", raw_text=json.dumps(b)[:2000])})
        return stream

    def _para_item(self, inlines: list[InlineNode]) -> dict:
        self._para_n += 1
        p = Paragraph(id=f"p{self._para_n}", inlines=inlines)
        return {"kind": "block", "block": Block(kind="paragraph", paragraph=p),
                "text": p.plain_text()}

    # -- assets (OOXML ground truth) ---------------------------------------

    def _extract_all_media(self) -> None:
        """Copy every file in word/media to the media directory.

        Unconditional: an image Pandoc dropped is exactly the one we most need
        on disk, and unreferenced media costs a few hundred kilobytes.
        """
        self.media_dir.mkdir(parents=True, exist_ok=True)
        for name, data in self.scan.media_files.items():
            (self.media_dir / name).write_bytes(data)
        if self.scan.orphan_media:
            self._issue("info", "orphan_media",
                        f"{len(self.scan.orphan_media)} media file(s) present in the "
                        "package but not referenced in the document body "
                        f"({', '.join(self.scan.orphan_media[:5])}); usually header "
                        "or footer graphics.")

    def _build_figures_from_oox(self) -> None:
        """Create one Figure per paragraph that contains images.

        Several images in one paragraph are treated as panels of a single
        multi-panel figure, which is how Word authors compose (a)(b)(c) layouts.
        """
        by_para: dict[int, list[oox.ImageRef]] = {}
        for im in self.scan.images:
            by_para.setdefault(im.para_index, []).append(im)

        for pidx in sorted(by_para):
            refs = by_para[pidx]
            self._fig_n += 1
            fid = f"fig{self._fig_n}"
            cap_full, label, cap_body = oox.caption_for(self.scan, pidx, "fig")
            if cap_full and oox.looks_like_path(cap_full):
                cap_full = label = cap_body = ""
            widths = [r.width_mm for r in refs if r.width_mm]
            heights = [r.height_mm for r in refs if r.height_mm]
            fig = Figure(
                id=fid, number=self._fig_n,
                placed_width_mm=round(max(widths), 1) if widths else 0.0,
                placed_height_mm=round(max(heights), 1) if heights else 0.0,
                # Prefer the vector original where Word kept one; the raster is
                # only a display fallback and would fail resolution checks.
                files=[r.preferred for r in refs],
                label=label,
                caption_raw=cap_full,
                caption=[InlineNode(kind="text", text=cap_body)] if cap_body else [],
                provenance=Provenance(
                    method="explicit", confidence=1.0,
                    note=(f"{len(refs)} panels; " if len(refs) > 1 else "")
                         + (f"placed at {widths[0]:.0f} mm wide in the source"
                            if widths else "no size recorded"),
                ),
            )
            self.ms.figures.append(fig)
            self._fig_anchor[fid] = (cap_full, pidx)
            if not cap_full:
                self._issue("warning", "missing_caption",
                            f"{fid} ({', '.join(r.filename for r in refs)}) has no "
                            "detectable caption within 3 paragraphs.", fid)
            if any(r.anchored for r in refs):
                self._issue("info", "floating_image",
                            f"{fid} is a floating (anchored) image. Its position in the "
                            "reading order is approximate.", fid)

    def _place_figures(self, stream: list[dict]) -> list[dict]:
        """Insert figure anchors into the body stream.

        Preferred signal is the caption text, which survives in Pandoc's output
        even when the image itself did not. Failing that we fall back to
        proportional position, which is approximate and is flagged as such.
        """
        n_src = max(1, len(self.scan.paragraphs))

        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", s).strip().lower()

        # index the stream's paragraph texts once
        texts = [
            norm(it["block"].paragraph.plain_text())
            if it["kind"] == "block" and it["block"].kind == "paragraph"
            else None
            for it in stream
        ]

        # Index of stream paragraph text -> stream position, for anchoring.
        index: dict[str, int] = {}
        for i, t in enumerate(texts):
            if t and len(t) >= 25:
                index.setdefault(t[:60], i)

        def find_in_stream(probe: str) -> int | None:
            probe = norm(probe)
            if len(probe) < 25:
                return None
            hit = index.get(probe[:60])
            if hit is not None:
                return hit
            for i, t in enumerate(texts):
                if t and probe[:40] in t:
                    return i
            return None

        insertions: list[tuple[int, dict, int | None]] = []
        for fig in self.ms.figures:
            cap, pidx = self._fig_anchor[fig.id]
            pos = None
            consume = None

            # 1. Anchor on the caption itself, and absorb that paragraph so the
            #    caption is not also emitted as body text.
            if cap:
                probe = norm(cap)[:40]
                for i, t in enumerate(texts):
                    if t and probe and probe in t:
                        pos, consume = i, i
                        break

            # 2. Pandoc discards text-box content wholesale, so a caption may
            #    exist in the OOXML and be absent from the stream. Fall back to
            #    the nearest surrounding source paragraph that did survive.
            if pos is None:
                for back in range(1, 25):
                    j = pidx - back
                    if j < 0:
                        break
                    hit = find_in_stream(self.scan.paragraphs[j].text)
                    if hit is not None:
                        pos = hit + 1
                        break
            if pos is None:
                for fwd in range(1, 25):
                    j = pidx + fwd
                    if j >= len(self.scan.paragraphs):
                        break
                    hit = find_in_stream(self.scan.paragraphs[j].text)
                    if hit is not None:
                        pos = hit
                        break
                if pos is not None:
                    self._issue("info", "figure_anchored_by_neighbour",
                                f"{fig.id} anchored via a neighbouring paragraph "
                                "because its caption is not in Pandoc's output.",
                                fig.id)

            # 3. Last resort.
            if pos is None:
                pos = min(len(stream), max(0, round(pidx / n_src * len(stream))))
                self._issue("warning", "figure_position_estimated",
                            f"{fig.id} could not be anchored by caption or neighbouring "
                            "text; placed by proportional position. Verify its location.",
                            fig.id)
            insertions.append((pos, {
                "kind": "block",
                "block": Block(kind="figure_ref", target_id=fig.id),
                "float_id": fig.id, "float_type": "figure",
            }, consume))

        consumed = {c for _, _, c in insertions if c is not None}
        out: list[dict] = []
        by_pos: dict[int, list[dict]] = {}
        for pos, item, _ in insertions:
            by_pos.setdefault(pos, []).append(item)
        for i, it in enumerate(stream):
            for item in by_pos.get(i, []):
                out.append(item)
            if i not in consumed:
                out.append(it)
        for item in by_pos.get(len(stream), []):
            out.append(item)
        return out

    def _make_table_item(self, b: dict) -> list[dict]:
        """Build a table, or recognise it as an equation-numbering layout.

        Word has no native numbered-equation construct, so authors overwhelmingly
        use an invisible two-column table: equation on the left, "(n)" on the
        right. Treating those as tables is wrong in every downstream direction --
        the table count is inflated, the equations never become numbered display
        equations, caption checks fire on things that need no caption, and a
        renderer would emit a tabular where LaTeX wants \\begin{equation}.

        On a real 10 500-word manuscript this pattern accounted for 30 of 32
        "tables" and 69 of 134 equations.
        """
        c = b["c"]
        # Pandoc >=2.10 table: [attr, caption, colspecs, head, bodies, foot]
        if len(c) >= 6:
            caption_blocks = c[1][1]
            head_rows = c[3][1]
            # TableBody = [attr, rowHeadColumns, intermediate-head rows, body rows]
            body_rows = [r for body in (c[4] or []) for r in (body[2] + body[3])]
            foot_rows = c[5][1] if len(c) > 5 else []
            rows = list(head_rows) + list(body_rows) + list(foot_rows)
            n_head = len(head_rows)
            grid = [self._row_cells(r) for r in rows]
            cap_raw = _plain_blocks(caption_blocks)
        else:
            # Pandoc <2.10 simple table: [caption, aligns, widths, headers, rows]
            cap_raw = _plain(c[0])
            headers, rows = c[3], c[4]
            grid = [[TableCell(blocks=self._blocks_from_stream(self._flatten(cell)))
                     for cell in headers]] if any(headers) else []
            n_head = 1 if grid else 0
            for r in rows:
                grid.append([TableCell(blocks=self._blocks_from_stream(self._flatten(cell)))
                             for cell in r])
        # Equation-numbering layout? Decided before a table id is allocated so
        # the table numbering stays correct.
        eq_rows = _as_equation_layout(grid)
        if eq_rows is not None and not cap_raw:
            self._issue("info", "equation_table",
                        f"A {len(eq_rows)}-row table was recognised as an "
                        "equation-numbering layout and converted to display "
                        "equations rather than a tabular.")
            return [{"kind": "eqtable", "rows": eq_rows}]

        self._tab_n += 1
        tid = f"tab{self._tab_n}"
        tbl = Table(id=tid, number=self._tab_n, header_rows=n_head,
                    grid=grid, caption_raw=cap_raw,
                    provenance=Provenance(method="explicit", confidence=1.0))
        self.ms.tables.append(tbl)
        return [{"kind": "block", "block": Block(kind="table_ref", target_id=tid),
                 "float_id": tid, "float_type": "table"}]

    def _row_cells(self, row: list) -> list[TableCell]:
        cells = []
        for cell in row[1]:
            # cell = [attr, alignment, rowspan, colspan, blocks]
            align = {"AlignLeft": "left", "AlignCenter": "center",
                     "AlignRight": "right"}.get(cell[1]["t"], "default")
            cells.append(TableCell(
                blocks=self._blocks_from_stream(self._flatten(cell[4])),
                rowspan=cell[2], colspan=cell[3], align=align,
            ))
        return cells

    def _blocks_from_stream(self, stream: list[dict]) -> list[Block]:
        return [it["block"] for it in stream if it["kind"] == "block"]

    def _rel_media(self, p: str) -> str:
        try:
            return str(Path(p).relative_to(self.media_dir))
        except ValueError:
            return str(Path(p).name if "media" not in p else Path(p).as_posix().split("media/", 1)[-1])

    # -- stage 2: display math ---------------------------------------------

    def _promote_display_math(self, stream: list[dict]) -> list[dict]:
        """Turn math-only paragraphs into numbered display equations.

        Word authors almost never use OMML's own display mode; they type an
        inline equation on its own line and add "(3)" by hand, usually after a
        tab. So the reliable signal is: the paragraph contains math and its
        non-math text is empty or is just an equation number.
        """
        out: list[dict] = []
        for item in stream:
            # Equation-numbering tables were deferred here so that their
            # equations receive ids in document order alongside the rest.
            if item["kind"] == "eqtable":
                for latex, number_raw in item["rows"]:
                    self._eq_n += 1
                    eq = Equation(
                        id=f"eq{self._eq_n}", latex=latex, display=True,
                        number=self._eq_n, number_raw=number_raw,
                        provenance=Provenance(
                            method="heuristic", confidence=0.95,
                            note="recovered from a Word equation-numbering table"),
                    )
                    self.ms.equations.append(eq)
                    out.append({"kind": "block",
                                "block": Block(kind="equation_ref", target_id=eq.id),
                                "float_id": eq.id, "float_type": "equation"})
                continue

            blk = item.get("block")
            if item["kind"] != "block" or not blk or blk.kind != "paragraph":
                # Clear the transient display flag on stray math nodes.
                out.append(item)
                continue

            para = blk.paragraph
            maths = [n for n in para.inlines if n.kind == "math"]
            if not maths:
                out.append(item)
                continue

            residue = "".join(
                n.text for n in para.inlines if n.kind != "math"
            ).replace(" ", " ").strip(" \t.:")
            num_match = _EQNUM_RE.match(residue) if residue else None
            pandoc_display = any(n.superscript for n in maths)

            if len(maths) == 1 and (not residue or num_match or pandoc_display):
                self._eq_n += 1
                eq = Equation(
                    id=f"eq{self._eq_n}",
                    latex=maths[0].text.strip(),
                    display=True,
                    number=self._eq_n if (num_match or not residue) else None,
                    number_raw=num_match.group(1) if num_match else "",
                    provenance=Provenance(
                        method="explicit" if pandoc_display else "heuristic",
                        confidence=1.0 if pandoc_display else (0.95 if num_match else 0.75),
                        note="" if pandoc_display else
                             ("numbered math-only paragraph" if num_match
                              else "math-only paragraph, no printed number"),
                    ),
                )
                if num_match and num_match.group(1) != str(self._eq_n):
                    self._issue("warning", "eqnum_mismatch",
                                f"Equation {eq.id}: source printed ({num_match.group(1)}) "
                                f"but sequential position is {self._eq_n}. "
                                "Renumbering will change cross-references.",
                                eq.id)
                self.ms.equations.append(eq)
                out.append({"kind": "block",
                            "block": Block(kind="equation_ref", target_id=eq.id),
                            "float_id": eq.id, "float_type": "equation"})
                continue

            # Genuinely inline math: clear the transient display flag.
            for n in maths:
                n.superscript = False
            out.append(item)
        return out

    # -- stage 3: captions --------------------------------------------------

    def _attach_captions(self, stream: list[dict]) -> list[dict]:
        """Bind "Fig. 3 ..." / "Table 2 ..." paragraphs to the nearest float.

        Search order: the paragraph immediately after the float, then the one
        immediately before it (Word tables conventionally caption above,
        figures below, but manuscripts are inconsistent).
        """
        out: list[dict] = []
        consumed: set[int] = set()

        for i, item in enumerate(stream):
            # Figure captions are resolved from the OOXML in _build_figures_from_oox.
            if item.get("float_type") != "table":
                continue
            for j in (i - 1, i + 1, i - 2, i + 2, i + 3):
                if j in consumed or not (0 <= j < len(stream)):
                    continue
                cand = stream[j]
                if cand["kind"] != "block" or not cand.get("block") \
                        or cand["block"].kind != "paragraph":
                    continue
                text = cand["block"].paragraph.plain_text().strip()
                m = _CAPTION_RE.match(text)
                if not m:
                    continue
                kind = m.group("kind").lower()
                is_tab = kind.startswith("tab")
                if is_tab != (item["float_type"] == "table"):
                    continue
                obj = (self.ms.table(item["float_id"]) if is_tab
                       else self.ms.figure(item["float_id"]))
                if obj is None or obj.caption_raw:
                    continue
                obj.caption_raw = text
                obj.label = f"{m.group('kind')} {m.group('num')}"
                obj.caption = _strip_caption_label(cand["block"].paragraph.inlines, m)
                obj.provenance = Provenance(
                    method="heuristic", confidence=0.9,
                    note=f"caption matched {'below' if j > i else 'above'} the float",
                )
                consumed.add(j)
                break
            else:
                obj = (self.ms.table(item["float_id"]) if item["float_type"] == "table"
                       else self.ms.figure(item["float_id"]))
                if obj is not None and not obj.caption_raw:
                    self._issue("warning", "missing_caption",
                                f"{item['float_id']} has no detectable caption.",
                                item["float_id"])

        for i, item in enumerate(stream):
            if i not in consumed:
                out.append(item)
        return out

    # -- stage 4: section tree ---------------------------------------------

    def _build_sections(self, stream: list[dict]) -> None:
        stream = self._detect_implicit_headings(stream)

        root = Section(id="s0", level=0, role=SectionRole.UNKNOWN, title_raw="")
        stack: list[Section] = [root]
        n = 0

        from . import cleanup  # noqa: PLC0415

        demoted = 0
        for item in stream:
            if item["kind"] == "heading":
                if not item["raw"].strip():
                    # Word documents routinely carry empty Heading-styled
                    # paragraphs used as spacers; they must not create sections.
                    continue

                # Journal boilerplate styled as a heading must not open a
                # section. "PUBLICATION FEE" is a Diagnostyka template artefact,
                # and because it sat near the end of the file it captured the
                # closing paragraphs of the paper -- so the AI panel dutifully
                # reported that the conclusions were in the publication-fee
                # section, and compliance reported no conclusion at all.
                if any(rx.search(item["raw"].strip()) for rx, _ in cleanup._COMPILED):
                    demoted += 1
                    continue
                n += 1
                raw = item["raw"].strip()
                num_m = _NUMBERING_RE.match(raw)
                sec = Section(
                    id=f"s{n}",
                    level=max(1, int(item["level"])),
                    title_raw=raw,
                    title=item["inlines"],
                    numbering_raw=num_m.group(1) if num_m else "",
                    role_provenance=Provenance(method=item.get("source", "style"),
                                               confidence=0.0),
                )
                while len(stack) > 1 and stack[-1].level >= sec.level:
                    stack.pop()
                stack[-1].children.append(sec)
                stack.append(sec)
            else:
                stack[-1].blocks.append(item["block"])

        if demoted:
            self._issue("info", "boilerplate_heading_demoted",
                        f"{demoted} heading(s) matching journal boilerplate "
                        "(publication fee, ISSN, citation info) were not treated "
                        "as sections; their content stays with the preceding "
                        "section.")

        self.ms.body = root.children if root.children else []
        if root.blocks:
            preamble = Section(id="s_pre", level=1, role=SectionRole.UNKNOWN,
                               title_raw="", blocks=root.blocks)
            self.ms.body.insert(0, preamble)

    _CANONICAL_BODY_ROLES = frozenset({
        SectionRole.ABSTRACT, SectionRole.INTRODUCTION, SectionRole.METHODS,
        SectionRole.RESULTS, SectionRole.DISCUSSION, SectionRole.CONCLUSION,
        SectionRole.REFERENCES, SectionRole.RESULTS_DISCUSSION,
    })

    def _unwrap_title_wrapper(self) -> None:
        """Promote the children of a lone top-level section that is the title.

        Word authors routinely style the paper title as `Heading 1` and every
        real section as `Heading 2`. What comes out of sectioning is then a
        single top-level node -- the title -- with the entire manuscript
        hanging beneath it, and its own blocks are the author and affiliation
        lines.

        Everything downstream reads that as one unclassified section, and the
        consequences are not cosmetic:

        * LaTeX emits `\section{<the paper title>}` with the author block
          printed as body prose underneath it.
        * Compliance sees one top-level section and reports the introduction,
          methods and conclusion as missing.
        * Worst: assigning that section a front-matter role in the Sections
          panel -- `title` is the obvious choice, since it *is* the title --
          removed the whole manuscript from the LaTeX output, because a
          front-matter section is not rendered as body. One dropdown click,
          an empty paper, no warning. That is what prompted this function, and
          `render_latex` no longer drops children either.

        Nothing is discarded. The wrapper's own blocks keep their content and
        their order; they move into a front-matter section, where renderers
        place them through the author macros rather than printing them twice.
        """
        body = self.ms.body
        wrappers = [s for s in body if s.children]
        if len(wrappers) != 1 or len(body) > 2:
            return
        wrapper = wrappers[0]

        title = _norm_ws(self.ms.meta.title).lower()
        wrapper_title = _norm_ws(wrapper.title_raw).lower()
        looks_like_title = bool(title) and (
            wrapper_title == title
            or (len(wrapper_title) > 25 and wrapper_title in title)
            or (len(title) > 25 and title in wrapper_title))
        if not looks_like_title:
            return

        # Second guard: the children must actually look like a manuscript, so
        # that a genuine single section named after the paper is left alone.
        canonical = {c.role for c in wrapper.children} & self._CANONICAL_BODY_ROLES
        if len(canonical) < 2:
            return

        def shift(sec: Section, by: int) -> None:
            sec.level = max(1, sec.level - by)
            for c in sec.children:
                shift(c, by)

        promoted = wrapper.children
        by = max(0, min(c.level for c in promoted) - 1)
        for c in promoted:
            shift(c, by)

        rest = [s for s in body if s is not wrapper]
        front: list[Section] = []
        if wrapper.blocks:
            front.append(Section(
                id=f"{wrapper.id}_front", level=1, role=SectionRole.AUTHORS,
                title_raw="", blocks=wrapper.blocks,
                role_provenance=Provenance(
                    method="style", confidence=0.9,
                    note="author and affiliation lines held by the title heading"),
            ))
        self.ms.body = rest + front + promoted
        self._issue("info", "title_wrapper_unwrapped",
                    f"The manuscript title was styled as a top-level heading, so "
                    f"{len(promoted)} section(s) were nested beneath it. They are "
                    "now top-level sections; the author and affiliation lines it "
                    "held are kept as front matter.")

    def _detect_implicit_headings(self, stream: list[dict]) -> list[dict]:
        """Recover headings that the author typed as bold/numbered paragraphs.

        Word manuscripts routinely apply no heading styles at all. A paragraph
        is promoted when it is short, has no terminal punctuation, contains no
        float or math, and is either fully bold or begins with section
        numbering that matches a plausible outline.
        """
        promoted = 0
        by_model = 0
        for item in stream:
            if item["kind"] != "block" or item["block"].kind != "paragraph":
                continue
            para = item["block"].paragraph
            text = para.plain_text().strip()
            if not (2 <= len(text) <= 120):
                continue

            # A locally trained detector acts as a *tiebreaker*, never as an
            # override.
            #
            # An earlier version let a confident model promote any paragraph.
            # Trained on ~500 examples it then marked "Zone 1:" and
            # "V  Voltage [V];" as headings. The failure is structural, not a
            # matter of more data: a small model sees shape, and a nomenclature
            # entry has the shape of a heading. Hard structural disqualifiers
            # -- trailing semicolon, a units bracket, a definition line -- are
            # things rules know and n-grams do not, so the rules keep the veto.
            if _cannot_be_heading(text):
                continue

            pred = _predict_heading(text)
            if pred is not None and pred[1] >= 0.85 and not pred[0]:
                continue        # confident it is body text: trust it
            if text.endswith((".", ",", ";", ":")) and not _NUMBERING_RE.match(text):
                continue
            if any(n.kind == "math" for n in para.inlines):
                continue
            words = text.split()
            if len(words) > 14:
                continue

            visible = [n for n in para.inlines if n.kind == "text" and n.text.strip()]
            all_bold = bool(visible) and all(n.bold for n in visible)
            num_m = _NUMBERING_RE.match(text)
            body = text[num_m.end():] if num_m else text
            lexical = _match_role(body) is not SectionRole.UNKNOWN

            # The model may break a tie the rules leave open: a numbered but
            # unbolded line with no lexicon match is genuinely ambiguous.
            model_says_yes = False
            if not (all_bold or lexical):
                pred = _predict_heading(text)
                model_says_yes = bool(
                    pred is not None and pred[0] and pred[1] >= 0.85 and num_m)

            if all_bold or (num_m and (all_bold or lexical)) or lexical or model_says_yes:
                level = 1
                if num_m:
                    level = min(4, num_m.group(1).count(".") + 1)
                item.clear()
                item.update({
                    "kind": "heading", "level": level,
                    "inlines": para.inlines, "raw": text,
                    "source": "model" if model_says_yes else "heuristic",
                })
                promoted += 1
                by_model += int(model_says_yes)

        if promoted:
            self._issue("info", "implicit_headings",
                        f"{promoted} heading(s) recovered from unstyled paragraphs"
                        + (f" ({by_model} by the locally trained model)" if by_model else
                           " by shape rules")
                        + ". Verify the section tree before rendering.")
        return [it for it in stream if it]

    # -- stage 5: roles -----------------------------------------------------

    def _classify_roles(self) -> None:
        used_model = 0
        for sec in self.ms.iter_sections():
            body = sec.title_raw
            m = _NUMBERING_RE.match(body)
            if m:
                body = body[m.end():]
            role = _match_role(body)

            if role is not SectionRole.UNKNOWN:
                sec.role = role
                sec.role_provenance = Provenance(
                    method="heuristic", confidence=0.95, note="lexicon match")
                continue

            # The lexicon can only ever match headings someone thought of. A
            # locally trained model, if the user has produced one, handles the
            # rest -- "Protection of a Very High Voltage Line Span" is a methods
            # section that no keyword list will contain.
            # 0.75, not 0.55. At 0.55 a role classifier trained on ~200
            # examples across 22 classes (cv accuracy 0.45) confidently
            # mislabels: it called "Zone 1:" a keywords section at 91 %.
            # Confidence from a thin multi-class model is not calibrated, so the
            # threshold has to absorb that.
            guess = _predict_role(body)
            if guess is not None and guess[1] >= 0.75:
                try:
                    sec.role = SectionRole(guess[0])
                    sec.role_provenance = Provenance(
                        method="model", confidence=round(guess[1], 2),
                        note="local model; confirm in the review console")
                    used_model += 1
                    continue
                except ValueError:
                    pass

            sec.role = SectionRole.UNKNOWN
            sec.role_provenance = Provenance(
                method="default", confidence=0.0,
                note="no lexicon match; needs human labelling")

        if used_model:
            self._issue("info", "role_model_used",
                        f"{used_model} section role(s) assigned by the locally "
                        "trained model rather than the lexicon. Confirm them.")
        unknown = [s for s in self.ms.body if s.role is SectionRole.UNKNOWN and s.title_raw]
        if unknown:
            self._issue("warning", "unclassified_sections",
                        f"{len(unknown)} top-level section(s) unclassified: "
                        + "; ".join(s.title_raw[:40] for s in unknown[:6]))

    # -- stage 6: front matter ---------------------------------------------

    def _extract_front_matter(self, ast: dict) -> None:
        meta = self.ms.meta

        # Title resolution, in descending order of reliability:
        #   1. dc:title in docProps/core.xml (set explicitly by the author)
        #   2. the first paragraph styled "Title"
        #   3. the first substantial paragraph before the author line
        meta.title = self._core_property("title")
        if not meta.title and self.scan:
            for p in self.scan.paragraphs[:40]:
                if p.style.lower() in ("title", "titre") and p.text.strip():
                    meta.title = p.text.strip()
                    break
        if not meta.title and self.scan:
            from . import cleanup  # noqa: PLC0415

            for p in self.scan.paragraphs[:25]:
                t = re.sub(r"\s+", " ", p.text).strip()
                if not (12 <= len(t) <= 300):
                    continue
                if _EMAIL_RE.search(t) or _match_role(t) is not SectionRole.UNKNOWN:
                    continue
                # Skip the previous journal's masthead. Without this the first
                # "substantial paragraph" of a Diagnostyka manuscript is
                # "DIAGNOSTYKA, 20xx, Vol. xx, No. x", which then propagates
                # everywhere -- into the LaTeX \title, into compliance, and into
                # the AI panel, where a referee duly objected that the title did
                # not reflect the content of the paper.
                if any(rx.search(t) for rx, _ in cleanup._COMPILED):
                    continue
                # An author line carries digit/asterisk markers on names.
                if len(re.findall(r"[A-Za-z][\d*†]", t)) >= 2:
                    break
                if t.lower().startswith(("abstract", "keywords")):
                    break
                meta.title = t
                self._issue("warning", "title_heuristic",
                            f"Title inferred from the first substantial paragraph: "
                            f"\"{t[:70]}\". Confirm before rendering.")
                break
        if not meta.title:
            self._issue("error", "no_title", "Could not determine the manuscript title.")

        abstract_sec = self.ms.section_by_role(SectionRole.ABSTRACT)
        if abstract_sec:
            # A run-in "Keywords: ..." paragraph at the end of the abstract
            # section is not part of the abstract. Left in, it is printed twice
            # in every renderer -- once inside \begin{abstract} and once in the
            # keyword macro -- and it inflates the abstract word count that
            # compliance checks against the journal limit.
            body_blocks = list(abstract_sec.blocks)
            while body_blocks and body_blocks[-1].paragraph and re.match(
                    r"^\s*(key\s*words?|index terms?)\s*[:\-\u2013]",
                    body_blocks[-1].paragraph.plain_text().strip(), re.I):
                body_blocks.pop()
                self._issue("info", "keywords_split_from_abstract",
                            "A run-in keywords line was excluded from the "
                            "abstract text; it is kept in the section and is "
                            "emitted through the keyword macro instead.")
            meta.abstract = body_blocks
            meta.abstract_raw = " ".join(
                b.paragraph.plain_text() for b in body_blocks if b.paragraph
            ).strip()
        else:
            self._issue("error", "no_abstract",
                        "No abstract section found. Every target journal requires one.")

        kw_sec = self.ms.section_by_role(SectionRole.KEYWORDS)
        if kw_sec:
            joined = " ".join(b.paragraph.plain_text() for b in kw_sec.blocks if b.paragraph)
            meta.keywords = _split_keywords(joined)
        else:
            # Keywords are frequently a run-in paragraph, not a section.
            for sec in self.ms.iter_sections():
                for b in sec.blocks:
                    if not b.paragraph:
                        continue
                    t = b.paragraph.plain_text().strip()
                    if re.match(r"^(key\s*words?|index terms?)\s*[:\-–]", t, re.I):
                        meta.keywords = _split_keywords(re.split(r"[:\-–]", t, 1)[-1])
                        break
                if meta.keywords:
                    break
        if not meta.keywords:
            self._issue("warning", "no_keywords", "No keywords detected.")

        self._extract_authors()

    # Word writes affiliation markers as real superscript characters, not as
    # ASCII digits with superscript formatting, whenever the author used the
    # Insert > Symbol route or pasted from a PDF. Every marker regex below is
    # ASCII, so "Ammari*¹" kept its marker inside the surname and picked up no
    # affiliation at all. Folding the superscripts first costs one line and
    # fixes both.
    _SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹"
                                  "ᵃᵇᶜ⁺⁻",
                                  "0123456789abc+-")

    def _extract_authors(self) -> None:
        """Recover authors and affiliations from the front-matter block.

        This is the least reliable part of any DOCX parser: the author line is
        pure typography with no semantic markup. We therefore keep every raw
        string and mark confidence low, expecting a human confirmation step.
        """
        meta = self.ms.meta
        candidates: list[str] = []
        for sec in list(self.ms.iter_sections())[:3]:
            if sec.title_raw:
                candidates.append(sec.title_raw)
            for b in sec.blocks[:12]:
                if b.paragraph:
                    candidates.append(b.paragraph.plain_text())
            if self.ms.section_by_role(SectionRole.ABSTRACT) in (sec,):
                break
        candidates = [c.translate(self._SUPERSCRIPTS) for c in candidates]

        emails = []
        author_line = ""
        for line in candidates:
            emails.extend(_EMAIL_RE.findall(line))
            # Author lines: several comma-separated capitalised names with
            # superscript-ish digit markers, no verb-like sentence structure.
            names = [p for p in re.split(r"\s*,\s*|\s+and\s+", line) if p.strip()]
            marked = sum(1 for p in names if re.search(r"[A-Za-z]\s*[\d*†a-c]\s*$", p.strip()))
            if len(names) >= 2 and marked >= 2 and len(line) < 400 and not author_line:
                author_line = line

        if author_line:
            for i, chunk in enumerate(re.split(r"\s*,\s*|\s+and\s+", author_line), 1):
                chunk = chunk.strip()
                if not chunk:
                    continue
                markers = re.findall(r"[\d*†]", chunk[-4:])
                name = re.sub(r"[\d*†\s]+$", "", chunk).strip()
                if not name:
                    continue
                parts = name.split()
                a = Author(
                    id=f"au{i}", raw=chunk,
                    given=" ".join(parts[:-1]) if len(parts) > 1 else "",
                    family=parts[-1] if parts else name,
                    corresponding="*" in markers,
                    affiliation_ids=[f"aff{m}" for m in markers if m.isdigit()],
                    provenance=Provenance(method="heuristic", confidence=0.5,
                                          note="parsed from unstructured author line"),
                )
                meta.authors.append(a)
            self._issue("warning", "authors_heuristic",
                        f"{len(meta.authors)} author(s) parsed heuristically from "
                        "an unstructured line; confirm names, order and markers.")
        else:
            self._issue("error", "no_authors",
                        "Could not locate an author line. Author metadata must be "
                        "supplied manually.")

        if emails:
            meta.corresponding_email = emails[0]
            for a in meta.authors:
                if a.corresponding and not a.email:
                    a.email = emails[0]

        # Affiliations: numbered lines in the front matter that are not the
        # author line and contain an institution keyword.
        inst = re.compile(r"universit|institut|laborator|department|faculty|college|"
                          r"school|centre|center|academy|LTD|inc\.", re.I)
        for line in candidates:
            if line == author_line or not inst.search(line):
                continue
            m = re.match(r"^\s*([\d*†])\s*[.\-)]?\s*(.+)$", line.strip())
            marker, rest = (m.group(1), m.group(2)) if m else ("", line.strip())
            aid = f"aff{marker or len(meta.affiliations) + 1}"
            if any(x.id == aid for x in meta.affiliations):
                continue
            meta.affiliations.append(Affiliation(
                id=aid, marker=marker, raw=rest.strip(),
                country=_guess_country(rest),
                provenance=Provenance(method="heuristic", confidence=0.5),
            ))

    def _core_property(self, name: str) -> str:
        try:
            with zipfile.ZipFile(self.docx_path) as z:
                xml = z.read("docProps/core.xml").decode("utf-8", "ignore")
            m = re.search(rf"<dc:{name}>(.*?)</dc:{name}>", xml, re.S)
            return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
        except Exception:
            return ""

    # -- stage 7: references -----------------------------------------------

    def _extract_references(self) -> None:
        sec = self.ms.section_by_role(SectionRole.REFERENCES)
        if sec is None:
            self._issue("error", "no_references", "No references section found.")
            return

        entries: list[str] = []
        for b in _walk_blocks(sec):
            if b.kind == "paragraph" and b.paragraph:
                t = b.paragraph.plain_text().strip()
                if t:
                    entries.append(t)
            elif b.kind == "list" and b.list_block:
                for item in b.list_block.items:
                    t = " ".join(x.paragraph.plain_text() for x in item if x.paragraph).strip()
                    if t:
                        entries.append(t)

        numbered = sum(1 for e in entries if _REF_MARKER_RE.match(e))
        if numbered >= max(2, len(entries) // 2):
            # Numbered list: markers are authoritative, so join only true
            # continuation lines.
            merged: list[str] = []
            for e in entries:
                if merged and not _REF_MARKER_RE.match(e):
                    merged[-1] = merged[-1].rstrip() + " " + e
                else:
                    merged.append(e)
        else:
            # Unnumbered list: one paragraph is one reference. Joining on
            # "looks like a continuation" merged four distinct references into
            # one on real input, so we now split rather than join, cutting after
            # a DOI/URL or a year-page tail when a new author name follows.
            merged = []
            for e in entries:
                merged.extend(_split_run_on_references(e))

        self.ms.references = []

        for i, raw in enumerate(merged, 1):
            self.ms.references.append(_parse_reference(raw, i))

        low = [r for r in self.ms.references if r.parse_confidence < 0.6]
        if low:
            self._issue("warning", "low_confidence_refs",
                        f"{len(low)}/{len(self.ms.references)} references parsed with low "
                        "confidence. Recommend re-importing the bibliography from a "
                        "reference manager (or running AnyStyle/GROBID) before restyling.")
        if not any(r.doi for r in self.ms.references):
            self._issue("warning", "no_dois",
                        "No DOIs found in the bibliography. Most publishers now require "
                        "DOIs for all references.")

        # No field-code citations anywhere means citations are hand-typed and
        # cannot be re-styled automatically.
        has_cite = any(
            n.kind == "cite"
            for s in self.ms.iter_sections() for b in _walk_blocks(s)
            if b.paragraph for n in b.paragraph.inlines
        )
        if not has_cite:
            self._issue("warning", "manual_citations",
                        "In-text citations are plain text, not reference-manager fields. "
                        "Numeric<->author-year conversion will require matching "
                        "bracketed markers to the reference list.")

    # -- stage 8: media audit ----------------------------------------------

    def _audit_media(self) -> None:
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError:
            Image = None
            self._issue("info", "no_pillow",
                        "Pillow not installed; figure resolution not verified.")

        for fig in self.ms.figures:
            for rel in fig.files:
                path = self.media_dir / rel
                if not path.exists():
                    self._issue("error", "missing_media",
                                f"{fig.id}: media file not found: {rel}", fig.id)
                    continue
                ext = path.suffix.lower()
                fig.fmt = ext.lstrip(".")
                fig.is_vector = ext in _VECTOR_EXT
                fig.needs_conversion = ext in _NEEDS_CONVERSION_EXT
                if ext in (".emf", ".wmf"):
                    # Windows metafiles are unusable everywhere outside Word.
                    self._issue("error", "unusable_image_format",
                                f"{fig.id}: '{ext}' cannot be included by pdfLaTeX and "
                                "renders unreliably outside Windows. Convert to PDF "
                                "(vector) or 600 dpi PNG before submission.", fig.id)
                elif fig.needs_conversion:
                    # TIFF/BMP/GIF are accepted by most publishers but cannot be
                    # placed by pdfLaTeX, so this only blocks the LaTeX route.
                    self._issue("warning", "latex_incompatible_image",
                                f"{fig.id}: '{ext}' is accepted by most publishers but "
                                "pdfLaTeX cannot include it. Convert to PDF or PNG if "
                                "you intend to submit LaTeX.", fig.id)
                if Image and ext in _RASTER_EXT:
                    try:
                        with Image.open(path) as im:
                            fig.width_px, fig.height_px = im.size
                            dpi = im.info.get("dpi")
                            if dpi:
                                fig.dpi = float(dpi[0])
                    except Exception as exc:
                        self._issue("warning", "image_unreadable",
                                    f"{fig.id}: {exc}", fig.id)
                    # Effective dpi at the size Word actually placed it, which is
                    # what a publisher checks -- not the raw pixel count.
                    if fig.width_px and fig.placed_width_mm:
                        fig.dpi = round(fig.width_px / (fig.placed_width_mm / 25.4), 1)
                elif fig.is_vector:
                    # Vector art has no resolution. Pillow will happily report a
                    # size for an EMF, but those are logical device units and
                    # showing them as pixels invites a meaningless comparison
                    # against a dpi threshold.
                    fig.width_px = fig.height_px = None
                    # Effective resolution at single-column width (90 mm).
                    if fig.width_px and fig.width_px < 1050:
                        self._issue("warning", "low_resolution",
                                    f"{fig.id}: {fig.width_px} px wide is below 300 dpi at "
                                    "90 mm single-column width (needs >=1063 px).", fig.id)

    # -- degenerate math ----------------------------------------------------

    def _check_math_quality(self) -> None:
        """Flag OMML that Pandoc converted to empty or structurally broken LaTeX.

        Word stores some symbols (Symbol font, Cambria Math private-use points,
        certain accent constructs) in forms Pandoc cannot map. It does not warn;
        it emits the surrounding structure with nothing inside it -- `_{}`,
        `^{}^{}`, or an empty string. That is both a silent content loss and
        invalid LaTeX: `^{}^{}` is a double-superscript error that stops the
        compile.
        """
        bad_display = [e.id for e in self.ms.equations if _is_degenerate_math(e.latex)]
        bad_inline = 0
        for sec in self.ms.iter_sections():
            for b in _walk_blocks(sec):
                if b.paragraph:
                    for n in b.paragraph.inlines:
                        if n.kind == "math" and _is_degenerate_math(n.text):
                            bad_inline += 1

        total = len(bad_display) + bad_inline
        if not total:
            return
        self._issue(
            "error", "degenerate_math",
            f"{total} equation(s) converted to empty or malformed LaTeX "
            f"({len(bad_display)} display, {bad_inline} inline). Pandoc could not "
            "map the underlying OMML - usually a Symbol-font character or a "
            "Cambria Math private-use glyph. These will render as a placeholder "
            "and must be retyped.",
        )
        for eid in bad_display[:10]:
            self._issue("warning", "degenerate_equation",
                        f"{eid}: LaTeX is empty or structurally broken.", eid)

    # -- empty layout tables ------------------------------------------------

    def _drop_layout_tables(self) -> None:
        """Remove tables with no content at all.

        Word authors use borderless tables purely for positioning. An empty one
        carries no information, but it would still emit a tabular full of blank
        cells and trigger a missing-caption warning.
        """
        empty = [t for t in self.ms.tables
                 if not any(_block_texts(b) for row in t.grid
                            for cell in row for b in cell.blocks)]
        if not empty:
            return
        ids = {t.id for t in empty}
        self.ms.tables = [t for t in self.ms.tables if t.id not in ids]
        for sec in self.ms.iter_sections():
            sec.blocks = [b for b in sec.blocks
                          if not (b.kind == "table_ref" and b.target_id in ids)]
        self._issue("info", "layout_table_dropped",
                    f"{len(empty)} empty layout table(s) removed "
                    f"({', '.join(sorted(ids))}).")

    # -- text-loss detection ------------------------------------------------

    def _check_text_loss(self) -> None:
        """Report OOXML paragraphs whose text never reached the IR.

        This is the single most important check in the parser. Pandoc silently
        discards the contents of Word text boxes and shape captions; on the
        reference manuscript that cost four figure captions. Prose lost this way
        would be invisible until a reviewer noticed it, so we compare the source
        paragraph corpus against the IR corpus directly.

        Recovered paragraphs are appended to the section that owns the nearest
        surviving source paragraph, so nothing is dropped from the output.
        """
        # Section -> every piece of text it carries, including list items and
        # table cells. Missing either of those produces false positives: on the
        # reference manuscript the whole bibliography lives in one OrderedList.
        sec_texts: list[tuple[Section, list[str]]] = []
        for sec in self.ms.iter_sections():
            texts: list[str] = []
            if sec.title_raw:
                texts.append(_norm_ws(sec.title_raw))
            for b in sec.blocks:
                texts.extend(_block_texts(b))
            sec_texts.append((sec, texts))

        ir_corpus: list[str] = [t for _, ts in sec_texts for t in ts]
        for f in self.ms.figures:
            ir_corpus.append(_norm_ws(f.caption_raw))
        for t in self.ms.tables:
            ir_corpus.append(_norm_ws(t.caption_raw))
            for row in t.grid:
                for cell in row:
                    for b in cell.blocks:
                        ir_corpus.extend(_block_texts(b))
        blob = "\n".join(x for x in ir_corpus if x)

        missing: list[tuple[int, str]] = []
        for p in self.scan.paragraphs:
            t = _norm_ws(p.text)
            if len(t) < 25:            # too short to match reliably
                continue
            probe = t[:60]
            if probe not in blob:
                missing.append((p.index, t))

        if not missing:
            return

        lost_words = sum(len(t.split()) for _, t in missing)
        self._issue(
            "error", "text_lost_by_reader",
            f"{len(missing)} source paragraph(s) ({lost_words} words) are present in "
            "the DOCX but absent from Pandoc's output - almost always text-box or "
            "shape content. They have been recovered from the OOXML and re-inserted "
            "into the nearest section; verify their placement.",
        )
        for idx, t in missing[:12]:
            self._issue("warning", "recovered_paragraph",
                        f"source paragraph {idx}: \"{t[:90]}\"")

        # Re-insert each lost paragraph into the section that owns the nearest
        # preceding *surviving* source paragraph, so recovered prose lands in
        # roughly the right place instead of being dumped at the end.
        src_index: dict[str, int] = {}
        for p in self.scan.paragraphs:
            t = _norm_ws(p.text)
            if len(t) >= 25:
                src_index.setdefault(t[:60], p.index)

        sec_span: list[tuple[int, Section]] = []
        for sec, texts in sec_texts:
            idxs = [src_index[t[:60]] for t in texts if t and t[:60] in src_index]
            if idxs:
                sec_span.append((min(idxs), sec))
        sec_span.sort(key=lambda x: x[0])

        for idx, t in missing:
            target = self.ms.body[-1] if self.ms.body else None
            for start, sec in sec_span:
                if start <= idx:
                    target = sec
                else:
                    break
            if target is None:
                continue
            self._para_n += 1
            target.blocks.append(Block(
                kind="paragraph",
                paragraph=Paragraph(
                    id=f"p{self._para_n}",
                    inlines=[InlineNode(kind="text", text=t)],
                    source_style="recovered-from-ooxml",
                ),
            ))

    # -- stats / issues -----------------------------------------------------

    def _collect_stats(self) -> None:
        self.ms.stats = {
            "sections": sum(1 for _ in self.ms.iter_sections()),
            "top_level_sections": len(self.ms.body),
            "paragraphs": self._para_n,
            "words": self.ms.word_count(),
            "equations": len(self.ms.equations),
            "display_equations": sum(1 for e in self.ms.equations if e.display),
            "figures": len(self.ms.figures),
            "tables": len(self.ms.tables),
            "references": len(self.ms.references),
            "authors": len(self.ms.meta.authors),
            "affiliations": len(self.ms.meta.affiliations),
            "issues_error": sum(1 for i in self.ms.issues if i.severity == "error"),
            "issues_warning": sum(1 for i in self.ms.issues if i.severity == "warning"),
        }

    def _issue(self, severity: str, code: str, message: str, location: str = "") -> None:
        self.ms.issues.append(
            ParseIssue(severity=severity, code=code, message=message, location=location)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_ws(s: str) -> str:
    """Collapse whitespace and the math sentinel for corpus comparison."""
    return re.sub(r"\s+", " ", s.replace("", " ")).strip()


def _plain(nodes: Iterable[dict]) -> str:
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            t = x.get("t")
            if t == "Str":
                out.append(x["c"])
            elif t in ("Space", "SoftBreak", "LineBreak"):
                out.append(" ")
            elif t == "Math":
                out.append(f"${x['c'][1]}$")
            elif t == "Code":
                out.append(x["c"][1])
            elif isinstance(x.get("c"), (list, dict)):
                walk(x["c"])
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(list(nodes))
    return "".join(out)


def _plain_blocks(blocks: Iterable[dict]) -> str:
    return " ".join(_plain(b.get("c", [])) for b in blocks if isinstance(b, dict)).strip()


def _block_texts(b: Block) -> list[str]:
    """All normalised text carried by a block, recursing into lists and quotes."""
    out: list[str] = []
    if b.paragraph:
        out.append(_norm_ws(b.paragraph.plain_text()))
    if b.list_block:
        for item in b.list_block.items:
            for sub in item:
                out.extend(_block_texts(sub))
    for sub in b.quote_blocks:
        out.extend(_block_texts(sub))
    if b.code_text:
        out.append(_norm_ws(b.code_text))
    return [t for t in out if t]


def _walk_blocks(sec: Section) -> Iterable[Block]:
    yield from sec.blocks
    for child in sec.children:
        yield from _walk_blocks(child)


# Shapes that are never section headings, whatever a classifier thinks. These
# encode knowledge a character n-gram model cannot acquire from a few hundred
# examples: nomenclature entries, figure captions, equation numbers and
# definition lines all look like short capitalised phrases.
_NOT_HEADING_PATTERNS = [
    re.compile(r";\s*$"),                              # nomenclature entry
    re.compile(r"\[\s*[^\]]{1,12}\s*\]\s*[;.]?\s*$"),  # trailing units bracket
    re.compile(r"^\s*\(?[a-z0-9]\)?\s*$", re.I),       # "(a)", "1"
    re.compile(r"^\s*\(\s*\d+\s*\)\s*$"),              # equation number
    re.compile(r"^\s*(fig(ure)?|tab(le)?|scheme|chart)\s*\.?\s*[A-Z]?\d", re.I),
    re.compile(r"^\s*(where|such that|subject to|note that|here)\b\s*[:,]", re.I),
    re.compile(r"@"),                                  # e-mail line
    re.compile(r"^\s*(zone|case|scenario|step|mode)\s+\d+\s*[:.]?\s*$", re.I),
]


def _cannot_be_heading(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    return any(rx.search(t) for rx in _NOT_HEADING_PATTERNS)


def _predict_role(title: str) -> tuple[str, float] | None:
    """Ask the locally trained role model, if one exists. Never raises."""
    try:
        from . import learn  # noqa: PLC0415

        return learn.predict_role(title)
    except Exception:
        return None


def _predict_heading(text: str) -> tuple[bool, float] | None:
    try:
        from . import learn  # noqa: PLC0415

        return learn.predict_heading(text)
    except Exception:
        return None


def _match_role(title: str) -> SectionRole:
    t = re.sub(r"[\s ]+", " ", title).strip().strip(".:;-–— ").lower()
    if not t:
        return SectionRole.UNKNOWN
    for role, rx in _ROLE_RE:
        if rx.match(t):
            return role
    return SectionRole.UNKNOWN


def _strip_caption_label(inlines: list[InlineNode], m: re.Match) -> list[InlineNode]:
    """Drop the "Figure 3." label from a caption, keeping the rest verbatim."""
    drop = len(m.group(0)) - len(m.group("rest"))
    out, budget = [], drop
    for n in inlines:
        if budget <= 0:
            out.append(n)
        elif n.kind != "text":
            budget = 0
            out.append(n)
        elif len(n.text) <= budget:
            budget -= len(n.text)
        else:
            out.append(n.model_copy(update={"text": n.text[budget:]}))
            budget = 0
    return out


def _split_keywords(s: str) -> list[str]:
    s = re.sub(r"^\s*(key\s*words?|index terms?)\s*[:\-–]?\s*", "", s, flags=re.I)
    parts = re.split(r"[;,]|•|\|", s)
    return [p.strip(" .;–-") for p in parts if len(p.strip(" .;–-")) > 1]


def _guess_country(s: str) -> str:
    tail = s.strip().rstrip(".").split(",")[-1].strip()
    return tail if 2 <= len(tail) <= 30 and not any(c.isdigit() for c in tail) else ""


def _looks_like_new_ref(s: str) -> bool:
    return bool(re.match(r"^[A-Z][\w'’-]+,?\s+[A-Z]\.", s.strip()))


# A reference ends at a DOI/URL, or at a "vol(issue) pages" / "(year)" tail.
_REF_BOUNDARY = re.compile(
    r"(?:"
    r"(?:doi:\s*)?https?://\S+"                 # trailing URL or DOI link
    r"|10\.\d{4,9}/[^\s,;]+"                    # bare DOI
    r"|\b\d{1,5}\s*[-–]{1,2}\s*\d{1,5}\b"       # page range
    r"|\(\s*(?:19|20)\d{2}[a-z]?\s*\)"          # (year)
    r")"
    r"\s*[.;]?\s+"
    r"(?=[A-Z][\w'’‐-]{1,}\s*,?\s*(?:[A-Z]\.|et\s+al\b|and\b))",  # next author
    re.I,
)


_MATH_NOISE_RE = re.compile(r"(\\[,;: ]|\\quad|\\qquad|[\^_]\s*\{\s*\}|[{}\s\\])+")


def _is_degenerate_math(latex: str) -> bool:
    """True when a LaTeX math string carries no actual content.

    Catches "", "_{}", "^{}^{}", "\\ " and combinations -- the shapes Pandoc
    produces when it cannot map an OMML construct.
    """
    if not latex or not latex.strip():
        return True
    return not _MATH_NOISE_RE.sub("", latex).strip()


def _cell_signature(cell: TableCell) -> tuple[int, str]:
    """(number of math runs, non-math text) for one table cell."""
    n_math = 0
    text: list[str] = []
    for b in cell.blocks:
        if not b.paragraph:
            if b.kind not in ("paragraph",):
                text.append("\x00")     # any non-paragraph content disqualifies
            continue
        for node in b.paragraph.inlines:
            if node.kind == "math":
                n_math += 1
            elif node.kind == "break":
                text.append(" ")
            else:
                text.append(node.text)
    return n_math, "".join(text).strip()


def _as_equation_layout(grid: list[list[TableCell]]) -> list[tuple[str, str]] | None:
    """Recognise a Word equation-numbering table.

    The canonical shapes are:

        | <equation> | (3) |            two columns
        |  | <equation> | (3) |         three columns, empty spacer
        | <equation> |                  one column, unnumbered

    Every row must match, and at least one row must carry a printed number --
    otherwise a genuine data table whose cells happen to contain mathematics
    would be silently destroyed. Returns [(latex, number_raw), ...] or None.
    """
    if not grid or not (1 <= len(grid) <= 40):
        return None

    rows: list[tuple[str, str]] = []
    numbered = 0
    for row in grid:
        sigs = [_cell_signature(c) for c in row]
        if any("\x00" in t for _, t in sigs):
            return None
        math_cells = [i for i, (n, t) in enumerate(sigs) if n == 1 and not t]
        num_cells = [i for i, (n, t) in enumerate(sigs)
                     if n == 0 and _EQNUM_RE.match(t or "")]
        empty = [i for i, (n, t) in enumerate(sigs) if n == 0 and not t]

        if len(math_cells) != 1:
            return None
        if len(math_cells) + len(num_cells) + len(empty) != len(sigs):
            return None
        if len(num_cells) > 1:
            return None

        latex = ""
        for b in row[math_cells[0]].blocks:
            if b.paragraph:
                for node in b.paragraph.inlines:
                    if node.kind == "math":
                        latex = node.text.strip()
                        break
        if not latex:
            return None

        number_raw = ""
        if num_cells:
            m = _EQNUM_RE.match(_cell_signature(row[num_cells[0]])[1])
            number_raw = m.group(1) if m else ""
            numbered += 1
        rows.append((latex, number_raw))

    if not numbered:
        return None
    return rows


def _split_run_on_references(entry: str) -> list[str]:
    """Split a paragraph that contains several run-on bibliography entries.

    Word manuscripts frequently hold the whole bibliography in a handful of
    paragraphs with soft line breaks, so paragraph boundaries under-segment.
    We cut only at high-confidence boundaries (a completed citation tail
    immediately followed by something shaped like an author name) and never
    produce a fragment shorter than 40 characters.
    """
    entry = entry.strip()
    if len(entry) < 120:
        return [entry] if entry else []
    parts, last = [], 0
    for m in _REF_BOUNDARY.finditer(entry):
        cut = m.end()
        if cut - last >= 40:
            parts.append(entry[last:cut].strip())
            last = cut
    tail = entry[last:].strip()
    if tail:
        if parts and len(tail) < 40:
            parts[-1] += " " + tail
        else:
            parts.append(tail)
    return parts or [entry]


def _parse_reference(raw: str, order: int) -> Reference:
    """Regex-parse one bibliography entry into partial CSL-JSON.

    Deliberately conservative: we score confidence by how many high-signal
    fields (year, DOI, volume/pages, quoted or emphasised title) we recovered,
    and the caller falls back to `raw` whenever confidence is low. This is a
    stopgap for AnyStyle/GROBID, not a replacement for them.
    """
    text = raw.strip()
    m = _REF_MARKER_RE.match(text)
    if m:
        text = text[m.end():].strip()

    csl: dict[str, Any] = {"id": f"ref{order}", "type": "article-journal"}
    score = 0.0

    doi_m = _DOI_RE.search(text)
    doi = doi_m.group(1).rstrip(".,;") if doi_m else ""
    if doi:
        csl["DOI"] = doi
        score += 0.3

    url_m = _URL_RE.search(text)
    url = url_m.group(0).rstrip(".,;") if url_m and not doi else ""
    if url:
        csl["URL"] = url

    y = _YEAR_RE.search(text)
    if y:
        year = y.group(1) or y.group(0)
        csl["issued"] = {"date-parts": [[int(re.sub(r"\D", "", year)[:4])]]}
        score += 0.25

    vol = re.search(r"\b(?:vol\.?\s*)?(\d{1,4})\s*[,(]\s*(?:no\.?\s*)?(\d{1,4})?\s*\)?\s*[,:]?\s*"
                    r"(?:pp?\.?\s*)?(\d{1,5})\s*[-–]{1,2}\s*(\d{1,5})", text, re.I)
    if vol:
        csl["volume"] = vol.group(1)
        if vol.group(2):
            csl["issue"] = vol.group(2)
        csl["page"] = f"{vol.group(3)}-{vol.group(4)}"
        score += 0.25

    # Authors: leading "Family, F. M., Family, F.," run before the year/title.
    head = text.split("(")[0] if "(" in text[:120] else text[:200]
    authors = []
    for chunk in re.split(r",\s*(?=[A-Z])|\band\b|&", head):
        chunk = chunk.strip().rstrip(",.")
        am = re.match(r"^([A-Z][\w'’‐-]+)\s*,?\s*((?:[A-Z]\.\s*){1,3})$", chunk)
        if am:
            authors.append({"family": am.group(1), "given": am.group(2).strip()})
            continue
        am = re.match(r"^((?:[A-Z]\.\s*){1,3})\s*([A-Z][\w'’‐-]+)$", chunk)
        if am:
            authors.append({"family": am.group(2), "given": am.group(1).strip()})
    if authors:
        csl["author"] = authors
        score += 0.2

    title_m = re.search(r"[“\"]([^”\"]{10,300})[”\"]", text)
    if title_m:
        csl["title"] = title_m.group(1).strip(" ,.")
        score += 0.2
    else:
        seg = re.split(r"\)\s*[.,]?\s*", text, 1)
        if len(seg) > 1:
            cand = re.split(r"\.\s+", seg[1], 1)[0]
            if 15 < len(cand) < 300:
                csl["title"] = cand.strip(" ,.")
                score += 0.1

    return Reference(
        id=f"ref{order}", raw=raw.strip(), order=order, csl=csl,
        doi=doi, url=url, parse_confidence=min(1.0, score),
        provenance=Provenance(method="heuristic", confidence=min(1.0, score),
                              note="regex bibliography parse"),
    )


def parse_docx(path: str | Path, media_dir: str | Path | None = None) -> Manuscript:
    """Parse a .docx manuscript into the IR."""
    return DocxParser(path, media_dir).parse()
