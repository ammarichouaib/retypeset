#!/usr/bin/env python3
"""Parse a .docx manuscript into the IR and print a fidelity audit.

    python run_parse.py "paper.docx" [-o out_dir]

Outputs into <out_dir>/:
    <stem>.ir.json      the intermediate representation
    <stem>.audit.txt    the fidelity report
    media/              extracted figures
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from retypeset import audit, format_report, parse_docx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx", help="path to the manuscript .docx")
    ap.add_argument("-o", "--out", default="parsed", help="output directory")
    ap.add_argument("--quiet", action="store_true", help="write files, print nothing")
    args = ap.parse_args()

    src = Path(args.docx)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ms = parse_docx(src, media_dir=out / "media")
    report = audit(ms, src)
    text = format_report(report, ms)

    stem = src.stem.strip().replace(" ", "_")
    (out / f"{stem}.ir.json").write_text(
        ms.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    (out / f"{stem}.audit.txt").write_text(text, encoding="utf-8")
    (out / f"{stem}.audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if not args.quiet:
        print(text)
    return 0 if report["ready_to_render"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
