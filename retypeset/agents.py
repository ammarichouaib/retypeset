"""
retypeset.agents -- multi-model peer review of a manuscript.

The idea is the same one that makes real peer review work: send the paper to
several referees with different priorities, then pay most attention to what they
independently agree on. Here the referees are language models, optionally from
different providers, each given a different brief.

The hard problem, and what this module does about it
---------------------------------------------------
A model asked to critique a paper will confidently describe things the paper
does not contain. It will object to a missing control experiment that is in
Section 4, or praise a dataset that does not exist. Unverified, that output is
worse than useless: it sends the author chasing corrections for problems they do
not have.

So every finding must carry a **verbatim quote** from the manuscript, and every
quote is checked against the source text before the finding is shown. Findings
whose quote cannot be located are withheld and counted. The resulting
*groundedness rate* is reported per model, which also happens to be the most
useful thing to know when choosing between free providers.

Two further guards:

* **Agreement is ranked, not averaged.** A point raised by three referees
  independently is reported above one raised once, because independent
  agreement is the only cheap evidence of reliability available.
* **Nothing is auto-applied.** Output is advice attached to a quote, never an
  edit to the manuscript.

Privacy
-------
This sends manuscript text to a third party. For work under review that may be
unacceptable to you, your co-authors or your institution, so it is opt-in, never
runs automatically, and the UI says so. `ollama` runs models on your own machine
and sends nothing anywhere; it is the right choice for confidential drafts.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .ir import Manuscript, SectionRole
from .profile import JournalProfile

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass
class Provider:
    """An endpoint that can be asked for a completion.

    `kind` is the wire format, not the vendor: almost every hosted provider
    speaks the OpenAI chat-completions shape, so one implementation covers Groq,
    OpenRouter, Together, DeepSeek and Moonshot. Gemini and Ollama have their
    own shapes.

    Ollama gets a native branch rather than using its OpenAI-compatible
    endpoint, because that endpoint gives no way to set `num_ctx`. Ollama
    defaults to a 2048-token window and **silently truncates** anything longer,
    so a 60 000-character manuscript arrives as its first few pages -- while
    still taking long enough to time out. The referee then reviews a fragment
    and reports what the discarded remainder already contained.
    """

    id: str
    label: str
    kind: str                     # "openai" | "gemini" | "ollama"
    base_url: str
    model: str
    api_key_env: str = ""
    free_tier: bool = True
    notes: str = ""
    num_ctx: int = 16384          # ollama only; tokens, not characters

    def api_key(self, override: str = "") -> str:
        return override or os.environ.get(self.api_key_env, "")

    def available(self, override: str = "") -> bool:
        return bool(self.api_key(override)) or not self.api_key_env


# Model identifiers rot. Providers retire models on weeks' notice and the error
# ("no longer available to new users") arrives as a 404, which is easy to
# misread as a broken URL. Every model below is editable in the UI, and
# `list_models()` asks the provider what your key can actually call.
PRESETS: dict[str, Provider] = {
    "groq-llama70b": Provider(
        "groq-llama70b", "Groq · Llama 3.3 70B", "openai",
        "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
        "GROQ_API_KEY", True,
        "Fast and generous. Note Groq sits behind Cloudflare and may refuse "
        "requests from cloud hosts regardless of your key."),
    "groq-qwen32b": Provider(
        "groq-qwen32b", "Groq · Qwen 3 32B", "openai",
        "https://api.groq.com/openai/v1", "qwen/qwen3-32b",
        "GROQ_API_KEY", True,
        "Same key as above; a genuinely different model family."),
    "gemini-flash": Provider(
        "gemini-flash", "Google · Gemini Flash", "gemini",
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.6-flash", "GEMINI_API_KEY", True,
        "Large context, good at long methods sections. Keys from AI Studio "
        "start with 'AIza'; a key in another format is probably for a "
        "different Google product and will not work here."),
    "openrouter": Provider(
        "openrouter", "OpenRouter", "openai",
        "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct",
        "OPENROUTER_API_KEY", False,
        "One key, many models. The ':free' suffixes come and go — if you get a "
        "404 the error names the slug to use instead."),
    "deepseek": Provider(
        "deepseek", "DeepSeek · chat", "openai",
        "https://api.deepseek.com/v1", "deepseek-chat",
        "DEEPSEEK_API_KEY", False,
        "Strong at technical critique for the price."),
    "moonshot": Provider(
        "moonshot", "Moonshot / Kimi", "openai",
        "https://api.moonshot.cn/v1", "moonshot-v1-32k",
        "KIMI_API_KEY", False,
        "Long-context Chinese provider; use api.moonshot.ai for the "
        "international endpoint."),
    "ollama-local": Provider(
        "ollama-local", "Ollama · local (private)", "ollama",
        "http://localhost:11434", "llama3.1:8b", "", True,
        "Runs on your machine and sends nothing anywhere — the only safe option "
        "for confidential drafts. Use a general model (llama3.1:8b, llama3); "
        "*-coder models are tuned for code and review prose poorly. Expect "
        "minutes per call on CPU: lower the context and raise the timeout."),
}


def ollama_models(base_url: str = "http://localhost:11434",
                  timeout: int = 10) -> list[str]:
    """Models already pulled on this machine, via Ollama's native /api/tags."""
    data = _request(base_url.rstrip("/") + "/api/tags", None, {}, timeout, "GET")
    return sorted(m.get("name", "") for m in (data.get("models") or [])
                  if m.get("name"))


