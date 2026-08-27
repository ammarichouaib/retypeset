"""Step 4 -- produce the deliverable: restyled DOCX, LaTeX project, IR export.

Three routes, in the order they should be preferred:

1. **Your template** (step 1, or uploaded here). Styles and page setup are
   transplanted into your own file. Equations, figures and tables are never
   rebuilt, so they cannot be damaged. This is the only route that reproduces a
   publisher's title block, because that lives in `styles.xml`, not in any rule.
2. **The profile.** Rule-based restyling: fonts, sizes, spacing, columns,
   margins, line numbers. Good, and strictly weaker than (1).
3. **LaTeX.** Built from the IR, because there is no other way to build it, and
   therefore carrying every conversion risk the audit reported.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

import retypeset
from retypeset.profile import JournalProfile

from .common import media_dir, zip_dir

DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")


def _template_route(ms, target: JournalProfile) -> None:
    st.markdown("### Word — from the publisher's own template")

    existing = st.session_state.get("tpl_path")
    if existing and Path(existing).exists():
        st.success(f"Using the template from step 1: **{Path(existing).name}**")
        tpl_path = Path(existing)
        if st.checkbox("Use a different template file", key="tpl_replace"):
            tpl_path = None
    else:
        tpl_path = None

    if tpl_path is None:
        up = st.file_uploader("Journal template (.docx or .dotx)",
                              type=["docx", "dotx"], key="tpl_upload_gen")
        if up is None:
            st.caption("No template loaded. Upload one here, or use the "
                       "profile-based route below.")
            return
        d = Path(tempfile.mkdtemp(prefix="retypeset_tpl_"))
        tpl_path = d / up.name
        tpl_path.write_bytes(up.getvalue())
        st.session_state["tpl_path"] = str(tpl_path)

    try:
        info = retypeset.inspect_template(tpl_path)
    except Exception as exc:
        st.error(f"Could not read that template: {exc}")
        return

    ic = st.columns(4)
    ic[0].metric("Styles", len(info.style_names))
    ic[1].metric("Default font", info.default_font or "—",
                 help=f"{info.default_size_pt or '?'} pt")
    ic[2].metric("Page", info.page_size or "—")
    ic[3].metric("Columns",
                 "/".join(str(c) for c in dict.fromkeys(info.columns)) or "—")
    if info.margins_mm:
        st.caption("Margins (mm): " + ", ".join(f"{k} {v:g}"
                                                for k, v in info.margins_mm.items()))

    colA, colB, colC = st.columns(3)
    take_page = colA.checkbox("Take page size, margins and columns", True)
    map_paras = colB.checkbox("Map headings and captions to template styles", True)
    strip_t = colC.checkbox("Remove the previous journal's furniture", True,
                            key="strip_tpl",
                            help="Logo, ISSN/DOI placeholders, running citation "
                                 "header, licence footnote and leftover template "
                                 "instructions.")

    if st.button("Apply template", type="primary", use_container_width=True):
        out = Path(tempfile.mkdtemp()) / (
            f"{Path(st.session_state['fname']).stem}_{tpl_path.stem}.docx")
        with st.spinner("Transplanting styles…"):
            try:
                res = retypeset.apply_template(
                    st.session_state["srcpath"], tpl_path, out, ms,
                    take_page_setup=take_page, map_paragraphs=map_paras,
                    strip_furniture=strip_t)
            except Exception as exc:
                st.error(f"Template application failed: {exc}")
                return
        st.session_state["tpl_out"] = str(res.path)
        st.session_state["tpl_res"] = (res.styles_merged, res.paragraphs_mapped,
                                       res.notes, res.unsupported)

    if st.session_state.get("tpl_out"):
        merged, mapped, notes, unsupported = st.session_state["tpl_res"]
        st.success(f"{merged} styles merged, {mapped} paragraphs mapped. "
                   "Equations, figures and tables untouched.")
        for x in notes:
            st.caption(f"· {x}")
        for x in unsupported:
            st.warning(x)
        st.download_button("⬇ Download .docx (from your template)",
                           Path(st.session_state["tpl_out"]).read_bytes(),
                           file_name=Path(st.session_state["tpl_out"]).name,
                           mime=DOCX_MIME, type="primary")


def _profile_route(ms, target: JournalProfile) -> None:
    cc = st.columns(2)

    with cc[0]:
        st.markdown("#### Word (.docx) — from the profile")
        st.markdown(
            f"Restyles to **{target.docx.body_font} {target.docx.body_size_pt:g} pt**, "
            f"line spacing {target.docx.line_spacing:g}, {target.docx.columns} "
            f"column(s), {target.docx.page_size.upper()}"
            + (", continuous line numbering" if target.docx.line_numbers else "") + ".")
        strip_p = st.checkbox("Remove the previous journal's furniture", True,
                              key="strip_prof")
        if st.button("Restyle DOCX", type="primary", use_container_width=True):
            out = (Path(tempfile.mkdtemp())
                   / f"{Path(st.session_state['fname']).stem}_{target.id}.docx")
            with st.spinner("Restyling…"):
                res = retypeset.render_docx(st.session_state["srcpath"], ms, target,
                                            out, strip_furniture=strip_p)
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
            st.download_button("⬇ Download .docx",
                               Path(st.session_state["docx_out"]).read_bytes(),
                               file_name=Path(st.session_state["docx_out"]).name,
                               mime=DOCX_MIME, use_container_width=True)

    with cc[1]:
        st.markdown("#### LaTeX (.tex)")
        st.markdown(f"Builds a project for "
                    f"`\\documentclass{{{target.latex.document_class}}}`, converting "
                    "every figure to something pdfLaTeX can place.")
        if st.button("Build LaTeX project", type="primary", use_container_width=True):
            out = Path(tempfile.mkdtemp()) / "tex"
            with st.spinner("Converting figures, writing LaTeX…"):
                res = retypeset.render_latex(ms, target, media_dir(), out)
            st.session_state["tex_out"] = str(res.out_dir)
            st.session_state["tex_res"] = (
                res.notes, res.failed_figures,
                {"sections": res.sections, "figures": res.figures,
                 "tables": res.tables, "equations": res.equations,
                 "body_words": res.body_words, "empty_body": res.empty_body},
            )

        if st.session_state.get("tex_out"):
            notes, failed, stats = st.session_state["tex_res"]

            # What reached the page, counted from the emitted file rather than
            # from the IR. A LaTeX project that compiles perfectly and contains
            # no body is the one failure this tool must never present as a
            # success, and it is invisible until someone opens the PDF.
            if stats.get("empty_body"):
                st.error(
                    "**The generated document has no body.** It will compile, "
                    "and it will contain the title, abstract and references "
                    "only. This happens when every section is nested under one "
                    "whose role is front matter — `title`, `abstract`, "
                    "`keywords` or `references`. Open **Verify → Sections** and "
                    "give the outer section a body role (or `unknown`), then "
                    "generate again.")
            else:
                m = st.columns(4)
                m[0].metric("Sections", stats.get("sections", 0))
                m[1].metric("Figures", stats.get("figures", 0))
                m[2].metric("Tables", stats.get("tables", 0))
                m[3].metric("Equations", stats.get("equations", 0))
                st.caption(f"{stats.get('body_words', 0)} words of body text "
                           "reached the document. `BUILD.md` in the zip "
                           "compares these against the manuscript, row by row.")
            if failed:
                st.error(f"{len(set(failed))} figure(s) could not be converted: "
                         + ", ".join(sorted(set(failed))))
            else:
                st.success("All figures converted.")
            with st.expander(f"Conversion log ({len(notes)})"):
                for x in notes:
                    st.caption(f"· {x}")
            st.download_button(
                "⬇ Download LaTeX project (.zip)",
                zip_dir(Path(st.session_state["tex_out"])),
                file_name=f"{Path(st.session_state['fname']).stem}_{target.id}_latex.zip",
                mime="application/zip", use_container_width=True)
            st.code("pdflatex main && pdflatex main", language="bash")


def export(ms, target: JournalProfile) -> None:
    st.markdown("### Export the verified parse")
    st.caption("The IR is the handoff to the renderers. Exporting it means the "
               "verification work done in this session is not thrown away.")

    st.download_button("Corrected IR (.json)",
                       ms.model_dump_json(indent=2).encode("utf-8"),
                       file_name=Path(st.session_state.get("fname", "manuscript")
                                      ).stem + ".ir.json",
                       mime="application/json")

    if st.button("Build the everything bundle (.zip)"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manuscript.ir.json", ms.model_dump_json(indent=2))
            z.writestr("fidelity.json", json.dumps(st.session_state["audit"],
                                                   indent=2, ensure_ascii=False))
            z.writestr("compliance.txt", retypeset.format_compliance(
                retypeset.check(ms, target, media_dir())))
            z.writestr("profile.json", target.model_dump_json(indent=2))
            for p in sorted(media_dir().glob("*")):
                if p.is_file():
                    z.write(p, f"media/{p.name}")
        st.session_state["bundle"] = buf.getvalue()

    if st.session_state.get("bundle"):
        st.download_button("⬇ Everything (.zip)", st.session_state["bundle"],
                           file_name="retypeset_export.zip", mime="application/zip")


def render(ms, target: JournalProfile) -> None:
    st.subheader(f"Generate for {target.journal}")
    _template_route(ms, target)
    st.divider()
    st.markdown("### Or generate from the profile")
    _profile_route(ms, target)
    st.divider()
    export(ms, target)
    st.divider()
    st.markdown(
        """
**What no route does automatically**

- **Citation style conversion.** In-text markers are plain text, not
  reference-manager fields, so numeric ↔ author-year cannot be done reliably.
- **Section reordering.** Restyling never moves content; the Compliance panel
  reports order problems and you reorder in Word.
- **Equations Pandoc could not read.** Flagged in Fidelity as `degenerate_math`
  and marked in the LaTeX output with a black square.
        """)
