"""
product_parser.py
-----------------
Everything this repo knows about Craigslist lives in this file.

Two things about this site shape the whole module, and both were measured
rather than assumed. The numbers are in the comments beside the code that
rests on them.

**The served markup is richer than the rendered page.** Craigslist ships a
no-JS fallback -- `<ol class="cl-static-search-results">`, hidden by CSS
unless `body.no-js` -- carrying url, id, title, displayed price and location
for every result. Alongside it sits a JSON-LD `ItemList` with structured
price, `priceCurrency`, geo coordinates and images. The rendered SPA, by
contrast, virtualises: ~200 nodes in the DOM at a time, recycled as the window
slides. So the static list is the spine here and the browser is what lets us
walk PAST the first batch, which is the reverse of the usual arrangement in
this family.

**The two views are not positionally aligned.** The first capture taken held
296 entries in both, aligned perfectly, and building the join on that would
have been the "junk-link data theft" failure of the family playbook in a new
costume -- every row after the first skipped entry wearing its neighbour's
price and coordinates. Across 14 captures the counts differ almost everywhere
(266 vs 294, 325 vs 350, 244 vs 355). What holds in all 14 without exception
is weaker and sufficient:

    the JSON-LD ItemList is an order-preserving SUBSEQUENCE of the static
    list, matched on title, consumed 100%

so the merge walks the static list and advances the JSON-LD pointer only on a
match. A dict join on title would be wrong -- 16 of 294 static titles in one
capture are duplicates.
"""

import html as _html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger("product_parser")


# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------
#
# Craigslist consolidated onto ONE hostname. Its own site index at
# https://www.craigslist.org/about/sites links 714 distinct areas and exactly
# one hostname -- measured 2026-09-14 -- with the area as a path segment:
#
#     https://www.craigslist.org/search/area/newyork?cat=sss
#
# So there is no host table to build here and no per-country hostname to get
# wrong, which is the one per-site chore this site does not have.
CANONICAL_HOST = "www.craigslist.org"

# The old per-city subdomains still answer, with a 301 to the canonical host:
# `newyork.craigslist.org/search/sss` -> `www.craigslist.org/search/area/
# newyork?cat=sss`. They are accepted because a user who has one in a script
# should not be told it is not a Craigslist URL; `normalize_url` does not
# rewrite them, because guessing the redirect target is the site's job and it
# does it on every request.
_LEGACY_HOST_RE = re.compile(r'^(?:[a-z0-9-]+\.)?craigslist\.(?:org|co\.uk|ca|com\.au|de|fr|it|es|com\.mx|jp|sg|in)$', re.I)


