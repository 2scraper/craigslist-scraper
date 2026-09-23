#!/usr/bin/env python3
"""
craigslist-scraper -- Selenium edition (secondary engine)
=========================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money -- the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode listing   (default)  a result list
    --mode posting              one /view/d/{slug}/{token} advert

Three limits of this engine, stated here rather than left to be discovered.
None is a bug in this code and none can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
    `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password. So --cdp-endpoint here works only for an endpoint that
    needs no credentials; a credentialed one is refused with exit 2 rather
    than connected to and silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=` accepts
    no credentials, and there is no equivalent of pyppeteer's
    `page.authenticate`. Credentials are stripped and a warning says so, so
    nobody believes a `user:pass` URL is doing something.
  * **JavaScript cannot be switched off per run here.** The Playwright engine
    disables it for a single-batch listing run, which is what stops
    Craigslist's application removing the served result list before the
    snapshot is taken. Chrome's equivalent is a profile preference set at
    launch, and this engine sets it -- but a remote `debuggerAddress` browser
    brings its own profile, so over --cdp-endpoint the setting does not
    apply. The parser falls back to the rendered grid in that case, which
    costs the structured columns rather than the run.

There is no --concurrency here either, and on this site there is none
anywhere: no batch of a Craigslist listing has an address of its own.

Usage
-----
    python selenium_scraper.py \\
        --url "https://www.craigslist.org/search/area/newyork?cat=sss" --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urlsplit, parse_qsl

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException,
                                        JavascriptException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS)
from product_parser import (parse_products, parse_posting,
                            parse_rendered_cards, page_currency, SELECTORS,
                            detect_bot_challenge, listing_kind, site_host,
                            is_supported_host, total_results, search_header,
                            unsupported_reason, served_by_craigslist,
                            RESULT_CAP)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy, per page kind. Not a
# share of rows that got a price at all. Measured across 14 captures: 95%-100%
# on for-sale-type categories, 100% on housing, jobs, services and community.
# ENRICHMENT_FLOOR is the structured-data share, applied only to the rows that
# could carry it -- 66% (paris) to 99% (toronto) where the page publishes any,
# and a measured zero where it publishes none.
PRICE_FLOOR = 90
ENRICHMENT_FLOOR = 60

# A page holding less than this share of the fullest page in the same run is
# reported as thin. There is no fixed batch size here -- 41 entries on the
# smallest served list measured and 359 on the largest -- and the LAST batch
# is legitimately short, so the bar stays loose.
THIN_PAGE_SHARE = 0.5

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site". A dead
# proxy and a slow page want opposite responses — a different exit versus
# another try at the same one — so they are told apart by the error text.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # Present only on a run that let the application paint: the served markup
    # states no total at all. Kept so
    # the sidecar's shape matches the family's.
    total_available: Optional[int] = None
    # The raw result-count header, verbatim, for the sidecar.
    header: Optional[str] = None
    # What the walk through the virtualised list did: how far the site's own
    # counter got, how many steps it took, how many GAPS it opened, and
    # whether it hit the 10,000 ceiling. A walk with gaps is PARTIAL -- the
    # window moved past rows nobody read.
    walk: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
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


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    `driver.capabilities["browserVersion"]` is the installed Chrome's version,
    so the claim matches what the JS engine and the TLS handshake report. A
    hardcoded number drifts the moment Chrome updates, and claiming an older
    Chrome than everything else reports is itself a signal.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason.

    chromedriver takes a bare address here and cannot send credentials, so an
    endpoint that carries them cannot work through this engine. Refused up
    front: connecting anyway would fail somewhere further in with an error
    that names none of this.
    """
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser — and a
    fresh browser is also the only thing that re-rolls the served page
    fresh cookie jar is what an ordinary user on another network looks like.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None
        # Whether this session runs the site's own application. Only walking
        # past the first batch does; see `open` below, and page_flow's "So
        # JavaScript is DISABLED for a single-batch run".
        #
        # NOT applied over --cdp-endpoint: that attaches to a browser someone
        # else launched, whose profile is already set. The parser falls back
        # to the rendered grid there, which costs the structured columns
        # rather than the run.
        self.javascript = (args.mode == "listing"
                           and page_flow.javascript_needed(args.pages))

    def open(self):
        options = Options()
        # Stop waiting at DOMContentLoaded rather than at `load`, which is
        # what Playwright's `wait_until="domcontentloaded"` does -- so the
        # three engines snapshot the page at the same MOMENT and not just
        # with the same code.
        #
        # It matters on this site rather than being a tidiness point. The
        # served result list is removed by the application about twenty
        # seconds in; waiting for `load` on a page with hundreds of images can
        # spend enough of that window to miss it, and the run then falls back
        # to the rendered grid and silently loses the structured columns.
        # Measured: a Selenium walk that snapshotted at `load` got 200 rows
        # from the grid where the Playwright walk got 353 from the served
        # list.
        options.page_load_strategy = "eager"
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with
        # a bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if not self.javascript:
            # A single-batch listing run does not run the site's application,
            # and on this site that is what KEEPS the data: Craigslist serves
            # a complete result list and its own script removes it about
            # twenty seconds later, replacing it with a virtualised grid of
            # 200 recycled nodes. With JavaScript off the list stays, the
            # hydration wait is not paid, and the window in which the page has
            # neither view cannot be hit.
            #
            # Chrome's switch for this is a content-setting preference, not a
            # command-line flag: `--disable-javascript` was removed years ago
            # and passing it does nothing at all, silently.
            options.add_experimental_option(
                "prefs", {"profile.managed_default_content_settings.javascript": 2})
            logger.info("JavaScript is off for this run: one batch comes from "
                        "the served result list, which the site's own script "
                        "would otherwise remove before it can be read.")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            # Set over CDP rather than as a launch switch, so it can use the
            # version the driver actually reports.
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs the
        # run: "every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import (get_fingerprint, playwright_init_script,
                                        fingerprint_user_agent)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        # Through the shared helper, NOT by reaching into the response.
        #
        # This line read `fp["userAgent"]["value"]` -- a key the API returns
        # in neither of its two formats -- so `--fingerprint` quietly set no
        # user agent here at all while reporting that it had applied one. The
        # API's own key is `userAgent.userAgent`, and the helper is where that
        # knowledge lives precisely so three engines cannot each get it wrong
        # separately. Verified against a live response, 2026-09-14.
        ua = fingerprint_user_agent(fp)
        intl = fp.get("intl") or {}
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            else:
                logger.warning("This fingerprint carries no user agent, so the "
                               "browser keeps its own. That is a contradiction "
                               "worth knowing about rather than a silent "
                               "half-application.")
            # The timezone the API states, which was not being applied at all.
            # A fingerprint whose locale says America/New_York over a browser
            # reporting Europe/Berlin is a mismatch of exactly the kind a
            # fingerprint exists to avoid.
            if intl.get("timeZone"):
                self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                            {"timezoneId": intl["timeZone"]})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s), UA %s, timezone %s",
                        fp.get("id"), fp.get("country"),
                        "set" if ua else "NOT set", intl.get("timeZone") or "not stated")
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) -- "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is why page_flow names
# operations instead of passing JavaScript.
def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land on the document swap. None tells the caller to
            # skip a check rather than fail the run.
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    def scroll_by(px):
        # Selenium's execute_script takes a function BODY with an explicit
        # `return` -- not `() => expr`, which is what the other two drivers
        # take. That disagreement is exactly why page_flow names the
        # OPERATION and each engine spells it in its own dialect.
        #
        # A BOUNDED wheel, deliberately not a jump to the document's bottom.
        # The family's rule is the opposite and it is right for a lazy-loading
        # grid that grows as it loads. Craigslist's list is virtualised: its
        # container's height is pre-computed for all 10,000 items, so one jump
        # to scrollHeight lands on item 9,997 and collects 7% of the list
        # while passing every readiness test the usual rule prescribes.
        try:
            driver.execute_script("window.scrollBy(0, arguments[0]);", px)
        except WebDriverException:
            pass

    def text(selector):
        # The first match's text, or None -- used for the site's own
        # "1 - 6 of 10,000+" counter, which is what the walk's stall check
        # reads instead of DOM churn.
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            return els[0].text if els else None
        except WebDriverException:
            return None

    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url, "scroll_by": scroll_by, "text": text}


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    # `page_num` is threaded through rather than defaulted: `position`
    # restarts at 1 on every page, so without the page number beside it a
    # row from page 2 claims the same position as one from page 1. Mirrors
    # playwright_scraper._parse_for_mode exactly.
    if args.mode == "posting":
        row = parse_posting(html, url, category=args.category)
        return [row] if row is not None else []
    return parse_products(html, url, page=page_num, category=args.category)


