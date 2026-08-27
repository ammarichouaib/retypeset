"""
Test suite for retypeset.

Deliberately built on synthetic objects and generated files rather than on
sample manuscripts: real manuscripts are unpublished and cannot be committed to
a public repository. The one integration test that needs a .docx skips itself
when none is available, so `pytest` is green on a clean clone.

    pip install pytest
    pytest -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retypeset import cleanup, learn, sectioning
from retypeset.compliance import check
from retypeset.ir import (
    Block,
    Figure,
    InlineNode,
    Manuscript,
    Metadata,
    Paragraph,
    Section,
    SectionRole,
    TableCell,
)
from retypeset.parse_docx import (
    _as_equation_layout,
    _is_degenerate_math,
    _match_role,
    _split_run_on_references,
)
from retypeset.profile import load_profiles
from retypeset.render_latex import escape, is_degenerate_math, render_inlines

# ---------------------------------------------------------------------------
# LaTeX escaping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("50% of $5", r"50\% of \$5"),
    ("a_b {x}", r"a\_b \{x\}"),
    ("C:\\path", r"C:\textbackslash{}path"),
    ("~n ^2", r"\textasciitilde{}n \textasciicircum{}2"),
])
def test_escape_specials(raw, expected):
    assert escape(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("H₂O", "H$_{2}$O"),
    ("m³", "m$^{3}$"),
    ("η", r"$\eta$"),
    ("Δp", r"$\Delta$p"),
    ("α–β", r"$\alpha$--$\beta$"),
    ("𝜸", r"$\boldsymbol{\gamma}$"),
    ("𝜞", r"$\boldsymbol{\Gamma}$"),
])
def test_escape_unicode_becomes_latex_commands(raw, expected):
    """Regression: escaping used to run *after* the Unicode table, turning
    `$\\eta$` into the literal text `\\$\\textbackslash{}eta\\$`. That still
    compiled, which is exactly why it needs a test."""
    assert escape(raw) == expected


def test_escape_leaves_nothing_unmappable():
    from retypeset.render_latex import UNMAPPED
    UNMAPPED.clear()
    escape("Efficiency η, ΔT, 10 °C, ≤ 5, 𝜸 and ∙")
    assert UNMAPPED == set()


def test_math_is_never_escaped():
    nodes = [InlineNode(kind="text", text="where "),
             InlineNode(kind="math", text=r"Z_{1} = R_{1} + jX_{1}")]
    out = render_inlines(nodes)
    assert r"$Z_{1} = R_{1} + jX_{1}$" in out
    assert "textbackslash" not in out


# ---------------------------------------------------------------------------
# Degenerate math
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("latex", ["", "   ", "_{}", "^{}^{}", r"\ ", "{}"])
def test_degenerate_math_detected(latex):
    assert _is_degenerate_math(latex)
    assert is_degenerate_math(latex)


@pytest.mark.parametrize("latex", ["x", "Z_{1}", r"\frac{a}{b}", "K_{0}"])
def test_real_math_not_flagged(latex):
    assert not _is_degenerate_math(latex)


def test_degenerate_math_renders_placeholder_not_broken_latex():
    out = render_inlines([InlineNode(kind="math", text="^{}^{}")])
    assert "^{}^{}" not in out          # would be a double-superscript error
    assert "blacksquare" in out


# ---------------------------------------------------------------------------
# Equation-numbering tables
# ---------------------------------------------------------------------------

def _cell(math: str = "", text: str = "") -> TableCell:
    inlines = []
    if math:
        inlines.append(InlineNode(kind="math", text=math))
    if text:
        inlines.append(InlineNode(kind="text", text=text))
    return TableCell(blocks=[Block(kind="paragraph",
                                   paragraph=Paragraph(inlines=inlines))])


def test_equation_table_recognised():
    grid = [[_cell(math="a=b"), _cell(text="(1)")],
            [_cell(math="c=d"), _cell(text="(2)")]]
    rows = _as_equation_layout(grid)
    assert rows == [("a=b", "1"), ("c=d", "2")]


def test_equation_table_with_spacer_column():
    grid = [[_cell(), _cell(math="a=b"), _cell(text="(3)")]]
    assert _as_equation_layout(grid) == [("a=b", "3")]


def test_real_data_table_is_not_destroyed():
    """A parameters table whose Symbol column holds mathematics must survive."""
    grid = [[_cell(text="Parameter"), _cell(text="Symbol"), _cell(text="Value")],
            [_cell(text="Open-circuit voltage"), _cell(math="V_{oc}"), _cell(text="46.19")]]
    assert _as_equation_layout(grid) is None


def test_unnumbered_math_table_is_not_converted():
    """Without a single printed number this is not an equation layout."""
    grid = [[_cell(math="a=b")], [_cell(math="c=d")]]
    assert _as_equation_layout(grid) is None


# ---------------------------------------------------------------------------
# Section role lexicon
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title, role", [
    ("Introduction", SectionRole.INTRODUCTION),
    ("Results and Discussion", SectionRole.RESULTS_DISCUSSION),
    ("Conclusions and Perspectives", SectionRole.CONCLUSION),
    ("Declaration of Competing Interest", SectionRole.CONFLICT_OF_INTEREST),
    ("Index Terms", SectionRole.KEYWORDS),
    ("Protection of a Very High Voltage Line Span", SectionRole.UNKNOWN),
])
def test_role_lexicon(title, role):
    assert _match_role(title) is role


# ---------------------------------------------------------------------------
# Reference splitting
# ---------------------------------------------------------------------------

def test_run_on_references_are_split():
    entry = ("Rouabhi, R., et al. New Cascade Control. IET Science. 19, 01 (2025). "
             "doi: https://doi.org/10.1049/smt2.70029 "
             "Sobhuza Z. I. et al. Another Paper Title Here. Energy Reports 12 (2024).")
    parts = _split_run_on_references(entry)
    assert len(parts) == 2
    assert parts[1].startswith("Sobhuza")


def test_short_reference_is_left_alone():
    assert _split_run_on_references("Smith J. A short one. 2020.") == \
        ["Smith J. A short one. 2020."]


# ---------------------------------------------------------------------------
# Sectioning: losslessness is the property that matters
# ---------------------------------------------------------------------------

def _manuscript(n_body: int = 6) -> Manuscript:
    ms = Manuscript(meta=Metadata(title="A Title"))
    for i, (title, role) in enumerate([("Abstract", SectionRole.ABSTRACT),
                                       ("Introduction", SectionRole.INTRODUCTION),
                                       ("References", SectionRole.REFERENCES)]):
        sec = Section(id=f"s{i}", level=1, role=role, title_raw=title)
        for j in range(n_body):
            sec.blocks.append(Block(
                kind="paragraph",
                paragraph=Paragraph(id=f"p{i}_{j}", inlines=[
                    InlineNode(kind="text",
                               text=f"Body sentence {j} of section {title}.")]),
            ))
        ms.body.append(sec)
    return ms


def test_flatten_rebuild_round_trip_is_lossless():
    ms = _manuscript()
    rows = sectioning.flatten(ms)
    before = [(r.block_kind, r.text) for r in rows]
    sectioning.rebuild(ms, rows)
    after = [(r.block_kind, r.text) for r in sectioning.flatten(ms)]
    assert before == after


def test_apply_ranges_keeps_every_block():
    ms = _manuscript()
    rows = sectioning.flatten(ms)
    n_blocks = sum(1 for r in rows if r.block is not None)
    sectioning.apply_ranges(ms, rows, [
        sectioning.Assignment("abstract", 1, 3),
    ])
    kept = sum(len(s.blocks) for s in ms.iter_sections())
    assert kept == n_blocks


def test_apply_ranges_preserves_untouched_roles():
    """Confirming one section must not un-label the others."""
    ms = _manuscript()
    rows = sectioning.flatten(ms)
    sectioning.apply_ranges(ms, rows, [sectioning.Assignment("abstract", 1, 3)])
    roles = {s.role for s in ms.body}
    assert SectionRole.INTRODUCTION in roles
    assert SectionRole.REFERENCES in roles


def test_apply_ranges_does_not_duplicate_heading_as_empty_section():
    ms = _manuscript()
    rows = sectioning.flatten(ms)
    sectioning.apply_ranges(ms, rows, [sectioning.Assignment("abstract", 1, 3)])
    titles = [s.title_raw for s in ms.body]
    assert titles.count("Abstract") == 1


def test_overlapping_ranges_do_not_lose_content():
    ms = _manuscript()
    rows = sectioning.flatten(ms)
    n_blocks = sum(1 for r in rows if r.block is not None)
    sectioning.apply_ranges(ms, rows, [
        sectioning.Assignment("abstract", 1, 5),
        sectioning.Assignment("introduction", 3, 9),
    ])
    assert sum(len(s.blocks) for s in ms.iter_sections()) == n_blocks


def test_training_examples_only_from_edited_rows():
    ms = _manuscript()
    rows = sectioning.flatten(ms)
    table = sectioning.to_table(rows)
    table[1]["heading"] = True          # one genuine edit
    table[1]["role"] = "methods"
    rows = sectioning.from_table(rows, table)
    ex = sectioning.training_examples(rows)
    assert len(ex) == 1
    assert ex[0]["role"] == "methods"


# ---------------------------------------------------------------------------
# Profiles and compliance
# ---------------------------------------------------------------------------

def test_profiles_load_and_are_valid():
    profiles = load_profiles()
    assert profiles, "no journal profiles found"
    for pid, p in profiles.items():
        assert p.id == pid
        assert p.figures.single_column_mm > 0
        assert p.figures.dpi_halftone >= 72


def test_template_profile_is_not_offered():
    assert "_template" not in load_profiles()


def test_unverified_profile_never_hard_fails_on_its_own_numbers():
    """A profile we could not verify may warn, but must not reject on a limit
    we are not sure about. Universal structural rules (no abstract at all, no
    references at all) are exempt and stay hard failures by design."""
    profiles = load_profiles()
    unverified = [p for p in profiles.values() if not p.verified]
    if not unverified:
        pytest.skip("all profiles are verified")
    ms = _manuscript()
    ms.meta.abstract_raw = "word " * 5000        # wildly over any limit
    report = check(ms, unverified[0])
    universal = {"abstract.present", "references.present"}
    profile_rule_failures = [f for f in report.failures if f.rule not in universal]
    assert not profile_rule_failures, profile_rule_failures
    assert any(f.rule == "abstract.max_words" for f in report.warnings)


def test_resolution_is_judged_at_placed_size():
    """A large raster placed very wide is still low resolution."""
    ms = _manuscript()
    ms.meta.abstract_raw = "short abstract"
    ms.figures.append(Figure(id="fig1", fmt="png", width_px=1200, height_px=800,
                             placed_width_mm=170.0, caption_raw="Fig. 1. X"))
    profile = load_profiles()["elsevier_generic"]
    report = check(ms, profile)
    res = [f for f in report.findings if f.rule == "figures.resolution"]
    assert res and res[0].severity == "fail"       # 1200 px / 170 mm = 179 dpi


def test_vector_figures_skip_the_resolution_check():
    ms = _manuscript()
    ms.figures.append(Figure(id="fig1", fmt="svg", is_vector=True,
                             placed_width_mm=170.0, caption_raw="Fig. 1. X"))
    report = check(ms, load_profiles()["elsevier_generic"])
    res = [f for f in report.findings if f.rule == "figures.resolution"]
    assert res and res[0].severity == "pass"


# ---------------------------------------------------------------------------
# Readiness review
# ---------------------------------------------------------------------------

_GOOD_ABSTRACT = (
    "Photovoltaic forecasting is increasingly important for grid operation. "
    "However, the effect of dust has not been tested against aerosol "
    "observations at a utility-scale plant. "
    "This study evaluates nine forecasting approaches using one year of "
    "30-minute operational data. "
    "The best model reduces the error by 18 % relative to persistence. "
    "These results demonstrate that cloud variability, not dust, limits "
    "intraday predictability."
)


def _reviewable(abstract: str = _GOOD_ABSTRACT) -> Manuscript:
    ms = _manuscript()
    ms.meta.title = "Cloud variability limits intraday photovoltaic predictability"
    ms.meta.keywords = ["solar", "forecasting", "energy", "dust"]
    ms.meta.abstract_raw = abstract
    ms.stats["words"] = 6000
    return ms


def test_good_abstract_passes_all_five_moves():
    """Regression: a tighter pattern set reported 'missing method, conclusion'
    for an abstract that plainly had both."""
    from retypeset import review
    rep = review.analyse(_reviewable(), load_profiles()["elsevier_generic"])
    moves = next(c for cat in rep.categories for c in cat.checks
                 if c.key == "abstract.moves")
    assert moves.score == 1.0, moves.evidence


def test_missing_gap_is_detected():
    from retypeset import review
    no_gap = ("Photovoltaic forecasting is important. This study evaluates nine "
              "approaches using operational data. The model reduces error by 18 %. "
              "These results demonstrate the value of the approach.")
    rep = review.analyse(_reviewable(no_gap), load_profiles()["elsevier_generic"])
    moves = next(c for cat in rep.categories for c in cat.checks
                 if c.key == "abstract.moves")
    assert "gap" in moves.evidence


def test_no_acceptance_probability_is_ever_produced():
    """The whole point. If a percentage of acceptance appears anywhere in the
    report, that is a defect."""
    from retypeset import review
    rep = review.analyse(_reviewable(), load_profiles()["elsevier_generic"])
    text = review.format_report(rep).lower()
    assert "probability of acceptance" not in text.replace(
        "not a probability of acceptance", "")
    band, reasons = rep.desk_rejection_risk()
    assert band in {"Low", "Moderate", "High"}
    assert reasons


def test_scope_mismatch_raises_desk_rejection_risk():
    from retypeset import review
    ms = _reviewable()
    ms.meta.title = "A study of medieval poetry"
    ms.meta.keywords = ["poetry", "literature"]
    ms.meta.abstract_raw = "This study examines rhyme in medieval verse."
    rep = review.analyse(ms, load_profiles()["elsevier_generic"])
    scope = next(c for cat in rep.categories for c in cat.checks
                 if c.key == "scope.match")
    assert scope.score < 0.34
    assert rep.desk_rejection_risk()[0] == "High"


def test_scope_check_skipped_when_profile_declares_none():
    from retypeset import review
    p = load_profiles()["elsevier_generic"].model_copy(
        update={"scope_keywords": []})
    rep = review.analyse(_reviewable(), p)
    scope = next(c for cat in rep.categories for c in cat.checks
                 if c.key == "scope.match")
    assert scope.weight == 0.0        # contributes nothing rather than guessing


def test_abstract_with_citations_is_flagged():
    from retypeset import review
    ms = _reviewable(_GOOD_ABSTRACT + " Previous work [12] disagrees.")
    rep = review.analyse(ms, load_profiles()["elsevier_generic"])
    c = next(x for cat in rep.categories for x in cat.checks
             if x.key == "abstract.selfcontained")
    assert c.score == 0.0


def test_every_check_has_traceable_evidence():
    """No score may appear without an observation behind it."""
    from retypeset import review
    rep = review.analyse(_reviewable(), load_profiles()["elsevier_generic"])
    for cat in rep.categories:
        for c in cat.checks:
            assert c.evidence or c.weight == 0.0, f"{c.key} has no evidence"


# ---------------------------------------------------------------------------
# AI review panel (no network: the completion function is injected)
# ---------------------------------------------------------------------------

def _panel_ms() -> Manuscript:
    ms = _reviewable()
    from retypeset.ir import Block, Paragraph, Section
    sec = Section(id="m1", level=1, role=SectionRole.METHODS,
                  title_raw="Methods")
    sec.blocks.append(Block(kind="paragraph", paragraph=Paragraph(
        inlines=[InlineNode(kind="text", text=(
            "Irradiance was measured with a calibrated pyranometer at one minute "
            "resolution and averaged to thirty minute means for the analysis."))])))
    ms.body.append(sec)
    return ms


def _finding(quote: str, issue: str = "Something is missing",
             severity: str = "major") -> str:
    return json.dumps({"findings": [{
        "issue": issue, "why": "because", "fix": "do this",
        "quote": quote, "section": "Methods", "severity": severity}]})


def test_ungrounded_findings_are_withheld():
    """A model that invents its evidence must not reach the author."""
    from retypeset import agents
    ms = _panel_ms()

    def liar(provider, system, user, api_key="", **kw):
        return _finding("We recruited 240 participants across three hospitals")

    p = agents.Provider("x", "X", "openai", "http://x", "m", "")
    rep = agents.review_manuscript(ms, load_profiles()["elsevier_generic"],
                                   [p], ["methods"], complete_fn=liar)
    assert rep.findings == []
    assert len(rep.withheld) == 1
    assert rep.groundedness()["x"] == 0.0


def test_grounded_finding_survives():
    from retypeset import agents
    ms = _panel_ms()
    quote = "measured with a calibrated pyranometer at one minute resolution"

    def honest(provider, system, user, api_key="", **kw):
        return _finding(quote)

    p = agents.Provider("x", "X", "openai", "http://x", "m", "")
    rep = agents.review_manuscript(ms, load_profiles()["elsevier_generic"],
                                   [p], ["methods"], complete_fn=honest)
    assert len(rep.findings) == 1
    assert rep.findings[0].grounded


def test_agreement_counts_distinct_agents_only():
    from retypeset import agents
    ms = _panel_ms()
    quote = "averaged to thirty minute means for the analysis"

    def same(provider, system, user, api_key="", **kw):
        return _finding(quote, "The averaging window is never justified")

    provs = [agents.Provider(f"p{i}", f"P{i}", "openai", "http://x", "m", "")
             for i in range(3)]
    rep = agents.review_manuscript(ms, load_profiles()["elsevier_generic"],
                                   provs, ["methods"], complete_fn=same)
    assert len(rep.findings) == 1
    assert rep.findings[0].agreement == 3
    assert len(rep.findings[0].agreed_by) == 3


def test_one_failing_provider_does_not_kill_the_panel():
    from retypeset import agents
    ms = _panel_ms()
    quote = "averaged to thirty minute means for the analysis"

    def flaky(provider, system, user, api_key="", **kw):
        if provider.id == "bad":
            raise agents.ProviderError("HTTP 429: rate limited")
        return _finding(quote)

    provs = [agents.Provider("good", "G", "openai", "http://x", "m", ""),
             agents.Provider("bad", "B", "openai", "http://x", "m", "")]
    rep = agents.review_manuscript(ms, load_profiles()["elsevier_generic"],
                                   provs, ["methods"], complete_fn=flaky)
    assert len(rep.findings) == 1
    assert any("429" in e for e in rep.errors)


def test_malformed_model_output_is_survived():
    from retypeset import agents
    ms = _panel_ms()

    for bad in ["not json at all", "", "{}", '{"findings": "nope"}',
                '```json\n{"findings": []}\n```']:
        def junk(provider, system, user, api_key="", _b=bad, **kw):
            return _b
        p = agents.Provider("x", "X", "openai", "http://x", "m", "")
        rep = agents.review_manuscript(ms, load_profiles()["elsevier_generic"],
                                       [p], ["methods"], complete_fn=junk)
        assert rep.findings == []


def test_context_includes_nested_subsections():
    """Regression: build_context walked only top-level blocks, cutting a
    10 500-word manuscript to 11 % of itself."""
    from retypeset import agents
    from retypeset.ir import Block, Paragraph, Section
    ms = _panel_ms()
    parent = Section(id="p1", level=1, role=SectionRole.RESULTS,
                     title_raw="Results")
    child = Section(id="c1", level=2, role=SectionRole.UNKNOWN,
                    title_raw="Sensitivity")
    child.blocks.append(Block(kind="paragraph", paragraph=Paragraph(
        inlines=[InlineNode(kind="text",
                            text="A distinctive nested sentence appears here.")])))
    parent.children.append(child)
    ms.body.append(parent)
    ctx = agents.build_context(ms)
    assert "A distinctive nested sentence appears here." in ctx
    assert "Sensitivity" in ctx


def test_requests_never_send_the_blocked_user_agent(monkeypatch):
    """Cloudflare, which fronts Groq, returns 403 error 1010 for the default
    `Python-urllib/3.x` User-Agent. The error mentions neither, so it reads as
    an authentication failure and sends you looking at your key."""
    import io as _io
    import urllib.request
    from retypeset import agents

    seen = {}

    class R(_io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return R(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    agents.complete(agents.PRESETS["groq-llama70b"], "s", "u", api_key="x")
    assert seen["ua"] and "Python-urllib" not in seen["ua"]


@pytest.mark.parametrize("code, detail, expect", [
    (403, "error code: 1010", "cloudflare"),
    (404, "model is no longer available", "list models"),
    (429, "rate limit", "rate limited"),
    (401, "bad key", "key was rejected"),
])
def test_provider_errors_are_explained_not_echoed(code, detail, expect):
    from retypeset import agents
    assert expect in agents._explain(code, detail, "http://x").lower()


def test_ollama_connection_refused_explains_localhost(monkeypatch):
    import urllib.error
    import urllib.request
    from retypeset import agents

    def refuse(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    ok, msg = agents.test_connection(agents.PRESETS["ollama-local"])
    assert not ok
    assert "same machine" in msg.lower()


def test_stale_submodule_is_repaired_by_reloading_the_package():
    """The failure this guards against: Streamlit re-executes the app script but
    keeps `retypeset.agents` cached, so a new UI calls an old library and reports
    `no attribute 'test_connection'` for a function that is there on disk.

    Run in a subprocess. Reloading a package rebuilds its pydantic model classes,
    and anything imported earlier in the *test session* keeps pointing at the
    previous generation — producing "Input should be a valid Block" for an
    object that is a Block. Rebinding this module's globals was not enough,
    because the library's own modules can end up straddling two generations.
    A separate interpreter cannot leak at all.
    """
    import subprocess
    import sys
    import textwrap

    root = str(Path(__file__).resolve().parent.parent)
    script = textwrap.dedent(f"""
        import importlib, sys
        sys.path.insert(0, {root!r})
        import retypeset
        from retypeset import agents
        assert hasattr(agents, "test_connection")

        # Simulate what Streamlit holds after an edit.
        sys.modules["retypeset.agents"].__dict__.pop("test_connection", None)
        sys.modules["retypeset.agents"].__dict__.pop("list_models", None)
        assert not hasattr(sys.modules["retypeset.agents"], "test_connection")

        first = ["retypeset.ir", "retypeset.profile", "retypeset.oox", "retypeset.learn"]
        names = [n for n in sys.modules if n == "retypeset" or n.startswith("retypeset.")]
        ordered = ([n for n in first if n in names]
                   + sorted(n for n in names if n not in first and n != "retypeset")
                   + (["retypeset"] if "retypeset" in names else []))
        for name in ordered:
            try:
                importlib.reload(sys.modules[name])
            except Exception:
                pass

        m = sys.modules["retypeset.agents"]
        assert hasattr(m, "test_connection"), "reload did not restore the module"
        assert hasattr(m, "list_models")
        assert "max_chars" in m.review_manuscript.__code__.co_varnames

        # The library must still be internally consistent afterwards.
        import retypeset as fresh
        ms = fresh.Manuscript()
        fresh.check(ms, fresh.load_profiles()["elsevier_generic"])
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-1500:]
    assert "ok" in r.stdout


