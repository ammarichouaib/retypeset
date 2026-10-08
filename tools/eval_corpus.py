#!/usr/bin/env python3
"""Batch fidelity evaluation over a folder of .docx manuscripts.

    python tools/eval_corpus.py  path/to/folder  [-o eval_out] [-j elsevier_generic]
                                 [--anon] [--no-render] [--recursive]

For every manuscript it records
  * content retention, parse stage: source OOXML vs. IR (equations, tables, images)
  * content retention, output stage: source OOXML vs. the restyled .docx
  * LaTeX figure-conversion failures and compliance counts against one profile
  * parser errors/warnings, blocking items, wall time

and writes  results.csv, results.json  and  results.md  (a paste-ready table,
with a totals row) into the output directory.

--anon replaces file names by D01, D02, ... in every output, so a table built
from unpublished or third-party manuscripts can be shared without naming them.
The mapping is written to <out>/_private_mapping.json: do not publish that file.

Nothing is fabricated or smoothed: a document that fails to parse is reported as
a failure row and counted in the denominator.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import retypeset  # noqa: E402
from retypeset.audit import _docx_ground_truth  # noqa: E402

COLUMNS = [
    "doc", "words",
    "eq_src", "eq_ir", "eq_out",
    "tbl_src", "tbl_ir", "tbl_out",
    "img_src", "img_ir", "img_out",
    "parse_ok", "parse_retained", "output_retained",
    "errors", "warnings", "blocking",
    "latex_failed_figs", "compliance_fail", "compliance_warn",
    "seconds", "note",
]


def _gt_counts(path: Path) -> tuple[int, int, int]:
    g = _docx_ground_truth(path)
    return g["omml_inline"], g["tables"], g["image_references"]


def evaluate(src: Path, label: str, profile, do_render: bool, scratch: Path) -> dict:
    row = {c: "" for c in COLUMNS}
    row["doc"] = label
    t0 = time.perf_counter()
    try:
        media = scratch / label / "media"
        ms = retypeset.parse_docx(src, media_dir=media)
        rep = retypeset.audit(ms, src)
    except Exception as exc:  # noqa: BLE001 - a failed parse is a result
        row.update(parse_ok=False, parse_retained=False, output_retained=False,
                   note=f"parse failed: {type(exc).__name__}: {str(exc)[:120]}",
                   seconds=round(time.perf_counter() - t0, 1))
        return row

    gt = rep["ground_truth"]
    checks = {c["name"]: c for c in rep["checks"]}
    row.update(
        words=ms.stats["words"],
        eq_src=checks["Equations (OMML)"]["source"], eq_ir=checks["Equations (OMML)"]["ir"],
        tbl_src=checks["Tables"]["source"], tbl_ir=checks["Tables"]["ir"],
        img_src=checks["Embedded images"]["source"], img_ir=checks["Embedded images"]["ir"],
        parse_ok=True,
        parse_retained=all(c["ok"] for c in rep["checks"]),
        errors=sum(1 for i in ms.issues if i.severity == "error"),
        warnings=sum(1 for i in ms.issues if i.severity == "warning"),
        blocking=len(rep["blocking"]),
    )
    notes = []
    if rep["blocking"]:
        notes.append("; ".join(b[:60] for b in rep["blocking"][:2]))

    if do_render:
        try:
            comp = retypeset.check(ms, profile, media)
            row["compliance_fail"] = len(comp.failures)
            row["compliance_warn"] = len(comp.warnings)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"compliance: {type(exc).__name__}")
        try:
            out_docx = scratch / label / f"{label}_{profile.id}.docx"
            res = retypeset.render_docx(src, ms, profile, out_docx, strip_furniture=True)
            eq, tb, im = _gt_counts(Path(res.path))
            row.update(eq_out=eq, tbl_out=tb, img_out=im)
            # Output retention: equations and tables must not decrease. Images may
            # fall only by the previous journal's logo, so a drop is flagged in the
            # note and left to inspection rather than silently excused.
            row["output_retained"] = eq >= row["eq_src"] and tb >= row["tbl_src"] and im >= row["img_src"]
            if im < row["img_src"] and eq >= row["eq_src"] and tb >= row["tbl_src"]:
                notes.append(f"images {row['img_src']}->{im} (furniture strip? inspect)")
        except Exception as exc:  # noqa: BLE001
            row["output_retained"] = False
            notes.append(f"docx render failed: {type(exc).__name__}: {str(exc)[:80]}")
        try:
            lt = retypeset.render_latex(ms, profile, media, scratch / label / "tex")
            row["latex_failed_figs"] = len(set(lt.failed_figures))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"latex render failed: {type(exc).__name__}: {str(exc)[:80]}")

    row["seconds"] = round(time.perf_counter() - t0, 1)
    row["note"] = " | ".join(n for n in notes if n)
    return row


def _md_table(rows: list[dict], total: dict) -> str:
    head = ("| Doc | Words | Equations src/IR/out | Tables src/IR/out | Images src/IR/out "
            "| Parse | Output | Err | Blocking | s |")
    sep = "|---|---:|---:|---:|---:|:-:|:-:|---:|---:|---:|"
    out = [head, sep]

    def tri(r, k):
        if r["parse_ok"] is False:
            return "–"
        o = r[f"{k}_out"]
        return f"{r[f'{k}_src']}/{r[f'{k}_ir']}/{o if o != '' else '–'}"

    def flag(v):
        return "–" if v == "" else ("OK" if v else "LOSS")

    for r in rows:
        out.append(f"| {r['doc']} | {r['words']} | {tri(r,'eq')} | {tri(r,'tbl')} | {tri(r,'img')} "
                   f"| {flag(r['parse_retained'])} | {flag(r['output_retained'])} "
                   f"| {r['errors']} | {r['blocking']} | {r['seconds']} |")
    out.append(f"| **Total ({total['n']})** | {total['words']} | {total['eq']} | {total['tbl']} | {total['img']} "
               f"| {total['parse_ok']}/{total['n']} | {total['out_ok']}/{total['n']} "
               f"| {total['errors']} | {total['blocking']} | {total['seconds']:.1f} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("-o", "--out", default="eval_out")
    ap.add_argument("-j", "--journal", default="elsevier_generic")
    ap.add_argument("--anon", action="store_true")
    ap.add_argument("--no-render", action="store_true",
                    help="parse + audit only; skip restyle, LaTeX and compliance")
    ap.add_argument("--recursive", action="store_true")
    args = ap.parse_args()

    folder = Path(args.folder)
    pat = "**/*.docx" if args.recursive else "*.docx"
    files = sorted(p for p in folder.glob(pat) if not p.name.startswith("~$"))
    if not files:
        print(f"no .docx files in {folder}", file=sys.stderr)
        return 2

    profile = None if args.no_render else retypeset.get_profile(args.journal)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="retypeset_eval_"))

    mapping, rows = {}, []
    for i, f in enumerate(files, 1):
        label = f"D{i:02d}" if args.anon else f.stem.strip().replace(" ", "_")
        mapping[label] = str(f)
        print(f"[{i}/{len(files)}] {label} ...", flush=True)
        try:
            rows.append(evaluate(f, label, profile, not args.no_render, scratch))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            rows.append({**{c: "" for c in COLUMNS}, "doc": label, "parse_ok": False,
                         "parse_retained": False, "output_retained": False,
                         "note": "unhandled error"})

    def s(k):
        return sum(r[k] for r in rows if isinstance(r[k], int))

    total = {
        "n": len(rows), "words": s("words"),
        "eq": f"{s('eq_src')}/{s('eq_ir')}/{s('eq_out')}",
        "tbl": f"{s('tbl_src')}/{s('tbl_ir')}/{s('tbl_out')}",
        "img": f"{s('img_src')}/{s('img_ir')}/{s('img_out')}",
        "parse_ok": sum(1 for r in rows if r["parse_retained"] is True),
        "out_ok": sum(1 for r in rows if r["output_retained"] is True),
        "errors": s("errors"), "blocking": s("blocking"),
        "seconds": sum(float(r["seconds"] or 0) for r in rows),
    }

    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    (out / "results.json").write_text(
        json.dumps({"journal": None if args.no_render else args.journal,
                    "version": retypeset.__version__, "rows": rows, "total": total},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    md = _md_table(rows, total)
    notes = [f"- **{r['doc']}**: {r['note']}" for r in rows if r["note"]]
    (out / "results.md").write_text(
        f"retypeset {retypeset.__version__}, profile `{args.journal}`\n\n{md}\n\n"
        + ("Notes\n\n" + "\n".join(notes) + "\n" if notes else ""), encoding="utf-8")
    if args.anon:
        (out / "_private_mapping.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")

    print("\n" + md)
    if notes:
        print("\nNotes\n" + "\n".join(notes))
    print(f"\nWritten to {out}/ (results.csv, results.json, results.md)")
    return 0 if total["parse_ok"] == total["n"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
