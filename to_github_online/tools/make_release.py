#!/usr/bin/env python3
"""Assemble the folder that goes to GitHub, and nothing else.

    python tools/make_release.py                 # -> to_github_online/
    python tools/make_release.py --check         # list what would be copied, copy nothing
    python tools/make_release.py --with-paper    # include the SoftwareX manuscript
    python tools/make_release.py --with-ci       # include the GitHub automation
    python tools/make_release.py --zip           # also write a source zip

Why an allowlist and not a .gitignore
-------------------------------------
A .gitignore protects a repository you already control. This working folder is
not that: it holds two unpublished manuscripts belonging to you and your
co-authors, a 40 MB Windows installer, parsed output from real papers, and a
`models/corrections.jsonl` built from your own files. A single `git add -A`
publishes all of it irreversibly -- GitHub keeps deleted blobs reachable, and a
force-push does not reliably remove them.

So this script copies *only* what is named below. Anything not on the list is
excluded by default, and the exclusions that matter are printed with a reason
on every run, so you can see what was withheld and why.

Two things are excluded that you have to ask for by name:

* `--with-paper` -- the SoftwareX manuscript. Unpublished work does not belong
  in a repository by default, whatever the repository's visibility setting.
  Turn it on when the paper is published, or when the repo is private and you
  want the paper alongside the code it describes.
* `--with-ci` -- the GitHub Actions files from `packaging/ci/`. They build the
  Windows `.exe` on GitHub's machines. Without them nothing runs automatically
  and you build the `.exe` yourself with `build_exe.bat`.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# What ships
# --------------------------------------------------------------------------
DIRS = [
    ("retypeset", "the library"),
    ("ui", "the Streamlit panels"),
    ("profiles", "journal profiles"),
    ("packaging", "how to build the Windows application"),
    ("tools", "this script, and the publisher"),
]

FILES = [
    ("app.py", "the app"),
    ("app_classic.py", "the previous console, kept runnable"),
    ("run_parse.py", "CLI: parse + audit"),
    ("run_render.py", "CLI: parse + check + generate"),
    ("train_local.py", "CLI: local training"),
    ("build_corpus.py", "training-data bootstrap"),
    ("build_exe.bat", "one-click Windows build"),
    ("publish_to_github.bat", "one-click publish"),
    ("requirements.txt", ""),
    ("packages.txt", "apt packages for Streamlit Cloud"),
    ("README.md", ""),
    ("CONTRIBUTING.md", ""),
    ("DEPLOY.md", ""),
    ("LICENSE", ""),
    (".gitignore", ""),
    (".streamlit/secrets.toml.example", "key names only, never the keys"),
]

# Test code ships; test *data* does not (see EXCLUDED). Globbed rather than
# listed: a named list silently stops shipping the test written yesterday.
TESTS = sorted(str(f.relative_to(ROOT)).replace("\\", "/")
               for f in (ROOT / "tests").glob("test_*.py"))

# Only with --with-paper.
PAPER = [
    "paper/manuscript_softwarex.md",
    "paper/cover_letter.md",
    "paper/paper.bib",
    "paper/CITATION.cff",
    "paper/SUBMISSION_CHECKLIST.md",
    "paper/build_docx.js",
    "paper/make_figures.py",
]
PAPER_FIGURES = "paper/figures"

# Only with --with-ci: copied to the path GitHub requires.
CI_SOURCE = "packaging/ci"
CI_TARGET = ".github/workflows"

KEEP_DIRS = ["models", "tests/samples"]

TEMPLATES_README = """\
# Publisher templates

The templates themselves are not redistributed here: they are the publishers'
files, and their licences are their own. Download the one you need and drop it
in this folder, or simply upload it in the app at step 1 -- retypeset reads a
template directly and derives a journal profile from it.

