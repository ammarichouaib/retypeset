r"""
retypeset.render_latex -- IR + journal profile -> a compilable LaTeX project.

Output layout:

    out/
      main.tex          the manuscript
      refs.bib          bibliography, from CSL-JSON where parsing succeeded
      figures/          every figure, converted to something pdfLaTeX can place
      BUILD.md          what was converted, what needs attention

Two decisions worth stating, because they are where naive converters go wrong:

**Escaping.** Body text is LaTeX-escaped; mathematics is not. The IR keeps them
as distinct inline kinds precisely so this distinction survives, and a single
`_escape()` applied to everything would destroy every equation in the document.

**Bibliography.** Entries are emitted as `thebibliography` using the author's
verbatim source string, not as generated BibTeX, whenever the parse confidence
is low. A mangled auto-generated entry looks correct and is wrong; the verbatim
string is at worst formatted for the previous journal, which a human notices
immediately. A `refs.bib` is written alongside for the entries that did parse
cleanly, so switching to BibTeX later is a one-line change in the preamble.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import assets
from .ir import (
    Block,
    Figure,
    InlineNode,
    Manuscript,
    Section,
    SectionRole,
    Table,
)
from .profile import JournalProfile

# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

# Order matters: the backslash must be replaced first, and its replacement must
# not itself be re-escaped, hence the sentinel.
_ESCAPES = [
    ("\\", "\x00BACKSLASH\x00"),
    ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
    ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
    ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}"),
]

_UNICODE_MAP: dict[str, str] = {
    # punctuation and spacing
    "\u2013": "--", "\u2014": "---", "\u2018": "`", "\u2019": "'",
    "\u201c": "``", "\u201d": "''", "\u00a0": "~", "\u2026": r"\ldots{}",
    "\u2032": r"$'$", "\u2033": r"$''$", "\u2022": r"$\bullet$",
    "\u2020": r"\dag{}", "\u2021": r"\ddag{}", "\u00b7": r"$\cdot$",
    "\ufeff": "", "\u200b": "", "\u00ad": "", "\u2009": r"\,", "\u202f": r"\,",
    # symbols
    "\u00b0": r"\textdegree{}", "\u00b5": r"$\mu$", "\u03bc": r"$\mu$",
    "\u00a9": r"\textcopyright{}", "\u00ae": r"\textregistered{}",
    "\u2122": r"\texttrademark{}", "\u20ac": r"\texteuro{}",
    "\u2030": r"\textperthousand{}",
    # Section and paragraph marks are ordinary prose in a manuscript that
    # cross-references its own numbered sections; NFKD leaves them with no
    # ASCII form at all, so without these two they were dropped silently.
    "\u00a7": r"\S{}", "\u00b6": r"\P{}", "\u2016": r"$\|$",
    "\u00ab": r"\guillemotleft{}", "\u00bb": r"\guillemotright{}",
    "\u200e": "", "\u200f": "",          # bidi marks
    "\u2011": "-",                        # non-breaking hyphen
    "\u2219": r"$\cdot$", "\u22c5": r"$\cdot$",
    # relations and operators
    "\u2212": "-", "\u2264": r"$\leq$", "\u2265": r"$\geq$", "\u2260": r"$\neq$",
    "\u2248": r"$\approx$", "\u2261": r"$\equiv$", "\u221d": r"$\propto$",
    "\u00d7": r"$\times$", "\u00f7": r"$\div$", "\u00b1": r"$\pm$",
    "\u2213": r"$\mp$", "\u221e": r"$\infty$", "\u221a": r"$\sqrt{}$",
    "\u2211": r"$\sum$", "\u220f": r"$\prod$", "\u222b": r"$\int$",
    "\u2202": r"$\partial$", "\u2207": r"$\nabla$", "\u2208": r"$\in$",
    "\u2205": r"$\emptyset$", "\u2192": r"$\rightarrow$", "\u2190": r"$\leftarrow$",
    "\u2194": r"$\leftrightarrow$", "\u21d2": r"$\Rightarrow$",
    # fractions
    "\u00bd": r"$\tfrac{1}{2}$", "\u00bc": r"$\tfrac{1}{4}$",
    "\u00be": r"$\tfrac{3}{4}$",
}

# Sub/superscript digits: H\u2082 and m\u00b3 are everywhere in energy manuscripts and
# crash pdfLaTeX under inputenc-utf8 if passed through untouched.
for _i, _sub in enumerate("\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089"):
    _UNICODE_MAP[_sub] = rf"$_{{{_i}}}$"
for _ch, _n in (("\u2070", 0), ("\u00b9", 1), ("\u00b2", 2), ("\u00b3", 3),
                ("\u2074", 4), ("\u2075", 5), ("\u2076", 6), ("\u2077", 7),
                ("\u2078", 8), ("\u2079", 9)):
    _UNICODE_MAP[_ch] = rf"$^{{{_n}}}$"
_UNICODE_MAP.update({"\u208b": r"$_{-}$", "\u207b": r"$^{-}$",
                     "\u208a": r"$_{+}$", "\u207a": r"$^{+}$"})

# Greek letters used as text (efficiency \u03b7, density \u03c1, ...).
_GREEK = {
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7", "theta": "\u03b8",
    "iota": "\u03b9", "kappa": "\u03ba", "lambda": "\u03bb", "nu": "\u03bd",
    "xi": "\u03be", "pi": "\u03c0", "rho": "\u03c1", "sigma": "\u03c3",
    "tau": "\u03c4", "upsilon": "\u03c5", "phi": "\u03c6", "chi": "\u03c7",
    "psi": "\u03c8", "omega": "\u03c9",
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
}
for _name, _ch in _GREEK.items():
    _UNICODE_MAP.setdefault(_ch, rf"$\{_name}$")

# Characters we could not translate, collected so the build notes can list them
# instead of letting them fail the compile silently.
UNMAPPED: set[str] = set()

# Math strings that arrived empty or malformed and were replaced by a marker.
DEGENERATE: list[str] = []


def escape(text: str) -> str:
    """LaTeX-escape prose. Never apply this to math.

    Anything outside ASCII that we have no mapping for is decomposed to its
    closest ASCII form rather than passed through: `inputenc` with `utf8` only
    defines a subset of Unicode, and an undefined character is a hard compile
    error, not a cosmetic problem.
    """
    # ORDER IS LOAD-BEARING. Escaping must happen first, because the Unicode
    # table substitutes LaTeX *commands* -- `$\eta$`, `$\leq$` -- and running
    # the escaper over those turns them into the literal text
    # `\$\textbackslash{}eta\$` on the page. That still compiles, which is
    # exactly why it has to be prevented here rather than caught downstream.
    for src, dst in _ESCAPES:
        text = text.replace(src, dst)
    text = text.replace("\x00BACKSLASH\x00", r"\textbackslash{}")

    if any(ord(c) > 0x7F for c in text):
        for src, dst in _UNICODE_MAP.items():
            if src in text:
                text = text.replace(src, dst)
    if any(ord(c) > 0x7F for c in text):
        text = _fold_remaining(text)
    return text


# Uppercased letter name -> canonical lowercase LaTeX command. Built from the
# lowercase entries only: _GREEK holds both "gamma" and "Gamma", and a naive
# `name.upper()` key collapses them so every letter comes out capitalised.
_GREEK_BY_UPPER = {n.upper(): n for n in _GREEK if n[0].islower()}


def _math_alphanumeric(ch: str) -> str | None:
    r"""Translate the Mathematical Alphanumeric Symbols block (U+1D400-U+1D7FF).

    Word inserts these when an author types a Greek letter with the maths
    keyboard rather than the Symbol font, so `𝜸` appears in ordinary prose.
    NFKD folds it to a bare ASCII letter, which silently changes the meaning,
    and leaving it alone is a hard `inputenc` error. Both are worse than
    emitting the corresponding math command.
    """
    import unicodedata  # noqa: PLC0415

    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    if not name.startswith("MATHEMATICAL"):
        return None

    style = ""
    if "BOLD" in name:
        style = "bold"
    parts = name.split()
    letter = parts[-1]
    capital = "CAPITAL" in name

    if letter in _GREEK_BY_UPPER:
        base = _GREEK_BY_UPPER[letter]
        # Only some capitals exist as LaTeX commands: there is no \Alpha,
        # because it is typographically identical to a Latin A.
        cmd = base.capitalize() if (capital and base.capitalize() in _GREEK) else base
        body = rf"\{cmd}" if not (capital and cmd == base) else base[0].upper()
    elif len(letter) == 1 and letter.isalpha():
        body = letter.upper() if capital else letter.lower()
    elif letter.isdigit():
        body = letter
    else:
        return None

    return rf"$\boldsymbol{{{body}}}$" if style == "bold" else f"${body}$"


def _fold_remaining(text: str) -> str:
    import unicodedata  # noqa: PLC0415

    out: list[str] = []
    for ch in text:
        if ord(ch) <= 0x7F:
            out.append(ch)
            continue
        # Latin letters with diacritics are safe under inputenc-utf8.
        if unicodedata.category(ch).startswith("L") and ord(ch) < 0x0250:
            out.append(ch)
            continue
        if 0x1D400 <= ord(ch) <= 0x1D7FF:
            mapped = _math_alphanumeric(ch)
            if mapped:
                out.append(mapped)
                continue
        # A combining mark on its own (U+0300-U+036F) decomposes to nothing.
        # Reporting it as a dropped character is correct but useless: what the
        # reader needs to know is that an accent was lost, not that an
        # invisible codepoint was. The base letter is already in `out`.
        if 0x0300 <= ord(ch) <= 0x036F:
            UNMAPPED.add(ch)
            continue
        folded = unicodedata.normalize("NFKD", ch)
        ascii_form = "".join(c for c in folded if ord(c) <= 0x7F)
        if ascii_form:
            out.append(ascii_form)
        else:
            UNMAPPED.add(ch)
            out.append("")
    return "".join(out)


_MARKER_TAIL = re.compile(r"[\s,;]*[\d*\u2020\u2021\u00a7]+\s*$")
_MARKER_HEAD = re.compile(r"^\s*[\d*\u2020\u2021\u00a7]+\s*[.)\-]?\s*")


def _clean_name(name: str) -> str:
    """Drop the affiliation markers Word leaves glued to a surname.

    "Ammari*1" is a name plus two markers, and printing it in the author block
    is both wrong and impossible for a reader to interpret -- the markers refer
    to affiliations that IEEEtran typesets separately.
    """
    return _MARKER_TAIL.sub("", name).strip()


def _strip_marker(text: str) -> str:
    """Drop a leading affiliation marker: "1 Department of ..." -> "Department..."."""
    return _MARKER_HEAD.sub("", text).strip()


# ---------------------------------------------------------------------------
# Fitting content to a two-column page
# ---------------------------------------------------------------------------
# Everything here is a character-count heuristic, deliberately. The exact width
# of a box is known only to TeX, after the fonts are loaded; a converter that
# waits for that answer cannot make a layout decision at all. Counting
# characters is crude, but it is the difference between a table that wraps and
# a table that prints over the next column, and it errs toward the safe side.

# Roughly how many characters of 10 pt body text fit on one line.
_COL_CHARS_TWO = 46          # one column of a two-column IEEE/MDPI page
_PAGE_CHARS_TWO = 96         # both columns, for a starred float
_COL_CHARS_ONE = 88          # a single-column, double-spaced manuscript


def _cell_text(cell) -> str:
    return " ".join(
        x.paragraph.plain_text() for x in cell.blocks if x.paragraph
    ).strip()


def _column_widths(tbl, ncols: int) -> list[int]:
    """Longest cell in each column, in characters, with a floor of 3."""
    widths = [3] * ncols
    for row in tbl.grid:
        col = 0
        for cell in row:
            if col < ncols:
                widths[col] = max(widths[col], len(_cell_text(cell)))
            col += max(1, cell.colspan)
    return widths


_MANUAL_NUMBER = re.compile(r"(?:\\[,;: ]|\\quad|\\qquad|~|\s)*\(\s*\d{1,3}[a-z]?\s*\)\s*$")


def _strip_manual_number(latex: str) -> str:
    r"""Remove an equation number the author typed into the equation itself.

    Word has no numbered-equation construct, so the number is literal text --
    usually in the right-hand cell of an invisible two-column table. LaTeX then
    numbers the equation as well and the line ends `(3) (3)`. The author's
    number is the one to drop: LaTeX's is consistent with \ref, and if the
    source skipped or repeated a number, keeping it would carry that error into
    the new manuscript.
    """
    return _MANUAL_NUMBER.sub("", latex).strip()


def _math_visual_length(latex: str) -> int:
    """Approximate printed width of a math body, in characters.

    Control sequences are one or two glyphs on the page but many characters in
    the source (`\quad` is four spaces wide, `\frac` is invisible), so the raw
    length overestimates by a factor that varies with the notation. Commands
    are therefore counted at a flat two glyphs and grouping characters at zero.
    """
    body = re.sub(r"\\label\{[^}]*\}", "", latex)
    commands = len(re.findall(r"\\[a-zA-Z]+", body))
    plain = re.sub(r"\\[a-zA-Z]+|[{}\\\s]", "", body)
    return len(plain) + 2 * commands


def _fit_equation(latex: str, profile) -> str:
    r"""Keep a display equation inside its column.

    Two steps, in this order, because they degrade the result by different
    amounts:

    1. **Break it.** Manuscripts routinely put two independent formulas on one
       line separated by `\quad\quad` -- that is how they were laid out in a
       single-column Word file. In a two-column class the pair does not fit,
       and the break is free: an `aligned` block loses nothing.
    2. **Scale it.** A single formula that is genuinely too wide gets
       `\resizebox`. This shrinks the type, which is why it is the fallback and
       not the first move; without it the equation simply runs off the page,
       which is what a reader of the PDF sees as text disappearing into the
       right margin.
    """
    if profile.docx.columns < 2:
        limit = _COL_CHARS_ONE
    else:
        limit = _COL_CHARS_TWO
    if _math_visual_length(latex) <= limit:
        return latex

    label = ""
    m = re.match(r"\s*(\\label\{[^}]*\})\s*", latex)
    if m:
        label, latex = m.group(1), latex[m.end():]

    parts = [p.strip() for p in re.split(r"\\quad\s*\\quad|\\qquad", latex)
             if p.strip()]

    def _wrap(payload: str) -> str:
        # No blank line, ever: inside `equation` an empty line is a paragraph
        # break, and TeX answers with "Missing $ inserted" pointing at a line
        # that looks perfectly fine.
        return "\n".join(x for x in (label, payload) if x)

    if len(parts) > 1 and all(_math_visual_length(x) <= limit for x in parts):
        rows = " \\\\\n    ".join(
            x.rstrip(",") + ("," if i < len(parts) - 1 else "")
            for i, x in enumerate(parts))
        return _wrap("  \\begin{aligned}\n    " + rows + "\n  \\end{aligned}")

    inner = " ".join(parts) if len(parts) > 1 else latex
    return _wrap(r"  \resizebox{\columnwidth}{!}{$\displaystyle "
                 + inner + "$}")


def _key(s: str) -> str:
    """A safe LaTeX label/citation key."""
    return re.sub(r"[^A-Za-z0-9]", "", s) or "x"


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------

_MATH_NOISE_RE = re.compile(r"(\\[,;: ]|\\quad|\\qquad|[\^_]\s*\{\s*\}|[{}\s\\])+")

# Marker left where an equation could not be converted. Visible on the page on
# purpose: a silently missing symbol in a published equation is far worse than
# an obvious placeholder the author has to fix.
MATH_PLACEHOLDER = r"\ensuremath{\blacksquare}"


def is_degenerate_math(latex: str) -> bool:
    if not latex or not latex.strip():
        return True
    return not _MATH_NOISE_RE.sub("", latex).strip()


def render_inlines(nodes: list[InlineNode]) -> str:
    out: list[str] = []
    for n in nodes:
        if n.kind == "math":
            if is_degenerate_math(n.text):
                # Emitting `$_{}$` or `$^{}^{}$` would be a compile error
                # (double superscript) as well as meaningless.
                out.append(MATH_PLACEHOLDER)
                DEGENERATE.append(n.text)
            else:
                out.append(f"${n.text}$")
            continue
        if n.kind == "break":
            out.append("\\\\\n")
            continue
        if n.kind == "link":
            label = escape(n.text or n.url)
            out.append(rf"\href{{{n.url}}}{{{label}}}" if n.url else label)
            continue
        if n.kind == "xref":
            out.append(rf"\ref{{{_key(n.target_id)}}}")
            continue
        if n.kind == "cite" and n.ref_ids:
            out.append(rf"\cite{{{','.join(_key(r) for r in n.ref_ids)}}}")
            continue

        t = escape(n.text)
        if not t:
            continue
        if n.code:
            t = rf"\texttt{{{t}}}"
        if n.subscript:
            t = rf"\textsubscript{{{t}}}"
        if n.superscript:
            t = rf"\textsuperscript{{{t}}}"
        if n.smallcaps:
            t = rf"\textsc{{{t}}}"
        if n.italic:
            t = rf"\emph{{{t}}}"
        if n.bold:
            t = rf"\textbf{{{t}}}"
        out.append(t)
    return "".join(out)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class RenderResult:
    out_dir: Path
    main_tex: Path
    notes: list[str] = field(default_factory=list)
    failed_figures: list[str] = field(default_factory=list)
    # What actually reached the page. A renderer that writes a syntactically
    # perfect document with no body is the worst failure this tool can have --
    # it compiles, so nothing complains -- and it happened. These counts exist
    # so the caller can refuse to present that as a result.
    sections: int = 0
    figures: int = 0
    tables: int = 0
    equations: int = 0
    body_words: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed_figures and self.sections > 0

    @property
    def empty_body(self) -> bool:
        return self.sections == 0 or self.body_words < 50


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class LatexRenderer:
    def __init__(self, ms: Manuscript, profile: JournalProfile,
                 media_dir: str | Path):
        self.ms = ms
        self.p = profile
        self.media_dir = Path(media_dir)
        self.notes: list[str] = []
        self.failed: list[str] = []
        self._narrow: list[str] = []
        self._fig_files: dict[str, list[str]] = {}

    # -- public ------------------------------------------------------------

    def render(self, out_dir: str | Path) -> RenderResult:
        out = Path(out_dir)
        (out / "figures").mkdir(parents=True, exist_ok=True)
        UNMAPPED.clear()
        DEGENERATE.clear()

        self._prepare_figures(out / "figures")
        tex = self._document()
        main = out / "main.tex"
        main.write_text(tex, encoding="utf-8")
        (out / "refs.bib").write_text(self._bibtex(), encoding="utf-8")

        stats = self._output_stats(tex)
        if stats["sections"] == 0:
            self.notes.append(
                "**The document has no body sections.** Everything in the "
                "manuscript is under a section whose role is front matter, or "
                "the section tree is empty. The file will compile and will "
                "contain only the title, abstract and references. Check the "
                "Sections panel before using this output.")
        (out / "BUILD.md").write_text(self._build_notes(stats), encoding="utf-8")

        return RenderResult(out, main, self.notes, self.failed,
                            sections=stats["sections"], figures=stats["figures"],
                            tables=stats["tables"], equations=stats["equations"],
                            body_words=stats["body_words"])

    @staticmethod
    def _output_stats(tex: str) -> dict[str, int]:
        """Count what is in the emitted file, not what was in the IR.

        Counting the IR would answer the wrong question: the failure mode being
        guarded against is content that exists in the IR and never reaches the
        document.
        """
        body = tex
        for marker in (r"\end{IEEEkeywords}", r"\end{abstract}", r"\maketitle"):
            if marker in body:
                body = body.split(marker, 1)[1]
                break
        if r"\begin{thebibliography}" in body:
            body = body.split(r"\begin{thebibliography}", 1)[0]
        prose = re.sub(r"\\[a-zA-Z@]+\*?(\[[^]]*])?(\{[^{}]*})?", " ", body)
        return {
            "sections": len(re.findall(r"\\section\b", body)),
            "figures": len(re.findall(r"\\includegraphics\b", tex)),
            "tables": len(re.findall(r"\\begin\{tabular", tex)),
            "equations": len(re.findall(r"\\begin\{equation", tex)),
            "body_words": len(prose.split()),
        }

    # -- figures -----------------------------------------------------------

    def _prepare_figures(self, fig_dir: Path) -> None:
        for fig in self.ms.figures:
            kept: list[str] = []
            for rel in fig.files:
                src = self.media_dir / rel
                res = assets.prepare_for_latex(src, fig_dir)
                if res.ok:
                    kept.append(res.output.name)
                    if res.converted:
                        self.notes.append(
                            f"{fig.id}: converted `{rel}` -> "
                            f"`{res.output.name}` via {res.method}."
                        )
                else:
                    self.failed.append(fig.id)
                    self.notes.append(f"**{fig.id}: `{rel}` NOT converted** - {res.error}")
            self._fig_files[fig.id] = kept

    # -- document ----------------------------------------------------------

    def _document(self) -> str:
        fam = self.p.latex.template_family
        parts = [self._preamble(), ""]

        if fam == "elsarticle":
            parts += self._front_elsarticle()
        elif fam == "IEEEtran":
            parts += self._front_ieeetran()
        else:
            parts += self._front_generic()

        parts.append("")
        for sec in self.ms.body:
            parts.append(self._section(sec, depth=0))

        parts.append(self._bibliography())
        parts.append(r"\end{document}")
        return "\n".join(x for x in parts if x is not None) + "\n"

    def _preamble(self) -> str:
        L = self.p.latex
        opts = f"[{','.join(L.class_options)}]" if L.class_options else ""
        lines = [
            "% Generated by retypeset. Body text is the author's, unmodified.",
            f"% Target: {self.p.publisher} / {self.p.journal}",
            rf"\documentclass{opts}{{{L.document_class}}}",
            "",
            r"\usepackage[T1]{fontenc}",
            r"\usepackage[utf8]{inputenc}",
            r"\usepackage{amsmath,amssymb}",
            r"\usepackage{graphicx}",
            r"\usepackage{booktabs}",
            r"\usepackage{multirow}",
            r"\usepackage{array}",
            r"\usepackage{url}",
        ]
        lines += [rf"\usepackage{{{pkg}}}" for pkg in L.preamble_packages]
        # hyperref must be loaded explicitly for every family: elsarticle does
        # not preload it, so \href from author e-mail addresses and ORCIDs is
        # undefined without this. It is loaded last, as hyperref requires.
        lines += [
            r"\usepackage[hidelinks]{hyperref}",
            r"\providecommand{\href}[2]{\texttt{#2}}",
        ]
        lines += [
            r"\graphicspath{{figures/}}",
            r"\providecommand{\keywordsname}{Keywords}",
            "",
            r"\begin{document}",
        ]
        return "\n".join(lines)

    # -- front matter per family ------------------------------------------

    def _authors_plain(self) -> str:
        return r" \and ".join(escape(a.display()) for a in self.ms.meta.authors) \
            or r"\relax"

    def _front_generic(self) -> list[str]:
        m = self.ms.meta
        out = [
            rf"\title{{{escape(m.title)}}}",
            rf"\author{{{self._authors_plain()}}}",
            r"\date{}",
            r"\maketitle",
            "",
        ]
        if m.abstract_raw:
            out += [r"\begin{abstract}", self._abstract_body(), r"\end{abstract}", ""]
        if m.keywords:
            out.append(rf"\noindent\textbf{{\keywordsname:}} "
                       rf"{', '.join(escape(k) for k in m.keywords)}")
            out.append("")
        return out

    def _front_elsarticle(self) -> list[str]:
        m = self.ms.meta
        out = [r"\begin{frontmatter}", "", rf"\title{{{escape(m.title)}}}", ""]

        aff_key = {a.id: i + 1 for i, a in enumerate(m.affiliations)}
        for a in m.authors:
            marks = "".join(f"[{aff_key[x]}]" for x in a.affiliation_ids if x in aff_key)
            star = r"\corref{cor1}" if a.corresponding else ""
            out.append(rf"\author{marks}{{{escape(a.display())}}}{star}")
            if a.corresponding and a.email:
                out.append(rf"\ead{{{a.email}}}")
        if any(a.corresponding for a in m.authors):
            out.append(r"\cortext[cor1]{Corresponding author}")
        for a in m.affiliations:
            out.append(rf"\affiliation[{aff_key[a.id]}]{{organization={{{escape(a.raw)}}}}}")

        out.append("")
        if m.highlights:
            out.append(r"\begin{highlights}")
            out += [rf"\item {escape(h)}" for h in m.highlights]
            out += [r"\end{highlights}", ""]
        if m.abstract_raw:
            out += [r"\begin{abstract}", self._abstract_body(), r"\end{abstract}", ""]
        if m.keywords:
            out.append(r"\begin{keyword}")
            out.append(" \\sep ".join(escape(k) for k in m.keywords))
            out += [r"\end{keyword}", ""]
        out += [r"\end{frontmatter}", ""]
        return out

    def _front_ieeetran(self) -> list[str]:
        """Title, authors, affiliations and the corresponding-author note.

        IEEEtran wants names in `\IEEEauthorblockN` and each affiliation in its
        own `\IEEEauthorblockA`; the corresponding address belongs in
        `\thanks`, which the class typesets as the first-page footnote. The
        earlier version emitted only the name block, so the affiliations --
        parsed, present in the IR, and required by every journal -- did not
        reach the page at all, and the affiliation markers stayed glued to the
        surnames.
        """
        m = self.ms.meta
        names = [escape(_clean_name(a.display())) for a in m.authors]
        authors = ",~".join(n for n in names if n) or r"\relax"

        block = [rf"\IEEEauthorblockN{{{authors}}}"]
        for aff in m.affiliations:
            text = escape(_strip_marker(aff.raw))
            if text:
                block.append(rf"\IEEEauthorblockA{{{text}}}")

        corr = next((a for a in m.authors if a.corresponding), None)
        email = (corr.email if corr and corr.email else m.corresponding_email or "")
        if corr or email:
            who = escape(_clean_name(corr.display())) if corr else ""
            note = "Corresponding author" + (f": {who}" if who else "")
            if email:
                note += rf" (e-mail: \texttt{{{escape(email)}}})"
            block.append(rf"\thanks{{{note}.}}")

        out = [
            rf"\title{{{escape(m.title)}}}",
            r"\author{" + "\n".join(block) + "}",
            r"\maketitle",
            "",
        ]
        if m.abstract_raw:
            out += [r"\begin{abstract}", self._abstract_body(), r"\end{abstract}", ""]
        if m.keywords:
            out.append(r"\begin{IEEEkeywords}")
            out.append(", ".join(escape(k) for k in sorted(m.keywords, key=str.lower)))
            out += [r"\end{IEEEkeywords}", ""]
        return out

    _RUN_IN_KEYWORDS = re.compile(r"^\s*(key\s*words?|index terms?)\s*[:\-\u2013]",
                                  re.I)

    def _abstract_body(self) -> str:
        """The abstract, without a run-in keyword line.

        The parser already splits that line off, but an IR corrected by hand --
        or produced by an older version -- can still carry it, and printing it
        here duplicates the keywords the class prints from its own macro two
        lines further down.
        """
        blocks = [b for b in self.ms.meta.abstract if b.paragraph]
        if self.ms.meta.keywords:
            blocks = [b for b in blocks
                      if not self._RUN_IN_KEYWORDS.match(b.paragraph.plain_text())]
        if blocks:
            return "\n".join(render_inlines(b.paragraph.inlines)
                              for b in blocks).strip()
        raw = self.ms.meta.abstract_raw
        if self.ms.meta.keywords:
            raw = re.split(r"(?:^|\s)(?:key\s*words?|index terms?)\s*[:\-\u2013]",
                           raw, maxsplit=1, flags=re.I)[0].strip()
        return escape(raw)

    # -- body --------------------------------------------------------------

    _HEADINGS = [r"\section", r"\subsection", r"\subsubsection", r"\paragraph"]

    _EMITTED_ELSEWHERE = (SectionRole.ABSTRACT, SectionRole.KEYWORDS,
                          SectionRole.HIGHLIGHTS, SectionRole.REFERENCES,
                          SectionRole.TITLE, SectionRole.AUTHORS,
                          SectionRole.AFFILIATIONS)

    def _section(self, sec: Section, depth: int) -> str:
        # Front matter is emitted above; the reference list is emitted below.
        # Their *children*, however, are ordinary body sections and must still
        # be rendered. Returning "" for the whole subtree here is how a single
        # role change in the Sections panel silently produced a paper with no
        # body: a manuscript whose title is styled as Heading 1 has every real
        # section nested under it, so marking that node `title` deleted all of
        # them. Skipping one node is right; skipping its descendants is not.
        if sec.role in self._EMITTED_ELSEWHERE:
            if not sec.children:
                return ""
            self.notes.append(
                f"Section `{sec.title_raw or sec.id}` is front matter "
                f"(`{sec.role.value}`) but has {len(sec.children)} nested "
                "section(s); those were rendered as body at the same level.")
            return "\n".join(self._section(c, depth) for c in sec.children).strip()

        out: list[str] = []
        if sec.title_raw:
            cmd = self._HEADINGS[min(depth, len(self._HEADINGS) - 1)]
            # Strip the author's manual numbering; LaTeX numbers sections itself.
            title = sec.title_raw
            if sec.numbering_raw and title.startswith(sec.numbering_raw):
                title = title[len(sec.numbering_raw):].lstrip(" .)-\u2013")
            starred = "*" if sec.role in (
                SectionRole.ACKNOWLEDGEMENTS, SectionRole.NOMENCLATURE,
                SectionRole.CONFLICT_OF_INTEREST, SectionRole.FUNDING,
                SectionRole.DATA_AVAILABILITY, SectionRole.AUTHOR_CONTRIBUTIONS,
            ) else ""
            out.append(rf"{cmd}{starred}{{{escape(title)}}}"
                       rf"\label{{sec:{_key(sec.id)}}}")
            out.append("")

        for b in sec.blocks:
            rendered = self._block(b)
            if rendered:
                out.append(rendered)
                out.append("")

        for child in sec.children:
            out.append(self._section(child, depth + 1))
        return "\n".join(out)

    def _block(self, b: Block) -> str:
        if b.kind == "paragraph" and b.paragraph:
            return render_inlines(b.paragraph.inlines).strip()

        if b.kind == "equation_ref":
            eq = self.ms.equation(b.target_id)
            if not eq:
                return ""
            if is_degenerate_math(eq.latex):
                DEGENERATE.append(eq.id)
                return (f"% [retypeset] {eq.id}: OMML could not be converted.\n"
                        f"\\begin{{equation}}\\label{{eq:{_key(eq.id)}}}\n"
                        f"{MATH_PLACEHOLDER}\\text{{ [retypeset: retype this equation]}}\n"
                        f"\\end{{equation}}")
            body = _fit_equation(_strip_manual_number(eq.latex), self.p)
            if body != eq.latex:
                self._narrow.append(eq.id)
            if eq.number is None:
                return f"\\begin{{equation*}}\n{body}\n\\end{{equation*}}"
            return (f"\\begin{{equation}}\\label{{eq:{_key(eq.id)}}}\n"
                    f"{body}\n\\end{{equation}}")

        if b.kind == "figure_ref":
            fig = self.ms.figure(b.target_id)
            return self._figure(fig) if fig else ""

        if b.kind == "table_ref":
            tbl = self.ms.table(b.target_id)
            return self._table(tbl) if tbl else ""

        if b.kind == "list" and b.list_block:
            env = "enumerate" if b.list_block.ordered else "itemize"
            lines = [rf"\begin{{{env}}}"]
            for item in b.list_block.items:
                inner = " ".join(x for x in (self._block(sub) for sub in item) if x)
                lines.append(rf"\item {inner}")
            lines.append(rf"\end{{{env}}}")
            return "\n".join(lines)

        if b.kind == "code":
            return "\\begin{verbatim}\n" + b.code_text + "\n\\end{verbatim}"

        if b.kind == "quote":
            inner = "\n".join(x for x in (self._block(s) for s in b.quote_blocks) if x)
            return "\\begin{quote}\n" + inner + "\n\\end{quote}"

        return ""

    def _figure(self, fig: Figure) -> str:
        files = self._fig_files.get(fig.id, [])
        if not files:
            return f"% [retypeset] {fig.id} omitted: no usable image file."

        star, width = self._figure_placement(fig)

        lines = [rf"\begin{{figure{star}}}[!t]", r"  \centering"]
        for name in files:
            lines.append(rf"  \includegraphics[width={width}]{{{name}}}")
        caption = (render_inlines(fig.caption) if fig.caption
                   else escape(_strip_label(fig.caption_raw)))
        lines.append(rf"  \caption{{{caption}}}")
        lines.append(rf"  \label{{fig:{_key(fig.id)}}}")
        lines.append(rf"\end{{figure{star}}}")
        return "\n".join(lines)

    def _figure_placement(self, fig: Figure) -> tuple[str, str]:
        r"""Decide between one column and the full page width.

        In a single-column class the author's placed width maps directly. In a
        two-column class it does not, and the obvious rule -- "as wide in the
        source as the text block, therefore full width here" -- makes every
        figure in a Word manuscript a `figure*`, because Word manuscripts are
        single-column and their figures are all about as wide as the page.

        Shape is the usable signal. A figure that is much wider than it is tall
        becomes unreadable at 88 mm, so it spans; a roughly square or portrait
        figure loses nothing in one column and costs the layout far less.
        """
        f = self.p.figures
        if self.p.docx.columns < 2:
            return "", assets.figure_width_fraction(
                fig.placed_width_mm or 0.0, f.single_column_mm,
                f.double_column_mm, 1)

        w, h = fig.placed_width_mm or 0.0, fig.placed_height_mm or 0.0
        aspect = (w / h) if (w and h) else 0.0
        if aspect >= 1.8 and w >= f.single_column_mm * 1.2:
            self.notes.append(
                f"{fig.id}: {w:.0f}x{h:.0f} mm in the source (aspect "
                f"{aspect:.1f}), too wide to stay legible in one column - "
                "placed across both.")
            return "*", r"\textwidth"
        return "", r"\columnwidth"

    def _table(self, tbl: Table) -> str:
        if not tbl.grid:
            return ""
        ncols = max(len(r) for r in tbl.grid)
        caption = (render_inlines(tbl.caption) if tbl.caption
                   else escape(_strip_label(tbl.caption_raw)))

        # How wide does this table actually want to be? Counting columns is not
        # the same question, and answering the wrong one is what put Table II
        # -- four columns, but 90 characters of prose in its widest row -- into
        # a single column, where it printed straight over the text beside it.
        widths = _column_widths(tbl, ncols)
        natural = sum(widths) + 3 * (ncols - 1)
        two_col = self.p.docx.columns >= 2

        if not two_col:
            star, avail, size = "", _COL_CHARS_ONE, ""
        elif natural <= _COL_CHARS_TWO:
            star, avail, size = "", _COL_CHARS_TWO, ""
        else:
            star, avail = "*", _PAGE_CHARS_TWO
            size = r"  \small" if natural <= _PAGE_CHARS_TWO * 1.3 else r"  \footnotesize"
            if natural > _PAGE_CHARS_TWO:
                self.notes.append(
                    f"{tbl.id}: {natural} characters wide at its widest row, "
                    "which does not fit the page even across both columns. The "
                    "cells were set to wrap; check the result and consider "
                    "splitting the table or abbreviating the long cells.")

        # Fixed-width paragraph columns rather than `l`: an `l` column cannot
        # wrap, so one long cell pushes the whole table past the margin with no
        # error -- only an overfull box in the log that nobody reads.
        if natural > avail:
            target = r"\textwidth" if star else r"\columnwidth"
            total = sum(widths) or 1
            # Subtract the inter-column padding explicitly rather than leaving
            # a percentage of slack for it. LaTeX adds 2\tabcolsep per column,
            # which at six columns is a seventh of the page -- a table sized to
            # "94 % of the width" then overflows by exactly that much, silently.
            spec = "".join(
                rf"p{{\dimexpr {w / total:.3f}{target}-2\tabcolsep\relax}}"
                for w in widths)
        else:
            spec = "l" * ncols

        lines = [rf"\begin{{table{star}}}[!t]", r"  \centering"]
        if size:
            lines.append(size)
        if self.p.figures.table_caption_position == "above":
            lines.append(rf"  \caption{{{caption}}}")
            lines.append(rf"  \label{{tab:{_key(tbl.id)}}}")
        lines.append(rf"  \begin{{tabular}}{{{spec}}}")
        lines.append(r"    \toprule")

        for i, row in enumerate(tbl.grid):
            cells = []
            for cell in row:
                inner = " ".join(
                    render_inlines(x.paragraph.inlines)
                    for x in cell.blocks if x.paragraph
                ).strip()
                if cell.colspan > 1:
                    inner = rf"\multicolumn{{{cell.colspan}}}{{c}}{{{inner}}}"
                cells.append(inner)
            cells += [""] * (ncols - len(cells))
            lines.append("    " + " & ".join(cells) + r" \\")
            if i + 1 == tbl.header_rows:
                lines.append(r"    \midrule")

        lines.append(r"    \bottomrule")
        lines.append(r"  \end{tabular}")
        if self.p.figures.table_caption_position == "below":
            lines.append(rf"  \caption{{{caption}}}")
            lines.append(rf"  \label{{tab:{_key(tbl.id)}}}")
        lines.append(rf"\end{{table{star}}}")
        return "\n".join(lines)

    # -- bibliography ------------------------------------------------------

    def _bibliography(self) -> str:
        refs = self.ms.references
        if not refs:
            return ""
        width = str(len(refs))
        lines = ["", rf"\begin{{thebibliography}}{{{width}}}", ""]
        for r in refs:
            lines.append(rf"\bibitem{{{_key(r.id)}}}")
            lines.append(escape(_strip_ref_marker(r.raw)))
            lines.append("")
        lines.append(r"\end{thebibliography}")
        lines.append("")
        parsed = sum(1 for r in refs if r.parse_confidence >= 0.6)
        if parsed == len(refs):
            lines.append("% Every reference also parsed into refs.bib. To use "
                         "BibTeX instead, replace the block above with:")
            lines.append(rf"%   \bibliographystyle{{{self.p.latex.bibliography_style}}}")
            lines.append(r"%   \bibliography{refs}")
        else:
            lines.append(f"% Only {parsed} of {len(refs)} references parsed into "
                         "refs.bib, so this verbatim list is the complete one.")
            lines.append("% Do not switch to \\bibliography{refs}: the entries "
                         "that did not parse would vanish.")
        return "\n".join(lines)

    def _bibtex(self) -> str:
        """BibTeX for the entries that parsed cleanly enough to be trustworthy.

        The header states the count, because a `refs.bib` holding 1 of 38
        references looks like a working bibliography and is not one. Which
        entries are missing is listed as well: with a hand-typed bibliography
        the regex parser is unreliable, and the honest fallback -- the verbatim
        `thebibliography` block in main.tex -- is already correct.
        """
        kept = [r for r in self.ms.references if r.parse_confidence >= 0.6]
        total = len(self.ms.references)
        out = ["% Generated by retypeset from CSL-JSON.",
               f"% {len(kept)} of {total} reference(s) parsed with confidence "
               ">= 0.6 and appear here.",
               "% The remaining entries are in main.tex, verbatim, inside "
               "thebibliography -- which is",
               "% what the document uses by default. Do NOT switch to "
               "\\bibliography{refs} unless this",
               "% file covers every reference, or citations will silently "
               "disappear from the PDF.", ""]
        if len(kept) < total:
            missing = ", ".join(r.id for r in self.ms.references
                                if r.parse_confidence < 0.6)
            out += [f"% Missing: {missing}", ""]
        for r in self.ms.references:
            if r.parse_confidence < 0.6:
                continue
            c = r.csl
            fields = []
            if c.get("author"):
                fields.append(("author", " and ".join(
                    f"{a.get('family','')}, {a.get('given','')}".strip(", ")
                    for a in c["author"])))
            if c.get("title"):
                fields.append(("title", c["title"]))
            if c.get("container-title"):
                fields.append(("journal", c["container-title"]))
            issued = (c.get("issued") or {}).get("date-parts") or []
            if issued and issued[0]:
                fields.append(("year", str(issued[0][0])))
            for k in ("volume", "issue", "page", "DOI", "URL"):
                if c.get(k):
                    fields.append((k.lower() if k not in ("DOI", "URL") else k.lower(),
                                   str(c[k])))
            if not fields:
                continue
            out.append(f"@article{{{_key(r.id)},")
            out += [f"  {k} = {{{v}}}," for k, v in fields]
            out.append("}")
            out.append("")
        return "\n".join(out)

    # -- notes -------------------------------------------------------------

    def _build_notes(self, stats: dict[str, int] | None = None) -> str:
        stats = stats or {}
        L = [
            f"# Build notes - {self.p.publisher} / {self.p.journal}",
            "",
            "```",
            "pdflatex main && pdflatex main",
            "```",
            "",
        ]
        if stats:
            renderable = sum(1 for x in self.ms.body
                             if x.role not in self._EMITTED_ELSEWHERE)
            L += [
                "## What reached the document",
                "",
                "| | in the manuscript | in `main.tex` |",
                "|---|---|---|",
                f"| Body sections | {renderable} | {stats['sections']} |",
                f"| Figures | {len(self.ms.figures)} | {stats['figures']} |",
                f"| Tables | {len(self.ms.tables)} | {stats['tables']} |",
                f"| Display equations | {len(self.ms.equations)} | "
                f"{stats['equations']} |",
                f"| Body words | {self.ms.stats.get('words', 0)} (whole file) "
                f"| {stats['body_words']} |",
                "",
                "Read this table before compiling. The two columns are counted "
                "independently -- the left from the parsed manuscript, the "
                "right from the emitted file -- so a row that does not match is "
                "content that did not survive rendering.",
                "",
            ]
            if stats.get("sections", 0) == 0:
                L += [
                    "> **This document has no body.** Every section is nested "
                    "under one whose role is front matter (`title`, `abstract`, "
                    "`keywords`, `references`), or the section tree is empty. "
                    "The file will still compile, which is exactly why this "
                    "warning is here. Fix the roles in the Sections panel and "
                    "generate again.",
                    "",
                ]
        L += [
            f"Document class `{self.p.latex.document_class}`. If your TeX "
            "distribution does not have it, install the publisher's template "
            "package (MiKTeX offers to fetch it automatically; on TeX Live use "
            "`tlmgr install`).",
            "",
        ]
        if self.failed:
            L += ["## Figures that could not be converted", ""]
            L += [f"- {x}" for x in dict.fromkeys(self.failed)]
            L.append("")
        if DEGENERATE:
            L += [
                "## Equations that must be retyped",
                "",
                f"{len(DEGENERATE)} piece(s) of mathematics arrived from Pandoc "
                "empty or malformed and were replaced with a black square "
                "placeholder. Pandoc cannot map every OMML construct - usually "
                "Symbol-font characters or Cambria Math private-use glyphs - and "
                "it gives no warning when it fails. Search the source for "
                "`[retypeset: retype this equation]` and `\\blacksquare`.",
                "",
            ]
        if UNMAPPED:
            L += [
                "## Characters dropped",
                "",
                "These had no LaTeX equivalent and no ASCII decomposition, so "
                "they were removed rather than left to fail the compile. "
                "Check the places they occurred:",
                "",
                "".join(f"`{c}` (U+{ord(c):04X}) " for c in sorted(UNMAPPED)),
                "",
            ]
        if self._narrow:
            L += [
                "## Equations that were reflowed to fit the column",
                "",
                f"{len(self._narrow)} display equation(s) were too wide for a "
                "column of this class. Where the source put two formulas on one "
                "line -- the single-column Word habit -- they were broken into "
                "an `aligned` block; a formula too wide on its own was scaled "
                "with `\\resizebox`, which reduces its type size. Check these "
                "and consider breaking them by hand: "
                + ", ".join(dict.fromkeys(self._narrow)) + ".",
                "",
            ]
        if self.notes:
            L += ["## Conversion log", ""] + [f"- {n}" for n in self.notes] + [""]
        L += [
            "## Things a human must still check",
            "",
            "- **Citations.** In-text markers are the author's original text. "
            "If the source used `[3]` and the target journal wants author-year, "
            "that conversion is not automatic.",
            "- **Float placement.** Figures and tables are placed at `[!t]`; "
            "LaTeX will move them.",
            "- **Cross-references.** `\\label`s are emitted for every section, "
            "figure, table and equation, but the author's inline references to "
            "them are still literal text, not `\\ref`.",
            "- **Equation numbering.** Numbers come from LaTeX, so they are "
            "sequential; if the source skipped or repeated a number, the output "
            "will differ from the original.",
        ]
        return "\n".join(L)


def _strip_label(caption: str) -> str:
    return re.sub(r"^\s*(fig(?:ure)?|tab(?:le)?|scheme|chart)\s*\.?\s*"
                  r"[A-Z]?\d+(?:[.\-]\d+)?\s*[.:\-\u2013\u2014)]?\s*",
                  "", caption, flags=re.I).strip()


def _strip_ref_marker(raw: str) -> str:
    return re.sub(r"^\s*(?:\[\d{1,3}\]|\d{1,3}\s*[.)])\s+", "", raw).strip()


def render_latex(ms: Manuscript, profile: JournalProfile,
                 media_dir: str | Path, out_dir: str | Path) -> RenderResult:
    return LatexRenderer(ms, profile, media_dir).render(out_dir)
