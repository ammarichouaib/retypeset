# Contributing

## Running the tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

The suite uses synthetic fixtures, so it passes on a clean clone. To exercise the
full parser, drop a `.docx` into `tests/samples/` — that directory is gitignored,
because manuscripts under review must not be committed.

## Adding a journal

Copy `profiles/_template.json`, fill in only what the journal's Guide for
Authors actually states, and leave `verified: false` until every number came
from the publisher's own page. Record the source URL of each limit in `sources`.
No code changes are needed.

## Design rules

These are not style preferences; breaking them breaks the tool's guarantee.

1. **Parsing and rendering stay deterministic.** No model in the content path.
   The same manuscript must produce the same output every time.
2. **Never lose content silently.** Anything uncertain becomes a `ParseIssue`.
   If a transformation can drop a block, it needs a test proving it does not.
3. **Keep the raw text.** Every heuristically parsed structure retains its
   verbatim source string and a confidence score.
4. **Profiles are data.** Adding a journal must never require Python.

## Reporting a conversion failure

Open an issue with the *smallest* `.docx` that reproduces it — often a single
paragraph. Include the output of `python run_parse.py file.docx`, which reports
what survived and what did not.
