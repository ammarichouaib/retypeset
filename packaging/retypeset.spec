# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build.

    pyinstaller packaging/retypeset.spec --noconfirm

One-folder, not one-file. A one-file build unpacks ~500 MB to a temporary
directory on every launch, which on a lab machine with a slow disk is a
twenty-second wait before anything appears, and it re-extracts every time. The
one-folder build starts in about three seconds and is what the installer ships.

Streamlit needs three things a default PyInstaller build does not give it:
its own package data (the compiled front end lives inside the wheel), its
distribution metadata (it reads its own version at import), and every submodule
of `streamlit.runtime`, which is imported by name at run time and therefore
invisible to static analysis.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path(os.path.abspath(SPECPATH)).parent

datas = []
binaries = []
hiddenimports = []

# --- Streamlit and the packages it loads dynamically ----------------------
for pkg in ("streamlit", "altair", "pyarrow"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

for dist in ("streamlit", "altair", "pyarrow", "pandas", "numpy",
             "python-docx", "lxml", "pillow", "pydantic"):
    try:
        datas += copy_metadata(dist)
    except Exception:                       # optional dependency, not fatal
        pass

# scikit-learn is optional: without it retypeset falls back to the rule-based path
# with no change in behaviour, and dropping it removes roughly 90 MB. Set
# RETYPESET_WITH_SKLEARN=1 before building to include it.
#
# Not collecting it is NOT the same as leaving it out. PyInstaller's analysis
# follows imports inside function bodies too, so `retypeset.learn`'s deliberately
# lazy `import sklearn` pulled the whole of scikit-learn and SciPy into the
# build anyway -- measured at 90 MB in a build that had asked for neither. Only
# an explicit exclude actually removes them.
_WITH_SKLEARN = os.environ.get("RETYPESET_WITH_SKLEARN") == "1"
_excludes = ["matplotlib", "tkinter", "IPython", "notebook", "pytest",
             "sphinx", "torch", "tensorflow"]
if _WITH_SKLEARN:
    for pkg in ("sklearn", "scipy", "joblib"):
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
else:
    _excludes += ["sklearn", "scipy", "joblib"]

hiddenimports += [
    "streamlit.runtime.scriptrunner.magic_funcs",
    "streamlit.web.cli",
    "retypeset", "ui",
]

# --- our own files, shipped as data so Streamlit can execute app.py -------
# Filtered by existence rather than listed blindly: PyInstaller aborts the whole
# build on one missing path, and `app_classic.py` or `README.md` being absent
# from a trimmed checkout is not a reason to fail a release build. app.py is
# checked separately below, because without it there is nothing to run.
_OURS = [
    ("app.py", "."),
    ("app_classic.py", "."),
    ("retypeset", "retypeset"),
    ("ui", "ui"),
    ("profiles", "profiles"),
    ("README.md", "."),
]
if not (ROOT / "app.py").exists():
    raise SystemExit(f"retypeset.spec: app.py not found in {ROOT} - "
                     "run PyInstaller from the repository root.")
datas += [(str(ROOT / src), dst) for src, dst in _OURS if (ROOT / src).exists()]

# --- pandoc, if the build script fetched it -------------------------------
pandoc = ROOT / "build" / "pandoc" / "pandoc.exe"
if pandoc.exists():
    datas += [(str(pandoc), "pandoc")]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Cut what Streamlit pulls in but retypeset never uses. matplotlib alone is
    # ~60 MB and is imported only by paper/make_figures.py, which is not part
    # of the application.
    excludes=_excludes,
    noarchive=False,
)

# Arrow ships several large libraries for features Streamlit does not touch.
# Flight is an RPC transport and Substrait a query-plan format; neither is
# reachable from `st.dataframe`, which needs only in-memory table conversion.
# Together they are ~34 MB of a 436 MB build. Anything less obviously unused is
# left alone: a missing Arrow library fails at import, not at first use, and a
# build that dies on startup to save 2 MB is a bad trade.
_ARROW_UNUSED = ("arrow_flight", "arrow_substrait")
a.binaries = [x for x in a.binaries
              if not any(k in os.path.basename(x[0]).lower()
                         for k in _ARROW_UNUSED)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="retypeset",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX on Python DLLs is a known source of
    console=True,              # false-positive antivirus reports
    icon=str(ROOT / "packaging" / "retypeset.ico")
        if (ROOT / "packaging" / "retypeset.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="retypeset",
)
