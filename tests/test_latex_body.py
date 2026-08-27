"""Regressions for the failure that produced a paper with no body.

A user reformatted a manuscript to IEEEtran and received a `main.tex` that
compiled cleanly and contained the title, the abstract, the keywords and the
bibliography -- and not one word of the paper. Three separate defects lined up:

1. The manuscript's title was styled `Heading 1`, so every real section was
   nested beneath it and the tree had exactly one top-level node.
2. Marking that node with a front-matter role -- `title` is the obvious choice,
   since it *is* the title -- made the renderer return "" for the node **and
   its entire subtree**.
3. Nothing counted what reached the page, so the empty document was reported
   as a successful render.

Each is tested here independently, because any one of them alone is enough to
lose a manuscript.
"""

from __future__ import annotations

import pytest

from retypeset.ir import (
    Block, Manuscript, Paragraph, Section, SectionRole, InlineNode,
)
from retypeset.profile import get_profile
from retypeset.render_latex import LatexRenderer


def _para(text: str) -> Block:
    return Block(kind="paragraph",
                 paragraph=Paragraph(inlines=[InlineNode(kind="text", text=text)]))


def _manuscript(body: list[Section]) -> Manuscript:
    ms = Manuscript()
    ms.meta.title = "A Title"
    ms.meta.abstract_raw = "An abstract of some length for the front matter."
    ms.body = body
    return ms


def _render(ms: Manuscript, tmp_path):
    prof = get_profile("ieee_transactions")
    return LatexRenderer(ms, prof, tmp_path).render(tmp_path / "tex")


@pytest.mark.parametrize("role", [SectionRole.TITLE, SectionRole.ABSTRACT,
                                  SectionRole.KEYWORDS, SectionRole.REFERENCES,
                                  SectionRole.HIGHLIGHTS])
def test_front_matter_role_never_swallows_nested_sections(role, tmp_path):
    """One dropdown click must not delete the manuscript."""
    inner = Section(id="s2", level=2, role=SectionRole.INTRODUCTION,
                    title_raw="1. Introduction",
                    blocks=[_para("Hydrogen production in arid regions " * 12)])
    wrapper = Section(id="s1", level=1, role=role, title_raw="A Title",
                      children=[inner])
    res = _render(_manuscript([wrapper]), tmp_path)
    tex = res.main_tex.read_text(encoding="utf-8")

    assert "Hydrogen production in arid regions" in tex
    assert r"\section{Introduction}" in tex or r"\section{1. Introduction}" in tex
    assert res.sections == 1
    assert not res.empty_body


def test_render_result_reports_an_empty_body(tmp_path):
    """A document with nothing in it must not be reported as a good render."""
    empty = Section(id="s1", level=1, role=SectionRole.ABSTRACT,
                    title_raw="Abstract", blocks=[_para("short")])
    res = _render(_manuscript([empty]), tmp_path)
    assert res.sections == 0
    assert res.empty_body
    assert not res.ok
    assert any("no body sections" in n.lower() for n in res.notes)
    assert "no body" in (res.out_dir / "BUILD.md").read_text(encoding="utf-8").lower()


def test_build_notes_counts_both_sides(tmp_path):
    sec = Section(id="s1", level=1, role=SectionRole.METHODS, title_raw="Methods",
                  blocks=[_para("We did the following. " * 20)])
    res = _render(_manuscript([sec]), tmp_path)
    notes = (res.out_dir / "BUILD.md").read_text(encoding="utf-8")
    assert "What reached the document" in notes
    assert res.body_words > 50


def test_keywords_are_not_printed_twice(tmp_path):
    ms = _manuscript([Section(id="s1", level=1, role=SectionRole.METHODS,
                              title_raw="Methods", blocks=[_para("Body text. " * 30)])])
    ms.meta.keywords = ["green hydrogen", "LCOH"]
    ms.meta.abstract = [_para("The abstract proper."),
                        _para("Keywords: green hydrogen; LCOH")]
    tex = _render(ms, tmp_path).main_tex.read_text(encoding="utf-8")
    abstract = tex.split(r"\begin{abstract}")[1].split(r"\end{abstract}")[0]
    assert "The abstract proper." in abstract
    assert "Keywords:" not in abstract
    assert "green hydrogen" in tex.split(r"\begin{IEEEkeywords}")[1]


