#!/usr/bin/env python3
"""
craigslist-scraper -- Playwright edition (primary engine)
=========================================================

Scrapes Craigslist result lists and postings, across all 714 areas.

    --mode listing   (default)  a result list
                                (/search/{area|subarea}/{slug}?cat=...)
    --mode posting              one /view/d/{slug}/{token} advert: the body
                                text, the attribute bag, every image, both
                                timestamps and the classic numeric post id

There is deliberately no `--country` flag. Craigslist consolidated onto ONE
hostname and the area is a path segment, so a country flag could only
disagree with the URL -- and the currency follows that area rather than the
exit, measured 2026-09-14 by fetching eight areas through a single US
residential exit and getting USD, CAD, EUR, JPY and MXN back.

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money; the shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about Craigslist
----------------------------------
* **The served markup is RICHER than the rendered page**, which is the
  reverse of every sibling repo. Craigslist ships a no-JS results list --
  complete, with a JSON-LD `ItemList` carrying structured price, currency,
  coordinates and images aligned onto it -- and the application then REMOVES
  that list about twenty seconds in, painting a virtualised grid of 200
  recycled nodes in its place.
* **So a single-batch run disables JavaScript.** The list is then never
  removed, the ~27s hydration wait is not paid, and the window in which the
  page has neither view cannot be hit. The browser still does the fetching:
  TLS, headers, cookies, the proxy, the Scraping Browser profile. `--pages
  > 1` turns JavaScript back on, because walking past the first batch is the
  one thing that genuinely needs the site's application.
* **There is no pagination of any kind.** `?page=2` and `?s=296` return a
  BYTE-IDENTICAL response -- measured, same length, same first result. No
  `link[rel=next]`, no page numbers, and the day paginator in the markup is
  inert on every category tested. A `page_url()` built on the family's usual
  convention would re-fetch the first page, find no new sku, and report a
  complete run holding a fraction of the data.
* **`--concurrency` above 1 is refused**, with that reason: a worker is only
  useful if it can be handed an address of its own.
* **Walking the grid inverts the family's scroll advice.** The list is
  virtualised, so its container's height is pre-computed for all 10,000
  items and one jump to `scrollHeight` TELEPORTS to the end -- 700 ids
  collected, after which the page passes every readiness test the usual rule
  prescribes. This engine wheels a bounded step and takes its stall signal
  from the site's own header counter.
* **One URL tops out at exactly 10,000 results.** Past that, narrow the URL
  -- a price band, a subarea, a search term, all of which the site honours as
  real query parameters -- and run each separately.
* **Whole categories publish no structured data.** Jobs, services and
  community ship no `ItemList` at all, and housing ships one with no prices
  in it. Those rows come from the served list alone and their `currency` is
  null rather than guessed: the `$` is ambiguous across the USD, CAD and MXN
  areas this site serves.

Usage
-----
    python playwright_scraper.py \\
        --url "https://www.craigslist.org/search/area/newyork?cat=sss" \\
        --format both

    python playwright_scraper.py \\
        --url "https://www.craigslist.org/search/area/berlin?cat=sss" --pages 3

    python playwright_scraper.py --mode posting \\
        --url "https://www.craigslist.org/view/d/new-york-dji-neo-fly-more-combo-with/qTuXqiGHU326VAGH4msB5A"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
from product_parser import (parse_products, parse_posting,
                            parse_rendered_cards, page_currency, SELECTORS,
                            detect_bot_challenge, listing_kind, site_host,
                            is_supported_host, total_results, search_header,
                            unsupported_reason, served_by_craigslist,
                            RESULT_CAP)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The page_flow state this page came back as ("content", "empty",
    # "unpainted", "blocked", "challenge"). Carried so the caller can tell an
    # EMPTY page -- a query matching nothing is the ordinary way to get one --
    # from a page that failed. Both produce zero rows and they mean opposite
    # things.
    state: Optional[str] = None
    # What the listing said its own size was. Present only on a run that let
    # the application paint: the served markup states no total at all, and the
    # rendered header says "of 10,000+" -- the ceiling one URL can be walked
    # to, not a count of the catalogue.
    total_available: Optional[int] = None
    # The site's OWN description of this listing, verbatim, for the sidecar:
    # "craigslist For Sale in New York City", with the query quoted when there
    # is one. The thing to compare against when a run returns a surprise.
    header: Optional[str] = None
    # What the walk through the virtualised list did: how far the site's own
    # counter got, how many distinct rows were harvested, and whether it hit
    # the 10,000 ceiling. A walk that ran out of budget with the counter still
    # advancing is PARTIAL, and reporting it as complete would read as a
    # shrinking catalogue.
    walk: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy.
#
# Measured across 14 captures: 95%-100% of rows carry a price on
# for-sale-type categories and 100% on housing, jobs, services and community,
# where the price is often the site's own `$0` -- a price the advert states
# rather than one that is missing. 90 sits below every measured value with
# room for a listing unusually full of free items, and far enough above zero
# to catch a read that broke.
PRICE_FLOOR = 90

# The lowest STRUCTURED-enrichment share that is still healthy, applied ONLY
# where the page published structured data at all.
#
# It is deliberately loose because the real figure varies by locale and
# category: 66% (paris) to 99% (toronto) across the for-sale captures, and a
# measured ZERO on jobs, services and community, which publish no `ItemList`
# whatsoever. A threshold that fired on a jobs listing would be reporting the
# site's own design as a fault.
ENRICHMENT_FLOOR = 60

# A batch holding less than this share of the fullest batch in the same run is
# reported as thin. The served list is not a fixed page size -- 41 entries on
# the smallest capture, 359 on the largest, all of them first batches -- so
# this is loose on purpose.
THIN_PAGE_SHARE = 0.5


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    # The scroll primitives are NAMED OPERATIONS rather than JavaScript, and
    # that is the point of the split. Selenium's execute_script takes a
    # function BODY with an explicit `return` while Playwright and pyppeteer
    # take `() => expr`, so a shared module handing JS across this boundary
    # would quietly acquire one driver's dialect.
    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "text": lambda selector: _text(page, selector),
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
        "scroll_by": lambda px: _scroll_by(page, px),
    }


def _text(page, selector: str) -> Optional[str]:
    """The first match's text, or None. Used for the site's own counter."""
    try:
        el = page.query_selector(selector)
        return el.inner_text() if el is not None else None
    except (PWError, PWTimeout):
        return None


def _scroll_by(page, px: int) -> None:
    """Wheel down a BOUNDED distance -- deliberately not to scrollHeight.

    The family's rule is the opposite, and it is right for a lazy-loading grid
    that grows as it loads. This list does not grow: it is virtualised, its
    container's height is pre-computed for all 10,000 items, and one jump to
    `scrollHeight` lands on item 9,997. Measured -- 700 unique ids collected
    that way, after which the page satisfies every readiness test the usual
    rule prescribes (count steady, height steady, three rounds unchanged)
    while holding 7% of the list.
    """
    try:
        page.mouse.wheel(0, px)
    except (PWError, PWTimeout):
        pass


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args) -> int:
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status=status, url=page.url)

# Every readiness constant, every pagination selector and every state policy
# lives in page_flow.py, with its measurement beside it. Nothing about WHAT
# to do with a page is duplicated here — this file only knows HOW to ask
# Playwright.


def _advertised_next_hrefs(page, page_num: int) -> List[str]:
    """Every href on the page that could be the link to page `page_num` + 1.

    ALL of them, not the first, and the full set is handed to
    `page_flow.next_page_candidates` to filter — so the filtering rule lives
    in one place for all three engines.

    On Craigslist this returns nothing, and that is measured rather than
    broken: neither served nor rendered markup carries an href containing
    `page=`, a `link[rel=next]`, or a working next-page control, on any
    category or locale captured. The function is here so that if the site ever
    grows real pagination markup the engines pick it up instead of quietly
    assuming forever that it has none.
    """
    selector = page_flow.next_page_selector(page_num)
    if not selector:
        return []
    return [el.get_attribute("href") for el in page.query_selector_all(selector)]


def _plan_page_urls(page, args, page_one_url: str) -> Optional[List[str]]:
    """None, always, and for a measured reason rather than a missing feature.

    Following a site's own next-link one page at a time is correct but
    strictly sequential; constructing the page parameter up front is what
    makes pages independent, and therefore what makes concurrency possible at
    all. Craigslist supports neither.

    Measured 2026-09-14: `?page=2` and `?s=296` on a search URL return a
    BYTE-IDENTICAL response -- 536,962 bytes, same first result. Not an empty
    set, not an error, not a redirect: the same page. A planner that built
    `?page=N` would hand every worker the same document; the run would find no
    new sku, conclude the listing was exhausted, and report `complete` while
    holding one batch. That exact bug shipped once in this family and survived
    four static reviews.

    Batches beyond the first live in the SAME document, reached by walking the
    virtualised list. There is nothing to plan.
    """
    if args.pages < 2:
        return None

    hrefs = [h for h in _advertised_next_hrefs(page, 1) if h]
    if hrefs:
        # If this ever fires, the site has grown pagination markup and this
        # function -- along with page_flow.pagination_is_addressable -- should
        # be rewritten rather than kept as a monument.
        logger.warning(
            "This page advertises %d next-page link(s), which Craigslist has "
            "not done on any page measured. Pagination markup may have been "
            "added; this run will still walk. Worth re-measuring: %s",
            len(hrefs), hrefs[0])

    logger.info(
        "No page URLs to plan: Craigslist serves one address per listing and "
        "ignores ?page= and ?s=. Batches 2-%d are reached by walking the "
        "rendered list in this same document, which is also why --concurrency "
        "is capped at 1 here.", args.pages)
    return None


def _next_url_from_page(page, args, page_num: int) -> Optional[str]:
    """None: this site has no next URL, planned or advertised.

    Kept as a named function rather than deleted so all three engines keep the
    same shape and the smoke suite can assert they answer identically. An
    engine that started constructing something here would disagree with its
    twins about what a run covered.
    """
    candidates = page_flow.next_page_candidates(
        page.url, _advertised_next_hrefs(page, page_num))
    return candidates[0] if candidates else None


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. An engine that carried its own copy of
    this in a sibling repo went stale and silently fell back to sequential
    fetching — the exact divergence page_flow.py exists to prevent,
    reproduced inside one engine.

    On this site the comparison has to strip a long tracking tail: a listing
    anchor arrives with `?extParam=…keyword=kopi&search_id=…&src=search` and
    a detail page's own canonical arrives with a UTM triple, so two views of
    one page never match unless both sides are cleaned.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool, javascript: bool = True):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    ctx_kwargs["java_script_enabled"] = javascript
    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None
        # Whether this session runs the site's own application.
        #
        # A single-batch listing run does NOT: Craigslist serves a complete
        # results list in the first response and its application then removes
        # that list about twenty seconds in, replacing it with a virtualised
        # grid of 200 recycled nodes. With JavaScript off the list stays, the
        # ~27s hydration wait is not paid, and the window in which the page
        # has neither view cannot be hit. The browser still does all the
        # fetching -- TLS, headers, cookies, the proxy, the remote profile.
        #
        # A posting page is server-rendered on every category measured, so it
        # does not need it either. Only walking past the first batch does.
        self.javascript = (args.mode == "listing"
                           and page_flow.javascript_needed(args.pages))

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool, javascript=self.javascript)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # the endpoint is a URL with the password in it. Unmasked, that
        # password lands in the terminal, in CI output and in any log the run
        # is piped to — which is the one thing this project promises does not
        # happen ("credentials never reach argv or logs"). The message is
        # rewritten with the credentials masked and the host and port kept,
        # because WHICH endpoint failed is the useful half and is not the
        # secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # The remote context belongs to the Scraping Browser profile and is
    # REUSED rather than replaced, so its fingerprint, session and exit stay
    # intact -- which means java_script_enabled cannot be set on it here. It
    # is left on: a remote single-batch run pays the hydration wait that a
    # local one avoids, and reads the served list from the first response
    # before the application removes it, which is the same rows either way.

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # No challenge of any kind has been observed on this site -- zero vendor
    # markers and zero occurrences of the word "captcha" across 22 captures --
    # so this is wired up because one can appear between deploys, not because
    # it is part of the happy path.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a Playwright connection
# error repeats the endpoint five times (the message plus a four-line call
# log), so a masker that handled only the first occurrence would print the
# password four times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. Craigslist does not geo-redirect -- the area is a path segment and
    one US exit returned five different currencies across eight areas -- but
    the legacy per-city subdomains 301 to the canonical host, and the
    application rewrites the URL fragment as it hydrates, so a snapshot taken
    right after goto() can land exactly on a swap.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (a legacy-subdomain redirect, "
                        "or the application taking over?) -- retrying "
                        "content() in %dms (%d/%d).",
                        pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. The
    static-HTML and runtime reCAPTCHA detectors are run and reconciled
    against each other rather than short-circuited, because they can disagree
    about the variant and the parameters for one are rejected for the other.

    NOTE what this cannot help with. No challenge of any kind has ever been
    observed on this site -- zero vendor markers and zero occurrences of the
    word "captcha" across 22 captures covering six categories and five
    locales -- and what Craigslist serves an address it has scored has not
    been measured here at all, because nothing this repo ran was refused.
    `detect_page_state` therefore decides "blocked" structurally rather than
    by marker, and reports it as "blocked" rather than "challenge" precisely
    so no solve is attempted or billed. This path exists because a bot
    manager can be switched on between deploys, and because the family's rule
    is that
    detection stays broad:
    different geos and scenarios surface different challenges.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this mode, always as a list even when the mode yields one.

    `parse_posting` returns a single row or None; wrapping it here keeps every
    caller downstream -- dedupe, merge, coverage logging, the writers --
    working on one shape instead of branching on the mode again.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every batch: without the batch number beside it, a row
    from batch 2 claims the same position as one from batch 1 and the two are
    indistinguishable in the output. A sibling repo's first live run wrote 120
    rows all labelled page 1 for exactly that reason.
    """
    if args.mode == "posting":
        row = parse_posting(html, url, category=args.category)
        return [row] if row is not None else []
    return parse_products(html, url, page=page_num, category=args.category)


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 403 refusal, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried.
    #
    # With a pool, each retry moves to a DIFFERENT exit and the budget is the
    # user's `--proxy-block-retries`. WITHOUT one — the ordinary case here,
    # because `--cdp-endpoint` brings its own exit — the retry re-fetches
    # through the same access path, and that is worth doing on this site
    # rather than giving up: a Scraping Browser profile was measured refusing
    # two requests and serving the third. Zero was the family default and it
    # made the first live run of this engine abandon page 1 on its first
    # block without retrying once.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            # A solve navigated the page. Give the destination a moment
            # before judging what came back.
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # what the first live search run of this engine got wrong. A CATEGORY
        # listing server-renders its grid container, so it classifies as
        # content at domcontentloaded; a SEARCH grid arrives with the
        # client-side GraphQL response, so at that moment the page is a
        # 608 KB shell with no grid in it. Classified naively that is
        # "unknown", "unknown" retries, and the run fetched the page twice,
        # scrolled not at all and reported 0 rows with exit 4.
        #
        # So wait for the anchor and re-classify BEFORE the retry decision.
        # See page_flow.is_unpainted.
        if page_flow.should_wait(state):
            wait_timeout = page_flow.content_timeout_ms(
                args.mode, js=session.javascript)
            logger.info("Batch %d is a page Craigslist served whose results "
                        "are not in it yet (%d bytes). This is a REAL state "
                        "here, not a theoretical one: the application removes "
                        "the served list about 20s in and paints its own grid "
                        "several seconds later, so there is a window with "
                        "neither. Waiting up to %.0fs rather than spending a "
                        "retry on it.", page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args), wait_timeout)
            if found < _min_matches(args):
                logger.info("Still nothing after %.0fs (%d match(es)).",
                            wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        # No interstitial-settling step here, and its absence is measured
        # rather than an omission: across 22 captures covering six categories
        # and five locales this site served no interstitial of any kind, and
        # the word "captcha" appears zero times on any of them. There is
        # nothing to wait out and nothing to reclassify.
        #
        # The paid path is reached only for state "challenge", which NO
        # capture of this site has ever produced. It is wired up because a
        # bot manager can be switched on between deploys and a scraper that
        # cannot name what stopped it is much harder to fix — and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning(
                        "The solve was NOT accepted: page %d is still %s. The "
                        "purchase is spent.", page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid, and one page past
            # the end of a listing has no products — so retrying it would
            # spend the user's budget re-confirming the same right answer,
            # and rotating the exit would blame an address for the URL it was
            # given.
            break

        # Blocked or challenged. A different exit is the one thing that
        # plausibly changes the outcome: the ADDRESS is what was scored, not
        # the URL, so retrying it unchanged would only confirm it. Measured
        # 2026-09-09 — the same URL that answers 403 from a datacentre exit
        # answers 200 from a residential one.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over `--cdp-endpoint` a profile allows
                # one live connection, so tearing the session down and
                # reconnecting risks `profile_locked` and would lose the very
                # cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d). On this "
                               "site that is often what clears it.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # What a caller needs here is a name for what happened and the next
        # thing to try. This repo has never seen Craigslist refuse a request
        # -- every run during its development was served -- so the message
        # says what was actually checked rather than inventing a diagnosis
        # for a state nobody has measured.
        #
        # The dump is written even when it is empty, because "0 bytes" is
        # itself a diagnosis and a reader who finds no file at all cannot
        # tell that from a run that never got here.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        served = served_by_craigslist(html or "")
        logger.error(
            "This response was not a Craigslist page -- %d bytes, %s the "
            "site's own asset host, saved to %s. The check is structural "
            "rather than a marker list, which is what answers correctly for "
            "an interstitial, a block page and Chromium's own network-error "
            "page alike -- that last one carries the site's hostname in its "
            "<title> and would fool any title test. No challenge vendor has "
            "ever been observed here, so a 2Captcha key is unlikely to be the "
            "answer; a different exit is the thing to try. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).%s",
            len(html or ""), "which references" if served else "with no "
            "reference to", debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = session.page.url
        return outcome

    walked_rows: List = []
    walked_seen = set()

    if state == "content":
        # The served list is already in `html`, captured right after
        # navigation. It is the COMPLETE first batch -- url, id, title, price
        # and location for every result, with the page's own JSON-LD aligned
        # onto it -- and it is what a single-batch run returns.
        #
        # Hold a reference to it before anything else happens. With
        # JavaScript on, the application REMOVES that list about twenty
        # seconds in and paints a virtualised grid in its place, so re-reading
        # the document later returns a page with no served list on it at all.
        served_html = html

        selector, threshold = _ready_selector(args), _min_matches(args)
        content_timeout = page_flow.content_timeout_ms(
            args.mode, js=session.javascript)
        # A POLL, not wait_for_function. wait_for_function hands the browser a
        # STRING to evaluate, which a strict Content-Security-Policy refuses --
        # a sibling repo took a whole run down with exit 1 that way, on its
        # site's most obvious URL. See page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            lambda sel: len(session.page.query_selector_all(sel)),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        if found < threshold:
            if args.mode == "posting":
                logger.info("The advert's body did not appear within %.0fs. A "
                            "posting page is server-rendered on every category "
                            "measured, so this is unusual rather than slow; "
                            "the parse below decides.", content_timeout / 1000)
            else:
                logger.info("Fewer than %d result entries appeared within "
                            "%.0fs. If this URL's query matches nothing, that "
                            "is the expected answer and the run reports 0 rows "
                            "(exit 4).", threshold, content_timeout / 1000)

        if args.mode == "listing" and args.pages > 1:
            # Past the first batch the only view is the site's own virtualised
            # grid, whose nodes are RECYCLED as the window slides. Rows are
            # therefore harvested DURING the walk: an id in the document now
            # is gone two steps later, and the single read at the end that a
            # lazy-load scroll needs would return the last 200 and nothing
            # else.
            currency = page_currency(served_html)
            logger.info("Walking the virtualised list for batches 2-%d. The "
                        "currency for those rows is read once, here (%s): the "
                        "page's own JSON-LD is itself virtualised, so reading "
                        "it during the walk would make one column depend on "
                        "scroll position.", args.pages, currency or "none published")

            # Wait for the application to finish taking over. There is a
            # window -- measured around t=22s -- in which it has removed the
            # served list and not yet painted its grid, so waiting on the grid
            # specifically is what gets past it.
            page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                session.page.wait_for_timeout, SELECTORS["rendered_card"],
                page_flow.MIN_CARD_MATCHES, page_flow.CONTENT_TIMEOUT_MS_HYDRATED)

            def _harvest():
                """Read the cards on screen now, and say which ones they were.

                The returned set is what lets the walk notice it has outrun
                itself: two consecutive reads with no id in common mean the
                window jumped a stretch that will never be read, because the
                nodes behind it are already recycled.
                """
                snapshot = _content_when_settled(session.page)
                if not snapshot:
                    return set()
                seen_now = set()
                for row in parse_rendered_cards(
                        snapshot, session.page.url, category=args.category,
                        currency=currency):
                    if not row.sku:
                        continue
                    seen_now.add(row.sku)
                    if row.sku not in walked_seen:
                        walked_seen.add(row.sku)
                        walked_rows.append(row)
                return seen_now

            driver = _driver(session.page)
            outcome.walk = page_flow.walk_virtualised(
                count=driver["count"], text=driver["text"],
                scroll_by=driver["scroll_by"], sleep=driver["sleep"],
                harvest=_harvest,
                want_items=args.pages * page_flow.BATCH_HINT)
            outcome.walk["harvested"] = len(walked_rows)
            reached = outcome.walk["counter_reached"]
            logger.info("The walk reached position %d in %d step(s) and "
                        "harvested %d distinct row(s).", reached,
                        outcome.walk["steps"], len(walked_rows))
            if outcome.walk["gaps"]:
                logger.warning(
                    "The walk outran its harvest %d time(s), so stretches of "
                    "the list went by unread and this run is PARTIAL. Its row "
                    "count is a floor. The step adapts downwards when this "
                    "happens, so a re-run on a less loaded machine will "
                    "usually collect more.", outcome.walk["gaps"])
            if reached >= RESULT_CAP:
                logger.warning(
                    "This URL hit Craigslist's %d-result ceiling. That is the "
                    "site's limit for ONE address, not the size of the "
                    "catalogue: narrow the URL with the site's own filters "
                    "(&min_price=/&max_price=, &query=, a subarea) and run "
                    "each of them separately.", RESULT_CAP)

        html = served_html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED, and that
    # narrowing was earned twice.
    #
    # A marker on a page whose products have rendered guards nothing — that
    # is the "detected is not blocking" rule the captcha default follows,
    # applied to the blocking decision instead of the spending one. But
    # `state != "content"` is still too wide: an EMPTY page is a correct
    # answer, and a live run of a /p/<slug> hub reported exit 3 on a 191 KB
    # page the site had plainly served, because the hub's own performance
    # script names `akamaihd.net` and "akamai" was in the marker list. Both
    # halves were wrong; the marker is gone (see
    # product_parser.BOT_CHALLENGE_MARKERS) and this now only refines the
    # REASON for a page the policy had already given up on.
    vendor = (detect_bot_challenge(html, url=session.page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     (f" (tried {block_retries + 1} exit(s))" if has_pool
                      else f" (re-fetched {block_retries + 1} time(s))"))
        outcome.blocked_by = vendor
        return outcome

    # Through the POLICY rather than unconditionally. `STATE_POLICY` is the
    # one place that says which states are worth reading, and until now
    # nothing consulted its `parse` column: the engines parsed whatever
    # reached this line, so every state without an earlier `return` was
    # read regardless of what the table said.
    #
    # # Latent here rather than live — measured on this repo's own fixtures on
    # 2026-09-23: no shipped fixture of a parse:False state yields a row,
    # because the parser is independently defensive. The gate is wired
    # anyway, because "the parser happens to return nothing" is not the
    # same guarantee as "the policy says do not read this", and two
    # siblings had exactly this shape turn into phantom rows.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined `should_parse` and none of
    # them called it.
    products = (_parse_for_mode(html, session.page.url, args, page_num)
                if page_flow.should_parse(state) else [])
    if walked_rows:
        # Batch 1 came from the served list, complete and enriched. The walk's
        # rows come from the rendered grid and carry less -- no coordinates,
        # no structured price -- which is exactly what `price_source`
        # records, and why diff_runs.py reports a price change accompanied by
        # a source change as `source_changed` rather than `changed`.
        #
        # Rows already in batch 1 reappear here (the walk starts at the top of
        # the same list) and are dropped by the run's dedupe, in page order,
        # after the merge. They are not dropped here: doing it in two places
        # is how the two disagree.
        first_skus = {r.sku for r in products if r.sku}
        fresh = [r for r in walked_rows if r.sku not in first_skus]
        logger.info("The walk added %d row(s) beyond the served list's %d "
                    "(%d of what it harvested were already in it).",
                    len(fresh), len(products), len(walked_rows) - len(fresh))
        for i, row in enumerate(fresh, start=1):
            # The walk is one continuous list, so its rows are numbered as
            # batches of the site's own first-response size. That keeps
            # `page` + `position` unique across the run, which is the only
            # thing the pair has to guarantee.
            row.page = 2 + (i - 1) // max(len(products), 1)
            row.position = 1 + (i - 1) % max(len(products), 1)
        products = products + fresh
    logger.info("Parsed %d row(s) from batch %d.", len(products), page_num)

    if args.mode == "listing" and page_num == 1:
        # The served markup states no total, so this is None unless the
        # application painted. That is honest rather than a gap: turning
        # `shown` into a total would invent a number nobody can act on.
        outcome.total_available = total_results(html, shown=len(products))
        outcome.header = search_header(html)
        if outcome.header:
            logger.info("The page's own description of this listing: %s",
                        outcome.header)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed.
        logger.info("Price coverage on batch %d: %d/%d (%.0f%%); the measured "
                    "floor is %d%%.", page_num, priced, len(products), share,
                    PRICE_FLOOR)
        if share < PRICE_FLOOR:
            logger.warning(
                "Only %.0f%% of batch %d carries a price, against a floor of "
                "%d%% measured across 14 captures where the lowest was 95%%. "
                "That is the read breaking rather than the listing being "
                "unusual -- re-run with --dump-html.",
                share, page_num, PRICE_FLOOR)

        # How much of this batch the page's own structured data covered.
        # Applied ONLY where the page published any: jobs, services and
        # community publish no `ItemList` at all, and a threshold that fired
        # on one of those would be reporting the site's design as a fault.
        # Measured against the rows that COULD carry it. A walked row is read
        # from a rendered card, which publishes no structured data at all, so
        # including those in the denominator would make the share fall as the
        # walk succeeds -- a warning that fires precisely when the run went
        # well is worse than no warning.
        eligible = [p for p in products
                    if p.price_source in ("jsonld", "static")]
        enriched = sum(1 for p in eligible if p.price_source == "jsonld")
        if enriched and eligible:
            enrich_share = 100.0 * enriched / len(eligible)
            logger.info("Structured-data coverage on the served batch: %d/%d "
                        "(%.0f%%). Measured range across captures: 66%% "
                        "(paris) to 99%% (toronto).", enriched, len(eligible),
                        enrich_share)
            if enrich_share < ENRICHMENT_FLOOR:
                logger.warning(
                    "Structured coverage is %.0f%%, below the %d%% floor. The "
                    "served list and the JSON-LD are aligned as an "
                    "order-preserving subsequence; a low share here can mean "
                    "the alignment was rejected, which it is on purpose when "
                    "the two views disagree.", enrich_share, ENRICHMENT_FLOOR)
        else:
            logger.info("This listing published no structured data -- normal "
                        "for jobs, services and community, which ship no "
                        "JSON-LD ItemList at all. Every row here was read "
                        "from the served result list.")

        with_geo = sum(1 for p in products if p.latitude is not None)
        logger.info("Coordinates on batch %d: %d/%d. Craigslist publishes "
                    "(0,0) as its placeholder outside North America -- 632 of "
                    "2,210 structured entries measured -- and this parser "
                    "reports those as null rather than as a point in the Gulf "
                    "of Guinea.", page_num, with_geo, len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


# ---------------------------------------------------------------------------
# Concurrency: deliberately NOT implemented here
# ---------------------------------------------------------------------------
# The sibling repos in this family carry `_worker_pool` and
# `_fetch_pages_concurrently` -- a worker per exit, each owning one browser
# and one address for its lifetime, page N handed out by number. About 120
# lines, and on those sites it is the thing that spreads a run's volume.
#
# It is removed here rather than ported, and this is a divergence from the
# family worth stating outright.
#
# A worker is only useful if it can be given an address of its own. No batch
# of a Craigslist listing has one: `?page=2` and `?s=296` return the SAME
# DOCUMENT, byte for byte, and batches after the first are reached by walking
# a virtualised list inside one already-fetched page. Workers would each
# re-fetch the first batch from a different exit and the run would merge N
# copies of it.
#
# So the code would be unreachable, and unreachable code that looks
# load-bearing is exactly the defect this family's own notes warn about: a
# reader would take its presence as evidence that concurrency works here.
# `--concurrency` REMAINS as a flag, because it is part of the family's CLI
# contract and the smoke suite asserts the three engines' flag sets against
# each other -- it is accepted, refused above 1 with the reason, and that is
# the whole of it.
#
# What DOES spread a run on this site is several runs: the site honours
# `&min_price=`/`&max_price=`, `&query=`, `&postedToday=1` and subarea slugs
# as real query parameters, each returning a different result set, and each
# such URL is independently fetchable.


def scrape(args) -> int:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # Both modes are one row per product, so `sku` is the key for both.
    dedupe_key = "sku"
    # Why the loop ended. "completed" means every requested page was fetched;
    # "no_new_products" means the listing itself ran out (also a complete
    # result). "single_page_mode" is complete by construction — a detail page
    # has no page 2. Anything else is an early stop, and the run is only a
    # partial view.
    # Only --mode posting is single-page. NOTE what that does NOT mean here:
    # a listing run is also a single FETCH, because every batch lives in the
    # one document. The distinction the status draws is between "one advert
    # was the whole job" and "a list was walked", which is what a consumer
    # needs to know.
    stop_reason = "single_page_mode" if args.mode == "posting" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.mode == "posting":
            logger.info("--concurrency is ignored in --mode posting: there is "
                        "one page to fetch.")
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        else:
            # Not a warning about proxies: on this site concurrency cannot
            # work at all, with or without a pool. A worker is only useful if
            # it can be handed an address of its own, and no batch of a
            # Craigslist listing has one -- ?page= and ?s= return the same
            # document byte for byte. Every worker would re-fetch the first
            # batch from a different exit.
            logger.warning("--concurrency %d requested: %s Falling back to 1.",
                           concurrency, page_flow.concurrency_refusal(args.url))
            concurrency = 1
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # ONE fetch is the whole run on this site, and that is the
            # measured shape of Craigslist rather than a simplification.
            #
            # The served response carries the complete first batch. Every
            # batch after it lives in the SAME document, reached by walking
            # the virtualised list -- there is no second address to fetch,
            # because `?page=` and `?s=` return this document byte for byte.
            # `_fetch_one_page` therefore does the walk internally when
            # `--pages > 1`, and what comes back already spans the run.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            elif args.mode == "posting":
                pass  # one advert is the whole run
            else:
                seen_keys.update(p.sku for p in first.products
                                 if p.sku is not None)
                _plan_page_urls(session.page, args, first.final_url)
                walk = first.walk or {}
                if args.pages > 1 and not walk:
                    # Asked for more than one batch and the walk never ran:
                    # the page never hydrated. Saying so is what stops the run
                    # reporting `complete` for a fraction of what was asked.
                    stop_reason = "walk_did_not_start"
                elif walk.get("gaps"):
                    stop_reason = "walk_gaps"
                elif walk.get("hit_cap"):
                    stop_reason = "result_cap_reached"
                elif args.pages > 1:
                    stop_reason = "completed"
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this is
    # what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Expected on a walking run and not on a single-batch one: the
            # walk starts at the top of the same list the served markup
            # already gave us, so its first harvests repeat what batch 1 held.
            logger.info("Batch %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "batches x rows-per-batch". There is no fixed batch size on this
    # site -- 41 entries on the smallest served list measured, 359 on the
    # largest, all of them first batches -- so multiplying the fullest by the
    # count would warn on healthy runs, and a threshold that fires on every
    # healthy run teaches the reader to ignore it.
    #
    # What is worth warning about is a page that came back materially THIN
    # against its siblings — that is what a truncated response or a
    # half-painted grid looks like. A page holding less than 60% of the
    # fullest page is well outside the +-3% spread that the varying page size
    # accounts for.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    if args.mode == "listing" and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        # The LAST batch is legitimately short -- the list simply ran out --
        # so it is excluded unless there are batches after it.
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A truncated response or a half-painted grid "
                "looks like this — re-run with --dump-html to check the "
                "snapshot for those pages.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("This listing holds %d product(s) in total; this run "
                        "took %d (%.1f%%).", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    #
    # In --mode listing that is the walk trace and the page's own header.
    # Both say how much of the list this run actually saw, which is the
    # question a consumer of a 10,000-item virtualised list most needs
    # answered and which no column can carry. In --mode posting there is
    # nothing of the kind: one advert is its own context.
    extra = None
    walks = {o.page_num: o.walk for o in outcomes if o.walk}
    headers = {o.page_num: o.header for o in outcomes if o.header}
    if walks or headers:
        extra = {"walk": walks, "result_header": headers}
    capped = [n for n, w in walks.items() if w and w.get("hit_cap")]
    if capped:
        logger.warning(
            "The walk hit Craigslist's %d-result ceiling on batch(es) %s, so "
            "this run's row count is a floor rather than the listing. That "
            "ceiling is per ADDRESS: narrow the URL with the site's own "
            "filters and run each separately.",
            RESULT_CAP, ", ".join(str(n) for n in capped))

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Craigslist scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="Craigslist URL: a result list "
                        "(/search/area/{slug}?cat=... or "
                        "/search/subarea/{slug}?cat=...) or one advert "
                        "(/view/d/{slug}/{token}) with --mode posting. ONE "
                        "hostname worldwide -- the area is a path segment, "
                        "and the site's own index lists 714 of them. "
                        "Required, unless CRAIGSLIST_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "posting"],
                   default="listing",
                   help="listing (default): a result list. The served "
                        "response carries the complete first batch with the "
                        "page's JSON-LD aligned onto it. posting: one "
                        "/view/d/{slug}/{token} advert, which adds the body "
                        "text, the attribute bag, every image, both "
                        "timestamps and Craigslist's classic numeric post id. "
                        "--pages applies to listing only; there is one page to "
                        "read in posting mode.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "`cat=` code from the URL, named where this repo has "
                        "measured it (sss, cta, apa, jjj, bbb, ccc, eee) and "
                        "passed through verbatim where it has not -- the site "
                        "has dozens, and inventing names for unmeasured ones "
                        "would put a guess in a data column.")
    p.add_argument("--pages", type=int, default=1,
                   help="How many batches to collect (default 1). Craigslist "
                        "has no pages: ?page= and ?s= return the same "
                        "document byte for byte, so a batch above the first "
                        "is reached by WALKING the rendered list inside the "
                        "same page, and --pages above 1 turns JavaScript on "
                        "to do it. One batch is about %d rows. One URL tops "
                        "out at %d results no matter how far it is walked; "
                        "past that, narrow the URL (&min_price=/&max_price=, "
                        "&query=, a subarea) and run each separately."
                        % (page_flow.BATCH_HINT, RESULT_CAP))
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted and capped at 1 on this site. A worker is "
                        "only useful if it can be handed an address of its "
                        "own, and no batch of a Craigslist listing has one -- "
                        "?page= and ?s= return the same document. To spread a "
                        "run, use the site's own filters to make several "
                        "narrower URLs and run each of them separately.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried -- see "
                        "page_flow.STATE_POLICY -- because a query matching "
                        "nothing is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="craigslist_products", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "the language or the currency: those follow the AREA "
                        "in the URL, measured by fetching eight areas through "
                        "one US exit and getting USD, CAD, EUR, JPY and MXN "
                        "back. This only affects what the browser claims "
                        "about itself.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused (HTTP 403) or behind "
                        "a captcha, retry it from this many OTHER exits before "
                        "giving up (default 2). Needs a pool of more than one; "
                        "ignored otherwise. This is the flag that matters most "
                        "on this site: the refusal is a property of the "
                        "ADDRESS, and a different exit is what clears it.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation. Measured
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`, `Chrome`
    # and `Desktop` each 400. fingerprint_client.py's own --tags help has
    # said so all along; the engines' default contradicted it.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. NO "
                        "challenge has ever been observed on this site -- "
                        "zero vendor markers and zero occurrences of the word "
                        "'captcha' across 22 captures -- so neither setting is "
                        "expected to spend anything here. The path is wired up "
                        "because a bot manager can be switched on between "
                        "deploys.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and CRAIGSLIST_URL is not set in the "
                "environment or in .env.")
    if not is_supported_host(args.url):
        # Refused rather than attempted. The parser's results container, its
        # posting-path pattern and its id alphabet are all Craigslist's, so
        # pointing this at another classifieds site would not fail loudly --
        # it would return zero rows and look like an empty listing.
        why = unsupported_reason(args.url)
        if why:
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not Craigslist. This "
                f"scraper reads www.craigslist.org, which is the site's only "
                f"hostname worldwide -- the area is a path segment, and the "
                f"site's own index lists 714 of them.")
    kind = listing_kind(args.url)
    if args.mode == "posting" and kind != "posting":
        p.error(f"--mode posting expects a /view/d/{{slug}}/{{token}} advert "
                f"URL; {args.url!r} is a {kind} page.")
    if args.mode == "listing" and kind == "posting":
        p.error(f"{args.url!r} is a single advert. Use --mode posting for it, "
                f"or pass a /search/area/{{slug}}?cat=... URL.")
    if args.mode == "listing" and kind == "hub":
        # Said as a warning rather than an error: it IS a Craigslist URL and
        # the run will honestly report zero rows (exit 4). But a reader who
        # tried the obvious area URL first would otherwise conclude the tool
        # is broken, so name what happened.
        logger.warning(
            "%s is an AREA LANDING page, not a result list. It carries the "
            "area's category links and no results of its own, so this run "
            "will return 0 rows and exit 4. A result list looks like "
            "/search/area/<slug>?cat=<code>, e.g. "
            "/search/area/newyork?cat=sss.", args.url)
    if args.mode == "posting" and args.pages != 1:
        # Said out loud rather than silently ignored: a user who passed
        # --pages 5 expects five pages of something.
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read. The run status will say single_page_mode.",
                       args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest one:
        # `profile_locked` means another run still holds this `pid`, and a
        # harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
