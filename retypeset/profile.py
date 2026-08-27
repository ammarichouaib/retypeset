"""
retypeset.profile -- declarative journal style profiles.

A profile is data, never code. Adding a journal must never require touching
Python, because the whole scaling argument rests on this: roughly fifteen
template families (elsarticle, IEEEtran, sn-jnl, MDPI, ...) cover the large
majority of journals, and per-journal variation is almost entirely reference
style, abstract limits and section structure. So a journal is a thin JSON file
that names a template family and overrides a handful of numbers.

Profiles live in `profiles/*.json` next to the package. Drop a new file in and
it appears in the UI on the next run.

Every numeric limit carries a `source` URL in the JSON so a reviewer can check
it, and `verified` marks whether the value was read from the publisher's own
guidelines or inferred. Unverified values are surfaced as advisory rather than
blocking, because a false rejection is worse than a missed one.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .ir import SectionRole

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


def user_profile_dir() -> Path:
    """Second profile directory, in a location the user can always write to.

    The packaged Windows build installs under `Program Files`, where a standard
    user cannot create files. A profile derived from a template would then be
    silently lost -- or, worse, appear to save and vanish on the next launch.
    Profiles are therefore also read from, and by default written to, a
    per-user directory. `RETYPESET_PROFILES` overrides it, which is what a shared
    lab folder of agreed profiles would use.
    """
    env = os.environ.get("RETYPESET_PROFILES")
    if env:
        return Path(env)
    base = (os.environ.get("LOCALAPPDATA")            # Windows
            or os.environ.get("XDG_DATA_HOME")        # Linux
            or str(Path.home() / ".local" / "share"))
    return Path(base) / "retypeset" / "profiles"


def writable_profile_dir() -> Path:
    """Where a newly derived profile should go: beside the others if possible."""
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        probe = PROFILE_DIR / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return PROFILE_DIR
    except OSError:
        d = user_profile_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d


class FigureRules(BaseModel):
    """Artwork requirements, expressed at final printed size.

    Resolution requirements are always *at publication size*: a figure drawn at
    three times final size and 300 dpi is effectively 100 dpi in print, which is
    the single most common reason artwork fails automated quality control.
    """

    single_column_mm: float = 90.0
    one_half_column_mm: float | None = 140.0
    double_column_mm: float = 190.0
    dpi_halftone: int = 300
    dpi_combination: int = 500
    dpi_line_art: int = 1000
    dpi_colour: int | None = None
    # Some publishers state a pixel floor directly instead of a dpi rule.
    min_px_single_column: int | None = None
    min_px_double_column: int | None = None
    accepted_formats: list[str] = Field(
        default_factory=lambda: ["tif", "tiff", "eps", "pdf", "png", "jpg", "jpeg"]
    )
    rejected_formats: list[str] = Field(
        default_factory=lambda: ["emf", "wmf", "bmp", "gif"]
    )
    caption_position: Literal["above", "below"] = "below"
    table_caption_position: Literal["above", "below"] = "above"

    def required_px(self, target_mm: float, art: str = "halftone") -> int:
        dpi = {
            "halftone": self.dpi_halftone,
            "combination": self.dpi_combination,
            "line": self.dpi_line_art,
        }.get(art, self.dpi_halftone)
        return round(target_mm / 25.4 * dpi)


class StructureRules(BaseModel):
    abstract_max_words: int | None = None
    abstract_min_words: int | None = None
    abstract_paragraphs_max: int | None = None
    abstract_structured: bool = False
    keywords_min: int | None = None
    keywords_max: int | None = None
    highlights_required: bool = False
    highlights_min: int | None = None
    highlights_max: int | None = None
    highlights_max_chars: int | None = None
    title_max_chars: int | None = None
    manuscript_max_words: int | None = None
    required_sections: list[SectionRole] = Field(default_factory=list)
    forbidden_sections: list[SectionRole] = Field(default_factory=list)
    # Roles that must appear, in this relative order, if present at all.
    section_order: list[SectionRole] = Field(default_factory=list)


class LatexRules(BaseModel):
    document_class: str = "article"
    class_options: list[str] = Field(default_factory=list)
    bibliography_style: str = ""
    preamble_packages: list[str] = Field(default_factory=list)
    template_family: str = "generic"


class DocxRules(BaseModel):
    body_font: str = "Times New Roman"
    body_size_pt: float = 10.0
    line_spacing: float = 1.0
    columns: int = 1
    page_size: Literal["a4", "letter"] = "a4"
    margins_mm: dict[str, float] = Field(
        default_factory=lambda: {"top": 25, "bottom": 25, "left": 25, "right": 25}
    )
    line_numbers: bool = False
    heading_numbering: Literal["arabic", "roman", "none"] = "arabic"
    template_file: str = ""       # optional .dotx / .docx to inherit styles from


class ReferenceRules(BaseModel):
    style: Literal["numeric", "author-year", "numeric-superscript"] = "numeric"
    csl_file: str = ""            # a file name from the Zotero CSL repository
    doi_required: bool = False
    max_references: int | None = None
    in_text_format: str = "[n]"


class JournalProfile(BaseModel):
    id: str
    journal: str
    publisher: str
    template_family: str = "generic"
    homepage: str = ""
    guide_url: str = ""
    verified: bool = False
    notes: str = ""

    # Terms from the journal's own aims-and-scope page. Used only to measure
    # topical overlap with the title, keywords and abstract -- never the body,
    # where common vocabulary would drown the signal. Leave empty and the scope
    # check is skipped rather than guessed at.
    scope_keywords: list[str] = Field(default_factory=list)

    structure: StructureRules = Field(default_factory=StructureRules)
    figures: FigureRules = Field(default_factory=FigureRules)
    references: ReferenceRules = Field(default_factory=ReferenceRules)
    latex: LatexRules = Field(default_factory=LatexRules)
    docx: DocxRules = Field(default_factory=DocxRules)

    # Per-field provenance so the UI can show where a limit came from.
    sources: dict[str, str] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        mark = "" if self.verified else "  (unverified)"
        return f"{self.publisher} - {self.journal}{mark}"


def _load_dir(d: Path) -> dict[str, JournalProfile]:
    out: dict[str, JournalProfile] = {}
    if not d.exists():
        return out
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue          # `_template.json` and other scaffolding
        try:
            p = JournalProfile.model_validate_json(f.read_text(encoding="utf-8"))
        except Exception as exc:  # a malformed profile must not kill the app
            raise ValueError(f"invalid profile {f.name}: {exc}") from exc
        out[p.id] = p
    return out


@lru_cache(maxsize=4)
def load_profiles(directory: str | Path | None = None) -> dict[str, JournalProfile]:
    """Load every profile, keyed by id.

    With no argument: the shipped profiles, then the per-user directory, which
    overrides on collision -- a local correction to a shipped profile should
    win, and should not require editing a file inside the installation.

    Given a directory, only that directory is read; tests and the CLI rely on
    that isolation.

    Cached, because the UI calls this on every rerun. Call
    `load_profiles.cache_clear()` after writing a profile to disk.
    """
    if directory:
        return _load_dir(Path(directory))
    out = _load_dir(PROFILE_DIR)
    out.update(_load_dir(user_profile_dir()))
    return out


def get_profile(profile_id: str) -> JournalProfile:
    profiles = load_profiles()
    if profile_id not in profiles:
        raise KeyError(f"unknown journal profile '{profile_id}'. "
                       f"Known: {', '.join(sorted(profiles))}")
    return profiles[profile_id]
