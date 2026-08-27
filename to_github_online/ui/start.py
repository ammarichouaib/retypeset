"""Step 1 -- one screen: the manuscript, and where it is going.

Design note
-----------
The previous console made a built-in journal profile mandatory before anything
could happen, and hid the "upload the publisher's own template" option at the
bottom of the last tab. That is backwards: the template is what most authors
actually have, and it is strictly more informative than a profile, because it
also carries the styles a rule set cannot express. Here it is the first thing
offered, and choosing it derives a usable profile automatically -- with every
inferred value shown next to the evidence for it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

import retypeset
from retypeset import template_profile
from retypeset.profile import load_profiles

from . import common


def _target_from_template() -> None:
    """Upload a publisher template, derive a profile, let the author correct it."""
    up = st.file_uploader(
        "Journal template (.docx or .dotx)", type=["docx", "dotx"],
        key="tpl_upload_start",
        help="Download it from the journal's author pages. retypeset reads the page "
             "setup and styles from it, and mines any author instructions the "
             "template itself contains.",
    )
    if up is None:
        prev = common.derived_profile()
        if prev:
            st.success(f"Using the template-derived profile **{prev.journal}**. "
                       "Upload another file to replace it.")
        else:
            st.info("No template yet. You can also start from a built-in profile "
                    "and attach the template later.")
        return

    # Persist the bytes: the applier needs the file again in step 4, and
    # Streamlit's UploadedFile does not survive a rerun.
    tpl_dir = Path(tempfile.mkdtemp(prefix="retypeset_tpl_"))
    tpl_path = tpl_dir / up.name
    tpl_path.write_bytes(up.getvalue())
    st.session_state["tpl_path"] = str(tpl_path)

    profiles = load_profiles()
    fam_ids = ["(none — read only what the template proves)"] + sorted(
        profiles, key=lambda k: profiles[k].label)
    seed_choice = st.selectbox(
        "Seed from a publisher baseline (optional)", fam_ids,
        format_func=lambda k: k if k.startswith("(") else profiles[k].label,
        help="If you know the publisher, start from its generic profile and let "
             "the template override what it actually proves. Without a seed, "
             "anything the template does not state stays unset — and an unset "
             "limit produces no findings at all.",
    )
    base = None if seed_choice.startswith("(") else profiles[seed_choice]

    try:
        derived = template_profile.derive(tpl_path, base=base)
    except Exception as exc:
        st.error(f"Could not read that template: {exc}")
        return

    st.markdown("**What was read from your template**")
    read_ev = [e for e in derived.evidence if e.startswith("read")]
    mined_ev = [e for e in derived.evidence if e.startswith("mined")]

    c = st.columns(4)
    c[0].metric("Styles", len(derived.info.style_names))
    c[1].metric("Page", derived.info.page_size or "—")
    c[2].metric("Columns", derived.profile.docx.columns)
    c[3].metric("Body", f"{derived.profile.docx.body_font.split()[0] or '—'} "
                        f"{derived.profile.docx.body_size_pt:g}pt")

    for e in read_ev:
        st.caption("· " + e.split("·", 1)[-1].strip())

    if mined_ev:
        with st.expander(f"Limits found in the template's own instructions "
                         f"({len(mined_ev)})", expanded=True):
            st.caption("These come from sentences in the template, not from the "
                       "publisher's website. Check each one before trusting it.")
            for e in mined_ev:
                st.markdown("· " + e.split("·", 1)[-1].strip())
    elif derived.text_chars:
        st.caption(f"No author instructions matched in {derived.text_chars} "
                   "characters of template text — structural limits stay unset, "
                   "so no structural rule will fire. Seed from a publisher "
                   "baseline above, or set them in Advanced → Profile.")

    c1, c2 = st.columns(2)
    journal = c1.text_input("Journal name", derived.profile.journal,
                            key="derived_journal")
    publisher = c2.text_input("Publisher", derived.profile.publisher,
                              key="derived_publisher")
    prof = derived.profile.model_copy(update={
        "journal": journal.strip() or derived.profile.journal,
        "publisher": publisher.strip() or derived.profile.publisher,
    })
    st.session_state["derived"] = prof.model_dump_json()
    st.session_state["target_id"] = prof.id

    if st.checkbox("Save this as a reusable profile in `profiles/`",
                   key="save_derived",
                   help="Writes a normal profile JSON file. It then appears in "
                        "the built-in list on every future run, for you and for "
                        "anyone you share the folder with."):
        if st.button("Save profile", key="do_save_derived"):
            try:
                path = template_profile.save(prof, overwrite=True)
                load_profiles.cache_clear()
                st.success(f"Written to `{path}`. Review it and set "
                           "`verified: true` once you have checked the numbers "
                           "against the journal's guide for authors.")
            except Exception as exc:
                st.error(f"Could not save: {exc}")


def _target_from_builtin() -> None:
    profiles = load_profiles()
    if not profiles:
        st.error("No journal profiles found in `profiles/`.")
        return

    pubs = sorted({p.publisher for p in profiles.values()})
    c1, c2 = st.columns([1, 2])
    pub = c1.selectbox("Publisher", ["All"] + pubs, key="pub_filter")
    pool = {k: v for k, v in profiles.items()
            if pub == "All" or v.publisher == pub}
    ids = sorted(pool, key=lambda k: (pool[k].publisher, pool[k].journal))
    current = st.session_state.get("target_id")
    idx = ids.index(current) if current in ids else 0
    tid = c2.selectbox("Target journal", ids, index=idx,
                       format_func=lambda k: pool[k].label, key="builtin_target")
    st.session_state["target_id"] = tid
    t = pool[tid]

    common.target_banner(t)
    with st.expander("What this profile will check"):
        s = t.structure
        rows = [
            ("Abstract", f"≤ {s.abstract_max_words} words" if s.abstract_max_words
             else "no stated limit"),
            ("Keywords", f"{s.keywords_min or '–'}–{s.keywords_max or '–'}"),
            ("Highlights", f"{s.highlights_min}–{s.highlights_max} × "
                           f"{s.highlights_max_chars} chars"
             if s.highlights_required else "not required"),
            ("Required sections", ", ".join(r.value for r in s.required_sections)
             or "none declared"),
            ("Figures", f"{t.figures.dpi_halftone} dpi at "
                        f"{t.figures.single_column_mm:g} mm single column"),
            ("References", t.references.style),
            ("LaTeX class", t.latex.document_class),
        ]
        for k, v in rows:
            st.write(f"**{k}** — {v}")
        if t.guide_url:
            st.write(f"[Guide for authors]({t.guide_url})")
        if t.notes:
            st.caption(t.notes)

    if st.session_state.get("tpl_path"):
        st.caption(f"A template is also loaded (`{Path(st.session_state['tpl_path']).name}`) "
                   "and will be offered in step 4 for the Word output.")


def render() -> None:
    st.subheader("Start")
    st.caption("Two things: the manuscript, and where it is going. "
               "Everything else follows from these.")

    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("#### 1 · Manuscript")
        up = st.file_uploader("Your manuscript (.docx)", type=["docx"],
                              key="ms_upload")
        if up is not None:
            st.session_state["pending_upload"] = (up.getvalue(), up.name)
            st.caption(f"`{up.name}` · {len(up.getvalue()) / 1e6:.1f} MB")
        elif common.has_manuscript():
            st.success(f"Loaded: **{st.session_state.get('fname')}**")
            if st.button("Use a different manuscript"):
                common.reset_manuscript()
                st.session_state.pop("pending_upload", None)
                st.rerun()

    with right:
        st.markdown("#### 2 · Target")
        mode = st.radio(
            "How do you want to specify the journal?",
            ["My own template (.docx/.dotx)", "A built-in journal profile"],
            key="target_mode", horizontal=False,
            help="The template route is the better one when you have the file: "
                 "it carries the publisher's real styles, which no rule set can "
                 "reproduce.",
        )
        if mode.startswith("My own"):
            _target_from_template()
        else:
            st.session_state.pop("derived", None)
            _target_from_builtin()

    st.divider()
    pending = st.session_state.get("pending_upload")
    ready = bool(pending or common.has_manuscript()) and common.target() is not None

    c1, c2 = st.columns([3, 1])
    c1.caption(
        "Parsing runs Pandoc for prose, mathematics and tables, then a second "
        "independent pass over the raw OOXML for figures and text boxes, and "
        "compares the two. A hundred-equation manuscript takes a few seconds."
        if ready else
        "Add a manuscript and choose a target to continue."
    )
    if c2.button("Parse and continue →", type="primary", disabled=not ready,
                 use_container_width=True):
        if pending:
            data, name = pending
            with st.spinner("Reading OOXML, converting equations…"):
                try:
                    ir_json, report, media, srcpath = common.parse_upload(data, name)
                except retypeset.PandocError as exc:
                    st.error(str(exc))
                    st.stop()
                except Exception as exc:
                    st.error(f"Parsing failed: {exc}")
                    st.stop()
            st.session_state.update(ir_json=ir_json, audit=report, media=media,
                                    fname=name, srcpath=srcpath)
            st.session_state.pop("pending_upload", None)
        common.goto(2)
        st.rerun()
