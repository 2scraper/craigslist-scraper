#!/usr/bin/env node
/**
 * Craigslist Scraper — Puppeteer Edition
 * GitHub : https://github.com/2scraper/craigslist-scraper
 * CAPTCHA: https://2captcha.com
 * Proxies: https://2prx.com
 * License: MIT
 *
 * Install:
 *   npm install puppeteer-extra puppeteer-extra-plugin-stealth axios csv-writer minimist
 *
 * Quick test:
 *   node craigslist_scraper_puppeteer.js \
 *     --categories for_sale --max-pages 1 --max-details 5 --debug
 */

"use strict";

const puppeteer     = require("puppeteer-extra");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");
const axios         = require("axios");
const { createObjectCsvWriter } = require("csv-writer");
const fs            = require("fs");
const path          = require("path");
const minimist      = require("minimist");

puppeteer.use(StealthPlugin());

// ─── Helpers ──────────────────────────────────────────────────────────────────

const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand  = (min, max) => Math.random() * (max - min) + min;
const ts    = () => new Date().toISOString().slice(11, 19);
const log   = msg => console.log(`[${ts()}] ${msg}`);

// ─── 2Captcha ─────────────────────────────────────────────────────────────────

class TwoCaptcha {
  constructor(apiKey) {
    this.apiKey  = apiKey;
    this.baseUrl = "https://2captcha.com";
  }

  async _poll(taskId) {
    for (let i = 0; i < 30; i++) {
      await sleep(5000);
      const { data } = await axios.get(`${this.baseUrl}/res.php`, {
        params: { key: this.apiKey, action: "get", id: taskId, json: 1 },
      });
      if (data.status === 1) return data.request;
      if (data.request !== "CAPCHA_NOT_READY")
        throw new Error(`2captcha error: ${data.request}`);
    }
    throw new Error("2captcha timed out");
  }

  async solveRecaptchaV2(siteKey, pageUrl) {
    const { data } = await axios.post(`${this.baseUrl}/in.php`, null, {
      params: {
        key: this.apiKey, method: "userrecaptcha",
        googlekey: siteKey, pageurl: pageUrl, json: 1,
      },
    });
    if (data.status !== 1) throw new Error(`2captcha submit: ${data.request}`);
    return this._poll(data.request);
  }

  async solveRecaptchaV3(siteKey, pageUrl, action = "verify", minScore = 0.7) {
    const { data } = await axios.post(`${this.baseUrl}/in.php`, null, {
      params: {
        key: this.apiKey, method: "userrecaptcha",
        googlekey: siteKey, pageurl: pageUrl,
        version: "v3", action, min_score: minScore, json: 1,
      },
    });
    if (data.status !== 1) throw new Error(`2captcha submit: ${data.request}`);
    return this._poll(data.request);
  }

  async solveHCaptcha(siteKey, pageUrl) {
    const { data } = await axios.post(`${this.baseUrl}/in.php`, null, {
      params: {
        key: this.apiKey, method: "hcaptcha",
        sitekey: siteKey, pageurl: pageUrl, json: 1,
      },
    });
    if (data.status !== 1) throw new Error(`2captcha submit: ${data.request}`);
    return this._poll(data.request);
  }
}

// ─── Fingerprint ─────────────────────────────────────────────────────────────

const USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
];
const VIEWPORTS = [
  { width: 1920, height: 1080 }, { width: 1440, height: 900  },
  { width: 1366, height: 768  }, { width: 1280, height: 800  },
];
const pick = arr => arr[Math.floor(Math.random() * arr.length)];

function randomFp() {
  return {
    userAgent:          pick(USER_AGENTS),
    viewport:           pick(VIEWPORTS),
    hardwareConcurrency: pick([2, 4, 6, 8, 12, 16]),
    deviceMemory:        pick([4, 8, 16]),
  };
}

// ─── JS snippets (identical logic to Playwright / Selenium versions) ──────────

// Extract all valid listing hrefs from the current page
const EXTRACT_HREFS_JS = `
  (() => {
    const base = location.origin;
    const seen = new Set();
    const res  = [];
    const pat  = /\\/d\\/[^/?#]+\\/\\d{10,}\\.html/;
    document.querySelectorAll('a[href]').forEach(a => {
      let href = a.getAttribute('href') || '';
      if (href.startsWith('/')) href = base + href;
      if (pat.test(href) && !seen.has(href)) { seen.add(href); res.push(href); }
    });
    return res;
  })()
`;

