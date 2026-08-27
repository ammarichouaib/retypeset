#!/usr/bin/env python3
"""Parse a manuscript and generate journal-formatted output.

    python run_render.py "MyPaper.docx" --journal elsevier_generic
    python run_render.py "MyPaper.docx" -j ieee_transactions --only latex
    python run_render.py "MyPaper.docx" -t journal_template.docx --derive
    python run_render.py --list

Outputs into <out>/:
    <stem>_<journal>.docx     the original, restyled (native content untouched)
    tex/main.tex              a compilable LaTeX project
    tex/BUILD.md              what was converted and what needs checking
    compliance.txt            what the journal will object to
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import retypeset


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx", nargs="?", help="path to the manuscript .docx")
    ap.add_argument("-j", "--journal", default="elsevier_generic",
                    help="journal profile id (see --list)")
    ap.add_argument("-o", "--out", default="rendered", help="output directory")
    ap.add_argument("--only", choices=["docx", "latex", "both"], default="both")
    ap.add_argument("-t", "--template", metavar="FILE",
                    help="publisher .docx/.dotx template. When given, the Word "
                         "output is produced by transplanting that template's "
                         "styles and page setup instead of the profile's rules.")
    ap.add_argument("--derive", action="store_true",
                    help="derive the journal profile from --template instead of "
                         "using -j: page setup and styles are read from the file, "
                         "and any author instructions inside it are mined for "
                         "abstract, keyword and figure limits. The result is "
                         "always unverified, so its rules report as warnings.")
    ap.add_argument("--save-profile", metavar="ID",
                    help="with --derive, also write the derived profile to "
                         "profiles/<ID>.json so it appears in --list next time")
    ap.add_argument("--keep-furniture", action="store_true",
                    help="do not remove the previous journal's logo, ISSN line, "
                         "running header, licence footnote and template "
                         "instructions from the Word output")
    ap.add_argument("--list", action="store_true", help="list journal profiles")
    args = ap.parse_args()

    if args.list:
        for pid, p in sorted(retypeset.load_profiles().items()):
            mark = " " if p.verified else " (unverified)"
            print(f"  {pid:22s} {p.publisher} - {p.journal}{mark}")
        return 0

    if not args.docx:
        ap.error("a manuscript path is required unless --list is given")

    src = Path(args.docx)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 2

    if args.derive:
        if not args.template:
            ap.error("--derive needs -t/--template")
        tpl = Path(args.template)
        if not tpl.exists():
            print(f"error: template {tpl} not found", file=sys.stderr)
            return 2
        # An explicit -j is treated as the seed rather than the answer: the
        # template then overrides only what it actually proves.
        seed = retypeset.load_profiles().get(args.journal) if args.journal else None
        derived = retypeset.template_profile.derive(
            tpl, base=seed, profile_id=args.save_profile or "")
        profile = derived.profile
        print(f"Derived profile from {tpl.name}"
              + (f", seeded from {seed.id}" if seed else ""))
        for e in derived.evidence:
            print(f"    · {e}")
        if args.save_profile:
            path = retypeset.template_profile.save(profile, overwrite=True)
            print(f"    written to {path}")
    else:
        try:
            profile = retypeset.get_profile(args.journal)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Parsing {src.name} …")
    ms = retypeset.parse_docx(src, media_dir=out / "media")
    report = retypeset.audit(ms, src)
    (out / "fidelity.txt").write_text(retypeset.format_report(report, ms), encoding="utf-8")

    errors = [i for i in ms.issues if i.severity == "error"]
    print(f"  {ms.stats['words']} words, {len(ms.equations)} display equations, "
          f"{len(ms.figures)} figures, {len(ms.tables)} tables, "
          f"{len(ms.references)} references")
    for e in errors:
        print(f"  ! {e.code}: {e.message[:110]}")

    result = retypeset.check(ms, profile, out / "media")
    (out / "compliance.txt").write_text(retypeset.format_compliance(result), encoding="utf-8")
    print(f"Compliance vs {profile.journal}: {len(result.passes)} pass, "
          f"{len(result.warnings)} warn, {len(result.failures)} fail")
    for f in result.failures:
        print(f"  ! {f.rule}: {f.message[:110]}")

    stem = src.stem.strip().replace(" ", "_")

    if args.only in ("docx", "both"):
        if args.template:
            tpl = Path(args.template)
            if not tpl.exists():
                print(f"error: template {tpl} not found", file=sys.stderr)
                return 2
            info = retypeset.inspect_template(tpl)
            print(f"Template {tpl.name}: {info.summary}")
            target = out / f"{stem}_{tpl.stem}.docx"
            res = retypeset.apply_template(src, tpl, target, ms,
                                      strip_furniture=not args.keep_furniture)
            print(f"DOCX  -> {res.path}  ({res.styles_merged} styles merged, "
                  f"{res.paragraphs_mapped} paragraphs mapped)")
        else:
            target = out / f"{stem}_{profile.id}.docx"
            res = retypeset.render_docx(src, ms, profile, target,
                                   strip_furniture=not args.keep_furniture)
            print(f"DOCX  -> {res.path}  ({res.changed_paragraphs} paragraphs restyled)")
        for n in res.notes:
            print(f"    · {n}")
        for n in res.unsupported:
            print(f"  ! {n[:120]}")

    if args.only in ("latex", "both"):
        res = retypeset.render_latex(ms, profile, out / "media", out / "tex")
        print(f"LaTeX -> {res.main_tex}")
        if res.failed_figures:
            print(f"  ! {len(set(res.failed_figures))} figure(s) not converted: "
                  + ", ".join(sorted(set(res.failed_figures))))
        print("  build: cd tex && pdflatex main && pdflatex main")

    print(f"\nSee {out/'fidelity.txt'}, {out/'compliance.txt'} and "
          f"{out/'tex'/'BUILD.md'} before submitting.")
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
