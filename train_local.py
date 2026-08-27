#!/usr/bin/env python3
"""Train retypeset's local models on your own corrections.

    python train_local.py                     # status (same as --status)
    python train_local.py --seed              # add the built-in seed corpus
    python train_local.py --harvest ./papers  # mine .docx that carry heading styles
    python train_local.py --train             # train what is trainable
    python train_local.py --findings          # train the finding-usefulness filter
    python train_local.py --all               # seed + train + findings, in order
    python train_local.py --test "Protection of a Very High Voltage Line Span"
    python train_local.py --reset             # delete trained models, keep the data

The same operations are available in the app — sidebar **Local training**, or
**Advanced → Training** for the full panel — which is where most people should
run them, because that is where the corrections are produced. This CLI exists
for scripted runs and for training on a machine with no browser.

Everything runs locally. No data leaves this machine, and nothing is uploaded.

Where the data comes from
    Every correction you make in the Sections panel is appended to
    `models/corrections.jsonl`. That file is plain JSON Lines: inspect it,
    edit it, delete a line and the next run forgets that example.

What is being trained
    1. Heading detection        — is this paragraph a section heading?
    2. Role classification      — which canonical role does that heading play?
    3. Finding usefulness       — which model-panel criticisms were worth reading?

Nothing else. The parser, restyler and LaTeX writer stay deterministic on
purpose: a manuscript converter that gives different output on two runs of the
same file is not usable, and a model's mistakes there would be silent.

Exit codes
    0 success · 1 nothing to do (not enough data) · 2 error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from retypeset import learn


def _status(data: Path | None) -> int:
    print(learn.status(data).report())
    n, useful, ok = learn.finding_status()
    print(f"finding ratings   : {n} ({useful} useful) — "
          + ("ready to train" if ok else "not enough yet"))
    return 0


def _seed(data: Path | None) -> int:
    import build_corpus
    n = build_corpus.write_seed(data)
    print(f"seed corpus: {n} new example(s) added")
    return 0


def _harvest(folder: str, data: Path | None, any_docx: bool) -> int:
    import build_corpus
    path = Path(folder)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    r = build_corpus.harvest(path, data, require_styles=not any_docx)
    print(f"harvested {path}\n"
          f"  usable files : {r['files']}\n"
          f"  headings     : {r['headings']}\n"
          f"  body lines   : {r['body']}\n"
          f"  new examples : {r['written']} (duplicates skipped)")
    for s in r["skipped"][:10]:
        print(f"  skipped: {s}")
    return 0


def _train(data: Path | None, out: Path | None) -> int:
    try:
        metrics = learn.train(data, out)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not any(k.endswith("_model") for k in metrics):
        print("\nNothing trained yet. Keep correcting sections in the app; the "
              "numbers below show what is still needed.\n")
        print(learn.status(data).report())
        return 1
    print("\nDone. The next parse will use these models automatically.")
    return 0


def _findings(data: Path | None, out: Path | None) -> int:
    try:
        m = learn.train_findings(data, out)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0 if m.get("model") else 1


def _reset() -> int:
    removed = []
    for p in (learn.HEADING_MODEL, learn.ROLE_MODEL, learn.FINDING_MODEL):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    print("deleted: " + (", ".join(removed) or "nothing"))
    print("Corrections are untouched — training again reproduces the models.")
    return 0


def _test(text: str) -> int:
    learn.reset_cache()
    h = learn.predict_heading(text)
    r = learn.predict_role(text)
    if h is None and r is None:
        print("No trained model found. Run --train first.")
        return 1
    if h:
        print(f"heading : {h[0]}  (confidence {h[1]:.0%})")
    if r:
        print(f"role    : {r[0]}  (confidence {r[1]:.0%})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="show data and model state")
    ap.add_argument("--seed", action="store_true",
                    help="add the built-in seed corpus of section names")
    ap.add_argument("--harvest", metavar="FOLDER",
                    help="mine .docx files that carry Word heading styles")
    ap.add_argument("--any-docx", action="store_true",
                    help="harvest files without heading styles too (lower quality)")
    ap.add_argument("--train", action="store_true", help="train heading + role models")
    ap.add_argument("--findings", action="store_true",
                    help="train the finding-usefulness filter from your ratings")
    ap.add_argument("--all", action="store_true",
                    help="seed, then train, then train findings")
    ap.add_argument("--reset", action="store_true",
                    help="delete trained models, keeping the corrections")
    ap.add_argument("--test", metavar="TEXT", help="predict for one line of text")
    ap.add_argument("--data", help="path to a corrections .jsonl (default models/)")
    ap.add_argument("--out", help="where to write models (default models/)")
    args = ap.parse_args()

    data = Path(args.data) if args.data else None
    out = Path(args.out) if args.out else None

    if args.test:
        return _test(args.test)
    if args.reset:
        return _reset()

    if args.all:
        _seed(data)
        rc = _train(data, out)
        _findings(data, out)          # optional: absence of ratings is not failure
        return rc

    rc = 0
    did = False
    if args.seed:
        rc = _seed(data) or rc
        did = True
    if args.harvest:
        rc = _harvest(args.harvest, data, args.any_docx) or rc
        did = True
    if args.train:
        rc = _train(data, out) or rc
        did = True
    if args.findings:
        rc = _findings(data, out) or rc
        did = True

    if not did or args.status:
        _status(data)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