# Local models tuned for code. They will answer a manuscript-review prompt, but
# their critique of prose is markedly worse than a general model of the same
# size, and it is not obvious from the output that this is why.
_CODER_HINT = re.compile(r"coder|code|starcoder|codellama|deepseek-coder", re.I)


def is_coder_model(name: str) -> bool:
    return bool(_CODER_HINT.search(name))


def list_models(provider: Provider, api_key: str = "",
                timeout: int = 30) -> list[str]:
    """Ask the provider which models this key may call.

    Worth its own button: a wrong model id and a wrong key both surface as a
    4xx, and this separates them in one call.
    """
    key = provider.api_key(api_key)
    if provider.kind == "ollama":
        return ollama_models(provider.base_url, timeout)

    if provider.kind == "gemini":
        data = _request(f"{provider.base_url.rstrip('/')}/models?key={key}",
                        None, {}, timeout, "GET")
        out = []
        for m in data.get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append(m.get("name", "").replace("models/", ""))
        return sorted(out)

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _request(provider.base_url.rstrip("/") + "/models", None, headers,
                    timeout, "GET")
    items = data.get("data") if isinstance(data, dict) else None
    return sorted(str(m.get("id", "")) for m in (items or []) if m.get("id"))


def test_connection(provider: Provider, api_key: str = "",
                    timeout: int = 45) -> tuple[bool, str]:
    """One tiny call. Returns (ok, message) and never raises."""
    try:
        out = complete(provider, "Reply with JSON only.",
                       'Return exactly {"ok": true}',
                       api_key=api_key, timeout=timeout)
        return True, f"responded ({len(out)} chars)"
    except ProviderError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class ProviderError(RuntimeError):
    pass


# Cloudflare, which fronts several of these APIs, blocks the default
# `Python-urllib/3.x` User-Agent outright and returns HTTP 403 with Cloudflare
# error 1010 ("banned based on your browser's signature"). The error mentions
# neither the User-Agent nor the header, so it reads like an authentication
# failure and sends you looking at your API key. Sending an ordinary UA fixes it.
_UA = "retypeset/0.7 (+https://github.com/) python-urllib"


def _explain(code: int, detail: str, url: str) -> str:
    d = detail.lower()
    if code == 403 and "1010" in detail:
        return ("HTTP 403 (Cloudflare 1010): the endpoint rejected the client "
                "signature. retypeset now sends a normal User-Agent, so if you still "
                "see this, the request is being blocked by network location — "
                "common when calling Groq from a cloud host. Try running locally.")
    if code == 404 and ("no longer available" in d or "not found" in d
                        or "unavailable" in d):
        return (f"HTTP 404: the model was rejected. {detail[:200]} "
                "Use 'List models' to see what your key can actually call.")
    if code in (401, 403):
        return (f"HTTP {code}: the key was rejected. Check it is for this "
                f"provider and has not expired. {detail[:160]}")
    if code == 429:
        return (f"HTTP 429: rate limited. Free tiers throttle hard — wait, or "
                f"use fewer models × angles. {detail[:120]}")
    return f"HTTP {code}: {detail[:300]}"


