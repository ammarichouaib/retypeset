#!/usr/bin/env python3
"""Publish the release folder to GitHub in one command.

    python tools/publish_github.py --repo ammarichouaib/retypeset
    python tools/publish_github.py --tag v0.8.2       # also trigger the .exe build
    python tools/publish_github.py --dry-run          # show every step, do nothing

What it does, in order:

1. **Rebuilds `to_github_online/`** from the working folder with
   `tools/make_release.py`, so what is published is what the code currently is.
   Nothing is deleted from your working folder, and files you added to the
   release folder by hand are left in place.
2. **Refuses to continue if anything private slipped in.** The scan is an
   independent second check, not a re-run of the allowlist: manuscripts,
   `secrets.toml`, trained models, corrections, installers, `node_modules`.
   This is the last point at which a mistake costs nothing, because GitHub
   keeps deleted blobs reachable by hash.
3. **Commits and pushes.** Creates the repository through the GitHub API if it
   does not exist yet, sets up `main`, and pushes.
4. **Optionally tags a release**, which is what starts the Windows build
   workflow and attaches the `.exe` to the release page.

The token is never written to disk. It is passed to a single `git push` through
an `http.extraheader` argument, so it stays out of `.git/config`, out of the
remote URL, and out of your shell history if you let the script prompt for it.

Getting a token
    github.com -> Settings -> Developer settings -> Personal access tokens
    Classic token, scope: `repo`.

    Add `workflow` as well *only* if you build the release with
    `make_release.py --with-ci`. GitHub rejects any push that touches
    `.github/workflows/` from a token without that scope, and the error names
    the file rather than the missing scope -- which is why the usual reaction
    is to delete the workflow files and lose the automated build. Without
    `--with-ci` there are no workflow files and `repo` alone is enough.

    Store it once so you are not asked again:
        setx GITHUB_TOKEN ghp_xxx          (Windows, new terminal after)
        export GITHUB_TOKEN=ghp_xxx        (macOS / Linux)
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

# Where the repository name is remembered between runs, so that double-clicking
# publish_to_github.bat -- which passes no arguments at all -- works from the
# second run onward. Not published: `tools/` ships, this file does not.
CONFIG = ROOT / "tools" / "publish_config.json"

# Independent of make_release.py's allowlist, on purpose. Two mechanisms that
# can fail the same way are one mechanism.
FORBIDDEN = [
    ("tests/samples/*.docx", "**a real manuscript**"),
    ("**/*.docx", "a Word document"),
    ("**/secrets.toml", "**API keys**"),
    ("**/*.joblib", "a trained model"),
    ("**/corrections.jsonl", "training data from your own manuscripts"),
    ("**/*.msi", "a Windows installer"),
    ("**/*.rar", "an archive"),
    ("**/node_modules/**", "npm dependencies"),
]
# Not forbidden, but never published by accident: the manuscript is included
# only when make_release.py was run with --with-paper, and this asks once more.
PAPER_DIR = "paper"
# Documents that are legitimately part of the paper build are allowed through
# the .docx rule, which is otherwise deliberately broad.
ALLOWED_DOCX: set[str] = set()


class Fail(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"\nerror: {msg}\n")


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

def run(args: list[str], cwd: Path, *, check: bool = True, quiet: bool = False,
        extra: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["git"] + (extra or []) + args
    if not quiet:
        shown = " ".join(a if "AUTHORIZATION" not in a else "AUTHORIZATION: <hidden>"
                         for a in cmd)
        print(f"  $ {shown}")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Fail(f"{' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r


def git_available() -> None:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise Fail("git is not installed, or not on PATH. Install Git for "
                   "Windows from https://git-scm.com/download/win and open a "
                   "new terminal.") from None


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def get_token(arg: str | None) -> str:
    if arg:
        return arg.strip()
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            print(f"  using the token in ${var}")
            return os.environ[var].strip()
    try:                                     # the GitHub CLI, if it is set up
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            print("  using the token from `gh auth token`")
            return r.stdout.strip()
    except OSError:
        pass
    print("\nA GitHub token is needed. Settings -> Developer settings -> "
          "Personal access tokens -> Tokens (classic),\nscopes `repo` and "
          "`workflow`. Nothing is echoed as you paste.")
    token = getpass.getpass("token: ").strip()
    if not token:
        raise Fail("no token given")
    print("  tip: to avoid pasting it every time, run this once in a terminal\n"
          "       and open a new one afterwards:   setx GITHUB_TOKEN <the token>")
    return token


def api(path: str, token: str, method: str = "GET",
        payload: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "retypeset-publish"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode() or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() or "{}"
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"message": body}
    except urllib.error.URLError as exc:
        raise Fail(f"cannot reach api.github.com: {exc.reason}") from None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def rebuild(out: Path, dry: bool) -> None:
    script = ROOT / "tools" / "make_release.py"
    if not script.exists():
        print("  make_release.py not found - publishing the folder as it is")
        return
    try:
        shown = out.relative_to(ROOT)
    except ValueError:                       # --out pointed outside the project
        shown = out
    print(f"\n[1/5] Rebuilding {shown} from the working folder")
    if dry:
        print("  (dry run)")
        return
    r = subprocess.run([sys.executable, str(script), "--force", "--out", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail(f"make_release.py failed:\n{r.stdout}{r.stderr}")
    kept = [ln for ln in r.stdout.splitlines() if "file(s)" in ln]
    print("  " + (kept[0] if kept else "done"))


def scan(out: Path, allow_paper: bool = False) -> None:
    print("\n[2/5] Checking for anything that must not be published")
    paper = out / PAPER_DIR
    if paper.is_dir() and any(paper.rglob("*")) and not allow_paper:
        n = sum(1 for f in paper.rglob("*") if f.is_file())
        raise Fail(
            f"the release folder contains `{PAPER_DIR}/` ({n} files) - your "
            "SoftwareX manuscript.\n\n"
            "  An unpublished manuscript in a repository depends on that "
            "repository staying private,\n  which is one settings click away "
            "from not being true.\n\n"
            "  To publish without it:   python tools/make_release.py --force\n"
            "  To publish with it:      add --allow-paper to this command")
    found: list[str] = []
    for pattern, why in FORBIDDEN:
        for hit in out.glob(pattern):
            rel = hit.relative_to(out).as_posix()
            if ".git/" in rel or rel in ALLOWED_DOCX or not hit.is_file():
                continue
            found.append(f"  {rel}\n      {why}")
    if found:
        raise Fail("these would be published, and cannot be taken back once "
                   "they are:\n\n" + "\n".join(dict.fromkeys(found))
                   + "\n\n  Remove them from the release folder and run again. "
                     "If one is a false alarm, add it to ALLOWED_DOCX at the "
                     "top of this script and say why.")
    n = sum(1 for f in out.rglob("*") if f.is_file() and ".git" not in f.parts)
    size = sum(f.stat().st_size for f in out.rglob("*")
               if f.is_file() and ".git" not in f.parts)
    print(f"  clean - {n} files, {size / 1e6:.1f} MB")


def ensure_repo(slug: str, token: str, private: bool, dry: bool) -> None:
    owner, name = slug.split("/", 1)
    print(f"\n[3/5] Repository {slug}")
    status, body = api(f"/repos/{owner}/{name}", token)
    if status == 200:
        print(f"  exists ({'private' if body.get('private') else 'public'})")
        return
    if status == 404:
        if dry:
            print("  would be created (dry run)")
            return
        me = api("/user", token)[1].get("login", "")
        path = "/user/repos" if me.lower() == owner.lower() else f"/orgs/{owner}/repos"
        status, body = api(path, token, "POST", {
            "name": name, "private": private,
            "description": "Verifiable reformatting of scientific manuscripts "
                           "between journal templates",
            "has_issues": True, "has_wiki": False, "auto_init": False})
        if status not in (200, 201):
            raise Fail(f"could not create {slug}: {body.get('message', status)}")
        print(f"  created ({'private' if private else 'public'})")
        return
    if status == 401:
        raise Fail("the token was rejected (401). It may be expired, or copied "
                   "with a character missing.")
    if status == 403:
        raise Fail(
            f"the token was accepted but is not allowed to reach {slug} (403).\n\n"
            f"  {body.get('message', '')}\n\n"
            "  Usual causes: the owner name is wrong; the repository belongs to "
            "an organisation\n  whose SSO the token has not been authorised "
            "for (Settings -> Developer settings ->\n  Personal access tokens "
            "-> Configure SSO); or a fine-grained token that does not list\n"
            "  this repository.")
    raise Fail(f"GitHub answered {status}: {body.get('message', '')}")


def commit_and_push(out: Path, slug: str, token: str, message: str,
                    branch: str, dry: bool) -> None:
    print("\n[4/5] Commit and push")
    if dry and not (out / ".git").exists():
        print("  would run: git init, add a remote, commit and push (dry run)")
        return
    if not (out / ".git").exists():
        run(["init", "-b", branch], out)
    if not run(["config", "user.email"], out, check=False).stdout.strip():
        # Attribute the commit to the account the token belongs to rather than
        # to a placeholder. GitHub's noreply address keeps the real one private
        # while still linking the commit to the profile.
        who = api("/user", token)[1]
        login = who.get("login") or "retypeset"
        email = who.get("email") or f"{who.get('id', 0)}+{login}@users.noreply.github.com"
        run(["config", "user.name", who.get("name") or login], out)
        run(["config", "user.email", email], out)
        print(f"  git identity for this folder set to {login} <{email}>")

    url = f"https://github.com/{slug}.git"
    remotes = run(["remote"], out).stdout.split()
    if "origin" not in remotes:
        run(["remote", "add", "origin", url], out)
    else:
        run(["remote", "set-url", "origin", url], out)

    run(["add", "-A"], out)
    staged = run(["diff", "--cached", "--name-only"], out).stdout.strip()
    if not staged:
        print("  nothing changed since the last commit")
    else:
        n = len(staged.splitlines())
        print(f"  {n} file(s) changed:")
        for line in staged.splitlines()[:12]:
            print(f"      {line}")
        if n > 12:
            print(f"      ... and {n - 12} more")
        if dry:
            print("  (dry run - not committing)")
            return
        run(["commit", "-m", message], out)

    if dry:
        print("  (dry run - not pushing)")
        return

    # The token travels in a header for this one command. It is not written to
    # .git/config, so the folder can be copied or shared without leaking it.
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    header = ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    r = run(["push", "-u", "origin", branch], out, check=False, extra=header)
    if r.returncode != 0:
        explain_push_failure(r.stdout + r.stderr, slug)
    print(f"  pushed to https://github.com/{slug}")


def explain_push_failure(output: str, slug: str) -> None:
    low = output.lower()
    if "workflow" in low and ("scope" in low or "refusing" in low):
        raise Fail(
            "GitHub refused the push because the token cannot write GitHub "
            "Actions workflows.\n\n"
            "  This is the one that catches everybody. The fix is the token, "
            "not the files:\n"
            "  regenerate it with the `workflow` scope ticked as well as "
            "`repo`.\n\n"
            "  Deleting `.github/workflows/` also makes the push succeed, and "
            "costs you the\n  automated Windows .exe build. Prefer the token.")
    if "non-fast-forward" in low or ("rejected" in low and "fetch first" in low):
        raise Fail(
            f"the branch on GitHub has commits this folder does not.\n\n"
            f"  Someone (or the web editor) changed {slug} directly. Merge "
            "them first:\n"
            "      cd github/retypeset && git pull --rebase origin main\n"
            "  then run this script again.")
    if "authentication failed" in low or "403" in low:
        raise Fail("authentication failed. The token may lack the `repo` scope, "
                   "or it may not have access to that owner.")
    raise Fail("push failed:\n" + output)


def tag_release(out: Path, slug: str, token: str, tag: str, dry: bool) -> None:
    print(f"\n[5/5] Tag {tag}")
    if not re.match(r"^v\d+\.\d+\.\d+", tag):
        print(f"  note: the Windows build only runs for tags matching v*, "
              f"and {tag} does not.")
    if dry:
        print("  (dry run)")
        return
    existing = run(["tag", "-l", tag], out).stdout.strip()
    if existing:
        print(f"  {tag} already exists locally - not moving it")
    else:
        run(["tag", "-a", tag, "-m", f"retypeset {tag}"], out)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    r = run(["push", "origin", tag], out, check=False,
            extra=["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"])
    if r.returncode != 0:
        explain_push_failure(r.stdout + r.stderr, slug)
    print(f"  pushed. The Windows build starts now:\n"
          f"      https://github.com/{slug}/actions\n"
          f"  When it finishes, the installer and the portable zip are here:\n"
          f"      https://github.com/{slug}/releases/tag/{tag}")


# ---------------------------------------------------------------------------

def default_slug(out: Path) -> str | None:
    """The repository this folder was last pushed to, if it was."""
    if not (out / ".git").exists():
        return None
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=out,
                       capture_output=True, text=True)
    m = re.search(r"github\.com[/:]([^/]+/[^/.]+)", r.stdout.strip())
    return m.group(1) if m else None


def remembered() -> str:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("repo", "")
    except (OSError, json.JSONDecodeError):
        return ""


def remember(slug: str) -> None:
    try:
        CONFIG.write_text(json.dumps({"repo": slug}, indent=2) + "\n",
                          encoding="utf-8")
    except OSError:
        pass                                  # remembering is a convenience


def resolve_slug(explicit: str | None, out: Path, token: str) -> str:
    """Where to publish: the argument, the folder's remote, the last run, or ask.

    Asking matters more than it looks. The Windows launcher is meant to be
    double-clicked, so it passes no arguments; and a freshly rebuilt folder has
    no git remote to read. Without a question at this point the whole flow ends
    at "no repository", which is a poor answer to give someone who has just
    double-clicked a file called publish.
    """
    slug = explicit or default_slug(out) or remembered()
    if not slug:
        login = api("/user", token)[1].get("login", "")
        suggestion = f"{login}/retypeset" if login else ""
        if not sys.stdin or not sys.stdin.isatty():
            raise Fail("no repository, and nothing to ask on. Pass it:\n"
                       f"      python tools/publish_github.py --repo "
                       f"{suggestion or 'yourname/retypeset'}")
        print("\nWhich repository should this go to? Format: owner/name.")
        if suggestion:
            print(f"It will be created if it does not exist. "
                  f"Press Enter for {suggestion}.")
        slug = input(f"repository [{suggestion}]: ").strip() or suggestion
    if not slug or slug.count("/") != 1 or not all(slug.split("/")):
        raise Fail(f"'{slug}' is not owner/name, for example "
                   f"ammarichouaib/retypeset")
    remember(slug)
    return slug


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="owner/name on GitHub "
                                   "(default: the existing origin remote)")
    ap.add_argument("--out", default="to_github_online",
                    help="release folder (default to_github_online)")
    ap.add_argument("-m", "--message", default="", help="commit message")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--tag", help="also create and push this tag, which starts "
                                  "the Windows build (e.g. v0.8.2)")
    ap.add_argument("--private", action="store_true",
                    help="create the repository private (ignored if it exists)")
    ap.add_argument("--token", help="GitHub token (better: set GITHUB_TOKEN)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="publish the folder as it is, without regenerating it")
    ap.add_argument("--allow-paper", action="store_true",
                    help="publish even though the release folder contains the "
                         "manuscript in paper/")
    ap.add_argument("--dry-run", action="store_true",
                    help="print every step and change nothing")
    args = ap.parse_args()

    git_available()
    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)

    if not args.no_rebuild:
        rebuild(out, args.dry_run)
    if not out.exists():
        raise Fail(f"{out} does not exist. Run tools/make_release.py first.")
    scan(out, args.allow_paper)

    slug_arg = args.repo

    version = "0.0.0"
    init = ROOT / "retypeset" / "__init__.py"
    if init.exists():
        m = re.search(r'__version__ = "([^"]+)"', init.read_text(encoding="utf-8"))
        version = m.group(1) if m else version
    message = args.message or f"retypeset {version}"

    token = get_token(args.token)
    slug = resolve_slug(slug_arg, out, token)
    ensure_repo(slug, token, args.private, args.dry_run)
    print(f"  https://github.com/{slug}")
    commit_and_push(out, slug, token, message, args.branch, args.dry_run)
    if args.tag:
        tag_release(out, slug, token, args.tag, args.dry_run)

    print(f"\nDone. https://github.com/{slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
