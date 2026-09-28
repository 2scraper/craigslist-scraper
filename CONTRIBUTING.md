# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Craigslist changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH
anchor broke. A result list is read from two views of the first response
(see the README's "How it reads the page"):

1. **The served no-JS list.** `ol.cl-static-search-results` holding one
   `li.cl-static-search-result` per result, with the posting link matched on
   its URL pattern (`/view/d/`). This is the spine: every row comes from it.
   If it moves the run reports 0 rows and exit 4, which is loud.
2. **The JSON-LD `ItemList`** beside it, which adds the structured price,
   `priceCurrency`, coordinates and images. It is an order-preserving
   subsequence of the served list, joined on title — never by position.
   Jobs, services and community publish none at all, by design.
3. **The rendered cards** (`[data-pid]`) and the header's `.visible-counts`,
   which only a walking run (`--pages > 1`) reads.

A third thing can break without any path failing: the **join** between the
served list and the structured data. When it breaks, the row count and the
prices stay healthy while currency and coordinates quietly empty out — so
every run logs its structured-data coverage and warns below
`ENRICHMENT_FLOOR`, applied only where the page publishes structured data
at all. If you are reporting a change, that percentage, the area and the
category are the numbers to include.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   Unlike its siblings this canary needs **no secret** and runs nightly:
   Craigslist serves datacentre addresses, and a GitHub runner is one. It
   makes two live runs — a walking one for the rendered grid and a one-batch
   one for the served list — because a walking run alone never exercises the
   served list at all, which is where the structured columns come from. That
   gap was found by dispatching it, not by reading it.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

The properties below are part of this repo's contract. The suite pins them,
so a PR that breaks one should fail rather than silently regress:

- **`sku` is the 22-character token in the posting URL.** JSON-LD carries no
  `url`, no `sku` and no `offers.url` anywhere on this site, so every id in
  every row comes from the URL; the classic numeric id is `post_id`, and only
  a posting page carries it.
- **The currency follows the AREA, not the exit IP, and a guessed one is
  null.** Rows without structured data report `currency: null`, because the
  printed `$` is ambiguous across the USD, CAD and MXN areas this site
  serves.
- **A block has never been observed here.** Craigslist served this repo's
  development machine — a datacentre address in Germany — the full listing
  with no proxy and no key. What it does to an address it has scored is
  therefore NOT measured, and nothing in this repo claims to know. Detection
  is structural: was this page built out of the site's own assets? That
  answers correctly for an interstitial, a block page and Chromium's own
  error page alike. `page_flow.STATE_POLICY` holds the retry/solve/blocked
  decision as data so the three engines cannot disagree about it.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked (a response not built out of the site's own assets),
  `4` zero rows — including an area landing page, which is a correct answer
  — `5` the content was never obtained,
  `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** An area
  landing page has no results of its own and a search that matches nothing
  has none; both are correct answers to the question that was asked.
  Retrying them spends the user's budget re-confirming the same answer, and
  rotating the exit blames an address for the URL it was given.
  `page_flow.STATE_POLICY` holds that for all three engines so they cannot
  disagree about it.
- **A challenge marker is only consulted for a state already counted as
  blocked**, and a marker that matches every page of the site is not a
  marker at all. This has bitten twice in this family: the Scraping Browser
  API's auto-solve extension injects `cf-turnstile` into every page it
  loads (so extension scripts are stripped before markers are looked for,
  and `cf-turnstile` is deliberately not in the list here), and the bare
  string `akamai` was in a sibling repo's list while its site — which is
  fronted by Akamai — names `akamaihd.net` in its own performance script on
  every page it serves. A live run there reported exit 3 on a 191 KB page
  that site had plainly served.
- **A sku already written earlier in the same run is dropped, not
  duplicated.** See `dedupe_by_key` in `output_writer.py`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
craigslist.org, say in the PR what you ran, which URL and page kind, from
which exit, and what you got — including the price and image coverage
percentages the run prints, and the walk trace from the sidecar. Row counts
differ by area, by category and by how far the walk got, and a busy area
turns over in hours, so a bare "worked for me" is not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

## Scope

This repo scrapes **public pages** on Craigslist: result lists and single
adverts, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
