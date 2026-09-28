"""
page_flow.py
------------
Craigslist's page-state, walk and pagination policy, shared by all three
engines.

Why this module exists, when part of this family keeps each engine
self-contained: Craigslist answers a listing request in four ways and three
of them want a different response.

    content     the site's own results container holds entries, or the
                hydrated grid holds cards -- parse it
    empty       a real page whose results container is present and EMPTY.
                A query that matches nothing is the ordinary way to get one.
                Not a fault, not a retry, and not something that should send
                anyone looking for a proxy problem.
    unpainted   served by Craigslist, container gone, no cards yet. A real
                and reachable state on this site rather than a theoretical
                one -- see "The gap" below. It WAITS; it does not retry and
                it does not spend money.
    blocked     not built out of the site's own assets, or a refusing status.

Three copies of that triage across three engines would drift, and the drift
would be silent -- one engine reporting exit 3 where its twin reports exit 4
on the same URL. The family already shares `output_writer.finish_run()` for
exactly this reason; this is the same argument applied to the decisions that
come before it.

The served list is the fast path, not the fallback
--------------------------------------------------
This is the reverse of every other repo in this family, and it is measured.
Craigslist ships a no-JS results list in the served bytes -- complete, with
structured price, currency, coordinates and images aligned to it -- and it is
readable about three seconds after `domcontentloaded`. The hydrated grid
takes nine times longer to appear and then shows 200 virtualised nodes that
are recycled as the window slides.

The gap
-------
Sampling both counts every two seconds from `domcontentloaded`:

    t =  3.5s .. 20.5s   static = 334   cards =   0
    t = 22.5s            static =   0   cards =   0     <- neither
    t = 26.6s            static =   0   cards = 200

The application removes the served list about twenty seconds in and paints
its own grid several seconds after that. A readiness check that samples in
between sees an empty document, which is why `unpainted` exists as a state
and why it waits.

So JavaScript is DISABLED for a single-batch run
------------------------------------------------
With JS off the served list is never removed, the hydration wait is not paid,
and the race above cannot happen -- measured: 334 entries still present after
eight further seconds, cards 0. The browser still does the fetching (TLS,
headers, cookies, the proxy, the Scraping Browser profile); it simply does not
run the site's application. `--pages > 1` turns JavaScript back on, because
walking past the first batch is the one thing that genuinely needs it.

Walking the virtualised list inverts the family's scroll advice
---------------------------------------------------------------
The family's rule is to scroll to `document.body.scrollHeight` rather than
wheeling a fixed distance, because a fixed wheel stops short of a lazy-load
trigger. On a virtualised list that is actively wrong: the container's height
is pre-computed for all 10,000 items, so one jump to the bottom TELEPORTS
past everything. Measured -- 700 unique ids collected, then the page passes
every readiness test the rule prescribes: count steady, height steady, three
rounds unchanged.

So the walk here wheels a bounded distance and takes its stall signal from
the site's OWN header counter ("1 - 6 of 10,000+"), not from DOM churn:
inside the rendered window a scroll adds no new ids, so a DOM-based check
gives up while still on item 60 of 200.

The functions here are either pure or driven through small callables, so each
engine passes its own driver's primitives and keeps its browser plumbing to
itself:

    count(selector) -> int          how many elements match
    text(selector) -> Optional[str] the first match's text, or None
    content() -> Optional[str]      current HTML, None if unavailable
    current_url() -> str            the URL the browser is on
    sleep(ms) -> None               the driver's own wait
    scroll_by(px) -> None           wheel down by that many pixels

Deliberately no `evaluate(js)`: passing JavaScript from here would decide its
dialect for every driver, and they disagree -- Playwright and pyppeteer take
`() => expr` while Selenium's `execute_script` takes a function body with an
explicit `return`. So the OPERATION is named and each engine spells it in its
own dialect. And there is a second reason, measured on a sibling repo
(tokopedia-scraper): a page whose Content-Security-Policy forbids `unsafe-eval`
kills any wait built on an evaluated string.

Every value here is measured, the numbers are in the comments, and the
measurements are dated because Craigslist's markup moves: the April 2026
prototype in this repo's own history targeted per-city subdomains and
`/d/{slug}/{10 digits}.html` URLs, neither of which exists any more.
"""

