"""Step 2 -- verification panels: fidelity, front matter, sections, figures, references.

Every panel here answers one question: *did the parser understand this part of
your manuscript?* Nothing downstream is trustworthy until they do, which is why
verification is a step of its own rather than an optional tab.
"""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st

from retypeset import learn, sectioning
from retypeset.ir import SectionRole
from retypeset.profile import JournalProfile

from .common import ISSUE_ICON, media_dir, store


# ---------------------------------------------------------------------------
# Fidelity
# ---------------------------------------------------------------------------

def fidelity(ms, report: dict) -> None:
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
# Front matter
# ---------------------------------------------------------------------------

def front_matter(ms, target: JournalProfile) -> None:
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
# Sections
# ---------------------------------------------------------------------------

def _guided(ms, target: JournalProfile) -> None:
    rows = sectioning.flatten(ms)

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
    st.caption("Confirmed so far: " + (", ".join(assignments) or "nothing yet"))

    st.markdown(
        f"#### Select the **{role.replace('_', ' ')}**\n"
        "Drag the handles to cover exactly the paragraphs that belong to this "
        "section. Do **not** include its heading — that is picked up automatically."
    )

    prior = assignments.get(role)
    suggested = ((prior.start, prior.end) if prior
                 else sectioning.suggest_range(rows, role))
    if suggested:
        default = suggested
    else:
        # Never fall back to the top of the document: that silently offers the
        # title block as if it were the abstract, and one careless Confirm
        # assigns the wrong text. Sections run in order, so start after the last
        # confirmed one.
        after = max((a.end for a in assignments.values()), default=-1) + 1
        after = min(after, max(0, len(rows) - 1))
        default = (after, min(after + 2, len(rows) - 1))

    lo, hi = st.slider("Paragraph range", 0, max(0, len(rows) - 1),
                       value=(int(default[0]), int(default[1])), key=f"range_{role}")
    if suggested:
        st.caption(f"Suggested from the current parse: {suggested[0]}–{suggested[1]}")
    else:
        st.warning("Nothing in the manuscript was detected as this section. Set "
                   "the range yourself, or press **Skip** if it is absent.")

    window_lo, window_hi = max(0, lo - 6), min(len(rows), hi + 8)
    html = ["<div style='max-height:420px;overflow-y:auto;border:1px solid "
            "rgba(128,128,128,.35);border-radius:8px;padding:12px;font-size:0.9rem;"
            "line-height:1.5'>"]
    for i in range(window_lo, window_hi):
        r = rows[i]
        inside = lo <= i <= hi
        body = html_escape(r.text[:400]) or "<em>(empty)</em>"
        if r.is_heading:
            body = f"<strong>{body}</strong>"
        style = ("background:rgba(255,196,0,.28);border-left:3px solid #f2a900;"
                 if inside else "border-left:3px solid transparent;")
        html.append(f"<div style='{style}padding:3px 8px;margin:1px 0'>"
                    f"<span style='opacity:.45;font-family:monospace'>{i:>3}</span> "
                    f"{body}</div>")
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)

    selected_words = sum(len(rows[i].text.split())
                         for i in range(lo, min(hi + 1, len(rows))))
    st.caption(f"Selection: {hi - lo + 1} block(s), ~{selected_words} words")

    if role == "abstract" and target.structure.abstract_max_words:
        limit = target.structure.abstract_max_words
        if selected_words > limit:
            st.warning(f"{selected_words} words — {target.journal} allows {limit}. "
                       "Either the selection is too wide or the abstract needs cutting.")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("← Back", disabled=step == 0, use_container_width=True,
                 key="sec_back"):
        st.session_state["sec_step"] = step - 1
        st.rerun()
    if c2.button("Skip", use_container_width=True, key="sec_skip",
                 help="This section is not in the manuscript."):
        assignments.pop(role, None)
        st.session_state["sec_step"] = step + 1
        st.rerun()
    if c3.button(f"Confirm {role} →", type="primary", use_container_width=True,
                 key="sec_confirm"):
        assignments[role] = sectioning.Assignment(role=role, start=lo, end=hi)
        st.session_state["sec_step"] = step + 1
        st.rerun()
    if c4.button("Apply all", use_container_width=True, disabled=not assignments,
                 key="sec_apply", help="Rebuild the section tree from everything "
                                       "confirmed."):
        rows = sectioning.flatten(ms)
        examples = sectioning.range_training_examples(rows, list(assignments.values()))
        sectioning.apply_ranges(ms, rows, list(assignments.values()))
        store(ms)
        if examples:
            try:
                st.session_state["last_taught"] = learn.append_examples(examples)
            except Exception as exc:
                st.warning(f"Could not record training examples: {exc}")
        st.success(f"Applied {len(assignments)} section(s).")
        st.rerun()

    if step >= len(wanted) - 1 and assignments:
        st.info("That is the last section. Press **Apply all** to rebuild.")
    if st.session_state.get("last_taught"):
        st.caption(f"{st.session_state['last_taught']} example(s) recorded for "
                   "training · train them in **Advanced → Training**")

    st.divider()
    st.markdown("**Current tree**")
    for sec in ms.body:
        st.write(f"L{sec.level} · **{sec.title_raw or '(untitled)'}** → "
                 f"`{sec.role.value}` · {len(sec.blocks)} block(s)")


