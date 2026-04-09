# Craigslist scraper — Open Source

> Free, open-source Craigslist scraper with built-in CAPTCHA bypass, anti-detect browser,
> and fingerprint spoofing. Extract data from all Craigslist categories in JSON or CSV format.

[![GitHub](https://img.shields.io/badge/GitHub-Open%20Source-black?logo=github)](https://github.com/2captcha/craigslist-scraper)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-primary-45ba4b?logo=playwright)](https://playwright.dev)
[![Selenium](https://img.shields.io/badge/Selenium-supported-43B02A?logo=selenium)](https://selenium.dev)
[![Puppeteer](https://img.shields.io/badge/Puppeteer-supported-40B5A4?logo=puppeteer)](https://pptr.dev)

---

## What Is This?

This is a **free, open-source web scraper** for [Craigslist](https://craigslist.org)
— one of the largest classified ads platforms in the world.

The scraper handles **all Craigslist categories** out of the box:
`For Sale` · `Housing` · `Jobs` · `Services` · `Community` · `Gigs` · `Resumes`
(70+ subcategories in total)

Output is clean **JSON** or **CSV**, ready for analysis, enrichment, or storage.

**No subscription. No API limits. You run it on your own machine.**

---

## Key Features

| Feature | Details |
|---|---|
| 🔄 All categories | 70+ Craigslist categories supported |
| 🏙️ All cities | 15+ US cities pre-configured, any city by slug |
| 🔍 Full listing data | Title, price, body, images, attributes, geo, timestamp |
| 📦 JSON / CSV output | Choose your format — or export both at once |
| 🤖 CAPTCHA bypass | Integrated with [2captcha.com](https://2captcha.com) — reCAPTCHA v2/v3, hCaptcha |
| 🕵️ Anti-detect browser | Stealth JS patches, disabled automation flags |
| 🖐️ Fingerprint spoofing | Random UA, viewport, locale, timezone, canvas noise |
| 🌐 Proxy support | HTTP/HTTPS proxies with auth |
| ⏱️ Randomized delays | Human-like timing between requests |
| 🔧 3 browser engines | Playwright (primary), Selenium, Puppeteer |

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/2scraper/craigslist-scraper.git
cd craigslist-scraper
```

### 2. Install dependencies

**Playwright (recommended)**
```bash
pip install playwright httpx
playwright install chromium
```

**Selenium**
```bash
pip install selenium webdriver-manager httpx
```

**Puppeteer (Node.js)**
```bash
npm install puppeteer puppeteer-extra puppeteer-extra-plugin-stealth axios csv-writer minimist
```

### 3. Run it

```bash
# Scrape all For Sale listings in New York → JSON
python craigslist_playwright_scraper.py --city newyork --categories for_sale --format json

# Scrape jobs + housing in San Francisco → CSV, 5 pages each
python craigslist_playwright_scraper.py --city sfbay --categories jobs housing --max-pages 5 --format csv

# Search for "MacBook" across electronics
python craigslist_playwright_scraper.py --categories electronics --query "MacBook" --format both

# With CAPTCHA bypass via 2captcha.com
python craigslist_playwright_scraper.py --captcha-key YOUR_2CAPTCHA_API_KEY --categories cars_trucks

# With proxy
python craigslist_playwright_scraper.py --proxy http://user:pass@host:3128

# Puppeteer version (Node.js)
node craigslist_puppeteer_scraper.js --city losangeles --categories for_sale --format json
```

---

## Output Format

Each scraped listing contains:

```json
{
  "url":        "https://newyork.craigslist.org/mnh/ele/d/iphone-15-pro/1234567890.html",
  "title":      "iPhone 15 Pro 256GB — like new",
  "price":      "$950",
  "location":   "Manhattan, NY",
  "posted":     "2024-04-01T14:32:00",
  "body":       "Selling my iPhone 15 Pro, 256GB, Space Black. Excellent condition...",
  "images":     ["https://images.craigslist.org/abc123_600x450.jpg"],
  "attributes": {
    "condition": "like new",
    "make / manufacturer": "Apple"
  },
  "category":   "electronics",
  "city":       "https://newyork.craigslist.org",
  "scraped_at": "2024-04-01T18:00:00"
}
```

---

## Supported Categories

<details>
<summary>📦 For Sale (40 subcategories)</summary>

`for_sale` · `antiques` · `appliances` · `arts_crafts` · `atvs` · `auto_parts` ·
`aviation` · `baby_kids` · `barter` · `bikes` · `boats` · `books` · `business` ·
`cars_trucks` · `cds_dvds` · `cell_phones` · `clothes` · `collectibles` · `computers` ·
`electronics` · `farm_garden` · `free_stuff` · `furniture` · `garage_sales` ·
`general_for_sale` · `health_beauty` · `heavy_equipment` · `household` · `jewelry` ·
`materials` · `motorcycles` · `musical_instruments` · `photo_video` · `rvs_campers` ·
`sporting_goods` · `tickets` · `tools` · `toys_games` · `trailers` · `video_games` · `wanted`
</details>

<details>
<summary>🏠 Housing (10 subcategories)</summary>

`housing` · `apartments` · `rooms_shared` · `sublets` · `housing_swap` ·
`housing_wanted` · `office_commercial` · `parking_storage` · `real_estate` · `vacation_rentals`
</details>

<details>
<summary>💼 Jobs (24 subcategories)</summary>

`jobs` · `accounting` · `admin` · `arch_engineering` · `art_media` · `biotech_science` ·
`business_mgmt` · `customer_service` · `education` · `food_beverage` · `general_labor` ·
`government` · `healthcare` · `hr` · `it` · `legal` · `manufacturing` · `marketing` ·
`nonprofit` · `retail` · `sales` · `security` · `skilled_trades` · `software` ·
`transportation` · `writing`
</details>

<details>
<summary>🛠 Services · 👥 Community · ⚡ Gigs · 📄 Resumes</summary>

`services` and 19 subcategories · `community` and 15 subcategories ·
`gigs` and 9 subcategories · `resumes`
</details>

---

## Supported Cities

| Slug | City |
|---|---|
| `newyork` | New York |
| `losangeles` | Los Angeles |
| `chicago` | Chicago |
| `sfbay` | San Francisco Bay Area |
| `seattle` | Seattle |
| `boston` | Boston |
| `miami` | Miami |
| `dallas` | Dallas |
| `houston` | Houston |
| `phoenix` | Phoenix |
| `denver` | Denver |
| `atlanta` | Atlanta |
| `portland` | Portland |
| `sandiego` | San Diego |
| `washingtondc` | Washington DC |
| `any-city` | Use any Craigslist city slug |

---

## CAPTCHA Bypass with 2captcha.com

Craigslist sometimes shows **reCAPTCHA v2**, **reCAPTCHA v3**, or **hCaptcha** challenges.
This scraper integrates with [2captcha.com](https://2captcha.com) — a fast, reliable
CAPTCHA solving service that handles all CAPTCHA types automatically.

### How it works

```
Scraper encounters CAPTCHA
         │
         ▼
  Sends CAPTCHA task to 2captcha.com API
         │
         ▼
  Human solvers or AI processes it (avg. 10–30 sec)
         │
         ▼
  Token returned → injected into page → scraping continues
```

### Setup

1. Register at [2captcha.com](https://2captcha.com) and top up your balance
2. Copy your API key from the dashboard
3. Pass it to the scraper:

```bash
python craigslist_scraper_playwright.py --captcha-key YOUR_2CAPTCHA_API_KEY
```

**Supported CAPTCHA types:**

| Type | Method |
|---|---|
| reCAPTCHA v2 | `userrecaptcha` |
| reCAPTCHA v3 | `userrecaptcha` + `version=v3` |
| hCaptcha | `hcaptcha` |

**Cost:** typically $0.001–$0.003 per CAPTCHA solved.
For most scraping scenarios this is negligible.

> 🔑 Get your API key at **[2captcha.com](https://2captcha.com)**

---

## Anti-Detect Browser & Fingerprint Spoofing

Running a headless browser without anti-detection is a quick way to get blocked.
This scraper implements multiple evasion layers:

### Stealth JS patches
- `navigator.webdriver` → `undefined` (removes automation flag)
- `window.chrome` → real Chrome object mock
- `Notification.permission` patch via Permissions API
- Fake `navigator.plugins` (5 plugins, not 0)
- Fixed `navigator.languages` → `['en-US', 'en']`

### Fingerprint randomization
Every session gets a **unique browser fingerprint**:
- 🖥️ Random User-Agent (Windows / macOS / Linux · Chrome / Firefox / Safari)
- 📐 Random viewport (1280×800 to 2560×1440)
- 🌍 Random locale (`en-US`, `en-GB`, `en-CA`, `en-AU`)
- 🕐 Random timezone (US timezones)
- ⚙️ Random `hardwareConcurrency` (2–16 cores)
- 💾 Random `deviceMemory` (4–16 GB)
- 🖼️ Canvas noise injection (defeats canvas fingerprinting)

### Human-like behavior
- Randomized delays between requests (configurable min/max)
- Automatic cookie banner dismissal
- Realistic page load wait strategies

### Proxy support
Route traffic through your own proxy pool:
```bash
--proxy http://username:password@proxy-host:3128
```
Use [2prx.com](https://2prx.com) residential proxies for maximum success rate.

---

## CLI Reference

```
usage: craigslist_scraper_playwright.py [-h]
  [--city CITY]
  [--categories [CATEGORIES ...]]
  [--query QUERY]
  [--max-pages MAX_PAGES]
  [--format {json,csv,both}]
  [--output-dir OUTPUT_DIR]
  [--proxy PROXY]
  [--captcha-key CAPTCHA_KEY]
  [--no-headless]
  [--no-stealth]
  [--no-fingerprint]
  [--delay-min DELAY_MIN]
  [--delay-max DELAY_MAX]

Options:
  --city            Craigslist city slug (default: newyork)
  --categories      Space-separated list of categories (default: all)
  --query           Optional search keyword
  --max-pages       Pages per category (default: 3)
  --format          Output format: json | csv | both (default: json)
  --output-dir      Output directory (default: ./output)
  --proxy           Proxy URL: http://user:pass@host:port
  --captcha-key     Your 2captcha.com API key
  --no-headless     Show browser window (useful for debugging)
  --no-stealth      Disable anti-detect JS patches
  --no-fingerprint  Disable fingerprint randomization
  --delay-min       Min delay between requests in seconds (default: 1.5)
  --delay-max       Max delay between requests in seconds (default: 4.5)
```

---

## Need More Power?

If you need **high-volume scraping**, **rotating residential proxies**,
or a **managed CAPTCHA solving infrastructure**, check out our services at
**[2captcha.com](https://2captcha.com)**:

| Service | What it solves |
|---|---|
| 🤖 **CAPTCHA API** | reCAPTCHA, hCaptcha, Cloudflare Turnstile, GeeTest and 20+ more |
| 🖐️ **Anti-Detect Browser** | Unique fingerprints per session, managed profiles |
| 🌐 **Residential Proxies** | Rotating IPs, city-level targeting, unlimited bandwidth |

---

## Technology Stack

| Engine | Language | Stealth | Async | CAPTCHA |
|---|---|---|---|---|
| **Playwright** | Python | ✅ | ✅ | ✅ |
| **Selenium** | Python | ✅ | ❌ | ✅ |
| **Puppeteer** | Node.js | ✅ (plugin) | ✅ | ✅ |

---

## Project Structure

```
craigslist-scraper/
├── craigslist_playwright_scraper.py   # Playwright scraper (recommended)
├── craigslist_selenium_scraper.py     # Selenium scraper
├── craigslist_puppeteer_scraper.js    # Puppeteer scraper (Node.js)
├── requirements.txt                   # Python dependencies
├── package.json                       # Node.js dependencies
├── output/                            # Scraped data (auto-created)
│   ├── for_sale_20240401_180000.json
│   ├── jobs_20240401_180100.csv
│   └── all_categories_20240401_185000.json
└── README.md
```

---

## requirements.txt

```
playwright>=1.44.0
httpx>=0.27.0
selenium>=4.21.0
webdriver-manager>=4.0.1
```

---

## License

MIT © [2captcha.com](https://2scraper.com)

**[⭐ Star on GitHub](https://github.com/2scraper/craigslist-scraper)**
· **[🐛 Report an Issue](https://github.com/2scraper/craigslist-scraper/issues)**
· **[💬 Discussions](https://github.com/2scraper/craigslist-scraper/discussions)**
