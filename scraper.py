import asyncio
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from markdownify import markdownify as md_convert
from playwright.async_api import async_playwright, Page

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JOURNALEN_URL  = "https://journalen.1177.se"
OUTPUT_DIR     = Path("output")
MD_DIR         = OUTPUT_DIR / "md"
JSON_DIR       = OUTPUT_DIR / "json"
TODAY          = datetime.today().strftime("%Y-%m-%d")
LOGIN_TIMEOUT  = 120_000   # 2 min for manual BankID
NAV_TIMEOUT    = 30_000
EXPAND_TIMEOUT = 15_000    # per-record AJAX expand

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

MD_DIR.mkdir(parents=True, exist_ok=True)
JSON_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Record:
    date:        str = ""
    record_type: str = ""
    author:      str = ""
    facility:    str = ""
    record_id:   str = ""
    content_md:  str = ""

# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def do_login(page: Page) -> None:
    log.info("Opening journalen.1177.se...")
    await page.goto(f"{JOURNALEN_URL}/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    log.info(f"Redirected to: {page.url}")
    log.info("Complete BankID login in the browser window (2 min timeout)...")

    await page.wait_for_url(
        re.compile(r"journalen\.1177\.se/(?!LoggedOut)"),
        timeout=LOGIN_TIMEOUT,
    )
    log.info(f"Logged in. URL: {page.url}")


async def navigate_to_anteckningar(page: Page) -> None:
    """Click Journalen → Anteckningar in the nav bar."""
    log.info("Clicking 'Journalen' in the navigation bar...")
    nav_link = page.get_by_role("link", name=re.compile(r"^Journalen$", re.I))
    if await nav_link.count() == 0:
        nav_link = page.locator("a").filter(has_text=re.compile(r"^Journalen$", re.I))
    if await nav_link.count() == 0:
        raise RuntimeError("No 'Journalen' link found.")

    await nav_link.first.click()
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
    log.info(f"Journalen: {page.url}")

    if "CareDocumentation" not in page.url:
        ant_link = page.get_by_role("link", name=re.compile(r"Anteckningar", re.I))
        if await ant_link.count() == 0:
            ant_link = page.locator("a").filter(has_text=re.compile(r"Anteckningar", re.I))
        if await ant_link.count() == 0:
            raise RuntimeError("No 'Anteckningar' link found.")
        await ant_link.first.click()
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)

    log.info(f"Anteckningar URL: {page.url}")
    if any(x in page.url for x in ["LoggedOut", "NotFound", "login"]):
        raise RuntimeError(f"Navigation failed: {page.url}")


async def load_all_records(page: Page) -> int:
    """
    Wait for all records to appear in the DOM.

    The page first renders 10 records with 'Visa 10 till' / 'Visa alla' controls.
    Clicking 'Visa alla' triggers multiple AJAX requests that progressively add
    <li> elements. We wait until the DOM count matches total-number.

    Returns the total record count.
    """
    log.info("Waiting for initial records to render...")
    try:
        await page.wait_for_selector(
            "#nc-list-posts li:not(.nc-loading-spinner-row)",
            timeout=NAV_TIMEOUT,
        )
    except Exception:
        log.error("Records never appeared.")
        return 0

    total_str = await page.locator("[data-cy-id='total-number']").get_attribute("data-cy-value")
    total = int(total_str or "0")
    log.info(f"Total records: {total}")

    if total == 0:
        return 0

    # Click 'Visa alla' if present; otherwise fall back to 'Visa 10 till' loop
    load_all = page.locator("button.load-all")
    load_more = page.locator("button.load-more")

    if await load_all.is_visible() and not await load_all.is_disabled():
        log.info("Clicking 'Visa alla' to request all records...")
        await load_all.click()
    elif await load_more.is_visible() and not await load_more.is_disabled():
        while await load_more.is_visible() and not await load_more.is_disabled():
            log.info("Clicking 'Visa 10 till'...")
            await load_more.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)

    # Wait until all <li> elements are in the DOM (up to 60 s)
    log.info(f"Waiting for all {total} records to appear in the DOM...")
    try:
        await page.wait_for_function(
            f"""() => document.querySelectorAll(
                '#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row)'
            ).length >= {total}""",
            timeout=60_000,
        )
    except Exception:
        actual = await page.locator(
            "#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row)"
        ).count()
        log.warning(f"Timeout — only {actual}/{total} records loaded. Proceeding with {actual}.")
        total = actual

    log.info(f"All {total} records loaded in DOM.")
    return total


