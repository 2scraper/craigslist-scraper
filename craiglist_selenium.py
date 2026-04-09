#!/usr/bin/env python3
"""
Craigslist Scraper — Selenium Edition
GitHub : https://github.com/2scraper/craigslist-scraper
CAPTCHA: https://2captcha.com
Proxies: https://2prx.com
License: MIT

Install:
    pip install selenium webdriver-manager httpx
    (ChromeDriver is installed automatically via webdriver-manager)

Quick test:
    python craigslist_scraper_selenium.py \
        --categories for_sale --max-pages 1 --max-details 5 --debug
"""

import time
import json
import csv
import random
import re
import argparse
import httpx
from pathlib import Path
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

try:
    from webdriver_manager.chrome import ChromeDriverManager
    AUTO_INSTALL = True
except ImportError:
    AUTO_INSTALL = False


# ─── 2Captcha ────────────────────────────────────────────────────────────────

class TwoCaptcha:
    BASE = "https://2captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _poll(self, task_id: str) -> str:
        for _ in range(30):
            time.sleep(5)
            r = httpx.get(f"{self.BASE}/res.php", params={
                "key": self.api_key, "action": "get",
                "id": task_id, "json": 1,
            })
            d = r.json()
            if d["status"] == 1:
                return d["request"]
            if d["request"] != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2captcha error: {d['request']}")
        raise TimeoutError("2captcha: no response in 150 s")

    def solve_recaptcha_v2(self, site_key: str, page_url: str) -> str:
        r = httpx.post(f"{self.BASE}/in.php", data={
            "key": self.api_key, "method": "userrecaptcha",
            "googlekey": site_key, "pageurl": page_url, "json": 1,
        })
        d = r.json()
        if d["status"] != 1:
            raise RuntimeError(f"2captcha submit: {d['request']}")
        return self._poll(d["request"])

    def solve_recaptcha_v3(self, site_key: str, page_url: str,
                           action: str = "verify", min_score: float = 0.7) -> str:
        r = httpx.post(f"{self.BASE}/in.php", data={
            "key": self.api_key, "method": "userrecaptcha",
            "googlekey": site_key, "pageurl": page_url,
            "version": "v3", "action": action,
            "min_score": min_score, "json": 1,
        })
        d = r.json()
        if d["status"] != 1:
            raise RuntimeError(f"2captcha submit: {d['request']}")
        return self._poll(d["request"])

    def solve_hcaptcha(self, site_key: str, page_url: str) -> str:
        r = httpx.post(f"{self.BASE}/in.php", data={
            "key": self.api_key, "method": "hcaptcha",
            "sitekey": site_key, "pageurl": page_url, "json": 1,
        })
        d = r.json()
        if d["status"] != 1:
            raise RuntimeError(f"2captcha submit: {d['request']}")
        return self._poll(d["request"])


# ─── Fingerprint / Stealth ────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
Object.defineProperty(navigator, 'plugins',   { get: () => Array.from({ length: 5 }) });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""

# ─── JS snippets (same logic as Playwright version) ──────────────────────────

# Extracts all valid listing hrefs from the current page
EXTRACT_HREFS_JS = """
var base = location.origin;
var seen = {};
var result = [];
var pat = /\\/d\\/[^/?#]+\\/\\d{10,}\\.html/;
var links = document.querySelectorAll('a[href]');
for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('href') || '';
    if (href.indexOf('/') === 0) href = base + href;
    if (pat.test(href) && !seen[href]) {
        seen[href] = true;
        result.push(href);
    }
}
return result;
"""

