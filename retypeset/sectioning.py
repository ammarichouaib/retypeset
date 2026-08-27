"""
retypeset.sectioning -- rebuild the section tree from explicit human marks.

Why this exists
---------------
Heading detection from a Word file is guesswork whenever the author applied no
heading styles, which is most of the time. The heuristic promotes short bold or
numbered paragraphs, and it makes exactly the mistakes you would expect: on a
real manuscript it promoted the sentence "The Mho Relay detects and localises
faults" to a top-level heading, which then swallowed 76 blocks of body text into
a single bogus section.

No amount of tuning removes that class of error, because the information is
genuinely absent from the file. The reliable fix is to let the author say where
the sections start -- once, in a flat list -- and rebuild the tree from that.

The flatten/rebuild pair below is deliberately lossless: every block in the
manuscript appears exactly once in `flatten()` output and exactly once in the
rebuilt tree, in the same order. Nothing can be dropped by editing marks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ir import Block, Manuscript, Provenance, Section, SectionRole


@dataclass
class Row:
    """One block, flattened out of the section tree."""

    index: int
    block_kind: str
    text: str
    is_heading: bool
    level: int
    role: str
    source: str = ""          # how the current marking was decided
    confidence: float = 0.0
    section_id: str = ""
    block: Block | None = field(default=None, repr=False)
    # Headings are not stored as blocks, so they need their own carrier.
    heading_section: Section | None = field(default=None, repr=False)


def flatten(ms: Manuscript) -> list[Row]:
    """Walk the tree depth-first into a flat, editable list of rows.

    A section's heading becomes a row with `is_heading=True`; its blocks follow.
    """
    rows: list[Row] = []

    def walk(sections: list[Section]) -> None:
        for sec in sections:
            if sec.title_raw:
                rows.append(Row(
                    index=len(rows), block_kind="heading",
                    text=sec.title_raw, is_heading=True,
                    level=max(1, sec.level), role=sec.role.value,
                    source=sec.role_provenance.method,
                    confidence=sec.role_provenance.confidence,
                    section_id=sec.id, heading_section=sec,
                ))
            for b in sec.blocks:
                rows.append(Row(
                    index=len(rows), block_kind=b.kind,
                    text=_block_preview(b), is_heading=False,
                    level=0, role="", section_id=sec.id, block=b,
                ))
            walk(sec.children)

    walk(ms.body)
    return rows


def rebuild(ms: Manuscript, rows: list[Row]) -> Manuscript:
    """Rebuild `ms.body` from edited rows. Mutates and returns `ms`.

    Blocks before the first heading go into an untitled preamble section, so a
    manuscript that starts with body text cannot lose it.
    """
    root = Section(id="s_root", level=0, role=SectionRole.UNKNOWN)
    stack: list[Section] = [root]
    n = 0
    preamble: Section | None = None

    for row in rows:
        if row.is_heading:
            n += 1
            level = min(max(int(row.level or 1), 1), 6)
            try:
                role = SectionRole(row.role) if row.role else SectionRole.UNKNOWN
            except ValueError:
                role = SectionRole.UNKNOWN

            sec = Section(
                id=f"s{n}", level=level, role=role,
                title_raw=row.text.strip(),
                title=(row.heading_section.title if row.heading_section else []),
                numbering_raw=(row.heading_section.numbering_raw
                               if row.heading_section else ""),
                role_provenance=Provenance(
                    method=row.source or "explicit",
                    confidence=row.confidence or 1.0,
                ),
            )
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            stack[-1].children.append(sec)
            stack.append(sec)
            continue

        if row.block is None:
            continue
        if len(stack) == 1:
            if preamble is None:
                preamble = Section(id="s_pre", level=1, role=SectionRole.UNKNOWN)
                root.children.insert(0, preamble)
            preamble.blocks.append(row.block)
        else:
            stack[-1].blocks.append(row.block)

    ms.body = root.children

    # Keep the mirrored abstract in step with whatever is now labelled abstract.
    abs_sec = ms.section_by_role(SectionRole.ABSTRACT)
    if abs_sec:
        ms.meta.abstract = abs_sec.blocks
        ms.meta.abstract_raw = " ".join(
            b.paragraph.plain_text() for b in abs_sec.blocks if b.paragraph
        ).strip()

    ms.stats["sections"] = sum(1 for _ in ms.iter_sections())
    ms.stats["top_level_sections"] = len(ms.body)
    return ms


@dataclass
class Assignment:
    """A contiguous run of rows the user assigned to one role."""

    role: str
    start: int
    end: int                 # inclusive
    title: str = ""

    def covers(self, i: int) -> bool:
        return self.start <= i <= self.end


def suggest_range(rows: list[Row], role: str) -> tuple[int, int] | None:
    """Where the parser currently thinks this section is, as a starting point.

    Returns the inclusive row range of the *body* of the section -- excluding its
    heading, because that is what a submission system asks you to select.
    """
    start = None
    for i, r in enumerate(rows):
        if r.is_heading and r.role == role:
            start = i + 1
            break
    if start is None:
        return None
    end = start
    while end < len(rows) and not rows[end].is_heading:
        end += 1
    end -= 1
    return (start, max(start, end)) if end >= start else None


def apply_ranges(ms: Manuscript, rows: list[Row],
                 assignments: list[Assignment]) -> Manuscript:
    """Rebuild the section tree from explicitly selected ranges.

    Lossless by construction: rows outside every assignment are kept and attached
    to the preceding section, so selecting only the abstract cannot discard the
    rest of the manuscript.

    Overlaps are resolved in favour of the earlier assignment rather than
    rejected, because a user correcting a boundary will often overlap by a row
    and failing the whole operation for that would be obstructive.
    """
    ordered = sorted((a for a in assignments if a.end >= a.start),
                     key=lambda a: a.start)
    claimed: list[Assignment] = []
    last_end = -1
    for a in ordered:
        start = max(a.start, last_end + 1)
        if start > a.end:
            continue
        claimed.append(Assignment(a.role, start, a.end, a.title))
        last_end = a.end

    idx_to_assignment = {a.start: a for a in claimed}

    # A heading directly above a confirmed range becomes that section's title.
    # Without this it is also emitted as a section of its own, producing an empty
    # duplicate immediately before every section the user selected.
    consumed_titles = {
        a.start - 1 for a in claimed
        if a.start - 1 >= 0 and rows[a.start - 1].is_heading
    }

    root = Section(id="s_root", level=0, role=SectionRole.UNKNOWN)
    n = 0
    current: Section | None = None

    for i, row in enumerate(rows):
        a = idx_to_assignment.get(i)
        if a is not None:
            n += 1
            try:
                role = SectionRole(a.role)
            except ValueError:
                role = SectionRole.UNKNOWN
            title = a.title.strip()
            if not title:
                prev = rows[i - 1] if i else None
                title = (prev.text.strip() if prev is not None and prev.is_heading
                         else role.value.replace("_", " ").title())
            current = Section(
                id=f"s{n}", level=1, role=role, title_raw=title,
                role_provenance=Provenance(method="explicit", confidence=1.0,
                                           note="selected by the author"),
            )
            root.children.append(current)

        if row.is_heading and a is None:
            if i in consumed_titles:
                continue            # already used as the next section's title
            if any(x.covers(i) for x in claimed):
                continue            # a sub-heading inside a selected range
            n += 1
            # Carry over whatever role this heading already had. Confirming the
            # abstract must not silently un-label the introduction and the
            # references that the parser had already got right.
            try:
                kept_role = SectionRole(row.role) if row.role else SectionRole.UNKNOWN
            except ValueError:
                kept_role = SectionRole.UNKNOWN
            current = Section(
                id=f"s{n}", level=max(1, row.level), title_raw=row.text.strip(),
                role=kept_role,
                role_provenance=Provenance(
                    method=row.source or "heuristic",
                    confidence=row.confidence,
                    note="carried over; not re-confirmed"),
            )
            root.children.append(current)
            continue

        if row.block is None:
            continue
        if current is None:
            current = Section(id="s_pre", level=1, role=SectionRole.UNKNOWN)
            root.children.append(current)
        current.blocks.append(row.block)

    # Drop sections that ended up with neither a title nor content.
    ms.body = [s for s in root.children if s.blocks or s.title_raw]

    abs_sec = ms.section_by_role(SectionRole.ABSTRACT)
    if abs_sec:
        ms.meta.abstract = abs_sec.blocks
        ms.meta.abstract_raw = " ".join(
            b.paragraph.plain_text() for b in abs_sec.blocks if b.paragraph
        ).strip()

    kw_sec = ms.section_by_role(SectionRole.KEYWORDS)
    if kw_sec:
        joined = " ".join(b.paragraph.plain_text()
                          for b in kw_sec.blocks if b.paragraph)
        parts = [p.strip(" .;–-") for p in
                 __import__("re").split(r"[;,]|•|\|", joined)]
        kws = [p for p in parts if len(p) > 1]
        if kws:
            ms.meta.keywords = kws

    ms.stats["sections"] = sum(1 for _ in ms.iter_sections())
    ms.stats["top_level_sections"] = len(ms.body)
    return ms


def range_training_examples(rows: list[Row],
                            assignments: list[Assignment]) -> list[dict]:
    """Labelled examples from a guided selection.

    The heading immediately above a confirmed range is a confirmed heading with a
    confirmed role -- the single most valuable kind of example. Rows inside the
    range are confirmed body text.
    """
    out: list[dict] = []
    for a in assignments:
        if a.start <= 0 or a.start > len(rows):
            continue
        head = rows[a.start - 1]
        if head.is_heading and head.block_kind in ("heading", "paragraph"):
            text = head.text.strip()
            if len(text) >= 3 and not text.startswith("["):
                out.append({"text": text, "is_heading": True, "role": a.role})
        for i in range(a.start, min(a.end + 1, len(rows))):
            r = rows[i]
            if r.block_kind != "paragraph":
                continue
            text = r.text.strip()
            if len(text) < 25 or text.startswith("["):
                continue
            out.append({"text": text, "is_heading": False, "role": ""})
    return out


def to_table(rows: list[Row], *, max_chars: int = 110) -> list[dict]:
    """Rows as plain dicts for a data editor."""
    return [
        {
            "#": r.index,
            "heading": r.is_heading,
            "level": r.level or 1,
            "role": r.role or SectionRole.UNKNOWN.value,
            "kind": r.block_kind,
            "text": r.text[:max_chars],
        }
        for r in rows
    ]


def from_table(rows: list[Row], table: list[dict]) -> list[Row]:
    """Apply edits from a data editor back onto the row objects.

    Only rows whose value actually changed are marked `explicit`. Marking every
    row as human-confirmed just because the user pressed Save would feed the
    heuristic's own guesses back in as ground truth, and the model would learn
    to reproduce the mistakes it is supposed to fix.
    """
    by_index = {r.index: r for r in rows}
    for rec in table:
        r = by_index.get(int(rec["#"]))
        if r is None:
            continue
        new_heading = bool(rec.get("heading", False))
        new_level = int(rec.get("level") or 1)
        new_role = str(rec.get("role") or SectionRole.UNKNOWN.value)

        touched = (
            new_heading != r.is_heading
            or (new_heading and new_level != r.level)
            or (new_heading and new_role != (r.role or SectionRole.UNKNOWN.value))
        )

        r.is_heading = new_heading
        r.level = new_level
        r.role = new_role
        if touched:
            r.source = "explicit"
            r.confidence = 1.0

        # A block promoted to a heading needs its text as the title.
        if r.is_heading and r.block is not None and r.heading_section is None:
            r.text = (r.block.paragraph.plain_text().strip()
                      if r.block.paragraph else r.text)
    return rows


def _block_preview(b: Block) -> str:
    if b.paragraph:
        return b.paragraph.plain_text().strip()
    if b.kind == "figure_ref":
        return f"[figure {b.target_id}]"
    if b.kind == "table_ref":
        return f"[table {b.target_id}]"
    if b.kind == "equation_ref":
        return f"[equation {b.target_id}]"
    if b.kind == "list" and b.list_block:
        n = len(b.list_block.items)
        first = ""
        if b.list_block.items and b.list_block.items[0]:
            fb = b.list_block.items[0][0]
            first = fb.paragraph.plain_text().strip() if fb.paragraph else ""
        return f"[list of {n}] {first}"
    if b.kind == "code":
        return f"[code] {b.code_text[:60]}"
    return f"[{b.kind}]"


def training_examples(rows: list[Row]) -> list[dict]:
    """Turn the current marks into labelled examples for retypeset.learn.

    Only rows a human touched (`source == "explicit"`) are treated as ground
    truth; heuristic guesses would just teach the model its own mistakes.
    """
    out: list[dict] = []
    for r in rows:
        if r.source != "explicit":
            continue
        # Floats and lists are rendered as "[figure fig3]" placeholders, which
        # are this tool's own notation, not manuscript text. Training on them
        # would teach the model about retypeset rather than about manuscripts.
        if r.block_kind not in ("paragraph", "heading"):
            continue
        text = r.text.strip()
        if len(text) < 3 or text.startswith("["):
            continue
        out.append({
            "text": text,
            "is_heading": bool(r.is_heading),
            "role": r.role if r.is_heading else "",
        })
    return out