def test_ieee_author_block_carries_affiliations_and_strips_markers(tmp_path):
    from retypeset.ir import Affiliation, Author

    ms = _manuscript([Section(id="s1", level=1, role=SectionRole.METHODS,
                              title_raw="Methods", blocks=[_para("Body. " * 30)])])
    ms.meta.authors = [
        Author(id="au1", given="Chouaib", family="Ammari*1", corresponding=True,
               email="a@example.org"),
        Author(id="au2", given="Abderrahim", family="Zemmit"),
    ]
    ms.meta.affiliations = [Affiliation(id="aff1", marker="1",
                                        raw="1 Department of Renewable Energy")]
    tex = _render(ms, tmp_path).main_tex.read_text(encoding="utf-8")
    author_block = tex.split(r"\author{")[1].split(r"\maketitle")[0]

    assert "Ammari*1" not in author_block          # markers belong to the class
    assert "Ammari" in author_block
    assert r"\IEEEauthorblockA{Department of Renewable Energy}" in author_block
    assert r"\thanks{" in author_block and "a@example.org" in author_block


def test_bibtex_header_states_how_many_entries_are_missing(tmp_path):
    from retypeset.ir import Reference

    ms = _manuscript([Section(id="s1", level=1, role=SectionRole.METHODS,
                              title_raw="Methods", blocks=[_para("Body. " * 30)])])
    ms.references = [
        Reference(id="ref1", raw="Good, A. A title. J 2020;1:1.",
                  parse_confidence=0.9, csl={"title": "A title"}),
        Reference(id="ref2", raw="Unparseable line", parse_confidence=0.2),
    ]
    res = _render(ms, tmp_path)
    bib = (res.out_dir / "refs.bib").read_text(encoding="utf-8")
    tex = res.main_tex.read_text(encoding="utf-8")

    assert "1 of 2" in bib and "ref2" in bib
    # main.tex must not *recommend* a switch that would silently drop ref2: the
    # only mention left is the warning against it.
    assert "Only 1 of 2 references parsed" in tex
    assert "did not parse would vanish" in tex
    assert r"\bibliographystyle" not in tex


# ---------------------------------------------------------------------------
# Two-column layout: content that does not fit the column
# ---------------------------------------------------------------------------
# Reported from a real IEEEtran build: a four-column table printed straight over
# the text beside it, two display equations ran off the right edge of the page,
# and every equation carried its number twice -- "(3) (3)" -- because the author
# had typed one and LaTeX added another. All three are width problems that the
# renderer has to decide before TeX ever sees the file.

from retypeset.render_latex import (  # noqa: E402
    _fit_equation, _math_visual_length, _strip_manual_number,
)


def _table_block(rows, header=1):
    from retypeset.ir import Table, TableCell

    grid = [[TableCell(blocks=[_para(c)]) for c in row] for row in rows]
    return Table(id="tab1", grid=grid, header_rows=header,
                 caption_raw="Table 1. A caption.")


@pytest.mark.parametrize("number", ["(3)", " (3)", r"\quad(3)", r"\ \ \ (10)",
                                    "(4a)"])
def test_manual_equation_numbers_are_removed(number):
    assert _strip_manual_number(r"Y = a + b" + number) == "Y = a + b"


def test_a_real_equation_number_inside_the_maths_is_kept():
    # Parenthesised terms at the end are ordinary mathematics, not numbering.
    latex = r"P = (a + b)"
    assert _strip_manual_number(latex) == latex


