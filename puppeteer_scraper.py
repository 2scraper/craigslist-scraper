#!/usr/bin/env python3
"""
craigslist-scraper -- pyppeteer edition (secondary engine)
==========================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money -- the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode listing   (default)  a result list
    --mode posting              one /view/d/{slug}/{token} advert

pyppeteer is effectively unmaintained and its own README points at
Playwright. It is here for parity, and because it CAN do the one thing
Selenium cannot: authenticate a proxy and an authenticated remote CDP
endpoint. Prefer playwright_scraper.py.

There is no --concurrency here, and on this site there is none anywhere: no
batch of a Craigslist listing has an address of its own.

Usage
-----
    python puppeteer_scraper.py \\
        --url "https://www.craigslist.org/search/area/newyork?cat=sss" --pages 3

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          pyppeteer downloads its own Chromium on first run.
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

# At module level, deliberately, and not inside the launch path where it
# started out. The offline suite guards `import puppeteer_scraper` behind
# try/except ImportError and REPORTS the skip, and CI's engine-smoke job fails
# on any reported skip — that whole mechanism only works if importing this
# module actually requires the driver. With the import hidden inside
# _Session.open(), the module imported cleanly with no pyppeteer installed at
# all, the group never skipped, and CI could not have noticed a broken import.
# It also let CI install pyppeteer 0.0.25 (a stub, resolved from an unpinned
# `pip install pyppeteer`) without anything failing, because nothing ever
# imported it.
from pyppeteer import launch, connect

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
logger = logging.getLogger("puppeteer_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy. Measured across 14
# captures: 95%-100% on for-sale-type categories, 100% on housing, jobs,
# services and community. ENRICHMENT_FLOOR is the structured-data share,
# applied only to the rows that could carry it -- 66% (paris) to 99%
# (toronto) where the page publishes any, and a measured zero where it
# publishes none. Mirrors playwright_scraper.
PRICE_FLOOR = 90
ENRICHMENT_FLOOR = 60

# A page holding less than this share of the fullest page in the same run is
# reported as thin. There is no fixed batch size here (41 entries on the
# category pages captured), but the LAST page of a listing is legitimately
# short, so the bar stays loose.
THIN_PAGE_SHARE = 0.5

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share (how long to wait for
    challenge, when to scroll, when only a fresh session helps) and it is
    written against plain synchronous callables — which is the right shape for
    two of the three drivers. Bridging here keeps the policy in one place
    rather than growing an async copy of it that would drift.

    The second benefit is the one the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which is not something
    pyppeteer's own API offers.
    """

    def __init__(self):
        self.closing = False
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one as "Future exception was never retrieved:
        # NetworkError('Protocol error Target.sendMessageToTarget: Target
        # closed.')" — at ERROR level, AFTER a successful run has printed its
        # results. Five of those under a "Saved 48 products" line read as a
        # failed run. Only that shape is swallowed; anything else still gets
        # the default handler, because silencing the loop wholesale would hide
        # real faults.
        self.loop.set_exception_handler(self._on_loop_exception)  # see below
        self.loop.run_forever()

    def _on_loop_exception(self, loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the library's
        # in `exception` (a NetworkError about a closed CDP session), and an
        # `or` between them looks at the exception and never sees the message
        # — which is why these kept printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                # asyncio's own words when the loop stops with work in
                # flight. Emitted after a successful run; see close().
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                # A CDP message addressed to a session that has gone away.
                # Routine over a remote browser: three of six captures of
                # this site had their target closed mid-scroll and succeeded
                # on the next attempt.
                "No session with given id",
                # A connect that timed out leaves pyppeteer's websocket reader
                # holding the failure nobody will ever read: the run already
                # got its own TimeoutError through the bridge and reported
                # exit 5. Printing the same failure again, as a traceback,
                # under an error message that already explained it, is how a
                # readable diagnosis turns into noise.
                "Task exception was never retrieved",
                "CancelledError",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        if self.closing:
            # Everything after teardown begins is teardown. The run's own
            # result is already decided by then, so nothing printed here can
            # change it -- it can only mislead.
            logger.debug("Ignoring post-teardown noise from pyppeteer: %s",
                         message)
            return
        loop.default_exception_handler(context)

    def run(self, awaitable, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        """Run one pyppeteer awaitable on the private loop, with a timeout.

        Takes any AWAITABLE, not only a coroutine, and the difference is not
        academic: `CDPSession.send` returns a Future rather than a coroutine,
        so `run_coroutine_threadsafe` rejects it with "A coroutine object is
        required" -- which is how the pyppeteer fingerprint path failed to
        apply a timezone while reporting that it had carried on without one.
        Every CDP call in this engine goes through here, so the wrapper is
        cheaper than remembering which library function returns which.
        """
        if not asyncio.iscoroutine(awaitable):
            async def _await_it(inner=awaitable):
                return await inner
            coro = _await_it()
        else:
            coro = awaitable
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's own background tasks
        pending — its websocket reader and keepalive — and asyncio then prints
        "Task was destroyed but it is pending!" plus a traceback for each of
        them. That happens AFTER the output has been written, so the run is
        fine and the log looks like a crash. Four tracebacks under a
        successful run is how a reader learns to ignore the log.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        async def _drain():
            """Cancel what is in flight and WAIT for it, then stop the loop.

            Cancelling without waiting is not enough, and the difference is
            visible in the log rather than in the data. pyppeteer's websocket
            reader reacts to its cancellation by closing the connection, which
            is itself async: if the loop stops first, that close runs against
            a dead loop and Python prints "Exception ignored in: <coroutine
            Connection._recv_loop>", "no running event loop" and "Event loop
            is closed" AFTER a run has already written its output and printed
            its results.

            The run is fine in every case; the log is what is wrong, and a log
            that ends in three tracebacks under a successful run is how a
            reader learns to stop reading it.

            Bounded, because teardown must not be able to hang a run that has
            already produced its answer.
            """
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelling %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
                try:
                    await asyncio.wait(pending, timeout=3)
                except Exception as e:  # noqa: BLE001 -- teardown only
                    logger.debug("Ignoring teardown error while draining: %s", e)
            self.loop.stop()

        self.closing = True
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(_drain()))
        self._thread.join(timeout=8)


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
    # Present only on a run that let the application paint. Kept so
    # the sidecar's shape matches the family's.
    total_available: Optional[int] = None
    # The raw result-count header, verbatim, for the sidecar.
    header: Optional[str] = None
    # What the lazy-load scroll did, and crucially whether it SETTLED. A page
    # whose grid was still growing when the budget ran out is partial, and a
    # run that reported it as complete would read as a shrinking catalogue.
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

    Not a hardcoded number: it drifts the moment a newer Chromium ships, and
    claiming an older Chrome than the JS engine and TLS handshake report is
    itself a mismatch a fingerprinter can key on. pyppeteer's
    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone, so the cookie jar goes with the exit.
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None
        # Whether this session runs the site's own application.
        #
        # A single-batch listing run does NOT, and on this site that is what
        # KEEPS the data: Craigslist serves a complete result list and its own
        # script removes it about twenty seconds later, replacing it with a
        # virtualised grid of 200 recycled nodes. With JavaScript off the list
        # stays and the hydration wait is not paid. The browser still does all
        # the fetching -- TLS, headers, cookies, the proxy.
        self.javascript = (args.mode == "listing"
                           and page_flow.javascript_needed(args.pages))

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            try:
                self.browser = self.bridge.run(
                    connect(browserWSEndpoint=self.args.cdp_endpoint,
                            ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
            except Exception as e:  # noqa: BLE001 -- see below
                # Caught broadly ON PURPOSE, and re-raised with the endpoint
                # NAMED. Two things were wrong without this, both found by
                # running the paid path rather than by reading it:
                #
                # The bridge's own timeout raises a bare TimeoutError whose
                # text is "pyppeteer call did not return within 30s" -- which
                # matches neither marker the handler at the bottom of this
                # file looks for, so a locked profile escaped as an unhandled
                # traceback and exit 1. A harness seeing exit 1 goes looking
                # for a bug in the scraper instead of waiting or passing a
                # different pid. The twin engine reports exit 5 for the same
                # condition, and the three must agree.
                #
                # And pyppeteer, like Playwright, puts the endpoint into its
                # exception text -- and the endpoint is a URL with a password
                # in it. Masked here, with host and port kept: WHICH endpoint
                # failed is the useful half and is not the secret.
                raise RuntimeError(
                    f"could not connect to --cdp-endpoint "
                    f"{_mask_credentials(self.args.cdp_endpoint)}: "
                    f"{_mask_credentials(str(e))}\n"
                    f"A Scraping Browser profile allows ONE live connection "
                    f"at a time, so this usually means another run still "
                    f"holds this `pid`. Wait for it to finish, or use a "
                    f"different pid."
                ) from None
            self.page = self.bridge.run(self.browser.newPage())
            self._apply_javascript()
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises
        # "signal only works in main thread of the main interpreter" because
        # the event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead, so nothing is
        # lost — the browser is still closed on both success and failure.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        if self.args.fingerprint:
            self._apply_fingerprint()
        self._apply_javascript()
        return self

    def _apply_fingerprint(self):
        """Apply a 2captcha fingerprint to this page.

        Only on the LOCAL branch. Over --cdp-endpoint the Scraping Browser
        already has its own, and layering a second on top produces a mismatch
        rather than better cover.

        The user agent comes through `fingerprint_user_agent`, never by
        reaching into the response: the API's key is `userAgent.userAgent`,
        a sibling engine read `userAgent.value`, and the result was a flag
        that reported success while setting nothing.
        """
        from fingerprint_client import (get_fingerprint, playwright_init_script,
                                        fingerprint_user_agent)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        screen = fp.get("screen") or {}
        intl = fp.get("intl") or {}
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            else:
                logger.warning("This fingerprint carries no user agent, so the "
                               "browser keeps its own.")
            if screen.get("outerWidth") and screen.get("outerHeight"):
                self.bridge.run(self.page.setViewport({
                    "width": int(screen["outerWidth"]),
                    "height": int(screen["outerHeight"]),
                    "deviceScaleFactor": float(screen.get("deviceScaleFactor") or 1)}))
            # The timezone the API states. Applying the screen and the UA and
            # not the clock is a contradiction of exactly the kind a
            # fingerprint exists to avoid.
            if intl.get("timeZone"):
                cdp = self.bridge.run(self.page.target.createCDPSession())
                self.bridge.run(cdp.send("Emulation.setTimezoneOverride",
                                         {"timezoneId": intl["timeZone"]}))
            # The same patch script the other two engines install, shared
            # deliberately: two engines applying different halves of one
            # fingerprint would contradict each other.
            self.bridge.run(self.page.evaluateOnNewDocument(
                playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s), UA %s, timezone %s",
                        fp.get("id"), fp.get("country"),
                        "set" if ua else "NOT set",
                        intl.get("timeZone") or "not stated")
        except Exception as e:  # noqa: BLE001 -- a fingerprint is not worth a run
            logger.warning("Could not apply the fingerprint (%s) -- continuing "
                           "without it.", e)

    def _apply_javascript(self):
        """Switch the site's application off when this run does not need it.

        pyppeteer can do this PER PAGE, which is the one place it beats both
        twins here: Playwright needs a fresh context (so it cannot do it over
        a reused remote profile) and Selenium needs a launch-time profile
        preference (so it cannot do it over an attached browser at all). Here
        it works on the remote path too.
        """
        if self.javascript:
            return
        self.bridge.run(self.page.setJavaScriptEnabled(False))
        logger.info("JavaScript is off for this run: one batch comes from the "
                    "served result list, which the site's own script would "
                    "otherwise remove before it can be read.")

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here; every decision about what to do
# with the answer is in page_flow.py so all three engines make it the same way.
def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        return len(bridge.run(page.querySelectorAll(selector)))

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land exactly on the document swap. None tells the
            # caller to skip a check rather than fail the run.
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

    def scroll_by(px):
        # A BOUNDED wheel, deliberately not a jump to the document's bottom.
        # The family's rule is the opposite and it is right for a lazy-loading
        # grid that grows as it loads. Craigslist's list is virtualised: its
        # container's height is pre-computed for all 10,000 items, so one jump
        # to scrollHeight lands on item 9,997 -- 700 ids collected, and then
        # the page passes every readiness test the usual rule prescribes.
        try:
            bridge.run(page.evaluate(
                "(px) => window.scrollBy(0, px)", px))
        except Exception:  # noqa: BLE001
            pass

    def text(selector):
        # The first match's text, or None -- used for the site's own
        # "1 - 6 of 10,000+" counter, which is what the walk's stall check
        # reads instead of DOM churn.
        try:
            el = bridge.run(page.querySelector(selector))
            if el is None:
                return None
            return bridge.run(page.evaluate("(e) => e.innerText", el))
        except Exception:  # noqa: BLE001
            return None

    # These are NAMED OPERATIONS rather than JavaScript crossing the page_flow
    # boundary: pyppeteer takes `() => expr` while Selenium takes a function
    # body with an explicit `return`, so a shared module passing JS would
    # acquire one driver's dialect.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url, "scroll_by": scroll_by, "text": text}


def _content(session) -> Optional[str]:
    return _driver(session)["content"]()


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
    """The site's own next-page link, resolved, or None.

    Returns EVERY candidate, filtered by page_flow. On Craigslist that filter
    returns nothing and the selector is empty, because no page measured
    carries a next-page link of any kind -- so this exists to NOTICE if that
    ever changes, not to be relied on.

    Reads the DOM's `.href` property rather than the raw attribute, which the
    browser has already resolved — the opposite of Playwright's
    get_attribute("href"). Kept explicit because the two engines differ here
    and a hand-rolled join got it wrong once.
    """
    bridge, page = session.bridge, session.page
    hrefs = bridge.run(page.evaluate(
        "(selector) => Array.from(document.querySelectorAll(selector))"
        ".map(a => a.href || a.getAttribute('href')).filter(Boolean)",
        page_flow.next_page_selector(page_num)))
    return page_flow.next_page_candidates(page.url, hrefs or [])


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine — see its docstring for why the anchor count is
    checked here rather than after the readiness wait.
    """
    bridge, page = session.bridge, session.page
    html = _content(session)
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = len(bridge.run(page.querySelectorAll(selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
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
    bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    The retry/rotate/wait policy is page_flow's and finish_run's; what differs
    here is only the driver calls. Kept structurally parallel on purpose —
    the two files are meant to be diffable, because "all three engines agree"
    is checked by reading them side by side as well as by the smoke suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)

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
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed = False
        for attempt in range(1, args.retries + 1):
            try:
                bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                           "timeout": 60000}))
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                # pyppeteer surfaces a dead proxy as a page error whose text
                # carries Chromium's own name for it, exactly as Playwright
                # does; a timeout and an unusable exit want opposite
                # responses, so they are told apart by that text.
                text = str(e)
                if any(marker in text for marker in _PROXY_ERROR_MARKERS):
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if load_failed and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = _content(session) or ""
        state = page_flow.classify(html, url=page.url)

        # "Not painted yet" is not a fault. A REAL state on this site rather
        # than a theoretical one: the application removes the served result
        # list about twenty seconds in and paints its own grid several
        # seconds later, so there is a window in which the page has neither.
        # Waiting is the right answer there -- retrying would throw away a
        # page that is about to be fine. Wait and
        # re-classify BEFORE the retry decision. Mirrors playwright_scraper
        # exactly; see page_flow.should_wait.
        if page_flow.should_wait(state):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Batch %d is a page Craigslist served whose results "
                        "painted (%d bytes, no grid) — waiting up to %.0fs "
                        "for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            need = page_flow.min_matches(args.mode)
            found = page_flow.wait_for_count(
                d["count"], d["sleep"], page_flow.ready_selector(args.mode),
                need, wait_timeout)
            if found <= need:
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content(session) or html
            state = page_flow.classify(html, url=page.url)

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
                html = _content(session) or html
                state = page_flow.classify(html, url=page.url)
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
            bridge, page = session.bridge, session.page
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
        # the diagnosis here, and a reader who finds no file cannot tell that
        # from a run that never got this far.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "This response was not a Craigslist page -- %d bytes, %s the site's "
            "own asset host, saved to %s. The check is structural rather "
            "than a marker list, which is what answers correctly for an "
            "interstitial, a block page and Chromium's own network-error "
            "page alike. No challenge vendor has ever been observed here, so "
            "a 2Captcha key is unlikely to be the answer; a different exit is "
            "the thing to try. This is exit 3, distinct from a genuinely "
            "empty result (exit 4).",
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
        # A POLL, not waitForFunction: that hands the browser a STRING to
        # evaluate, which a strict Content-Security-Policy refuses. See
        # page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            d["count"], d["sleep"], selector, threshold,
            page_flow.content_timeout_ms(args.mode, js=js))
        if found < threshold:
            if args.mode == "posting":
                logger.info("The advert's body did not appear in time. A "
                            "posting page is server-rendered on every category "
                            "measured, so this is unusual rather than slow; "
                            "the parse below decides.")
            else:
                logger.info("Fewer than %d result entries appeared in time. If "
                            "this URL's query matches nothing, that is the "
                            "expected answer and the run reports 0 rows "
                            "(exit 4).", threshold)

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

    # Only for a state page_flow already counts as BLOCKED. An EMPTY
    # page is a correct answer, and in a sibling repo (tokopedia-scraper)
    # a served hub page reported exit 3 because that site's own
    # performance script names `akamaihd.net`. Mirrors playwright_scraper
    # exactly.
    vendor = (detect_bot_challenge(html, url=page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    products = _parse_for_mode(html, page.url, args, page_num)
    if walked_rows:
        # Batch 1 came from the served list, complete and enriched; the walk's
        # rows come from the rendered grid and carry less, which is what
        # `price_source` records. Duplicates are dropped by the run's dedupe
        # after the merge, in page order -- not here, because doing it in two
        # places is how the two disagree.
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
        # The served markup publishes NO result total; the rendered header
        # says "of 10,000+", which is the ceiling one URL can be walked to
        # rather than a count of the catalogue. So there is no arithmetic
        # completeness check here, and the header is recorded verbatim.
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

        # Images are sparse on purpose: the site serves its own
        # `/images/d/<pid>/empty.png` placeholder for an advert with no
        # photograph, and this parser reports that as null rather than as a
        # URL. A column that is 100% populated and half placeholder is worse
        # than one that is honestly sparse.
        with_image = sum(1 for p in products if p.image_url)
        logger.info("Images on batch %d: %d/%d (%.0f%%).",
                    page_num, with_image, len(products),
                    100.0 * with_image / len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = page.url
    return outcome


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


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

    bridge = _AsyncBridge()
    session = None
    try:
        session = _Session(bridge, args, pool).open()
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
            walk = first.walk or {}
            if args.pages > 1 and not walk:
                stop_reason = "walk_did_not_start"
            elif walk.get("gaps"):
                stop_reason = "walk_gaps"
            elif walk.get("hit_cap"):
                stop_reason = "result_cap_reached"

    finally:
        if session is not None:
            session.close()
        bridge.close()

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
    # Mirrors the Playwright engine exactly: the walk trace plus the page's
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
            "this run's row count is a floor. That ceiling is per ADDRESS.",
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
        description="Craigslist scraper (pyppeteer edition). pyppeteer is "
                    "effectively unmaintained — playwright_scraper.py is the "
                    "primary engine.")
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
                   help="Accepted for flag parity and capped at 1: no batch "
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
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. "
                        "Credentials are sent over CDP (page.authenticate), "
                        "never on the browser's command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser supplies "
                        "its own.")
    # ONE OS-family tag, not a list -- and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400, so --fingerprint
    # failed on every invocation. Measured 2026-09-14: `Windows` succeeds;
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list -- "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. Use --fp-country to narrow further. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country -- a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
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
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so a credentialed Scraping "
                        "Browser endpoint works here.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium 117, which runs "
                        "under Rosetta far enough to print --version and then "
                        "fails to open its DevTools socket (measured "
                        "2026-09-08; the same failure occurs with no wrapper "
                        "code at all, so it is the build, not this engine). "
                        "Point it at a Chrome or Chromium of your own — "
                        "Playwright's, if you have it installed.")
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
