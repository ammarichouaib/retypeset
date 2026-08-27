"""Shared state, caching and small widgets for the retypeset UI.

State model
-----------
Streamlit re-runs the whole script on every interaction, so anything that must
survive a click lives in `st.session_state`. The manuscript is kept as its JSON
serialisation rather than as a live object: a pydantic model held across reruns
can outlive a module reload and become an instance of a class that no longer
exists, which surfaces as a validation error on an object that looks correct.

Keys used, all in one place so they can be reset coherently:

    ir_json   serialised Manuscript          fname     original file name
    audit     fidelity report dict           srcpath   path to the uploaded .docx
    media     extracted media directory      target_id selected profile id
    derived   a profile derived from an uploaded template (JSON)
    step      wizard position (1-4)
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

import retypeset
from retypeset.ir import Manuscript
from retypeset.profile import JournalProfile, load_profiles

SEV_ICON = {"fail": "🔴", "warn": "🟠", "info": "🔵", "pass": "🟢"}
ISSUE_ICON = {"error": "🔴", "warning": "🟠", "info": "🔵"}
STEPS = ["1 · Start", "2 · Verify", "3 · Check", "4 · Generate"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def parse_upload(data: bytes, name: str) -> tuple[str, dict, str, str]:
    """Parse uploaded bytes. Returns (ir_json, audit_report, media_dir, src).

    Cached on the file bytes, so editing front matter or reassigning a section
    never re-runs Pandoc.
    """
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


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def manuscript() -> Manuscript | None:
    raw = st.session_state.get("ir_json")
    return Manuscript.model_validate_json(raw) if raw else None


def store(ms: Manuscript) -> None:
    st.session_state["ir_json"] = ms.model_dump_json()


def has_manuscript() -> bool:
    return bool(st.session_state.get("ir_json"))


def media_dir() -> Path:
    return Path(st.session_state.get("media", "."))


def reset_manuscript() -> None:
    for k in ("ir_json", "audit", "media", "fname", "srcpath", "assignments",
              "sec_step", "panel", "docx_out", "docx_res", "tex_out", "tex_res",
              "tpl_out", "tpl_res"):
        st.session_state.pop(k, None)


def goto(step: int) -> None:
    st.session_state["step"] = max(1, min(4, step))


# ---------------------------------------------------------------------------
# Target journal
# ---------------------------------------------------------------------------

def derived_profile() -> JournalProfile | None:
    raw = st.session_state.get("derived")
    return JournalProfile.model_validate_json(raw) if raw else None


def all_targets() -> dict[str, JournalProfile]:
    """Built-in profiles plus, if one was derived from an upload, that one first."""
    out: dict[str, JournalProfile] = {}
    d = derived_profile()
    if d:
        out[d.id] = d
    out.update(load_profiles())
    return out


def target() -> JournalProfile | None:
    targets = all_targets()
    tid = st.session_state.get("target_id")
    if tid in targets:
        return targets[tid]
    return None


def template_path() -> Path | None:
    p = st.session_state.get("tpl_path")
    return Path(p) if p and Path(p).exists() else None


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

def target_banner(t: JournalProfile) -> None:
    bits = [f"**{t.label}**", f"`{t.template_family}`",
            f"{t.references.style} references",
            f"{t.docx.body_font} {t.docx.body_size_pt:g} pt",
            f"{t.docx.columns} column(s)"]
    st.caption(" · ".join(bits))
    if not t.verified:
        st.caption("⚠️ Unverified profile — every rule is reported as a warning, "
                   "never as a failure.")


def nav(step: int, *, back_label: str = "← Back", next_label: str = "Next →",
        can_next: bool = True, next_help: str = "") -> None:
    """Wizard footer. Kept identical on every step so the buttons never move."""
    st.divider()
    c1, c2, c3 = st.columns([1, 4, 1])
    if step > 1 and c1.button(back_label, use_container_width=True, key=f"back_{step}"):
        goto(step - 1)
        st.rerun()
    if step < 4 and c3.button(next_label, type="primary", use_container_width=True,
                              disabled=not can_next, help=next_help or None,
                              key=f"next_{step}"):
        goto(step + 1)
        st.rerun()