import logging
import re
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from product_parser import (RESULT_CAP, SELECTORS, detect_page_state,
                            listing_kind, pagination_refusal, served_by_craigslist,
                            strip_tracking)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# "Ready" means either view is present, because which one we get depends on
# whether JavaScript is running and how long the application has had.
READY_SELECTOR_LISTING = ", ".join((SELECTORS["static_result"],
                                    SELECTORS["rendered_card"]))
# A posting page is entirely server-rendered on every category and locale
# captured, so its body is present in the first response. Not the title:
# `#titletextonly` is inside a heading that exists on a shell too.
READY_SELECTOR_POSTING = "#postingbody"

# How many matches mean "the list is there". Must be > 1: waiting for a
# single match resolves on an unrelated link long before anything paints.
# The served list arrives whole rather than in batches -- 41 entries on the
# smallest capture, 359 on the largest -- so 5 is clear of the bottom without
# excluding a genuinely small area.
MIN_CARD_MATCHES = 5
MIN_CARD_MATCHES_POSTING = 1

# With JavaScript off the served list is there as soon as the document is,
# measured at 3.5-4.4s over a residential exit. With it on, the hydrated grid
# took 26.6s in the same conditions. 20s and 45s leave room for the slow tail
# without letting a genuinely dead page hold a worker.
CONTENT_TIMEOUT_MS = 20_000
CONTENT_TIMEOUT_MS_HYDRATED = 45_000
CONTENT_TIMEOUT_MS_POSTING = 20_000


def javascript_needed(pages: int) -> bool:
    """Whether this run has to run the site's application at all.

    One batch is the served list, which JavaScript only takes away. More than
    one means walking the virtualised grid, which only JavaScript provides.
    """
    return pages > 1


def ready_selector(mode: str) -> str:
    return READY_SELECTOR_POSTING if mode == "posting" else READY_SELECTOR_LISTING


def min_matches(mode: str) -> int:
    return MIN_CARD_MATCHES_POSTING if mode == "posting" else MIN_CARD_MATCHES


def content_timeout_ms(mode: str, js: bool = False) -> int:
    if mode == "posting":
        return CONTENT_TIMEOUT_MS_POSTING
    return CONTENT_TIMEOUT_MS_HYDRATED if js else CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int],
                   sleep: Callable[[int], None],
                   selector: str,
                   want: int,
                   timeout_ms: int,
                   poll_ms: int = 500) -> int:
    """Poll until `selector` matches `want` elements, or the budget runs out.

    Polling `querySelectorAll` through the protocol rather than waiting on an
    evaluated string: the latter is what a strict Content-Security-Policy
    kills, and it spells differently in each of the three drivers.

    Returns the count actually reached, so a caller can tell "arrived" from
    "timed out with some of it" without a second query.
    """
    waited = 0
    seen = count(selector)
    while seen < want and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    return seen


# ---------------------------------------------------------------------------
# Walking the virtualised list
# ---------------------------------------------------------------------------
# One wheel step. Small enough that the rendered window slides rather than
# jumps -- a jump to the bottom collects 700 of 10,000 and looks settled.
SCROLL_STEP_PX = 2_500
# Measured: the counter advances about 460 positions per 25 steps at this
# step size, so the site's 10,000-item ceiling is ~550 steps away. The cap is
# generous rather than tight because the stall check below is what normally
# ends the walk.
WALK_STEPS_MAX = 900
SCROLL_PAUSE_MS = 500
# The smallest the adaptive step will go. Below this the walk crawls without
# collecting meaningfully more, because the rendered window holds 200-300
# nodes and a step this size already overlaps heavily.
SCROLL_STEP_MIN = 600
# What counts as a thin or a fat overlap between two consecutive harvests,
# as a share of the ids in the later one. Thin means the window is moving
# nearly as fast as the harvest can read, which is one bad step away from a
# gap; fat means there is room to move faster.
OVERLAP_THIN = 0.25
OVERLAP_FAT = 0.80
# What one unit of `--pages` means in items.
#
# There are no pages on this site, so the flag needs a size and the honest
# one is the site's own first-response batch. Measured across 14 captures:
# 41 entries on the smallest area (paris), 359 on the largest (toronto),
# with everything else between 294 and 358. 300 is the middle of that and
# is only ever a target for how far to walk -- no row count depends on it.
BATCH_HINT = 300

