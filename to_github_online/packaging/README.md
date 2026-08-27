# Packaging retypeset as a Windows application

The target is a colleague with no Python, no pandoc, no administrator rights and
possibly no internet connection on the machine that does the work.

## What to build

| Artefact | Size | Needs admin | For |
|---|---|---|---|
| `retypeset-<v>-win64.zip` | ~480 MB unzipped, ~170–200 MB zipped | no | lab machines, USB sticks, locked-down desktops |
| `retypeset-setup-<v>.exe` | ~170–200 MB | no (per-user) | anyone who wants a Start-menu entry |
| `+ scikit-learn` (`-WithSklearn`) | +95 MB | — | only if training must run inside the .exe |
| `- pandoc` (`-NoPandoc`) | −~180 MB | — | only if the target machine already has pandoc |

Where the size goes. The first column is **measured** on a Linux build of this
spec (307 MB one-folder, no pandoc, no scikit-learn); the Windows figures track
it closely apart from pandoc, whose Windows binary is larger.

| Component | Measured |
|---|---|
| pyarrow, after pruning Flight and Substrait | 118 MB |
| SciPy + scikit-learn, *if included* | 95 MB |
| Streamlit and its compiled front end | 30 MB |
| pandas | 18 MB |
| numpy, with its bundled BLAS | 42 MB |
| Python runtime, lxml, Pillow, python-docx, pydantic, altair | ~95 MB |
| pandoc.exe (Windows), *if bundled* | ~180 MB |

Two of these are worth knowing about. **pandoc is a third of the download** and
cannot be trimmed: it is a Haskell binary and there is no smaller build. If a
lab already has pandoc, `-NoPandoc` cuts the artefact by that much. **pyarrow is
not our choice**: Streamlit imports it for every dataframe, and the app uses
`st.data_editor` in the Sections table.

Two size traps this spec already avoids, both found by building it:

* **Excluding a package is not the same as not collecting it.** `retypeset.learn`
  imports scikit-learn inside a function, deliberately, so that the app runs
  without it. PyInstaller follows imports inside function bodies anyway, and a
  build that asked for neither scikit-learn nor SciPy shipped 95 MB of both.
  Only an explicit `excludes` entry removes them.
* **Arrow ships libraries for features Streamlit never reaches.** Flight (an RPC
  transport) and Substrait (a query-plan format) are 34 MB together and are
  dropped from `a.binaries`. Anything less obviously unused is left alone: a
  missing Arrow library fails at import, not at first use.

## What has been tested, and what has not

The freeze recipe in `retypeset.spec` was built and run: **307 MB one folder, the
executable starts, and the app serves its page.** That covers the part that
usually breaks — Streamlit's package data, its distribution metadata, and the
submodules it imports by name.

Not yet exercised, because they need Windows: the pandoc download step, the
Inno Setup installer, and the launcher's `%LOCALAPPDATA%` paths. Run the
workflow or the script below and those are covered too — the CI job starts the
frozen executable and fetches a page before it will publish anything.

## Building it

On any Windows machine with Python 3.10+:

```powershell
git clone https://github.com/USERNAME/retypeset.git
cd retypeset
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

The script makes a clean build environment, downloads pandoc, freezes with
PyInstaller, writes the portable zip, and builds the installer if
[Inno Setup 6](https://jrsoftware.org/isdl.php) is present. Roughly ten minutes
on a first run.

**You do not have to own a Windows machine.** `.github/workflows/windows-build.yml`
does exactly the same on GitHub's Windows runners: push a tag (`git tag v0.8.0 &&
git push --tags`) and both artefacts are attached to the release, or run the
workflow by hand from the Actions tab. This is the recommended route — it also
smoke-tests the frozen app by starting it and fetching a page, which catches the
usual PyInstaller failure where the executable imports fine and then serves
nothing.

## How the freeze works

A Streamlit app is a *script that Streamlit executes*, not a program with a
`main()`. So `app.py` is shipped as data and `packaging/launcher.py` starts the
Streamlit runtime on it from inside the executable. The launcher also points
`$PANDOC` at the bundled binary, sends Streamlit's state to
`%LOCALAPPDATA%\retypeset`, turns off telemetry, picks a free port so a second
copy can run, and opens the browser once the server is actually listening.

One-folder, not one-file: a one-file build re-extracts ~400 MB to a temporary
directory on every launch.

## Things that will go wrong, and what they mean

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: streamlit.runtime.scriptrunner.magic_funcs` | Streamlit imports parts of itself by name; the spec lists them under `hiddenimports`. Add any new one there. |
| The window opens, the browser shows nothing | The app is running but the port was taken. The launcher scans 8501–8520; check the console line it prints. |
| `PandocError: pandoc not found` in the frozen app | The build ran with `-NoPandoc`, or the download step failed. Check for `pandoc\pandoc.exe` next to `retypeset.exe`. |
| Antivirus quarantines the .exe | Unsigned PyInstaller binaries are a common false positive. UPX compression is deliberately off because it makes this much worse. A code-signing certificate is the real fix; for a university tool, distributing the zip and telling people where it came from is usually enough. |
| Users' saved profiles disappear after an update | Profiles derived in the app are written next to the executable. Under `Program Files` that path is read-only for a standard user, so the installer's per-user default matters. |

## Alternatives, if 400 MB is too much

1. **Streamlit Community Cloud or Hugging Face Spaces** — zero install, a URL to
   share, `packages.txt` already lists the apt packages needed. Rules it out only
   if manuscripts may not leave the institution, which for work under review is
   often the case.
2. **One shared machine on the lab network.** `streamlit run app.py
   --server.address 0.0.0.0` and everyone uses a browser. One install, no
   packaging, and the manuscripts stay inside the building.
3. **CLI only.** `run_parse.py` and `run_render.py` need neither Streamlit nor
   pyarrow — a frozen CLI is about 220 MB with pandoc, 40 MB without.
