# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a command-line toolkit can. A **patch** release means fixes — not that
every flag is frozen. Where a fix changes what a default does, the release
notes say so first, because nobody should discover that from their output.

## [Unreleased]

### Fixed

- **Text copied from sibling repos that described their sites as this one.**
  No behaviour changes except in two log messages:
  - `puppeteer_scraper.py`'s exit-3 message claimed a residential exit was
    "measured" to clear a refusal and that an Indonesian exit was compared.
    Neither is true of Craigslist; it now says what the other two engines
    say. `scraper_api_client.py`'s exit-3 message and its `--url` help
    described another site's routes and datacentre gate the same way, and
    `playwright_scraper.py`'s no-pool retry line claimed a re-fetch "is
    often what clears it" on this site, where no refusal has been observed.
  - `SECURITY.md` asked for a `tokopedia-scraper security` subject and named
    another site's protection and markup; it also said this project has no
    releases.
  - Both issue templates were written for another marketplace (its URLs, its
    bot manager, its currency rules, a tile overlay this repo does not
    have). Rewritten from this repo's README and TROUBLESHOOTING.
  - `CONTRIBUTING.md` described another site's anchors, seller ratings, sold
    counts and a carousel-driven dedupe; rewritten from this repo's parser
    and README.
  - Comments and docstrings in the engines, `output_writer.py`,
    `diff_runs.py` and `page_flow.py` that stated another site's pagination,
    hub pages, empty-search copy, currency and block behaviour as
    Craigslist's. Where a lesson came from a sibling it now names the
    sibling.
  - `requirements.txt` was headed `# tokopedia-scraper`.
- **An unused `_same_url` helper removed from all three engines.** Nothing
  called it, it described another site's URLs, and it compared the boolean
  `page_flow.comparable()` of two URLs rather than the URLs themselves.

## [0.1.3] — 2026-09-16

### Fixed

- **Fifteen lines of unreachable code removed from `playwright_scraper.py`.**
  A function's `def` line had been lost at some point before this repo's
  first commit, leaving its docstring and its `try: return page.content()`
  body indented into the end of `_mask_credentials`, where the control flow
  can never arrive. Nothing called it — `_content_when_settled` below it
  does the job — so no behaviour changes. The same fifteen lines, byte for
  byte, were in six repos of this family.

### Added

- **A check for a statement the control flow can never reach.** The
  undefined-name walk beside it cannot see this class by design: it pools
  every binding in a file rather than tracking scopes, so a name used inside
  dead code passes as long as anything else in the module binds it. The new
  one is narrow — a statement after a `return`/`raise`/`break`/`continue` in
  the SAME block — and measured across the eighteen repos of this family it
  found six real problems and zero false positives. Verified by control:
  appending `return 1` followed by a statement turns the suite red.

## [0.1.2] — 2026-09-14

> **The licence FILE was wrong in v0.1.0 and v0.1.1.** The badge, the
> README's own Licence section and `pyproject.toml` all said MIT, while
> `LICENSE` was 35 KB of GPL-3.0 inherited from the prototype this repo
> replaced. Both tarballs carry that file. It is now the MIT text every other
> repo in this family ships, which is what the rest of this one has claimed
> all along.

### Fixed

- `LICENSE` is MIT, matching what `pyproject.toml`, the README badge and the
  README's Licence section have said since 0.1.0.
- A check now asserts that the LICENSE file agrees with everything that
  claims a licence, and that no GPL text survives. Verified by putting the
  old file back: it fails on both counts.

## [0.1.1] — 2026-09-14

> **If you took v0.1.0, replace it.** Its `scraper_api_client.py` — a fourth,
> standalone CLI — still described a sibling site: its grid, its
> five-product fetch, its exit country, and an output prefix naming it. The
> file works, and everything it said about itself was about somewhere else.

### Fixed

- `scraper_api_client.py` rewritten for this site, and **run** — which turned
  out to matter, because on Craigslist the browserless path is the strong
  one: **350 rows, ~5s, $0.0005, no CDP routing at all**, against 353 from
  `playwright_scraper.py` on the same URL with the same columns and within a
  percent of the same coverage. Two runs forty seconds apart returned all 350
  of the same ids. The sibling repo this file came from warns that its site
  returns five; the difference is structural and reads the other way round
  here.
- `diff_runs.py` was watching columns that cannot change. `TRACKED_FIELDS`
  still held `sold` and `sold_is_floor`, which this schema does not have, so
  two of its eight comparisons were no-ops on every row of every diff — while
  `title`, `location` and `image_count`, which a poster edits routinely, were
  not watched. Its currency guard was backwards too: on this site the
  currency follows the AREA, and `source` is `craigslist.org` on both sides
  of every diff, so the currency is the only thing that catches a Tokyo run
  diffed against a Toronto one.
- `CONTRIBUTING.md` likewise still described the other site in eight places.
- Three documents were referenced and absent: `TROUBLESHOOTING.md` (now
  written), plus `FINDINGS.md` and `CLAUDE.md`, whose references were removed
  — a local working file has no business being named by a published one.
- `captcha_solver.get_balance` was referenced nowhere. Kept and given a
  consumer, because its error branch is the half nobody exercises: a wrong
  key returns HTTP 200 with an `errorId` in the body, so a client checking
  only the status code reads a failure as a balance.

### Added

- A measurement worth having: **a busy listing turns over in under two
  hours.** On New York for sale, 451 of 729 dated rows were posted within the
  last hour and 278 in the one before; two runs two hours apart shared *no*
  ids, while two runs forty seconds apart shared all 350. So `diff_runs.py`
  against that URL at a two-hour interval reports everything as delisted and
  everything as new — correctly, and uselessly. It also sizes the
  10,000-result ceiling: about twenty hours of that area's postings.
- Guards so none of the above can recur silently: every shipped file is
  counted for sibling-site names, every `ALL_CAPS.md` a file references must
  exist, every field `diff_runs` tracks must be a real column this site
  fills, and every public name must be read somewhere.

Offline checks: 653, up from 455.

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
- 647 offline checks (`python3 smoke_test.py`, or `pytest`), with fixtures cut
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
