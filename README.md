# craigslist-scraper

[![release](https://img.shields.io/github/v/release/2scraper/craigslist-scraper?sort=semver)](https://github.com/2scraper/craigslist-scraper/releases)
[![tests](https://github.com/2scraper/craigslist-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/craigslist-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/craigslist-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/craigslist-scraper/actions/workflows/canary.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines: Playwright · Selenium · pyppeteer](https://img.shields.io/badge/engines-Playwright%20%C2%B7%20Selenium%20%C2%B7%20pyppeteer-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs-without%20an%20account-success)](#do-i-need-anything-to-run-this)

Reads Craigslist result lists and adverts into JSON or CSV, across all 714
areas the site publishes. Three interchangeable browser engines, one row
schema, and every number below measured rather than estimated.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python playwright_scraper.py \
    --url "https://www.craigslist.org/search/area/newyork?cat=sss" \
    --format both
```

```
[INFO] Parsed 353 row(s) from batch 1.
[INFO] Price coverage on batch 1: 349/353 (99%); the measured floor is 90%.
[INFO] Structured-data coverage on the served batch: 314/353 (89%).
[+] Saved 353 products -> craigslist_products.json
```

Two and a half seconds, no key, no proxy, no account.

---

## Do I need anything to run this?

**No.** Measured 2026-09-14: a datacentre address in Germany, with no proxy
and no key, was served the full New York for-sale listing — 536,962 bytes,
296 results — and no challenge of any kind appeared. Across 22 captures
covering six categories and five locales there were zero reCAPTCHA, hCaptcha,
Turnstile, DataDome, PerimeterX, Incapsula, Akamai and AWS WAF markers, and
the word "captcha" does not appear on any of them.

So this is one of the sites in this family where the paid products are
genuinely optional. What they buy, when you want it:

| | what it gets you |
|---|---|
| a residential **proxy** | a chosen exit country, and volume spread across many addresses instead of one |
| the **Scraping Browser API** | a remote browser you do not run or patch, with a persistent profile |
| **fingerprints** | a consistent device identity across runs |
| **captcha solving** | nothing here today — wired up in case a bot manager is switched on between deploys |

All four are 2Captcha products behind one key. See [`.env.example`](.env.example),
which documents every variable this code reads and nothing it does not.

---

## What you get

One row per advert, the family's column prefix first and this site's columns
after it. Cut from a real run — [`sample_output.json`](sample_output.json) and
[`sample_output.csv`](sample_output.csv) ship with the repo.

```json
{
  "source": "craigslist.org",
  "url": "https://www.craigslist.org/view/d/flushing-fully-serviced-pre-owned/1yTKyWioo6fXG76Mntgoxg",
  "sku": "1yTKyWioo6fXG76Mntgoxg",
  "title": "Fully Serviced Pre-owned Raleigh M50 DX Aluminum Mountain Bike",
  "price": 250.0,
  "currency": "USD",
  "price_source": "jsonld",
  "page": 1,
  "position": 1,
  "post_id": null,
  "area": "newyork",
  "location": "queens",
  "region": "NY",
  "latitude": 40.7605985603477,
  "longitude": -73.7967995699928,
  "image_count": 1
}
```

`--mode posting` adds the body text, the attribute bag (`odometer`,
`compensation`, `broker fee details` — they are category-shaped), every image,
both timestamps, and Craigslist's classic numeric `post_id`.

**Six columns are null on every row of every run**, and that is the site
rather than the scraper: Craigslist publishes no seller name, no ratings, no
review counts, no stock state and no was-price anywhere. They are kept
because consumers read this family's columns by name across repos, and each
one says in [`output_writer.py`](output_writer.py) what measurement
establishes it.

---

## Traps that look like bugs

Read these before concluding something is broken.

**There are no pages.** `?page=2` and `?s=296` return a **byte-identical**
response — same length, same first result. Not an empty set, not an error:
the same page. `--pages` therefore walks the site's own rendered list instead
of fetching more URLs, and `--concurrency` above 1 is refused, because a
worker is only useful if it can be handed an address of its own.

**One URL tops out at exactly 10,000 results.** Walked to exhaustion, the list
yields 10,000 distinct ids and then the counter stops. To go further, narrow
the URL: the site honours its own filters as real server-side parameters, so
each narrowed URL is independently fetchable. Measured on one base URL, same
minute:

| query | results in the served response |
|---|---|
| none | 294 |
| `&min_price=0&max_price=25` | 342 |
| `&min_price=26&max_price=100` | 313 |
| `&postedToday=1` | 356 |
| `&query=bike` | 293 |
| `&hasPic=1` | 330 |

Subarea slugs (`/search/subarea/lgi?cat=sss`) partition it too. This is also
how you parallelise a large job — one run per narrowed URL — since
`--concurrency` cannot help inside a single run.

**Whole categories publish no structured data.** Jobs, services and community
ship no JSON-LD `ItemList` at all; housing ships one with no prices in it.
Rows from those categories come from the served result list alone, so their
`currency` is `null` — the printed `$` is ambiguous across the USD, CAD and
MXN areas this site serves, and a guess in a data column is worse than a null.

**Coordinates are absent outside North America.** Craigslist publishes
`geo: {0, 0}` as its placeholder, and which adverts get it is decided by the
area: 0 of 1,249 structured entries across the US and Canadian captures, 37 of
37 in Berlin, 244 of 244 in Mexico City, 321 of 324 in Tokyo. This scraper
reports those as `null` rather than as a point in the Gulf of Guinea.

**The currency follows the AREA, not your exit IP.** Eight areas fetched
through a single US residential exit returned USD, CAD, EUR, JPY and MXN.
There is no `--country` flag for the same reason: the area is a path segment,
so a flag could only disagree with the URL.

**An area landing page has no results.** `/area/newyork` carries that area's
category links and nothing else, so a run against it honestly reports 0 rows
and exit 4. A result list looks like `/search/area/newyork?cat=sss`.

**A busy area turns over faster than you expect.** Measured on New York for
sale: a walk of ~1,000 rows covered **under two hours** of postings — 451 rows
posted within the last hour, 278 in the hour before. Two runs two hours apart
shared **no ids at all**, while two runs forty seconds apart shared all 350.
So `diff_runs.py` against that URL at a two-hour interval reports everything
as delisted and everything as new, correctly and uselessly. Diff at short
intervals, or narrow the URL to something that moves more slowly. It also puts
the 10,000-result ceiling in perspective: on that area it is about twenty
hours of postings.

**The site's own data is sometimes nonsense, and is reported as-is.** One
Tokyo advert publishes ¥2,500,013,000 for a set of IKEA shelves, in both its
structured data and its printed price, while its title says ¥13,000 each.
Correcting it would make this output disagree with the site for reasons no
consumer could audit.

---

## How it reads the page

Unusually for this family, **the served markup is richer than the rendered
page**, and the scraper is built around that.

Craigslist ships a complete no-JS result list in the first response, with a
JSON-LD `ItemList` carrying structured price, currency, coordinates and images
alongside it. Its application then **removes that list about twenty seconds
in** and paints a virtualised grid of ~200 recycled nodes in its place.

```
t =  3.5s .. 20.5s   served list = 334 entries   rendered cards =   0
t = 22.5s            served list =   0           rendered cards =   0     <- neither
t = 26.6s            served list =   0           rendered cards = 200
```

So **a single-batch run disables JavaScript**. The list is never removed, the
hydration wait is not paid, and the window where the page has neither view
cannot be hit. The browser still does all the fetching — TLS, headers,
cookies, the proxy, the remote profile — it simply does not run the site's
application. `--pages > 1` turns JavaScript back on, because walking past the
first batch is the one thing that needs it.

The two views are **not positionally aligned**: the JSON-LD is an
order-preserving *subsequence* of the result list, and the entries it skips
vary. Matching them by position would give every row after a skip its
neighbour's price and coordinates. If the alignment cannot be reconciled the
enrichment is dropped wholesale rather than guessed at, and `price_source`
records which view each row's price came from.

---

## Engines

All three produce identical output and agree on exit codes, run status, and
whether a run crashes or spends money. Differences that are real and measured:

| | Playwright | Selenium | pyppeteer |
|---|---|---|---|
| authenticated `--cdp-endpoint` | yes | **no** — `debuggerAddress` has nowhere for a password | yes |
| authenticated `--proxy` | yes | **no** — credentials are stripped, with a warning | yes |
| JavaScript off over a remote profile | no | no | **yes** — per page |
| status | primary | secondary | secondary; upstream is unmaintained |

That third row matters more than it looks. Over `--cdp-endpoint`, Playwright
and Selenium cannot switch JavaScript off, so they read the rendered grid —
about 200 rows with no coordinates — where pyppeteer reads the served list:
**352 rows, 100% priced, 96% with coordinates**. For a single-batch run over a
remote profile, pyppeteer is the better choice.

### And a fourth way, with no browser at all

`scraper_api_client.py` POSTs a URL to 2captcha's **Scraper API** and parses
the HTML it gets back. On most sites in this family that is a compromise; here
it is a first-class path, for the same structural reason the rest of this page
keeps coming back to — Craigslist serves a complete result list in its first
response.

```bash
python3 scraper_api_client.py --url "https://www.craigslist.org/search/area/newyork?cat=sss"
```

**350 rows, ~5 seconds, $0.0005, no Chromium anywhere** — against 353 from
`playwright_scraper.py` on the same URL, with the same columns and within a
percent of the same coverage. Two runs forty seconds apart returned all 350 of
the same ids.

What it cannot do is walk: there is no `--pages` there, because batches beyond
the first live behind the site's virtualised grid. For more than one fetch's
worth, either use a browser engine or narrow the URL and fetch each narrowed
URL — which at that price is reasonable.

Install exactly one engine. Their pins are mutually unsatisfiable —
playwright and pyppeteer disagree on `pyee`, pyppeteer and selenium on
`urllib3` — so use a virtualenv per engine if you want more than one.

```bash
pip install -r requirements.txt -r requirements-selenium.txt
pip install -r requirements.txt -r requirements-puppeteer.txt
```

pyppeteer's own bundled Chromium does not launch on every machine; pass
`--chromium-path` at another Chromium if it fails to start.

---

## Measured, 2026-09-14

Every figure here comes from a run whose artefacts are in the repo's history.
Shares are given as ranges where they legitimately vary between runs.

| | |
|---|---|
| rows from one served response | 41 (Berlin) to 359 (Toronto); 294–358 for a large area |
| rows with a price | 95%–100% |
| rows with structured enrichment | 66% (Paris) to 99% (Toronto), where the category publishes any; **0%** for jobs, services and community |
| rows with coordinates | 89% (New York) to 0% (any non-US area) |
| `--pages 3`, walking | 1,091 rows, counter reached 903 in 45 steps, repeatable across runs |
| time for one batch, JavaScript off | ~2.5s |
| currencies observed | USD, CAD, EUR, JPY, MXN |
| Scraper API, one fetch, no browser | 350 rows, ~5s, $0.0005 |
| turnover, New York for sale | ~500 new adverts an hour; ~1,000 rows span under two hours |

---

## Usage

```bash
# one batch of a result list
python playwright_scraper.py --url "https://www.craigslist.org/search/area/newyork?cat=sss"

# three batches, walking the rendered list
python playwright_scraper.py --url "https://www.craigslist.org/search/area/berlin?cat=sss" --pages 3

# one advert, with body, attributes, images and timestamps
python playwright_scraper.py --mode posting \
    --url "https://www.craigslist.org/view/d/new-york-dji-neo-fly-more-combo-with/qTuXqiGHU326VAGH4msB5A"

# a price band, which is how you get past the 10,000 ceiling
python playwright_scraper.py --url "https://www.craigslist.org/search/area/newyork?cat=sss&min_price=0&max_price=100"
```

Exit codes: `0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero rows ·
`5` remote API error · `6` partial. A run writes `<out>.meta.json` beside its
output recording the status, the stop reason and which batches failed; a
**failed** run writes none, and leaves the previous good output in place.

`diff_runs.py` compares two runs by `sku` and refuses to compare runs that are
not both `complete`.

---

## Checks

```bash
python3 smoke_test.py     # 647 checks, no network, no browser
pytest                    # the same suite, wrapped
```

The fixtures are real captures, trimmed to whole nodes and verified to parse
identically to the untrimmed originals, with one advert's phone number and one
broker's name and licence numbers replaced by placeholders that show.

---

## Licence

MIT. This is a tool for reading a public listing site; what you do with the
data, and whether that is allowed where you are, is yours to decide.