def parse_expander_label(label: str) -> tuple[str, str, str, str]:
    """Extract date, record_type, author, facility from the button aria-label."""
    date        = re.search(r"Datum\s+([\d-]+)", label)
    record_type = re.search(r"anteckningstyp\s+([^,]+)", label, re.I)
    author      = re.search(r"antecknad av\s+([^,]+)", label, re.I)
    facility    = re.search(r"på\s+([^,.]+)", label, re.I)
    return (
        date.group(1).strip()        if date        else "",
        record_type.group(1).strip() if record_type else "",
        author.group(1).strip()      if author      else "",
        facility.group(1).strip()    if facility    else "",
    )


async def expand_and_extract(page: Page, index: int, total: int) -> Record:
    """
    Click record at `index`, wait for its AJAX content to load, return a Record.
    The detail container starts empty with class 'nu-hidden'. After clicking the
    expander button, the content is injected and 'nu-hidden' is removed.
    """
    expander_sel = (
        "#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row) "
        "button.nc-list-post-expander"
    )
    btn = page.locator(expander_sel).nth(index)

    label       = await btn.get_attribute("aria-label") or ""
    record_id   = await btn.get_attribute("data-id") or ""
    date, record_type, author, facility = parse_expander_label(label)

    log.info(f"  [{index+1}/{total}] {date} — {record_type}")

    # Click to load detail
    await btn.click()

    # The sibling div loses 'nu-hidden' once content arrives
    container = btn.locator("xpath=following-sibling::div[contains(@class,'nc-list-post-container')]")
    try:
        await page.wait_for_function(
            f"""() => {{
                const btns = document.querySelectorAll(
                    '#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row) button.nc-list-post-expander'
                );
                const btn = btns[{index}];
                if (!btn) return false;
                const div = btn.nextElementSibling;
                return div && !div.classList.contains('nu-hidden') && div.innerHTML.trim().length > 0;
            }}""",
            timeout=EXPAND_TIMEOUT,
        )
    except Exception:
        log.warning(f"    Timeout expanding record {index+1}. Content may be empty.")

    content_html = await container.inner_html()
    content_md = md_convert(content_html).strip() if content_html.strip() else ""

    # Collapse before moving to next (keeps DOM clean)
    if await btn.get_attribute("aria-expanded") == "true":
        await btn.click()

    return Record(
        date        = date,
        record_type = record_type,
        author      = author,
        facility    = facility,
        record_id   = record_id,
        content_md  = content_md,
    )


async def extract_all_records(page: Page, total: int) -> list[Record]:
    records = []
    for i in range(total):
        rec = await expand_and_extract(page, i, total)
        records.append(rec)
    return records

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_results(records: list[Record]) -> None:
    if not records:
        log.warning("No records to save.")
        return

    by_date: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        by_date[rec.date].append(rec)

    saved_md = []
    for date, day_records in sorted(by_date.items()):
        safe_date = date.replace("/", "-")
        md_path   = MD_DIR / f"{safe_date}.md"

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Anteckningar — {date}\n\n")
            f.write(f"**Exporterat:** {TODAY}  \n")
            f.write(f"**Antal anteckningar denna dag:** {len(day_records)}\n\n---\n\n")

            for i, rec in enumerate(day_records, 1):
                f.write(f"## {i}. {rec.record_type}\n\n")
                f.write(f"**Datum:** {rec.date}  \n")
                f.write(f"**Antecknad av:** {rec.author}  \n")
                if rec.facility:
                    f.write(f"**Vardgivare:** {rec.facility}  \n")
                f.write("\n")
                f.write(rec.content_md if rec.content_md else "*(Inget innehall extraherat)*")
                f.write("\n\n---\n\n")

        saved_md.append(md_path)
        log.info(f"  {md_path}  ({len(day_records)} post{'er' if len(day_records) != 1 else ''})")

    json_path = JSON_DIR / f"journal_{TODAY}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, ensure_ascii=False, indent=2)

    log.info(f"\nSaved {len(saved_md)} date files and {json_path}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = await (await browser.new_context()).new_page()
        records: list[Record] = []

        try:
            await do_login(page)
            await navigate_to_anteckningar(page)
            total = await load_all_records(page)
            if total > 0:
                records = await extract_all_records(page, total)
        except Exception as e:
            log.error(f"Fatal error: {e}", exc_info=True)
            raise
        finally:
            await browser.close()

        save_results(records)
        log.info(f"Done! Extracted {len(records)} records across {len(set(r.date for r in records))} dates.")


if __name__ == "__main__":
    asyncio.run(main())