def site_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_supported_host(url: str) -> bool:
    host = site_host(url)
    return host == CANONICAL_HOST or bool(_LEGACY_HOST_RE.match(host))


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be scraped, in words that point at the fix.

    Naming the reason matters more than refusing: "is not a Craigslist site"
    sends a reader hunting for a typo when the real problem is that they
    passed the posting form or an account page.
    """
    host = site_host(url)
    if not host:
        return "no hostname in URL"
    if not is_supported_host(url):
        return f"{host} is not a Craigslist hostname"
    if host in ("post.craigslist.org", "accounts.craigslist.org"):
        return f"{host} is the posting/account application, not a listing"
    return None


# --------------------------------------------------------------------------
# Selectors
# --------------------------------------------------------------------------
#
# Anchored on URL PATTERN and on the site's own semantic classes, never on a
# build hash. `cl-static-search-result` and `data-pid` are both stable names
# Craigslist writes itself.
SELECTORS = {
    # A posting link, in served and rendered markup alike.
    "item_link": 'a[href*="/view/d/"]',
    # The no-JS spine.
    "static_result": "li.cl-static-search-result",
    # The rendered card. Carries the numeric post id the static list does not.
    "rendered_card": "[data-pid]",
    # The site's own results container. Present on a served page whether or
    # not it holds anything, which is what makes "empty" separable from
    # "blocked" without reading any language-specific copy.
    "results_container": "ol.cl-static-search-results",
    # The rendered header's "1 - 6 of 10,000+". The readiness check is driven
    # off this rather than off DOM churn: inside the rendered window a scroll
    # adds no new ids, so a DOM-based stall check gives up on item 60 of 200.
    "visible_counts": ".visible-counts",
}

# How many product links mean "rendered". Must be > 1: waiting for one match
# resolves on an unrelated link long before the grid paints.
MIN_CARD_MATCHES = 5


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

# A posting: /view/d/{slug}/{22-char token}.
#
# The alphabet is base64url and the length is exact -- measured over 3,830
# tokens across all 22 captures: every one is 22 characters and every one
# matches [A-Za-z0-9_-]. The `-` and `_` matter. A first draft of this
# pattern used [A-Za-z0-9] and silently dropped 54 of 350 entries on a cars
# listing and 24 of 333 on jobs, because those categories' ids happen to use
# the two extra characters more often. It reported a clean run while losing
# 15% of it, which is this codebase's most common historical bug class.
_POSTING_PATH_RE = re.compile(r'^/view/d/[^/]+/([A-Za-z0-9_-]{22})/?$')
# The classic 10-digit id, present only on a RENDERED card's data-pid.
_POST_ID_RE = re.compile(r'^\d{8,12}$')

_SEARCH_PATH_RE = re.compile(r'^/search/(area|subarea)/([a-z0-9]+)/?$', re.I)
_AREA_PATH_RE = re.compile(r'^/(?:area|subarea)/([a-z0-9]+)/?$', re.I)

TRACKING_PARAMS = {"lang", "cc", "utm_source", "utm_medium", "utm_campaign",
                   "utm_term", "utm_content"}


def strip_tracking(url: str) -> str:
    p = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    return urlunparse(p._replace(query=urlencode(kept), fragment=""))


def sku_from_url(url: str) -> Optional[str]:
    """The 22-character token out of a posting URL.

    JSON-LD carries no `url`, no `sku` and no `offers.url` anywhere on this
    site, so every id in every row comes from here.
    """
    m = _POSTING_PATH_RE.match(urlparse(url).path)
    return m.group(1) if m else None


def posting_slug(url: str) -> Optional[str]:
    parts = urlparse(url).path.strip("/").split("/")
    return parts[2] if len(parts) >= 4 and parts[0] == "view" else None


def area_from_url(url: str) -> Optional[str]:
    """The area slug -- `newyork`, `tokyo`, `berlin`.

    This is what decides the currency on this site, and it is a path segment
    rather than a hostname or a cookie.
    """
    p = urlparse(url)
    m = _SEARCH_PATH_RE.match(p.path) or _AREA_PATH_RE.match(p.path)
    if m:
        return m.group(m.lastindex).lower()
    return None


def area_kind(url: str) -> Optional[str]:
    """`area` or `subarea` -- they are different slug namespaces."""
    m = _SEARCH_PATH_RE.match(urlparse(url).path)
    return m.group(1).lower() if m else None


# Craigslist writes the category as a three-letter `cat` parameter. The map
# below covers the codes actually captured and measured; an unknown code
# passes through verbatim rather than being dropped or guessed at, because
# the site has dozens and inventing names for unmeasured ones would put a
# guess in a data column.
CATEGORY_NAMES = {
    "sss": "for sale",
    "cta": "cars & trucks",
    "apa": "apartments / housing",
    "jjj": "jobs",
    "bbb": "services",
    "ccc": "community",
    "eee": "event calendar",
}


def category_from_url(url: str) -> Optional[str]:
    """The `cat=` code, named where it has been measured."""
    q = dict(parse_qsl(urlparse(url).query))
    code = (q.get("cat") or "").lower()
    if not code:
        return None
    return CATEGORY_NAMES.get(code, code)


def category_code(url: str) -> Optional[str]:
    q = dict(parse_qsl(urlparse(url).query))
    return (q.get("cat") or "").lower() or None


def listing_kind(url: str) -> str:
    """What kind of page this URL addresses.

    `posting` reads one advert, `search` reads a result list, `hub` is an
    area landing page which has no results of its own.
    """
    path = urlparse(url).path
    if _POSTING_PATH_RE.match(path):
        return "posting"
    if _SEARCH_PATH_RE.match(path):
        return "search"
    if _AREA_PATH_RE.match(path):
        return "hub"
    return "unknown"


# --------------------------------------------------------------------------
# Pagination -- there is none, and that is the dangerous part
# --------------------------------------------------------------------------
#
# Measured 2026-09-14: `?page=2` and `?s=296` on a search URL return a
# BYTE-IDENTICAL response -- 536,962 bytes, same first result. Not an empty
# set, not an error, not a redirect: the same page.
#
# That is the family's silent-single-page bug waiting to happen. A `page_url`
# built on the usual `?page=N` convention would fetch page 1 N times, find no
# new sku, conclude the listing was exhausted and report `complete` while
# holding a fraction of the data -- which is exactly how the bug survived in
# an older repo in this family for months.
#
# There is no `link[rel=next]`, no page number control, and the day paginator
# in the markup is inert (its element exists on for sale, housing and the
# event calendar alike; `is_visible()` is False on all three and its date
# label is empty).
#
# So: this site is NOT addressable page by page, and saying so out loud is the
# whole point of these two functions.
PAGINATED_KINDS: Tuple[str, ...] = ()


def paginates_by_url(url: str) -> bool:
    """False for every Craigslist URL, deliberately and measurably."""
    return False


def page_url(url: str, page_num: int) -> Optional[str]:
    """No Craigslist listing page has its own address.

    Returns None rather than constructing something plausible. A caller that
    wants page 2 has to walk the rendered list to it.
    """
    return None


def pagination_refusal(url: str) -> str:
    return ("Craigslist serves one address per listing: ?page= and ?s= are "
            "ignored and return a byte-identical page. Pages beyond the first "
            "batch are reached by walking the rendered list, so they cannot "
            "be fetched independently or concurrently.")


# The site caps ONE search URL at this many results, measured by walking the
# virtualised list to exhaustion: exactly 10,000 unique ids, then the counter
# stops. Past it, narrow the URL -- a price band, a subarea, a search term --
# and run each narrowed URL separately.
RESULT_CAP = 10000


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------

_GROUP_SPACES = "    "   # NBSP, narrow NBSP, thin space, space
_AMOUNT = (r'\d{1,3}(?:[.,' + _GROUP_SPACES + r']\d{3})+(?:[.,]\d{1,2})?'
           r'|\d+(?:[.,]\d{1,2})?')
# Craigslist prefixes the symbol on every locale captured: $70, ¥150,000,
# €150. Longest-first so a prefixed form is not swallowed by the bare one.
_SYMBOLS = ["CA$", "A$", "NZ$", "MX$", "US$", "$", "€", "£", "¥", "₹", "₩", "R$"]
_PRICE_RE = re.compile(
    r'(?:' + "|".join(re.escape(s) for s in _SYMBOLS) + r')\s*(' + _AMOUNT + r')')
# Percentages come out BEFORE prices are matched, never after: a rejected
# match has still consumed the symbol, so filtering afterwards loses the real
# price too.
_PCT_RE = re.compile(r'-?\s*%?\s*\d{1,3}(?:[.,]\d+)?\s*%')


def _normalize_amount(raw: str) -> Optional[float]:
    """Turn a written amount into a number, honouring all three groupings.

    `1,234.56` · `1.234,56` · `1 234,56`, the space form allowing NBSP,
    narrow NBSP and thin space -- a rendered page uses a no-break variant so
    the number does not wrap, and missing them parses `1 234` as 234.
    """
    s = raw.strip()
    for sp in _GROUP_SPACES:
        if sp != " ":
            s = s.replace(sp, " ")
    s = s.replace(" ", ",")
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # Whichever separator comes LAST is the decimal point.
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif has_comma:
        head, _, tail = s.rpartition(",")
        # Exactly three trailing digits is a thousands grouping: no currency
        # here has a three-digit subunit, so "$1,234" is 1234.
        #
        # ALL the separators come out, not just the last one. Removing only
        # the final group left "$1,234,567" as "1,234567", which float()
        # rejects, so every price with two or more groups came back as NO
        # PRICE AT ALL -- silently, on a column that looked healthy because
        # most adverts are cheap. It bites hardest exactly where prices are
        # large: a 1,700,000 MXN business, a 2,500,013,000 JPY listing, any
        # US property or vehicle over a million. Found by pinning a value
        # rather than a coverage percentage.
        s = s.replace(",", "") if len(tail) == 3 else s.replace(",", ".")
    elif has_dot:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def prices_in(text: str) -> List[float]:
    """Every symbol-prefixed amount in reading order."""
    out: List[float] = []
    for m in _PRICE_RE.finditer(_PCT_RE.sub(" ", text or "")):
        val = _normalize_amount(m.group(1))
        if val is not None:
            out.append(val)
    return out


# --------------------------------------------------------------------------
# Page state
# --------------------------------------------------------------------------
#
# The signals are ordered by how much they PROVE, not by how cheap they are.
# An earlier repo in this family put a threshold heuristic ahead of an
# unambiguous positive signal and reported exit 3 -- "blocked" -- for a
# perfectly good page that happened to reference the site's assets fewer
# times than the threshold wanted.

# A served Craigslist page is built out of Craigslist's own assets. Measured
# on all 22 captures -- every category, every locale, listings and postings
# and the empty-result page alike -- this appears exactly 3 times on each and
# 0 times on anything that is not a served page.
_ASSET_MARKER = re.compile(r'https://www\.craigslist\.org/static/')
_ASSET_MIN_MATCHES = 2   # measured 3 on 22 of 22; 2 leaves headroom

# Deliberately EMPTY, and that is a measurement rather than an oversight.
#
# Across all 22 captures there are zero occurrences of akamai, cloudflare,
# cf-turnstile, cdn-cgi, recaptcha, data-sitekey, hcaptcha, datadome and
# perimeterx -- and the word "captcha" itself appears zero times, on every
# page of every category and locale. Craigslist wires no challenge vendor
# into these pages at all.
#
# A marker that matches a good page is worse than no marker: it turns every
# successful run into a reported block. So nothing goes in here until it has
# been counted on a page known to be good and found to be absent.
BOT_CHALLENGE_MARKERS: Dict[str, str] = {}


def served_by_craigslist(html: str) -> bool:
    """Was this built out of the site's own assets?

    The one check that answers correctly for Chromium's own network-error
    page, which carries the site's hostname in its `<title>` and would fool
    any title or marker test.
    """
    return len(_ASSET_MARKER.findall(html or "")) >= _ASSET_MIN_MATCHES


def detect_bot_challenge(html: str, url: str = "") -> Optional[str]:
    """The vendor name of a challenge on this page, or None.

    Always None today. Kept wired because a bot manager can be switched on
    between deploys and a scraper that cannot name what stopped it is much
    harder to fix than one that can.
    """
    for vendor, marker in BOT_CHALLENGE_MARKERS.items():
        if re.search(marker, html or "", re.I):
            return vendor
    return None


def _results_container(soup: BeautifulSoup):
    return soup.select_one(SELECTORS["results_container"])


def is_no_results(html: str) -> bool:
    """Did the site serve a real page that simply has no matches?

    The unambiguous signal, and it needs no language: the site's own results
    container is present and holds zero result entries. Measured against a
    nonsense query -- 8,131 bytes, container present, holding only its "see
    also" hub-links item. No "no results" sentence is served at all, so a
    text marker would have had nothing to match in any locale.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    container = _results_container(soup)
    if container is None:
        return False
    return len(container.select(SELECTORS["static_result"])) == 0


