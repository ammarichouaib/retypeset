"""Local training panel -- run the two trainable models from the main window.

Until now training lived only in `train_local.py`, which meant the loop was:
correct sections in the browser, remember that a CLI exists, find a terminal,
run it, restart the app. Most people did the first step and none of the rest, so
the corrections accumulated and nothing ever learned from them. The training
step belongs where the data is produced.

What is trainable is unchanged, and deliberately small:

* **heading detection** — is this paragraph a section heading?
* **role classification** — which canonical role does this heading play?
* **finding usefulness** — which model-panel criticisms were worth reading?

Parsing, restyling and LaTeX generation stay deterministic. A converter that
gives different output on two runs of the same file is not usable.

Everything runs on this machine. No data leaves it.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import streamlit as st

from retypeset import learn


def _log(fn, *args, **kwargs) -> tuple[object, str]:
    """Run a training function, capturing what it prints for display."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _bar(label: str, have: int, need: int) -> None:
    st.progress(min(1.0, have / need) if need else 1.0,
                text=f"{label}: {have}/{need}")


def render() -> None:
    st.subheader("Training")
    st.caption("Two decisions in this pipeline are genuinely ambiguous — whether "
               "a paragraph is a heading, and what role that heading plays. Your "
               "corrections are the only honest source of labels for them. "
               "Everything here runs locally; nothing is uploaded.")

    st.session_state.setdefault("train_log", "")
    stat = learn.status()

    if not stat.sklearn:
        st.error("scikit-learn and joblib are not installed, so nothing can be "
                 "trained. `pip install scikit-learn joblib`, then restart the app. "
                 "Without them retypeset falls back to the rule-based path with no "
                 "change in behaviour.")
        return

    c = st.columns(4)
    c[0].metric("Examples", stat.n_examples)
    c[1].metric("Headings", stat.n_headings)
    c[2].metric("Role-labelled", f"{stat.n_roles} / {stat.role_classes} class(es)")
    n_rated, n_useful, can_findings = learn.finding_status()
    c[3].metric("Findings rated", f"{n_rated} ({n_useful} useful)")

    st.divider()
    left, right = st.columns([2, 1], gap="large")

    with left:
        st.markdown("#### Heading detector + role classifier")
        _bar("examples", stat.n_examples, learn.MIN_HEADING_EXAMPLES)
        _bar("role-labelled headings", stat.n_roles, learn.MIN_ROLE_EXAMPLES)

        if stat.can_train_heading() or stat.can_train_role():
            st.success("Enough data to train "
                       + " and ".join(
                           x for x in [
                               "the heading detector" if stat.can_train_heading() else "",
                               "the role classifier" if stat.can_train_role() else "",
                           ] if x) + ".")
        else:
            st.info(
                f"Not enough data yet. The heading detector needs "
                f"{learn.MIN_HEADING_EXAMPLES} examples with at least 10 of each "
                f"class; the role classifier needs {learn.MIN_ROLE_EXAMPLES} "
                f"role-labelled headings across {learn.MIN_ROLE_CLASSES}+ roles. "
                "Every correction you make in **Verify → Sections** adds examples.")

        b1, b2 = st.columns(2)
        if b1.button("Train now", type="primary", use_container_width=True,
                     disabled=not (stat.can_train_heading() or stat.can_train_role())):
            with st.spinner("Fitting…"):
                try:
                    metrics, log = _log(learn.train)
                except RuntimeError as exc:
                    metrics, log = {}, f"error: {exc}"
            learn.reset_cache()
            st.cache_data.clear()
            st.session_state["train_log"] = log
            st.session_state["train_metrics"] = metrics
            st.rerun()

        if b2.button("Train the finding filter", use_container_width=True,
                     disabled=not can_findings,
                     help="Needs 40 rated findings with at least 8 of each verdict. "
                          "Rate them in Check → AI review."):
            with st.spinner("Fitting…"):
                try:
                    metrics, log = _log(learn.train_findings)
                except RuntimeError as exc:
                    metrics, log = {}, f"error: {exc}"
            learn.reset_cache()
            st.session_state["train_log"] = log
            st.session_state["train_metrics"] = metrics
            st.rerun()

        metrics = st.session_state.get("train_metrics")
        if metrics:
            cv = {k: v for k, v in metrics.items() if k.endswith("_cv")}
            if cv:
                st.markdown("**Cross-validated scores**")
                for k, v in cv.items():
                    st.write(f"`{k}` {v}")
                st.caption("Cross-validated on your own corrections, which come "
                           "from a handful of manuscripts in one field. Treat "
                           "these as a sanity check, not as generalisation.")
        if st.session_state.get("train_log"):
            st.code(st.session_state["train_log"] or "(no output)", language=None)

    with right:
        st.markdown("#### Models on disk")
        for label, path in (("heading", learn.HEADING_MODEL),
                            ("role", learn.ROLE_MODEL),
                            ("finding", learn.FINDING_MODEL)):
            if path.exists():
                kb = path.stat().st_size / 1024
                st.write(f"🟢 **{label}** — {kb:.0f} kB")
            else:
                st.write(f"⚪ **{label}** — not trained")

        st.markdown("#### Try it")
        probe = st.text_input("A line of text", "2.3 Experimental setup",
                              key="probe_text")
        if probe.strip():
            learn.reset_cache()
            h = learn.predict_heading(probe)
            r = learn.predict_role(probe)
            if h is None and r is None:
                st.caption("No trained model yet — the rule-based path decides "
                           "this line.")
            else:
                if h:
                    st.write(f"heading: **{h[0]}** ({h[1]:.0%})")
                if r:
                    st.write(f"role: **{r[0]}** ({r[1]:.0%})")

    st.divider()
    with st.expander("Get more training data", expanded=stat.n_examples < learn.MIN_HEADING_EXAMPLES):
        st.caption(
            "You do not have to annotate anything by hand. Two sources cost "
            "nothing: the built-in seed corpus of conventional section names, "
            "and any manuscripts on this machine whose authors applied Word "
            "heading styles — those files have already labelled their own "
            "headings.")

        c1, c2 = st.columns([1, 2])
        if c1.button("Add the seed corpus", use_container_width=True,
                     help="Several hundred conventional heading phrases with "
                          "their roles, plus body-text negatives. Not extracts "
                          "from anyone's paper."):
            try:
                import build_corpus
                n, log = _log(build_corpus.write_seed)
                st.success(f"{n} new example(s) added.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not write the seed corpus: {exc}")

        folder = c2.text_input(
            "…or harvest a folder of .docx files", "",
            placeholder=r"C:\Users\you\Documents\papers",
            help="Reads heading styles only. Nothing is uploaded, and no "
                 "manuscript text is stored beyond the individual lines used as "
                 "examples.")
        any_docx = st.checkbox(
            "Include files with no heading styles (lower quality)", False,
            help="Those labels come from the same heuristic the model is meant "
                 "to replace, so they teach it to repeat today's mistakes.")
        if folder.strip() and st.button("Harvest folder"):
            path = Path(folder.strip())
            if not path.exists():
                st.error(f"`{path}` not found.")
            else:
                try:
                    import build_corpus
                    with st.spinner("Reading .docx files…"):
                        r, _ = _log(build_corpus.harvest, path,
                                    require_styles=not any_docx)
                    st.success(f"{r['files']} usable file(s) · {r['headings']} "
                               f"heading(s) · {r['body']} body line(s) · "
                               f"{r['written']} new example(s)")
                    for skipped in r["skipped"][:10]:
                        st.caption(f"skipped: {skipped}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Harvest failed: {exc}")

    with st.expander("Training data"):
        st.caption(f"`{learn.DATA_FILE}` — plain JSON Lines, one labelled "
                   "paragraph per line. Inspect or edit it by hand; delete a line "
                   "and the next training run forgets it.")
        if learn.DATA_FILE.exists():
            raw = learn.DATA_FILE.read_text(encoding="utf-8")
            st.download_button("Download corrections.jsonl", raw.encode("utf-8"),
                               file_name="corrections.jsonl", mime="application/json")
            rows = [json.loads(x) for x in raw.splitlines() if x.strip()]
            st.dataframe(rows[-200:], use_container_width=True, height=260)
        else:
            st.caption("No corrections recorded yet.")

        st.markdown("**Reset**")
        st.caption("Deletes the trained models only. Your corrections are kept, "
                   "so training again reproduces them.")
        if st.button("Delete trained models"):
            removed = []
            for p in (learn.HEADING_MODEL, learn.ROLE_MODEL, learn.FINDING_MODEL):
                if p.exists():
                    Path(p).unlink()
                    removed.append(p.name)
            learn.reset_cache()
            st.success("Deleted: " + (", ".join(removed) or "nothing"))
            st.rerun()


def summary() -> None:
    """Compact sidebar version: how much data there is, and one button.

    Guided mode has no Training tab, and the corrections it records would
    otherwise sit unused until someone remembered the CLI. Four lines in the
    sidebar close that loop.
    """
    stat = learn.status()
    if not stat.sklearn:
        st.caption("Local training is off — `pip install scikit-learn joblib`.")
        return

    trainable = stat.can_train_heading() or stat.can_train_role()
    st.caption(f"{stat.n_examples} correction(s) recorded · "
               f"{stat.n_roles} role-labelled")
    if trainable:
        if st.button("Train local models", use_container_width=True,
                     key="sidebar_train"):
            with st.spinner("Fitting…"):
                try:
                    metrics, log = _log(learn.train)
                except RuntimeError as exc:
                    metrics, log = {}, f"error: {exc}"
            learn.reset_cache()
            st.session_state["train_log"] = log
            st.session_state["train_metrics"] = metrics
            st.success("Trained. The next parse uses the new models.")
    else:
        need = max(0, learn.MIN_HEADING_EXAMPLES - stat.n_examples)
        st.caption(f"{need} more example(s) before the heading detector can "
                   "train. Corrections in Verify → Sections add them."
                   if need else
                   "More classes needed before the role classifier can train.")