def _request(url: str, payload: dict | None, headers: dict,
             timeout: int = 120, method: str = "POST") -> dict:
    headers = {"User-Agent": _UA, "Accept": "application/json", **headers}
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:600]
        raise ProviderError(_explain(exc.code, detail, url)) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "refused" in reason.lower() and "localhost" in url:
            raise ProviderError(
                "nothing is listening on localhost:11434. Ollama must be running "
                "on the SAME machine as this app — if the app is deployed on "
                "Streamlit Cloud, 'localhost' is the server, not your computer, "
                "and a local model can never work there. Run retypeset locally for "
                "Ollama."
            ) from exc
        raise ProviderError(f"could not reach {url}: {reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(
            f"timed out after {timeout}s. Local models on CPU are slow: reduce "
            "the context size, pick a smaller model, or raise the timeout."
        ) from exc


def _post(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    return _request(url, payload, headers, timeout, "POST")


def complete(provider: Provider, system: str, user: str,
             api_key: str = "", timeout: int = 120,
             temperature: float = 0.2) -> str:
    """One completion. Raises ProviderError; never returns partial nonsense."""
    key = provider.api_key(api_key)
    if provider.api_key_env and not key:
        raise ProviderError(f"no API key: set {provider.api_key_env}")

    if provider.kind == "openai":
        url = provider.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = _post(url, {
            "model": provider.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }, headers, timeout)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from exc

    if provider.kind == "ollama":
        url = provider.base_url.rstrip("/") + "/api/chat"
        data = _post(url, {
            "model": provider.model,
            "stream": False,
            "format": "json",
            "options": {
                # Without this Ollama uses 2048 tokens and drops the rest of the
                # prompt without saying so.
                "num_ctx": provider.num_ctx,
                "temperature": temperature,
            },
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }, {"Content-Type": "application/json"}, timeout)
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from exc

    if provider.kind == "gemini":
        url = (f"{provider.base_url.rstrip('/')}/models/{provider.model}"
               f":generateContent?key={key}")
        data = _post(url, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature,
                                 "responseMimeType": "application/json"},
        }, {"Content-Type": "application/json"}, timeout)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from exc

    raise ProviderError(f"unknown provider kind {provider.kind!r}")


# ---------------------------------------------------------------------------
# Reviewer briefs
# ---------------------------------------------------------------------------

_COMMON = """You are reviewing a manuscript submitted to {journal} ({publisher}).

Return JSON only, matching exactly:
{{"findings": [
  {{"issue": "<what is wrong, one sentence>",
    "why": "<why it matters to a reviewer or editor, one sentence>",
    "fix": "<a specific, actionable change>",
    "quote": "<VERBATIM text copied from the manuscript, 8-30 words>",
    "section": "<section name>",
    "severity": "major" | "minor"}}
]}}

RULES, in order of importance:
1. `quote` MUST be copied character-for-character from the text supplied below.
   Do not paraphrase, correct, translate or shorten it. A finding whose quote
   cannot be found verbatim in the manuscript is discarded automatically, so an
   invented quote wastes the finding entirely.
2. Only comment on what is present in the text you were given. You are shown
   excerpts, not the whole paper. If something appears missing, it may simply
   not be in the excerpt - say so rather than asserting its absence.
3. No praise, no summary, no preamble. Findings only.
4. At most {limit} findings. Fewer good ones beat many weak ones.
5. Be concrete. "Improve the discussion" is useless; "the 18% improvement in
   Section 4 is never compared against the persistence baseline" is useful.
"""