def _table(ms) -> None:
    role_values = [r.value for r in SectionRole]
    st.markdown("Every block of the manuscript is listed in order. Tick "
                "**heading** on the lines that actually start a section, set its "
                "level and role, then save. The tree is rebuilt from your marks.")
    rows = sectioning.flatten(ms)
    table = sectioning.to_table(rows)

    f1, f2 = st.columns([3, 1])
    needle = f1.text_input("Filter text", "", key="sec_filter")
    only_head = f2.checkbox("Headings only", False, key="sec_only_head")
    view = [r for r in table
            if (not needle or needle.lower() in r["text"].lower())
            and (not only_head or r["heading"])]
    st.caption(f"{len(view)} of {len(table)} blocks shown")

    edited = st.data_editor(
        view, key="sec_editor", use_container_width=True, height=460,
        hide_index=True,
        column_config={
            "#": st.column_config.NumberColumn("#", disabled=True, width="small"),
            "heading": st.column_config.CheckboxColumn("heading", width="small"),
            "level": st.column_config.NumberColumn("lvl", min_value=1, max_value=6,
                                                   step=1, width="small"),
            "role": st.column_config.SelectboxColumn("role", options=role_values,
                                                     width="medium"),
            "kind": st.column_config.TextColumn("kind", disabled=True, width="small"),
            "text": st.column_config.TextColumn("text", disabled=True, width="large"),
        },
    )

    if st.button("Save section marks", type="primary", use_container_width=True,
                 key="sec_save_table"):
        rows = sectioning.from_table(rows, edited)
        sectioning.rebuild(ms, rows)
        store(ms)
        examples = sectioning.training_examples(rows)
        if examples:
            try:
                st.session_state["last_taught"] = learn.append_examples(examples)
            except Exception as exc:
                st.warning(f"Could not record training examples: {exc}")
        st.success(f"Rebuilt {len(ms.body)} top-level section(s).")
        st.rerun()

    st.divider()
    st.markdown("**Resulting tree**")
    for sec in ms.body:
        st.write(f"{'　' * (sec.level - 1)}L{sec.level} · "
                 f"**{sec.title_raw or '(untitled preamble)'}** → `{sec.role.value}` "
                 f"· {len(sec.blocks)} block(s)")


