# retypeset — journal-agnostic manuscript reformatting

**Status: end to end. Parse → verify → check → generate DOCX and LaTeX.**

```
DOCX ──parse──> IR (JSON) ──+ journal style profile──> DOCX | LaTeX
      ^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^
      implemented, verified   implemented               implemented
```

Generated LaTeX compiles to PDF with **zero errors** for both test manuscripts
under `elsarticle` (16 and 38 pages) and the Diagnostyka profile (10 and 25
pages). Restyled DOCX preserves native equation, table and picture counts
exactly.

## Install

**Windows, no Python required.** Download from the
[latest release](https://github.com/USERNAME/retypeset/releases/latest):

| File | What it is |
|---|---|
| `retypeset-setup-<version>.exe` | Installer. Per-user by default, so it needs no administrator rights. |
| `retypeset-<version>-win64.zip` | Portable. Unzip anywhere and run `retypeset.exe`. |

Both bundle Python and pandoc; nothing else has to be installed, and everything
runs offline on the machine. The only feature that touches the network is the
optional model peer-review panel, and only when you enter an API key. The
download is around 150 MB because pandoc is half of it — see
[`packaging/README.md`](packaging/README.md) for the size breakdown and how to
build it yourself.

**From source** — any platform, and the only way to get the CLI tools:

```bash
pip install -r requirements.txt
python -m streamlit run app.py                  # the app
python run_parse.py "MyPaper.docx" -o parsed    # or the CLI
```

## Quick start

The app opens on **Start**: drop in your manuscript, then either pick one of the
28 built-in journal profiles or upload the publisher's own Word template and let
retypeset derive a profile from it. Four steps follow — **Start, Verify, Check,
Generate** — and the sidebar's **Advanced** mode shows every panel as a tab, plus
local training and the profile the checker is actually using.

If you see `PandocError: pandoc not found`, the fastest fix is `pip install pypandoc_binary`
— it bundles a pandoc binary and needs no admin rights or PATH changes. `retypeset` also
probes `$PANDOC`, PATH, `%LOCALAPPDATA%\Pandoc` and `%PROGRAMFILES%\Pandoc`, so a
normal MSI install works too (open a **new** terminal afterwards).

## Why the previous app produced poor results

The old `app.py` pipeline was `docx → para.text → LLM → LaTeX`. Three fatal properties:

1. **`para.text` returns `""` for OMML equations**, skips `doc.tables` entirely, and never touches images. The model therefore never saw the equations, tables or figures — it invented replacements.
2. **The LLM regenerated body prose.** Non-deterministic rewriting of a peer-reviewed manuscript is disqualifying regardless of model quality.
3. **No intermediate representation.** N input formats × M journals needs N×M converters without one.

`retypeset` inverts this. The LLM is removed from the content path entirely; it is reserved for *labelling* (assigning a `SectionRole`) and for *offline* guide-for-authors ingestion. Rendering is deterministic.

## Modules

| File | Role |
|---|---|
| `retypeset/ir.py` | The intermediate representation. Publisher-neutral semantics only — never fonts, margins or columns. |
| `retypeset/oox.py` | Direct OOXML inspection. **Ground truth for figures and text**, because Pandoc is lossy (see below). |
| `retypeset/parse_docx.py` | Pandoc (prose, math, tables) + `oox` (assets, loss recovery) → IR. Entirely rule-based. |
| `retypeset/audit.py` | Counts primitives in the raw OOXML and compares against the IR. Nothing gets rendered until this is clean. |
| `retypeset/profile.py` | `JournalProfile` schema + loader for `profiles/*.json`. |
| `retypeset/compliance.py` | Validates an IR against a profile — the part that pays for itself before any renderer exists. |
| `profiles/*.json` | One file per journal. **Adding a journal requires no code.** |
| `retypeset/assets.py` | Figure conversion: SVG→PDF, TIFF→PNG, EMF→PDF. |
| `retypeset/render_latex.py` | IR + profile → compilable LaTeX project. |
| `retypeset/render_docx.py` | Restyles the original .docx from a profile; never rebuilds it. |
| `retypeset/template_docx.py` | Transplants a publisher's own .docx/.dotx template. |
| `retypeset/cleanup.py` | Strips the previous journal's logo, headers, ISSN lines and boilerplate. |
| `retypeset/template_profile.py` | Derives a journal profile from an uploaded `.docx`/`.dotx`: page setup and styles are read, structural limits are mined from the template's own author instructions. |
| `app.py` | The app: four-step wizard, plus an Advanced mode with every panel as a tab. |
| `ui/` | The panels themselves (`start`, `verify`, `check`, `produce`, `training`). Each is a plain function of `(manuscript, profile)`, so the wizard and the tabbed mode draw the same objects rather than two copies. |
| `app_classic.py` | The previous ten-tab console, kept runnable for reference. |
| `run_parse.py` | Parse + audit CLI. |
| `run_render.py` | Parse + check + generate CLI. |

`run_parse.py` outputs `parsed/<stem>.ir.json`, `parsed/<stem>.audit.txt` and
`parsed/media/`. Exit code is 0 only when the audit reports no blocking issues.

## The app

Four steps, in the order the work has to happen.

| Step | What happens |
|---|---|
| **1 Start** | The manuscript, and the target. The target is either a built-in profile (filterable by publisher) or **the publisher's own template, uploaded here**. One button parses and moves on. |
| **2 Verify** | Fidelity (what the reader lost), front matter, sections, figures, references. Nothing downstream is trustworthy until this is right, which is why it is a step and not an optional tab. |
| **3 Check** | Compliance against the target, submission readiness, and optionally a panel of model referees whose findings must quote your text verbatim. |
| **4 Generate** | Restyled `.docx` (from your template, or from the profile), a compilable LaTeX project, and the verified IR. |

**Advanced** in the sidebar replaces the wizard with every panel as a tab and
adds two more: **Training**, and **Profile**, which shows the profile in force
exactly as the checker sees it. The panels are the same functions; only the
navigation differs.

### Upload your own template — from step 1

A profile can say *Times New Roman 10 pt, two columns*. It cannot reproduce a
publisher's title block, author blocks, abstract run style or caption styles.
Those live in the template, so the template is offered first rather than last.

Uploading one at step 1 does two things:

1. **Derives a profile from it.** Page size, margins, columns, body font and
   size, line spacing and line numbering are *read* from the file. Structural
   limits are *mined* from the instructions publishers leave inside their own
   templates — "The abstract must be between 150–250 words", "a minimum of three
   to a maximum of six keywords". Every derived value is shown next to the
   sentence it came from, and the whole profile is marked `verified: false`, so
   every rule reports as a warning rather than a failure. Nothing is guessed to
   fill a field: an unmatched limit stays unset and fires no finding at all.
2. **Keeps the file for step 4**, where its styles are transplanted into your
   manuscript.

Optionally seed the derivation from a publisher baseline: the template then
overrides only what it actually proves. Tick *Save this as a reusable profile*
and it becomes a normal `profiles/*.json`, available on every future run.

Measured on the two templates in `templates/`: the IEEE template yields two
columns, Times New Roman, its exact margins, a 250-word abstract limit, numeric
references and Arabic heading numbering; the ELECTRICA template yields A4, 12 pt,
25 mm margins, a 250-word abstract limit and 3–6 keywords — the last two written
three lines apart and spelled out in words. Both are covered by tests.

## Adding a journal

Copy `profiles/_template.json`, rename it, fill it in, restart. No code changes.

```bash
cp profiles/_template.json profiles/applied_energy.json
```

Set `id` to the filename stem, then **delete every key you cannot verify** —
omitted keys fall back to the schema defaults in `retypeset/profile.py` and produce
no false failures. Leave `verified: false` until every number came from the
publisher's own page; unverified profiles report warnings only.

Most journals need almost nothing. Per-journal variation is overwhelmingly just
abstract length, keyword count and required sections, so the usual workflow is
to copy the publisher baseline (`elsevier_generic.json`, `ieee_transactions.json`,
`springer_sn.json`, `mdpi.json`) and change three or four numbers. Files whose
name starts with `_` are ignored by the loader.

Faster still: upload the journal's template at step 1 and save the derived
profile.

### What ships

28 profiles across 12 publishers:

| Publisher | Profiles |
|---|---|
| Elsevier | `elsevier_generic`, `softwarex`, `applied_energy`, `renewable_energy`, `energy_conversion_management`, `energy_reports`, `journal_energy_storage`, `solar_energy`, `heliyon` |
| IEEE | `ieee_transactions`, `ieee_access`, `ieee_tie`, `ieee_tpel`, `ieee_tste` |
| Springer Nature | `springer_sn`, `springer_energy_generic`, `scientific_reports` |
| MDPI | `mdpi`, `mdpi_energies`, `mdpi_sustainability`, `mdpi_applied_sciences` |
| Others | `wiley_generic`, `taylor_francis_generic`, `acs_generic`, `plos_one`, `frontiers_generic`, `joss`, `diagnostyka` |

All the new ones carry `verified: false` and a `guide_url`. Their limits were
transcribed from the publishers' author guides rather than re-read from the live
pages at build time, so they report warnings only. Check the numbers you care
about against the guide and flip the flag — that is a one-line edit, and it is
what `verified` is for.

## Generating output

```bash
python run_render.py "MyPaper.docx" -j elsevier_generic -o rendered
python run_render.py --list                       # available journals
python run_render.py "MyPaper.docx" -j ieee_transactions --only latex
```

Or the **Generate** tab in the review console.

### Bring your own template (best fidelity for Word)

```bash
python run_render.py "MyPaper.docx" -j ieee_transactions \
    -t templates/ieee_template.docx --only docx
```

Or upload it in the **Generate** tab.

A profile can say *Times New Roman 10 pt, two columns*. It cannot reproduce a
publisher's title block, author/affiliation blocks, abstract run style, Roman
section numbering, caption styles or theme fonts — those live in the template's
`styles.xml`, `theme1.xml` and `numbering.xml`. Publishers already ship those
files, so the highest-fidelity path is to **transplant** the template rather
than describe it.

Transplanted: `styles.xml` (merged), `docDefaults`, `theme1.xml`, `numbering.xml`,
page size, margins, column layout.
Untouched: every equation, image, table, footnote and field code.

Two things this gets right that are easy to get wrong:

- **Styles are matched by name, not by styleId.** Word only guarantees ids are
  unique *within* a document. One test manuscript used ids `a`, `1`, `2`, … so
  an id-keyed merge matched nothing: the template's `Normal` was appended as an
  unused second style while the body kept its original look. Matching on name
  took that file from 0 to 24 styles actually overridden.
- **The manuscript's styleId is kept** when a match is found, because every
  paragraph in the body already points at it. Inter-style references
  (`basedOn`, `next`, `link`) are remapped into the manuscript's id space.

### The two routes work differently, on purpose

| | DOCX | LaTeX |
|---|---|---|
| Method | **restyles your original file** | **builds from the IR** |
| Equations | untouched, native OMML | converted to LaTeX by Pandoc |
| Figures/tables | untouched | re-emitted, figures converted |
| Risk | essentially none | conversion gaps, all logged |

**DOCX restyles rather than rebuilds** because `python-docx` cannot write OMML.
Rebuilding would force every equation through LaTeX → MathML → OMML, which needs
Word's own `MML2OMML.XSL`; any gap in that chain turns an equation into a picture
or into plain text. On a manuscript with 134 equations that is not a risk worth
taking. So the restyler changes only presentation — fonts, sizes, spacing,
margins, column count, line numbering, heading and caption styles — and never
touches content.

Verified: for both test manuscripts against all five profiles, the OOXML
equation, table and picture counts in the output are **identical** to the source.

LaTeX has no equivalent option, so it is built from the IR and every conversion
is recorded in `tex/BUILD.md`.

### Asset conversion (LaTeX route)

| From | To | Tool |
|---|---|---|
| svg | pdf | cairosvg → rsvg-convert → Inkscape |
| tif/bmp/gif | png | Pillow |
| emf/wmf | pdf | Inkscape or LibreOffice, if installed |
| png/jpg/pdf/eps | unchanged | — |

EMF and WMF are the only formats with no dependency-free path. Rather than ship
a silently wrong figure, they are reported.

### Removing the previous journal's furniture

Restyling changes presentation, not content — and a manuscript prepared for one
journal carries a lot of that journal's identity *as content*:

- running headers and footers with the journal name and citation line
- the journal logo, sitting above the title as an inline image
- e-ISSN / DOI placeholders, `Vol. xx, No. x`, `20xx`
- a copyright or Creative Commons footnote
- leftover template instructions the author never deleted

Restyled to Elsevier's rules, a Diagnostyka manuscript came out in correct
single-column double-spaced form with line numbers — and still had the
Diagnostyka logo on page 1 and its citation header on every page. The formatting
was right; the document was unusable.

`retypeset.cleanup` removes this by default on both Word routes (`--keep-furniture`
to disable). It is pattern-matched and every removal is reported — on that file:
5 boilerplate paragraphs, 1 masthead logo, 6 headers, 1 licence footnote, and
177 paragraphs whose inherited two-column indents were cleared.

### Column layout: do not force it

An early version set `w:cols num="2"` on every section for a two-column journal.
That is wrong, and it visibly broke the first IEEE output: a two-column
manuscript is not two columns throughout — the title, authors and abstract span
the full page, and a continuous section break switches the body to two columns.
Forcing the count collapses the title block into the left column.

The rule is asymmetric, because the risk is:

- **Collapsing to one column is always safe** — one column *is* full width, so
  there is no title block to destroy. Always applied.
- **Expanding to two or more** needs the guard: if the document already varies
  its column count, only the multi-column sections are normalised.

A first attempt made this symmetric, which then left single-column journals like
Elsevier with a two-column manuscript. Both directions are now tested.

Also: setting `w:num` alone is not enough. When `w:equalWidth="0"` the element
carries explicit `<w:col>` children giving each column's width, and those win —
a document rewritten to `w:num="1"` while still holding a half-width `<w:col>`
renders as two columns. The children are cleared.

### What neither route does

- **Citation style conversion.** Plain-text markers cannot be reliably converted
  between numeric and author-year.
- **Section reordering.** Restyling never moves content.
- **Equations Pandoc could not read.** Flagged as `degenerate_math`, marked in
  the LaTeX with `\blacksquare` and `[retypeset: retype this equation]`.

## A failure worth documenting: the paper with no body

A manuscript reformatted to IEEEtran produced a `main.tex` that compiled
without a single error and contained the title, the abstract, the keywords and
the bibliography — and none of the paper. Three defects lined up, and the
combination is instructive because each one alone is survivable:

1. **The title was styled `Heading 1`.** Every real section was therefore
   nested beneath it, and the section tree had exactly one top-level node.
2. **A front-matter role skipped the whole subtree.** Marking that node
   `title` in the Sections panel — the obvious choice, since it *is* the title —
   made the renderer return nothing for the node *and its descendants*.
3. **Nothing counted what reached the page.** The render was reported as
   successful, and LaTeX, having been handed a valid document, compiled it.

All three are fixed. The parser now recognises the title-as-`Heading 1` idiom
and promotes the sections beneath it (`title_wrapper_unwrapped`); the renderer
skips a front-matter section but never its children; and every render reports
what actually reached the file, compared against the manuscript row by row in
`BUILD.md` and shown in the Generate panel:

| | in the manuscript | in `main.tex` |
|---|---|---|
| Body sections | 7 | 7 |
| Figures | 12 | 12 |
| Tables | 5 | 5 |
| Display equations | 12 | 12 |

An empty body is now a red error in the app, not a silent success. Covered by
`tests/test_latex_body.py`, which asserts that no front-matter role can delete
a nested section for any of the five roles that could reach that code path.

The same manuscript exposed four smaller defects, all fixed: affiliation
markers written as Unicode superscripts (`Ammari*¹`) were never matched by the
ASCII marker regexes, so they stayed glued to the surname and the author picked
up no affiliation; the IEEE author block emitted names only, dropping
affiliations that were parsed and present in the IR; a run-in `Keywords:` line
was printed inside the abstract *and* through the keyword macro, and counted
against the journal's abstract word limit; and `refs.bib` was written with 1 of
38 entries while `main.tex` cheerfully suggested switching to it.

### Fitting a single-column manuscript into a two-column class

The same manuscript surfaced three layout defects, all of them the same
mistake: deciding a width from the wrong evidence.

| Symptom in the PDF | Cause | Fix |
|---|---|---|
| A four-column table printed over the text in the next column | floats spanned both columns only when they had **more than four columns** — a count, not a width | the widest row is measured in characters; anything wider than a column spans, and cells are set in fixed-width `p{}` columns so they wrap |
| Text vanishing past the right edge of the page | display equations were emitted as one line whatever their length | two formulas joined by `\quad\quad` (the single-column Word habit) are broken into an `aligned` block; a single formula that is genuinely too wide is scaled with `\resizebox` and reported in `BUILD.md` |
| `(3) (3)` after every equation | the author's typed number was kept *and* LaTeX added its own | the author's number is stripped; LaTeX's is the one `\ref` agrees with |
| Every figure squeezed into one column, or printed across the neighbour | the placed width was passed as a hard-coded `0.0` | the figure's own shape decides: wider than 1.8:1 spans both columns, anything squarer stays in one |

The table's column widths subtract `2\tabcolsep` per column through `\dimexpr`
rather than leaving a percentage of slack: at six columns the padding is a
seventh of the page, which is exactly how a table sized at "94 % of the width"
overflows by 41 pt with nothing in the log but an overfull box.

Measured on the manuscript that prompted this, IEEEtran, 12 pages:
**10 overfull boxes (worst 41 pt) → 1 (0.5 pt), 0 errors, 0 warnings.**

## Setting sections yourself

Heading detection is guesswork whenever the author applied no heading styles,
which is most of the time. On a real manuscript one section ended up holding
**76 blocks** — most of the paper — because the author had used only a handful
of headings.

*(An earlier version of this note blamed the heuristic for promoting a sentence
to a heading. That was wrong: checking the OOXML shows the author had styled it
`heading 1` themselves. The parser was right; the manuscript simply has coarse
sections. The lesson stands — you often want finer boundaries than the file
records — but the tool was not at fault.)*

No amount of tuning removes that class of error — the information is genuinely
absent from the file. So the Sections tab has three modes:

- **Guided** (default) — the flow Wiley's submission system uses: one section at
  a time, the manuscript text shown with the candidate range highlighted, adjust,
  **Confirm**, advance. The steps are the sections the target journal requires,
  plus whatever was already detected.
- **Quick** — assign roles to the sections that were detected.
- **Table** (advanced) — every block at once with a `heading` checkbox, level
  and role.

All three go through `flatten()` → `rebuild()` / `apply_ranges()`, which are
lossless by construction: every block appears exactly once in the flat list and
exactly once in the rebuilt tree, in the same order. Asserted in testing —
124 blocks in, 124 out, on every path.

Three failure modes that testing caught and the code now prevents:

| Problem | Why it matters |
|---|---|
| A heading used as a section title was *also* emitted as its own empty section | "Abstract" appeared twice, once with no content |
| A role with no detected section defaulted to the top of the document | one careless Confirm assigned the title block as the abstract |
| Applying a selection reset every untouched section to `unknown` | confirming the abstract silently un-labelled the introduction and references |

## Can it be trained?

Partly, and *which* parts matters.

**Not trainable, and must never be:** reading OMML, extracting figures, counting
tables, restyling a DOCX, emitting LaTeX. These are deterministic
transformations of a known file format. A model there would make the same input
give different output on two runs — disqualifying for a manuscript — and its
mistakes would be silent.

**Genuinely learnable**, because the information is ambiguous even to a careful
human reading one paragraph at a time:

1. **Is this paragraph a heading?** Inferred from shape: length, capitalisation,
   numbering, terminal punctuation, stopword density.
2. **What role does the heading play?** *"Protection of a Very High Voltage
   (VHV) Line Span"* is a methods section, and no keyword lexicon will ever
   contain it.

Both are small text-classification problems: logistic regression over character
n-grams plus ~15 structural features. Hundreds of examples, not millions;
milliseconds on a CPU; coefficients you can read when it misbehaves. Character
n-grams rather than words, so a French or Polish manuscript still works.

Training runs **in the app**: sidebar **Local training** for the one-button
version, **Advanced → Training** for the full panel — how much data you have and
what each model still needs, the seed corpus, folder harvesting, the training run
with its cross-validated scores, a box to try a line of text against the trained
model, and the raw `corrections.jsonl`. That is where the corrections are
produced, so that is where training belongs; the earlier version put it behind a
CLI nobody remembered to run, and the corrections simply accumulated.

The CLI is still there for scripted runs and headless machines:

```bash
pip install scikit-learn joblib
python train_local.py --seed               # ~340 conventional heading/body examples
python train_local.py --harvest ./papers   # mine .docx you already have
python train_local.py --train
python train_local.py --all                # seed, train, then train the finding filter
python train_local.py --test "Protection of a Very High Voltage Line Span"
python train_local.py --reset              # drop the models, keep the data
```

### You do not need to find 50 papers

**Word heading styles are free ground truth.** A manuscript whose author applied
`Heading 1` has already labelled its own headings. Two real papers yielded 41
headings and 219 body lines — 253 labelled examples — at zero annotation cost.
`--harvest` reads any folder of `.docx` you already have. Files without heading
styles are skipped, because labelling them with the same heuristic the model is
meant to replace teaches it nothing.

For more, the legitimate bulk sources are the **PubMed Central Open Access
Subset** (JATS XML, where `<sec sec-type>` gives the role directly) and
**arXiv**. Bulk-downloading paywalled PDFs is not, which is why `build_corpus.py`
does not do it.

### Measured results, including the failure

Corpus: 591 examples (338 seed + 253 harvested from two papers).

| Model | Cross-validated |
|---|---|
| Heading detector | **F1 0.885** |
| Role classifier | accuracy **0.50** over the 21 roles with ≥2 examples |

The role classifier is weak — 222 examples across 22 classes is about ten each.
It is used only above 0.75 confidence, so most of its guesses are discarded.

**The first trained model made the output worse**, and the fix is worth
recording. It promoted `Zone 1:` and `V  Voltage [V];` to headings and labelled
them `keywords` at 91 % confidence. Two causes:

1. The seed corpus was all *long* body text and *short* headings, so the model
   learned "short and capitalised means heading". Fixed by adding ~70 short
   non-headings — nomenclature entries, zone labels, captions, units.
2. More fundamentally, a confident model was allowed to *override* the rules.
   It no longer can. Structural disqualifiers — trailing semicolon, a units
   bracket, `Fig. 3.`, an e-mail — are knowledge character n-grams cannot
   acquire from a few hundred examples, so the rules keep the veto and the model
   only breaks ties the rules leave open.

After the fix the model adds two correct labels the lexicon cannot reach
(`List of Symbols/Acronyms` → nomenclature; *Protection of a Very High Voltage
(VHV) Line Span* → methods) and introduces no spurious headings.

Training data is **your own corrections**: every heading you fix in the Precise
editor is appended to `models/corrections.jsonl`. Only rows whose value actually
changed are recorded — feeding the heuristic's own guesses back in would teach
the model to reproduce the mistakes it exists to fix.

Everything runs locally; nothing is uploaded. Trained models are picked up
automatically on the next parse, and retypeset falls back to the rule-based path
unchanged when they are absent. The model only overrides the rules above 70 %
confidence, and role predictions below 55 % are discarded.

Smoke-tested on 123 synthetic examples: heading F1 1.00 (inflated — templated
data leaks across folds), role accuracy 0.73. Expect the real numbers to be
lower and the heading detector to be the more useful of the two.

## Hosting it online, privately

See [`DEPLOY.md`](DEPLOY.md). Four options, cheapest first, with the two
constraints that actually decide it: unpublished manuscripts on a third-party
host, and the fact that Streamlit has no multi-user isolation.

## Journal profiles

A profile is data, never code. The scaling argument rests on this: ~15 template
families cover most journals, and per-journal variation is almost entirely
reference style, abstract limits and section structure. So a journal is a thin
JSON file naming a family and overriding a few numbers.

| Profile | Verified | Notable rules |
|---|---|---|
| `elsevier_generic` | yes | 90/140/190 mm columns; 300 dpi halftone, 500 combination, 1000 line art → **1063 px** at single column; highlights 3–5 × ≤85 chars |
| `ieee_transactions` | yes | 600 dpi line art, 300 dpi photos, 400 dpi colour; pixel floor 1050 px column-wide / 2150 px page-wide |
| `mdpi` | yes | abstract ≤200 words, one paragraph, no citations or equations; requires author contributions, funding, data availability, conflict of interest |
| `springer_sn` | no | inferred; verify per journal |
| `diagnostyka` | no | inferred from the journal's own Word template |

`verified: false` profiles never report `fail`, only `warn` — a false rejection
wastes more of your time than a missed one.

Every numeric limit carries a `source` URL in the JSON.

## What testing on a real manuscript found

Verified against `DIAGNOSTYKA_MEHRAZ.docx` (13 images, 24 equations, 2 tables, 35 references).

**Pandoc alone is not a sufficient DOCX reader.** Two independent silent-loss modes, reproduced in both Pandoc 2.9 and 3.9:

| Loss | Effect on that file |
|---|---|
| Images dropped without warning | 4 of 13 lost (3 EMF + 1 PNG) |
| Text-box / shape paragraphs dropped wholesale | 4 figure captions lost |

Neither raises a diagnostic. This is precisely why `retypeset.oox` reads the OOXML directly and `_check_text_loss` diffs the source paragraph corpus against the IR corpus, recovering anything Pandoc discarded.

**Final measured fidelity:**

| Metric | Result |
|---|---|
| Equations (OMML → LaTeX) | 24 / 24 |
| Tables | 2 / 2 |
| Embedded images | 13 / 13 |
| Word retention | 98.2 % (residual = headers/footers and fragments < 25 chars) |
| Duplicated paragraphs | 1 |
| Figure captions recovered | 7 / 13 (6 have none in the source — 5 are author photographs) |

The equation conversion is clean, e.g.

```latex
Z_{2} = R_{2} + jX_{2} = Z_{\text{AB}} + 0.2 \bullet Z_{\text{AB}}
      = \left( R_{\text{AB}} + jX_{\text{AB}} \right)
      + 0.2 \bullet (R_{\text{BC}} + jX_{\text{BC}})
```

## Second test manuscript (10 500 words, hydrogen/RE techno-economics)

A harder file, and it exposed three things the first one could not.

**1. Equation-numbering tables.** Word has no numbered-equation construct, so
authors use an invisible two-column table — equation left, `(n)` right. In this
manuscript that was **30 of 32 tables and 69 of 134 equations**. Treating them as
tables inflated the table count, left 69 equations unnumbered, fired caption
warnings on 25 non-tables, and would have made a renderer emit `tabular` where
LaTeX needs `\begin{equation}`. `_as_equation_layout()` now recognises the
pattern; it requires every row to match *and* at least one printed number, so
genuine data tables with a symbol column (`tab5`–`tab8` here) are untouched.

**2. SVG vector originals.** Word 2016+ stores an SVG as an extension hanging off
a raster fallback (`asvg:svgBlip` inside `a:blip`). Reading only `a:blip` yields
the PNG fallback and never the vector. 10 of this file's 15 figures are SVG —
they now resolve to the vector, which also exempts them from DPI checks.

**3. Two counting bugs in the audit itself**, both of which reported loss that
had not happened:

| Metric | Was | Cause | Now |
|---|---|---|---|
| Equations | 70 / 134 | inline math counted only in top-level blocks, not table cells | 134 / 134 |
| Images | 15 / 25 | ground truth counted relationship references; each SVG figure consumes two | 15 / 15 |

Tables now read 32 / 32, and the real table count is 8.

Compliance failures dropped from 4 to 1 (IEEE) — the remaining one is genuine:
`fig4` at 1002 px, `fig5` and `fig6` at 720 px, all under IEEE's 1050 px floor.

## Compliance results for the first test manuscript

Same IR, five journals, no re-parsing:

| Journal | Pass | Warn | **Fail** |
|---|---|---|---|
| Elsevier (elsarticle) | 4 | 5 | **4** |
| IEEE Transactions | 4 | 6 | **3** |
| MDPI | 3 | 7 | **3** |
| Springer (sn-jnl) | 4 | 10 | 0 (unverified → warnings only) |
| Diagnostyka | 4 | 9 | 0 (unverified → warnings only) |

The Elsevier failures are: abstract split across 2 paragraphs, no highlights,
3 EMF figures, and 9 figures below 1063 px. All four are decidable from the IR
alone and all four would have cost a submission cycle.

## Manuscript defects the audit surfaced

Independent of tooling, that file will fail technical checks at most publishers:

- **All 9 raster figures are below 300 dpi at single-column width** (102–715 px; ≥ 1063 px required). Elsevier, IEEE and Springer all reject at this stage.
- **3 figures are EMF.** pdfLaTeX cannot include them, and Word renders them unpredictably outside Windows. Convert to PDF (vector) or 600 dpi PNG.
- **Figure order is inconsistent** — "Fig. 4" appears in the text before "Fig. 3".
- **The bibliography is hand-typed**, with no Zotero/Mendeley/EndNote field codes, and 16 of 35 entries carry no DOI. Automatic numeric ↔ author-year conversion is unreliable until the bibliography is re-linked in a reference manager.
- **No heading styles are applied**; 3 headings had to be recovered from bold/numbered paragraphs, and only 3 of 9 top-level sections match the canonical role lexicon.

## Publishing this repository

### The folders, in plain language

| Folder | What it is |
|---|---|
| `retypeset/`, `ui/`, `profiles/`, `tests/` | the software |
| `paper/` | your SoftwareX manuscript. **Never published unless you ask.** |
| `templates/`, `parsed/`, `models/` | publisher templates, working output, your training data. Local only. |
| `packaging/` | how to build the Windows `.exe`. `packaging/ci/` holds optional GitHub automation, switched off by default. |
| **`to_github_online/`** | **the only folder that goes to GitHub.** Generated; never edit it by hand. |

Nothing is called `github` or `.github` any more. `.github` is a name GitHub
itself requires for automation, and it is created inside `to_github_online/`
only when you ask for it with `--with-ci`.

### Making the upload folder

The working folder holds things that must not be published: two real
manuscripts under `tests/samples/`, your own manuscript in `paper/`,
`.streamlit/secrets.toml`, parsed output from other people's papers, and
training data derived from your files. `git add -A` publishes all of it, and
GitHub keeps deleted blobs reachable by hash, so it cannot be taken back.

`tools/make_release.py` copies only what is named in an allowlist — not an
ignore list — and prints what it withheld and why, every run:

```bash
python tools/make_release.py --check      # list, copy nothing
python tools/make_release.py              # -> to_github_online/  (83 files, 0.9 MB)
python tools/make_release.py --with-paper # include the manuscript, deliberately
python tools/make_release.py --with-ci    # include GitHub's automated .exe build
```

The destination is cleared before each run, keeping only `.git`. Without that,
a folder built once with `--with-paper` would keep publishing the manuscript
from every later command that never mentioned it.

A fresh clone has no manuscripts and no publisher templates. Every test that
needs them skips, so CI still passes: **107 passed, 6 skipped** on a clean
checkout.

### Publishing in one command

```bash
python tools/publish_github.py --dry-run              # every step, no changes
python tools/publish_github.py                        # publish
```

On Windows, `publish_to_github.bat` runs the same thing. It rebuilds the upload
folder, scans it independently for anything private, creates the repository if
needed, commits and pushes. If `paper/` is present it stops and asks, even
though you built the folder yourself.

The token is read from `GITHUB_TOKEN`, from `gh auth token`, or asked for
without echoing, and is passed to one `git push` as an `http.extraheader` — so
it never reaches `.git/config`, the remote URL, or your shell history. Scope
`repo` is enough; add `workflow` only if you publish with `--with-ci`.

## Design invariants

1. **Lossless body text.** No model ever rewrites `InlineNode.text`.
2. **Raw fallback.** Every heuristically parsed structure (references, captions, authors) keeps its verbatim source string plus a confidence score.
3. **Fail loudly.** Anything uncertain becomes a `ParseIssue`; nothing is silently guessed.

## Next steps, in order

0. **Verify the shipped profiles.** 23 of the 28 were transcribed rather than
   read from the live guide, and until each is checked they are advisory only.
1. **Section-role labelling.** The lexicon resolves 3 of 9 sections here. This is the one place an LLM belongs at runtime — classification only, constrained to the `SectionRole` enum, with the result shown for confirmation.
2. **Reference ingestion.** Replace the regex parser with AnyStyle or GROBID → CSL-JSON, then Crossref lookup by title to fill DOIs. Once references are CSL-JSON, restyling across journals is `citeproc` + a CSL file, and the Zotero style repository already covers roughly 10,000 journals.
3. **Style-profile schema** (~40 declarative fields per journal) + profiles for 3 target journals.
4. **Renderers.** `python-docx` into the publisher's own `.dotx`; Jinja2 (with `\VAR{}`/`\BLOCK{}` delimiters, since `{{ }}` collides with TeX) into `elsarticle` / `IEEEtran`.
5. **Asset pipeline.** EMF/WMF → PDF conversion, DPI validation, single/double-column resizing.

## Scope note

Elsevier, Springer Nature, Wiley and IEEE all accept *format-free* initial submissions; strict formatting binds at revision and acceptance. And roughly 15 template families (elsarticle, IEEEtran, sn-jnl, Wiley, MDPI, T&F, ACS, RSC, APS, AIP, IOP, …) cover the large majority of journals — per-journal variation is mostly reference style, abstract limits and section structure.

So the target is **~15 renderers plus a few thousand thin JSON profiles**, not thousands of templates. The defensible wedge is high-fidelity **DOCX → DOCX** restyling with equations, figures and citations intact, which is currently poorly served.
