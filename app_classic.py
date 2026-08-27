#!/usr/bin/env python3
"""
retypeset review console -- upload a manuscript, verify what the parser understood,
check it against a target journal.

    streamlit run app.py

Nine tabs, in the order the work has to happen: verify the parse (Fidelity,
Front matter, Sections, Figures, References), check it against the target
journal (Compliance), see what the manuscript still needs (Readiness), then
produce the deliverable (Generate, Export).

Readiness reports what is measurable from the text and deliberately does not
output a probability of acceptance; see retypeset.review for why.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from dataclasses import replace
from html import escape as html_escape
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="retypeset review console", layout="wide",
                   initial_sidebar_state="expanded")

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------
# `import retypeset` can succeed while giving you nothing. If `retypeset/__init__.py` is
# missing -- which happens when a file loses its extension in a zip round trip
# or a partial upload -- Python silently treats the folder as a namespace
# package: the import works, every attribute is absent, and the first symptom is
# "module 'retypeset' has no attribute 'parse_docx'" somewhere deep in a callback.
# Failing here, with the actual cause, costs one screen instead of an hour.

import importlib  # noqa: E402
import sys  # noqa: E402

import retypeset  # noqa: E402

# What this build of the app requires. Checked against the *loaded* package, not
# the files on disk, because those are frequently not the same thing.
_REQUIRED = ["parse_docx", "audit", "check", "render_docx", "render_latex",
             "apply_template", "inspect_template", "load_profiles"]
_REQUIRED_SUB = {
    "retypeset.agents": ["test_connection", "list_models", "review_manuscript"],
    "retypeset.review": ["analyse"],
    "retypeset.sectioning": ["apply_ranges", "flatten"],
    "retypeset.learn": ["predict_heading"],
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
    imported packages in `sys.modules`. Edit `retypeset/agents.py` while the app is
    running and you get the new UI calling the old library: the symptom is
    `module 'retypeset.agents' has no attribute 'test_connection'` for a function
    that is plainly there on disk, or `unexpected keyword argument` for a
    parameter you just added. Restarting fixes it, but expecting the user to
    know that is a poor trade against ten lines of reload.

    Order matters and is not obvious. Every submodule sits at the same depth, so
    sorting by depth alone leaves their relative order arbitrary — and a module
    reloaded *before* `retypeset.ir` keeps a reference to the previous pydantic model
    classes. The result is two incompatible `Manuscript` types in one process
    and validation errors on objects that look identical. Dependencies are
    therefore reloaded explicitly first, and the package `__init__` last.
    """
    first = ["retypeset.ir", "retypeset.profile", "retypeset.oox", "retypeset.learn"]
    names = [n for n in sys.modules if n == "retypeset" or n.startswith("retypeset.")]
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
    st.error("**retypeset did not import correctly**, and reloading it did not help.")
    pkg_dir = Path(getattr(retypeset, "__file__", "") or ".").parent
    has_init = (pkg_dir / "__init__.py").exists()
    st.markdown(
        f"""
Missing: `{', '.join(_missing)}`
Loaded from: `{getattr(retypeset, '__file__', 'namespace package — no __init__.py')}`
Package version: `{getattr(retypeset, '__version__', 'unknown')}`
`retypeset/__init__.py` present: **{has_init}**

**Most likely cause:** the `retypeset` folder on disk is older than this app file —
they were copied separately, or only `app.py` was updated. Check that
`{pkg_dir}` contains the current sources.

**Also possible:** `retypeset/__init__.py` lost its `.py` extension in a zip round
trip, so Python treated the folder as a namespace package — the import then
succeeds while every attribute is absent.

If neither applies, stop the app entirely (Ctrl+C) and run
`python -m streamlit run app.py` again.
        """
    )
    st.stop()

from retypeset import agents, learn, review, sectioning  # noqa: E402
from retypeset.ir import Manuscript, SectionRole  # noqa: E402
from retypeset.profile import load_profiles  # noqa: E402

SEV_ICON = {"fail": "🔴", "warn": "🟠", "info": "🔵", "pass": "🟢"}
ISSUE_ICON = {"error": "🔴", "warning": "🟠", "info": "🔵"}


# ---------------------------------------------------------------------------
# Parsing (cached on file bytes so edits in the UI do not trigger a re-parse)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def parse_upload(data: bytes, name: str) -> tuple[str, dict, str, str]:
    """Parse uploaded bytes. Returns (ir_json, audit_report, media_dir, src)."""
    workdir = Path(tempfile.mkdtemp(prefix="retypeset_"))
    src = workdir / name
    src.write_bytes(data)
    ms = retypeset.parse_docx(src, media_dir=workdir / "media")
    report = retypeset.audit(ms, src)
    return ms.model_dump_json(), report, str(workdir / "media"), str(src)


