"""retypeset -- journal-agnostic manuscript reformatting.

Pipeline:  DOCX --parse--> IR --(+ style profile)--> DOCX | LaTeX

End to end: parse, audit, check against a journal profile, render DOCX or LaTeX.
Nothing downstream runs before `retypeset.audit` reports on the parse, because
every renderer inherits the parser's losses.

A target journal is either a profile in `profiles/*.json` or one derived from the
publisher's own template by `retypeset.template_profile.derive`.
"""

from .ir import Manuscript, Section, SectionRole  # noqa: F401
from .parse_docx import parse_docx, PandocError  # noqa: F401
from .audit import audit, format_report  # noqa: F401
from .profile import JournalProfile, get_profile, load_profiles  # noqa: F401
from .compliance import ComplianceReport, Finding, check  # noqa: F401
from .compliance import format_report as format_compliance  # noqa: F401
from .render_latex import render_latex, RenderResult  # noqa: F401
from .render_docx import render_docx, DocxResult  # noqa: F401
from .template_docx import (  # noqa: F401
    apply_template, inspect as inspect_template, TemplateInfo, ApplyResult,
)

from .template_profile import derive as derive_profile  # noqa: F401

from . import (  # noqa: F401
    agents, cleanup, learn, review, sectioning, template_profile,
)

__version__ = "0.8.3"
__all__ = [
    "Manuscript", "Section", "SectionRole",
    "parse_docx", "PandocError", "audit", "format_report",
    "JournalProfile", "get_profile", "load_profiles",
    "check", "ComplianceReport", "Finding", "format_compliance",
    "render_latex", "RenderResult", "render_docx", "DocxResult",
    "apply_template", "inspect_template", "TemplateInfo", "ApplyResult",
    "template_profile", "derive_profile",
]
