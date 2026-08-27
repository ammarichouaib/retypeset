#!/usr/bin/env python3
"""
Build training data for retypeset's heading and section-role models.

    python build_corpus.py --seed                      # write the built-in seed corpus
    python build_corpus.py --harvest "C:/papers"       # mine .docx you already have
    python build_corpus.py --harvest ./papers --seed   # both
    python build_corpus.py --stats

Why you do not need to "find 50 papers"
---------------------------------------
Two sources, neither of which requires downloading anything:

**1. Word heading styles are free ground truth.** Any manuscript whose author
applied `Heading 1`/`Heading 2` has already labelled its own headings. One
10-page paper with styled headings yields ~15 heading examples and ~150
body-text examples, correctly labelled, at no annotation cost. Ten papers you
already have on disk — yours, your co-authors', anything from your own library —
is a bigger corpus than most people bother to build. `--harvest` extracts this.

**2. A seed corpus of heading phrases.** Section headings are short, generic and
highly repetitive across a field: "Materials and Methods", "Techno-economic
analysis", "Sensitivity analysis". The built-in seed corpus lists several hundred
with their roles. These are not extracts from anyone's paper; they are the
conventional names of sections.

If you want more, the legitimate bulk sources are the PubMed Central Open Access
Subset (JATS XML with `<sec sec-type>` — headings already labelled by role) and
arXiv. Both permit programmatic access to their open subsets. Downloading
paywalled PDFs in bulk does not, which is why this script does not do it.

Everything runs locally. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from retypeset import learn
from retypeset.parse_docx import _match_role
from retypeset.ir import SectionRole

# ---------------------------------------------------------------------------
# Seed corpus: conventional section names, by role
# ---------------------------------------------------------------------------
# Written out rather than scraped. Weighted towards engineering, energy and
# power systems, because that is where this tool is used and because a lexicon
# trained on biomedical headings transfers poorly.

SEED_HEADINGS: dict[str, list[str]] = {
    "introduction": [
        "Introduction", "1. Introduction", "I. INTRODUCTION", "General introduction",
        "Background", "Background and motivation", "Motivation",
        "Introduction and background", "1 Introduction", "Context and motivation",
        "Problem statement", "Research context", "Scope of the study",
        "Aim of the study", "Objectives", "Research objectives",
    ],
    "related_work": [
        "Related work", "Literature review", "State of the art", "Prior art",
        "2. Literature review", "Review of related studies", "Previous studies",
        "Research gap", "Related studies", "A review of existing approaches",
        "Background literature", "Comparison with existing work",
    ],
    "theory": [
        "Theory", "Theoretical background", "Theoretical framework",
        "Mathematical model", "Mathematical formulation", "Mathematical modelling",
        "System modelling", "System model", "System description",
        "Modelling of the photovoltaic array", "Modelling of the wind turbine",
        "Electrochemical model", "Equivalent circuit model",
        "Governing equations", "Problem formulation", "Model formulation",
        "Physical modelling", "Thermodynamic analysis", "Energy balance",
        "State-space representation", "Dynamic model of the converter",
        "Component modelling", "Mathematical background",
    ],
    "methods": [
        "Methods", "Methodology", "Materials and methods", "Method",
        "Proposed method", "Proposed approach", "Proposed methodology",
        "Proposed algorithm", "Proposed control strategy", "Proposed system",
        "3. Proposed methodology", "Optimisation", "Optimization problem",
        "Optimisation strategy", "Multi-objective optimisation",
        "Energy management strategy", "Control strategy", "Design methodology",
        "Sizing methodology", "Techno-economic analysis",
        "Techno-economic assessment", "Economic model", "Cost model",
        "Objective function", "Constraints", "Design constraints",
        "Solution approach", "Implementation", "Hardware implementation",
        "FPGA implementation", "Algorithm description", "Data and methods",
        "Study area and data", "Data collection", "Data preprocessing",
        "Feature extraction", "Model training", "Protection of a very high voltage line span",
        "Fault detection scheme", "Neural network architecture",
        "Genetic algorithm implementation", "Particle swarm optimisation",
    ],
    "experimental": [
        "Experimental setup", "Experimental section", "Experimental procedure",
        "Experimental validation", "Experimental study", "Experiments",
        "Test bench", "Test bench description", "Simulation setup",
        "Simulation environment", "Case study", "Case studies",
        "Simulation parameters", "Experimental configuration",
        "Measurement setup", "Instrumentation", "Validation setup",
        "Prototype description", "Laboratory setup",
    ],
    "results": [
        "Results", "Simulation results", "Numerical results",
        "Experimental results", "Results of the simulation", "Findings",
        "4. Results", "Performance evaluation", "Performance analysis",
        "Numerical results and analysis", "Sensitivity analysis",
        "Parametric study", "Comparative analysis", "Comparative study",
        "Model validation", "Validation results", "Optimisation results",
        "Economic results", "Energy analysis results",
    ],
    "discussion": [
        "Discussion", "Discussions", "Analysis and discussion",
        "Interpretation of results", "General discussion",
        "Discussion of the findings", "Limitations",
        "Limitations of the study", "Practical implications",
    ],
    "results_and_discussion": [
        "Results and discussion", "Results and discussions",
        "4. Results and discussion", "Results & discussion",
        "Simulation results and discussion", "Experimental results and discussion",
    ],
    "conclusion": [
        "Conclusion", "Conclusions", "Concluding remarks",
        "Conclusions and perspectives", "Conclusions and future work",
        "Conclusion and future work", "Summary and conclusions",
        "5. Conclusion", "V. CONCLUSION", "Conclusions and recommendations",
    ],
    "future_work": [
        "Future work", "Future works", "Perspectives", "Outlook",
        "Recommendations", "Recommendations for future research",
        "Directions for future research",
    ],
    "nomenclature": [
        "Nomenclature", "Notation", "Abbreviations", "List of symbols",
        "List of symbols/acronyms", "Symbols and abbreviations",
        "Acronyms", "Glossary",
    ],
    "acknowledgements": [
        "Acknowledgements", "Acknowledgments", "Acknowledgement",
    ],
    "funding": [
        "Funding", "Funding information", "Funding statement",
        "Financial support", "Financial disclosure",
    ],
    "conflict_of_interest": [
        "Conflict of interest", "Conflicts of interest",
        "Declaration of competing interest",
        "Declaration of competing interests", "Competing interests",
        "Disclosure statement", "Conflict of interest statement",
    ],
    "author_contributions": [
        "Author contributions", "CRediT authorship contribution statement",
        "Authors' contributions", "Author contribution statement",
    ],
    "data_availability": [
        "Data availability", "Data availability statement",
        "Availability of data and materials", "Data and code availability",
    ],
    "ethics": [
        "Ethics approval", "Ethical approval", "Ethics statement",
        "Informed consent", "Ethical considerations",
    ],
    "appendix": [
        "Appendix", "Appendices", "Appendix A", "Appendix B",
        "Appendix A. Technical parameters", "Supplementary material",
        "Supplementary information",
    ],
    "references": [
        "References", "Bibliography", "Literature cited", "Works cited",
        "REFERENCES",
    ],
    "abstract": ["Abstract", "ABSTRACT", "Summary", "Graphical abstract"],
    "keywords": ["Keywords", "Key words", "Index terms", "INDEX TERMS"],
    "highlights": ["Highlights"],
}

# Body sentences: generic scientific prose in the shapes that most often get
# mistaken for headings (short, capitalised, or starting with a number).
SEED_BODY: list[str] = [
    "Transmission lines are a fundamental component of the power grid, responsible for transmitting electricity from power plants to substations.",
    "The proposed system was evaluated under three distinct operating conditions and the results are reported in the following section.",
    "As shown in Fig. 4, the response time decreases as the size of the training set increases.",
    "Table 2 summarises the parameters used throughout the optimisation procedure.",
    "This value was obtained by averaging the measured output over the full simulation horizon of one year.",
    "The Mho relay detects and localises faults based on the artificial neural network algorithm described earlier.",
    "In this configuration the converter is connected in series with the filter in order to reduce switching losses.",
    "The proposed strategy achieves a lower cost of energy than the conventional rule-based approach.",
    "It should be noted that the model assumes constant ambient temperature throughout the day.",
    "The error between the measured and predicted values remains below five percent in all cases.",
    "These results confirm the effectiveness of the proposed control scheme under variable irradiance.",
    "The simulation was carried out in MATLAB/Simulink with a fixed time step of 50 microseconds.",
    "The dataset covers one full calendar year at a temporal resolution of thirty minutes.",
    "A sensitivity study was performed by varying the discount rate between four and twelve percent.",
    "The electrolyser was operated at rated power for six hours per day during the summer months.",
    "Consequently, the levelised cost of hydrogen is dominated by the capital cost of the stack.",
    "The results obtained are consistent with those reported in comparable studies.",
    "The battery state of charge is constrained to remain between twenty and ninety percent.",
    "Figure 7 presents the Pareto front obtained after two hundred generations.",
    "The optimisation converged after approximately forty iterations in all test cases.",
    "Note that the wind speed data were measured at a height of ten metres above ground level.",
    "This assumption is justified by the relatively short duration of the transient.",
    "The remainder of this paper is organised as follows.",
    "Section 2 describes the system model and Section 3 presents the proposed methodology.",
    "First, the input data are normalised to the interval between zero and one.",
    "Second, the network is trained using the Levenberg-Marquardt algorithm.",
    "Finally, the trained model is validated against an independent test set.",
    "All monetary values are expressed in United States dollars for the reference year.",
    "The efficiency of the inverter was assumed constant and equal to ninety-six percent.",
    "Three scenarios were considered, corresponding to low, medium and high demand.",
    "The measured irradiance exceeded one thousand watts per square metre at solar noon.",
    "A comparison with the baseline case is provided in the following subsection.",
    "The computational time required for a single run did not exceed four minutes.",
    "These findings have direct implications for the design of hybrid energy systems.",
    "The influence of dust deposition on module performance is discussed below.",
    "Under these conditions the system reaches steady state within two seconds.",
    "The authors gratefully acknowledge the technical support provided during the measurement campaign.",
    "For the sake of brevity, only the most representative results are presented here.",
    "The proposed approach can be extended to larger networks without modification.",
    "It follows that the total annual cost is minimised when the storage capacity is 120 kWh.",
]

# SHORT non-headings. Without these the corpus teaches "short and capitalised
# means heading", and the model then promotes nomenclature entries, zone labels
# and captions. This was not hypothetical: the first model trained without them
# marked "Zone 1:" and "V  Voltage [V];" as headings, and called them keywords
# with 91 % confidence. Balancing the short end of the length distribution
# matters more than adding more long body sentences.
SEED_SHORT_BODY: list[str] = [
    "ANN Artificial Neural Network;", "FPGA Field Programmable Gate Array;",
    "VHV Very High Voltage;", "VHDL Hardware Description Language.",
    "PV Photovoltaic;", "SOC State of Charge;", "AOD Aerosol Optical Depth;",
    "LCOE Levelised Cost of Energy;", "LPSP Loss of Power Supply Probability;",
    "I Current [A];", "V Voltage [V];", "Z Impedance [Ohm].",
    "P Active power [kW];", "Q Reactive power [kVAr];", "T Temperature [C];",
    "f Frequency [Hz];", "R Resistance [Ohm];", "X Reactance [Ohm];",
    "eta Efficiency [%];", "Dis Distance [km];", "N Project lifetime [years];",
    "i Interest rate [%];", "C Capacitance [F];", "L Inductance [H].",
    "Zone 1:", "Zone 2:", "Zone 3:", "Zone 4 (Reverse):",
    "Case 1:", "Case 2:", "Case A:", "Scenario 1:", "Scenario 2:",
    "Step 1:", "Step 2:", "Step 3:", "Mode 1:", "Mode 2:",
    "Fig. 1. Remote protection zones", "Fig. 2. Architecture of the ANN.",
    "Figure 3. Load demand profile", "Figure 4. Pareto front curve",
    "Table 1. Comparative statistics", "Table 2. Simulation parameters",
    "Table A1. Technical parameters of the photovoltaic module",
    "where:", "such that:", "subject to:", "Note that:", "Here,",
    "Population size 100", "Function tolerance 1e-6", "Crossover fraction 0.8",
    "Rated power 20 kW", "Cut-in wind speed 2.75 m/s", "Open-circuit voltage 46.19 V",
    "Number of series cells 72", "Charging efficiency 0.85",
    "(a)", "(b)", "(c)", "(a) Voltage waveform", "(b) Current waveform",
    "No funding was received.", "Not applicable.", "The authors declare none.",
    "All data are available on request.", "Corresponding author.",
    "e-mail: author@university.edu", "Received 2026-01-01; accepted 2026-02-01",
    "1", "2", "3", "(1)", "(2)", "(3)",
    "Author 1, Author 2 and Author 3",
    "Department of Electrical Engineering, University of Somewhere",
]


def write_seed(path: Path | None = None) -> int:
    examples = []
    for role, titles in SEED_HEADINGS.items():
        for t in titles:
            examples.append({"text": t, "is_heading": True, "role": role})
    for s in SEED_BODY + SEED_SHORT_BODY:
        examples.append({"text": s, "is_heading": False, "role": ""})
    return learn.append_examples(examples, path)


# ---------------------------------------------------------------------------
# Harvesting from .docx you already have
# ---------------------------------------------------------------------------

_HEADING_STYLE = re.compile(r"^(heading\s*\d|titre\s*\d|title|überschrift\s*\d)$", re.I)
# Styles that look like headings but are not section headings.
_SKIP_STYLE = re.compile(r"caption|figure|table|toc|header|footer|footnote", re.I)


def harvest(folder: Path, path: Path | None = None,
            require_styles: bool = True, verbose: bool = True) -> dict:
    """Extract labelled examples from every .docx under `folder`.

    A paragraph styled `Heading N` is a heading *because its author said so* --
    that is ground truth, not a guess, and it is the reason this works without
    any manual annotation.
    """
    from retypeset import oox  # noqa: PLC0415

    files = sorted(p for p in folder.rglob("*.docx")
                   if not p.name.startswith("~$"))
    if not files:
        return {"files": 0, "written": 0, "headings": 0, "body": 0, "skipped": []}

    examples: list[dict] = []
    n_head = n_body = 0
    skipped: list[str] = []

    for f in files:
        try:
            scan = oox.scan(f)
        except Exception as exc:
            skipped.append(f"{f.name}: {exc}")
            continue

        styled = [p for p in scan.paragraphs if _HEADING_STYLE.match(p.style.strip())]
        if require_styles and len(styled) < 3:
            # Without heading styles we would be labelling with the same
            # heuristic the model is meant to replace, which teaches it nothing.
            skipped.append(f"{f.name}: only {len(styled)} styled heading(s)")
            continue

        for p in scan.paragraphs:
            text = re.sub(r"\s+", " ", p.text).strip()
            if not text or text.startswith("["):
                continue
            style = p.style.strip()
            if _SKIP_STYLE.search(style):
                continue

            if _HEADING_STYLE.match(style):
                if len(text) > 120:
                    continue                      # not a heading, whatever Word says
                role = _match_role(re.sub(r"^\s*[\d.IVXLC]+\s*", "", text))
                examples.append({
                    "text": text, "is_heading": True,
                    "role": role.value if role is not SectionRole.UNKNOWN else "",
                })
                n_head += 1
            elif 40 <= len(text) <= 400 and not p.in_table:
                examples.append({"text": text, "is_heading": False, "role": ""})
                n_body += 1

        if verbose:
            print(f"  {f.name}: {len(styled)} styled headings")

    written = learn.append_examples(examples, path)
    return {"files": len(files) - len(skipped), "written": written,
            "headings": n_head, "body": n_body, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", action="store_true", help="add the built-in seed corpus")
    ap.add_argument("--harvest", metavar="FOLDER", help="mine .docx files in a folder")
    ap.add_argument("--any-docx", action="store_true",
                    help="harvest even from files with no heading styles (lower quality)")
    ap.add_argument("--stats", action="store_true", help="show corpus status")
    ap.add_argument("--data", help="corrections .jsonl (default models/)")
    args = ap.parse_args()

    data = Path(args.data) if args.data else None

    if args.seed:
        n = write_seed(data)
        print(f"seed corpus: {n} new example(s) added")

    if args.harvest:
        folder = Path(args.harvest)
        if not folder.exists():
            print(f"error: {folder} not found", file=sys.stderr)
            return 2
        print(f"harvesting {folder} ...")
        r = harvest(folder, data, require_styles=not args.any_docx)
        print(f"  usable files : {r['files']}")
        print(f"  headings     : {r['headings']}")
        print(f"  body lines   : {r['body']}")
        print(f"  new examples : {r['written']} (duplicates skipped)")
        for s in r["skipped"][:10]:
            print(f"  skipped: {s}")
        if r["skipped"] and not args.any_docx:
            print("  (files without heading styles are skipped; --any-docx overrides,\n"
                  "   but those labels come from the same heuristic the model replaces)")

    if args.stats or not (args.seed or args.harvest):
        print()
        print(learn.status(data).report())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
