"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   a search result list -> Product
    --mode posting   one /view/d/{slug}/{token} advert -> Product, with the
                     trailing posting-only fields populated

Both modes yield the SAME class: on Craigslist an advert is not a different
kind of object from its result-list entry, it is the same advert described
more fully. So there is no second dataclass here, and `diff_runs.py` can
compare a listing run against a posting run on the columns both populate.

`Product` keeps the family's first sixteen columns in the family's order,
with the Craigslist-specific ones appended after `price_source`, so a
consumer written against another repo in this family still reads the prefix
unchanged.

Six of those sixteen are null on every row of every run, and that is a
property of the site rather than a parsing failure -- Craigslist publishes no
seller name, no ratings, no review counts, no stock state and no was-price
anywhere. They are documented one by one below with what was measured, and
kept rather than dropped because the family's consumers read these columns by
name across repos. The rule that a column null on every row should not exist
applies to columns this repo INVENTS; the shared prefix is a contract.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Craigslist consolidated onto ONE hostname --
# its own site index links 714 areas and exactly one host -- so this column
# is `craigslist.org` on every row of every run, and the AREA the row came
# from is a column of its own further down. Kept because the family's schema
# has it in this position.
SOURCE_DEFAULT = "craigslist.org"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The 22-character token out of a posting URL --
    # `/view/d/{slug}/qTuXqiGHU326VAGH4msB5A`.
    #
    # Craigslist has two ids per advert and this is the one that is actually
    # available: the JSON-LD carries no `sku`, no `url` and no `offers.url`
    # anywhere on this site, and the classic 10-digit id appears only on a
    # RENDERED card's `data-pid`. So a run that never opened a browser would
    # have a null id on every row if the numeric one were the sku. The token
    # is in served markup, is what the URL commits to, and is what a listing
    # row and a posting row join on. `post_id` below carries the numeric one
    # where a rendered grid was read.
    sku: Optional[str] = None
    title: Optional[str] = None
    # Null on every row of every run.
    #
    # Craigslist publishes no seller name, shop name or manufacturer on a
    # result list or on an advert -- measured across 22 captures covering six
    # categories and five locales. A marketplace of anonymous individuals has
    # no brand to state, and inventing one out of the posting body would put
    # a guess in a data column. Kept for the family prefix; see the module
    # docstring.
    brand: Optional[str] = None
    price: Optional[float] = None
    # From the page's own `offers.priceCurrency`, which is a FACT and is
    # never overwritten from the DOM.
    #
    # On this site the currency follows the AREA, not the exit IP: eight
    # areas fetched through ONE US residential exit returned USD (newyork,
    # losangeles, longisland), CAD (toronto), EUR (berlin, paris), JPY
    # (tokyo) and MXN (mexicocity). Every item on a page carries the same
    # code.
    #
    # Null -- never defaulted -- when the page publishes no structured data,
    # which is the case for whole categories (jobs, services and community
    # publish no `ItemList` at all). The `$` printed on those pages is
    # ambiguous across the USD, CAD and MXN areas this site serves, so a
    # symbol-derived currency would be a guess wearing the look of a fact.
    currency: Optional[str] = None
    # Null on every row of every run. Craigslist prints one price per advert
    # and has no strikethrough, no was-price and no EU Omnibus 30-day-low
    # disclosure anywhere -- 0 occurrences across all 22 captures. Kept for
    # the family prefix.
    original_price: Optional[float] = None
    # Null on every row, and necessarily: it is computed from `price` and
    # `original_price`, and there is no `original_price` on this site.
    discount_pct: Optional[float] = None
    # Null on every row. Craigslist has no ratings and no reviews -- there is
    # no seller reputation system on the listing side of the site at all.
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # Null on every row. An advert is either live or deleted; the site states
    # no stock level and no availability field, and a live advert is not a
    # promise that the item is still there.
    in_stock: Optional[bool] = None
    # The first image from the advert's structured data, where it has any.
    # Sparse ON PURPOSE: entries absent from the JSON-LD carry no image in
    # served markup, and whole categories (housing, jobs, services,
    # community) publish no images at all. `image_count` says how many the
    # advert actually has, so a null here is separable from a one-image
    # advert.
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from:
    #   "jsonld"   the page's own structured data, with its stated currency
    #   "static"   the no-JS result entry's printed price, when this entry is
    #              one of the ones the structured data skips
    #   None       no price found, which on this site usually means the
    #              advert states none
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about Craigslist.
    price_source: Optional[str] = None

    # ---- Craigslist-specific, appended so the family prefix stays stable ----
    # Which batch of the result list this row came from (1-based) and its
    # position within that batch as the site ordered it. Without `page`,
    # `position` is ambiguous -- it restarts at 1 on every batch, so rows
    # from batch 2 would silently claim positions batch 1 already holds.
    page: Optional[int] = None
    position: Optional[int] = None
    # Craigslist's classic 10-digit id (`7955359526`), from a rendered card's
    # `data-pid`. Null on a run that read only served markup, which is why it
    # is not the sku. Carried because it is the id Craigslist's own tooling
    # and every pre-2026 dataset uses, so it is what joins this output to
    # historical data.
    post_id: Optional[str] = None
    # The area slug from the URL -- `newyork`, `tokyo`, `berlin`. This is the
    # field that decides the currency on this site, and it is a path segment
    # rather than a hostname, a cookie or an exit IP.
    area: Optional[str] = None
    # The neighbourhood the advert prints ("SoHo", "Brooklyn", "Milford,
    # Ct"), from the result entry. Free text the poster chose, so it is not a
    # controlled vocabulary and should not be treated as one.
    location: Optional[str] = None
    # `addressRegion` from structured data -- "NY", "CT". Null where the
    # advert is one the structured data skips.
    region: Optional[str] = None
    # Coordinates from `offers.availableAtOrFrom.geo`. Craigslist publishes
    # these for result entries it includes in structured data, and they are
    # the advert's approximate location rather than an exact address.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # How many images the advert's structured data lists. Null rather than 0
    # where there is no structured data to count, so "no images" and "we did
    # not look" stay separable.
    image_count: Optional[int] = None
    # ---- populated by --mode posting only; null on a listing run ----
    # The advert's body text. Read from the server-rendered `postingbody`
    # element, which is present on EVERY category -- unlike the structured
    # `description`, which housing, jobs and services omit entirely.
    body: Optional[str] = None
    # The advert's attribute bag, as the site prints it: `odometer` for cars,
    # `compensation` / `experience level` / `job title` for jobs, `application
    # fee details` / `broker fee details` / `listed by` for housing. A
    # key/value mapping rather than a fixed column set, because the keys are
    # category-shaped and there are far too many to pin as columns.
    attributes: Optional[dict] = None
    # Every image URL the advert carries, not just the first.
    images: Optional[list] = None
    # When the advert was posted, ISO-8601 as the page states it.
    posted_at: Optional[str] = None
    # When it was last edited, where the page says so. Distinct from
    # `posted_at`: a relisted advert keeps its posting date and gains an
    # update one.
    updated_at: Optional[str] = None

# Row classes by --mode, so an engine maps its mode to a schema in one place.
# Both modes are Product here; the mapping exists so adding a mode later is a
# one-line change rather than a search for every place that assumed Product.
ROW_CLASS_BY_MODE = {"listing": Product, "posting": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a result list
# names each advert once, and a posting page IS one advert.
UNIQUE_BY_SKU_MODES = ("listing", "posting")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On Craigslist this DOES fire
    on healthy runs: page 1 and page 2 of one category listing shared
    exactly 3 products, all three from the "cheaper products" carousel that
    appears on every page of a listing. So a small non-zero drop count here
    is expected and a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On Craigslist this code specifically does NOT cover the ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here: Craigslist has never been observed sending an
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `craigslist.org` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On Craigslist that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]` and no numbered
# anchors anywhere, a CATEGORY listing is addressable by `?page=N`, and a
# SEARCH is not addressable at all — `?page=2` there returns an empty result
# set rather than page 2. So "no new products" is the one termination
# condition available on a search. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
