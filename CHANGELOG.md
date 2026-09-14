# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a command-line toolkit can. A **patch** release means fixes — not that
every flag is frozen. Where a fix changes what a default does, the release
notes say so first, because nobody should discover that from their output.

## [0.1.0] — 2026-09-14

First release of this repository on the family architecture. It replaces an
unreleased April 2026 prototype entirely, and not by choice: **the site that
prototype described no longer exists.**

| the prototype assumed | what Craigslist does now |
|---|---|
| `newyork.craigslist.org/search/sss` | **301** to `www.craigslist.org/search/area/newyork?cat=sss` |
| adverts at `/d/{slug}/{10 digits}.html` | `/view/d/{slug}/{22-char base64url token}` |
| 15 hard-coded city subdomains | **714 area slugs under ONE hostname** |

Nothing in the old code addressed a page that still exists, so none of it was
kept.

### Added

- Three interchangeable engines — `playwright_scraper.py` (primary),
  `selenium_scraper.py`, `puppeteer_scraper.py` — agreeing on exit codes, run
  status, and whether a run crashes or spends money.
- Two modes. `--mode listing` reads a result list; `--mode posting` reads one
  advert, adding its body text, attribute bag, every image, both timestamps
  and Craigslist's classic numeric post id.
- JSON and CSV output on the family's row schema, a `<out>.meta.json` sidecar
  per run, and `diff_runs.py` to compare two runs by `sku`.
- `--pages N` walks the site's own virtualised list, harvesting as the window
  slides. The walk is gap-aware: two consecutive reads with no id in common
  mean it outran its own harvest, which is counted, halves the step, and ends
  the run as PARTIAL rather than complete.
- 455 offline checks (`python3 smoke_test.py`, or `pytest`), with fixtures cut
  from real captures and verified to parse identically to the untrimmed
  originals.
- A nightly canary that needs no secret, because this site serves datacentre
  addresses.

### Notes on behaviour that may surprise

- **A single-batch run disables JavaScript.** Craigslist serves a complete
  result list and its own application removes it about twenty seconds later,
  replacing it with a virtualised grid. With JavaScript off the list stays,
  the hydration wait is not paid, and the run takes about 2.5 seconds.
- **`--concurrency` above 1 is refused.** `?page=` and `?s=` return the same
  document byte for byte, so no batch has an address of its own to hand a
  worker. The flag remains, for the family's CLI contract, capped at 1 with
  the reason.
- **One URL tops out at 10,000 results**, which is the site's ceiling and not
  this scraper's. Narrow the URL with the site's own filters to go further.
- **Six columns are null on every row**: Craigslist publishes no seller name,
  ratings, review counts, stock state or was-price anywhere. They are kept
  because consumers read this family's columns by name across repos.

### Fixed, in code inherited from this family

Each of these was live in the copied core and found by running it rather than
reading it:

- **Thousands grouping lost every price with two or more groups.**
  `$1,234,567` became `1,234567`, which `float()` rejects, so the row came
  back with no price at all. DOM path only; concentrated in the expensive
  adverts and outside the US.
- **`--fingerprint` set no user agent in the Selenium engine**, reading a key
  the API returns in neither format, while the shared helper had it right. The
  timezone the API states was applied by no engine but Playwright, and
  pyppeteer had no `--fingerprint` flag at all.
- **`image_url` was the alphabetically-first image**, not the advert's first.
- **A connect timeout in the pyppeteer engine escaped as a traceback and
  exit 1** instead of the exit 5 its twin reports for the same condition — a
  locked Scraping Browser profile being the commonest cause.
- **Three tracebacks printed after every successful pyppeteer run**, from
  cancelling its background tasks without waiting for them.
- **The credential scan's allowlist failed on its own repository.**