def test_section_is_located_not_believed():
    """On a real run the model filed four findings under 'PUBLICATION FEE',
    including one whose quote came from the abstract. The quote position is
    already known from verification, so the section is read off instead."""
    from retypeset import agents
    ctx = ("TITLE: A paper\nKEYWORDS: a, b\n\nABSTRACT:\n"
           "The system achieves faster response times than the baseline.\n\n"
           "SECTION: Methods\n"
           "The network was trained with the Levenberg-Marquardt algorithm.\n\n"
           "SECTION: PUBLICATION FEE\n"
           "The cost of publishing an article is PLN 800.\n")
    assert agents.locate_section(
        "achieves faster response times than the baseline", ctx) == "Abstract"
    assert agents.locate_section(
        "trained with the Levenberg-Marquardt algorithm", ctx) == "Methods"
    assert agents.locate_section(
        "cost of publishing an article is PLN 800", ctx) == "PUBLICATION FEE"


def test_ollama_uses_native_api_with_num_ctx(monkeypatch):
    """Ollama's OpenAI-compatible endpoint cannot set num_ctx, so it defaults to
    2048 tokens and silently discards the rest of a long manuscript."""
    import io as _io
    import urllib.request
    from retypeset import agents

    seen = {}

    class R(_io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return R(json.dumps({"message": {"content": "{}"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    p = agents.PRESETS["ollama-local"]
    assert p.kind == "ollama"
    agents.complete(p, "s", "u")
    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["options"]["num_ctx"] >= 16384
    assert seen["body"]["stream"] is False


@pytest.mark.parametrize("name, coder", [
    ("qwen2.5-coder:14b", True),
    ("deepseek-coder-v2:latest", True),
    ("llama3.1:8b", False),
    ("llama3:latest", False),
])
def test_coder_models_are_identified(name, coder):
    from retypeset import agents
    assert agents.is_coder_model(name) is coder


def test_journal_masthead_is_not_taken_as_the_title():
    """'DIAGNOSTYKA, 20xx, Vol. xx, No. x' was being used as the manuscript
    title, which propagated into LaTeX, compliance and the AI panel -- where a
    referee objected that the title did not reflect the content."""
    from retypeset import cleanup
    assert any(rx.search("DIAGNOSTYKA, 20xx, Vol. xx, No. x")
               for rx, _ in cleanup._COMPILED)


def test_local_models_run_sequentially_not_in_parallel():
    """One model on one CPU cannot serve four concurrent calls: they queue, and
    all but the first expire. Measured on a real laptop — 1 of 4 succeeded."""
    import threading
    from retypeset import agents

    ms = _panel_ms()
    quote = "averaged to thirty minute means for the analysis"
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(provider, system, user, api_key="", **kw):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        try:
            return _finding(quote)
        finally:
            with lock:
                concurrent["now"] -= 1

    local = agents.PRESETS["ollama-local"]
    rep = agents.review_manuscript(
        ms, load_profiles()["elsevier_generic"], [local],
        ["methods", "novelty", "clarity", "reproducibility"],
        complete_fn=slow)
    assert concurrent["max"] == 1, "local calls must not overlap"
    assert len(rep.runs) == 4


def test_hosted_providers_still_run_in_parallel():
    import threading
    from retypeset import agents

    ms = _panel_ms()
    quote = "averaged to thirty minute means for the analysis"
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=10)

    def slow(provider, system, user, api_key="", **kw):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        try:
            barrier.wait()          # only passes if three run at once
            return _finding(quote)
        finally:
            with lock:
                concurrent["now"] -= 1

    p = agents.Provider("x", "X", "openai", "http://x", "m", "")
    rep = agents.review_manuscript(
        ms, load_profiles()["elsevier_generic"], [p],
        ["methods", "novelty", "clarity"], complete_fn=slow, max_workers=3)
    assert concurrent["max"] == 3
    assert len(rep.runs) == 3


def test_runs_record_elapsed_time():
    from retypeset import agents
    ms = _panel_ms()

    def quick(provider, system, user, api_key="", **kw):
        return _finding("averaged to thirty minute means for the analysis")

    p = agents.Provider("x", "X", "openai", "http://x", "m", "")
    rep = agents.review_manuscript(ms, load_profiles()["elsevier_generic"],
                                   [p], ["methods"], complete_fn=quick)
    assert rep.runs[0].seconds >= 0.0


def test_short_quotes_are_never_accepted_as_evidence():
    from retypeset import agents
    hay = agents._norm("the calibrated pyranometer measured irradiance")
    assert not agents.verify_quote("pyranometer", hay)


# ---------------------------------------------------------------------------
# Cleanup patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Article citation info: Xxxxx X. Title. Diagnostyka, 20xx;xx(x):xxxx",
    "e-ISSN 2449-5220",
    "DOI:",
    "(text in red retain unchanged)",
    "© 202x by the Authors. Licensee Polish Society of Technical Diagnostics",
    "DIAGNOSTYKA, 20xx, Vol. xx, No. x",
])
def test_journal_furniture_is_matched(text):
    assert any(rx.search(text) for rx, _ in cleanup._COMPILED), text


@pytest.mark.parametrize("text", [
    "Transmission lines are a fundamental component of the power grid.",
    "The proposed method achieves a lower cost of energy.",
    "Table 2 summarises the parameters used in the optimisation.",
])
def test_manuscript_prose_is_not_matched_as_furniture(text):
    assert not any(rx.search(text) for rx, _ in cleanup._COMPILED), text


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "ANN Artificial Neural Network;",
    "V Voltage [V];",
    "Z Impedance [Ohm].",
    "Zone 1:",
    "Case 2:",
    "(a)",
    "(3)",
    "Fig. 4. Mho relay programmed by ANN",
    "Table A1. Technical parameters",
    "where:",
    "e-mail: author@university.edu",
])
def test_structural_disqualifiers_block_heading_promotion(text):
    """A trained model trained on a few hundred examples marked several of
    these as headings, and one as a keywords section at 91 % confidence. Rules
    keep the veto because these shapes are knowledge n-grams cannot acquire."""
    from retypeset.parse_docx import _cannot_be_heading
    assert _cannot_be_heading(text), text


