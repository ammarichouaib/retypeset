"""Step 3 -- checking: journal compliance, submission readiness, model peer review.

The three panels are ordered by how much you should trust them. Compliance is
deterministic and traceable to a profile field. Readiness is a set of measurable
observations about the text. The model panel is the only part that involves a
language model at runtime, and every finding it shows must quote your manuscript
verbatim or it is withheld.
"""

from __future__ import annotations

from dataclasses import replace

import streamlit as st

import retypeset
from retypeset import agents, learn, review
from retypeset.profile import JournalProfile

from .common import SEV_ICON, media_dir


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

def compliance(ms, target: JournalProfile):
    result = retypeset.check(ms, target, media_dir())

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

    st.download_button("Download compliance report (.txt)",
                       retypeset.format_compliance(result).encode("utf-8"),
                       file_name=f"compliance_{target.id}.txt", mime="text/plain")
    return result


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def readiness(ms, target: JournalProfile) -> None:
    rep = review.analyse(ms, target)
    band, reasons = rep.desk_rejection_risk()

    st.subheader(f"What this manuscript needs for {target.journal}")

    cc = st.columns([1, 1, 2])
    cc[0].metric("Readiness", f"{rep.readiness:.0%}",
                 help="How complete and submittable the manuscript is. Not a "
                      "probability of acceptance.")
    cc[1].metric("Desk-rejection risk", band,
                 help="Desk rejection is mostly mechanical, so its drivers can be "
                      "named. How often each triggers a rejection varies by editor.")
    with cc[2]:
        st.markdown("**Why**")
        for x in reasons[:4]:
            st.caption(f"· {x}")

    with st.expander("Why there is no “chance of acceptance” percentage"):
        st.markdown(
            """
- **Acceptance turns on novelty and correctness**, judged by two or three
  people. No surface feature of a manuscript predicts that.
- **There is no training data.** Rejected manuscripts are not public, so the
  outcome variable cannot be observed at all — there is nothing to fit.
- **Base rates swing from ~8 % to ~60 %** between journals and move year to year.
- **The harm is asymmetric.** “62 % chance of acceptance” reads as knowledge.

Everything on this page traces to a specific observation about your text. A
percentage would not, so it is not shown.
            """)

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
        with st.expander(f"{cat.name} — {cat.score:.0%}", expanded=cat.score < 0.7):
            for c in cat.checks:
                icon = {"blocker": "🔴", "major": "🟠", "minor": "🟡",
                        "ok": "🟢"}.get(c.severity, "⚪")
                st.markdown(f"{icon} **{c.label}** · {c.evidence}")
                if c.advice:
                    st.caption(c.advice)

    st.download_button("Download readiness report (.txt)",
                       review.format_report(rep).encode("utf-8"),
                       file_name=f"readiness_{target.id}.txt", mime="text/plain")

    if not target.scope_keywords:
        st.info("This journal profile has no `scope_keywords`, so topic fit was "
                "skipped. Add them from the journal's aims-and-scope page to "
                "enable the single most useful check here.")


# ---------------------------------------------------------------------------
# Model peer-review panel
# ---------------------------------------------------------------------------