def test_two_formulas_on_one_line_are_broken_not_shrunk():
    prof = get_profile("ieee_transactions")
    latex = (r"CF = \int_{0}^{\infty} f(v;k,c)\, p_{c}(v)\, dv,\quad\quad "
             r"p_{c}(v) = \frac{v^{3} - v_{ci}^{3}}{v_{r}^{3} - v_{ci}^{3}}")
    assert _math_visual_length(latex) > 46
    out = _fit_equation(latex, prof)
    assert r"\begin{aligned}" in out
    assert r"\resizebox" not in out


def test_a_single_over_wide_formula_is_scaled():
    prof = get_profile("ieee_transactions")
    latex = ("X = " + " + ".join(f"a_{{{i}}} b_{{{i}}} c_{{{i}}}" for i in range(12)))
    assert _math_visual_length(latex) > 46
    out = _fit_equation(latex, prof)
    assert r"\resizebox{\columnwidth}" in out


def test_short_equations_are_left_alone():
    prof = get_profile("ieee_transactions")
    latex = r"E = mc^2"
    assert _fit_equation(latex, prof) == latex


def test_wide_table_spans_both_columns_and_wraps(tmp_path):
    rows = [["Dataset", "Variable(s)", "Source", "Native resolution"]]
    rows += [["Global Solar Atlas 2.0", "GHI, DNI",
              "World Bank / Solargis [29]", "~1 km"],
             ["Renewables.ninja / ERA5", "8760-h PV and wind capacity factor",
              "Pfenninger and Staffell", "hourly, site point"]]
    ms = _manuscript([Section(id="s1", level=1, role=SectionRole.METHODS,
                              title_raw="Methods",
                              blocks=[_para("Body. " * 30)])])
    ms.tables = [_table_block(rows)]
    ms.body[0].blocks.append(Block(kind="table_ref", target_id="tab1"))
    tex = _render(ms, tmp_path).main_tex.read_text(encoding="utf-8")

    assert r"\begin{table*}" in tex          # spans, instead of overprinting
    assert r"\dimexpr" in tex                # fixed widths, so cells wrap
    assert "2\\tabcolsep" in tex             # padding subtracted, not guessed


def test_narrow_table_stays_in_one_column(tmp_path):
    rows = [["Symbol", "Value"], ["k", "2"], ["c", "7.1"]]
    ms = _manuscript([Section(id="s1", level=1, role=SectionRole.METHODS,
                              title_raw="Methods", blocks=[_para("Body. " * 30)])])
    ms.tables = [_table_block(rows)]
    ms.body[0].blocks.append(Block(kind="table_ref", target_id="tab1"))
    tex = _render(ms, tmp_path).main_tex.read_text(encoding="utf-8")

    assert r"\begin{table*}" not in tex
    assert r"\begin{tabular}{ll}" in tex


@pytest.mark.parametrize("w,h,expect_star", [
    (148.0, 62.0, True),     # wide and short: illegible at 88 mm
    (148.0, 132.0, False),   # roughly square: fits a column
    (128.0, 157.0, False),   # portrait
    (0.0, 0.0, False),       # unknown size: the safe choice is one column
])
def test_figure_spans_only_when_its_shape_demands_it(w, h, expect_star, tmp_path):
    from retypeset.ir import Figure

    ms = _manuscript([Section(id="s1", level=1, role=SectionRole.METHODS,
                              title_raw="Methods", blocks=[_para("Body. " * 30)])])
    ms.figures = [Figure(id="fig1", files=["image1.png"], fmt="png",
                         placed_width_mm=w, placed_height_mm=h,
                         caption_raw="Fig. 1. A caption.")]
    ms.body[0].blocks.append(Block(kind="figure_ref", target_id="fig1"))
    (tmp_path / "image1.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)          # never opened, only copied
    tex = _render(ms, tmp_path).main_tex.read_text(encoding="utf-8")

    assert (r"\begin{figure*}" in tex) is expect_star
    assert (r"width=\textwidth" in tex) is expect_star
