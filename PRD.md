# PRD: Personal Medical Records Exporter — journalen.1177.se

**Author:** Hisham Ali
**Last updated:** 2026-05-18
**Status:** Complete

---

## Problem

1177.se (Sweden's national healthcare portal) provides access to personal medical records through a web interface. There is no official export feature that produces a portable, structured file. Reading or archiving records requires manual browsing session by session.

The goal is to automate the extraction of all personal journal entries (Anteckningar) and save them locally as human-readable Markdown and machine-readable JSON.

---

## Scope

**In scope**
- Anteckningar (visit notes) for the authenticated user only
- Full note content, not just headers

**Out of scope**
- Other journal sections (Diagnoser, Läkemedel, Remisser, etc.)
- Any other user's records
- Automating the BankID authentication step

---

## Technical Context

| Property | Value |
|---|---|
| Target | `journalen.1177.se` |
| Auth method | Swedish BankID — manual, cannot be automated |
| Anteckningar URL | `/JournalCategories/CareDocumentation` |
| Session scope | Independent from `e-tjanster.1177.se` — must log in on `journalen.1177.se` directly |
| Rendering | JS/AJAX — records load progressively; detail content is injected per click |

---

## Solution

A single-file Python script (`scraper.py`) that drives a real Chromium browser via Playwright. The user completes BankID login manually in the opened window; the script then automates all subsequent navigation and extraction.

### Stack

| Package | Role |
|---|---|
| `playwright` | Browser automation |
| `markdownify` | HTML-to-Markdown conversion |

### Flow

1. Open `journalen.1177.se` — triggers BankID redirect
2. User completes BankID authentication in the browser window (2-minute window)
3. Script detects successful login and navigates to Anteckningar via nav bar links
4. Clicks "Visa alla" to load all record stubs, polls DOM until count matches the total declared by the page
5. Expands each record one at a time, waits for AJAX content to inject, converts to Markdown
6. Groups records by date and writes output files

### Key implementation decisions

| Decision | Reason |
|---|---|
| Poll DOM count instead of `networkidle` | `networkidle` fires in ~49 ms after "Visa alla" — only the first 10 of N records are loaded at that point |
| Navigate via nav bar links, not hardcoded URLs | The site URL structure changed during development; link-based navigation is more resilient |
| Expand records sequentially, not in parallel | AJAX per record; parallel expansion caused DOM state conflicts |
| `aria-label` as metadata source | All structured metadata (date, type, author, facility) is available in the button label without expanding |

---

## Key Findings During Development

| Issue | Root Cause | Resolution |
|---|---|---|
| SSO landed on `LoggedOut/TimedOutResult` | `e-tjanster.1177.se` and `journalen.1177.se` maintain independent sessions | Login directly on `journalen.1177.se` |
| `/Anteckningar` returned `Error/NotFound` | No valid session for journalen subdomain | Fixed by correcting the login entry point |
| Only 10 of N records extracted | `networkidle` fires before progressive AJAX loading completes | Poll DOM `<li>` count against `data-cy-value` total |
| Record detail content empty | `nc-list-post-container` starts hidden (`nu-hidden`) and is AJAX-filled on click | Wait for `nu-hidden` removal and non-empty `innerHTML` after each expand click |

---

## Output

```text
output/
├── md/
│   └── YYYY-MM-DD.md        # One file per date; all records for that day
└── json/
    └── journal_YYYY-MM-DD.json  # All records combined, structured

scraper.log                  # Full run log (project root)
```

---

## Constraints

- BankID authentication is manual — each run requires a fresh login
- Session expires and is not reusable across runs
- Script accesses only the authenticated user's own data (GDPR Art. 15, 20)
- Output files contain sensitive medical data and must be handled accordingly
