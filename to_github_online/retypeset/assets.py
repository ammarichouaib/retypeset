"""
retypeset.assets -- prepare figure files for a target output format.

Word tolerates image formats that LaTeX cannot place, and stores vector art in
forms neither LaTeX nor a fresh DOCX can use directly. This module normalises
them, and is honest about what it could not convert rather than emitting a
document with missing figures.

Conversion matrix for the LaTeX route:

    svg   -> pdf   (cairosvg, then rsvg-convert / Inkscape as fallbacks)
    tif   -> png   (Pillow)
    bmp   -> png   (Pillow)
    gif   -> png   (Pillow, first frame)
    emf   -> pdf   (Inkscape or LibreOffice if present; otherwise reported)
    wmf   -> pdf   (same)
    png/jpg/pdf/eps -> unchanged

EMF and WMF are the only formats with no dependency-free path. They are Windows
metafiles, and every open-source converter for them is approximate. Rather than
ship a silently wrong figure we report them and let the author re-export.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Formats pdfLaTeX can \includegraphics directly.
PDFLATEX_OK = {".pdf", ".png", ".jpg", ".jpeg"}
# EPS needs latex->dvips or epstopdf; modern TeX Live handles it via epstopdf.
EPS_OK = {".eps", ".ps"}


@dataclass
class ConversionResult:
    source: Path
    output: Path | None
    converted: bool
    method: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.output is not None


def _which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _svg_to_pdf(src: Path, dst: Path) -> tuple[bool, str, str]:
    try:
        import cairosvg  # noqa: PLC0415

        cairosvg.svg2pdf(url=str(src), write_to=str(dst))
        if dst.exists() and dst.stat().st_size > 0:
            return True, "cairosvg", ""
    except Exception as exc:
        last = f"cairosvg: {exc}"
    else:
        last = "cairosvg produced an empty file"

    exe = _which("rsvg-convert")
    if exe:
        r = subprocess.run([exe, "-f", "pdf", "-o", str(dst), str(src)],
                           capture_output=True, text=True)
        if r.returncode == 0 and dst.exists():
            return True, "rsvg-convert", ""
        last = f"rsvg-convert: {r.stderr.strip()[:200]}"

    exe = _which("inkscape")
    if exe:
        r = subprocess.run([exe, str(src), "--export-type=pdf",
                            f"--export-filename={dst}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and dst.exists():
            return True, "inkscape", ""
        last = f"inkscape: {r.stderr.strip()[:200]}"

    return False, "", last


def _raster_to_png(src: Path, dst: Path) -> tuple[bool, str, str]:
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(src) as im:
            if im.mode in ("P", "LA", "PA"):
                im = im.convert("RGBA")
            elif im.mode == "CMYK":
                # CMYK TIFFs are common from journals; converting to RGB is
                # lossy in principle but required for PNG.
                im = im.convert("RGB")
            im.save(dst, "PNG", dpi=im.info.get("dpi", (300, 300)))
        return True, "pillow", ""
    except Exception as exc:
        return False, "", f"pillow: {exc}"


def _metafile_to_pdf(src: Path, dst: Path) -> tuple[bool, str, str]:
    exe = _which("inkscape")
    if exe:
        r = subprocess.run([exe, str(src), "--export-type=pdf",
                            f"--export-filename={dst}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and dst.exists():
            return True, "inkscape", ""
    exe = _which("libreoffice", "soffice")
    if exe:
        r = subprocess.run([exe, "--headless", "--convert-to", "pdf",
                            "--outdir", str(dst.parent), str(src)],
                           capture_output=True, text=True, timeout=120)
        produced = dst.parent / (src.stem + ".pdf")
        if produced.exists():
            if produced != dst:
                produced.rename(dst)
            return True, "libreoffice", ""
    return False, "", ("no EMF/WMF converter available (tried Inkscape and "
                       "LibreOffice). Re-export this figure from its original "
                       "application as PDF or 600 dpi PNG.")


def prepare_for_latex(src: Path, out_dir: Path) -> ConversionResult:
    """Return a file pdfLaTeX can include, converting if necessary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()

    if not src.exists():
        return ConversionResult(src, None, False, error="source file missing")

    if ext in PDFLATEX_OK or ext in EPS_OK:
        dst = out_dir / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return ConversionResult(src, dst, False, "copy")

    if ext == ".svg":
        dst = out_dir / (src.stem + ".pdf")
        ok, how, err = _svg_to_pdf(src, dst)
        return ConversionResult(src, dst if ok else None, ok, how, err)

    if ext in (".tif", ".tiff", ".bmp", ".gif"):
        dst = out_dir / (src.stem + ".png")
        ok, how, err = _raster_to_png(src, dst)
        return ConversionResult(src, dst if ok else None, ok, how, err)

    if ext in (".emf", ".wmf"):
        dst = out_dir / (src.stem + ".pdf")
        ok, how, err = _metafile_to_pdf(src, dst)
        return ConversionResult(src, dst if ok else None, ok, how, err)

    # Unknown extension: try Pillow, else give up loudly.
    dst = out_dir / (src.stem + ".png")
    ok, how, err = _raster_to_png(src, dst)
    return ConversionResult(src, dst if ok else None, ok, how,
                            err or f"unsupported format '{ext}'")


def figure_width_fraction(width_mm: float, profile_single_mm: float,
                          profile_double_mm: float, columns: int) -> str:
    """Choose a LaTeX width for a figure placed at `width_mm` in the source.

    Returns a `\\includegraphics` width expression. A figure wider than ~60 % of
    the text block in the source is treated as full width; anything else is
    fitted to the column. This reproduces the author's intent without letting a
    figure overflow the margin, which is the usual failure of naive conversion.
    """
    if not width_mm:
        return r"\linewidth"
    if columns >= 2:
        return r"\textwidth" if width_mm > profile_single_mm * 1.4 else r"\linewidth"
    frac = min(1.0, max(0.25, width_mm / profile_double_mm))
    return rf"{frac:.2f}\linewidth"
