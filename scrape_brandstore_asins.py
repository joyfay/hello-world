#!/usr/bin/env python3
"""Scrape ASINs off Amazon Brand Store pages.

Brand Store product grids are lazy-loaded widgets: the HTML you get from a plain
HTTP GET is a shell, and the products arrive over internal ajax once a widget
scrolls into view. So this drives a real (headless) Chromium via Playwright,
scrolls each page to the bottom, pages through every product grid, and then
harvests ASINs from the settled DOM.

Usage
-----
    pip install playwright
    playwright install chromium

    # one page
    python scrape_brandstore_asins.py --page-id 29AB675D-2238-4826-BDE2-09EFEAB81AA6

    # every page of a store, read from a JSON file (see --pages-file below)
    python scrape_brandstore_asins.py --pages-file ussolid_pages.json -o asins.csv

    # watch it work the first time; Amazon is less likely to challenge a real window
    python scrape_brandstore_asins.py --page-id ... --headful

--pages-file expects what the Ads API `brandStores` query returns, i.e.
    [{"tag": "503F8933-...", "title": "3/4 inch"}, ...]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import sys
from dataclasses import dataclass, field

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

STORE_URL = "https://www.amazon.com/stores/page/{page_id}"

# An ASIN is 10 chars. Modern ones start with B0; older catalog entries are
# ISBN-derived and are all digits. Anything else is some other identifier that
# happens to be 10 chars, and we do not want it.
ASIN_RE = re.compile(r"^(?:B0[A-Z0-9]{8}|\d{10})$")

# Places an ASIN shows up in store markup, in rough order of reliability.
HTML_PATTERNS = [
    re.compile(r"/dp/([A-Z0-9]{10})"),
    re.compile(r"amzn1\.asin\.([A-Z0-9]{10})"),
    re.compile(r'"asin"\s*:\s*"([A-Z0-9]{10})"'),
]

DOM_ATTR_SELECTORS = [
    ("[data-asin]", "data-asin"),
    ("[data-csa-c-asin]", "data-csa-c-asin"),
    ("[data-csa-c-item-id]", "data-csa-c-item-id"),
]

# Real-ish desktop fingerprint. A default Playwright UA gets challenged fast.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class PageResult:
    page_id: str
    title: str = ""
    asins: set[str] = field(default_factory=set)
    error: str = ""


def _clean(raw: str | None) -> str | None:
    """Pull a bare ASIN out of an attribute value, or return None."""
    if not raw:
        return None
    candidate = raw.strip().rsplit(".", 1)[-1].upper()
    return candidate if ASIN_RE.match(candidate) else None


async def _dismiss_interstitials(page: Page) -> None:
    """Close the delivery-location / cookie prompts that eat the first click."""
    for selector in (
        "input[data-action-type='DISMISS']",
        "#sp-cc-accept",
        "button[name='glowDoneButton']",
    ):
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1500):
                await el.click(timeout=2000)
                await page.wait_for_timeout(400)
        except (PWTimeout, Exception):
            pass


async def _is_blocked(page: Page) -> bool:
    """Detect the 'Enter the characters you see below' captcha wall."""
    html = (await page.content()).lower()
    return (
        "enter the characters you see below" in html
        or "/errors/validatecaptcha" in html
        or "sorry, we just need to make sure you're not a robot" in html
    )


async def _auto_scroll(page: Page, max_rounds: int = 40) -> None:
    """Scroll to the bottom in human-sized steps until the height stops growing.

    Each widget mounts when it nears the viewport, so a single jump to the
    bottom loads far less than a walk down the page.
    """
    last_height = 0
    stable_rounds = 0
    for _ in range(max_rounds):
        await page.mouse.wheel(0, random.randint(600, 900))
        await page.wait_for_timeout(random.randint(350, 650))
        height = await page.evaluate("document.body.scrollHeight")
        at_bottom = await page.evaluate(
            "window.innerHeight + window.scrollY >= document.body.scrollHeight - 200"
        )
        if height == last_height and at_bottom:
            stable_rounds += 1
            if stable_rounds >= 2:  # two quiet rounds means it is done growing
                return
        else:
            stable_rounds = 0
        last_height = height


async def _exhaust_grid_pagination(page: Page, max_clicks: int = 60) -> None:
    """Click through every product grid's next-page arrow until they are spent.

    Store product grids paginate in place rather than adding to the page, so
    each click reveals ASINs the previous state never had in the DOM.
    """
    selectors = [
        "button[aria-label*='Next' i]:not([disabled])",
        "a[aria-label*='Next' i]",
        "[data-testid*='pagination'] button:not([disabled])",
        "button:has-text('See more')",
    ]
    clicks = 0
    while clicks < max_clicks:
        clicked = False
        for selector in selectors:
            buttons = page.locator(selector)
            count = await buttons.count()
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if not await btn.is_visible(timeout=800):
                        continue
                    await btn.scroll_into_view_if_needed(timeout=2000)
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(random.randint(700, 1200))
                    clicks += 1
                    clicked = True
                    break
                except (PWTimeout, Exception):
                    continue
            if clicked:
                break
        if not clicked:
            return


async def _harvest(page: Page) -> set[str]:
    """Collect ASINs from both the live DOM and the serialized HTML."""
    found: set[str] = set()

    for selector, attr in DOM_ATTR_SELECTORS:
        for value in await page.eval_on_selector_all(
            selector, f"els => els.map(e => e.getAttribute('{attr}'))"
        ):
            if asin := _clean(value):
                found.add(asin)

    for href in await page.eval_on_selector_all(
        "a[href*='/dp/']", "els => els.map(e => e.getAttribute('href'))"
    ):
        if href and (m := HTML_PATTERNS[0].search(href)):
            if asin := _clean(m.group(1)):
                found.add(asin)

    html = await page.content()
    for pattern in HTML_PATTERNS:
        for match in pattern.findall(html):
            if asin := _clean(match):
                found.add(asin)

    return found


async def scrape_page(page: Page, page_id: str, title: str = "") -> PageResult:
    result = PageResult(page_id=page_id, title=title)
    url = STORE_URL.format(page_id=page_id)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except PWTimeout:
        result.error = "navigation timeout"
        return result

    if await _is_blocked(page):
        result.error = "captcha / bot wall"
        return result

    await _dismiss_interstitials(page)
    await page.wait_for_timeout(1500)

    # Harvest between each interaction: grid pagination replaces DOM nodes, so
    # anything read only at the end would miss every earlier grid page.
    await _auto_scroll(page)
    result.asins |= await _harvest(page)

    await _exhaust_grid_pagination(page)
    result.asins |= await _harvest(page)

    await _auto_scroll(page)
    result.asins |= await _harvest(page)

    if not result.asins and await _is_blocked(page):
        result.error = "captcha / bot wall"

    if not result.title:
        try:
            result.title = (await page.title()).split("|")[0].strip()
        except Exception:
            pass

    return result


async def run(pages: list[dict], headful: bool, out_path: str) -> int:
    results: list[PageResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headful,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        # navigator.webdriver is the cheapest bot tell there is.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        for i, entry in enumerate(pages, 1):
            page_id = entry["tag"]
            title = entry.get("title", "")
            print(f"[{i}/{len(pages)}] {title or page_id} ...", file=sys.stderr, flush=True)

            result = await scrape_page(page, page_id, title)
            results.append(result)

            status = result.error or f"{len(result.asins)} ASINs"
            print(f"    {status}", file=sys.stderr, flush=True)

            if result.error == "captcha / bot wall":
                print(
                    "    hit the bot wall — slow down, or rerun with --headful",
                    file=sys.stderr,
                )
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(random.uniform(2.5, 5.0))

        await browser.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["page_title", "page_id", "asin"])
        for result in results:
            for asin in sorted(result.asins):
                writer.writerow([result.title, result.page_id, asin])

    total = len({a for r in results for a in r.asins})
    failed = [r for r in results if r.error]
    print(f"\n{total} distinct ASINs across {len(results)} pages -> {out_path}", file=sys.stderr)
    if failed:
        print(f"{len(failed)} page(s) failed:", file=sys.stderr)
        for r in failed:
            print(f"  {r.title or r.page_id}: {r.error}", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--page-id", help="a single store page tag (GUID)")
    src.add_argument("--pages-file", help="JSON list of {tag, title} from the Ads API")
    ap.add_argument("-o", "--out", default="brandstore_asins.csv")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    args = ap.parse_args()

    if args.page_id:
        pages = [{"tag": args.page_id, "title": ""}]
    else:
        with open(args.pages_file, encoding="utf-8") as f:
            pages = json.load(f)
        if isinstance(pages, dict):  # tolerate a raw brandStores response
            pages = pages.get("pageInfos", [])

    return asyncio.run(run(pages, args.headful, args.out))


if __name__ == "__main__":
    sys.exit(main())