// Extract all detail fields from a listing page
const EXTRACT_DETAIL_JS = `
  (() => {
    const txt = sel => (document.querySelector(sel)?.textContent || '').trim();
    const atr = (sel, a) => (document.querySelector(sel)?.getAttribute(a) || '').trim();

    const title =
      txt('#titletextonly') ||
      txt('h1.postingtitle span.titletextonly') ||
      txt('span.postingtitletext > span') ||
      txt('h1');

    const price =
      txt('.price') || txt('span.price') || txt('[class*="price"]');

    const location =
      txt('.mapaddress') || txt('[class*="mapaddress"]');

    const posted =
      atr('time.timeago', 'datetime') ||
      atr('p.postinginfo time', 'datetime') ||
      atr('time[datetime]', 'datetime');

    let body =
      txt('#postingbody') || txt('section#postingbody') || txt('.postingbody') || '';
    body = body.replace(/QR Code Link to This Post[\\s\\S]*$/, '').replace(/\\s+/g, ' ').trim();

    const attributes = {};
    document.querySelectorAll('.attrgroup span').forEach(s => {
      const t = s.textContent.trim();
      if (!t) return;
      const idx = t.indexOf(':');
      if (idx > -1) attributes[t.slice(0, idx).trim()] = t.slice(idx + 1).trim();
      else attributes[t] = true;
    });

    const images = [...document.querySelectorAll('.iw img, #thumbs img')]
      .map(i => (i.getAttribute('src') || '').replace(/_\\d+x\\d+\\.jpg$/, '_600x450.jpg'))
      .filter(Boolean);

    return { title, price, location, posted, body, attributes, images };
  })()
`;

// ─── Categories & cities ──────────────────────────────────────────────────────

const CATEGORIES = {
  for_sale: "sss", antiques: "ata", appliances: "ppa",
  arts_crafts: "ara", atvs: "pta", auto_parts: "pts",
  aviation: "ava", baby_kids: "baa", barter: "bar",
  bikes: "bia", boats: "boo", books: "bka",
  business: "bfs", cars_trucks: "cta", cds_dvds: "ema",
  cell_phones: "moa", clothes: "cla", collectibles: "cba",
  computers: "sya", electronics: "ela", farm_garden: "gra",
  free_stuff: "zip", furniture: "fua", garage_sales: "gms",
  general_for_sale: "foa", health_beauty: "haa",
  heavy_equipment: "hva", household: "hsa", jewelry: "jwa",
  materials: "maa", motorcycles: "mca",
  musical_instruments: "msa", photo_video: "pha",
  rvs_campers: "rva", sporting_goods: "sga", tickets: "tia",
  tools: "tla", toys_games: "taa", trailers: "tra",
  video_games: "vga", wanted: "waa",
  housing: "hhh", apartments: "apa", rooms_shared: "roo",
  sublets: "sub", housing_swap: "hsw", housing_wanted: "hou",
  office_commercial: "off", parking_storage: "prk",
  real_estate: "rea", vacation_rentals: "vac",
  jobs: "jjj", accounting: "acc", admin: "adm",
  arch_engineering: "egr", art_media: "med",
  biotech_science: "sci", business_mgmt: "bus",
  customer_service: "csr", education: "edu",
  food_beverage: "fbh", general_labor: "ofd",
  government: "gov", healthcare: "hea", hr: "hum",
  it: "sof", legal: "lgl", manufacturing: "mnu",
  marketing: "mar", nonprofit: "npo", retail: "ret",
  sales: "sls", security: "sec", skilled_trades: "trd",
  software: "web", transportation: "trp", writing: "wri",
  services: "bbb", automotive_svcs: "aos", beauty_svcs: "bts",
  computer_svcs: "cps", creative_svcs: "crs",
  event_svcs: "evs", farm_svcs: "fgs", financial_svcs: "fns",
  health_svcs: "hls", household_svcs: "hss", labor_svcs: "lbs",
  legal_svcs: "lss", lessons_svcs: "les", moving_svcs: "mov",
  pet_svcs: "pets", real_estate_svcs: "res",
  skilled_trades_svcs: "tls", travel_svcs: "trv",
  therapeutic_svcs: "thp", writing_svcs: "wrs",
  community: "ccc", activities: "act", artists: "ats",
  childcare: "kid", classes: "cls", events: "eve",
  groups: "grp", local_news: "nws", lost_found: "laf",
  missed_connections: "mis", musicians: "muc", pets: "pet",
  politics: "pol", rideshare: "rid", volunteers: "vol",
  gigs: "ggg", computer_gigs: "cpg", creative_gigs: "crg",
  crew_gigs: "cwg", domestic_gigs: "dmg", event_gigs: "evg",
  labor_gigs: "lbg", talent_gigs: "tlg", writing_gigs: "wrg",
  resumes: "rrr",
};