@pytest.mark.parametrize("text", [
    "Introduction",
    "3.2 Proposed Method",
    "Protection of a Very High Voltage (VHV) Line Span",
    "Results and Discussion",
    "Techno-Economic Assessment",
])
def test_real_headings_are_not_blocked(text):
    from retypeset.parse_docx import _cannot_be_heading
    assert not _cannot_be_heading(text), text


def test_structural_features_are_stable():
    f = learn.structural_features("3.2 Proposed Method")
    assert f["starts_number"] == 1.0
    assert f["num_depth"] == 2.0
    assert f["ends_period"] == 0.0
    assert f["short"] == 1.0


def test_prediction_is_none_without_a_model(tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "HEADING_MODEL", tmp_path / "none.joblib")
    monkeypatch.setattr(learn, "ROLE_MODEL", tmp_path / "none.joblib")
    learn.reset_cache()
    assert learn.predict_heading("Introduction") is None
    assert learn.predict_role("Introduction") is None
    learn.reset_cache()


def test_examples_are_deduplicated(tmp_path):
    f = tmp_path / "c.jsonl"
    ex = [{"text": "Introduction", "is_heading": True, "role": "introduction"}]
    assert learn.append_examples(ex, f) == 1
    assert learn.append_examples(ex, f) == 0
    assert len(learn.load_examples(f)) == 1


# ---------------------------------------------------------------------------
# Integration: only runs if a sample document is available
# ---------------------------------------------------------------------------

SAMPLES = sorted(Path(__file__).parent.glob("samples/*.docx"))


@pytest.mark.skipif(not SAMPLES, reason="no sample .docx in tests/samples/")
def test_parse_is_lossless_against_the_ooxml():
    import retypeset
    from retypeset.audit import audit

    ms = retypeset.parse_docx(SAMPLES[0], media_dir=Path("/tmp/retypeset_test_media"))
    report = audit(ms, SAMPLES[0])
    for c in report["checks"]:
        assert c["ok"], f"{c['name']}: {c['ir']} of {c['source']} survived"
