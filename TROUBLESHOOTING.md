# Troubleshooting

Symptoms in the order you are likely to hit them. Every number here was
measured; where something is not measured, it says so.

---

## The run returned 0 rows and exited 4

**First: is the URL a result list?**

```
/search/area/{slug}?cat={code}      a result list        ✅
/search/subarea/{slug}?cat={code}   a subarea's list     ✅
/view/d/{slug}/{token}              one advert           ✅ with --mode posting
/area/{slug}                        an AREA LANDING page ❌ no results of its own
```

An area landing page carries that area's category links and nothing else. The
run is correct to report zero; the scraper warns by name when you pass one.

**Second: does the query match anything?** A `&query=` that matches nothing is
an honest empty answer — the site's own results container is served, empty.
`<out>.meta.json` will say `status: "complete"` with zero products, which is
different from a failure.

**Third: check the dump.** `--dump-html` writes the exact bytes the parser was
given, on success as well as failure. If that file contains
`cl-static-search-result` and you still got zero rows, the parser broke and
that file is the bug report.

---

## The run exited 3 (blocked)

This is the interesting one, because it has never been observed during this
repo's development. Craigslist served a datacentre address in Germany the
full New York listing, with no proxy and no key, on 2026-09-14 — and the
nightly canary runs from a GitHub runner for the same reason.

So exit 3 means something changed. The message names what was checked: the
response was not built out of the site's own assets. That check is structural
rather than a vendor marker list, which is what answers correctly for an
interstitial, a block page, and **Chromium's own network-error page** — that
last one carries `<title>www.craigslist.org</title>`, so anything reading the
title would call it a real page.

What to try, in order:

1. Read the debug dump the run wrote. If it is a Chromium error page, the
   problem is the network or the proxy, not the site.
2. A residential exit: `CRAIGSLIST_PROXY` in `.env`, socks5 on port 2333 (the
   http form on 2334 was measured dead for this credential).
3. The Scraping Browser API: `CRAIGSLIST_CDP_ENDPOINT`.

A 2Captcha solving key is unlikely to help. There is no challenge on this
site to solve — 22 captures across six categories and five locales, zero
markers of any vendor, and the word "captcha" appears zero times.

---

## Rows came back with no price / no coordinates

Usually the site, not the scraper. In order of likelihood:

**Whole categories publish no structured data.** Jobs, services and community
ship no JSON-LD `ItemList` at all, and housing ships one with no prices in
it. Those rows come from the served result list alone, so `currency` is
`null` — the printed `$` is ambiguous across the USD, CAD and MXN areas this
site serves, and a guess in a data column is worse than a null.

**Coordinates are absent outside North America.** Craigslist publishes
`geo: {0, 0}` as its placeholder. Measured: 0 of 1,249 structured entries
across the US and Canadian captures, 37 of 37 in Berlin, 244 of 244 in Mexico
City, 321 of 324 in Tokyo. This scraper reports those as `null` rather than
as a point in the Gulf of Guinea.

**A `$0` price is a price the advert states**, not a missing one.

**`price_source` tells you which view a row came from.** `jsonld` means the
page's structured data, `static` means its printed result entry, `dom` means
the rendered grid — and a `dom` row has no coordinates because a card does
not publish them.

---

## `--pages 3` returned about the same as `--pages 1`

The walk did not happen, or it stalled. Check the log for:

- `Walking the virtualised list` — if absent, the page never hydrated.
- `The walk outran its harvest N time(s)` — the window moved past rows nobody
  read. The run is reported PARTIAL rather than complete. It is load-related:
  a re-run on a less busy machine usually collects more, and the step halves
  itself when it happens.
- `hit the 10,000-result ceiling` — the site's limit for **one address**.
  Narrow the URL (`&min_price=`/`&max_price=`, `&query=`, `&postedToday=1`,
  `&hasPic=1`, or a subarea) and run each separately.

---

## `--concurrency 4` was ignored

Deliberately, and it is capped at 1 in all three engines. `?page=2` and
`?s=296` return a **byte-identical** response, so no batch of a listing has
an address of its own to hand a worker — every worker would re-fetch the
first batch. The flag stays for the family's CLI contract.

To spread a run, make several narrower URLs with the site's own filters and
run each separately.

---

## `diff_runs.py` reports that everything changed

Very likely real, and a property of the site rather than a fault: a busy
area's listing turns over fast. Measured on New York for-sale — a walk of
~1,000 rows covered **under two hours** of postings (451 rows posted in the
last hour, 278 in the hour before), and two runs two hours apart shared **no
ids at all**, while two runs forty seconds apart shared all 350.

So on a high-volume area, diff at short intervals, or narrow the URL to
something that moves more slowly.

It also refuses to compare two runs that are not both `complete`, and refuses
two runs quoting different currencies — which on this site means two
different AREAS, since the currency follows the area and `source` is
`craigslist.org` on both sides.

---

## Selenium refused my `--cdp-endpoint` / stripped my proxy password

Both are real limits of chromedriver, not bugs here:

- `debuggerAddress` takes a bare `host:port` with nowhere to put a password,
  so a credentialed endpoint is refused with exit 2 rather than connected to
  and silently failing.
- `--proxy-server=` accepts no credentials and there is no Selenium
  equivalent of `page.authenticate`, so they are stripped with a warning
  rather than letting you believe a `user:pass` URL is doing something.

Use `playwright_scraper.py` or `puppeteer_scraper.py`; both authenticate on
the WebSocket upgrade.

---

## Over `--cdp-endpoint` I get ~200 rows instead of ~350

Expected, and engine-dependent. A single-batch run reads the served result
list, which the site's application removes about twenty seconds in — and over
a remote profile, Playwright and Selenium cannot switch JavaScript off
(Playwright would need a fresh context, which would discard the profile;
Selenium needs a launch-time preference, and the browser is already running).
So they read the rendered grid instead: ~200 rows, no coordinates.

`puppeteer_scraper.py` **can** — it sets it per page — and returns the served
list over the same endpoint: 352 rows, 100% priced, 96% with coordinates,
measured.

---

## pyppeteer will not start

`Browser closed unexpectedly` means its own bundled Chromium does not run on
your machine, which is common on macOS. Point it at another one:

```bash
python puppeteer_scraper.py --chromium-path "/path/to/Chromium" --url ...
```

Over `--cdp-endpoint` it needs no local browser at all.

---

## The Scraper API returned HTTP 422 with `CDP connect failed`

It routed through `CRAIGSLIST_CDP_ENDPOINT` from your `.env` and that profile
was busy — a Scraping Browser profile allows one live connection. Wait a
minute, use a different `pid`, or drop the routing: on this site the Scraper
API's own exit is served, measured, and returns 350 rows for $0.0005.

---

## Still stuck

Run with `--dump-html` and open what the parser actually saw. A run can
return the right *number* of rows with a field silently unpopulated, and then
the exact bytes are the only way to tell a parsing bug from a too-early
snapshot.
