"""
retypeset.review -- what this manuscript needs before it is submitted.

What this does, and what it deliberately refuses to do
-----------------------------------------------------
It scores a manuscript on things that are *measurable from the text*: whether
the required sections exist, whether the abstract does the four jobs an abstract
has to do, whether the reference list is current and complete, whether the
figures will survive production, whether the work is reproducible from what is
written, and whether the topic matches the journal's scope.

It does **not** output a probability of acceptance, and that is not modesty.

  * Acceptance turns on novelty and correctness as judged by two or three
    people. Nothing in a manuscript's surface features predicts that.
  * There is no training data. Rejected manuscripts are not public, so the
    outcome variable cannot be observed at all, let alone at scale.
  * Base rates vary from ~8 % to ~60 % between journals and shift year to year,
    so even a perfectly calibrated model would need per-journal, per-year
    calibration that no one has.
  * The harm is asymmetric and real. "62 % chance of acceptance" reads as
    knowledge. An author who submits a weak paper because a number encouraged
    them, or shelves a good one because it did not, has been actively misled.

What can be estimated honestly is **desk-rejection risk**, because desk
rejection is largely mechanical: out of scope, missing required sections,
over-length abstract, unusable figures, a reference list that signals the
authors have not read the field. That is reported as a band with its reasons
attached, never as a single number pretending to be a probability.

Every score below decomposes into named, inspectable checks. If a number cannot
be traced to a specific observation about the text, it does not appear.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .ir import Manuscript, SectionRole
from .profile import JournalProfile

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

# Rhetorical moves a competent abstract makes. Matching is deliberately loose:
# the aim is to notice a *missing* move, not to grade prose.
# Written wide on purpose. An earlier, tighter version reported "missing:
# method, conclusion" for an abstract that plainly had both -- it said
# "designed through cost-minimization modeling" and "The results demonstrate
# that", neither of which matched. A checker that cries wolf on a good abstract
# is worse than no checker, because the author stops reading it.
_MOVE_PATTERNS = {
    "context": r"\b(is|are|remains?|has been|have been|widely|increasingly|"
               r"growing|critical|important|essential|key|challenge|problem|"
               r"demand|potential|promising|attracted)\b",
    "gap": r"\b(however|yet|although|whereas|despite|nevertheless|nonetheless|"
           r"limited|lack(s|ing)?|scarce|few studies|little attention|"
           r"remains? (unclear|challenging|an open)|has (not|never) been|"
           r"no .{0,25}(study|work|analysis)|rarely|seldom|under-?explored|"
           r"has not yet)\b",
    "method": r"\b(we (propose|present|develop|introduce|apply|use|combine|"
              r"implement|design|derive|assess|evaluate|investigate|examine|"
              r"analyse|analyze|compare|model|simulate|measure)|"
              r"this (paper|study|work|article) (proposes|presents|develops|"
              r"introduces|assesses|evaluates|investigates|examines|explores|"
              r"analyses|analyzes|reports|describes|applies|compares)|"
              r"(is|are|was|were) (designed|developed|evaluated|assessed|"
              r"performed|carried out|conducted|obtained|simulated|modell?ed|"
              r"implemented|optimi[sz]ed|validated)|"
              r"using|based on|by means of|through|via|by (applying|using|"
              r"combining|means of)|method(ology)?|framework|approach|"
              r"algorithm|model(l?ing)?|simulation|experiment)\b",
    "result": r"\b(results?|achieve[sd]?|achieving|attains?|obtain(ed|s)?|"
              r"show(s|ed|n)?|demonstrate[sd]?|reduce[sd]?|improve[sd]?|"
              r"increase[sd]?|decrease[sd]?|yield(s|ed)?|reach(es|ed)?|"
              r"outperform|equals?|corresponds? to|found)\b",
    "conclusion": r"\b(conclude|conclusion|suggests?|implies|implying|"
                  r"indicates?|demonstrat(e|es|ing) that|show(s|ing) that|"
                  r"confirm(s|ed)?|highlights?|reveals?|therefore|thus|hence|"
                  r"offers?|provides?|can be used|could (enable|be)|"
                  r"potentially|these (results|findings)|overall)\b",
}

_NOVELTY_PATTERNS = [
    r"\bfor the first time\b", r"\bnovel\b", r"\bwe (propose|introduce|develop)\b",
    r"\bthe (main |key )?(contributions?|novelty) of this (paper|study|work)\b",
    r"\bthis (paper|study|work|article) (proposes|presents|introduces|develops|"
    r"assesses|evaluates|investigates|examines|explores|reports|addresses)\b",
    r"\bunlike (previous|existing|prior|conventional)\b",
    r"\bin contrast to (previous|existing|conventional)\b",
    r"\bto the best of (our|the authors') knowledge\b",
    r"\bfirst (study|work|attempt|time) to\b",
    r"\bhas (not|never) been (previously )?(reported|studied|investigated|tested)\b",
]

_REPRO_PATTERNS = {
    "data availability": r"\bdata (are|is|will be) available\b|\bdata availability\b|"
                         r"\bavailable (from|on) (request|the corresponding author)\b|"
                         r"\brepositor(y|ies)\b|\bzenodo\b|\bfigshare\b|\bdryad\b",
    "code availability": r"\bcode (is|are|will be) available\b|\bgithub\b|"
                         r"\bsource code\b|\bopen[- ]source\b",
    "software named": r"\bMATLAB\b|\bSimulink\b|\bPython\b|\bHOMER\b|\bCOMSOL\b|"
                      r"\bANSYS\b|\bTRNSYS\b|\bPVsyst\b|\bR\b\s+statistical",
    "parameters given": r"\bparameters?\b.{0,40}\b(table|listed|given|summaris|summariz)",
}

_QUANT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(%|percent|kW|MW|GW|kWh|MWh|GWh|"
                       r"kg|km|mm|cm|m\b|s\b|ms|Hz|V\b|A\b|W\b|°C|USD|\$|€)")
_HEDGE_RE = re.compile(r"\b(may|might|could|possibly|perhaps|somewhat|relatively|"
                       r"appears? to|seems? to|suggests? that|it is believed)\b", re.I)


@dataclass
class Check:
    """One inspectable observation. `score` is 0..1, `weight` its importance."""

    key: str
    label: str
    score: float
    weight: float = 1.0
    evidence: str = ""
    advice: str = ""
    severity: str = "info"        # blocker | major | minor | info | ok

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass
class Category:
    name: str
    checks: list[Check] = field(default_factory=list)

    @property
    def score(self) -> float:
        w = sum(c.weight for c in self.checks)
        return (sum(c.contribution for c in self.checks) / w) if w else 1.0

    @property
    def weakest(self) -> list[Check]:
        return sorted((c for c in self.checks if c.score < 0.99),
                      key=lambda c: (c.score, -c.weight))


@dataclass
class ReviewReport:
    profile: JournalProfile
    categories: list[Category]
    scope_terms_matched: list[str] = field(default_factory=list)

    @property
    def readiness(self) -> float:
        """Weighted mean of category scores. NOT a probability of acceptance."""
        weights = {"Scope fit": 1.4, "Structure": 1.2, "Abstract": 1.2,
                   "References": 1.0, "Figures and tables": 1.0,
                   "Reproducibility": 0.9, "Writing signals": 0.6}
        num = sum(c.score * weights.get(c.name, 1.0) for c in self.categories)
        den = sum(weights.get(c.name, 1.0) for c in self.categories)
        return num / den if den else 0.0

    @property
    def blockers(self) -> list[Check]:
        return [c for cat in self.categories for c in cat.checks
                if c.severity == "blocker"]

    @property
    def majors(self) -> list[Check]:
        return [c for cat in self.categories for c in cat.checks
                if c.severity == "major"]

    def priorities(self, n: int = 8) -> list[Check]:
        """What to fix first: biggest weighted loss, blockers first."""
        rank = {"blocker": 0, "major": 1, "minor": 2, "info": 3, "ok": 4}
        items = [c for cat in self.categories for c in cat.checks if c.score < 0.99]
        return sorted(items,
                      key=lambda c: (rank.get(c.severity, 3),
                                     -(1 - c.score) * c.weight))[:n]

    def desk_rejection_risk(self) -> tuple[str, list[str]]:
        """A band and its reasons. Deliberately not a percentage.

        Desk rejection is mostly mechanical, so the drivers can be named. How
        often each one actually triggers a rejection varies by editor and
        journal, and is not something this tool can know.
        """
        reasons: list[str] = []
        scope = next((c for cat in self.categories for c in cat.checks
                      if c.key == "scope.match"), None)
        if scope and scope.score < 0.34:
            reasons.append("Topic overlap with the journal's stated scope is weak — "
                           "the single most common cause of desk rejection.")
        # Report the observation, not the name of the check: "4 figures below
        # 300 dpi" is actionable, "Figures will pass production checks" is not.
        for c in self.blockers + self.majors[:4]:
            reasons.append(f"{c.label}: {c.evidence}" if c.evidence else c.label)

        if any(c.severity == "blocker" for cat in self.categories for c in cat.checks) \
                or (scope and scope.score < 0.34):
            band = "High"
        elif len(self.majors) >= 3 or self.readiness < 0.6:
            band = "Moderate"
        else:
            band = "Low"
        if not reasons:
            reasons.append("No mechanical desk-rejection trigger detected. "
                           "Acceptance now depends on novelty and correctness, "
                           "which this tool does not assess.")
        return band, reasons


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _all_text(ms: Manuscript) -> str:
    parts: list[str] = []
    for sec in ms.iter_sections():
        if sec.title_raw:
            parts.append(sec.title_raw)
        for b in sec.blocks:
            if b.paragraph:
                parts.append(b.paragraph.plain_text())
    return "\n".join(parts)


def _section_text(ms: Manuscript, role: SectionRole) -> str:
    sec = ms.section_by_role(role)
    if not sec:
        return ""
    return " ".join(b.paragraph.plain_text() for b in sec.blocks if b.paragraph)


def _score_scope(ms: Manuscript, p: JournalProfile) -> tuple[Check, list[str]]:
    """Overlap between the manuscript's own vocabulary and the journal's scope.

    Uses title, keywords and abstract only. Body text would drown the signal:
    every energy paper mentions "temperature" somewhere.
    """
    terms = [t.lower() for t in (p.scope_keywords or [])]
    if not terms:
        return Check("scope.match", "Journal scope not declared in the profile",
                     1.0, 0.0,
                     advice="Add `scope_keywords` to this journal's profile to "
                            "enable scope checking.",
                     severity="info"), []

    hay = " ".join([ms.meta.title, " ".join(ms.meta.keywords),
                    ms.meta.abstract_raw]).lower()
    matched = [t for t in terms if re.search(r"\b" + re.escape(t) + r"\b", hay)]
    frac = len(matched) / max(1, min(len(terms), 12))
    score = min(1.0, frac * 2.0)        # matching half the listed terms is strong

    if score >= 0.67:
        sev, advice = "ok", ""
    elif score >= 0.34:
        sev = "minor"
        advice = ("Scope overlap is partial. Make the connection explicit in the "
                  "title and the first two sentences of the abstract, using the "
                  "journal's own vocabulary.")
    else:
        sev = "major"
        advice = ("Little overlap with this journal's stated scope. Either the "
                  "framing needs to change or this is the wrong journal — check "
                  "the aims and scope page before writing a cover letter.")
    return Check("scope.match", "Topic fit with the journal's scope", score, 3.0,
                 evidence=(f"matched {len(matched)}/{len(terms)}: "
                           + ", ".join(matched[:8]) if matched else "no scope terms matched"),
                 advice=advice, severity=sev), matched


def _score_abstract(ms: Manuscript, p: JournalProfile) -> Category:
    cat = Category("Abstract")
    text = ms.meta.abstract_raw.strip()
    words = len(text.split())

    if not text:
        cat.checks.append(Check("abstract.present", "No abstract found", 0.0, 4.0,
                                advice="Every journal requires one.",
                                severity="blocker"))
        return cat

    lo = p.structure.abstract_min_words or 0
    hi = p.structure.abstract_max_words or 10_000
    if words > hi:
        s, sev = max(0.0, 1 - (words - hi) / max(1, hi)), "major"
        adv = f"Cut {words - hi} words."
    elif lo and words < lo:
        s, sev = max(0.0, words / lo), "minor"
        adv = f"Add about {lo - words} words; short abstracts read as thin."
    else:
        s, sev, adv = 1.0, "ok", ""
    cat.checks.append(Check("abstract.length", "Abstract length", s, 2.0,
                            evidence=f"{words} words (limit {hi})",
                            advice=adv, severity=sev))

    # Rhetorical completeness, judged sentence by sentence so the report can
    # point at the sentence that satisfies each move rather than asserting a
    # verdict the author cannot check.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    found: dict[str, str] = {}
    for name, pat in _MOVE_PATTERNS.items():
        for sent in sentences:
            if re.search(pat, sent, re.I):
                found[name] = sent[:70]
                break
    missing = [n for n in _MOVE_PATTERNS if n not in found]
    s = 1.0 - len(missing) / len(_MOVE_PATTERNS)
    cat.checks.append(Check(
        "abstract.moves", "Abstract covers context, gap, method, result, conclusion",
        s, 2.0,
        evidence=("all five present" if not missing
                  else "missing: " + ", ".join(missing)
                       + " | found: " + ", ".join(found)),
        advice=("" if not missing else
                "Editors skim the abstract for these five moves. Add one sentence "
                f"for each of: {', '.join(missing)}."
                + (" A one-sentence gap statement ('However, X has not been tested "
                   "against Y') is the cheapest way to make a contribution legible."
                   if "gap" in missing else "")),
        severity="ok" if not missing else ("major" if len(missing) > 1 else "minor")))

    # Quantified results
    quant = _QUANT_RE.findall(text)
    s = 1.0 if len(quant) >= 2 else (0.5 if quant else 0.0)
    cat.checks.append(Check(
        "abstract.quantified", "Abstract reports quantified results", s, 1.5,
        evidence=f"{len(quant)} numeric result(s) with units",
        advice=("" if s == 1.0 else
                "Give at least two concrete numbers — the improvement, the error, "
                "the cost. Abstracts that only claim 'improved performance' are "
                "the ones editors reject as unfocused."),
        severity="ok" if s == 1.0 else "major"))

    # Things that do not belong in an abstract
    bad = []
    if re.search(r"\[\d+\]|\(\w+ et al\.,? \d{4}\)", text):
        bad.append("citations")
    if any(n.kind == "math" for b in ms.meta.abstract if b.paragraph
           for n in b.paragraph.inlines):
        bad.append("equations")
    if re.search(r"\b(fig(ure)?|table)\s*\.?\s*\d", text, re.I):
        bad.append("figure/table references")
    cat.checks.append(Check(
        "abstract.selfcontained", "Abstract is self-contained", 0.0 if bad else 1.0,
        1.5, evidence=("contains " + ", ".join(bad)) if bad else "clean",
        advice=("Remove them: the abstract is displayed alone in databases."
                if bad else ""),
        severity="major" if bad else "ok"))

    # Novelty statement anywhere in the front half
    front = " ".join([ms.meta.title, text, _section_text(ms, SectionRole.INTRODUCTION)])
    hits = [pat for pat in _NOVELTY_PATTERNS if re.search(pat, front, re.I)]
    cat.checks.append(Check(
        "abstract.novelty", "Contribution is stated explicitly",
        1.0 if hits else 0.0, 2.0,
        evidence=f"{len(hits)} explicit novelty statement(s)",
        advice=("" if hits else
                "State the contribution in one sentence, in the abstract and again "
                "at the end of the introduction. Reviewers who cannot find the "
                "claim will not construct it for you."),
        severity="ok" if hits else "major"))
    return cat


def _score_structure(ms: Manuscript, p: JournalProfile) -> Category:
    cat = Category("Structure")
    present = {s.role for s in ms.iter_sections()}
    if ms.meta.abstract_raw:
        present.add(SectionRole.ABSTRACT)
    if ms.meta.keywords:
        present.add(SectionRole.KEYWORDS)
    if SectionRole.RESULTS_DISCUSSION in present:
        present |= {SectionRole.RESULTS, SectionRole.DISCUSSION}

    missing = [r for r in p.structure.required_sections if r not in present]
    unknown = [s for s in ms.body if s.role is SectionRole.UNKNOWN and s.title_raw]
    s = 1.0 - len(missing) / max(1, len(p.structure.required_sections))
    sev = "ok" if not missing else ("blocker" if len(missing) > 2 else "major")
    if missing and unknown:
        sev = "minor"          # may simply be unlabelled rather than absent
    cat.checks.append(Check(
        "structure.required", "Required sections present", s, 3.0,
        evidence=("all present" if not missing
                  else "missing: " + ", ".join(r.value for r in missing)),
        advice=("" if not missing else
                ("Some sections are still unlabelled — assign roles in the "
                 "Sections tab before trusting this."
                 if unknown else "Write the missing sections.")),
        severity=sev))

    n_kw = len(ms.meta.keywords)
    lo, hi = p.structure.keywords_min or 0, p.structure.keywords_max or 99
    ok = lo <= n_kw <= hi
    cat.checks.append(Check(
        "structure.keywords", "Keyword count", 1.0 if ok else 0.0, 1.0,
        evidence=f"{n_kw} (allowed {lo}-{hi})",
        advice=("" if ok else
                "Keywords drive database discovery; use the terms a reader would "
                "search for, not the ones already in your title."),
        severity="ok" if ok else "minor"))

    # Title
    title = ms.meta.title.strip()
    tw = len(title.split())
    ok_title = bool(title) and 6 <= tw <= 20
    cat.checks.append(Check(
        "structure.title", "Title length and specificity",
        1.0 if ok_title else (0.5 if title else 0.0), 1.0,
        evidence=f"{tw} words",
        advice=("" if ok_title else
                "Aim for 8-16 words naming the method and the object of study. "
                "Very short titles read as vague; very long ones get truncated."),
        severity="ok" if ok_title else "minor"))
    return cat


def _score_references(ms: Manuscript, p: JournalProfile) -> Category:
    cat = Category("References")
    refs = ms.references
    n = len(refs)
    if not n:
        cat.checks.append(Check(
            "refs.present", "No reference list", 0.0, 3.0,
            evidence="0 references parsed",
            advice="If the manuscript does have one, it was not recognised — "
                   "label the section as `references` in the Sections tab.",
            severity="blocker"))
        return cat

    words = max(1, ms.stats.get("words", 0))
    expected = 30 if words < 5000 else (45 if words < 9000 else 60)
    ratio = n / expected
    s = 1.0 if ratio >= 0.75 else max(0.0, ratio / 0.75)
    cat.checks.append(Check(
        "refs.count", "Reference count for a paper of this length", s, 1.5,
        evidence=f"{n} references, ~{expected} typical for {words} words",
        advice=("" if s >= 1.0 else
                "A short reference list reads as an incomplete literature review "
                "and is a common reviewer complaint."),
        severity="ok" if s >= 1.0 else "major"))

    years: list[int] = []
    for r in refs:
        dp = (r.csl.get("issued") or {}).get("date-parts") or []
        if dp and dp[0]:
            try:
                years.append(int(dp[0][0]))
            except (TypeError, ValueError):
                pass
    now = datetime.now().year
    recent = [y for y in years if now - y <= 5]
    frac = len(recent) / len(years) if years else 0.0
    s = min(1.0, frac / 0.5) if years else 0.4
    cat.checks.append(Check(
        "refs.recency", "Share of references from the last five years", s, 1.5,
        evidence=(f"{len(recent)}/{len(years)} ({frac:.0%}) since {now - 5}"
                  if years else "publication years could not be parsed"),
        advice=("" if s >= 1.0 else
                "Reviewers read a dated reference list as evidence that the "
                "authors are not current. Aim for roughly half within five years."),
        severity="ok" if s >= 1.0 else "major"))

    with_doi = sum(1 for r in refs if r.doi)
    s = with_doi / n
    cat.checks.append(Check(
        "refs.doi", "References carrying a DOI", s,
        1.5 if p.references.doi_required else 0.8,
        evidence=f"{with_doi}/{n}",
        advice=("" if s > 0.9 else
                "Look the missing DOIs up via Crossref. Production will ask for "
                "them and it is faster to do now."),
        severity="ok" if s > 0.9 else ("major" if p.references.doi_required else "minor")))

    low = sum(1 for r in refs if r.parse_confidence < 0.6)
    s = 1.0 - low / n
    cat.checks.append(Check(
        "refs.consistency", "Reference formatting is machine-readable", s, 1.0,
        evidence=f"{low}/{n} entries could not be parsed cleanly",
        advice=("" if s > 0.85 else
                "Inconsistent reference formatting is the clearest signal that a "
                "bibliography was assembled by hand. Re-import it from a "
                "reference manager."),
        severity="ok" if s > 0.85 else "minor"))
    return cat


def _score_figures(ms: Manuscript, p: JournalProfile) -> Category:
    cat = Category("Figures and tables")
    figs, tabs = ms.figures, ms.tables
    n = len(figs) + len(tabs)
    words = max(1, ms.stats.get("words", 0))
    per_1k = n / (words / 1000)
    ok_density = 0.5 <= per_1k <= 2.5
    cat.checks.append(Check(
        "figs.density", "Balance of figures and tables against text length",
        1.0 if ok_density else 0.6, 0.8,
        evidence=f"{len(figs)} figures, {len(tabs)} tables ({per_1k:.1f} per 1000 words)",
        advice=("" if ok_density else
                ("Very few visuals for a paper this long — reviewers read dense "
                 "text without figures as hard work." if per_1k < 0.5 else
                 "A high float count can read as padding; consider merging panels.")),
        severity="ok" if ok_density else "minor"))

    if figs:
        no_cap = [f.id for f in figs if not f.caption_raw]
        s = 1.0 - len(no_cap) / len(figs)
        cat.checks.append(Check(
            "figs.captions", "Every figure has a caption", s, 1.2,
            evidence=f"{len(no_cap)} without a caption",
            advice=("" if s == 1.0 else
                    "Captions must be self-explanatory: a reader should understand "
                    "the figure without the body text."),
            severity="ok" if s == 1.0 else "major"))

        bad_fmt = [f.id for f in figs
                   if f.fmt.lower() in [x.lower() for x in p.figures.rejected_formats]]
        low_res = []
        for f in figs:
            if f.is_vector or not f.width_px or not f.placed_width_mm:
                continue
            if f.width_px / (f.placed_width_mm / 25.4) < p.figures.dpi_halftone:
                low_res.append(f.id)
        bad = len(set(bad_fmt) | set(low_res))
        s = 1.0 - bad / len(figs)
        cat.checks.append(Check(
            "figs.production", "Figures will pass production checks", s, 2.0,
            evidence=(f"{len(bad_fmt)} in a rejected format, "
                      f"{len(low_res)} below {p.figures.dpi_halftone} dpi at placed size"),
            advice=("" if s == 1.0 else
                    "Artwork quality control is automated at most publishers and "
                    "runs before an editor sees the paper. Re-export at final "
                    "printed size, or as vector."),
            severity="ok" if s == 1.0 else ("blocker" if s < 0.5 else "major")))
    return cat


def _score_reproducibility(ms: Manuscript, p: JournalProfile) -> Category:
    cat = Category("Reproducibility")
    text = _all_text(ms)
    found = [name for name, pat in _REPRO_PATTERNS.items()
             if re.search(pat, text, re.I)]
    s = len(found) / len(_REPRO_PATTERNS)
    cat.checks.append(Check(
        "repro.signals", "Data, code and software are identified", s, 1.5,
        evidence=("present: " + ", ".join(found)) if found else "none found",
        advice=("" if s == 1.0 else
                "State where the data are, name the software with versions, and "
                "tabulate the parameters. Reviewers who cannot see how to repeat "
                "the work ask for major revision by default."),
        severity="ok" if s >= 0.75 else "major"))

    has_da = ms.section_by_role(SectionRole.DATA_AVAILABILITY) is not None
    needed = SectionRole.DATA_AVAILABILITY in p.structure.required_sections
    cat.checks.append(Check(
        "repro.statement", "Data availability statement", 1.0 if has_da else 0.0,
        1.2 if needed else 0.6,
        evidence="present" if has_da else "absent",
        advice=("" if has_da else
                "Required by this journal." if needed else
                "Increasingly expected even where not mandatory."),
        severity="ok" if has_da else ("major" if needed else "minor")))
    return cat


def _score_writing(ms: Manuscript) -> Category:
    cat = Category("Writing signals")
    text = _all_text(ms)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) > 2]
    if not sentences:
        return cat
    lengths = [len(s.split()) for s in sentences]
    mean_len = sum(lengths) / len(lengths)
    long_frac = sum(1 for x in lengths if x > 40) / len(lengths)
    s = 1.0 if (14 <= mean_len <= 26 and long_frac < 0.1) else 0.6
    cat.checks.append(Check(
        "writing.sentences", "Sentence length", s, 0.8,
        evidence=f"mean {mean_len:.0f} words, {long_frac:.0%} over 40 words",
        advice=("" if s == 1.0 else
                "Long sentences are the most common reason reviewers describe a "
                "paper as hard to follow. Split anything over 40 words."),
        severity="ok" if s == 1.0 else "minor"))

    hedges = len(_HEDGE_RE.findall(text))
    per_1k = hedges / max(1, len(text.split()) / 1000)
    s = 1.0 if per_1k <= 12 else 0.6
    cat.checks.append(Check(
        "writing.hedging", "Hedging density", s, 0.6,
        evidence=f"{hedges} hedges ({per_1k:.0f} per 1000 words)",
        advice=("" if s == 1.0 else
                "Heavy hedging reads as low confidence in your own results. "
                "Hedge the interpretation, not the measurement."),
        severity="ok" if s == 1.0 else "minor"))
    return cat


def analyse(ms: Manuscript, profile: JournalProfile) -> ReviewReport:
    scope_check, matched = _score_scope(ms, profile)
    scope_cat = Category("Scope fit", [scope_check])
    cats = [
        scope_cat,
        _score_structure(ms, profile),
        _score_abstract(ms, profile),
        _score_references(ms, profile),
        _score_figures(ms, profile),
        _score_reproducibility(ms, profile),
        _score_writing(ms),
    ]
    return ReviewReport(profile, cats, matched)


def format_report(r: ReviewReport) -> str:
    L = ["=" * 74,
         f"SUBMISSION READINESS - {r.profile.publisher} / {r.profile.journal}",
         "=" * 74, ""]
    L.append(f"Readiness score: {r.readiness:.0%}")
    L.append("  This measures how complete and submittable the manuscript is.")
    L.append("  It is NOT a probability of acceptance - see below.")
    band, reasons = r.desk_rejection_risk()
    L.append(f"\nDesk-rejection risk: {band}")
    for x in reasons:
        L.append(f"  - {x}")

    L.append("\n" + "-" * 74)
    for cat in r.categories:
        L.append(f"\n{cat.name}: {cat.score:.0%}")
        for c in cat.checks:
            mark = {"blocker": "!!", "major": " !", "minor": "  ",
                    "info": "  ", "ok": " +"}.get(c.severity, "  ")
            L.append(f"  {mark} {c.label}: {c.evidence}")
            if c.advice:
                L.append(f"       -> {c.advice}")

    L.append("\n" + "-" * 74)
    L.append("Fix these first:")
    for i, c in enumerate(r.priorities(), 1):
        L.append(f"  {i}. [{c.severity}] {c.label} - {c.evidence}")

    L.append("\n" + "=" * 74)
    L.append("No acceptance probability is given, deliberately. Acceptance turns on")
    L.append("novelty and correctness as judged by referees; no surface feature of")
    L.append("a manuscript predicts that, no training data exists because rejected")
    L.append("manuscripts are not public, and a fabricated percentage would change")
    L.append("real submission decisions. What is above is measurable; a number")
    L.append("would not be.")
    L.append("=" * 74)
    return "\n".join(L)
