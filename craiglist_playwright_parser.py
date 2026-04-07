#!/usr/bin/env python3
"""
Craigslist Scraper — Playwright Edition
GitHub : https://github.com/2captcha/craigslist-scraper
CAPTCHA: https://2captcha.com
License: MIT

Install:
    pip install playwright httpx
    playwright install chromium

Quick test:
    python craigslist_scraper_playwright.py \
        --categories for_sale --max-pages 1 --max-details 5 --debug
"""

import asyncio
import json
import csv
import argparse
import random
import re
import httpx
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# ─── 2Captcha ────────────────────────────────────────────────────────────────

class TwoCaptcha:
    BASE = "https://2captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _poll(self, task_id: str) -> str:
        for _ in range(30):
            await asyncio.sleep(5)
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.BASE}/res.php", params={
                    "key": self.api_key, "action": "get",
                    "id": task_id, "json": 1,
                })
            d = r.json()
            if d["status"] == 1:
                return d["request"]
            if d["request"] != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2captcha error: {d['request']}")
        raise TimeoutError("2captcha: no response in 150 s")

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> str:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self.BASE}/in.php", data={
                "key": self.api_key, "method": "userrecaptcha",
                "googlekey": site_key, "pageurl": page_url, "json": 1,
            })
        d = r.json()
        if d["status"] != 1:
            raise RuntimeError(f"2captcha submit: {d['request']}")
        return await self._poll(d["request"])

    async def solve_hcaptcha(self, site_key: str, page_url: str) -> str:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self.BASE}/in.php", data={
                "key": self.api_key, "method": "hcaptcha",
                "sitekey": site_key, "pageurl": page_url, "json": 1,
            })
        d = r.json()
        if d["status"] != 1:
            raise RuntimeError(f"2captcha submit: {d['request']}")
        return await self._poll(d["request"])


# ─── Fingerprint / Stealth ────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
VIEWPORTS  = [{"width": 1920, "height": 1080}, {"width": 1440, "height": 900},
              {"width": 1366, "height": 768},  {"width": 1280, "height": 800}]
LOCALES    = ["en-US", "en-GB", "en-CA", "en-AU"]
TIMEZONES  = ["America/New_York", "America/Chicago",
              "America/Denver",   "America/Los_Angeles"]

def random_fingerprint() -> dict:
    return {
        "user_agent":           random.choice(USER_AGENTS),
        "viewport":             random.choice(VIEWPORTS),
        "locale":               random.choice(LOCALES),
        "timezone_id":          random.choice(TIMEZONES),
        "device_scale_factor":  random.choice([1, 1.25, 1.5, 2]),
        "hardware_concurrency": random.choice([2, 4, 6, 8, 12, 16]),
        "device_memory":        random.choice([4, 8, 16]),
    }

STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
const _origPerms = navigator.permissions.query.bind(navigator.permissions);
navigator.permissions.query = (p) =>
  p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : _origPerms(p);