def detect_page_state(html: str, status: Optional[int], url: str = "") -> str:
    """One of: content · empty · blocked · unpainted.

    Ordered by how much each signal PROVES, not by how cheap it is. A sibling
    repo put a threshold heuristic ahead of an unambiguous positive signal and
    reported exit 3 -- blocked -- for a perfectly good page that happened to
    reference the site's assets fewer times than the threshold wanted.

    So: the site's own containers first (a posting body, a results
    container), because no interstitial carries one; then the rendered grid;
    then vendor markers and refusing statuses; and only last the
    asset-reference heuristic, which is the weakest of the four and the only
    one a legitimately unusual page could trip.
    """
    html = html or ""
    soup = BeautifulSoup(html, "html.parser")

    # A POSTING page has no results container and never will, so it has to be
    # recognised on its own terms or every advert classifies as a listing that
    # has not painted. Its body is server-rendered on every category measured,
    # which makes it the same kind of unambiguous positive signal the results
    # container is for a listing.
    if soup.select_one("#postingbody") is not None:
        return "content"
    # A posting the site has taken down answers 200 with its own removal
    # notice and no body. That is an EMPTY answer, not a failure: the advert
    # is gone, which is a fact worth reporting rather than retrying.
    if soup.select_one(".removed, #removed") is not None:
        return "empty"

    container = _results_container(soup)

    if container is not None:
        if container.select(SELECTORS["static_result"]):
            return "content"
        # The application removes the static list once it takes over, so a
        # page with rendered cards is content even though the container they
        # came from is now empty.
        if soup.select(SELECTORS["rendered_card"]):
            return "content"
        # An EMPTY container is two different pages, and telling them apart
        # matters: one is a correct answer and the other is a page caught
        # mid-handover that will be fine in a moment.
        #
        # The application empties the container BEFORE it paints its grid, so
        # for a few seconds a perfectly good listing looks exactly like a
        # query that matched nothing. Found by running the Selenium engine:
        # `--pages 3` reported 0 rows and exit 4 on a page holding 329
        # structured items.
        #
        # The structured data is what separates them. A page mid-handover
        # still carries its JSON-LD `ItemList`; a genuinely empty result set
        # carries none -- measured 0 items against a nonsense query, and 329
        # on the page that was caught mid-handover.
        if _item_list(_json_ld_blocks(soup)):
            return "unpainted"
        return "empty"

    if soup.select(SELECTORS["rendered_card"]):
        return "content"

    vendor = detect_bot_challenge(html, url)
    if vendor:
        return "blocked"
    if status is not None and status in (403, 429, 503):
        return "blocked"
    if not served_by_craigslist(html):
        # Not built out of the site's assets and carrying no container: an
        # interstitial, a block page, or Chromium's own error page.
        return "blocked"
    # Served by the site, no container yet -- the SPA shell before the grid
    # has painted. This WAITS rather than retrying.
    return "unpainted"