# How many consecutive steps without the counter advancing mean the end.
# Deliberately large: the counter pauses while a fetch lands, and a small
# tolerance ends the walk in the middle of the list. 40 steps is ~20s of
# no progress.
WALK_STALL_STEPS = 40

# "1 - 6 of 10,000+" -- the upper bound of the visible window is the number
# that advances as the walk proceeds.
_COUNTS_RE = re.compile(r'([\d,]+)\s*[-–]\s*([\d,]+)')


def window_high(counts_text: Optional[str]) -> Optional[int]:
    """The upper bound of the site's own visible-window counter.

    None when the header is absent or still says "retrieving more", which is
    a real state at the start of a walk rather than a failure.
    """
    if not counts_text:
        return None
    m = _COUNTS_RE.search(counts_text)
    if not m:
        return None
    try:
        return int(m.group(2).replace(",", ""))
    except ValueError:
        return None


def walk_virtualised(count: Callable[[str], int],
                     text: Callable[[str], Optional[str]],
                     scroll_by: Callable[[int], None],
                     sleep: Callable[[int], None],
                     harvest: Callable[[], set],
                     want_items: Optional[int] = None,
                     steps: int = WALK_STEPS_MAX,
                     step_px: int = SCROLL_STEP_PX,
                     pause_ms: int = SCROLL_PAUSE_MS,
                     stall_steps: int = WALK_STALL_STEPS) -> dict:
    """Slide the rendered window down the list, harvesting as it goes.

    `harvest` is called before every scroll and must RETURN THE SET OF IDS it
    saw. Two things depend on that, and the second is why it is not a plain
    callable:

    The nodes are RECYCLED. An id in the document now will not be in it two
    steps later, so anything not taken at the time is gone -- the whole
    difference between this and a lazy-load scroll, where nodes accumulate and
    one read at the end suffices.

    And the walk can OUTRUN its own harvest. Measured on two consecutive live
    runs of the same URL: one harvested 1,000 distinct rows by counter
    position 903, the other 360 by position 906. Same code, same page,
    different machine load -- when a harvest takes longer than the scroll
    step covers, the window moves past rows that are never read, and nothing
    in the result says so. A row count that swings 3x between runs while both
    report success is exactly the failure this codebase treats as its worst
    class.

    So each harvest's ids are compared against the previous one's. Overlap
    means the window slid; NO overlap means it jumped a gap. The step then
    adapts -- halved on a thin overlap, eased back up on a fat one -- and any
    gap that does happen is counted and returned, so the caller can report a
    partial walk instead of a confident wrong number.

    Returns a dict: how far the site's own counter got, how many steps were
    spent, and how many gaps were detected.
    """
    highest = 0
    stalled = 0
    gaps = 0
    taken = 0
    step = step_px
    previous: set = set()

    for taken in range(1, steps + 1):
        seen = harvest() or set()
        if previous and seen:
            overlap = len(seen & previous)
            if overlap == 0:
                # The window jumped clean past a stretch of the list. Those
                # rows are not recoverable by scrolling on -- they are behind
                # us and their nodes are gone.
                gaps += 1
                step = max(step // 2, SCROLL_STEP_MIN)
                logger.warning(
                    "The walk outran its harvest: no ids in common with the "
                    "previous read, so a stretch of the list went by unread. "
                    "Halving the scroll step to %dpx. This run is PARTIAL.",
                    step)
            elif overlap < len(seen) * OVERLAP_THIN:
                step = max(int(step * 0.75), SCROLL_STEP_MIN)
            elif overlap > len(seen) * OVERLAP_FAT and step < step_px:
                step = min(int(step * 1.25) + 1, step_px)
        previous = seen

        high = window_high(text(SELECTORS["visible_counts"]))
        if high is not None and high > highest:
            highest, stalled = high, 0
        else:
            stalled += 1
        if stalled >= stall_steps:
            break
        if want_items is not None and highest >= want_items:
            break
        if highest >= RESULT_CAP:
            logger.info("Reached the site's %d-result ceiling for one URL. "
                        "Narrow the URL -- a price band, a subarea, a search "
                        "term -- to reach more.", RESULT_CAP)
            break
        scroll_by(step)
        sleep(pause_ms)

    harvest()
    return {"counter_reached": highest, "steps": taken, "gaps": gaps,
            "hit_cap": highest >= RESULT_CAP, "final_step_px": step}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """The page's state, as `product_parser` decides it.

    A thin delegation on purpose: the signals live with the markup knowledge
    and the POLICY lives here, so an engine cannot quietly disagree with its
    twins about what a page was.
    """
    return detect_page_state(html or "", status, url)


STATE_POLICY = {
    "content":   {"retry": False, "solve": False, "blocked": False, "parse": True},
    # The site's own container, present and empty. A real answer to a real
    # question, and the commonest way to get one is a query that matches
    # nothing.
    "empty":     {"retry": False, "solve": False, "blocked": False, "parse": True},
    # Served by Craigslist, container gone, grid not painted. WAITS. Retrying
    # would throw away a page that is about to be fine, and solving would buy
    # a token for a challenge that is not there.
    "unpainted": {"retry": False, "solve": False, "blocked": False, "parse": False},
    # Not built out of the site's own assets, or a refusing status. A
    # different exit is the response; there is nothing to solve.
    "blocked":   {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Never observed on this site -- zero challenge markers and zero
    # occurrences of the word "captcha" across 22 captures. If a bot manager
    # is ever switched on, this is the state that would pay for it, and
    # `--solve-captcha when-blocked` still gates the spend on there being no
    # results on the page.
    "challenge": {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_wait(state: str) -> bool:
    """Whether this state resolves by waiting rather than by acting."""
    return state == "unpainted"


# A refused address is worth retrying from a different exit: that is what a
# pool is for, and a fresh browser at a fresh exit is the only thing that
# changes the answer. Consulted by every engine -- a policy constant nothing
# reads is the same defect as dead code.
RETRY_ON_BLOCKED = True
# Without a pool a retry goes back to the same address, so one is a
# formality and more is just noise in the log.
BLOCK_RETRIES_WITHOUT_POOL = 1
# At most one purchase per page, so a misdetection cannot spend in a loop.
SOLVES_PER_PAGE = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# There is none, and saying so is the point.
#
# Measured 2026-09-14: `?page=2` and `?s=296` on a search URL return a
# BYTE-IDENTICAL response. Not an empty set, not an error -- the same page.
# So the family's usual second layer, "reconstruct ?page=N when no selector
# matches", would fetch page 1 N times, find no new sku, conclude the
# listing was exhausted and report `complete` while holding a fraction of the
# data. That is the exact bug this family shipped once before.
#
# There is also no `link[rel=next]`, no page-number control, and the day
# paginator in the markup is inert: its element is present on for sale,
# housing and the event calendar alike, `is_visible()` is False on all three
# and its date label is empty.
NEXT_PAGE_SELECTOR: tuple = ()


def next_page_selector(page_num: int = 1) -> str:
    """Empty, deliberately. Nothing on this site advertises a next page."""
    return ""


def pagination_is_addressable(page1_url: str,
                              advertised_hrefs: Optional[List[str]] = None) -> bool:
    """False for every Craigslist listing, measurably.

    An engine that gets False here must fetch one page and walk it, must not
    plan page URLs, and must not run workers concurrently.
    """
    return False


def next_page_candidates(current_url: str,
                         advertised_hrefs: Optional[List[str]] = None) -> List[str]:
    """No addresses to try. Batch 2 lives in the same document as batch 1."""
    return []


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The most workers this URL can honestly be given.

    One, always. A worker is only useful if it can be handed an address of
    its own, and no page of a Craigslist listing has one.
    """
    return 1


def concurrency_refusal(url: str) -> str:
    return pagination_refusal(url)


def comparable(url: str) -> bool:
    """Whether two runs of this URL can be diffed against each other."""
    return listing_kind(url) in ("search", "posting")
