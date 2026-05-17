# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python scraper that exports personal medical journal entries (Anteckningar) from `journalen.1177.se` to Markdown and JSON. BankID authentication is manual — the user completes it in the browser window that Playwright opens.

## Running

```bash
# Activate venv first
source venv/bin/activate

# Run the scraper (opens a real browser window for BankID login)
python scraper.py
```

## Installing dependencies

`requirements.in` lists direct dependencies only (`playwright`, `markdownify`). `requirements.txt` is the full pinned lockfile including transitives.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

To regenerate the lockfile after a version bump:
```bash
pip install pip-tools
pip-compile requirements.in
pip-sync requirements.txt
```

## Architecture

Everything lives in `scraper.py`. The flow is:

1. `do_login()` — opens `journalen.1177.se`, waits (up to 2 min) for BankID redirect
2. `navigate_to_anteckningar()` — clicks "Journalen" then "Anteckningar" in the nav bar (link-based, not URL-hardcoded); real URL is `/JournalCategories/CareDocumentation`
3. `load_all_records()` — waits for JS/AJAX to render the initial 10 records, clicks "Visa alla", then uses `wait_for_function` to poll until the DOM `<li>` count matches `data-cy-value` on the total-number element; `networkidle` fires too early (~49 ms) and must not be relied on here
4. `extract_all_records()` → `expand_and_extract()` — for each `button.nc-list-post-expander`, clicks it, waits for the sibling `div.nc-list-post-container` to lose the `nu-hidden` class and gain content (AJAX per record), converts inner HTML to Markdown
5. `save_results()` — groups records by date; writes one `output/md/YYYY-MM-DD.md` per date and a combined `output/json/journal_YYYY-MM-DD.json`

## Site DOM facts (as of 2026-05)

- Records live in `<ul id="nc-list-posts"><li class="nc-list-post">` — **not** a `<table>`
- Each `<li>` contains a `<button class="nc-list-post-expander" data-id="..." data-date="...">` whose `aria-label` holds date, type, author, and facility as plain text
- Detail content is in the sibling `<div class="nc-list-post-container nu-hidden">`, injected by AJAX on click; starts empty
- `journalen.1177.se` has an independent session from `e-tjanster.1177.se` — always login directly on journalen

## Output

```text
output/
├── md/
│   └── YYYY-MM-DD.md        # one file per date, all records for that day
└── json/
    └── journal_YYYY-MM-DD.json  # all records combined
```

`scraper.log` is written to the project root. The `output/` directory is git-ignored.

Output files contain sensitive medical data — handle accordingly.