def ai_review(ms, target: JournalProfile) -> None:
    st.subheader("Model peer-review panel")
    st.caption("Several models, each given a different referee brief, then ranked "
               "by what they independently agree on. Every finding must quote your "
               "manuscript verbatim; quotes that cannot be located are withheld.")

    st.warning(
        "**This sends your manuscript text to a third party.** For work under "
        "review that may not be acceptable to you, your co-authors or your "
        "institution. `Ollama · local` runs on your own machine and sends nothing "
        "anywhere — use it for confidential drafts.", icon="⚠️")

    presets = agents.PRESETS
    chosen_ids = st.multiselect(
        "Referees (models)", list(presets),
        default=[k for k in ("groq-llama70b", "gemini-flash") if k in presets],
        format_func=lambda k: presets[k].label)

    briefs = st.multiselect(
        "Review angles", list(agents.REVIEWERS),
        default=["methods", "novelty", "clarity"],
        format_func=lambda k: agents.REVIEWERS[k]["label"],
        help="Each angle is a separate call per model, so cost and time scale "
             "with models × angles.")

    with st.expander("API keys and setup"):
        st.markdown(
            """
Keys are read from environment variables, or from `.streamlit/secrets.toml`:

```toml
GROQ_API_KEY = "gsk_..."
GEMINI_API_KEY = "AIza..."
OPENROUTER_API_KEY = "sk-or-..."
```

Free tiers, as of writing: **Groq**, **Google AI Studio** (Gemini Flash),
**OpenRouter** (`:free` variants). **Ollama** needs no key — `ollama serve` and
`ollama pull llama3.1:8b`. Never commit keys.
            """)
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
            val = st.text_input(f"{p.label} — {p.api_key_env}", type="password",
                                placeholder="found in environment / secrets" if have
                                else "paste key", key=f"key_{pid}")
            if val:
                keys[pid] = val
            elif have:
                try:
                    keys[pid] = st.secrets.get(p.api_key_env, "") or p.api_key()
                except Exception:
                    keys[pid] = p.api_key()

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

        if p.kind == "ollama":
            try:
                installed = agents.ollama_models(p.base_url)
            except Exception:
                installed = []
            if installed:
                general = [m for m in installed if not agents.is_coder_model(m)]
                pick = st.selectbox("installed models", installed,
                                    index=installed.index(model) if model in installed
                                    else 0, key=f"ollama_pick_{pid}")
                if pick != model:
                    prov = replace(prov, model=pick)
                    model = pick
                if agents.is_coder_model(model):
                    st.warning(
                        f"`{model}` is tuned for code. Its critique of prose is "
                        "noticeably weaker than a general model of the same size."
                        + (f" You have {', '.join(general[:3])} installed."
                           if general else ""))
                ctx_tokens = st.number_input(
                    "num_ctx (tokens)", 2048, 131072, int(prov.num_ctx), 2048,
                    key=f"numctx_{pid}",
                    help="Ollama defaults to 2048 and silently discards anything "
                         "longer. Roughly 4 characters per token.")
                prov = replace(prov, num_ctx=int(ctx_tokens))
                ready[-1] = prov
            else:
                st.error(f"No Ollama server answered on `{p.base_url}`. Start it "
                         "with `ollama serve`.")

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
    budget = c2.number_input("Context (chars)", 8000, 200_000, 60_000, 4000)
    has_local = any(p.kind == "ollama" for p in ready)
    n_local = sum(1 for p in ready if p.kind == "ollama") * len(briefs)
    timeout = c3.number_input("Timeout (s)", 30, 3600, 900 if has_local else 120, 30)
    if has_local:
        st.info(f"**Local model: {n_local} call(s) run one after another.** Most of "
                "the wait is *reading* the prompt, so context length dominates. "
                "Start at 15 000 characters and raise it once you have seen the "
                "timing.")

    if st.button("Run review panel", type="primary", disabled=n_calls == 0,
                 use_container_width=True):
        with st.spinner(f"Running {n_calls} referee call(s)…"):
            st.session_state["panel"] = agents.review_manuscript(
                ms, target, ready, briefs, api_keys=keys, max_findings=6,
                max_chars=int(budget), timeout=int(timeout))

    rep = st.session_state.get("panel")
    if not rep:
        return

    ok = sum(1 for r in rep.runs if not r.error)
    m = st.columns(4)
    m[0].metric("Agents responded", f"{ok}/{len(rep.runs)}")
    m[1].metric("Grounded findings", len(rep.findings))
    m[2].metric("Withheld", len(rep.withheld),
                help="Quote could not be found in your manuscript.")
    m[3].metric("Agreed by ≥2", sum(1 for f in rep.findings if f.agreement > 1))

    if len({r.provider for r in rep.runs if not r.error}) < 2:
        st.caption("Only one model ran, so agreement here means the *same* model "
                   "raised a point under two briefs — much weaker evidence than "
                   "two independent models converging.")

    g = rep.groundedness()
    if g:
        st.caption("Quote-verification rate per model: "
                   + " · ".join(f"**{k}** {v:.0%}" for k, v in g.items()))
    for e in rep.errors:
        st.error(e)

    if not rep.findings:
        st.info("No grounded findings. If several were withheld, the models are "
                "paraphrasing rather than quoting — try a stronger model.")

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
            if fc[0].button("Useful", key=f"up_{i}", use_container_width=True):
                learn.rate_finding(f"{f.issue} {f.fix}", True)
            if fc[1].button("Not useful", key=f"dn_{i}", use_container_width=True):
                learn.rate_finding(f"{f.issue} {f.fix}", False)
            fc[2].caption("raised by " + ", ".join(f.agreed_by)
                          + f" · usefulness {f.specificity:.0%}")

    if rep.withheld:
        with st.expander(f"Withheld — quote not found in your text ({len(rep.withheld)})"):
            for f in rep.withheld:
                st.markdown(f"- *{f.issue}* — claimed quote: “{f.quote[:120]}” "
                            f"({f.provider}/{f.reviewer})")

    n_rated, n_useful, trainable = learn.finding_status()
    if n_rated:
        st.caption(f"{n_rated} finding(s) rated ({n_useful} useful). "
                   + ("Train the filter in **Advanced → Training**." if trainable
                      else f"Rate {max(0, 40 - n_rated)} more to train a filter "
                           "that pushes vacuous criticism down the list."))

    st.download_button("Download panel report (.txt)",
                       agents.format_report(rep).encode("utf-8"),
                       file_name="ai_review_panel.txt", mime="text/plain")


# ---------------------------------------------------------------------------
# Wizard step
# ---------------------------------------------------------------------------

def render(ms, target: JournalProfile) -> None:
    st.subheader(f"Check against {target.journal}")
    tabs = st.tabs([f"Compliance · {target.publisher}", "Readiness",
                    "AI review (optional)"])
    with tabs[0]:
        compliance(ms, target)
    with tabs[1]:
        readiness(ms, target)
    with tabs[2]:
        ai_review(ms, target)