const CL_CITIES = {
  newyork:      "https://newyork.craigslist.org",
  losangeles:   "https://losangeles.craigslist.org",
  chicago:      "https://chicago.craigslist.org",
  sfbay:        "https://sfbay.craigslist.org",
  seattle:      "https://seattle.craigslist.org",
  boston:       "https://boston.craigslist.org",
  miami:        "https://miami.craigslist.org",
  dallas:       "https://dallas.craigslist.org",
  houston:      "https://houston.craigslist.org",
  phoenix:      "https://phoenix.craigslist.org",
  denver:       "https://denver.craigslist.org",
  atlanta:      "https://atlanta.craigslist.org",
  portland:     "https://portland.craigslist.org",
  sandiego:     "https://sandiego.craigslist.org",
  washingtondc: "https://washingtondc.craigslist.org",
};

// ─── Scraper ──────────────────────────────────────────────────────────────────

class CraigslistScraperPuppeteer {
  constructor(opts = {}) {
    this.baseUrl      = CL_CITIES[opts.city] || `https://${opts.city || "newyork"}.craigslist.org`;
    this.categories   = opts.categories  || ["for_sale"];
    this.maxPages     = opts.maxPages    || 3;
    this.outputFormat = opts.outputFormat|| "json";
    this.outputDir    = opts.outputDir   || "output";
    this.proxy        = opts.proxy       || null;
    this.captcha      = opts.captchaKey  ? new TwoCaptcha(opts.captchaKey) : null;
    this.headless     = opts.headless    !== false;
    this.delayMin     = opts.delayMin    || 2000;
    this.delayMax     = opts.delayMax    || 5000;
    this.searchQuery  = opts.searchQuery || null;
    this.maxDetails   = opts.maxDetails  || 20;
    this.waitTimeout  = opts.waitTimeout || 15000;
    this.debug        = opts.debug       || false;
    this.results      = [];
    fs.mkdirSync(this.outputDir, { recursive: true });
  }

  _dbg(msg) { if (this.debug) log(`  DBG  ${msg}`); }

  // ── Browser ────────────────────────────────────────────────────────────────

  async _buildBrowser() {
    const fp   = randomFp();
    const args = [
      "--no-sandbox", "--disable-setuid-sandbox",
      "--disable-blink-features=AutomationControlled",
      `--window-size=${fp.viewport.width},${fp.viewport.height}`,
    ];
    if (this.proxy) args.push(`--proxy-server=${this.proxy}`);

    const browser = await puppeteer.launch({
      headless: this.headless ? "new" : false,
      args, ignoreHTTPSErrors: true,
    });
    const page = await browser.newPage();
    await page.setUserAgent(fp.userAgent);
    await page.setViewport(fp.viewport);

    // Spoof navigator properties
    await page.evaluateOnNewDocument((hc, dm) => {
      Object.defineProperty(navigator, "webdriver",           { get: () => undefined });
      Object.defineProperty(navigator, "hardwareConcurrency", { get: () => hc });
      Object.defineProperty(navigator, "deviceMemory",        { get: () => dm });
      Object.defineProperty(navigator, "plugins",             { get: () => Array.from({ length: 5 }) });
      Object.defineProperty(navigator, "languages",           { get: () => ["en-US", "en"] });
      window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
    }, fp.hardwareConcurrency, fp.deviceMemory);

    log(`Browser UA : ${fp.userAgent.slice(0, 75)}`);
    log(`Viewport   : ${fp.viewport.width}x${fp.viewport.height}`);
    return { browser, page };
  }

  // ── CAPTCHA ────────────────────────────────────────────────────────────────