REVIEWERS: dict[str, dict[str, str]] = {
    "methods": {
        "label": "Methodology referee",
        "focus": "Experimental design, validity, and whether the conclusions "
                 "follow from the evidence.",
        "brief": "You are a demanding methodological referee. Look for: "
                 "unsupported causal claims; missing baselines or controls; "
                 "validation on the data used for fitting; sample sizes and "
                 "uncertainty never stated; parameters chosen without "
                 "justification; conclusions broader than the evidence.",
    },
    "novelty": {
        "label": "Novelty and positioning referee",
        "focus": "Contribution, framing, and relation to prior work.",
        "brief": "You are an editor deciding whether this paper says something "
                 "new. Look for: a contribution that is never stated in one "
                 "sentence; claims of novelty with no comparison to prior work; "
                 "related work that is listed rather than contrasted; results "
                 "that restate what the field already knows; a title or abstract "
                 "that undersells or oversells the finding.",
    },
    "clarity": {
        "label": "Presentation referee",
        "focus": "Structure, clarity and figures.",
        "brief": "You are reviewing for readability. Look for: undefined "
                 "symbols and acronyms; figures never referenced or discussed in "
                 "the text; results presented without interpretation; sentences "
                 "so long the claim is lost; inconsistent terminology for the "
                 "same concept; sections that do not do what their heading says.",
    },
    "reproducibility": {
        "label": "Reproducibility referee",
        "focus": "Whether an independent group could repeat this work.",
        "brief": "You are checking whether the work could be reproduced from "
                 "the text alone. Look for: software and versions unnamed; data "
                 "provenance unstated; parameters given in prose but never "
                 "tabulated; preprocessing steps skipped; hardware or runtime "
                 "unstated where it matters; no statement of data or code "
                 "availability.",
    },
}


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def build_context(ms: Manuscript, max_chars: int = 60_000) -> str:
    """A faithful excerpt of the manuscript for the referees.

    60 kB (~15k tokens) fits comfortably in the free tiers worth using -- Groq
    serves 128k context and Gemini Flash far more -- and covers most engineering
    manuscripts whole. Abridgement only kicks in above that.

    The default was originally 14 kB, which reduced a 10 500-word paper to 11 %
    of itself. Referees given a fraction of a paper and told it is the paper
    will object to things the excluded 89 % already addresses, which is both
    useless and the fastest way to lose the author's trust.
    """
    parts: list[str] = [
        f"TITLE: {ms.meta.title}",
        f"KEYWORDS: {', '.join(ms.meta.keywords)}",
        "",
        "ABSTRACT:",
        ms.meta.abstract_raw or "(none)",
        "",
    ]
    used = sum(len(p) for p in parts)

    body = [s for s in ms.body
            if s.role not in (SectionRole.ABSTRACT, SectionRole.KEYWORDS,
                              SectionRole.REFERENCES, SectionRole.HIGHLIGHTS)]
    budget = max(1000, max_chars - used)

    def section_text(sec) -> str:
        """All prose in a section, including its subsections.

        Iterating only `sec.blocks` silently drops every nested subsection. On a
        10 500-word manuscript that produced a 5.7 kB excerpt -- the referees
        would have been reviewing a fraction of the paper while being told it
        was the paper.
        """
        parts = [b.paragraph.plain_text() for b in sec.blocks if b.paragraph]
        for child in sec.children:
            if child.title_raw:
                parts.append(f"[{child.title_raw}]")
            parts.append(section_text(child))
        return " ".join(p for p in parts if p).strip()

    pairs = [(sec, section_text(sec)) for sec in body]
    pairs = [(sec, t) for sec, t in pairs if t]
    total = sum(len(t) for _, t in pairs)

    # Two passes. Dividing the budget by the section count regardless of the
    # total abridges a manuscript that would have fitted whole: it cut a paper
    # to 44 % of itself while using half the available budget. Abridge only when
    # the whole genuinely does not fit, and then in proportion to length.
    if total <= budget:
        allowance = {id(sec): len(t) for sec, t in pairs}
    else:
        allowance = {id(sec): max(600, int(budget * len(t) / total))
                     for sec, t in pairs}

    for sec, text in pairs:
        title = sec.title_raw or f"({sec.role.value})"
        per_section = allowance[id(sec)]
        if len(text) > per_section:
            # Keep the opening and the closing: the claim is usually in one or
            # the other, and a mid-sentence cut is what produces fake quotes.
            head = text[: int(per_section * 0.6)].rsplit(". ", 1)[0] + "."
            tail = text[-int(per_section * 0.4):]
            tail = tail.split(". ", 1)[-1] if ". " in tail else tail
            text = f"{head}\n[... section abridged ...]\n{tail}"
        parts += [f"SECTION: {title}", text, ""]

    parts.append(f"FIGURES: {len(ms.figures)}, TABLES: {len(ms.tables)}, "
                 f"REFERENCES: {len(ms.references)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    issue: str
    why: str = ""
    fix: str = ""
    quote: str = ""
    section: str = ""
    severity: str = "minor"
    reviewer: str = ""
    provider: str = ""
    grounded: bool = False
    agreement: int = 1
    agreed_by: list[str] = field(default_factory=list)
    specificity: float = 0.0


@dataclass
class AgentRun:
    provider: str
    reviewer: str
    findings: list[Finding] = field(default_factory=list)
    error: str = ""
    raw: str = ""
    seconds: float = 0.0

    @property
    def grounded_rate(self) -> float:
        if not self.findings:
            return 0.0
        return sum(1 for f in self.findings if f.grounded) / len(self.findings)


@dataclass
class PanelReport:
    runs: list[AgentRun]
    findings: list[Finding]            # grounded only, consensus-ranked
    withheld: list[Finding]            # ungrounded, kept for transparency

    @property
    def errors(self) -> list[str]:
        return [f"{r.provider}/{r.reviewer}: {r.error}" for r in self.runs if r.error]

    def groundedness(self) -> dict[str, float]:
        """Share of findings per provider whose quote was verifiable."""
        by: dict[str, list[float]] = {}
        for r in self.runs:
            if r.findings:
                by.setdefault(r.provider, []).append(r.grounded_rate)
        return {k: sum(v) / len(v) for k, v in by.items()}


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quote(quote: str, haystack_norm: str, threshold: float = 0.92) -> bool:
    """Is this quote really in the manuscript?

    Exact match after whitespace and punctuation normalisation, then a fuzzy
    fallback for models that "helpfully" fix a typo while quoting. The fallback
    is deliberately tight: loosening it lets paraphrase through, and paraphrase
    is exactly what we are trying to catch.
    """
    q = _norm(quote)
    if len(q) < 20:
        return False                       # too short to be evidence of anything
    if q in haystack_norm:
        return True

    window = len(q)
    step = max(1, window // 4)
    best = 0.0
    for i in range(0, max(1, len(haystack_norm) - window + 1), step):
        seg = haystack_norm[i:i + window]
        r = difflib.SequenceMatcher(None, q, seg).quick_ratio()
        if r < threshold - 0.08:
            continue
        r = difflib.SequenceMatcher(None, q, seg).ratio()
        best = max(best, r)
        if best >= threshold:
            return True
    return False


def locate_section(quote: str, context: str) -> str:
    """Which section does this quote actually sit in?

    The model is asked for a section name and is unreliable about it: on a real
    run it filed four findings under "PUBLICATION FEE" -- the last heading it
    had seen -- including one whose quote came from the abstract. Since the
    quote has already been verified against the context, its position is known,
    so the section can be read off rather than believed.

    Returns "" when the quote cannot be placed, which the caller treats as
    "unknown" rather than inventing one.
    """
    q = _norm(quote)
    if len(q) < 20:
        return ""

    # Walk the context, tracking the current SECTION: header, and stop at the
    # block that contains the quote.
    current = ""
    hay = ""
    for line in context.splitlines():
        if line.startswith("SECTION: "):
            if q in _norm(hay):
                return current
            current = line[len("SECTION: "):].strip()
            hay = ""
        elif line.startswith(("TITLE:", "KEYWORDS:")):
            if q in _norm(hay):
                return current
            current = line.split(":", 1)[0].title()
            hay = line
        elif line.strip() == "ABSTRACT:":
            if q in _norm(hay):
                return current
            current, hay = "Abstract", ""
        else:
            hay += "\n" + line
    return current if q in _norm(hay) else ""


def _parse_findings(raw: str) -> list[Finding]:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Finding] = []
    for item in (data.get("findings") or [])[:20]:
        if not isinstance(item, dict) or not item.get("issue"):
            continue
        sev = str(item.get("severity", "minor")).lower()
        out.append(Finding(
            issue=str(item.get("issue", ""))[:400],
            why=str(item.get("why", ""))[:400],
            fix=str(item.get("fix", ""))[:400],
            quote=str(item.get("quote", ""))[:400],
            section=str(item.get("section", ""))[:120],
            severity="major" if sev.startswith("maj") else "minor",
        ))
    return out


# Phrasings that look like criticism but carry no information. A referee who
# writes "the discussion could be improved" has told the author nothing; a model
# produces these in bulk because they are safe. Measured on a real panel, five
# of seventeen findings were of this kind.
_VACUOUS = [
    re.compile(p, re.I) for p in [
        r"\b(could|should|might) be (improved|enhanced|strengthened|expanded|better)\b",
        r"\bmore (details?|information|discussion|explanation)\b(?!.{0,40}\babout the\b)",
        r"\bdoes not discuss the (potential )?(limitations|implications|applications)\b",
        r"\badd (a )?(clear|concise|brief) (statement|discussion|explanation)\b$",
        r"\bis not clearly (stated|explained|described)\b$",
        r"\blacks depth\b|\bis too general\b|\bneeds work\b",
    ]
]

_SPECIFIC = [
    re.compile(p, re.I) for p in [
        r"\b\d+(\.\d+)?\s*(%|percent|dpi|px|mm|kw|mwh|words?|references?)\b",
        r"\b(fig(ure)?|table|eq(uation)?|section)\s*\.?\s*\d+",
        r"\bcompared (to|with)\b", r"\bbaseline\b", r"\bcontrol\b",
        r"\bversus\b", r"\bwhereas\b",
        r"\bis (incorrect|wrong|misdefined|inconsistent)\b",
        r"\bshould be\b.{0,60}\bnot\b",
    ]
]


def specificity(f: Finding) -> float:
    """How actionable is this finding, from its own wording?

    Not a quality judgement of the underlying point -- it cannot be -- but a
    usable proxy for whether the author is told something they can act on. Used
    only to order findings that agreement has already tied.
    """
    text = f"{f.issue} {f.fix}"
    score = 0.5
    score += 0.15 * sum(1 for rx in _SPECIFIC if rx.search(text))
    score -= 0.25 * sum(1 for rx in _VACUOUS if rx.search(text))
    if len(f.fix.split()) >= 12:
        score += 0.1
    if f.quote and len(f.quote.split()) >= 8:
        score += 0.1
    return max(0.0, min(1.0, score))


def _cluster(findings: list[Finding], threshold: float = 0.55) -> list[Finding]:
    """Merge findings that say the same thing, counting distinct reviewers.

    Agreement between *different agents* is the signal. Two findings from the
    same agent are not corroboration, so `agreement` counts unique
    provider/reviewer pairs rather than raw duplicates.
    """
    clusters: list[list[Finding]] = []
    for f in findings:
        key = _norm(f.issue + " " + f.fix)
        placed = False
        for c in clusters:
            ref = _norm(c[0].issue + " " + c[0].fix)
            if difflib.SequenceMatcher(None, key, ref).ratio() >= threshold:
                c.append(f)
                placed = True
                break
        if not placed:
            clusters.append([f])

    merged: list[Finding] = []
    for c in clusters:
        # Keep the longest-reasoned version as the representative.
        rep = max(c, key=lambda x: len(x.why) + len(x.fix))
        agents = sorted({f"{x.provider}/{x.reviewer}" for x in c})
        rep.agreement = len(agents)
        rep.agreed_by = agents
        if any(x.severity == "major" for x in c):
            rep.severity = "major"
        merged.append(rep)

    order = {"major": 0, "minor": 1}
    # Agreement first, then severity, then how actionable the wording is. A
    # locally trained usefulness model, if the user has rated findings, overrides
    # the wording heuristic.
    for f in merged:
        f.specificity = round(_usefulness(f), 3)
    merged.sort(key=lambda f: (-f.agreement, order.get(f.severity, 1),
                               -f.specificity))
    return merged


def _usefulness(f: Finding) -> float:
    try:
        from . import learn  # noqa: PLC0415

        pred = learn.predict_finding(f"{f.issue} {f.fix}")
        if pred is not None:
            return pred
    except Exception:
        pass
    return specificity(f)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

def review_manuscript(
    ms: Manuscript,
    profile: JournalProfile,
    providers: list[Provider],
    reviewers: list[str] | None = None,
    *,
    api_keys: dict[str, str] | None = None,
    max_findings: int = 6,
    complete_fn: Callable[..., str] | None = None,
    max_workers: int = 4,
    max_chars: int = 60_000,
    timeout: int = 120,
) -> PanelReport:
    """Run the panel. `complete_fn` is injectable so tests need no network."""
    reviewers = reviewers or ["methods", "novelty", "clarity"]
    api_keys = api_keys or {}
    call = complete_fn or complete

    context = build_context(ms, max_chars=max_chars)
    haystack = _norm(context)

    jobs: list[tuple[Provider, str]] = [
        (p, r) for p in providers for r in reviewers if r in REVIEWERS
    ]
    if not jobs:
        return PanelReport([], [], [])

    system_common = _COMMON.format(journal=profile.journal,
                                   publisher=profile.publisher,
                                   limit=max_findings)

    def run_one(provider: Provider, reviewer: str) -> AgentRun:
        spec = REVIEWERS[reviewer]
        system = f"{spec['brief']}\n\n{system_common}"
        user = ("Review the following manuscript excerpts.\n\n"
                "=== MANUSCRIPT ===\n" + context + "\n=== END ===")
        run = AgentRun(provider=provider.id, reviewer=reviewer)
        started = time.monotonic()
        try:
            raw = call(provider, system, user,
                       api_key=api_keys.get(provider.id, ""), timeout=timeout)
            run.raw = raw[:4000]
            for f in _parse_findings(raw):
                f.reviewer, f.provider = reviewer, provider.id
                f.grounded = verify_quote(f.quote, haystack)
                if f.grounded:
                    # Trust the located position over the model's claim.
                    located = locate_section(f.quote, context)
                    if located:
                        f.section = located
                run.findings.append(f)
        except ProviderError as exc:
            run.error = str(exc)
        except Exception as exc:                       # never kill the panel
            run.error = f"{type(exc).__name__}: {exc}"
        run.seconds = round(time.monotonic() - started, 1)
        return run

    # Local models must run ONE AT A TIME.
    #
    # Firing four review angles concurrently at a single Ollama instance does
    # not parallelise anything: there is one model on one CPU, so the calls
    # either queue inside Ollama or fight over the same cores. Measured on a
    # 16-core laptop, one call finished and the other three expired at 600 s
    # while waiting — reported as "the local model is too slow" when in fact it
    # had answered correctly once.
    #
    # Hosted providers are genuinely concurrent and stay parallel.
    local_jobs = [(p, r) for p, r in jobs if p.kind == "ollama"]
    remote_jobs = [(p, r) for p, r in jobs if p.kind != "ollama"]

    runs: list[AgentRun] = []
    if remote_jobs:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(run_one, p, r): (p, r) for p, r in remote_jobs}
            for fut in as_completed(futures):
                runs.append(fut.result())
    for p, r in local_jobs:
        runs.append(run_one(p, r))

    grounded = [f for r in runs for f in r.findings if f.grounded]
    withheld = [f for r in runs for f in r.findings if not f.grounded]
    return PanelReport(runs=runs, findings=_cluster(grounded), withheld=withheld)