def _next_page_candidates(session, page_num: int) -> List[str]:
    """The site's own next-page link, resolved by the browser, or None.

    Returns EVERY candidate, filtered by page_flow. On Craigslist that filter
    returns nothing and the selector is empty, because no page measured
    carries a next-page link of any kind -- so this exists to NOTICE if that
    ever changes, not to be relied on.

    Reads the DOM's `.href` property, which is already absolute — the
    opposite of Playwright's get_attribute("href"), which returns the raw
    attribute. Kept explicit because the engines differ here.

    Note the JS is a function BODY with an explicit `return`, not the arrow
    expression the other two engines pass. That difference is exactly why no
    JavaScript crosses the page_flow boundary.
    """
    selector = page_flow.next_page_selector(page_num)
    if not selector:
        # Empty on this site, and passing "" to querySelectorAll raises a
        # SyntaxError in the browser. Returning early rather than letting the
        # exception handler below swallow it: normal flow should not travel
        # through an except branch.
        return []
    try:
        hrefs = session.driver.execute_script(
            "return Array.from(document.querySelectorAll(arguments[0]))"
            ".map(a => a.href || a.getAttribute('href')).filter(Boolean);",
            selector)
    except WebDriverException:
        return []
    return page_flow.next_page_candidates(session.driver.current_url,
                                          hrefs or [])


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same detectors, same reconciliation and the same "detected is not
    blocking" rule as the Playwright engine — the three must agree about
    when a run spends money.

    NOTE what this cannot help with. No challenge of any kind has been
    observed on this site across 22 captures, and what Craigslist serves a
    refused address has not been measured here at all. A refusal with no
    widget on it has nothing to solve, so no solve applies there and none is
    attempted. See product_parser.detect_page_state.
    """
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = d["count"](selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, d["current_url"]())
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=d["current_url"]())
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    try:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    except WebDriverException as e:
        logger.error("Could not inject the token (%s).", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    Kept structurally parallel to its twins on purpose — "all three engines
    agree" is checked by reading them side by side as well as by the smoke
    suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    html, state, load_failed = None, "ok", False

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
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

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = d["content"]() or ""
        state = page_flow.classify(html, url=d["current_url"]())

        # "Not painted yet" is not a fault. A REAL state on this site rather
        # than a theoretical one: the application removes the served result
        # list about twenty seconds in and paints its own grid several
        # seconds later, so there is a window in which the page has neither.
        # Waiting is the right answer there -- retrying would throw away a
        # page that is about to be fine. Mirrors
        # playwright_scraper exactly; see page_flow.should_wait.
        if page_flow.should_wait(state):
            js = args.mode == "listing" and page_flow.javascript_needed(args.pages)
            wait_s = page_flow.content_timeout_ms(args.mode, js=js) / 1000.0
            sel = page_flow.ready_selector(args.mode)
            need = page_flow.min_matches(args.mode)
            logger.info("Batch %d is a page Craigslist served whose results "
                        "are not in it yet (%d bytes) -- waiting up to %.0fs "
                        "rather than spending a retry on it.",
                        page_num, len(html), wait_s)
            found = page_flow.wait_for_count(d["count"], d["sleep"], sel,
                                             need, int(wait_s * 1000))
            if found < need:
                logger.info("Still nothing after %.0fs (%d match(es)).",
                            wait_s, found)
            html = d["content"]() or html
            state = page_flow.classify(html, url=d["current_url"]())

        # No interstitial-settling step, and its absence is measured rather
        # than an omission: 22 captures covering six categories and five
        # locales carry no interstitial of any kind, and the word "captcha"
        # appears zero times on any of them. There is nothing to wait out.
        #
        # The paid path is reached only for state "challenge", which no
        # capture of this site has ever produced. Wired up because a bot
        # manager can be switched on between deploys, and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)
                html = d["content"]() or html
                state = page_flow.classify(html, url=d["current_url"]())
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — an area landing page has no results — so retrying it
            # would re-confirm the same right answer, and rotating the exit
            # would blame an address for the URL it was given.
            break

        # Blocked or challenged. The ADDRESS is what was scored, not the URL,
        # so a different exit is the only thing that plausibly changes the
        # outcome.
        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            d = _driver(session)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # There is no challenge on this site to solve. Craigslist sends an
        # address it has scored NOTHING — no status code, no interstitial, no
        # vendor marker — so a 2Captcha key does not help and a residential
        # exit does. The dump is written even when empty: "0 bytes" is itself
        # the diagnosis here.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "This response was not a Craigslist page -- %d bytes, %s the "
            "site's own asset host, saved to %s. The check is structural "
            "rather than a marker list, which is what answers correctly for "
            "an interstitial, a block page and Chromium's own network-error "
            "page alike -- that last one carries the site's hostname in its "
            "<title> and would fool any title test. No challenge vendor has "
            "ever been observed here, so a key is unlikely to be the answer; "
            "a different exit is the thing to try. Note this engine cannot "
            "use an authenticated remote CDP endpoint or an authenticated "
            "proxy; see the README's engine limits. This is exit 3, distinct "
            "from a genuinely empty result (exit 4).",
            len(html or ""),
            "which references" if served_by_craigslist(html or "")
            else "with no reference to", debug_html)
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = d["current_url"]()
        return outcome


    walked_rows = []
    walked_seen = set()

    if state == "content":
        # The served list is already in `html`, captured right after
        # navigation. It is the COMPLETE first batch, with the page's own
        # JSON-LD aligned onto it, and it is what a single-batch run returns.
        #
        # Hold a reference before anything else: with JavaScript running, the
        # application removes that list about twenty seconds in and paints a
        # virtualised grid in its place.
        served_html = html

        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode)
        js = args.mode == "listing" and page_flow.javascript_needed(args.pages)
        timeout_s = page_flow.content_timeout_ms(args.mode, js=js) / 1000.0
        # A POLL through the shared helper, so all three engines wait the same
        # way. This engine could use WebDriverWait, but a shared wait is one
        # fewer thing for the three to drift on.
        found = page_flow.wait_for_count(d["count"], d["sleep"], selector,
                                         threshold, int(timeout_s * 1000))
        if found < threshold:
            if args.mode == "posting":
                logger.info("The advert's body did not appear within %.0fs. A "
                            "posting page is server-rendered on every category "
                            "measured, so this is unusual rather than slow; "
                            "the parse below decides.", timeout_s)
            else:
                logger.info("Fewer than %d result entries appeared within "
                            "%.0fs. If this URL's query matches nothing, that "
                            "is the expected answer and the run reports 0 rows "
                            "(exit 4).", threshold, timeout_s)

        if args.mode == "listing" and args.pages > 1:
            currency = page_currency(served_html)
            logger.info("Walking the virtualised list for batches 2-%d. The "
                        "currency for those rows is read once, here (%s): the "
                        "page's own JSON-LD is itself virtualised.",
                        args.pages, currency or "none published")
            page_flow.wait_for_count(d["count"], d["sleep"],
                                     SELECTORS["rendered_card"],
                                     page_flow.MIN_CARD_MATCHES,
                                     page_flow.CONTENT_TIMEOUT_MS_HYDRATED)

            def _harvest():
                """Read the cards on screen now, and say which ones they were.

                The returned set is what lets the walk notice it has outrun
                itself: two consecutive reads with no id in common mean the
                window jumped a stretch that will never be read, because the
                nodes behind it are already recycled.
                """
                snapshot = d["content"]()
                if not snapshot:
                    return set()
                seen_now = set()
                for row in parse_rendered_cards(
                        snapshot, d["current_url"](), category=args.category,
                        currency=currency):
                    if not row.sku:
                        continue
                    seen_now.add(row.sku)
                    if row.sku not in walked_seen:
                        walked_seen.add(row.sku)
                        walked_rows.append(row)
                return seen_now

            outcome.walk = page_flow.walk_virtualised(
                count=d["count"], text=d["text"], scroll_by=d["scroll_by"],
                sleep=d["sleep"], harvest=_harvest,
                want_items=args.pages * page_flow.BATCH_HINT)
            outcome.walk["harvested"] = len(walked_rows)
            logger.info("The walk reached position %d in %d step(s) and "
                        "harvested %d distinct row(s).",
                        outcome.walk["counter_reached"], outcome.walk["steps"],
                        len(walked_rows))
            if outcome.walk["gaps"]:
                logger.warning(
                    "The walk outran its harvest %d time(s), so stretches of "
                    "the list went by unread and this run is PARTIAL. Its row "
                    "count is a floor.", outcome.walk["gaps"])
            if outcome.walk["counter_reached"] >= RESULT_CAP:
                logger.warning(
                    "This URL hit Craigslist's %d-result ceiling -- the site's "
                    "limit for ONE address, not the size of the catalogue. "
                    "Narrow the URL with the site's own filters and run each "
                    "separately.", RESULT_CAP)

        html = served_html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only when the page is NOT already content. A challenge marker on a
    # page whose products have rendered guards nothing — and over
    # --cdp-endpoint the Scraping Browser's own auto-solve extension injects
    # such markers into every page it loads.
    # Only for a state page_flow already counts as BLOCKED. An EMPTY page is
    # a correct answer, and in a sibling repo (tokopedia-scraper) a served
    # hub page reported exit 3 because that site's own performance script
    # names `akamaihd.net`. Mirrors playwright_scraper exactly.
    vendor = (detect_bot_challenge(html, url=d["current_url"]())
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    final_url = d["current_url"]() or url
    products = _parse_for_mode(html, final_url, args, page_num)
    if walked_rows:
        # Batch 1 came from the served list, complete and enriched. The walk's
        # rows come from the rendered grid and carry less -- no coordinates,
        # no structured price -- which is what `price_source` records.
        #
        # Rows already in batch 1 reappear here (the walk starts at the top of
        # the same list) and are dropped by the run's dedupe, in page order,
        # after the merge. Not here: doing it in two places is how the two
        # disagree.
        first_skus = {r.sku for r in products if r.sku}
        fresh = [r for r in walked_rows if r.sku not in first_skus]
        logger.info("The walk added %d row(s) beyond the served list's %d.",
                    len(fresh), len(products))
        for i, row in enumerate(fresh, start=1):
            row.page = 2 + (i - 1) // max(len(products), 1)
            row.position = 1 + (i - 1) % max(len(products), 1)
        products = products + fresh
    logger.info("Parsed %d row(s) from batch %d.", len(products), page_num)

    if args.mode == "listing" and page_num == 1:
        # The served markup publishes NO result total -- the rendered header
        # "Menampilkan 1 - 60 barang dari total  untuk …" with the total
        # EMPTY — so there is no arithmetic completeness check here. The
        # header is recorded verbatim instead.
        outcome.total_available = total_results(html, shown=len(products))
        outcome.header = search_header(html)
        if outcome.header:
            logger.info("The page's own result header says: %s", outcome.header)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        logger.info("Price coverage on batch %d: %d/%d (%.0f%%); the measured "
                    "floor is %d%%.", page_num, priced, len(products), share,
                    PRICE_FLOOR)
        if share < PRICE_FLOOR:
            logger.warning(
                "Only %.0f%% of batch %d carries a price, against a floor of "
                "%d%% measured across 14 captures where the lowest was 95%%. "
                "That is the read breaking rather than the listing being "
                "unusual.", share, page_num, PRICE_FLOOR)

        # How much of this batch the page's own structured data covered.
        # Measured against the rows that COULD carry it: a walked row comes
        # from a rendered card, which publishes none, so including those would
        # make the share fall as the walk succeeds.
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
                    "Structured coverage is %.0f%%, below the %d%% floor. A "
                    "low share can mean the served list and the JSON-LD were "
                    "rejected as misaligned, which is on purpose when the two "
                    "views disagree.", enrich_share, ENRICHMENT_FLOOR)
        else:
            logger.info("This listing published no structured data -- normal "
                        "for jobs, services and community, which ship no "
                        "JSON-LD ItemList at all.")

        with_geo = sum(1 for p in products if p.latitude is not None)
        logger.info("Coordinates on batch %d: %d/%d. Craigslist publishes "
                    "(0,0) as its placeholder outside North America and this "
                    "parser reports those as null.",
                    page_num, with_geo, len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = final_url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # Both modes are one row per product, so `sku` is the key for both.
    dedupe_key = "sku"
    # Only --mode posting is single-page: one advert is the whole job, while
    # a listing run walks a list. Mirrors playwright_scraper.
    stop_reason = "single_page_mode" if args.mode == "posting" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    session = None
    try:
        session = _Session(args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode == "listing":
            # ONE fetch is the whole run on this site. Every batch after the
            # first lives in the SAME document, reached by walking the
            # virtualised list -- `?page=` and `?s=` return this document byte
            # for byte -- so `_fetch_one_page` does the walk internally and
            # what comes back already spans the run.
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

            advertised = [h for h in _next_page_candidates(session, 1) if h]
            if advertised:
                # If this ever fires, the site has grown pagination markup and
                # page_flow.pagination_is_addressable deserves re-measuring
                # rather than being trusted. Checked in all three engines so
                # none of them notices it alone.
                logger.warning(
                    "This page advertises %d next-page link(s), which "
                    "Craigslist has not done on any page measured. This run "
                    "still walks. Worth re-measuring: %s",
                    len(advertised), advertised[0])

            walk = first.walk or {}
            if args.pages > 1 and not walk:
                # More than one batch was asked for and the walk never ran:
                # the page never hydrated. Saying so is what stops the run
                # reporting `complete` for a fraction of what was asked.
                stop_reason = "walk_did_not_start"
            elif walk.get("gaps"):
                stop_reason = "walk_gaps"
            elif walk.get("hit_cap"):
                stop_reason = "result_cap_reached"
    finally:
        if session is not None:
            session.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page", which is what the sibling repo does and
    # what fires on healthy runs here. There is no fixed batch size:
    # a three-page run returned 62, 60 and 62 rows, and multiplying the
    # largest page by the page count then declared the run short by 8. A
    # threshold that warns on every healthy run teaches the reader to ignore
    # it.
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
        # The LAST page of a listing is legitimately short — the catalogue
        # simply ran out — so it is excluded unless there are pages after it.
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
    # Mirrors the other two engines exactly: the walk trace plus the page's
    # own result header in --mode listing, because those are what say how
    # much of the list the run actually saw. --mode posting has none.
    extra = None
    walks = {o.page_num: o.walk for o in outcomes if o.walk}
    headers = {o.page_num: o.header for o in outcomes if o.header}
    if walks or headers:
        extra = {"walk": walks, "result_header": headers}
    capped = [n for n, w in walks.items() if w and w.get("hit_cap")]
    if capped:
        logger.warning(
            "The walk hit Craigslist's %d-result ceiling on batch(es) %s, so "
            "this run's row count is a floor. That ceiling is per ADDRESS: "
            "narrow the URL and run each part separately.",
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
        description="Craigslist scraper (Selenium edition). Cannot authenticate a "
                    "proxy or a remote CDP endpoint — see the module "
                    "docstring; playwright_scraper.py is the primary engine.")
    p.add_argument("--url", default=None,
                   help="Craigslist URL: /search/area/{slug}?cat=..., "
                        "/search/subarea/{slug}?cat=..., or "
                        "/view/d/{slug}/{token} with --mode posting. ONE "
                        "hostname worldwide -- the area is a path segment. "
                        "Required, unless CRAIGSLIST_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "posting"],
                   default="listing",
                   help="listing (default) or posting. posting reads one "
                        "/view/d/{slug}/{token} advert and adds the body "
                        "text, the attribute bag, every image, both "
                        "timestamps and Craigslist's classic numeric post id "
                        "-- the columns a listing row cannot carry. No "
                        "--pages in posting mode.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "`cat=` code from the URL, named where measured.")
    p.add_argument("--pages", type=int, default=1,
                   help="How many batches to collect (default 1). Craigslist "
                        "has no pages -- ?page= and ?s= return the same "
                        "document -- so a batch above the first is reached by "
                        "WALKING the rendered list inside the same page. One "
                        "URL tops out at %d results however far it is walked."
                        % RESULT_CAP)
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and capped at 1. No batch "
                        "of a Craigslist listing has an address of its own, "
                        "so there is nothing to hand a worker -- in any "
                        "engine. Use the site's own filters to make several "
                        "narrower URLs and run each separately.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: a query "
                        "matching nothing is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="craigslist_products", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy; credentials are stripped and a warning says "
                        "so. Use the Playwright or pyppeteer engine for an "
                        "authenticated exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
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
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note that "
                        "NO challenge has ever been observed on this site — a "
                        "refused request gets no page at all — so neither "
                        "setting has anything to act on today, and neither "
                        "helps with a refusal.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to a running browser at host:port. Must NOT "
                        "carry credentials — chromedriver's debuggerAddress "
                        "cannot send them, so a credentialed endpoint is "
                        "refused with exit 2 rather than silently failing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and CRAIGSLIST_URL is not set in the "
                "environment or in .env.")
    if not is_supported_host(args.url):
        # Refused rather than attempted: the selectors, the posting-path
        # pattern and the id alphabet are all Craigslist's, so another
        # classifieds site would not fail loudly -- it would return zero rows
        # and read as an empty listing.
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
        # A warning rather than an error: it IS a Craigslist URL and the run
        # will honestly report zero rows (exit 4). But a reader who tried the
        # obvious area URL first would otherwise conclude the tool is broken.
        logger.warning(
            "%s is an AREA LANDING page, not a result list. It carries the "
            "area's category links and no results of its own, so this run "
            "will return 0 rows and exit 4. A result list looks like "
            "/search/area/<slug>?cat=<code>, e.g. "
            "/search/area/newyork?cat=sss.", args.url)
    if args.mode == "posting" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read.", args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
