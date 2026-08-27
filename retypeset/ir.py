"""
retypeset.ir -- Intermediate Representation for journal-agnostic manuscripts.

Design contract
---------------
The IR is the ONLY thing renderers consume. It is publisher-neutral: it stores
*semantics* (this is the abstract; this is display equation #3; this figure has
this caption) and never presentation (font, margins, column count). Presentation
lives exclusively in the journal style profile.

Two invariants that must never be violated:

  1. LOSSLESS BODY TEXT. Every character of author-written prose reaches the
     renderer unmodified. No LLM ever rewrites `InlineNode.text`. Generative
     models may only *label* (assign SectionRole), never *generate*.

  2. RAW FALLBACK. Every structure that we parse heuristically (references,
     captions, author strings) retains its original source string in a `raw`
     field, so a downstream renderer can always emit the untouched original if
     our parse was wrong.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Section taxonomy
# ---------------------------------------------------------------------------

class SectionRole(str, Enum):
    """Canonical semantic role of a top-level section.

    Journal style profiles express structural requirements against these roles
    (e.g. Elsevier: abstract <= 150 words; IEEE: no `data_availability`), so the
    vocabulary must stay publisher-neutral and closed.
    """

    TITLE = "title"
    AUTHORS = "authors"
    AFFILIATIONS = "affiliations"
    ABSTRACT = "abstract"
    KEYWORDS = "keywords"
    HIGHLIGHTS = "highlights"
    NOMENCLATURE = "nomenclature"
    INTRODUCTION = "introduction"
    RELATED_WORK = "related_work"
    THEORY = "theory"
    METHODS = "methods"
    EXPERIMENTAL = "experimental"
    RESULTS = "results"
    DISCUSSION = "discussion"
    RESULTS_DISCUSSION = "results_and_discussion"
    CONCLUSION = "conclusion"
    FUTURE_WORK = "future_work"
    ACKNOWLEDGEMENTS = "acknowledgements"
    FUNDING = "funding"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    AUTHOR_CONTRIBUTIONS = "author_contributions"
    DATA_AVAILABILITY = "data_availability"
    ETHICS = "ethics"
    APPENDIX = "appendix"
    REFERENCES = "references"
    UNKNOWN = "unknown"


# Roles that are metadata, not body prose. Renderers place these via the style
# profile's front-matter macros rather than as numbered sections.
FRONT_MATTER_ROLES: frozenset[SectionRole] = frozenset({
    SectionRole.TITLE,
    SectionRole.AUTHORS,
    SectionRole.AFFILIATIONS,
    SectionRole.ABSTRACT,
    SectionRole.KEYWORDS,
    SectionRole.HIGHLIGHTS,
})

BACK_MATTER_ROLES: frozenset[SectionRole] = frozenset({
    SectionRole.ACKNOWLEDGEMENTS,
    SectionRole.FUNDING,
    SectionRole.CONFLICT_OF_INTEREST,
    SectionRole.AUTHOR_CONTRIBUTIONS,
    SectionRole.DATA_AVAILABILITY,
    SectionRole.ETHICS,
    SectionRole.APPENDIX,
    SectionRole.REFERENCES,
})


class Provenance(BaseModel):
    """How a given field was determined. Drives the review UI and the audit."""

    # "model" = a locally trained classifier (retypeset.learn); "style" = the author's
    # own Word heading style, which is the strongest signal available.
    method: Literal[
        "explicit", "style", "heuristic", "model", "llm", "default"
    ] = "heuristic"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    note: str = ""


# ---------------------------------------------------------------------------
# Inline content
# ---------------------------------------------------------------------------

class InlineNode(BaseModel):
    """A run of inline content.

    `text` is verbatim author prose for kind="text" and is never altered.
    For kind="math" it holds LaTeX (converted from OMML by Pandoc).
    For kind="cite" it holds the citation marker as it appeared in the source,
    with `ref_ids` resolving to `Manuscript.references`.
    """

    kind: Literal[
        "text", "math", "cite", "xref", "footnote", "link", "break",
    ] = "text"
    text: str = ""
    # Character styling that carries meaning rather than house style.
    bold: bool = False
    italic: bool = False
    superscript: bool = False
    subscript: bool = False
    smallcaps: bool = False
    code: bool = False
    # kind-specific payloads
    url: str = ""                     # link
    target_id: str = ""               # xref -> Figure.id / Table.id / Equation.id
    ref_ids: list[str] = Field(default_factory=list)   # cite
    footnote_id: str = ""             # footnote


class Paragraph(BaseModel):
    id: str = ""
    inlines: list[InlineNode] = Field(default_factory=list)
    # Source Word style name, retained for debugging and for re-detection passes.
    source_style: str = ""

    def plain_text(self) -> str:
        """Concatenated visible text. Math is rendered as $...$ for matching."""
        out: list[str] = []
        for n in self.inlines:
            if n.kind == "math":
                out.append(f"${n.text}$")
            elif n.kind == "break":
                out.append(" ")
            else:
                out.append(n.text)
        return "".join(out)


class ListBlock(BaseModel):
    id: str = ""
    ordered: bool = False
    items: list[list["Block"]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Floats and equations
# ---------------------------------------------------------------------------

class Figure(BaseModel):
    id: str                                  # "fig1"
    label: str = ""                          # "Figure 1" as it read in the source
    number: int | None = None
    files: list[str] = Field(default_factory=list)   # relative paths into media/
    caption: list[InlineNode] = Field(default_factory=list)
    caption_raw: str = ""
    # Populated by the asset auditor, consumed by style-profile validation.
    width_px: int | None = None
    height_px: int | None = None
    dpi: float | None = None
    # Size Word actually placed the figure at, from the OOXML drawing extent.
    # This is the only meaningful size for vector art, where "pixels" do not
    # exist, and it is also what decides whether a raster meets the journal's
    # dpi requirement *at printed size*.
    placed_width_mm: float = 0.0
    placed_height_mm: float = 0.0
    fmt: str = ""                            # png / jpeg / emf / eps / pdf
    is_vector: bool = False
    needs_conversion: bool = False           # EMF/WMF must be converted for LaTeX
    provenance: Provenance = Field(default_factory=Provenance)


class Table(BaseModel):
    id: str                                  # "tab1"
    label: str = ""
    number: int | None = None
    caption: list[InlineNode] = Field(default_factory=list)
    caption_raw: str = ""
    header_rows: int = 1
    # grid[row][col]; each cell is a list of blocks so cells may hold math/lists.
    grid: list[list["TableCell"]] = Field(default_factory=list)
    notes: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class TableCell(BaseModel):
    blocks: list["Block"] = Field(default_factory=list)
    rowspan: int = 1
    colspan: int = 1
    align: Literal["left", "center", "right", "default"] = "default"


class Equation(BaseModel):
    id: str                                  # "eq1"
    latex: str
    display: bool = True
    number: int | None = None                # None => unnumbered
    number_raw: str = ""                     # "(3)" / "(A.2)" as printed
    provenance: Provenance = Field(default_factory=Provenance)


class Footnote(BaseModel):
    id: str
    blocks: list["Block"] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Block-level content
# ---------------------------------------------------------------------------

class Block(BaseModel):
    """Tagged union of block-level content.

    A single class with an explicit `kind` discriminator keeps the JSON flat and
    trivially round-trippable, at the cost of some unused fields per node.
    """

    kind: Literal[
        "paragraph", "list", "figure_ref", "table_ref", "equation_ref",
        "code", "quote", "raw",
    ] = "paragraph"
    paragraph: Paragraph | None = None
    list_block: ListBlock | None = None
    # Floats are stored once in Manuscript.figures/tables/equations; the body
    # holds only an anchor so renderers can honour journal float placement
    # policy (inline vs. end-of-document).
    target_id: str = ""
    code_text: str = ""
    code_lang: str = ""
    quote_blocks: list["Block"] = Field(default_factory=list)
    raw_text: str = ""


class Section(BaseModel):
    id: str = ""
    level: int = 1
    role: SectionRole = SectionRole.UNKNOWN
    role_provenance: Provenance = Field(default_factory=Provenance)
    title_raw: str = ""                      # "3.2 Mho relay characteristic"
    title: list[InlineNode] = Field(default_factory=list)
    numbering_raw: str = ""                  # "3.2" stripped from title_raw
    blocks: list[Block] = Field(default_factory=list)
    children: list["Section"] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

class Affiliation(BaseModel):
    id: str                                  # "aff1"
    marker: str = ""                         # "1" / "a" / "*"
    raw: str = ""
    department: str = ""
    institution: str = ""
    city: str = ""
    country: str = ""
    provenance: Provenance = Field(default_factory=Provenance)


class Author(BaseModel):
    id: str = ""
    raw: str = ""
    given: str = ""
    family: str = ""
    suffix: str = ""
    email: str = ""
    orcid: str = ""
    affiliation_ids: list[str] = Field(default_factory=list)
    corresponding: bool = False
    equal_contribution: bool = False
    provenance: Provenance = Field(default_factory=Provenance)

    def display(self) -> str:
        return f"{self.given} {self.family}".strip() or self.raw


class Reference(BaseModel):
    """A bibliography entry.

    `csl` holds CSL-JSON, which is what makes journal-to-journal restyling a
    solved problem: pair it with a CSL file (the Zotero style repository covers
    ~10k journals) and citeproc emits the correct format. `raw` is the verbatim
    source string and is the fallback whenever `parse_confidence` is low.
    """

    id: str                                  # "ref1" -- also the BibTeX key stem
    raw: str
    order: int = 0
    csl: dict[str, Any] = Field(default_factory=dict)
    doi: str = ""
    url: str = ""
    parse_confidence: float = 0.0
    provenance: Provenance = Field(default_factory=Provenance)


class Metadata(BaseModel):
    title: str = ""
    title_inlines: list[InlineNode] = Field(default_factory=list)
    short_title: str = ""
    authors: list[Author] = Field(default_factory=list)
    affiliations: list[Affiliation] = Field(default_factory=list)
    abstract: list[Block] = Field(default_factory=list)
    abstract_raw: str = ""
    keywords: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    corresponding_email: str = ""
    source_file: str = ""
    source_language: str = "en"


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

class ParseIssue(BaseModel):
    """A single fidelity concern raised during parsing.

    Severity semantics:
      error   -- content was lost or is certainly wrong; do not render silently
      warning -- content survived but a heuristic may have mislabelled it
      info    -- normalisation that changed structure but not content
    """

    severity: Literal["error", "warning", "info"] = "warning"
    code: str = ""
    message: str = ""
    location: str = ""


class Manuscript(BaseModel):
    schema_version: str = "1.0"
    meta: Metadata = Field(default_factory=Metadata)
    body: list[Section] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    equations: list[Equation] = Field(default_factory=list)
    footnotes: list[Footnote] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    media_dir: str = ""
    issues: list[ParseIssue] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    # -- convenience lookups -------------------------------------------------

    def figure(self, fid: str) -> Figure | None:
        return next((f for f in self.figures if f.id == fid), None)

    def table(self, tid: str) -> Table | None:
        return next((t for t in self.tables if t.id == tid), None)

    def equation(self, eid: str) -> Equation | None:
        return next((e for e in self.equations if e.id == eid), None)

    def iter_sections(self, sections: list[Section] | None = None):
        for s in sections if sections is not None else self.body:
            yield s
            yield from self.iter_sections(s.children)

    def section_by_role(self, role: SectionRole) -> Section | None:
        return next((s for s in self.iter_sections() if s.role == role), None)

    def word_count(self) -> int:
        n = 0
        for s in self.iter_sections():
            for b in s.blocks:
                if b.paragraph:
                    n += len(b.paragraph.plain_text().split())
        return n


# Resolve the forward references used above.
for _m in (ListBlock, Table, TableCell, Footnote, Block, Section):
    _m.model_rebuild()