Object.defineProperty(navigator, 'plugins',   { get: () => Array.from({ length: 5 }) });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
(() => {
  const hc = __HC__;
  const dm = __DM__;
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => hc });
  Object.defineProperty(navigator, 'deviceMemory',        { get: () => dm });
})();
const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
  const ctx = this.getContext('2d');
  if (ctx) {
    const img = ctx.getImageData(0, 0, this.width, this.height);
    for (let i = 0; i < img.data.length; i += 4) {
      img.data[i]   ^= (Math.random() * 2 | 0);
      img.data[i+1] ^= (Math.random() * 2 | 0);
    }
    ctx.putImageData(img, 0, 0);
  }
  return _origToDataURL.apply(this, arguments);
};
"""

# ─── Categories & cities ──────────────────────────────────────────────────────

CATEGORIES = {
    "for_sale": "sss", "antiques": "ata", "appliances": "ppa",
    "arts_crafts": "ara", "atvs": "pta", "auto_parts": "pts",
    "aviation": "ava", "baby_kids": "baa", "barter": "bar",
    "bikes": "bia", "boats": "boo", "books": "bka",
    "business": "bfs", "cars_trucks": "cta", "cds_dvds": "ema",
    "cell_phones": "moa", "clothes": "cla", "collectibles": "cba",
    "computers": "sya", "electronics": "ela", "farm_garden": "gra",
    "free_stuff": "zip", "furniture": "fua", "garage_sales": "gms",
    "general_for_sale": "foa", "health_beauty": "haa",
    "heavy_equipment": "hva", "household": "hsa", "jewelry": "jwa",
    "materials": "maa", "motorcycles": "mca",
    "musical_instruments": "msa", "photo_video": "pha",
    "rvs_campers": "rva", "sporting_goods": "sga", "tickets": "tia",
    "tools": "tla", "toys_games": "taa", "trailers": "tra",
    "video_games": "vga", "wanted": "waa",
    "housing": "hhh", "apartments": "apa", "rooms_shared": "roo",
    "sublets": "sub", "housing_swap": "hsw", "housing_wanted": "hou",
    "office_commercial": "off", "parking_storage": "prk",
    "real_estate": "rea", "vacation_rentals": "vac",
    "jobs": "jjj", "accounting": "acc", "admin": "adm",
    "arch_engineering": "egr", "art_media": "med",
    "biotech_science": "sci", "business_mgmt": "bus",
    "customer_service": "csr", "education": "edu",
    "food_beverage": "fbh", "general_labor": "ofd",
    "government": "gov", "healthcare": "hea", "hr": "hum",
    "it": "sof", "legal": "lgl", "manufacturing": "mnu",
    "marketing": "mar", "nonprofit": "npo", "retail": "ret",
    "sales": "sls", "security": "sec", "skilled_trades": "trd",
    "software": "web", "transportation": "trp", "writing": "wri",
    "services": "bbb", "automotive_svcs": "aos", "beauty_svcs": "bts",
    "computer_svcs": "cps", "creative_svcs": "crs",
    "event_svcs": "evs", "farm_svcs": "fgs", "financial_svcs": "fns",
    "health_svcs": "hls", "household_svcs": "hss", "labor_svcs": "lbs",
    "legal_svcs": "lss", "lessons_svcs": "les", "moving_svcs": "mov",
    "pet_svcs": "pets", "real_estate_svcs": "res",
    "skilled_trades_svcs": "tls", "travel_svcs": "trv",
    "therapeutic_svcs": "thp", "writing_svcs": "wrs",
    "community": "ccc", "activities": "act", "artists": "ats",
    "childcare": "kid", "classes": "cls", "events": "eve",
    "groups": "grp", "local_news": "nws", "lost_found": "laf",
    "missed_connections": "mis", "musicians": "muc", "pets": "pet",
    "politics": "pol", "rideshare": "rid", "volunteers": "vol",
    "gigs": "ggg", "computer_gigs": "cpg", "creative_gigs": "crg",
    "crew_gigs": "cwg", "domestic_gigs": "dmg", "event_gigs": "evg",
    "labor_gigs": "lbg", "talent_gigs": "tlg", "writing_gigs": "wrg",
    "resumes": "rrr",
}

CL_CITIES = {
    "newyork":      "https://newyork.craigslist.org",
    "losangeles":   "https://losangeles.craigslist.org",
    "chicago":      "https://chicago.craigslist.org",
    "sfbay":        "https://sfbay.craigslist.org",
    "seattle":      "https://seattle.craigslist.org",
    "boston":       "https://boston.craigslist.org",
    "miami":        "https://miami.craigslist.org",
    "dallas":       "https://dallas.craigslist.org",
    "houston":      "https://houston.craigslist.org",
    "phoenix":      "https://phoenix.craigslist.org",
    "denver":       "https://denver.craigslist.org",
    "atlanta":      "https://atlanta.craigslist.org",
    "portland":     "https://portland.craigslist.org",
    "sandiego":     "https://sandiego.craigslist.org",
    "washingtondc": "https://washingtondc.craigslist.org",
}

# Regex: valid CL listing URL  /d/<slug>/<10+ digit id>.html
LISTING_RE = re.compile(r"/d/[^/?#]+/\d{10,}\.html")

# ─────────────────────────────────────────────────────────────────────────────
#  ROOT CAUSE (v1–v3):
#
#  Craigslist search results are rendered by React/JS *after* the initial HTML
#  response.  `wait_until="domcontentloaded"` fires before React has mounted
#  the listing cards — so every CSS selector returns 0 elements.
#
#  FIX: after goto(), explicitly wait for the first listing link to appear in
#  the DOM (with a generous timeout).  Only then extract hrefs.
#
#  We also extract links via page.evaluate() — pure JS running inside the
#  browser — which is immune to Playwright selector-engine quirks.
# ─────────────────────────────────────────────────────────────────────────────

# CSS selectors used only as the *wait target* (one of these must appear)
WAIT_SELECTORS = [
    "li.cl-search-result",          # new React layout
    "li.cl-static-search-result",   # transitional layout
    ".result-row",                   # classic layout
]

# JS snippet run inside the browser to harvest listing hrefs
# Returns a plain list of absolute URL strings.
EXTRACT_HREFS_JS = r"""
() => {
  const base   = location.origin;
  const seen   = new Set();
  const result = [];
  const pat    = /\/d\/[^/?#]+\/\d{10,}\.html/;

  document.querySelectorAll('a[href]').forEach(a => {
    let href = a.getAttribute('href') || '';
    // make absolute
    if (href.startsWith('/')) href = base + href;
    // must match listing pattern and not be a duplicate
    if (pat.test(href) && !seen.has(href)) {
      seen.add(href);
      result.push(href);
    }
  });
  return result;
}
"""

# JS snippet to scrape a detail page — runs entirely in-browser
EXTRACT_DETAIL_JS = r"""
() => {
  const txt = sel => (document.querySelector(sel)?.textContent || '').trim();
  const atr = (sel, a) => (document.querySelector(sel)?.getAttribute(a) || '').trim();

  // title
  const title =
    txt('#titletextonly') ||
    txt('h1.postingtitle span.titletextonly') ||
    txt('span.postingtitletext > span') ||
    txt('h1');

  // price
  const price =
    txt('.price') ||
    txt('span.price') ||
    txt('[class*="price"]');

  // location
  const location =
    txt('.mapaddress') ||
    txt('[class*="mapaddress"]');

  // posted datetime
  const posted =
    atr('time.timeago', 'datetime') ||
    atr('p.postinginfo time', 'datetime') ||
    atr('time[datetime]', 'datetime');

  // body — strip the injected QR footer
  let body =
    txt('#postingbody') ||
    txt('section#postingbody') ||
    txt('.postingbody') || '';
  body = body.replace(/QR Code Link to This Post[\s\S]*$/, '').replace(/\s+/g, ' ').trim();

  // attributes
  const attributes = {};
  document.querySelectorAll('.attrgroup span').forEach(s => {
    const t = s.textContent.trim();
    if (!t) return;
    const idx = t.indexOf(':');
    if (idx > -1) {
      attributes[t.slice(0, idx).trim()] = t.slice(idx + 1).trim();
    } else {
      attributes[t] = true;
    }
  });

  // images — upgrade thumbnail → full-res
  const images = Array.from(
    document.querySelectorAll('.iw img, #thumbs img')
  ).map(i => (i.getAttribute('src') || '').replace(/_\d+x\d+\.jpg$/, '_600x450.jpg'))
   .filter(Boolean);

  return { title, price, location, posted, body, attributes, images };
}
"""


# ─── Scraper ─────────────────────────────────────────────────────────────────

class CraigslistScraper:

    def __init__(
        self,
        city:          str   = "newyork",
        categories:    list  = None,
        max_pages:     int   = 3,
        output_format: str   = "json",   # "json" | "csv" | "both"
        output_dir:    str   = "output",
        proxy:         str   = None,
        captcha_key:   str   = None,
        headless:      bool  = True,
        stealth:       bool  = True,
        random_fp:     bool  = True,
        delay_min:     float = 2.0,
        delay_max:     float = 5.0,
        search_query:  str   = None,
        max_details:   int   = 20,
        wait_timeout:  int   = 15_000,   # ms to wait for listings to render
        debug:         bool  = False,
    ):
        self.base_url      = CL_CITIES.get(city, f"https://{city}.craigslist.org")
        self.categories    = categories or ["for_sale"]
        self.max_pages     = max_pages
        self.output_format = output_format
        self.output_dir    = Path(output_dir)
        self.proxy         = proxy
        self.captcha       = TwoCaptcha(captcha_key) if captcha_key else None
        self.headless      = headless
        self.stealth       = stealth
        self.random_fp     = random_fp
        self.delay_min     = delay_min
        self.delay_max     = delay_max
        self.search_query  = search_query
        self.max_details   = max_details
        self.wait_timeout  = wait_timeout
        self.debug         = debug
        self.results: list = []
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _log(self, msg: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def _dbg(self, msg: str):
        if self.debug:
            self._log(f"  DBG  {msg}")

    # ── Browser ───────────────────────────────────────────────────────────────

    async def _build_context(self, pw):
        fp = random_fingerprint() if self.random_fp else {
            "user_agent": USER_AGENTS[0], "viewport": VIEWPORTS[0],
            "locale": "en-US", "timezone_id": "America/New_York",
            "device_scale_factor": 1, "hardware_concurrency": 4, "device_memory": 8,
        }
        args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            f"--window-size={fp['viewport']['width']},{fp['viewport']['height']}",
        ]
        launch_opts: dict = {"headless": self.headless, "args": args}
        if self.proxy:
            launch_opts["proxy"] = {"server": self.proxy}

        browser = await pw.chromium.launch(**launch_opts)
        ctx = await browser.new_context(
            user_agent=fp["user_agent"],
            viewport=fp["viewport"],
            locale=fp["locale"],
            timezone_id=fp["timezone_id"],
            device_scale_factor=fp["device_scale_factor"],
        )
        if self.stealth:
            js = (STEALTH_JS
                  .replace("__HC__", str(fp["hardware_concurrency"]))
                  .replace("__DM__", str(fp["device_memory"])))
            await ctx.add_init_script(js)

        self._log(f"Browser UA : {fp['user_agent'][:75]}")
        self._log(f"Viewport   : {fp['viewport']['width']}x{fp['viewport']['height']} "
                  f"| TZ: {fp['timezone_id']} | Locale: {fp['locale']}")
        return browser, ctx

    async def _delay(self):
        t = random.uniform(self.delay_min, self.delay_max)
        self._dbg(f"delay {t:.1f}s")
        await asyncio.sleep(t)

    # ── CAPTCHA ───────────────────────────────────────────────────────────────

    async def _handle_captcha(self, page) -> bool:
        if not self.captcha:
            return False
        try:
            rc = page.locator("iframe[src*='recaptcha']")
            if await rc.count() > 0:
                src = (await rc.first.get_attribute("src")) or ""
                m = re.search(r'[?&]k=([^&"]+)', src)
                if m:
                    self._log("reCAPTCHA v2 → solving via 2captcha...")
                    token = await self.captcha.solve_recaptcha_v2(m.group(1), page.url)
                    await page.evaluate(
                        f"document.getElementById('g-recaptcha-response').value='{token}'"
                    )
                    return True
            hc = page.locator("iframe[src*='hcaptcha']")
            if await hc.count() > 0:
                src = (await hc.first.get_attribute("src")) or ""
                m = re.search(r'sitekey=([^&"]+)', src)
                if m:
                    self._log("hCaptcha → solving via 2captcha...")
                    token = await self.captcha.solve_hcaptcha(m.group(1), page.url)
                    await page.evaluate(
                        f"document.querySelector('[name=h-captcha-response]').value='{token}'"
                    )
                    return True
        except Exception as e:
            self._log(f"CAPTCHA handler error: {e}")
        return False

    # ─────────────────────────────────────────────────────────────────────────
    #  _wait_for_listings()
    #
    #  THE KEY FIX: we must wait until React has mounted the listing cards.
    #  We try each selector in WAIT_SELECTORS and return as soon as one appears.
    #  If none appear within `wait_timeout` ms we fall through (returns False).
    # ─────────────────────────────────────────────────────────────────────────

    async def _wait_for_listings(self, page) -> bool:
        for sel in WAIT_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=self.wait_timeout)
                self._dbg(f"listings rendered — matched '{sel}'")
                return True
            except PWTimeout:
                self._dbg(f"wait_for_selector('{sel}') timed out")
                continue
        return False

    # ─────────────────────────────────────────────────────────────────────────
    #  _collect_hrefs()
    #
    #  Runs EXTRACT_HREFS_JS inside the live browser via page.evaluate().
    #  This is immune to Playwright selector-engine issues: the JS sees the
    #  exact same DOM that a real user would see.
    # ─────────────────────────────────────────────────────────────────────────

    async def _collect_hrefs(self, page) -> list[str]:
        hrefs: list[str] = await page.evaluate(EXTRACT_HREFS_JS)
        self._dbg(f"evaluate() returned {len(hrefs)} listing URLs")
        if self.debug and hrefs:
            for h in hrefs[:3]:
                self._dbg(f"  sample: {h}")
        return hrefs

    # ── Detail page ───────────────────────────────────────────────────────────

    async def _scrape_detail(self, page, url: str) -> dict:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Wait for the posting body to render
        for sel in ("#postingbody", "section#postingbody", "h1.postingtitle", "h1"):
            try:
                await page.wait_for_selector(sel, timeout=8_000)
                break
            except PWTimeout:
                continue

        await self._delay()
        await self._handle_captcha(page)

        # Extract everything in one JS call — fast and reliable
        data: dict = await page.evaluate(EXTRACT_DETAIL_JS)
        data["url"] = url
        return data

    # ── Category loop ─────────────────────────────────────────────────────────

    async def _scrape_category(self, ctx, category: str, code: str) -> list:
        page = await ctx.new_page()
        cat_results: list = []
        offset = 0

        for page_num in range(1, self.max_pages + 1):
            qs = f"query={self.search_query}&s={offset}" if self.search_query else f"s={offset}"
            url = f"{self.base_url}/search/{code}?{qs}"
            self._log(f"[{category}] page {page_num}/{self.max_pages} → {url}")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await self._handle_captcha(page)

                # ── FIX: wait for React to render listing cards ────────────
                rendered = await self._wait_for_listings(page)
                if not rendered:
                    self._log(f"[{category}] WARNING: listings did not render in "
                              f"{self.wait_timeout}ms — trying anyway")

                await self._delay()

                # Dismiss cookie / consent banners
                for label in ("Accept", "OK", "Got it", "I agree"):
                    btn = page.locator(f"button:has-text('{label}')")
                    if await btn.count() > 0:
                        await btn.first.click()
                        self._dbg(f"dismissed banner '{label}'")
                        await asyncio.sleep(0.4)
                        break

                # ── FIX: extract hrefs via in-browser JS ───────────────────
                hrefs = await self._collect_hrefs(page)
                self._log(f"[{category}] found {len(hrefs)} listing URLs on page {page_num}")

                if not hrefs:
                    # Debug dump: show first 400 chars of body HTML
                    if self.debug:
                        snippet = await page.evaluate(
                            "() => document.body.innerHTML.slice(0, 400)"
                        )
                        self._dbg(f"body HTML snippet:\n{snippet}\n")
                    self._log(f"[{category}] no listings — stopping category.")
                    break

                # Scrape detail pages
                detail_page = await ctx.new_page()
                batch = hrefs[:self.max_details]
                self._log(f"[{category}] scraping {len(batch)} detail pages...")

                for i, href in enumerate(batch, 1):
                    self._log(f"[{category}] {i:>3}/{len(batch)}  {href}")
                    try:
                        detail = await self._scrape_detail(detail_page, href)
                        detail["category"]   = category
                        detail["city"]       = self.base_url
                        detail["scraped_at"] = datetime.utcnow().isoformat()
                        cat_results.append(detail)
                        self.results.append(detail)
                        self._log(f"           ✔  \"{detail.get('title','')[:55]}\" "
                                  f" {detail.get('price','')}")
                    except PWTimeout:
                        self._log(f"           ✗  timeout: {href}")
                    except Exception as exc:
                        self._log(f"           ✗  {href}: {exc}")

                await detail_page.close()
                offset += 120

            except PWTimeout:
                self._log(f"[{category}] page load timeout — stopping.")
                break
            except Exception as exc:
                self._log(f"[{category}] error: {exc}")
                break

        await page.close()
        return cat_results

    # ── Save helpers ──────────────────────────────────────────────────────────

    def _save_json(self, data: list, path: Path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._log(f"Saved JSON → {path}  ({len(data)} records)")

    def _save_csv(self, data: list, path: Path):
        if not data:
            return
        flat = []
        for r in data:
            row = {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
            row["images"] = " | ".join(r.get("images", []))
            for k, v in r.get("attributes", {}).items():
                row[f"attr_{k}"] = v
            flat.append(row)
        keys = list(dict.fromkeys(k for r in flat for k in r))
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(flat)
        self._log(f"Saved CSV  → {path}  ({len(data)} records)")

    def _save(self, data: list, name: str):
        if not data:
            self._log(f"Nothing to save for '{name}'")
            return
        ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        base = self.output_dir / f"{name}_{ts}"
        if self.output_format in ("json", "both"):
            self._save_json(data, base.with_suffix(".json"))
        if self.output_format in ("csv", "both"):
            self._save_csv(data, base.with_suffix(".csv"))

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self):
        async with async_playwright() as pw:
            browser, ctx = await self._build_context(pw)
            sep = "=" * 62
            self._log(sep)
            self._log("  Craigslist Scraper — Playwright")
            self._log(f"  City        : {self.base_url}")
            self._log(f"  Categories  : {self.categories}")
            self._log(f"  Max pages   : {self.max_pages}")
            self._log(f"  Max details : {self.max_details} per page")
            self._log(f"  Output      : {self.output_format.upper()} → {self.output_dir}/")
            self._log(f"  Stealth     : {self.stealth}")
            self._log(f"  2captcha    : {'enabled' if self.captcha else 'disabled'}")
            self._log(sep)

            for cat in self.categories:
                code = CATEGORIES.get(cat)
                if not code:
                    self._log(f"Unknown category '{cat}' — skipping.")
                    self._log(f"Available: {', '.join(sorted(CATEGORIES))}")
                    continue
                cat_results = await self._scrape_category(ctx, cat, code)
                self._log(f"[{cat}] DONE — {len(cat_results)} listings collected.")
                self._save(cat_results, cat)

            await browser.close()

        self._log("=" * 62)
        self._log(f"ALL DONE. Total listings: {len(self.results)}")
        self._save(self.results, "all_categories")
        self._log("=" * 62)
        return self.results


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Craigslist scraper — Playwright | github.com/2captcha/craigslist-scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python craigslist_scraper_playwright.py --categories for_sale\n"
            "  python craigslist_scraper_playwright.py --categories cars_trucks "
            "--city losangeles --format csv\n"
            "  python craigslist_scraper_playwright.py --categories jobs "
            "--query 'python developer' --format both\n"
            "  python craigslist_scraper_playwright.py --categories all\n"
            f"\nAll categories:\n  {', '.join(sorted(CATEGORIES))}"
        ),
    )
    parser.add_argument("--city",           default="newyork")
    parser.add_argument("--categories",     nargs="+", default=["for_sale"], metavar="CAT",
                        help="Category names, or 'all'. Default: for_sale")
    parser.add_argument("--query",          default=None)
    parser.add_argument("--max-pages",      type=int,   default=3)
    parser.add_argument("--max-details",    type=int,   default=20)
    parser.add_argument("--format",         default="json", choices=["json", "csv", "both"])
    parser.add_argument("--output-dir",     default="output")
    parser.add_argument("--proxy",          default=None, help="http://user:pass@host:port")
    parser.add_argument("--captcha-key",    default=None, help="2captcha.com API key")
    parser.add_argument("--wait-timeout",   type=int,   default=15000,
                        help="Ms to wait for listings to render (default: 15000)")
    parser.add_argument("--no-headless",    action="store_true")
    parser.add_argument("--no-stealth",     action="store_true")
    parser.add_argument("--no-fingerprint", action="store_true")
    parser.add_argument("--delay-min",      type=float, default=2.0)
    parser.add_argument("--delay-max",      type=float, default=5.0)
    parser.add_argument("--debug",          action="store_true")
    args = parser.parse_args()

    categories = (list(CATEGORIES.keys())
                  if "all" in args.categories
                  else args.categories)

    scraper = CraigslistScraper(
        city          = args.city,
        categories    = categories,
        max_pages     = args.max_pages,
        max_details   = args.max_details,
        output_format = args.format,
        output_dir    = args.output_dir,
        proxy         = args.proxy,
        captcha_key   = args.captcha_key,
        headless      = not args.no_headless,
        stealth       = not args.no_stealth,
        random_fp     = not args.no_fingerprint,
        delay_min     = args.delay_min,
        delay_max     = args.delay_max,
        search_query  = args.query,
        wait_timeout  = args.wait_timeout,
        debug         = args.debug,
    )
    asyncio.run(scraper.run())


if __name__ == "__main__":
    main()