  async _handleCaptcha(page) {
    if (!this.captcha) return false;
    try {
      const rcSrc = await page.$eval("iframe[src*='recaptcha']", el => el.src).catch(() => "");
      if (rcSrc) {
        const m = rcSrc.match(/[?&]k=([^&"]+)/);
        if (m) {
          log("reCAPTCHA v2 → solving via 2captcha...");
          const token = await this.captcha.solveRecaptchaV2(m[1], page.url());
          await page.evaluate(
            t => { document.getElementById("g-recaptcha-response").value = t; }, token
          );
          return true;
        }
      }
      const hcSrc = await page.$eval("iframe[src*='hcaptcha']", el => el.src).catch(() => "");
      if (hcSrc) {
        const m = hcSrc.match(/sitekey=([^&"]+)/);
        if (m) {
          log("hCaptcha → solving via 2captcha...");
          const token = await this.captcha.solveHCaptcha(m[1], page.url());
          await page.evaluate(
            t => { document.querySelector("[name=h-captcha-response]").value = t; }, token
          );
          return true;
        }
      }
    } catch (e) {
      log(`CAPTCHA handler error: ${e.message}`);
    }
    return false;
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  _waitForListings()
  //
  //  Craigslist renders cards via React/JS after domcontentloaded.
  //  We use page.waitForNetworkIdle() so React has time to mount cards.
  // ─────────────────────────────────────────────────────────────────────────

  async _waitForListings(page) {
    try {
      await page.waitForNetworkIdle({ idleTime: 500, timeout: this.waitTimeout });
      this._dbg("networkidle reached");
      return true;
    } catch {
      this._dbg("networkidle timed out — proceeding anyway");
      return false;
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  _collectHrefs()  — same JS logic as Playwright / Selenium versions
  // ─────────────────────────────────────────────────────────────────────────

  async _collectHrefs(page) {
    const hrefs = await page.evaluate(EXTRACT_HREFS_JS);
    this._dbg(`evaluate() returned ${hrefs.length} listing URLs`);
    if (this.debug && hrefs.length) {
      hrefs.slice(0, 3).forEach(h => this._dbg(`  sample: ${h}`));
    }
    return hrefs;
  }

  // ── Detail page ────────────────────────────────────────────────────────────

  async _scrapeDetail(page, url) {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await this._waitForListings(page);
    await sleep(rand(this.delayMin, this.delayMax));
    await this._handleCaptcha(page);

    const data = await page.evaluate(EXTRACT_DETAIL_JS);
    data.url = url;
    return data;
  }

  // ── Category loop ──────────────────────────────────────────────────────────

  async _scrapeCategory(browser, category, code) {
    const page       = await browser.newPage();
    const catResults = [];
    let offset       = 0;

    for (let p = 1; p <= this.maxPages; p++) {
      const qs  = this.searchQuery
        ? `query=${encodeURIComponent(this.searchQuery)}&s=${offset}`
        : `s=${offset}`;
      const url = `${this.baseUrl}/search/${code}?${qs}`;
      log(`[${category}] page ${p}/${this.maxPages} → ${url}`);

      try {
        await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
        await this._handleCaptcha(page);
        await this._waitForListings(page);
        await sleep(rand(this.delayMin, this.delayMax));

        // Dismiss cookie banner
        const btn = await page.$("button");
        if (btn) {
          const label = await page.evaluate(b => b.textContent, btn);
          if (/accept|ok|agree/i.test(label)) {
            await btn.click().catch(() => {});
            await sleep(400);
          }
        }

        // Extract hrefs via in-browser JS
        const hrefs = await this._collectHrefs(page);
        log(`[${category}] found ${hrefs.length} listing URLs on page ${p}`);

        if (!hrefs.length) {
          const title = await page.title();
          log(`[${category}] page title: '${title}'`);
          if (this.debug) {
            const snippet = await page.evaluate(() => document.body.innerHTML.slice(0, 400));
            this._dbg(`body snippet:\n${snippet}\n`);
          }
          log(`[${category}] no listings — stopping category.`);
          break;
        }

        // Scrape detail pages
        const detailPage = await browser.newPage();
        const batch      = hrefs.slice(0, this.maxDetails);
        log(`[${category}] scraping ${batch.length} detail pages...`);

        for (let i = 0; i < batch.length; i++) {
          const href = batch[i];
          log(`[${category}] ${String(i + 1).padStart(3)}/${batch.length}  ${href}`);
          try {
            const detail = await this._scrapeDetail(detailPage, href);
            detail.category  = category;
            detail.city      = this.baseUrl;
            detail.scrapedAt = new Date().toISOString();
            catResults.push(detail);
            this.results.push(detail);
            log(`           ✔  "${(detail.title || "").slice(0, 55)}"  ${detail.price || ""}`);
          } catch (e) {
            log(`           ✗  ${href}: ${e.message}`);
          }
        }

        await detailPage.close();
        offset += 120;

      } catch (e) {
        log(`[${category}] error: ${e.message} — stopping.`);
        break;
      }
    }

    await page.close();
    return catResults;
  }

  // ── Save helpers ───────────────────────────────────────────────────────────

  _saveJson(data, filePath) {
    fs.writeFileSync(filePath, JSON.stringify(data, null, 2), "utf8");
    log(`Saved JSON → ${filePath}  (${data.length} records)`);
  }

  async _saveCsv(data, filePath) {
    if (!data.length) return;
    const allKeys = new Set();
    const flat = data.map(r => {
      const row = {};
      for (const [k, v] of Object.entries(r)) {
        if (Array.isArray(v))             { row[k] = v.join(" | "); allKeys.add(k); }
        else if (v && typeof v === "object") {
          for (const [ak, av] of Object.entries(v)) {
            row[`attr_${ak}`] = av; allKeys.add(`attr_${ak}`);
          }
        } else { row[k] = v; allKeys.add(k); }
      }
      return row;
    });
    const writer = createObjectCsvWriter({
      path: filePath,
      header: [...allKeys].map(k => ({ id: k, title: k })),
    });
    await writer.writeRecords(flat);
    log(`Saved CSV  → ${filePath}  (${data.length} records)`);
  }

  async _save(data, name) {
    if (!data.length) { log(`Nothing to save for '${name}'`); return; }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const base  = path.join(this.outputDir, `${name}_${stamp}`);
    if (["json", "both"].includes(this.outputFormat)) this._saveJson(data, `${base}.json`);
    if (["csv",  "both"].includes(this.outputFormat)) await this._saveCsv(data, `${base}.csv`);
  }

  // ── Run ────────────────────────────────────────────────────────────────────

  async run() {
    const { browser } = await this._buildBrowser();
    const sep = "=".repeat(62);
    log(sep);
    log("  Craigslist Scraper — Puppeteer");
    log(`  City        : ${this.baseUrl}`);
    log(`  Categories  : ${this.categories.join(", ")}`);
    log(`  Max pages   : ${this.maxPages}`);
    log(`  Max details : ${this.maxDetails} per page`);
    log(`  Output      : ${this.outputFormat.toUpperCase()} → ${this.outputDir}/`);
    log(`  2captcha    : ${this.captcha ? "enabled" : "disabled"}`);
    log(sep);

    try {
      for (const cat of this.categories) {
        const code = CATEGORIES[cat];
        if (!code) {
          log(`Unknown category '${cat}' — skipping.`);
          log(`Available: ${Object.keys(CATEGORIES).sort().join(", ")}`);
          continue;
        }
        const catResults = await this._scrapeCategory(browser, cat, code);
        log(`[${cat}] DONE — ${catResults.length} listings collected.`);
        await this._save(catResults, cat);
      }
    } finally {
      await browser.close();
    }

    log("=".repeat(62));
    log(`ALL DONE. Total listings: ${this.results.length}`);
    await this._save(this.results, "all_categories");
    log("=".repeat(62));
    return this.results;
  }
}

// ─── CLI ──────────────────────────────────────────────────────────────────────

(async () => {
  const argv = minimist(process.argv.slice(2), {
    string:  ["city", "categories", "query", "format", "output-dir", "proxy", "captcha-key"],
    boolean: ["no-headless", "debug"],
    default: {
      city: "newyork", format: "json", "output-dir": "output",
      "max-pages": 3, "max-details": 20,
      "delay-min": 2000, "delay-max": 5000, "wait-timeout": 15000,
    },
  });

  const categories = argv.categories
    ? (argv.categories === "all"
        ? Object.keys(CATEGORIES)
        : argv.categories.split(",").map(s => s.trim()))
    : ["for_sale"];

  const scraper = new CraigslistScraperPuppeteer({
    city:         argv.city,
    categories,
    maxPages:     parseInt(argv["max-pages"]),
    maxDetails:   parseInt(argv["max-details"]),
    outputFormat: argv.format,
    outputDir:    argv["output-dir"],
    proxy:        argv.proxy        || null,
    captchaKey:   argv["captcha-key"] || null,
    headless:     !argv["no-headless"],
    delayMin:     parseInt(argv["delay-min"]),
    delayMax:     parseInt(argv["delay-max"]),
    waitTimeout:  parseInt(argv["wait-timeout"]),
    searchQuery:  argv.query        || null,
    debug:        argv.debug,
  });

  await scraper.run();
})();