def zip_dir(root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                z.write(p, str(p.relative_to(root)))
    return buf.getvalue()


def manuscript() -> Manuscript | None:
    raw = st.session_state.get("ir_json")
    return Manuscript.model_validate_json(raw) if raw else None


def store(ms: Manuscript) -> None:
    st.session_state["ir_json"] = ms.model_dump_json()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("retypeset")
    st.caption(f"Manuscript parser + journal compliance check · "
               f"v{getattr(retypeset, '__version__', '?')}")

    uploaded = st.file_uploader("Manuscript (.docx)", type=["docx"])

    profiles = load_profiles()
    if not profiles:
        st.error("No journal profiles found in `profiles/`.")
        st.stop()

    ids = sorted(profiles, key=lambda k: (profiles[k].publisher, profiles[k].journal))
    target_id = st.selectbox(
        "Target journal", ids, format_func=lambda k: profiles[k].label,
    )
    target = profiles[target_id]

    if not target.verified:
        st.warning("This profile is **unverified** — its limits are inferred from a "
                   "template, not read from the publisher's guidelines. Every rule "
                   "is reported as a warning rather than a failure.")

    with st.expander("Profile details"):
        st.write(f"**Template family:** `{target.template_family}`")
        st.write(f"**LaTeX class:** `{target.latex.document_class}`")
        st.write(f"**References:** {target.references.style}")
        st.write(f"**Single column:** {target.figures.single_column_mm:g} mm "
                 f"@ {target.figures.dpi_halftone} dpi")
        if target.guide_url:
            st.write(f"[Guide for authors]({target.guide_url})")
        if target.notes:
            st.caption(target.notes)

    st.divider()
    st.caption("Add a journal by dropping a JSON file into `profiles/`. "
               "No code changes needed.")

    if uploaded is not None and st.button("Parse manuscript", type="primary",
                                          use_container_width=True):
        with st.spinner("Reading OOXML, converting equations…"):
            try:
                ir_json, report, media, srcpath = parse_upload(
                    uploaded.getvalue(), uploaded.name)
            except retypeset.PandocError as exc:
                st.error(str(exc))
                st.stop()
        st.session_state.update(ir_json=ir_json, audit=report, media=media,
                                fname=uploaded.name, srcpath=srcpath)
        st.rerun()


ms = manuscript()
if ms is None:
    st.title("retypeset review console")
    st.markdown(
        """
Upload a `.docx` in the sidebar and press **Parse manuscript**.

**What this does today**

- Reads the manuscript into a publisher-neutral intermediate representation:
  equations converted from Word's OMML to LaTeX, tables, figures, references.
- Reports *fidelity* — what the reader silently lost. Pandoc, the only mature
  OMML→LaTeX converter, drops images and text-box paragraphs without warning,
  so figure inventory is read straight from the OOXML and the two are compared.
- Lets you correct anything the parser guessed: title, authors, section roles.
- Checks the result against the selected journal's rules and tells you what
  would get the manuscript returned.
- Reports what the manuscript still needs before submission, and produces the
  formatted `.docx` or a compilable LaTeX project.

**What it does not do**

Predict whether your paper will be accepted. Acceptance turns on novelty and
correctness judged by referees; no surface feature of a manuscript predicts it,
and there is no data to fit because rejected manuscripts are not public. The
Readiness tab reports what is measurable instead.
        """
    )
    st.stop()


report = st.session_state["audit"]
media_dir = Path(st.session_state["media"])

st.title(ms.meta.title or st.session_state.get("fname", "Untitled manuscript"))

c = st.columns(6)
c[0].metric("Words", ms.stats.get("words", 0))
inline_math = sum(
    1 for s in ms.iter_sections() for b in s.blocks
    if b.paragraph for n in b.paragraph.inlines if n.kind == "math"
)
c[1].metric("Equations", len(ms.equations),
            help=f"{len(ms.equations)} display + {inline_math} inline math runs")
c[2].metric("Figures", len(ms.figures))
c[3].metric("Tables", len(ms.tables))
c[4].metric("References", len(ms.references))
roles_ok = sum(1 for s in ms.body if s.role is not SectionRole.UNKNOWN)
c[5].metric("Roles resolved", f"{roles_ok}/{len(ms.body)}")

tabs = st.tabs(["Fidelity", "Front matter", "Sections", "Figures",
                "References", f"Compliance · {target.publisher}",
                "Readiness", "AI review", "Generate", "Export"])

# ---------------------------------------------------------------------------
# 1. Fidelity
# ---------------------------------------------------------------------------
with tabs[0]:
    st.subheader("Did anything get lost on the way in?")
    st.caption("Source counts come from the raw OOXML, not from the reader — "
               "that is the whole point of the check.")

    cols = st.columns(3)
    for col, chk in zip(cols, report["checks"]):
        col.metric(chk["name"], f"{chk['ir']} / {chk['source']}",
                   delta=chk["delta"] or None,
                   delta_color="normal" if chk["ok"] else "inverse")

    if report["blocking"]:
        st.error("**Blocking**")
        for b in report["blocking"]:
            st.write(f"- {b}")

    by_sev: dict[str, list] = {}
    for i in ms.issues:
        by_sev.setdefault(i.severity, []).append(i)

    for sev in ("error", "warning", "info"):
        items = by_sev.get(sev, [])
        if not items:
            continue
        with st.expander(f"{ISSUE_ICON[sev]} {sev.title()}s ({len(items)})",
                         expanded=(sev == "error")):
            seen = set()
            for i in items:
                key = (i.code, i.message[:60])
                if key in seen:
                    continue
                seen.add(key)
                n = sum(1 for x in items if x.code == i.code)
                suffix = f"  ×{n}" if n > 1 else ""
                st.write(f"`{i.code}`{suffix} — {i.message}")

# ---------------------------------------------------------------------------
# 2. Front matter
# ---------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Confirm what the parser inferred")
    st.caption("Word carries no semantic markup for authors or affiliations — "
               "these are typography, so they are guessed. Low confidence values "
               "are flagged; correcting them here updates the compliance check.")

    with st.form("front_matter"):
        title = st.text_input("Title", ms.meta.title)
        kw = st.text_input("Keywords (semicolon-separated)",
                           "; ".join(ms.meta.keywords))
        hl = st.text_area(
            "Highlights (one per line)", "\n".join(ms.meta.highlights),
            help=f"{target.publisher} requires "
                 f"{target.structure.highlights_min or '–'}–"
                 f"{target.structure.highlights_max or '–'} bullets of at most "
                 f"{target.structure.highlights_max_chars or '–'} characters."
                 if target.structure.highlights_required
                 else "Not required by this journal.",
            height=110,
        )
        abstract = st.text_area("Abstract", ms.meta.abstract_raw, height=200)

        st.markdown("**Authors**")
        author_rows = []
        for a in ms.meta.authors:
            cc = st.columns([3, 3, 3, 1])
            given = cc[0].text_input("Given", a.given, key=f"g_{a.id}")
            family = cc[1].text_input("Family", a.family, key=f"f_{a.id}")
            email = cc[2].text_input("Email", a.email, key=f"e_{a.id}")
            corr = cc[3].checkbox("Corr.", a.corresponding, key=f"c_{a.id}")
            author_rows.append((a.id, given, family, email, corr))

        if ms.meta.affiliations:
            st.markdown("**Affiliations**")
            for aff in ms.meta.affiliations:
                st.text_input(f"[{aff.marker or aff.id}]", aff.raw, key=f"aff_{aff.id}")

        if st.form_submit_button("Save front matter", type="primary"):
            ms.meta.title = title.strip()
            ms.meta.keywords = [k.strip() for k in kw.split(";") if k.strip()]
            ms.meta.highlights = [h.strip() for h in hl.splitlines() if h.strip()]
            ms.meta.abstract_raw = abstract.strip()
            for aid, given, family, email, corr in author_rows:
                a = next(x for x in ms.meta.authors if x.id == aid)
                a.given, a.family, a.email, a.corresponding = given, family, email, corr
                a.provenance.method = "explicit"
                a.provenance.confidence = 1.0
            for aff in ms.meta.affiliations:
                aff.raw = st.session_state.get(f"aff_{aff.id}", aff.raw)
            store(ms)
            st.success("Saved.")
            st.rerun()

    low = [a for a in ms.meta.authors if a.provenance.confidence < 0.8]
    if low:
        st.warning(f"{len(low)} author record(s) still carry low parser confidence. "
                   "Confirm them above — author order and corresponding-author "
                   "markers are not recoverable from the file automatically.")

# ---------------------------------------------------------------------------
# 3. Sections
# ---------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Sections")
    st.caption("Roles drive everything downstream: which limits apply, where the "
               "renderer places content, what the journal requires.")

    role_values = [r.value for r in SectionRole]
    mode = st.radio(
        "How do you want to set sections?",
        ["Guided — pick each section from the text, one at a time",
         "Quick — assign roles to detected sections",
         "Table — edit every block at once (advanced)"],
        key="sec_mode",
        help="Heading detection is guesswork when the author applied no heading "
             "styles. The guided picker is the reliable way to correct it.",
    )

    # ---------------- guided: one section at a time ------------------------
    if mode.startswith("Guided"):
        rows = sectioning.flatten(ms)

        # Which sections to walk through: what this journal requires, plus
        # anything already detected, in reading order.
        wanted: list[str] = []
        for r in target.structure.required_sections:
            if r.value not in wanted:
                wanted.append(r.value)
        for sec in ms.body:
            if sec.role is not SectionRole.UNKNOWN and sec.role.value not in wanted:
                wanted.append(sec.role.value)
        for essential in ("introduction", "methods", "results", "conclusion"):
            if essential not in wanted:
                wanted.append(essential)
        wanted = [w for w in wanted if w not in ("title", "authors", "affiliations")]

        assignments: dict[str, sectioning.Assignment] = st.session_state.setdefault(
            "assignments", {})
        step = st.session_state.setdefault("sec_step", 0)
        step = max(0, min(step, len(wanted) - 1))
        role = wanted[step]

        st.progress((step + 1) / len(wanted),
                    text=f"Step {step + 1} of {len(wanted)} · **{role}**")

        done = ", ".join(f"{k}" for k in assignments) or "nothing yet"
        st.caption(f"Confirmed so far: {done}")

        st.markdown(
            f"#### Select the **{role.replace('_', ' ')}**\n"
            "Drag the handles to cover exactly the paragraphs that belong to this "
            "section. Do **not** include its heading — that is picked up "
            "automatically."
        )

        prior = assignments.get(role)
        suggested = ((prior.start, prior.end) if prior
                     else sectioning.suggest_range(rows, role))
        if suggested:
            default = suggested
        else:
            # No detected section for this role. Do NOT fall back to the top of
            # the document: that silently offers the title block as if it were
            # the abstract, and one careless Confirm assigns the wrong text.
            # Sections run in order, so start just after the last confirmed one.
            after = max((a.end for a in assignments.values()), default=-1) + 1
            after = min(after, max(0, len(rows) - 1))
            default = (after, min(after + 2, len(rows) - 1))

        lo, hi = st.slider(
            "Paragraph range", 0, max(0, len(rows) - 1),
            value=(int(default[0]), int(default[1])),
            key=f"range_{role}",
            help="The numbers are block positions in the manuscript.",
        )
        if suggested:
            st.caption(f"Suggested from the current parse: {suggested[0]}–{suggested[1]}")
        else:
            st.warning(
                "Nothing in the manuscript was detected as this section. The "
                "range below is just the position after your last confirmation "
                "— set it yourself, or press **Skip** if the section is absent.")

        # Manuscript view with the selection highlighted, scrolled near it.
        window_lo = max(0, lo - 6)
        window_hi = min(len(rows), hi + 8)
        html = [
            "<div style='max-height:420px;overflow-y:auto;border:1px solid "
            "rgba(128,128,128,.35);border-radius:8px;padding:12px;font-size:0.9rem;"
            "line-height:1.5'>"
        ]
        for i in range(window_lo, window_hi):
            r = rows[i]
            inside = lo <= i <= hi
            body = html_escape(r.text[:400]) or "<em>(empty)</em>"
            if r.is_heading:
                body = f"<strong>{body}</strong>"
            style = ("background:rgba(255,196,0,.28);border-left:3px solid #f2a900;"
                     if inside else "border-left:3px solid transparent;")
            html.append(
                f"<div style='{style}padding:3px 8px;margin:1px 0'>"
                f"<span style='opacity:.45;font-family:monospace'>{i:>3}</span> "
                f"{body}</div>"
            )
        html.append("</div>")
        st.markdown("".join(html), unsafe_allow_html=True)

        selected_words = sum(
            len(rows[i].text.split()) for i in range(lo, min(hi + 1, len(rows)))
        )
        st.caption(f"Selection: {hi - lo + 1} block(s), ~{selected_words} words")

        if role == "abstract" and target.structure.abstract_max_words:
            limit = target.structure.abstract_max_words
            if selected_words > limit:
                st.warning(f"{selected_words} words — {target.journal} allows "
                           f"{limit}. Either the selection is too wide or the "
                           "abstract needs cutting.")

        c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
        if c1.button("← Back", disabled=step == 0, use_container_width=True):
            st.session_state["sec_step"] = step - 1
            st.rerun()
        if c2.button("Skip", use_container_width=True,
                     help="This section is not in the manuscript."):
            assignments.pop(role, None)
            st.session_state["sec_step"] = step + 1
            st.rerun()
        if c3.button(f"Confirm {role} →", type="primary", use_container_width=True):
            assignments[role] = sectioning.Assignment(role=role, start=lo, end=hi)
            st.session_state["sec_step"] = step + 1
            st.rerun()
        if c4.button("Apply all", use_container_width=True,
                     disabled=not assignments,
                     help="Rebuild the section tree from everything confirmed."):
            rows = sectioning.flatten(ms)
            examples = sectioning.range_training_examples(
                rows, list(assignments.values()))
            sectioning.apply_ranges(ms, rows, list(assignments.values()))
            store(ms)
            if examples:
                try:
                    n = learn.append_examples(examples)
                    st.session_state["last_taught"] = n
                except Exception as exc:
                    st.warning(f"Could not record training examples: {exc}")
            st.success(f"Applied {len(assignments)} section(s).")
            st.rerun()

        if step >= len(wanted) - 1 and assignments:
            st.info("That is the last section. Press **Apply all** to rebuild.")

        if st.session_state.get("last_taught"):
            st.caption(f"{st.session_state['last_taught']} example(s) recorded for "
                       "training · `python train_local.py --status`")

        st.divider()
        st.markdown("**Current tree**")
        for sec in ms.body:
            st.write(f"L{sec.level} · **{sec.title_raw or '(untitled)'}** → "
                     f"`{sec.role.value}` · {len(sec.blocks)} block(s)")
        st.stop()

    # ---------------- table: paragraph-level editor ------------------------
    if mode.startswith("Table"):
        st.markdown(
            "Every block of the manuscript is listed in order. Tick **heading** "
            "on the lines that actually start a section, set its level and role, "
            "then save. The tree is rebuilt from your marks — nothing is dropped."
        )
        rows = sectioning.flatten(ms)
        table = sectioning.to_table(rows)

        f1, f2 = st.columns([3, 1])
        needle = f1.text_input("Filter text", "", key="sec_filter")
        only_head = f2.checkbox("Headings only", False, key="sec_only_head")
        view = [
            r for r in table
            if (not needle or needle.lower() in r["text"].lower())
            and (not only_head or r["heading"])
        ]
        st.caption(f"{len(view)} of {len(table)} blocks shown")

        edited = st.data_editor(
            view,
            key="sec_editor",
            use_container_width=True,
            height=460,
            hide_index=True,
            column_config={
                "#": st.column_config.NumberColumn("#", disabled=True, width="small"),
                "heading": st.column_config.CheckboxColumn("heading", width="small"),
                "level": st.column_config.NumberColumn("lvl", min_value=1,
                                                       max_value=6, step=1,
                                                       width="small"),
                "role": st.column_config.SelectboxColumn("role", options=role_values,
                                                         width="medium"),
                "kind": st.column_config.TextColumn("kind", disabled=True,
                                                    width="small"),
                "text": st.column_config.TextColumn("text", disabled=True,
                                                    width="large"),
            },
        )

        b1, b2 = st.columns([1, 1])
        if b1.button("Save section marks", type="primary", use_container_width=True):
            rows = sectioning.from_table(rows, edited)
            sectioning.rebuild(ms, rows)
            store(ms)
            examples = sectioning.training_examples(rows)
            if examples:
                try:
                    n = learn.append_examples(examples)
                    st.session_state["last_taught"] = n
                except Exception as exc:
                    st.warning(f"Could not record training examples: {exc}")
            st.success(f"Rebuilt {len(ms.body)} top-level section(s).")
            st.rerun()

        if st.session_state.get("last_taught"):
            b2.info(f"{st.session_state['last_taught']} new example(s) saved for "
                    "training. Run `python train_local.py --status`.")

        st.divider()
        st.markdown("**Resulting tree**")
        for sec in ms.body:
            st.write(f"{'　' * (sec.level - 1)}L{sec.level} · "
                     f"**{sec.title_raw or '(untitled preamble)'}** → `{sec.role.value}` "
                     f"· {len(sec.blocks)} block(s)")
        st.stop()

    # ---------------- quick: role dropdowns per detected section -----------
    changed = False
    for sec in ms.body:
        cc = st.columns([5, 3, 2])
        label = sec.title_raw or "(untitled preamble)"
        cc[0].write(f"**{label[:70]}**")
        cc[0].caption(f"{len(sec.blocks)} block(s) · level {sec.level}")
        new = cc[1].selectbox("role", role_values,
                              index=role_values.index(sec.role.value),
                              key=f"role_{sec.id}", label_visibility="collapsed")
        conf = sec.role_provenance.confidence
        cc[2].caption(f"{sec.role_provenance.method} · {conf:.0%}")
        if new != sec.role.value:
            sec.role = SectionRole(new)
            sec.role_provenance.method = "explicit"
            sec.role_provenance.confidence = 1.0
            changed = True

    if changed:
        # Abstract text is mirrored into meta, so keep it in sync when the user
        # relabels a section as the abstract.
        abs_sec = ms.section_by_role(SectionRole.ABSTRACT)
        if abs_sec:
            ms.meta.abstract = abs_sec.blocks
            ms.meta.abstract_raw = " ".join(
                b.paragraph.plain_text() for b in abs_sec.blocks if b.paragraph
            ).strip()
        store(ms)
        st.rerun()

    unknown = [s for s in ms.body if s.role is SectionRole.UNKNOWN and s.title_raw]
    if unknown:
        st.info(f"{len(unknown)} section(s) unlabelled. This is the one place a "
                "language model belongs at runtime — classification only, "
                "constrained to the role list, with the result shown for "
                "confirmation. Not built yet.")

# ---------------------------------------------------------------------------
# 4. Figures
# ---------------------------------------------------------------------------
with tabs[3]:
    st.subheader("Figures")
    f = target.figures
    need = f.min_px_single_column or f.required_px(f.single_column_mm, "halftone")
    st.caption(f"{target.publisher} needs **{need} px** width for "
               f"{f.dpi_halftone} dpi at {f.single_column_mm:g} mm single-column, "
               f"and **{f.required_px(f.double_column_mm, 'halftone')} px** at "
               f"{f.double_column_mm:g} mm full width. Resolution is always "
               "measured at final printed size.")

    for fig in ms.figures:
        cc = st.columns([1, 3])
        path = media_dir / fig.files[0] if fig.files else None
        if path and path.exists() and path.suffix.lower() in (
            ".png", ".jpg", ".jpeg", ".gif"
        ):
            cc[0].image(str(path), width=150)
        else:
            cc[0].info(f"`{fig.fmt or '?'}`\nnot previewable")

        ok_fmt = fig.fmt.lower() not in [x.lower() for x in f.rejected_formats]

        if fig.is_vector:
            # Vector art has no resolution to check: it scales without loss.
            res = "🟢 vector — resolution not applicable"
        elif fig.width_px:
            ok_res = fig.width_px >= need
            res = (f"{'🟢' if ok_res else '🔴'} {fig.width_px}×{fig.height_px} px"
                   + (f" (needs ≥{need})" if not ok_res else ""))
            if fig.dpi:
                res += f" · {fig.dpi:g} dpi at its placed size"
        else:
            res = "⚪ size could not be measured"

        placed = (f" · placed at {fig.placed_width_mm:g}×{fig.placed_height_mm:g} mm"
                  if fig.placed_width_mm else "")

        cc[1].markdown(
            f"**{fig.id}** — {fig.label or '(no label)'}  \n"
            f"{fig.caption_raw[:110] or '_no caption_'}  \n"
            f"{'🟢' if ok_fmt else '🔴'} format `{fig.fmt}` · {res}{placed}  \n"
            f"`{', '.join(fig.files)}`"
        )
        if fig.is_vector and fig.fmt.lower() in ("emf", "wmf"):
            cc[1].caption(
                "EMF/WMF is a Windows metafile: no browser can preview it and "
                "pdfLaTeX cannot place it. It is vector, so quality is fine — "
                "re-export as PDF from the original application, or the LaTeX "
                "route will convert it if Inkscape or LibreOffice is installed."
            )
        st.divider()

# ---------------------------------------------------------------------------
# 5. References
# ---------------------------------------------------------------------------
with tabs[4]:
    st.subheader("References")
    st.caption("Stored as CSL-JSON where parsing succeeded. Once every entry is "
               "CSL-JSON, restyling between journals is citeproc plus a CSL file — "
               "the Zotero style repository already covers roughly 10,000 journals. "
               "The verbatim source string is always kept as a fallback.")

    good = sum(1 for r in ms.references if r.parse_confidence >= 0.6)
    st.progress(good / max(1, len(ms.references)),
                text=f"{good}/{len(ms.references)} parsed with usable confidence")

    show_low = st.checkbox("Show only low-confidence entries", value=False)
    for r in ms.references:
        if show_low and r.parse_confidence >= 0.6:
            continue
        icon = "🟢" if r.parse_confidence >= 0.6 else "🟠"
        with st.expander(f"{icon} {r.id} · {r.parse_confidence:.0%} · {r.raw[:80]}"):
            st.text(r.raw)
            st.json(r.csl, expanded=False)

# ---------------------------------------------------------------------------
# 6. Compliance
# ---------------------------------------------------------------------------
with tabs[5]:
    result = retypeset.check(ms, target, media_dir)

    cc = st.columns(4)
    cc[0].metric("Passed", len(result.passes))
    cc[1].metric("Warnings", len(result.warnings))
    cc[2].metric("Failures", len(result.failures))
    cc[3].metric("Score", f"{result.score():.0%}")

    if result.ready:
        st.success(f"No blocking compliance failures for {target.journal}.")
    else:
        st.error(f"{len(result.failures)} blocking failure(s) for {target.journal}.")

    for sev, label in (("fail", "Failures"), ("warn", "Warnings"),
                       ("info", "Info"), ("pass", "Passed")):
        items = [x for x in result.findings if x.severity == sev]
        if not items:
            continue
        with st.expander(f"{SEV_ICON[sev]} {label} ({len(items)})",
                         expanded=sev in ("fail", "warn")):
            for x in items:
                st.markdown(f"**`{x.rule}`** {x.message}")
                if x.detail:
                    st.caption(x.detail)
                if x.fix:
                    st.markdown(f"→ *{x.fix}*")
                if x.locations:
                    st.code(", ".join(x.locations[:25]), language=None)

    st.download_button(
        "Download compliance report (.txt)",
        retypeset.format_compliance(result).encode("utf-8"),
        file_name=f"compliance_{target.id}.txt",
        mime="text/plain",
    )

# ---------------------------------------------------------------------------
# 7. Readiness
# ---------------------------------------------------------------------------
with tabs[6]:
    rep = review.analyse(ms, target)
    band, reasons = rep.desk_rejection_risk()

    st.subheader(f"What this manuscript needs for {target.journal}")

    cc = st.columns([1, 1, 2])
    cc[0].metric("Readiness", f"{rep.readiness:.0%}",
                 help="How complete and submittable the manuscript is. "
                      "Not a probability of acceptance.")
    cc[1].metric("Desk-rejection risk", band,
                 help="Desk rejection is mostly mechanical, so its drivers can "
                      "be named. How often each triggers a rejection varies by "
                      "editor and is not knowable from the text.")
    with cc[2]:
        st.markdown("**Why**")
        for x in reasons[:4]:
            st.caption(f"· {x}")

    with st.expander("Why there is no “chance of acceptance” percentage", expanded=False):
        st.markdown(
            """
This was requested, and refusing it is a deliberate decision rather than a
missing feature.

- **Acceptance turns on novelty and correctness**, judged by two or three
  people. No surface feature of a manuscript predicts that.
- **There is no training data.** Rejected manuscripts are not public, so the
  outcome variable cannot be observed at all — there is nothing to fit.
- **Base rates swing from ~8 % to ~60 %** between journals and move year to
  year. Even a perfect model would need per-journal, per-year calibration that
  nobody has.
- **The harm is asymmetric.** “62 % chance of acceptance” reads as knowledge.
  An author who submits a weak paper because a number encouraged them, or
  shelves a good one because it did not, has been actively misled.

Everything on this page traces to a specific observation about your text. A
percentage would not, so it is not shown.
            """
        )

    st.divider()
    st.markdown("### Fix these first")
    for i, c in enumerate(rep.priorities(8), 1):
        icon = {"blocker": "🔴", "major": "🟠", "minor": "🟡"}.get(c.severity, "⚪")
        st.markdown(f"**{i}. {icon} {c.label}** — {c.evidence}")
        if c.advice:
            st.caption(c.advice)

    st.divider()
    st.markdown("### By category")
    for cat in rep.categories:
        with st.expander(f"{cat.name} — {cat.score:.0%}",
                         expanded=cat.score < 0.7):
            for c in cat.checks:
                icon = {"blocker": "🔴", "major": "🟠", "minor": "🟡",
                        "ok": "🟢"}.get(c.severity, "⚪")
                st.markdown(f"{icon} **{c.label}** · {c.evidence}")
                if c.advice:
                    st.caption(c.advice)

    st.download_button(
        "Download readiness report (.txt)",
        review.format_report(rep).encode("utf-8"),
        file_name=f"readiness_{target.id}.txt",
        mime="text/plain",
    )

    if not target.scope_keywords:
        st.info("This journal profile has no `scope_keywords`, so topic fit was "
                "skipped. Add them from the journal's aims-and-scope page to "
                "enable the single most useful check here.")

# ---------------------------------------------------------------------------
# 8. AI review panel
# ---------------------------------------------------------------------------
with tabs[7]:
    st.subheader("Model peer-review panel")
    st.caption(
        "Several models, each given a different referee brief, then ranked by "
        "what they independently agree on. Every finding must quote your "
        "manuscript verbatim; quotes that cannot be located in your text are "
        "withheld and counted."
    )

    st.warning(
        "**This sends your manuscript text to a third party.** For work under "
        "review that may not be acceptable to you, your co-authors or your "
        "institution. `Ollama · local` runs on your own machine and sends "
        "nothing anywhere — use it for confidential drafts.",
        icon="⚠️",
    )

    presets = agents.PRESETS
    chosen_ids = st.multiselect(
        "Referees (models)",
        list(presets),
        default=[k for k in ("groq-llama70b", "gemini-flash") if k in presets],
        format_func=lambda k: presets[k].label,
    )

    briefs = st.multiselect(
        "Review angles",
        list(agents.REVIEWERS),
        default=["methods", "novelty", "clarity"],
        format_func=lambda k: agents.REVIEWERS[k]["label"],
        help="Each angle is a separate call per model, so cost and time scale "
             "with models × angles.",
    )

    with st.expander("API keys and setup", expanded=False):
        st.markdown(
            """
Keys are read from environment variables, or from `.streamlit/secrets.toml`:

```toml
GROQ_API_KEY = "gsk_..."
GEMINI_API_KEY = "AIza..."
OPENROUTER_API_KEY = "sk-or-..."
```

Free tiers, as of writing: **Groq** (fast, generous), **Google AI Studio**
(Gemini Flash), **OpenRouter** (`:free` model variants, rate-limited).
**Ollama** needs no key — `ollama serve` and `ollama pull llama3.1:8b`.

Never commit keys. `.streamlit/secrets.toml` is already gitignored.
            """
        )
        keys: dict[str, str] = {}
        for pid in chosen_ids:
            p = presets[pid]
            if not p.api_key_env:
                continue
            have = bool(p.api_key())
            try:
                have = have or bool(st.secrets.get(p.api_key_env))
            except Exception:
                pass
            val = st.text_input(
                f"{p.label} — {p.api_key_env}",
                type="password",
                placeholder="found in environment / secrets" if have else "paste key",
                key=f"key_{pid}",
            )
            if val:
                keys[pid] = val
            elif have:
                try:
                    keys[pid] = st.secrets.get(p.api_key_env, "") or p.api_key()
                except Exception:
                    keys[pid] = p.api_key()

    # Model ids go stale: providers retire them and return a 404 that reads
    # like a broken endpoint. Make them editable, and let the user ask the
    # provider what it will actually accept.
    ready: list = []
    if chosen_ids:
        st.markdown("**Check each referee before running the panel**")
    for pid in chosen_ids:
        p = presets[pid]
        cols = st.columns([3, 3, 1.1, 1.1])
        cols[0].markdown(f"**{p.label}**")
        cols[0].caption(p.notes)
        model = cols[1].text_input("model id", p.model, key=f"model_{pid}",
                                   label_visibility="collapsed")
        prov = p if model == p.model else replace(p, model=model)
        ready.append(prov)

        if cols[2].button("Test", key=f"test_{pid}", use_container_width=True):
            with st.spinner("…"):
                ok, msg = agents.test_connection(prov, keys.get(pid, ""))
            st.session_state[f"probe_{pid}"] = (ok, msg)
        if cols[3].button("Models", key=f"list_{pid}", use_container_width=True,
                          help="Ask the provider which models this key can call"):
            try:
                st.session_state[f"models_{pid}"] = agents.list_models(
                    prov, keys.get(pid, ""))
            except Exception as exc:
                st.session_state[f"models_{pid}"] = [f"error: {exc}"]

        # Local models need their own handling: the installed list is knowable,
        # the context window has to be set explicitly, and a code-tuned model
        # will answer a manuscript prompt badly without saying so.
        if p.kind == "ollama":
            try:
                installed = agents.ollama_models(p.base_url)
            except Exception:
                installed = []
            if installed:
                general = [m for m in installed if not agents.is_coder_model(m)]
                pick = st.selectbox(
                    "installed models", installed,
                    index=installed.index(model) if model in installed else 0,
                    key=f"ollama_pick_{pid}",
                )
                if pick != model:
                    prov = replace(prov, model=pick)
                    model = pick
                if agents.is_coder_model(model):
                    st.warning(
                        f"`{model}` is tuned for code. It will answer, but its "
                        "critique of prose is noticeably weaker than a general "
                        "model of the same size — and nothing in the output "
                        "tells you that is why."
                        + (f" You have {', '.join(general[:3])} installed."
                           if general else ""))
                ctx_tokens = st.number_input(
                    "num_ctx (tokens)", 2048, 131072, int(prov.num_ctx), 2048,
                    key=f"numctx_{pid}",
                    help="Ollama defaults to 2048 and silently discards anything "
                         "longer. Roughly 4 characters per token: 16384 tokens "
                         "holds about 60 000 characters.")
                prov = replace(prov, num_ctx=int(ctx_tokens))
                ready[-1] = prov
            else:
                st.error(
                    "No Ollama server answered on "
                    f"`{p.base_url}`. Start it with `ollama serve`. If retypeset is "
                    "deployed on a server, a model on your PC is unreachable by "
                    "definition — run retypeset locally for this provider.")

        probe = st.session_state.get(f"probe_{pid}")
        if probe:
            (st.success if probe[0] else st.error)(f"{p.label}: {probe[1]}")
        got = st.session_state.get(f"models_{pid}")
        if got:
            st.caption(f"{len(got)} model(s) available")
            st.code("\n".join(got[:40]), language=None)

    n_calls = len(ready) * len(briefs)
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.caption(f"{len(ready)} model(s) × {len(briefs)} angle(s) = **{n_calls}** "
               "API call(s), run in parallel.")
    budget = c2.number_input("Context (chars)", 8000, 200_000, 60_000, 4000,
                             help="How much of the manuscript each referee sees. "
                                  "Lower it for slow local models.")
    has_local = any(p.kind == "ollama" for p in ready)
    n_local = sum(1 for p in ready if p.kind == "ollama") * len(briefs)
    timeout = c3.number_input("Timeout (s)", 30, 3600,
                              900 if has_local else 120, 30,
                              help="Per call. A local 8B model on CPU spends "
                                   "most of its time reading the prompt, and "
                                   "the first call also loads the model.")
    if has_local:
        st.info(
            f"**Local model: {n_local} call(s) will run one after another, not "
            "in parallel.** There is one model on one CPU, so concurrent calls "
            "only queue — that is what made three of four expire earlier while "
            "a fourth answered fine.\n\n"
            "Most of the wait is *reading* the prompt, not writing the answer, "
            "so context length dominates: at 60 000 characters expect several "
            "minutes per call. **Start at 15 000** and raise it once you have "
            "seen the timing."
        )

    if st.button("Run review panel", type="primary", disabled=n_calls == 0,
                 use_container_width=True):
        with st.spinner(f"Running {n_calls} referee call(s)…"):
            rep = agents.review_manuscript(
                ms, target, ready, briefs, api_keys=keys,
                max_findings=6, max_chars=int(budget), timeout=int(timeout),
            )
        st.session_state["panel"] = rep

    rep = st.session_state.get("panel")
    if rep:
        ok = sum(1 for r in rep.runs if not r.error)
        m = st.columns(4)
        m[0].metric("Agents responded", f"{ok}/{len(rep.runs)}")
        m[1].metric("Grounded findings", len(rep.findings))
        m[2].metric("Withheld", len(rep.withheld),
                    help="Quote could not be found in your manuscript — the "
                         "model invented it.")
        agreed = sum(1 for f in rep.findings if f.agreement > 1)
        m[3].metric("Agreed by ≥2", agreed)

        n_providers = len({r.provider for r in rep.runs if not r.error})
        if n_providers < 2:
            st.caption(
                "Only one model ran, so agreement here means the *same* model "
                "raised a point under two different briefs — much weaker "
                "evidence than two independent models converging. Add a second "
                "provider if you want agreement to mean anything."
            )

        g = rep.groundedness()
        if g:
            st.caption("Quote-verification rate per model: "
                       + " · ".join(f"**{k}** {v:.0%}" for k, v in g.items()))
        timings = sorted(rep.runs, key=lambda r: -r.seconds)[:6]
        if any(r.seconds for r in timings):
            st.caption("Time per call: " + " · ".join(
                f"{r.provider}/{r.reviewer} {r.seconds:.0f}s"
                + ("✗" if r.error else "")
                for r in timings))
        for e in rep.errors:
            st.error(e)

        if not rep.findings:
            st.info("No grounded findings. If several were withheld, the models "
                    "are paraphrasing rather than quoting — try a stronger model.")

        for i, f in enumerate(rep.findings, 1):
            icon = "🟠" if f.severity == "major" else "🟡"
            agree = f" · **{f.agreement} referees agree**" if f.agreement > 1 else ""
            with st.container(border=True):
                st.markdown(f"{icon} **{i}. {f.issue}**{agree}")
                if f.section:
                    st.caption(f"section: {f.section}")
                if f.why:
                    st.markdown(f"*Why it matters:* {f.why}")
                if f.fix:
                    st.markdown(f"*Suggested fix:* {f.fix}")
                if f.quote:
                    st.markdown(f"> {f.quote}")
                fc = st.columns([1, 1, 6])
                key = f"{f.issue[:60]}"
                if fc[0].button("Useful", key=f"up_{i}", use_container_width=True):
                    learn.rate_finding(f"{f.issue} {f.fix}", True)
                    st.session_state["rated"] = st.session_state.get("rated", 0) + 1
                if fc[1].button("Not useful", key=f"dn_{i}",
                                use_container_width=True):
                    learn.rate_finding(f"{f.issue} {f.fix}", False)
                    st.session_state["rated"] = st.session_state.get("rated", 0) + 1
                fc[2].caption("raised by " + ", ".join(f.agreed_by)
                              + f" · usefulness {f.specificity:.0%}")

        if rep.withheld:
            with st.expander(f"Withheld — quote not found in your text "
                             f"({len(rep.withheld)})"):
                st.caption(
                    "These are shown only so you can judge model quality. The "
                    "quote does not appear in your manuscript, so the finding "
                    "is probably about a paper the model imagined."
                )
                for f in rep.withheld:
                    st.markdown(f"- *{f.issue}* — claimed quote: “{f.quote[:120]}” "
                                f"({f.provider}/{f.reviewer})")

        n_rated, n_useful, trainable = learn.finding_status()
        if n_rated:
            st.caption(
                f"{n_rated} finding(s) rated ({n_useful} useful). "
                + ("Ready to train: `python train_local.py --findings`."
                   if trainable else
                   f"Rate {max(0, 40 - n_rated)} more to train a filter that "
                   "pushes vacuous criticism down the list."))

        st.download_button("Download panel report (.txt)",
                           agents.format_report(rep).encode("utf-8"),
                           file_name="ai_review_panel.txt", mime="text/plain")

# ---------------------------------------------------------------------------
# 9. Generate
# ---------------------------------------------------------------------------
with tabs[8]:
    st.subheader(f"Generate for {target.journal}")
    st.caption(
        "Two routes, and they work differently on purpose. **DOCX restyles your "
        "original file** — equations, figures and tables are never rebuilt, so "
        "they cannot be damaged. **LaTeX is built from the IR**, which is the "
        "only option there, and therefore carries the conversion risks listed "
        "in each project's BUILD.md."
    )

    st.markdown("### Word — use the publisher's own template")
    st.caption(
        "A profile can say *Times New Roman 10 pt, two columns*. It cannot "
        "reproduce a publisher's title block, author blocks, abstract run style, "
        "Roman section numbering, caption styles or theme fonts — those live in "
        "the template's `styles.xml` and `theme1.xml`. If you have the official "
        "template, transplanting it beats any set of rules."
    )

    tpl_file = st.file_uploader(
        "Journal template (.docx or .dotx)", type=["docx", "dotx"],
        key="tpl_upload",
        help="Download it from the journal's author pages. Content is never "
             "rebuilt — only styles and page setup are transplanted.",
    )

    if tpl_file is not None:
        tpl_dir = Path(tempfile.mkdtemp())
        tpl_path = tpl_dir / tpl_file.name
        tpl_path.write_bytes(tpl_file.getvalue())
        try:
            info = retypeset.inspect_template(tpl_path)
        except AttributeError:
            # Streamlit reloads the script on save but keeps already-imported
            # packages, so a retypeset updated mid-session looks like it is missing
            # functions that plainly exist on disk.
            st.error(
                f"Loaded retypeset is version {getattr(retypeset, '__version__', '?')} and "
                "does not have the template functions. Streamlit is holding an "
                "older copy of the package in memory — stop the app (Ctrl+C) and "
                "run `python -m streamlit run app.py` again."
            )
            info = None
        except Exception as exc:
            st.error(f"Could not read that template: {exc}")
            info = None

        if info is not None:
            ic = st.columns(4)
            ic[0].metric("Styles", len(info.style_names))
            ic[1].metric("Default font",
                         info.default_font or "—",
                         help=f"{info.default_size_pt or '?'} pt")
            ic[2].metric("Page", info.page_size or "—")
            ic[3].metric("Columns",
                         "/".join(str(c) for c in dict.fromkeys(info.columns)) or "—")
            if info.margins_mm:
                st.caption("Margins (mm): " + ", ".join(
                    f"{k} {v:g}" for k, v in info.margins_mm.items()))

            colA, colB, colC = st.columns(3)
            take_page = colA.checkbox("Take page size, margins and columns", True)
            map_paras = colB.checkbox("Map headings and captions to template styles", True)
            strip_t = colC.checkbox(
                "Remove the previous journal's furniture", True,
                key="strip_tpl",
                help="Logo, ISSN/DOI placeholders, running citation header, "
                     "licence footnote and leftover template instructions.")

            if st.button("Apply template", type="primary", use_container_width=True):
                out = Path(tempfile.mkdtemp()) / (
                    f"{Path(st.session_state['fname']).stem}_"
                    f"{Path(tpl_file.name).stem}.docx")
                with st.spinner("Transplanting styles…"):
                    res = retypeset.apply_template(
                        st.session_state["srcpath"], tpl_path, out, ms,
                        take_page_setup=take_page, map_paragraphs=map_paras,
                        strip_furniture=strip_t,
                    )
                st.session_state["tpl_out"] = str(res.path)
                st.session_state["tpl_res"] = (res.styles_merged,
                                               res.paragraphs_mapped,
                                               res.notes, res.unsupported)

    if st.session_state.get("tpl_out"):
        merged, mapped, notes, unsupported = st.session_state["tpl_res"]
        st.success(f"{merged} styles merged, {mapped} paragraphs mapped. "
                   "Equations, figures and tables untouched.")
        for x in notes:
            st.caption(f"· {x}")
        for x in unsupported:
            st.warning(x)
        st.download_button(
            "Download .docx (from your template)",
            Path(st.session_state["tpl_out"]).read_bytes(),
            file_name=Path(st.session_state["tpl_out"]).name,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            type="primary",
        )

    st.divider()
    st.markdown("### Or generate from the built-in profile")

    cc = st.columns(2)

    with cc[0]:
        st.markdown("#### Word (.docx)")
        st.markdown(
            f"Restyles to **{target.docx.body_font} {target.docx.body_size_pt:g} pt**, "
            f"line spacing {target.docx.line_spacing:g}, "
            f"{target.docx.columns} column(s), {target.docx.page_size.upper()}"
            + (", continuous line numbering" if target.docx.line_numbers else "")
            + "."
        )
        st.caption("Rule-based. Good for margins, fonts and spacing; it cannot "
                   "reproduce a publisher's title block. Prefer the template "
                   "route above when you have the official file.")
        strip_p = st.checkbox(
            "Remove the previous journal's furniture", True, key="strip_prof",
            help="Logo, ISSN/DOI placeholders, running citation header, licence "
                 "footnote and leftover template instructions.")
        if st.button("Restyle DOCX", type="primary", use_container_width=True):
            out = Path(tempfile.mkdtemp()) / f"{Path(st.session_state['fname']).stem}_{target.id}.docx"
            with st.spinner("Restyling…"):
                res = retypeset.render_docx(st.session_state["srcpath"], ms, target, out,
                                       strip_furniture=strip_p)
            st.session_state["docx_out"] = str(res.path)
            st.session_state["docx_res"] = (res.changed_paragraphs, res.notes,
                                            res.unsupported)

        if st.session_state.get("docx_out"):
            n, notes, unsupported = st.session_state["docx_res"]
            st.success(f"{n} paragraphs restyled. Native content untouched.")
            for x in notes:
                st.caption(f"· {x}")
            for x in unsupported:
                st.warning(x)
            st.download_button(
                "Download .docx",
                Path(st.session_state["docx_out"]).read_bytes(),
                file_name=Path(st.session_state["docx_out"]).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )

    with cc[1]:
        st.markdown("#### LaTeX (.tex)")
        st.markdown(
            f"Builds a project for `\\documentclass{{{target.latex.document_class}}}`, "
            "converting every figure to something pdfLaTeX can place."
        )
        if st.button("Build LaTeX project", type="primary", use_container_width=True):
            out = Path(tempfile.mkdtemp()) / "tex"
            with st.spinner("Converting figures, writing LaTeX…"):
                res = retypeset.render_latex(ms, target, media_dir, out)
            st.session_state["tex_out"] = str(res.out_dir)
            st.session_state["tex_res"] = (res.notes, res.failed_figures)

        if st.session_state.get("tex_out"):
            notes, failed = st.session_state["tex_res"]
            if failed:
                st.error(f"{len(set(failed))} figure(s) could not be converted: "
                         + ", ".join(sorted(set(failed))))
            else:
                st.success("All figures converted.")
            with st.expander(f"Conversion log ({len(notes)})"):
                for x in notes:
                    st.caption(f"· {x}")
            st.download_button(
                "Download LaTeX project (.zip)",
                zip_dir(Path(st.session_state["tex_out"])),
                file_name=f"{Path(st.session_state['fname']).stem}_{target.id}_latex.zip",
                mime="application/zip",
                use_container_width=True,
            )
            st.code("pdflatex main && pdflatex main", language="bash")

    st.divider()
    st.markdown(
        """
**What neither route does automatically**

- **Citation style conversion.** Your in-text markers are plain text, not
  reference-manager fields, so numeric ↔ author-year cannot be done reliably.
- **Section reordering.** Restyling never moves content; check the Compliance
  tab for order warnings and reorder in Word.
- **Equations Pandoc could not read.** Flagged in Fidelity as
  `degenerate_math` and marked in the LaTeX output with a black square.
        """
    )

# ---------------------------------------------------------------------------
# 10. Export
# ---------------------------------------------------------------------------
with tabs[9]:
    st.subheader("Export")
    st.caption("The IR is the handoff to the renderers. Exporting it now means "
               "the verification work done in this session is not thrown away "
               "when they land.")

    st.download_button(
        "Corrected IR (.json)",
        ms.model_dump_json(indent=2).encode("utf-8"),
        file_name=Path(st.session_state.get("fname", "manuscript")).stem + ".ir.json",
        mime="application/json",
        type="primary",
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manuscript.ir.json", ms.model_dump_json(indent=2))
        z.writestr("fidelity.json", json.dumps(report, indent=2, ensure_ascii=False))
        z.writestr("compliance.txt",
                   retypeset.format_compliance(retypeset.check(ms, target, media_dir)))
        for p in sorted(media_dir.glob("*")):
            if p.is_file():
                z.write(p, f"media/{p.name}")
    st.download_button("Everything (.zip)", buf.getvalue(),
                       file_name="retypeset_export.zip", mime="application/zip")

    st.divider()
    st.markdown(
        """
**Next milestones, in order**

1. **Section-role labelling** via a constrained model call — the one runtime
   use of an LLM in this design, restricted to choosing from the role list.
2. **Reference ingestion** with AnyStyle or GROBID, then Crossref lookup to
   fill missing DOIs.
3. **Renderers** — `python-docx` into the publisher's own template, and Jinja2
   into `elsarticle` / `IEEEtran` / `sn-jnl`.
4. **Asset pipeline** — EMF/WMF to PDF conversion and column-width resizing.
        """
    )
