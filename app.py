#!/usr/bin/env python3
"""
retypeset — reformat a scientific manuscript for another journal, and see what changed.

    streamlit run app.py

Four steps, in the order the work has to happen:

    1 Start     the manuscript, and the journal it is going to — either a
                built-in profile or the publisher's own template, uploaded here
    2 Verify    what the parser understood: fidelity, front matter, sections,
                figures, references
    3 Check     compliance with the target journal, submission readiness, and
                optionally a panel of model referees
    4 Generate  the restyled .docx (from your template, or from the profile) or
                a compilable LaTeX project

**Advanced** in the sidebar drops the wizard and shows every panel as a tab,
plus local training and a profile inspector. The panels are the same objects;
only the navigation differs.

Readiness reports what is measurable from the text and deliberately does not
output a probability of acceptance; see retypeset.review for why.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="retypeset", layout="wide",
                   initial_sidebar_state="expanded", page_icon="📄")

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------
# `import retypeset` can succeed while giving you nothing. If `retypeset/__init__.py`
# is missing — which happens when a file loses its extension in a zip round trip
# — Python silently treats the folder as a namespace package: the import works,
# every attribute is absent, and the first symptom is "module 'retypeset' has no
# attribute 'parse_docx'" somewhere deep in a callback. Failing here, with the
# actual cause, costs one screen instead of an hour.

import retypeset  # noqa: E402

_REQUIRED = ["parse_docx", "audit", "check", "render_docx", "render_latex",
             "apply_template", "inspect_template", "load_profiles",
             "template_profile"]
_REQUIRED_SUB = {
    "retypeset.agents": ["test_connection", "list_models", "review_manuscript"],
    "retypeset.review": ["analyse"],
    "retypeset.sectioning": ["apply_ranges", "flatten"],
    "retypeset.learn": ["predict_heading", "status", "train"],
    "retypeset.template_profile": ["derive", "save"],
}


def _stale() -> list[str]:
    out = [a for a in _REQUIRED if not hasattr(retypeset, a)]
    for mod, attrs in _REQUIRED_SUB.items():
        m = sys.modules.get(mod)
        if m is None:
            continue
        out += [f"{mod}.{a}" for a in attrs if not hasattr(m, a)]
    return out


def _reload_package() -> None:
    """Force-reload retypeset and every submodule already in sys.modules.

    Streamlit re-executes the top-level script on every rerun but leaves
    imported packages in `sys.modules`. Edit a module while the app is running
    and you get the new UI calling the old library. Order matters: a module
    reloaded *before* `retypeset.ir` keeps a reference to the previous pydantic
    model classes, which gives you two incompatible `Manuscript` types in one
    process and validation errors on objects that look identical.
    """
    first = ["retypeset.ir", "retypeset.profile", "retypeset.oox", "retypeset.learn"]
    names = [n for n in sys.modules if n == "retypeset" or n.startswith("retypeset.")]
    names += [n for n in sys.modules if n == "ui" or n.startswith("ui.")]
    ordered = ([n for n in first if n in names]
               + sorted(n for n in names if n not in first and n != "retypeset")
               + (["retypeset"] if "retypeset" in names else []))
    for name in ordered:
        try:
            importlib.reload(sys.modules[name])
        except Exception:      # a half-reloaded package is still better than none
            pass


_missing = _stale()
if _missing:
    _reload_package()
    retypeset = importlib.import_module("retypeset")
    _missing = _stale()

if _missing:
    pkg_dir = Path(getattr(retypeset, "__file__", "") or ".").parent
    st.error("**retypeset did not import correctly**, and reloading it did not help.")
    st.markdown(f"""
Missing: `{', '.join(_missing)}`
Loaded from: `{getattr(retypeset, '__file__', 'namespace package — no __init__.py')}`
Package version: `{getattr(retypeset, '__version__', 'unknown')}`
`retypeset/__init__.py` present: **{(pkg_dir / '__init__.py').exists()}**