# --------------------------------------------------------------------------
# Listing parsing
# --------------------------------------------------------------------------
#
# `parse_products` calls `parse_rendered_cards` and `page_currency`, both
# defined further down this file. That is resolved at call time, not at
# import, and the order is deliberate: the served list is what this site is
# read from, and the rendered grid is the fallback, so they are written in
# that order.

def _json_ld_blocks(soup: BeautifulSoup) -> List[Any]:
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            out.append(json.loads(raw))
        except (ValueError, TypeError):
            # A malformed block is not a reason to lose the page.
            continue
    return out


def _item_list(blocks: List[Any]) -> List[dict]:
    for d in blocks:
        if isinstance(d, dict) and d.get("@type") == "ItemList":
            items = d.get("itemListElement")
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
    return []


def _offer_of(node: dict) -> dict:
    """`offers` legally comes as a dict, a list, or an explicit null."""
    off = node.get("offers")
    if isinstance(off, list):
        for o in off:
            if isinstance(o, dict):
                return o
        return {}
    return off if isinstance(off, dict) else {}


def _images_of(node: dict) -> List[str]:
    """`image` is legally a string, an ImageObject, or a list of either."""
    raw = node.get("image")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    out = []
    for item in (raw if isinstance(raw, list) else [raw]):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            u = item.get("url") or item.get("contentUrl")
            if isinstance(u, str):
                out.append(u)
    return out