# Extracts all detail fields from a listing page
EXTRACT_DETAIL_JS = """
function txt(sel) {
    var el = document.querySelector(sel);
    return el ? el.textContent.trim() : '';
}
function atr(sel, a) {
    var el = document.querySelector(sel);
    return el ? (el.getAttribute(a) || '').trim() : '';
}
var title =
    txt('#titletextonly') ||
    txt('h1.postingtitle span.titletextonly') ||
    txt('span.postingtitletext > span') ||
    txt('h1');

var price =
    txt('.price') ||
    txt('span.price') ||
    txt('[class*="price"]');

var location =
    txt('.mapaddress') ||
    txt('[class*="mapaddress"]');

var posted =
    atr('time.timeago', 'datetime') ||
    atr('p.postinginfo time', 'datetime') ||
    atr('time[datetime]', 'datetime');

var body =
    txt('#postingbody') ||
    txt('section#postingbody') ||
    txt('.postingbody') || '';
body = body.replace(/QR Code Link to This Post[\\s\\S]*$/, '').replace(/\\s+/g, ' ').trim();

var attributes = {};
var spans = document.querySelectorAll('.attrgroup span');
for (var i = 0; i < spans.length; i++) {
    var t = spans[i].textContent.trim();
    if (!t) continue;
    var idx = t.indexOf(':');
    if (idx > -1) {
        attributes[t.slice(0, idx).trim()] = t.slice(idx + 1).trim();
    } else {
        attributes[t] = true;
    }
}

var imgs = document.querySelectorAll('.iw img, #thumbs img');
var images = [];
for (var j = 0; j < imgs.length; j++) {
    var src = (imgs[j].getAttribute('src') || '').replace(/_\\d+x\\d+\\.jpg$/, '_600x450.jpg');
    if (src) images.push(src);
}

return {
    title: title, price: price, location: location,
    posted: posted, body: body, attributes: attributes, images: images
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


# ─── Driver factory ───────────────────────────────────────────────────────────

def build_driver(
    headless:   bool = True,
    proxy:      str  = None,
    user_agent: str  = None,
) -> webdriver.Chrome:
    ua = user_agent or random.choice(USER_AGENTS)
    opts = Options()
    opts.add_argument(f"--user-agent={ua}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-extensions")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")
    if proxy:
        opts.add_argument(f"--proxy-server={proxy}")

    if AUTO_INSTALL:
        svc = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=svc, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)

    # Stealth patches via CDP
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                           {"source": STEALTH_JS})
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
    return driver


# ─── Scraper ─────────────────────────────────────────────────────────────────

class CraigslistScraperSelenium:

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
        delay_min:     float = 2.0,
        delay_max:     float = 5.0,
        search_query:  str   = None,
        max_details:   int   = 20,
        wait_timeout:  int   = 15,       # seconds (Selenium uses seconds, not ms)
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
        self.delay_min     = delay_min
        self.delay_max     = delay_max
        self.search_query  = search_query
        self.max_details   = max_details
        self.wait_timeout  = wait_timeout
        self.debug         = debug
        self.results: list = []
        self.driver        = None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _log(self, msg: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def _dbg(self, msg: str):
        if self.debug:
            self._log(f"  DBG  {msg}")

    def _delay(self):
        t = random.uniform(self.delay_min, self.delay_max)
        self._dbg(f"delay {t:.1f}s")
        time.sleep(t)

    # ── CAPTCHA ───────────────────────────────────────────────────────────────

    def _handle_captcha(self) -> bool:
        if not self.captcha:
            return False
        try:
            frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
            if frames:
                src = frames[0].get_attribute("src") or ""
                m = re.search(r'[?&]k=([^&"]+)', src)
                if m:
                    self._log("reCAPTCHA v2 → solving via 2captcha...")
                    token = self.captcha.solve_recaptcha_v2(m.group(1), self.driver.current_url)
                    self.driver.execute_script(
                        f"document.getElementById('g-recaptcha-response').value='{token}'"
                    )
                    return True
            hc = self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='hcaptcha']")
            if hc:
                src = hc[0].get_attribute("src") or ""
                m = re.search(r'sitekey=([^&"]+)', src)
                if m:
                    self._log("hCaptcha → solving via 2captcha...")
                    token = self.captcha.solve_hcaptcha(m.group(1), self.driver.current_url)
                    self.driver.execute_script(
                        f"document.querySelector('[name=h-captcha-response]').value='{token}'"
                    )
                    return True
        except Exception as e:
            self._log(f"CAPTCHA handler error: {e}")
        return False

    # ─────────────────────────────────────────────────────────────────────────
    #  _wait_for_listings()
    #
    #  Craigslist renders cards via React/JS after the initial page load.
    #  We wait until the network goes quiet (document.readyState == 'complete'
    #  + a short extra pause) so React has time to mount the listing cards.
    #  Selenium has no networkidle equivalent, so we poll readyState + jQuery.
    # ─────────────────────────────────────────────────────────────────────────

    def _wait_for_listings(self) -> bool:
        try:
            # 1. Wait for document.readyState == 'complete'
            WebDriverWait(self.driver, self.wait_timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            # 2. Wait for jQuery ajax (if present) to finish
            WebDriverWait(self.driver, 5).until(
                lambda d: d.execute_script(
                    "return (typeof jQuery === 'undefined') || (jQuery.active === 0)"
                )
            )
            # 3. Small fixed pause for React rendering
            time.sleep(2)
            self._dbg("page ready")
            return True
        except TimeoutException:
            self._dbg("readyState wait timed out — proceeding anyway")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  _collect_hrefs()  — same JS logic as Playwright version
    # ─────────────────────────────────────────────────────────────────────────

    def _collect_hrefs(self) -> list:
        hrefs = self.driver.execute_script(EXTRACT_HREFS_JS) or []
        self._dbg(f"execute_script returned {len(hrefs)} listing URLs")
        if self.debug and hrefs:
            for h in hrefs[:3]:
                self._dbg(f"  sample: {h}")
        return hrefs

    # ── Detail page ───────────────────────────────────────────────────────────

    def _scrape_detail(self, url: str) -> dict:
        self.driver.get(url)
        self._wait_for_listings()
        self._delay()
        self._handle_captcha()

        data = self.driver.execute_script(EXTRACT_DETAIL_JS) or {}
        data["url"] = url
        return data

    # ── Category loop ─────────────────────────────────────────────────────────

    def _scrape_category(self, category: str, code: str) -> list:
        cat_results = []
        offset = 0

        for page_num in range(1, self.max_pages + 1):
            qs = f"query={self.search_query}&s={offset}" if self.search_query else f"s={offset}"
            url = f"{self.base_url}/search/{code}?{qs}"
            self._log(f"[{category}] page {page_num}/{self.max_pages} → {url}")

            try:
                self.driver.get(url)
                self._handle_captcha()

                # Wait for React to render listings
                self._wait_for_listings()

                # Dismiss cookie / consent banners
                for label in ("Accept", "OK", "Got it", "I agree"):
                    try:
                        btn = WebDriverWait(self.driver, 2).until(
                            EC.element_to_be_clickable(
                                (By.XPATH, f"//button[contains(text(),'{label}')]")
                            )
                        )
                        btn.click()
                        self._dbg(f"dismissed banner '{label}'")
                        time.sleep(0.4)
                        break
                    except TimeoutException:
                        pass

                # Extract hrefs via JS
                hrefs = self._collect_hrefs()
                self._log(f"[{category}] found {len(hrefs)} listing URLs on page {page_num}")

                if not hrefs:
                    title = self.driver.title
                    self._log(f"[{category}] page title: '{title}'")
                    if self.debug:
                        snippet = self.driver.execute_script(
                            "return document.body.innerHTML.slice(0, 400)"
                        )
                        self._dbg(f"body snippet:\n{snippet}\n")
                    self._log(f"[{category}] no listings — stopping category.")
                    break

                # Scrape detail pages
                batch = hrefs[:self.max_details]
                self._log(f"[{category}] scraping {len(batch)} detail pages...")

                for i, href in enumerate(batch, 1):
                    self._log(f"[{category}] {i:>3}/{len(batch)}  {href}")
                    try:
                        detail = self._scrape_detail(href)
                        detail["category"]   = category
                        detail["city"]       = self.base_url
                        detail["scraped_at"] = datetime.utcnow().isoformat()
                        cat_results.append(detail)
                        self.results.append(detail)
                        self._log(f"           ✔  \"{str(detail.get('title',''))[:55]}\" "
                                  f" {detail.get('price','')}")
                    except WebDriverException as e:
                        self._log(f"           ✗  {href}: {e.msg}")
                    except Exception as e:
                        self._log(f"           ✗  {href}: {e}")

                offset += 120

            except WebDriverException as e:
                self._log(f"[{category}] driver error: {e.msg} — stopping.")
                break
            except Exception as e:
                self._log(f"[{category}] error: {e} — stopping.")
                break

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

    def run(self):
        self.driver = build_driver(
            headless=self.headless,
            proxy=self.proxy,
        )
        sep = "=" * 62
        self._log(sep)
        self._log("  Craigslist Scraper — Selenium")
        self._log(f"  City        : {self.base_url}")
        self._log(f"  Categories  : {self.categories}")
        self._log(f"  Max pages   : {self.max_pages}")
        self._log(f"  Max details : {self.max_details} per page")
        self._log(f"  Output      : {self.output_format.upper()} → {self.output_dir}/")
        self._log(f"  2captcha    : {'enabled' if self.captcha else 'disabled'}")
        self._log(sep)

        try:
            for cat in self.categories:
                code = CATEGORIES.get(cat)
                if not code:
                    self._log(f"Unknown category '{cat}' — skipping.")
                    self._log(f"Available: {', '.join(sorted(CATEGORIES))}")
                    continue
                cat_results = self._scrape_category(cat, code)
                self._log(f"[{cat}] DONE — {len(cat_results)} listings collected.")
                self._save(cat_results, cat)
        finally:
            self.driver.quit()

        self._log("=" * 62)
        self._log(f"ALL DONE. Total listings: {len(self.results)}")
        self._save(self.results, "all_categories")
        self._log("=" * 62)
        return self.results


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    scraper = argparse.Argumentscraper(
        description="Craigslist scraper — Selenium | github.com/2captcha/craigslist-scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python craigslist_scraper_selenium.py --categories for_sale\n"
            "  python craigslist_scraper_selenium.py --categories cars_trucks "
            "--city losangeles --format csv\n"
            "  python craigslist_scraper_selenium.py --categories jobs "
            "--query 'python developer' --format both\n"
            "  python craigslist_scraper_selenium.py --categories all\n"
            f"\nAll categories:\n  {', '.join(sorted(CATEGORIES))}"
        ),
    )
    scraper.add_argument("--city",           default="newyork")
    scraper.add_argument("--categories",     nargs="+", default=["for_sale"], metavar="CAT")
    scraper.add_argument("--query",          default=None)
    scraper.add_argument("--max-pages",      type=int,   default=3)
    scraper.add_argument("--max-details",    type=int,   default=20)
    scraper.add_argument("--format",         default="json", choices=["json", "csv", "both"])
    scraper.add_argument("--output-dir",     default="output")
    scraper.add_argument("--proxy",          default=None, help="http://user:pass@host:port")
    scraper.add_argument("--captcha-key",    default=None)
    scraper.add_argument("--wait-timeout",   type=int,   default=15,
                        help="Seconds to wait for page render (default: 15)")
    scraper.add_argument("--no-headless",    action="store_true")
    scraper.add_argument("--delay-min",      type=float, default=2.0)
    scraper.add_argument("--delay-max",      type=float, default=5.0)
    scraper.add_argument("--debug",          action="store_true")
    args = scraper.parse_args()

    categories = (list(CATEGORIES.keys())
                  if "all" in args.categories
                  else args.categories)

    scraper = CraigslistScraperSelenium(
        city          = args.city,
        categories    = categories,
        max_pages     = args.max_pages,
        max_details   = args.max_details,
        output_format = args.format,
        output_dir    = args.output_dir,
        proxy         = args.proxy,
        captcha_key   = args.captcha_key,
        headless      = not args.no_headless,
        delay_min     = args.delay_min,
        delay_max     = args.delay_max,
        search_query  = args.query,
        wait_timeout  = args.wait_timeout,
        debug         = args.debug,
    )
    scraper.run()


if __name__ == "__main__":
    main()
