# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a command-line toolkit can. A **patch** release means fixes — not that
every flag is frozen. Where a fix changes what a default does, the release
notes say so first, because nobody should discover that from their output.

## [Unreleased]

### Added

- **CSV cells that a spreadsheet would run as a formula are prefixed with an
  apostrophe.** A cell beginning `=`, `+`, `-`, `@`, tab, CR or LF is executed
  by Excel, Sheets and LibreOffice, and a listing title is free text written
  by the seller. Only strings are touched (a `-5` price stays a number), it
  is applied after a list is joined into one cell, and it is CSV only: the JSON
  keeps the site's bytes. The count is recorded as `csv_cells_escaped` in the
  sidecar so the two outputs' divergence is declared rather than discovered;
  an engine's own `extra` wins a collision. Not measured on live Craigslist
  data, so it is not claimed to fire today. `scraper_api_client.py` calls
  `save` without a sidecar and prints the count instead.

### Fixed

- **Output files are written atomically.** `write_json`, `write_csv` and
  `write_run_meta` opened their target with a truncating `open`, so a crash,
  a kill or a full disk halfway through left a SHORTER file where the last
  good run had been - the previous output destroyed by the attempt to replace
  it, not by its outcome. The sidecar is the file a consumer branches on, so a
  truncated `.meta.json` beside good rows read as a broken run over fine data.
  Each now writes a temporary file in the target's own directory (a rename is
  only atomic within one filesystem), `fsync`s it, and renames it over the
  target. The temporary file is created 0600 and a rename keeps that, so the
  mode is set explicitly: an existing file keeps its own, a new one gets what
  `open()` would have given it. Checked by planting each fault and requiring
  the suite to go red on the check that names it.

- **An empty response is exit 5 on every engine, not exit 3.** A document
  with no text and no element (0 bytes, or the 39-byte
  `<html><head></head><body></body></html>` a browser holds after a
  navigation that failed) used to be classed as "blocked" by pyppeteer and
  Selenium, while Playwright reported the same failed navigation as exit 5.
  Measured 2026-10-01 against the live site: a wrong proxy password gave
  exit 3 on pyppeteer (0 bytes, 0.4 s) and Playwright exit 5 after 60 s;
  Selenium through a proxy it cannot send credentials to gave exit 3 on 39
  bytes. A refusal sends the reader looking for a block this site has never
  been seen to issue; the real fault was the proxy. The new `unreached`
  state is retried (a different exit is what a dead proxy wants), is not
  counted as blocked, and ends as `page_load_timeout`, exit 5. After the
  change all three agree: pyppeteer 0 bytes and Selenium 39 bytes both exit
  5, and a working proxy still returns rows. **Unchanged on purpose:** an
  empty body under HTTP 403/429/503 stays blocked, and Chromium's own
  network-error page (which has text) is still classed as blocked.

- **pyppeteer could not use an authenticated proxy on current Chromium.**
  `page.authenticate()` needs `Network.setRequestInterception`, which current
  Chromium no longer has. Measured 2026-10-05 with `--chromium-path` at
  Chromium 153: exit 1 and a traceback (`'Network.setRequestInterception'
  wasn't found`) before the first navigation, with a working proxy and with a
  wrong password alike. It still works on the Chromium pyppeteer bundles
  (r1181205), so it is tried first and only that one error falls back to CDP's
  Fetch domain (`handleAuthRequests`, as Playwright does). The fallback is
  slower because every request is paused and continued, and it answers only a
  challenge whose source is the proxy. After the change on Chromium 153 a
  working proxy returns 327 rows (exit 0) and a wrong password ends as exit 5.

- **The Scraper API path (`scraper_api_client.py`) failed whenever a wait flag
  was given, and never saw the target's status.** Measured 2026-09-23 against
  the live `/tasks/sync` endpoint: `waitFor` sent as a JSON-encoded string
  (what this client sent) is answered HTTP 422 "params.waitFor must be an
  object" and is still billed ($0.0005); sent as an object it is answered
  HTTP 200. It is now an object. And the response's `status` is the API's own
  verdict ("success"), while the target site's HTTP code is `http_code` — the
  client handed `status` onward, so a target 403/503 never reached the page
  classifier. It now reads `http_code`, falling back to `status` only if that
  is an integer. After the fix, one live call (`--wait-text craigslist` on the canary's New York search) answered HTTP 200, upstream 200, 301 rows.

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

- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.

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