def _place_of(offer: dict) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
    """Locality, region and coordinates -- with null island rejected.

    Craigslist publishes `geo: {latitude: 0.0, longitude: 0.0}` as its
    PLACEHOLDER for "no coordinates", and it is not rare: measured across
    2,210 structured entries, 632 of them (28%) carry it, and which ones is
    decided entirely by the area. Every US and Canadian capture has zero of
    them; berlin has 37 of 37, mexicocity 244 of 244, tokyo 321 of 324 and
    paris 30 of 31.

    Emitting that as a coordinate would place every Berlin, Tokyo and Mexico
    City advert in the Gulf of Guinea, on a column that looks fully
    populated. Exactly (0, 0) is therefore read as absent -- both components,
    so a genuine equatorial or Greenwich coordinate is not thrown away with
    it.

    This is the second-locale rule earning its place: a US-only test bench
    would have shipped it.
    """
    place = offer.get("availableAtOrFrom")
    if not isinstance(place, dict):
        return None, None, None, None
    addr = place.get("address") if isinstance(place.get("address"), dict) else {}
    geo = place.get("geo") if isinstance(place.get("geo"), dict) else {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    lat = float(lat) if isinstance(lat, (int, float)) else None
    lon = float(lon) if isinstance(lon, (int, float)) else None
    if lat == 0.0 and lon == 0.0:
        lat = lon = None
    return (addr.get("addressLocality") or None,
            addr.get("addressRegion") or None,
            lat, lon)


def page_currency(html: str) -> Optional[str]:
    """The currency this PAGE states, from its own structured data.

    On this site the currency follows the AREA, not the exit IP -- measured
    by fetching eight areas through one US residential exit and getting USD,
    CAD, EUR, JPY and MXN back. Every item on a page carries the same code,
    so it is a fact about the page.

    Null when the page publishes no `ItemList` (jobs, services and community
    publish none at all) rather than defaulted from the `$` symbol, which is
    ambiguous across the USD, CAD and MXN areas this site serves.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for item in _item_list(_json_ld_blocks(soup)):
        node = item.get("item")
        if isinstance(node, dict):
            cur = _offer_of(node).get("priceCurrency")
            if isinstance(cur, str) and cur.strip():
                return cur.strip().upper()
    return None


def _static_entries(soup: BeautifulSoup) -> List[dict]:
    """The no-JS spine: one entry per result, in the site's own order."""
    out = []
    for li in soup.select(SELECTORS["static_result"]):
        a = li.find("a", href=True)
        if not a:
            continue
        title_attr = li.get("title")
        title_div = li.select_one(".title")
        title = (title_attr or (title_div.get_text(" ", strip=True) if title_div else "")).strip()
        price_div = li.select_one(".price")
        loc_div = li.select_one(".location")
        out.append({
            "url": a["href"].strip(),
            "title": _html.unescape(title),
            "price_text": price_div.get_text(" ", strip=True) if price_div else None,
            "location": loc_div.get_text(" ", strip=True) if loc_div else None,
        })
    return out


def _align_structured(entries: List[dict], items: List[dict]) -> Tuple[List[Optional[dict]], bool]:
    """Align the JSON-LD subsequence onto the static spine.

    Order-preserving, matched on title, pointer advanced only on a match.
    Returns one structured node (or None) per static entry, plus whether the
    alignment consumed every structured item.

    When it does not consume them all the two views disagree about what is on
    this page, and the caller drops the enrichment rather than guessing:
    overwriting a correct row is worse than leaving one uncorrected.
    """
    aligned: List[Optional[dict]] = []
    j = 0
    for entry in entries:
        node = None
        if j < len(items):
            candidate = items[j].get("item")
            if isinstance(candidate, dict):
                name = (candidate.get("name") or "").strip()
                if name and name == entry["title"]:
                    node = candidate
                    j += 1
        aligned.append(node)
    return aligned, j == len(items)


def parse_products(html: str, url: str, page: Optional[int] = None,
                   category: Optional[str] = None) -> List[Product]:
    """Every posting on a listing page, in the site's own order.

    `page` is threaded in rather than defaulted, because `position` restarts
    at 1 on each batch: without it, rows from batch 2 silently claim
    positions rows from batch 1 already hold.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    entries = _static_entries(soup)

    if not entries and soup.select(SELECTORS["rendered_card"]):
        # The served list is gone and the application's own grid is in its
        # place. That is not a failure -- it is what this page looks like
        # once JavaScript has run for long enough, and how long "long enough"
        # is depends on the browser: locally the list survived ~20s, while
        # over a remote Scraping Browser profile the swap had already
        # happened by the time the first snapshot was taken.
        #
        # So read the grid rather than returning zero rows from a page that
        # plainly has results on it. The rows carry less -- no coordinates,
        # no structured price -- and `price_source` says so on every one of
        # them.
        logger.info("The served result list is not in this page; reading the "
                    "rendered grid instead. Rows will carry price_source="
                    "'dom' and no coordinates.")
        return parse_rendered_cards(html, url, page=page, category=category,
                                    currency=page_currency(html))

    items = _item_list(_json_ld_blocks(soup))
    aligned, complete = _align_structured(entries, items)
    if not complete:
        logger.warning(
            "The page's structured data does not align with its result list "
            "(%d structured items, %d entries, alignment stopped early). The "
            "two views disagree about what is on this page, so the "
            "enrichment is dropped and rows carry their printed prices only. "
            "A row wearing its neighbour's coordinates would be worse than a "
            "row without any.", len(items), len(entries))
        aligned = [None] * len(entries)

    cat = category or category_from_url(url)
    area = area_from_url(url)
    # One currency per page, taken from the page's own structured data.
    cur = None
    for node in aligned:
        if node is not None:
            cur = _offer_of(node).get("priceCurrency")
            if isinstance(cur, str) and cur.strip():
                cur = cur.strip().upper()
                break
            cur = None

    # The rendered grid, when we were handed one, carries the classic numeric
    # id that the served markup does not. Keyed by posting URL, which both
    # views agree on.
    numeric_ids = {}
    for card in soup.select(SELECTORS["rendered_card"]):
        pid = (card.get("data-pid") or "").strip()
        if not _POST_ID_RE.match(pid):
            continue
        a = card.select_one(SELECTORS["item_link"]) or card.find("a", href=True)
        if a and a.get("href"):
            numeric_ids[strip_tracking(a["href"].strip())] = pid

    rows: List[Product] = []
    dropped: List[str] = []
    for position, (entry, node) in enumerate(zip(entries, aligned), start=1):
        clean_url = strip_tracking(entry["url"])
        sku = sku_from_url(clean_url)
        if not sku:
            # A link matching the shape by coincidence -- a hub link, a promo
            # card. Dropped rather than emitted with a null id, and COUNTED:
            # a parser quietly discarding entries is how a run reports
            # success while holding a fraction of the page.
            dropped.append(clean_url)
            continue

        price = None
        price_source = None
        if node is not None:
            raw = _offer_of(node).get("price")
            if raw not in (None, ""):
                try:
                    price = float(raw)
                    price_source = "jsonld"
                except (TypeError, ValueError):
                    price = None
        if price is None and entry["price_text"]:
            found = prices_in(entry["price_text"])
            if found:
                price = found[0]
                price_source = "static"

        locality = region = lat = lon = None
        images: List[str] = []
        if node is not None:
            locality, region, lat, lon = _place_of(_offer_of(node))
            images = _images_of(node)

        rows.append(Product(
            url=clean_url,
            sku=sku,
            title=entry["title"] or None,
            price=price,
            currency=cur if price is not None else None,
            image_url=images[0] if images else None,
            category=cat,
            price_source=price_source,
            page=page,
            position=position,
            post_id=numeric_ids.get(clean_url),
            area=area,
            location=entry["location"] or locality,
            region=region,
            latitude=lat,
            longitude=lon,
            image_count=len(images) or None,
        ))

    if dropped:
        logger.warning(
            "%d of %d result entries carried no recognisable posting id and "
            "were dropped (first: %s). If this is more than a stray hub link, "
            "the posting URL shape has changed.",
            len(dropped), len(entries), dropped[0])
    return rows


# --------------------------------------------------------------------------
# Posting parsing
# --------------------------------------------------------------------------
#
# A posting page is entirely SERVER-RENDERED, on every category and locale
# captured, which makes this mode the cheap one: no browser is needed to read
# one. It also carries things the result list does not -- the body text, the
# attribute bag, both timestamps, and Craigslist's classic numeric id, which
# on a listing page appears only in a rendered card's `data-pid`.

_POST_ID_LINE_RE = re.compile(r'post id:\s*(\d+)', re.I)
# The image id, without the size suffix. Craigslist serves the same image at
# several sizes (`..._600x450.jpg`, `..._300x300.jpg`); deduping on the id
# keeps one entry per photograph instead of one per rendition.
_IMAGE_RE = re.compile(r'https://images\.craigslist\.org/([A-Za-z0-9_]+?)_(\d+x\d+)\.jpg')


def _posting_body(soup: BeautifulSoup) -> Optional[str]:
    """The advert's own words.

    `#postingbody` opens with a print-only QR-code block -- a label reading
    "QR Code Link to This Post" and a div whose `data-location` is the page's
    own URL. Read without removing it, every body in the output starts with
    that sentence, on every row, and looks like something the poster wrote.
    """
    node = soup.select_one("#postingbody")
    if node is None:
        return None
    node = BeautifulSoup(str(node), "html.parser")
    for junk in node.select(".print-information, .print-qrcode, .print-qrcode-label"):
        junk.decompose()
    text = node.get_text("\n", strip=True)
    return re.sub(r'\n{3,}', "\n\n", text).strip() or None


def _posting_attributes(soup: BeautifulSoup) -> Dict[str, str]:
    """The attribute bag as the site prints it.

    Two shapes live under `.attrgroup`, and only one of them is labelled:

        <div class="attr condition">
            <span class="labl">condition:</span><span class="valu">good</span>
        <div class="attr important">
            <span class="valu year">2021</span>
            <span class="valu makemodel">toyota sequoia platinum</span>

    The unlabelled one is where cars put the year and the make/model, so
    skipping it would drop the two fields most worth having on a vehicle. Its
    own class names are the key instead.
    """
    attrs: Dict[str, str] = {}
    for attr in soup.select(".attrgroup .attr"):
        label_node = attr.select_one(".labl")
        values = attr.select(".valu")
        if label_node is not None:
            key = label_node.get_text(" ", strip=True).rstrip(":").strip().lower()
            val = " ".join(v.get_text(" ", strip=True) for v in values).strip()
            if key and val:
                attrs[key] = val
            continue
        for v in values:
            classes = [c for c in (v.get("class") or []) if c != "valu"]
            key = (classes[0] if classes else "").strip().lower()
            val = v.get_text(" ", strip=True)
            if key and val:
                attrs[key] = val
    return attrs


def _posting_times(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    """`posted` and `updated`, read by their LABEL rather than by position.

    Postings carry two or three `<time>` elements depending on whether they
    have been edited, so taking the first and second would put an edit time
    in `posted_at` on some adverts and not others.
    """
    posted = updated = None
    for p in soup.select(".postinginfos .postinginfo"):
        t = p.find("time")
        if t is None or not t.get("datetime"):
            continue
        label = p.get_text(" ", strip=True).lower()
        if label.startswith("posted"):
            posted = t["datetime"]
        elif label.startswith("updated"):
            updated = t["datetime"]
    return posted, updated


def _posting_images(html: str) -> List[str]:
    """One URL per photograph, deduped across the site's size renditions.

    In DOCUMENT ORDER, which is the advert's own order, so `image_url` is the
    photograph the poster put first rather than whichever URL happens to sort
    first. An earlier version sorted by the URL string and therefore picked an
    arbitrary image as the primary one on every multi-image advert -- found by
    trimming a fixture, when the trimmed copy disagreed with the original
    about which image came first.
    """
    best: Dict[str, tuple] = {}
    order: List[str] = []
    for m in _IMAGE_RE.finditer(html or ""):
        image_id, size = m.group(1), m.group(2)
        w, h = (int(x) for x in size.split("x"))
        prev = best.get(image_id)
        if prev is None:
            order.append(image_id)
        if prev is None or (w * h) > prev[0]:
            # Keep the largest rendition of each photograph, but at the
            # position its FIRST rendition appeared.
            best[image_id] = (w * h, m.group(0))
    return [best[image_id][1] for image_id in order]


def _posting_geo(soup: BeautifulSoup) -> Tuple[Optional[float], Optional[float]]:
    """Coordinates off the map element, with null island rejected as above."""
    node = soup.select_one("[data-latitude][data-longitude]")
    if node is None:
        return None, None
    try:
        lat = float(node["data-latitude"])
        lon = float(node["data-longitude"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if lat == 0.0 and lon == 0.0:
        return None, None
    return lat, lon


def _breadcrumb_category(soup: BeautifulSoup) -> Optional[str]:
    """The category from the advert's own breadcrumb.

    A posting URL carries no `cat=` parameter, so without this the column is
    null on every row of a posting run. The page states it -- the JSON-LD
    `BreadcrumbList` ends with the listing this advert belongs to -- and the
    site's own statement beats both a null and a guess off the slug.
    """
    for block in _json_ld_blocks(soup):
        if not isinstance(block, dict):
            continue
        crumbs = block.get("itemListElement")
        if not isinstance(crumbs, list):
            continue
        # The LAST crumb pointing at a result list -- not the last crumb,
        # which is the advert's own title. Measured on four postings: the
        # trail runs [site, area, subarea?, category, subcategory, title],
        # and only the middle entries carry a /search/ URL.
        category = None
        for crumb in crumbs:
            if not isinstance(crumb, dict):
                continue
            name, item = crumb.get("name"), crumb.get("item")
            if not isinstance(name, str) or not isinstance(item, str):
                continue
            if "/search/" in item:
                category = name.strip() or category
        if category:
            return category
    return None


def parse_posting(html: str, url: str, category: Optional[str] = None) -> Optional[Product]:
    """One advert, read off its own page.

    Returns None when the page is not a posting -- a deleted advert, an
    expired one, or a URL that addresses something else. None rather than an
    empty Product: a row of nulls with a real url in it is indistinguishable
    from a parsing failure by anything downstream.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    title_node = soup.select_one("#titletextonly")
    body = _posting_body(soup)
    if title_node is None and body is None:
        return None

    clean_url = strip_tracking(url)
    price_node = soup.select_one(".postingtitletext .price") or soup.select_one("span.price")
    price = None
    if price_node is not None:
        found = prices_in(price_node.get_text(" ", strip=True))
        if found:
            price = found[0]

    # The currency is taken from structured data where the page publishes it,
    # and left null otherwise. The printed `$` is ambiguous across the USD,
    # CAD and MXN areas this site serves, so deriving it from the symbol
    # would be a guess wearing the look of a fact.
    currency = None
    for block in _json_ld_blocks(soup):
        if isinstance(block, dict):
            cur = _offer_of(block).get("priceCurrency")
            if isinstance(cur, str) and cur.strip():
                currency = cur.strip().upper()
                break

    pid = None
    infos = soup.select_one(".postinginfos")
    if infos is not None:
        m = _POST_ID_LINE_RE.search(infos.get_text(" ", strip=True))
        if m:
            pid = m.group(1)

    posted, updated = _posting_times(soup)
    breadcrumb_category = _breadcrumb_category(soup)
    lat, lon = _posting_geo(soup)
    images = _posting_images(html)
    attrs = _posting_attributes(soup)

    return Product(
        url=clean_url,
        sku=sku_from_url(clean_url),
        title=_html.unescape(title_node.get_text(" ", strip=True)) if title_node else None,
        price=price,
        currency=currency if price is not None else None,
        image_url=images[0] if images else None,
        category=category or category_from_url(url) or breadcrumb_category,
        price_source="dom" if price is not None else None,
        post_id=pid,
        area=area_from_url(url),
        latitude=lat,
        longitude=lon,
        image_count=len(images) or None,
        body=body,
        attributes=attrs or None,
        images=images or None,
        posted_at=posted,
        updated_at=updated,
    )


# --------------------------------------------------------------------------
# The rendered grid
# --------------------------------------------------------------------------
#
# A second extraction path, and it exists for exactly one reason: walking
# past the first batch. The served list is complete for the first ~300
# results and is where a single-batch run gets everything, structured
# enrichment included. Beyond that the only view is the site's own
# virtualised grid, whose nodes are RECYCLED as the window slides -- so rows
# have to be taken while they are on screen, and what a card carries is less
# than what the served list plus its JSON-LD carries.
#
# The difference is visible in `price_source`, which is the column's whole
# job: "dom" rows have no coordinates and no structured price, because a
# card does not publish them. diff_runs.py already treats a price change
# that comes with a price_source change as `source_changed` rather than
# `changed`, so a run that walked and a run that did not do not read as a
# catalogue full of price movements.

_RENDERED = {
    "link": "a.posting-title, a.main",
    "title": "a.posting-title .label",
    "price": ".priceinfo",
    "location": ".result-location",
    "posted": ".result-posted-date",
    "image": "img[src]",
}


def parse_rendered_cards(html: str, url: str, page: Optional[int] = None,
                         category: Optional[str] = None,
                         currency: Optional[str] = None,
                         start_position: int = 1) -> List[Product]:
    """Rows for the cards currently in the document.

    `currency` is passed IN rather than read here. The page's JSON-LD is
    itself virtualised -- it tracks the visible window, holding 296 items on
    arrival and 316 after a walk -- so reading the currency from whatever
    happens to be in it at this moment would make one column depend on scroll
    position. It is a fact about the page, read once before the walk starts.

    `start_position` continues the numbering across harvests within one
    batch, so `position` stays meaningful when the caller stitches several
    reads together.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    cat = category or category_from_url(url)
    area = area_from_url(url)

    rows: List[Product] = []
    for offset, card in enumerate(soup.select(SELECTORS["rendered_card"])):
        link = card.select_one(_RENDERED["link"])
        href = (link.get("href") or "").strip() if link else ""
        if not href:
            continue
        clean_url = strip_tracking(href)
        sku = sku_from_url(clean_url)
        if not sku:
            continue

        title_node = card.select_one(_RENDERED["title"])
        title = (title_node.get_text(" ", strip=True) if title_node
                 else (card.get("title") or "").strip())

        price = None
        price_node = card.select_one(_RENDERED["price"])
        if price_node is not None:
            found = prices_in(price_node.get_text(" ", strip=True))
            if found:
                price = found[0]

        loc_node = card.select_one(_RENDERED["location"])
        posted_node = card.select_one(_RENDERED["posted"])
        img = card.select_one(_RENDERED["image"])
        img_url = (img.get("src") or "").strip() if img else ""
        # The site's own "no image" placeholder, served from its own host
        # under `/images/d/<pid>/empty.png`. Recognised positively rather
        # than let through: a column that is 100% populated and half
        # placeholder is worse than one that is honestly sparse.
        if img_url and "/empty.png" in img_url:
            img_url = ""

        pid = (card.get("data-pid") or "").strip()
        rows.append(Product(
            url=clean_url,
            sku=sku,
            title=_html.unescape(title) if title else None,
            price=price,
            currency=currency if price is not None else None,
            image_url=img_url or None,
            category=cat,
            price_source="dom" if price is not None else None,
            page=page,
            position=start_position + offset,
            post_id=pid if _POST_ID_RE.match(pid) else None,
            area=area,
            location=loc_node.get_text(" ", strip=True) if loc_node else None,
            # Relative on a card ("<1hr ago", "3 days ago"), so it goes in
            # the column named for what it is rather than being turned into
            # a timestamp by arithmetic against the clock of whoever ran it.
            posted_at=posted_node.get_text(" ", strip=True) if posted_node else None,
        ))
    return rows


# --------------------------------------------------------------------------
# What the page says about itself
# --------------------------------------------------------------------------

def search_header(html: str) -> Optional[str]:
    """The site's own description of this listing.

    Craigslist writes it into the static header -- "craigslist For Sale in
    New York City", or with the query quoted when there is one. Useful in the
    run sidecar because it is the site's OWN statement of what was asked for,
    which is the thing to compare against when a run returns a surprise.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    node = soup.select_one(".cl-static-header")
    if node is None:
        return None
    text = _html.unescape(node.get_text(" ", strip=True))
    return re.sub(r"\s{2,}", " ", text).strip() or None


_TOTAL_RE = re.compile(r'of\s+([\d,]+)(\+?)')


def total_results(html: str, shown: Optional[int] = None) -> Optional[int]:
    """How many results the site says this listing has, if it says.

    Only the RENDERED header carries this ("1 - 6 of 10,000+"), so it is
    None on a served-markup run -- which is honest rather than a gap: the
    served list does not state a total and guessing one from what arrived
    would turn `shown` into a fake total.

    The `+` matters and is deliberately not preserved as a number: 10,000 is
    the ceiling one URL can be walked to, not a count of the catalogue, and
    `RESULT_CAP` is where that fact lives.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    node = soup.select_one(SELECTORS["visible_counts"])
    if node is None:
        return None
    m = _TOTAL_RE.search(node.get_text(" ", strip=True))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None