def format_report(rep: PanelReport) -> str:
    L = ["=" * 74, "AI PEER REVIEW PANEL", "=" * 74, ""]
    ok = sum(1 for r in rep.runs if not r.error)
    L.append(f"{ok}/{len(rep.runs)} agent(s) responded")
    for r in sorted(rep.runs, key=lambda x: -x.seconds)[:6]:
        L.append(f"  {r.provider}/{r.reviewer}: {r.seconds:.0f}s"
                 + (" (failed)" if r.error else f", {len(r.findings)} finding(s)"))
    for prov, rate in rep.groundedness().items():
        L.append(f"  {prov}: {rate:.0%} of findings had a verifiable quote")
    if rep.withheld:
        L.append(f"  {len(rep.withheld)} finding(s) withheld - quote not found "
                 "in the manuscript")
    for e in rep.errors:
        L.append(f"  ! {e}")

    L.append("\n" + "-" * 74)
    if not rep.findings:
        L.append("No grounded findings.")
    for i, f in enumerate(rep.findings, 1):
        agree = (f" [{f.agreement} referees agree]" if f.agreement > 1 else "")
        L.append(f"\n{i}. [{f.severity}]{agree} {f.issue}")
        if f.section:
            L.append(f"   section: {f.section}")
        if f.why:
            L.append(f"   why: {f.why}")
        if f.fix:
            L.append(f"   fix: {f.fix}")
        if f.quote:
            L.append(f"   quote: \"{f.quote[:160]}\"")
        L.append(f"   raised by: {', '.join(f.agreed_by)}")

    L.append("\n" + "=" * 74)
    L.append("These are model-generated comments, not a decision. Every finding")
    L.append("shown is anchored to a quote verified against your text; findings")
    L.append("whose quotes could not be found were withheld. Judge them yourself.")
    L.append("=" * 74)
    return "\n".join(L)
