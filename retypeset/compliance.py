"""
retypeset.compliance -- validate a parsed manuscript against a journal profile.

This is the part of the pipeline that pays for itself before any renderer
exists. Desk rejections and production hold-ups are overwhelmingly caused by
mechanical failures -- abstract too long, figures below resolution at printed
size, a required declaration missing -- all of which are decidable from the IR
plus a profile.

Severity contract:
    fail  -- the journal will reject or return the manuscript for this
    warn  -- likely a problem, or a hard rule we could not verify
    info  -- worth knowing, no action strictly required
    pass  -- checked and satisfied

Rules whose threshold comes from a profile with `verified=false` are never
reported as `fail`; they are downgraded to `warn`. A false rejection wastes more
of the author's time than a missed one.

That downgrade applies to *numeric limits taken from the profile* -- abstract
length, keyword count, figure resolution, highlight rules. It deliberately does
not apply to universal structural facts: a manuscript with no abstract or no
reference list is unsubmittable everywhere, and reporting that as a warning
because we could not verify one journal's word limit would be a strange kind of
caution. Those few rules are marked UNIVERSAL below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .ir import Manuscript, SectionRole
from .profile import JournalProfile


@dataclass
class Finding:
    severity: str                  # fail | warn | info | pass
    rule: str
    message: str
    detail: str = ""
    fix: str = ""
    locations: list[str] = field(default_factory=list)


@dataclass
class ComplianceReport:
    profile: JournalProfile
    findings: list[Finding]

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def passes(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "pass"]

    @property
    def ready(self) -> bool:
        return not self.failures

    def score(self) -> float:
        """Fraction of decidable checks that passed. Not a quality measure."""
        decidable = [f for f in self.findings if f.severity in ("pass", "fail", "warn")]
        if not decidable:
            return 1.0
        return len(self.passes) / len(decidable)


def check(ms: Manuscript, profile: JournalProfile,
          media_dir: str | Path | None = None) -> ComplianceReport:
    out: list[Finding] = []
    s = profile.structure
    verified = profile.verified

    def add(ok: bool, rule: str, ok_msg: str, bad_msg: str,
            fix: str = "", detail: str = "", locations: list[str] | None = None,
            hard: bool = True) -> None:
        if ok:
            out.append(Finding("pass", rule, ok_msg))
        else:
            sev = "fail" if (hard and verified) else "warn"
            out.append(Finding(sev, rule, bad_msg, detail, fix, locations or []))

    # -- abstract ----------------------------------------------------------
    abstract = ms.meta.abstract_raw.strip()
    words = len(abstract.split())
    if not abstract:
        out.append(Finding("fail", "abstract.present",      # UNIVERSAL
                           "No abstract found in the manuscript.",
                           fix="Add an abstract, or check that the parser labelled "
                               "the right section as `abstract`."))
    else:
        if s.abstract_max_words:
            add(words <= s.abstract_max_words, "abstract.max_words",
                f"Abstract is {words} words (limit {s.abstract_max_words}).",
                f"Abstract is {words} words, over the {s.abstract_max_words}-word limit.",
                fix=f"Cut {words - s.abstract_max_words} words.")
        if s.abstract_min_words:
            add(words >= s.abstract_min_words, "abstract.min_words",
                f"Abstract meets the {s.abstract_min_words}-word minimum.",
                f"Abstract is {words} words, under the {s.abstract_min_words}-word minimum.",
                fix=f"Add about {s.abstract_min_words - words} words.", hard=False)
        if s.abstract_paragraphs_max:
            n_par = sum(1 for b in ms.meta.abstract if b.paragraph
                        and b.paragraph.plain_text().strip())
            add(n_par <= s.abstract_paragraphs_max, "abstract.paragraphs",
                f"Abstract is a single paragraph.",
                f"Abstract has {n_par} paragraphs; at most "
                f"{s.abstract_paragraphs_max} allowed.",
                fix="Merge into one paragraph.")
        # Citations, equations and floats inside an abstract are near-universally
        # forbidden and are a common cause of production queries.
        has_math = any(
            n.kind == "math" for b in ms.meta.abstract if b.paragraph
            for n in b.paragraph.inlines
        )
        if has_math:
            out.append(Finding("warn", "abstract.no_math",
                               "The abstract contains display or inline mathematics.",
                               fix="Most publishers forbid equations in the abstract; "
                                   "restate them in words."))

    # -- keywords ----------------------------------------------------------
    kw = ms.meta.keywords
    if s.keywords_min or s.keywords_max:
        lo, hi = s.keywords_min or 0, s.keywords_max or 99
        add(lo <= len(kw) <= hi, "keywords.count",
            f"{len(kw)} keywords (allowed {lo}-{hi}).",
            f"{len(kw)} keywords; this journal allows {lo}-{hi}.",
            fix="Add keywords." if len(kw) < lo else "Remove the weakest keywords.")

    # -- highlights --------------------------------------------------------
    if s.highlights_required:
        hl = ms.meta.highlights
        if not hl:
            out.append(Finding(
                "fail" if verified else "warn", "highlights.present",
                "Highlights are required but none were found.",
                fix=f"Write {s.highlights_min or 3}-{s.highlights_max or 5} bullet "
                    f"points of at most {s.highlights_max_chars or 85} characters each, "
                    "submitted as a separate file."))
        else:
            lo, hi = s.highlights_min or 1, s.highlights_max or 99
            add(lo <= len(hl) <= hi, "highlights.count",
                f"{len(hl)} highlights (allowed {lo}-{hi}).",
                f"{len(hl)} highlights; allowed {lo}-{hi}.")
            if s.highlights_max_chars:
                over = [h for h in hl if len(h) > s.highlights_max_chars]
                add(not over, "highlights.length",
                    f"All highlights are within {s.highlights_max_chars} characters.",
                    f"{len(over)} highlight(s) exceed {s.highlights_max_chars} characters.",
                    detail="; ".join(f"{len(h)} chars: {h[:60]}" for h in over[:3]))

    # -- title -------------------------------------------------------------
    if s.title_max_chars and ms.meta.title:
        add(len(ms.meta.title) <= s.title_max_chars, "title.length",
            "Title length is acceptable.",
            f"Title is {len(ms.meta.title)} characters; limit {s.title_max_chars}.")

    # -- sections ----------------------------------------------------------
    present = {sec.role for sec in ms.iter_sections()}
    if ms.meta.abstract_raw:
        present.add(SectionRole.ABSTRACT)
    if ms.meta.keywords:
        present.add(SectionRole.KEYWORDS)
    # Treat a combined results-and-discussion as satisfying both.
    if SectionRole.RESULTS_DISCUSSION in present:
        present |= {SectionRole.RESULTS, SectionRole.DISCUSSION}

    missing = [r for r in s.required_sections if r not in present]
    unknown = [sec.title_raw for sec in ms.body
               if sec.role is SectionRole.UNKNOWN and sec.title_raw]
    if missing and unknown:
        out.append(Finding(
            "warn", "structure.required_sections",
            f"{len(missing)} required section(s) not found, but "
            f"{len(unknown)} section(s) are still unclassified - the content may "
            "be present under a heading the parser could not label.",
            detail="Missing: " + ", ".join(r.value for r in missing)
                   + " | Unclassified: " + "; ".join(u[:40] for u in unknown[:5]),
            fix="Assign roles to the unclassified sections, then re-run."))
    else:
        add(not missing, "structure.required_sections",
            "All required sections are present.",
            "Missing required section(s): " + ", ".join(r.value for r in missing),
            fix="Add the missing sections.")

    forbidden = [r for r in s.forbidden_sections if r in present]
    if forbidden:
        out.append(Finding("warn", "structure.forbidden_sections",
                           "Section(s) this journal does not use: "
                           + ", ".join(r.value for r in forbidden),
                           fix="Remove or merge them."))

    if s.section_order:
        order = {r: i for i, r in enumerate(s.section_order)}
        seq = [order[sec.role] for sec in ms.body if sec.role in order]
        add(seq == sorted(seq), "structure.section_order",
            "Section order matches the journal's expected sequence.",
            "Sections appear out of the journal's expected order.",
            detail=" -> ".join(sec.role.value for sec in ms.body if sec.role in order),
            hard=False)

    if s.manuscript_max_words:
        wc = ms.word_count()
        add(wc <= s.manuscript_max_words, "structure.length",
            f"Manuscript is {wc} words (limit {s.manuscript_max_words}).",
            f"Manuscript is {wc} words, over the {s.manuscript_max_words}-word limit.")

    # -- figures -----------------------------------------------------------
    _check_figures(ms, profile, out, media_dir)

    # -- references --------------------------------------------------------
    refs = ms.references
    if not refs:
        out.append(Finding("fail", "references.present",    # UNIVERSAL
                           "No references found."))
    else:
        if profile.references.doi_required:
            no_doi = [r.id for r in refs if not r.doi]
            add(not no_doi, "references.doi",
                "All references carry a DOI.",
                f"{len(no_doi)} of {len(refs)} references have no DOI.",
                fix="Look the missing DOIs up via Crossref; this journal requires them.",
                locations=no_doi[:20], hard=False)
        if profile.references.max_references:
            add(len(refs) <= profile.references.max_references, "references.count",
                f"{len(refs)} references.",
                f"{len(refs)} references; limit {profile.references.max_references}.")

        low = [r.id for r in refs if r.parse_confidence < 0.6]
        if low:
            out.append(Finding(
                "warn", "references.parse_quality",
                f"{len(low)} of {len(refs)} references were parsed with low confidence.",
                fix="Automatic restyling into "
                    f"'{profile.references.style}' format will be unreliable for these. "
                    "Re-import the bibliography from Zotero/Mendeley, or run it "
                    "through AnyStyle/GROBID first.",
                locations=low[:20]))

        manual = any(i.code == "manual_citations" for i in ms.issues)
        if manual:
            out.append(Finding(
                "warn", "references.field_codes",
                "In-text citations are plain text rather than reference-manager fields.",
                fix=f"This journal uses {profile.references.style} citations. "
                    "Without field codes, converting between numeric and author-year "
                    "requires matching every bracketed marker by hand."))

    # -- parser confidence carried through --------------------------------
    for issue in ms.issues:
        if issue.severity == "error" and issue.code in (
            "text_lost_by_reader", "no_authors", "no_title", "no_abstract",
        ):
            out.append(Finding("warn", f"parse.{issue.code}",
                               "Parser flagged this before compliance was evaluated: "
                               + issue.message))

    return ComplianceReport(profile, out)


def _check_figures(ms: Manuscript, profile: JournalProfile,
                   out: list[Finding], media_dir: str | Path | None) -> None:
    f = profile.figures
    figs = ms.figures
    if not figs:
        out.append(Finding("info", "figures.present", "No figures in the manuscript."))
        return

    bad_fmt = [x.id for x in figs if x.fmt.lower() in
               [e.lower() for e in f.rejected_formats]]
    add_sev = "fail" if profile.verified else "warn"
    if bad_fmt:
        out.append(Finding(
            add_sev, "figures.format",
            f"{len(bad_fmt)} figure(s) are in a format this journal does not accept "
            f"({', '.join(sorted({x.fmt for x in figs if x.id in bad_fmt}))}).",
            fix="Convert to PDF or EPS for vector artwork, or to TIFF/PNG at the "
                "required resolution for raster.",
            locations=bad_fmt))
    else:
        out.append(Finding("pass", "figures.format",
                           "All figure formats are accepted by this journal."))

    # Resolution is judged AT PRINTED SIZE, which is how publishers state the
    # rule and how their automated checks apply it. A 1229 px figure sounds
    # comfortable until you notice Word placed it 160 mm wide, where it is only
    # 194 dpi. Comparing raw pixel counts against a single-column threshold both
    # passes figures that will fail and fails figures that would have passed.
    need_single = f.min_px_single_column or f.required_px(f.single_column_mm, "halftone")
    low_res: list[str] = []
    unknown_res: list[str] = []
    for fig in figs:
        if fig.is_vector:
            continue                     # vector art has no fixed resolution
        if not fig.width_px:
            unknown_res.append(fig.id)
            continue

        if fig.placed_width_mm:
            effective_dpi = fig.width_px / (fig.placed_width_mm / 25.4)
            if effective_dpi < f.dpi_halftone:
                low_res.append(
                    f"{fig.id} ({fig.width_px} px at {fig.placed_width_mm:g} mm "
                    f"= {effective_dpi:.0f} dpi)")
        elif fig.width_px < need_single:
            # No placement size recorded: fall back to the single-column rule.
            low_res.append(f"{fig.id} ({fig.width_px} px)")

    if low_res:
        out.append(Finding(
            add_sev, "figures.resolution",
            f"{len(low_res)} figure(s) are below {f.dpi_halftone} dpi at the size "
            "they are placed in the document.",
            detail="; ".join(low_res[:10]),
            fix="Re-export from the original source at final printed size, or "
                "place the figure smaller. Upscaling an existing raster does not "
                "help - the detail is gone. Plots should be exported as vector "
                f"PDF/EPS instead. At {f.single_column_mm:g} mm single column this "
                f"journal needs {need_single} px; at {f.double_column_mm:g} mm full "
                f"width it needs {f.required_px(f.double_column_mm, 'halftone')} px.",
            locations=[x.split()[0] for x in low_res]))
    else:
        out.append(Finding("pass", "figures.resolution",
                           f"All raster figures reach {f.dpi_halftone} dpi at the "
                           "size they are placed."))

    if unknown_res:
        out.append(Finding("info", "figures.resolution_unknown",
                           f"{len(unknown_res)} figure(s) could not be measured.",
                           locations=unknown_res))

    no_cap = [x.id for x in figs if not x.caption_raw]
    if no_cap:
        out.append(Finding(
            "warn", "figures.caption",
            f"{len(no_cap)} figure(s) have no caption.",
            fix="Every figure needs a caption. Note that decorative images "
                "(logos, author photographs) should not be numbered figures at all.",
            locations=no_cap))
    else:
        out.append(Finding("pass", "figures.caption", "All figures have captions."))

    no_tab_cap = [t.id for t in ms.tables if not t.caption_raw]
    if no_tab_cap:
        out.append(Finding("warn", "tables.caption",
                           f"{len(no_tab_cap)} table(s) have no caption.",
                           locations=no_tab_cap))


def format_report(report: ComplianceReport) -> str:
    p = report.profile
    L = [
        "=" * 74,
        f"COMPLIANCE - {p.publisher} / {p.journal}",
        "=" * 74,
    ]
    if not p.verified:
        L.append("\n! This profile is UNVERIFIED: limits are inferred, not read from the")
        L.append("  publisher's guidelines. All rules are reported as warnings only.")
    if p.notes:
        L.append(f"\nNote: {p.notes}")

    L.append(f"\n{len(report.passes)} passed, {len(report.warnings)} warnings, "
             f"{len(report.failures)} failures")

    for label, sev in (("FAILURES", "fail"), ("WARNINGS", "warn"), ("INFO", "info")):
        items = [f for f in report.findings if f.severity == sev]
        if not items:
            continue
        L.append(f"\n-- {label} " + "-" * (69 - len(label)))
        for f in items:
            L.append(f"  [{f.rule}] {f.message}")
            if f.detail:
                L.append(f"      {f.detail}")
            if f.fix:
                L.append(f"      fix: {f.fix}")

    ok = [f for f in report.findings if f.severity == "pass"]
    if ok:
        L.append("\n-- PASSED " + "-" * 63)
        for f in ok:
            L.append(f"  [{f.rule}] {f.message}")

    L.append("\n" + "=" * 74)
    L.append("VERDICT: " + ("no blocking compliance failures"
                            if report.ready else
                            f"{len(report.failures)} blocking failure(s)"))
    L.append("=" * 74)
    return "\n".join(L)
