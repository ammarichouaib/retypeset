"""
retypeset.learn -- small local models for the two genuinely learnable decisions.

Can this tool be "trained"? Partly, and it matters a great deal *which* parts.

NOT trainable, and must never be:
    Reading OMML, extracting figures, counting tables, restyling a DOCX,
    emitting LaTeX. These are deterministic transformations of a known file
    format. A model here would introduce non-determinism into a manuscript --
    the same input giving different output on two runs is disqualifying, and
    any error it made would be silent.

Genuinely learnable, because the information is ambiguous even to a careful
human reading one paragraph at a time:
    1. Is this paragraph a section heading?
       Word files usually carry no heading styles, so this is inferred from
       shape: length, capitalisation, numbering, boldness, terminal punctuation.
    2. What role does this heading play?
       "Protection of a Very High Voltage (VHV) Line Span" is a methods section,
       but no keyword lexicon will ever contain it.

Both are small text-classification problems. They need hundreds of examples, not
millions, and they run in milliseconds on a CPU. Logistic regression over
character n-grams plus a handful of hand-built structural features beats a large
model here, because the signal is largely shape rather than meaning, and because
you can read the coefficients when it misbehaves.

Training data comes from your own corrections in the review console: every time
you fix a heading or assign a role, that becomes a labelled example. This is the
only honest source -- the heuristic's own guesses would just teach the model to
repeat them.

    python train_local.py --status
    python train_local.py --train

Models are written to `models/` and picked up automatically on the next parse.
If scikit-learn is not installed, or no model exists, retypeset falls back to the
rule-based path with no change in behaviour.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
DATA_FILE = MODEL_DIR / "corrections.jsonl"
HEADING_MODEL = MODEL_DIR / "heading.joblib"
ROLE_MODEL = MODEL_DIR / "role.joblib"

# Below this, a trained model is worse than the lexicon it replaces.
MIN_HEADING_EXAMPLES = 60
MIN_ROLE_EXAMPLES = 40
MIN_ROLE_CLASSES = 3


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^\s*\(?((?:\d+|[IVXLC]+|[A-Z])(?:[.\-]\d+)*)\)?[.)]?\s+")


def structural_features(text: str) -> dict[str, float]:
    """Shape features. These carry most of the heading signal.

    Deliberately independent of language: a French or Polish manuscript has the
    same geometry even though none of its words match an English lexicon.
    """
    t = text.strip()
    words = t.split()
    letters = [c for c in t if c.isalpha()]
    return {
        "n_chars": len(t),
        "n_words": len(words),
        "ends_period": float(t.endswith(".")),
        "ends_colon": float(t.endswith(":")),
        "ends_punct": float(bool(t) and t[-1] in ".,;:!?"),
        "starts_number": float(bool(_NUM_RE.match(t))),
        "num_depth": float(_NUM_RE.match(t).group(1).count(".") + 1) if _NUM_RE.match(t) else 0.0,
        "all_caps": float(bool(letters) and all(c.isupper() for c in letters)),
        "title_case": float(bool(words) and sum(
            1 for w in words if w[:1].isupper()) / max(1, len(words)) > 0.6),
        "upper_ratio": (sum(1 for c in letters if c.isupper()) / len(letters)
                        if letters else 0.0),
        "has_digit": float(any(c.isdigit() for c in t)),
        "has_comma": float("," in t),
        "short": float(len(words) <= 8),
        "very_short": float(len(words) <= 4),
        "n_stopwords": float(sum(
            1 for w in words if w.lower() in
            {"the", "a", "an", "of", "in", "is", "are", "was", "were", "and",
             "to", "for", "with", "that", "this", "which", "by", "on", "as"}
        )) / max(1, len(words)),
    }


FEATURE_ORDER = sorted(structural_features("x").keys())


def _vec(text: str) -> list[float]:
    f = structural_features(text)
    return [f[k] for k in FEATURE_ORDER]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def append_examples(examples: list[dict], path: Path | None = None) -> int:
    """Append corrections to the training file. Returns the number written."""
    p = path or DATA_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    seen = {json.dumps(e, sort_keys=True) for e in load_examples(p)}
    n = 0
    with p.open("a", encoding="utf-8") as fh:
        for e in examples:
            key = json.dumps(e, sort_keys=True)
            if key in seen:
                continue
            fh.write(key + "\n")
            seen.add(key)
            n += 1
    return n


def load_examples(path: Path | None = None) -> list[dict]:
    p = path or DATA_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@dataclass
class Status:
    n_examples: int
    n_headings: int
    n_roles: int
    role_classes: int
    sklearn: bool
    heading_model: bool
    role_model: bool

    def can_train_heading(self) -> bool:
        return (self.sklearn and self.n_examples >= MIN_HEADING_EXAMPLES
                and self.n_headings >= 10
                and self.n_examples - self.n_headings >= 10)

    def can_train_role(self) -> bool:
        return (self.sklearn and self.n_roles >= MIN_ROLE_EXAMPLES
                and self.role_classes >= MIN_ROLE_CLASSES)

    def report(self) -> str:
        L = [
            f"training examples : {self.n_examples}",
            f"  headings        : {self.n_headings}",
            f"  non-headings    : {self.n_examples - self.n_headings}",
            f"  with a role     : {self.n_roles} across {self.role_classes} class(es)",
            f"scikit-learn      : {'yes' if self.sklearn else 'NOT INSTALLED'}",
            f"heading model     : {'trained' if self.heading_model else 'none'}",
            f"role model        : {'trained' if self.role_model else 'none'}",
            "",
        ]
        if not self.sklearn:
            L.append("Install it first:  pip install scikit-learn joblib")
            return "\n".join(L)
        if self.can_train_heading():
            L.append("Heading detector : ready to train.")
        else:
            need = max(0, MIN_HEADING_EXAMPLES - self.n_examples)
            L.append(f"Heading detector : needs {need} more example(s), "
                     "with at least 10 of each class.")
        if self.can_train_role():
            L.append("Role classifier  : ready to train.")
        else:
            L.append(f"Role classifier  : needs {MIN_ROLE_EXAMPLES} role-labelled "
                     f"headings across {MIN_ROLE_CLASSES}+ roles "
                     f"(have {self.n_roles}/{self.role_classes}).")
        return "\n".join(L)


def status(path: Path | None = None) -> Status:
    ex = load_examples(path)
    roles = [e for e in ex if e.get("role")]
    return Status(
        n_examples=len(ex),
        n_headings=sum(1 for e in ex if e.get("is_heading")),
        n_roles=len(roles),
        role_classes=len({e["role"] for e in roles}),
        sklearn=_have_sklearn(),
        heading_model=HEADING_MODEL.exists(),
        role_model=ROLE_MODEL.exists(),
    )


def _have_sklearn() -> bool:
    try:
        import sklearn  # noqa: F401, PLC0415
        import joblib  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

# These must live at module scope: joblib pickles the fitted pipeline by
# reference, and a closure defined inside _build_pipeline cannot be found again
# on load. Defining them inline fails only at save time, after training has
# already run, which is a particularly annoying way to lose work.
def _take_text(X):
    return [x[0] for x in X]


def _take_feats(X):
    return [_vec(x[0]) for x in X]


def _build_pipeline(kind: str):
    from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415
    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415
    from sklearn.pipeline import Pipeline  # noqa: PLC0415
    from sklearn.preprocessing import FunctionTransformer, StandardScaler  # noqa: PLC0415

    text_branch = Pipeline([
        ("pick", FunctionTransformer(_take_text)),
        # Character n-grams: robust to language, morphology and the
        # abbreviations that fill engineering headings.
        ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                                  min_df=1, sublinear_tf=True,
                                  max_features=20000, lowercase=True)),
    ])
    feat_branch = Pipeline([
        ("pick", FunctionTransformer(_take_feats)),
        ("scale", StandardScaler()),
    ])

    from sklearn.pipeline import FeatureUnion  # noqa: PLC0415

    return Pipeline([
        ("features", FeatureUnion([("text", text_branch), ("shape", feat_branch)])),
        ("clf", LogisticRegression(
            max_iter=2000,
            class_weight="balanced",     # headings are far rarer than body text
            C=2.0 if kind == "heading" else 4.0,
        )),
    ])


def train(path: Path | None = None, out_dir: Path | None = None,
          verbose: bool = True) -> dict[str, Any]:
    """Train whichever models have enough data. Returns a metrics dict."""
    if not _have_sklearn():
        raise RuntimeError("scikit-learn and joblib are required: "
                           "pip install scikit-learn joblib")

    import joblib  # noqa: PLC0415
    from sklearn.model_selection import cross_val_score  # noqa: PLC0415

    out = Path(out_dir) if out_dir else MODEL_DIR
    out.mkdir(parents=True, exist_ok=True)
    ex = load_examples(path)
    st = status(path)
    metrics: dict[str, Any] = {"n_examples": len(ex)}

    if st.can_train_heading():
        X = [(e["text"],) for e in ex]
        y = [bool(e.get("is_heading")) for e in ex]
        pipe = _build_pipeline("heading")
        folds = min(5, min(y.count(True), y.count(False)))
        if folds >= 2:
            scores = cross_val_score(pipe, X, y, cv=folds, scoring="f1")
            metrics["heading_f1_cv"] = round(float(scores.mean()), 3)
            metrics["heading_f1_std"] = round(float(scores.std()), 3)
        pipe.fit(X, y)
        joblib.dump({"pipeline": pipe, "features": FEATURE_ORDER},
                    out / HEADING_MODEL.name)
        metrics["heading_model"] = str(out / HEADING_MODEL.name)
        if verbose:
            print(f"heading detector trained on {len(X)} examples "
                  f"(cv F1 {metrics.get('heading_f1_cv', 'n/a')})")
    elif verbose:
        print("heading detector: not enough data yet")

    role_ex = [e for e in ex if e.get("role") and e.get("is_heading")]
    if st.can_train_role():
        X = [(e["text"],) for e in role_ex]
        y = [e["role"] for e in role_ex]
        pipe = _build_pipeline("role")
        counts = {c: y.count(c) for c in set(y)}

        # Cross-validation needs at least two examples per class. Roles seen
        # once cannot be scored, but they are still worth training on -- so
        # score on the scorable subset and say plainly what was excluded,
        # rather than reporting "n/a" and leaving the user to guess why.
        scorable = {c for c, n in counts.items() if n >= 2}
        thin = sorted(c for c, n in counts.items() if n < 2)
        if len(scorable) >= 2:
            Xs = [x for x, lab in zip(X, y) if lab in scorable]
            ys = [lab for lab in y if lab in scorable]
            folds = min(5, min(ys.count(c) for c in scorable))
            if folds >= 2:
                scores = cross_val_score(pipe, Xs, ys, cv=folds, scoring="accuracy")
                metrics["role_acc_cv"] = round(float(scores.mean()), 3)
                metrics["role_classes_scored"] = len(scorable)
        if thin:
            metrics["role_classes_unscored"] = thin

        pipe.fit(X, y)
        joblib.dump({"pipeline": pipe, "classes": sorted(set(y))},
                    out / ROLE_MODEL.name)
        metrics["role_model"] = str(out / ROLE_MODEL.name)
        if verbose:
            acc = metrics.get("role_acc_cv")
            scored = metrics.get("role_classes_scored")
            print(f"role classifier trained on {len(X)} examples across "
                  f"{len(counts)} role(s)"
                  + (f"; cv accuracy {acc} over the {scored} role(s) with enough "
                     "data to score" if acc is not None else
                     "; too few examples per role to cross-validate"))
            if thin:
                print(f"  roles with a single example (trained, not scored): "
                      f"{', '.join(thin)}")
    elif verbose:
        print("role classifier: not enough data yet")

    return metrics


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

class _Loaded:
    heading = None
    role = None
    finding = None
    tried = False
    tried_finding = False


def _load() -> None:
    if _Loaded.tried:
        return
    _Loaded.tried = True
    if not _have_sklearn():
        return
    import joblib  # noqa: PLC0415

    try:
        if HEADING_MODEL.exists():
            _Loaded.heading = joblib.load(HEADING_MODEL)["pipeline"]
        if ROLE_MODEL.exists():
            _Loaded.role = joblib.load(ROLE_MODEL)["pipeline"]
    except Exception:
        _Loaded.heading = _Loaded.role = None


def predict_heading(text: str) -> tuple[bool, float] | None:
    """(is_heading, confidence), or None when no model is available."""
    _load()
    if _Loaded.heading is None:
        return None
    proba = _Loaded.heading.predict_proba([(text,)])[0]
    classes = list(_Loaded.heading.classes_)
    idx = classes.index(True) if True in classes else 1
    p = float(proba[idx])
    return p >= 0.5, p


def predict_role(text: str) -> tuple[str, float] | None:
    """(role, confidence), or None when no model is available."""
    _load()
    if _Loaded.role is None:
        return None
    proba = _Loaded.role.predict_proba([(text,)])[0]
    classes = list(_Loaded.role.classes_)
    i = int(proba.argmax())
    return str(classes[i]), float(proba[i])


def reset_cache() -> None:
    """Force a reload after retraining."""
    _Loaded.heading = _Loaded.role = None
    _Loaded.finding = None
    _Loaded.tried = _Loaded.tried_finding = False


# ---------------------------------------------------------------------------
# Finding usefulness
# ---------------------------------------------------------------------------
# You cannot fine-tune the referee models -- they are hosted APIs behind a
# request. What you *can* train is the filter in front of you: which of their
# findings were worth reading. Rating findings in the review console teaches a
# small classifier to push vacuous criticism ("the discussion could be
# improved") below the specific kind, without ever changing what the models say.
#
# This is the same shape as the heading model and needs the same order of data:
# a few dozen ratings, not thousands.

FINDING_DATA = MODEL_DIR / "finding_feedback.jsonl"
FINDING_MODEL = MODEL_DIR / "finding.joblib"
MIN_FINDING_EXAMPLES = 40


def rate_finding(text: str, useful: bool, path: Path | None = None) -> int:
    """Record one judgement about a model finding."""
    p = path or FINDING_DATA
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"text": text.strip()[:600], "useful": bool(useful)},
                      sort_keys=True)
    existing = {json.dumps(e, sort_keys=True) for e in load_examples(p)}
    if line in existing:
        return 0
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return 1


def finding_status(path: Path | None = None) -> tuple[int, int, bool]:
    """(rated, useful, trainable)."""
    ex = load_examples(path or FINDING_DATA)
    useful = sum(1 for e in ex if e.get("useful"))
    ok = (_have_sklearn() and len(ex) >= MIN_FINDING_EXAMPLES
          and useful >= 8 and len(ex) - useful >= 8)
    return len(ex), useful, ok


def train_findings(path: Path | None = None, out_dir: Path | None = None,
                   verbose: bool = True) -> dict[str, Any]:
    if not _have_sklearn():
        raise RuntimeError("pip install scikit-learn joblib")
    import joblib  # noqa: PLC0415
    from sklearn.model_selection import cross_val_score  # noqa: PLC0415

    n, useful, ok = finding_status(path)
    if not ok:
        if verbose:
            print(f"finding filter: {n} rated ({useful} useful) — need "
                  f"{MIN_FINDING_EXAMPLES} with at least 8 of each")
        return {"n": n}

    ex = load_examples(path or FINDING_DATA)
    X = [(e["text"],) for e in ex]
    y = [bool(e["useful"]) for e in ex]
    pipe = _build_pipeline("heading")          # same shape works here
    folds = min(5, min(y.count(True), y.count(False)))
    out: dict[str, Any] = {"n": n}
    if folds >= 2:
        out["f1_cv"] = round(float(cross_val_score(
            pipe, X, y, cv=folds, scoring="f1").mean()), 3)
    pipe.fit(X, y)
    target = Path(out_dir) if out_dir else MODEL_DIR
    target.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pipeline": pipe}, target / FINDING_MODEL.name)
    out["model"] = str(target / FINDING_MODEL.name)
    if verbose:
        print(f"finding filter trained on {n} ratings "
              f"(cv F1 {out.get('f1_cv', 'n/a')})")
    return out


def predict_finding(text: str) -> float | None:
    """Probability this finding is worth the author's attention, or None."""
    if not _Loaded.tried_finding:
        _Loaded.tried_finding = True
        if _have_sklearn() and FINDING_MODEL.exists():
            try:
                import joblib  # noqa: PLC0415

                _Loaded.finding = joblib.load(FINDING_MODEL)["pipeline"]
            except Exception:
                _Loaded.finding = None
    if _Loaded.finding is None:
        return None
    proba = _Loaded.finding.predict_proba([(text,)])[0]
    classes = list(_Loaded.finding.classes_)
    idx = classes.index(True) if True in classes else 1
    return float(proba[idx])