| Template | Where |
|---|---|
| `elsarticle` (Elsevier LaTeX) | https://www.elsevier.com/authors/policies-and-guidelines/latex-instructions |
| IEEE Transactions (Word + LaTeX) | https://template-selector.ieee.org/ |
| Springer Nature `sn-jnl` | https://www.springernature.com/gp/authors/campaigns/latex-author-support |
| MDPI | https://www.mdpi.com/authors/layout |

The tests that exercise template derivation skip when these files are absent.
"""


def excluded(with_paper: bool, with_ci: bool) -> list[tuple[str, str]]:
    out = [
        ("tests/samples/*.docx",
         "TWO REAL MANUSCRIPTS. One is a colleague's Diagnostyka submission, "
         "the other your unpublished renewable-energy paper. Publishing either "
         "is a copyright and confidentiality problem, and it cannot be undone: "
         "GitHub keeps deleted blobs reachable by hash. The tests skip cleanly "
         "when they are absent."),
        ("models/corrections.jsonl, models/*.joblib",
         "training data derived from your own manuscripts, and the models built "
         "from it. A fresh clone regenerates both with `python train_local.py "
         "--seed --train`."),
        ("parsed/, rendered/, H2_ieee_fixed/",
         "output from real papers, including extracted figures."),
        ("pandoc-3.10.1-windows-x86_64.msi (40 MB)",
         "a third-party installer. `pip install pypandoc_binary` fetches it, "
         "and the Windows build downloads it at build time."),
        ("app.rar, _legacy_app_gemini.py",
         "superseded; git history is the place for old versions."),
        ("templates/*.docx, templates/elsarticle/",
         "publisher templates are the publishers' files, not yours. A short "
         "templates/README.md ships instead, saying where to download each."),
        (".streamlit/secrets.toml",
         "YOUR API KEYS. The .example file ships; this one never does."),
    ]
    if not with_paper:
        out.insert(1, (
            "paper/ (the SoftwareX manuscript, cover letter and figures)",
            "AN UNPUBLISHED MANUSCRIPT. Excluded by default: a private "
            "repository is one settings click away from public, and a "
            "manuscript under review should not depend on that click. Pass "
            "--with-paper when it is published, or when you have decided the "
            "repository stays private."))
    if not with_ci:
        out.append((
            "packaging/ci/*.yml -> .github/workflows/",
            "GitHub's automated build of the Windows .exe. Not published unless "
            "you pass --with-ci. Without it, build the .exe yourself by "
            "double-clicking build_exe.bat on a Windows machine."))
    return out


def _drop(p: Path) -> bool:
    parts = set(p.parts)
    if parts & {"__pycache__", ".pytest_cache", "node_modules", "ci"}:
        return True
    # publish_config.json remembers which repository this folder is published
    # to. That is a local setting, not part of the software.
    return p.name in ("package-lock.json", "package.json",
                      "publish_config.json") or p.suffix in (".pyc", ".joblib")


def _copy(src: Path, dst: Path, manifest: list[str], dry: bool) -> None:
    if not src.exists():
        print(f"  ! missing, skipped: {src.relative_to(ROOT)}")
        return
    if src.is_dir():
        for f in sorted(src.rglob("*")):
            if f.is_file() and not _drop(f.relative_to(ROOT)):
                _copy(f, dst / f.relative_to(src), manifest, dry)
        return
    manifest.append(str(src.relative_to(ROOT)).replace("\\", "/"))
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="to_github_online",
                    help="destination folder (default to_github_online)")
    ap.add_argument("--check", action="store_true",
                    help="list what would be copied, copy nothing")
    ap.add_argument("--with-paper", action="store_true",
                    help="include the SoftwareX manuscript (excluded by default)")
    ap.add_argument("--with-ci", action="store_true",
                    help="include GitHub's automated Windows build")
    ap.add_argument("--zip", action="store_true", help="also write a source zip")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a non-empty destination")
    args = ap.parse_args()

    out = (ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    # A destination that is already a git repository is refreshed in place: the
    # .git folder is what remembers where it was published.
    busy = out.exists() and any(p.name != ".git" for p in out.iterdir())
    if busy and not (args.check or args.force):
        print(f"error: {out} is not empty. Delete it, or pass --force.",
              file=sys.stderr)
        return 2

    # Clear the destination first, keeping only .git. Copying over an existing
    # folder leaves whatever the previous run put there: a build made once with
    # --with-paper would keep publishing the manuscript for ever afterwards,
    # even from a command that never mentions it. That is the failure this
    # whole script exists to prevent, so it cannot be left to chance.
    if not args.check and out.exists():
        removed = 0
        for item in out.iterdir():
            if item.name == ".git":
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
            removed += 1
        if removed:
            print(f"cleared {removed} item(s) from {out.name} "
                  "(the .git folder, if any, was kept)\n")

    manifest: list[str] = []
    print(f"{'Listing' if args.check else 'Assembling'} {out}\n")

    for name, _ in DIRS:
        _copy(ROOT / name, out / name, manifest, args.check)
    for name, _ in FILES:
        _copy(ROOT / name, out / name, manifest, args.check)
    for name in TESTS:
        _copy(ROOT / name, out / name, manifest, args.check)

    if args.with_paper:
        for name in PAPER:
            _copy(ROOT / name, out / name, manifest, args.check)
        _copy(ROOT / PAPER_FIGURES, out / PAPER_FIGURES, manifest, args.check)

    if args.with_ci:
        for yml in sorted((ROOT / CI_SOURCE).glob("*.yml")):
            _copy(yml, out / CI_TARGET / yml.name, manifest, args.check)

    if not args.check:
        for d in KEEP_DIRS:
            (out / d).mkdir(parents=True, exist_ok=True)
            (out / d / ".gitkeep").write_text("", encoding="utf-8")
        (out / "templates").mkdir(parents=True, exist_ok=True)
        (out / "templates" / "README.md").write_text(TEMPLATES_README,
                                                     encoding="utf-8")
        (out / "MANIFEST.txt").write_text(
            "Files published from the working folder, generated by "
            "tools/make_release.py\n\n" + "\n".join(sorted(manifest)) + "\n",
            encoding="utf-8")

    if args.check:
        print(f"\n{len(manifest)} file(s) would be copied")
    else:
        # Count what is actually in the folder, not what was copied into it.
        # Four files are generated here rather than copied -- MANIFEST.txt,
        # templates/README.md and two .gitkeep placeholders -- and reporting
        # only the copies made this line disagree with the publisher's own
        # count two lines later, which reads like something went missing.
        on_disk = [f for f in out.rglob("*")
                   if f.is_file() and ".git" not in f.parts]
        size = sum(f.stat().st_size for f in on_disk)
        generated = len(on_disk) - len(manifest)
        print(f"\n{len(on_disk)} file(s), {size / 1e6:.1f} MB "
              f"({len(manifest)} copied"
              + (f" + {generated} generated here)" if generated else ")"))
    print("  paper/       : " + ("INCLUDED (--with-paper)" if args.with_paper
                                 else "excluded"))
    print("  GitHub CI    : " + ("INCLUDED (--with-ci)" if args.with_ci
                                 else "excluded"))

    print("\nWITHHELD ON PURPOSE")
    for what, why in excluded(args.with_paper, args.with_ci):
        print(f"  · {what}\n      {why}")

    if args.zip and not args.check:
        z = out.parent / f"{out.name}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out.rglob("*")):
                if f.is_file() and ".git" not in f.parts:
                    zf.write(f, f.relative_to(out))
        print(f"\nzip: {z} ({z.stat().st_size / 1e6:.1f} MB)")

    if not args.check:
        print(f"""
Next:
    python tools/publish_github.py --dry-run     # see exactly what would happen
    python tools/publish_github.py               # publish {out.name} to GitHub""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