**Most likely cause:** the `retypeset` folder on disk is older than this app file.
Check that `{pkg_dir}` contains the current sources, then stop the app entirely
(Ctrl+C) and run `python -m streamlit run app.py` again.
    """)
    st.stop()

from ui import check as check_ui  # noqa: E402
from ui import common, produce, start, training, verify  # noqa: E402
from ui.common import STEPS  # noqa: E402


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _summary_bar(ms) -> None:
    c = st.columns(6)
    c[0].metric("Words", ms.stats.get("words", 0))
    inline_math = sum(1 for s in ms.iter_sections() for b in s.blocks
                      if b.paragraph for n in b.paragraph.inlines if n.kind == "math")
    c[1].metric("Equations", len(ms.equations),
                help=f"{len(ms.equations)} display + {inline_math} inline math runs")
    c[2].metric("Figures", len(ms.figures))
    c[3].metric("Tables", len(ms.tables))
    c[4].metric("References", len(ms.references))
    from retypeset.ir import SectionRole
    roles_ok = sum(1 for s in ms.body if s.role is not SectionRole.UNKNOWN)
    c[5].metric("Roles resolved", f"{roles_ok}/{len(ms.body)}")


def _stepper(current: int) -> None:
    """Clickable step header. Steps past the first need a parsed manuscript."""
    cols = st.columns(len(STEPS))
    for i, (col, label) in enumerate(zip(cols, STEPS), start=1):
        disabled = i > 1 and not common.has_manuscript()
        if col.button(label, use_container_width=True, key=f"step_{i}",
                      type="primary" if i == current else "secondary",
                      disabled=disabled):
            common.goto(i)
            st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("retypeset")
    st.caption(f"Reformat · verify · check · generate  \n"
               f"v{getattr(retypeset, '__version__', '?')}")

    mode = st.radio("Mode", ["Guided", "Advanced"], horizontal=True,
                    key="app_mode",
                    help="Guided walks the four steps. Advanced shows every "
                         "panel at once, plus training and profile details.")

    st.divider()
    if common.has_manuscript():
        st.markdown(f"**Manuscript**  \n`{st.session_state.get('fname')}`")
    else:
        st.caption("No manuscript loaded.")
    t = common.target()
    if t:
        st.markdown(f"**Target**  \n{t.label}")
        if st.session_state.get("tpl_path"):
            st.caption(f"template: `{Path(st.session_state['tpl_path']).name}`")
    else:
        st.caption("No target journal selected.")

    st.divider()
    with st.expander("Local training"):
        training.summary()
        st.caption("Full panel: switch Mode to **Advanced** → Training.")

    if st.button("Start over", use_container_width=True):
        common.reset_manuscript()
        common.goto(1)
        st.rerun()
    st.caption("Add a journal by dropping a JSON file into `profiles/`, or let "
               "step 1 derive one from a template. No code changes needed.")


ms = common.manuscript()
target = common.target()
report = st.session_state.get("audit")
step = st.session_state.setdefault("step", 1)

st.title(ms.meta.title if ms and ms.meta.title
         else st.session_state.get("fname", "retypeset"))
if ms:
    _summary_bar(ms)
    if target:
        common.target_banner(target)

# ---------------------------------------------------------------------------
# Guided mode — the four steps
# ---------------------------------------------------------------------------

if mode == "Guided":
    _stepper(step)
    st.divider()

    if step == 1 or not common.has_manuscript():
        if step != 1:
            common.goto(1)
        start.render()

    elif step == 2:
        verify.render(ms, report, target)
        common.nav(2)

    elif step == 3:
        check_ui.render(ms, target)
        common.nav(3)

    else:
        produce.render(ms, target)
        common.nav(4)

# ---------------------------------------------------------------------------
# Advanced mode — every panel as a tab
# ---------------------------------------------------------------------------

else:
    if not common.has_manuscript() or target is None:
        st.info("Advanced mode still needs a manuscript and a target. "
                "Load them below — this is the same panel as step 1.")
        start.render()
        st.stop()

    tabs = st.tabs(["Fidelity", "Front matter", "Sections", "Figures",
                    "References", f"Compliance · {target.publisher}", "Readiness",
                    "AI review", "Generate", "Training", "Profile"])
    with tabs[0]:
        verify.fidelity(ms, report)
    with tabs[1]:
        verify.front_matter(ms, target)
    with tabs[2]:
        verify.sections(ms, target)
    with tabs[3]:
        verify.figures(ms, target)
    with tabs[4]:
        verify.references(ms)
    with tabs[5]:
        check_ui.compliance(ms, target)
    with tabs[6]:
        check_ui.readiness(ms, target)
    with tabs[7]:
        check_ui.ai_review(ms, target)
    with tabs[8]:
        produce.render(ms, target)
    with tabs[9]:
        training.render()
    with tabs[10]:
        st.subheader(target.label)
        st.caption("The profile in force, exactly as the checker sees it. "
                   "Every limit here is a field in a JSON file you can edit.")
        if target.guide_url:
            st.markdown(f"[Guide for authors]({target.guide_url})")
        if target.notes:
            st.info(target.notes)
        st.json(target.model_dump(mode="json"), expanded=False)
        st.download_button("Download this profile (.json)",
                           target.model_dump_json(indent=2).encode("utf-8"),
                           file_name=f"{target.id}.json", mime="application/json")