def _quick(ms) -> None:
    role_values = [r.value for r in SectionRole]
    changed = False
    for sec in ms.body:
        cc = st.columns([5, 3, 2])
        label = sec.title_raw or "(untitled preamble)"
        cc[0].write(f"**{label[:70]}**")
        cc[0].caption(f"{len(sec.blocks)} block(s) · level {sec.level}")
        new = cc[1].selectbox("role", role_values,
                              index=role_values.index(sec.role.value),
                              key=f"role_{sec.id}", label_visibility="collapsed")
        cc[2].caption(f"{sec.role_provenance.method} · "
                      f"{sec.role_provenance.confidence:.0%}")
        if new != sec.role.value:
            sec.role = SectionRole(new)
            sec.role_provenance.method = "explicit"
            sec.role_provenance.confidence = 1.0
            changed = True

    if changed:
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
        st.info(f"{len(unknown)} section(s) unlabelled. Use the guided picker if "
                "the headings were never styled as headings in Word.")


def sections(ms, target: JournalProfile) -> None:
    st.subheader("Sections")
    st.caption("Roles drive everything downstream: which limits apply, where the "
               "renderer places content, what the journal requires.")

    mode = st.radio(
        "How do you want to set sections?",
        ["Quick — assign roles to detected sections",
         "Guided — pick each section from the text, one at a time",
         "Table — edit every block at once (advanced)"],
        key="sec_mode",
        help="Start with Quick. If the author applied no heading styles, "
             "detection is guesswork and the guided picker is the reliable fix.",
    )
    if mode.startswith("Guided"):
        _guided(ms, target)
    elif mode.startswith("Table"):
        _table(ms)
    else:
        _quick(ms)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def figures(ms, target: JournalProfile) -> None:
    st.subheader("Figures")
    f = target.figures
    media = media_dir()
    need = f.min_px_single_column or f.required_px(f.single_column_mm, "halftone")
    st.caption(f"{target.publisher} needs **{need} px** width for "
               f"{f.dpi_halftone} dpi at {f.single_column_mm:g} mm single-column, "
               f"and **{f.required_px(f.double_column_mm, 'halftone')} px** at "
               f"{f.double_column_mm:g} mm full width. Resolution is always "
               "measured at final printed size.")

    if not ms.figures:
        st.info("No figures found in the manuscript.")
        return

    for fig in ms.figures:
        cc = st.columns([1, 3])
        path = media / fig.files[0] if fig.files else None
        if path and path.exists() and path.suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".gif"):
            cc[0].image(str(path), width=150)
        else:
            cc[0].info(f"`{fig.fmt or '?'}`\nnot previewable")

        ok_fmt = fig.fmt.lower() not in [x.lower() for x in f.rejected_formats]
        if fig.is_vector:
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
            f"`{', '.join(fig.files)}`")
        if fig.is_vector and fig.fmt.lower() in ("emf", "wmf"):
            cc[1].caption(
                "EMF/WMF is a Windows metafile: no browser can preview it and "
                "pdfLaTeX cannot place it. It is vector, so quality is fine — "
                "re-export as PDF, or the LaTeX route will convert it if "
                "Inkscape or LibreOffice is installed.")
        st.divider()


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def references(ms) -> None:
    st.subheader("References")
    st.caption("Stored as CSL-JSON where parsing succeeded. Once every entry is "
               "CSL-JSON, restyling between journals is citeproc plus a CSL file. "
               "The verbatim source string is always kept as a fallback.")

    if not ms.references:
        st.info("No reference list was detected. If the manuscript has one, mark "
                "its section as `references` in the Sections panel.")
        return

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
# Wizard step
# ---------------------------------------------------------------------------

def render(ms, report: dict, target: JournalProfile) -> None:
    st.subheader("Verify the parse")
    st.caption("Correct anything the parser guessed. Everything after this point "
               "inherits what you confirm here.")

    tabs = st.tabs(["Fidelity", "Front matter", "Sections", "Figures", "References"])
    with tabs[0]:
        fidelity(ms, report)
    with tabs[1]:
        front_matter(ms, target)
    with tabs[2]:
        sections(ms, target)
    with tabs[3]:
        figures(ms, target)
    with tabs[4]:
        references(ms)
