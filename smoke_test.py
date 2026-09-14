#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for craigslist-scraper.

Run this FIRST, before touching a real browser or craigslist.org, to confirm
the parsing, the output contract, the page-state policy and the shared engine
decisions still hold:

    python3 smoke_test.py        # exits non-zero if anything failed

One file of plain functions with inline fixtures -- no pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps this as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each engine
in its own virtualenv and fails if the corresponding group reports a skip --
"skipped, engine absent" reads identically to a real import error, so the two
have to be told apart somewhere.

WHAT THESE CHECKS ARE FOR
-------------------------
Not coverage. Every defect this repo has actually shipped was invisible to a
green suite of the obvious kind, so the checks here are aimed at the shapes
those took:

  * a row count that swings between runs while both report success
  * a column that is 100% populated and wrong
  * a call site that binds fine at import and dies on the first fetch
  * a constant whose type changed under a caller that still uses the old one
  * a policy constant nothing reads
  * a fixture that carries someone's phone number into a public repo
"""

import ast
import builtins
import csv
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import fields

import captcha_solver
import env_config
import page_flow
import product_parser
import proxy_pool
from diff_runs import diff_products
from output_writer import (Product, save, finish_run, write_csv, run_meta,
                           dedupe_by_key, ROW_CLASS_BY_MODE,
                           UNIQUE_BY_SKU_MODES, SOURCE_DEFAULT,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR)
from product_parser import (parse_products, parse_posting, parse_rendered_cards,
                            page_currency, page_url, paginates_by_url,
                            pagination_refusal, category_from_url,
                            category_code, listing_kind, site_host,
                            is_supported_host, unsupported_reason,
                            area_from_url, area_kind, sku_from_url,
                            posting_slug, strip_tracking, prices_in,
                            detect_page_state, detect_bot_challenge,
                            served_by_craigslist, is_no_results,
                            search_header, total_results, SELECTORS,
                            RESULT_CAP, CANONICAL_HOST, CATEGORY_NAMES,
                            BOT_CHALLENGE_MARKERS)
from proxy_pool import ProxyPool, mask, split_credentials

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINES = ("playwright_scraper.py", "selenium_scraper.py",
           "puppeteer_scraper.py")

_failures = []


def check(label, condition):
    """Print and record one check.

    Returns the condition so callers can accumulate with `ok &= check(...)`.
    """
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def section(title):
    print("\n== %s" % title)


LISTING_URL = "https://www.craigslist.org/search/area/newyork?cat=sss"
MEXICO_URL = "https://www.craigslist.org/search/area/mexicocity?cat=sss"
TOKYO_URL = "https://www.craigslist.org/search/area/tokyo?cat=sss"
JOBS_URL = "https://www.craigslist.org/search/area/newyork?cat=jjj"
POSTING_URL = ("https://www.craigslist.org/view/d/"
               "brooklyn-for-sale2021-toyota-sequoia/kfvqiXscPUVb9PyC34B4YN")


# ==========================================================================
# Fixtures
# ==========================================================================
#
# Real captures, taken 2026-09-14, trimmed to whole nodes and then VERIFIED
# to parse identically to the untrimmed original -- every pinned field of
# every row they keep, checked before they were committed.
#
# ASSERT VALUES, NOT COVERAGE. A column can be 100% populated and entirely
# wrong: a sibling repo shipped a review_count of 445279961 on every row of
# every mode because it stripped the digits out of an aria-label, while its
# coverage check happily said 100%. So the checks below pin expected prices,
# ids, coordinates and titles for named adverts.

# A served New York for-sale listing: 12 result entries, 11 of which the
# page's JSON-LD publishes. The one it skips is the whole point -- the two
# views are NOT positionally aligned, and a fixture where they were would
# test the opposite of what ships.
LISTING_NEWYORK = r"""<!doctype html><html><head><title>us_newyork_forsale</title>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script type="application/ld+json" id="ld_searchpage_results">{"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [{"item": {"@type": "Product", "image": ["https://images.craigslist.org/01111_4huroNo94s_0t20CI_600x450.jpg", "https://images.craigslist.org/00808_5nUsj1K1Mqc_0t20CI_600x450.jpg", "https://images.craigslist.org/00z0z_2uqfi4Bk569_0t20CI_600x450.jpg", "https://images.craigslist.org/00z0z_cBKB8MBlVmg_0t20CI_600x450.jpg", "https://images.craigslist.org/01717_cjIen7RZqxs_0t20CI_600x450.jpg"], "description": "", "name": "Plates Edwin Knowles", "@context": "http://schema.org", "offers": {"availableAtOrFrom": {"address": {"addressRegion": "NY", "addressLocality": "College Point", "postalCode": "", "streetAddress": "", "@type": "PostalAddress", "addressCountry": ""}, "@type": "Place", "geo": {"latitude": 40.7817005959413, "@type": "GeoCoordinates", "longitude": -73.8317018670391}}, "price": "30000.00", "@type": "Offer", "priceCurrency": "USD"}}, "position": "0", "@type": "ListItem"}, {"@type": "ListItem", "position": "1", "item": {"image": ["https://images.craigslist.org/01111_6Z4wrvPV3Xi_0CI0lq_600x450.jpg", "https://images.craigslist.org/00O0O_hPIvYMkcoeV_0CI0nj_600x450.jpg", "https://images.craigslist.org/00u0u_kRiyo3e30Xa_0CI0hk_600x450.jpg"], "description": "", "@context": "http://schema.org", "name": "Bumper badger Bumper Guard", "@type": "Product", "offers": {"priceCurrency": "USD", "availableAtOrFrom": {"@type": "Place", "geo": {"longitude": -73.8738984897749, "@type": "GeoCoordinates", "latitude": 40.7612987147734}, "address": {"@type": "PostalAddress", "addressCountry": "", "streetAddress": "", "postalCode": "", "addressLocality": "East Elmhurst", "addressRegion": "NY"}}, "price": "20.00", "@type": "Offer"}}}, {"item": {"@type": "Product", "description": "", "image": ["https://images.craigslist.org/00Z0Z_4pEmhKfxidb_0t20CI_600x450.jpg", "https://images.craigslist.org/01515_PKiJCGKtrV_0CI0t2_600x450.jpg"], "name": "Gloves", "@context": "http://schema.org", "offers": {"priceCurrency": "USD", "@type": "Offer", "availableAtOrFrom": {"geo": {"longitude": -73.4044667197981, "latitude": 41.1095183984077, "@type": "GeoCoordinates"}, "@type": "Place", "address": {"postalCode": "", "addressLocality": "Norwalk", "addressRegion": "CT", "addressCountry": "", "@type": "PostalAddress", "streetAddress": ""}}, "price": "20.00"}}, "position": "2", "@type": "ListItem"}, {"item": {"@type": "Product", "@context": "http://schema.org", "name": "Battery charger/engine starter", "image": ["https://images.craigslist.org/00L0L_h9pUzLpqZzL_0t20CI_600x450.jpg"], "description": "", "offers": {"availableAtOrFrom": {"address": {"addressLocality": "Southport", "postalCode": "", "addressRegion": "CT", "@type": "PostalAddress", "addressCountry": "", "streetAddress": ""}, "@type": "Place", "geo": {"latitude": 41.1348964180261, "@type": "GeoCoordinates", "longitude": -73.2969185309532}}, "price": "25.00", "@type": "Offer", "priceCurrency": "USD"}}, "position": "3", "@type": "ListItem"}, {"item": {"name": "Emporio Armani Wristwatch", "@context": "http://schema.org", "description": "", "image": ["https://images.craigslist.org/00p0p_iYEEHxECwfa_0CI0t2_600x450.jpg"], "@type": "Product", "offers": {"priceCurrency": "USD", "price": "100.00", "availableAtOrFrom": {"@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": 41.0380986362907, "longitude": -73.8596983037811}, "address": {"streetAddress": "", "addressCountry": "", "@type": "PostalAddress", "addressRegion": "NY", "addressLocality": "Irvington", "postalCode": ""}}, "@type": "Offer"}}, "@type": "ListItem", "position": "4"}, {"item": {"offers": {"priceCurrency": "USD", "price": "250.00", "availableAtOrFrom": {"address": {"addressLocality": "Brooklyn", "postalCode": "", "addressRegion": "NY", "addressCountry": "", "@type": "PostalAddress", "streetAddress": ""}, "@type": "Place", "geo": {"latitude": 40.6783991702737, "@type": "GeoCoordinates", "longitude": -73.9211021906819}}, "@type": "Offer"}, "@type": "Product", "image": ["https://images.craigslist.org/00A0A_31Stnp3FH4P_0CI0t2_600x450.jpg", "https://images.craigslist.org/00V0V_NR3TicxQwt_0t20CI_600x450.jpg", "https://images.craigslist.org/00j0j_hwPWepUwL3X_0CI0t2_600x450.jpg", "https://images.craigslist.org/01313_2xHqmXNOGKY_0CI0t2_600x450.jpg"], "description": "", "@context": "http://schema.org", "name": "Apple MacBook Air 2020"}, "position": "5", "@type": "ListItem"}, {"item": {"offers": {"priceCurrency": "USD", "@type": "Offer", "availableAtOrFrom": {"address": {"@type": "PostalAddress", "addressCountry": "", "streetAddress": "", "postalCode": "", "addressLocality": "Oakland Gardens", "addressRegion": "NY"}, "@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": 40.742799626443, "longitude": -73.7588050197263}}, "price": "45.00"}, "@type": "Product", "@context": "http://schema.org", "name": "(NEW) Platypus QuickDraw 1L Water Filter System", "image": ["https://images.craigslist.org/00707_dlBDswE6Rm5_0oo0ww_600x450.jpg", "https://images.craigslist.org/00X0X_4WDhEER3i0n_0oo0ww_600x450.jpg", "https://images.craigslist.org/00u0u_8bb1PxVb7ko_0ga0ao_600x450.jpg"], "description": ""}, "position": "6", "@type": "ListItem"}, {"item": {"@type": "Product", "image": ["https://images.craigslist.org/00K0K_iwQKrpepVZB_0CI0lM_600x450.jpg", "https://images.craigslist.org/00000_eKgUiYfqLU5_0CI0lM_600x450.jpg", "https://images.craigslist.org/00V0V_2jFVB6WkbTQ_0CI0lM_600x450.jpg", "https://images.craigslist.org/00r0r_5VYzKyGJMDs_0CI0lM_600x450.jpg", "https://images.craigslist.org/00J0J_6dLeMj8c0Vm_0CI0lM_600x450.jpg", "https://images.craigslist.org/00d0d_kOHi6lcsJk1_0CI0lM_600x450.jpg", "https://images.craigslist.org/00y0y_gMeBUm6XEk7_0CI0lM_600x450.jpg", "https://images.craigslist.org/00404_bBdiATyIkzn_0CI0lM_600x450.jpg", "https://images.craigslist.org/00G0G_dQo4NNc2I3t_0CI0lM_600x450.jpg"], "description": "", "name": "2026 Sure Trac 6x10 Low Profile Dump Landscape Trailer RAMPS 10k", "@context": "http://schema.org", "offers": {"priceCurrency": "USD", "price": "8395.00", "availableAtOrFrom": {"geo": {"latitude": 40.4108994996864, "@type": "GeoCoordinates", "longitude": -74.2380022823628}, "@type": "Place", "address": {"postalCode": "", "addressLocality": "Matawan", "addressRegion": "NJ", "addressCountry": "", "@type": "PostalAddress", "streetAddress": ""}}, "@type": "Offer"}}, "position": "7", "@type": "ListItem"}, {"@type": "ListItem", "position": "8", "item": {"offers": {"priceCurrency": "USD", "@type": "Offer", "availableAtOrFrom": {"address": {"streetAddress": "", "@type": "PostalAddress", "addressCountry": "", "addressRegion": "NY", "postalCode": "", "addressLocality": "East Elmhurst"}, "@type": "Place", "geo": {"longitude": -73.8738984897749, "@type": "GeoCoordinates", "latitude": 40.7612987147734}}, "price": "20.00"}, "@type": "Product", "@context": "http://schema.org", "name": "Samsung Subwoofer model # PS-WJ450", "image": ["https://images.craigslist.org/00y0y_gad0kv3jivq_0Gb0P0_600x450.jpg", "https://images.craigslist.org/00f0f_1DPQ4BO0VHe_1761u8_600x450.jpg", "https://images.craigslist.org/00808_7skk6C3ZC1P_0MM132_600x450.jpg", "https://images.craigslist.org/01515_czhRkIjFHa_0BY0MJ_600x450.jpg"], "description": ""}}, {"item": {"offers": {"priceCurrency": "USD", "price": "15.00", "availableAtOrFrom": {"address": {"addressLocality": "Melville", "postalCode": "", "addressRegion": "NY", "@type": "PostalAddress", "addressCountry": "", "streetAddress": ""}, "geo": {"latitude": 40.7945990218246, "@type": "GeoCoordinates", "longitude": -73.4029982289692}, "@type": "Place"}, "@type": "Offer"}, "name": "I-Ecko  Eco Friendly Speakers", "@context": "http://schema.org", "description": "", "image": ["https://images.craigslist.org/00303_iiOIx8Y0ZkW_0t20CI_600x450.jpg", "https://images.craigslist.org/00s0s_fxNmeClYnVH_0t20CI_600x450.jpg", "https://images.craigslist.org/00h0h_kOuXLNEi9Hm_0t20CI_600x450.jpg", "https://images.craigslist.org/00505_b5kUhxvhZcL_0t20CI_600x450.jpg"], "@type": "Product"}, "position": "9", "@type": "ListItem"}, {"item": {"@context": "http://schema.org", "name": "7ft. Pre-Lit Stella Pine Artificial Christmas Tree, Warm White LED Lig", "description": "", "image": ["https://images.craigslist.org/00L0L_4j23gElHIZ5_0cA0kk_600x450.jpg", "https://images.craigslist.org/00m0m_gHlu8121M5G_08Y0ku_600x450.jpg", "https://images.craigslist.org/01313_7MtlkfVpNwZ_0l20io_600x450.jpg"], "@type": "Product", "offers": {"priceCurrency": "USD", "price": "60.00", "availableAtOrFrom": {"address": {"@type": "PostalAddress", "addressCountry": "", "streetAddress": "", "postalCode": "", "addressLocality": "East Elmhurst", "addressRegion": "NY"}, "@type": "Place", "geo": {"longitude": -73.8738984897749, "latitude": 40.7612987147734, "@type": "GeoCoordinates"}}, "@type": "Offer"}}, "position": "10", "@type": "ListItem"}, {"item": {"offers": {"@type": "Offer", "availableAtOrFrom": {"@type": "Place", "geo": {"latitude": 40.7945990218246, "@type": "GeoCoordinates", "longitude": -73.4029982289692}, "address": {"streetAddress": "", "@type": "PostalAddress", "addressCountry": "", "addressRegion": "NY", "postalCode": "", "addressLocality": "Melville"}}, "price": "3.00", "priceCurrency": "USD"}, "image": ["https://images.craigslist.org/00z0z_6v9EPG5Dwsq_0lM0t2_600x450.jpg", "https://images.craigslist.org/01111_jt9nPMo3jZp_0lM0t2_600x450.jpg"], "description": "", "name": "DVDs of Movies", "@context": "http://schema.org", "@type": "Product"}, "@type": "ListItem", "position": "11"}, {"position": "12", "@type": "ListItem", "item": {"name": "WHITE CALLIGARIS DINING ROOM SET EXTENDABLE TABLE AND 6 CHAIRS", "@context": "http://schema.org", "image": ["https://images.craigslist.org/00l0l_hh35ktKzhkF_0uY0ne_600x450.jpg", "https://images.craigslist.org/00p0p_kkwd1nMcu1Y_0uY0ne_600x450.jpg", "https://images.craigslist.org/00404_2q8gARGhLEm_0lM0t2_600x450.jpg", "https://images.craigslist.org/00R0R_jrp42iF23eS_0uY0ne_600x450.jpg", "https://images.craigslist.org/00M0M_c8oTyX5gsaa_0lM0t2_600x450.jpg", "https://images.craigslist.org/01212_5tdFCWjpgLG_0lM0t2_600x450.jpg", "https://images.craigslist.org/00R0R_kD5chXgubxC_0lM0t2_600x450.jpg", "https://images.craigslist.org/00A0A_hRFxjuxlIf7_0uY0ne_600x450.jpg", "https://images.craigslist.org/00G0G_ccHBADzkkgH_0uY0ne_600x450.jpg", "https://images.craigslist.org/00R0R_dIFYnyXRljf_0uY0ne_600x450.jpg", "https://images.craigslist.org/00I0I_e12TYZW6wRs_0uY0ne_600x450.jpg", "https://images.craigslist.org/00A0A_hRFxjuxlIf7_0uY0ne_600x450.jpg", "https://images.craigslist.org/00G0G_ccHBADzkkgH_0uY0ne_600x450.jpg", "https://images.craigslist.org/00R0R_dIFYnyXRljf_0uY0ne_600x450.jpg", "https://images.craigslist.org/00O0O_7aB2dXmJZiY_0lM0t2_600x450.jpg", "https://images.craigslist.org/00g0g_jUXITvb4yBN_0lM0t2_600x450.jpg", "https://images.craigslist.org/00R0R_3Lt1CB04mS0_0uY0ne_600x450.jpg", "https://images.craigslist.org/00L0L_1hvsrbDBoxW_0uY0ne_600x450.jpg"], "description": "", "@type": "Product", "offers": {"availableAtOrFrom": {"address": {"addressCountry": "", "@type": "PostalAddress", "streetAddress": "", "postalCode": "", "addressLocality": "Staten Island", "addressRegion": "NY"}, "@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": 40.6039003329658, "longitude": -74.1472016498687}}, "price": "650.00", "@type": "Offer", "priceCurrency": "USD"}}}]}</script>
</head><body class="no-js">
<div class="cl-static-header">
<a href="/">craigslist</a>
<h1>For Sale in New York City</h1>
</div>
<ol class="cl-static-search-results">
<li class="cl-static-hub-links"><div>see also</div></li>
<li class="cl-static-search-result" title="Plates Edwin Knowles">
<a href="https://www.craigslist.org/view/d/college-point-plates-edwin-knowles/vAjGnkwp5v35FdiHDf8H9X">
<div class="title">Plates Edwin Knowles</div>
<div class="details">
<div class="price">$30,000</div>
<div class="location">
                        New York
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Bumper badger Bumper Guard">
<a href="https://www.craigslist.org/view/d/east-elmhurst-bumper-badger-bumper-guard/s6f7Am3i12wiQnXLaMYAUf">
<div class="title">Bumper badger Bumper Guard</div>
<div class="details">
<div class="price">$20</div>
<div class="location">
                        east elmhurst
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Gloves">
<a href="https://www.craigslist.org/view/d/norwalk-gloves/1CfDJSLrKCttpRxFjRtN6W">
<div class="title">Gloves</div>
<div class="details">
<div class="price">$20</div>
<div class="location">
                        Norwalk
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Battery charger/engine starter">
<a href="https://www.craigslist.org/view/d/southport-battery-charger-engine-starter/sH8aCUisoeGssGL74uJweP">
<div class="title">Battery charger/engine starter</div>
<div class="details">
<div class="price">$25</div>
<div class="location">
                        Norwalk
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Emporio Armani Wristwatch">
<a href="https://www.craigslist.org/view/d/irvington-emporio-armani-wristwatch/jjzSNXNeYosJoSxKYwfy8r">
<div class="title">Emporio Armani Wristwatch</div>
<div class="details">
<div class="price">$100</div>
<div class="location">
                        Irvington NY
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Apple MacBook Air 2020">
<a href="https://www.craigslist.org/view/d/brooklyn-apple-macbook-air-2020/bZvcdcqd7g8AFVZVf46RT2">
<div class="title">Apple MacBook Air 2020</div>
<div class="details">
<div class="price">$250</div>
<div class="location">
                        Brooklyn
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="(NEW) Platypus QuickDraw 1L Water Filter System">
<a href="https://www.craigslist.org/view/d/oakland-gardens-new-platypus-quickdraw/vtvNV16B8dMjdAUQ9zWcR6">
<div class="title">(NEW) Platypus QuickDraw 1L Water Filter System</div>
<div class="details">
<div class="price">$45</div>
<div class="location">
                        Bayside
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="2026 Sure Trac 6x10 Low Profile Dump Landscape Trailer RAMPS 10k">
<a href="https://www.craigslist.org/view/d/matawan-2026-sure-trac-6x10-low-profile/fGSvcjgKsiafVyXpXU45Eh">
<div class="title">2026 Sure Trac 6x10 Low Profile Dump Landscape Trailer RAMPS 10k</div>
<div class="details">
<div class="price">$8,395</div>
<div class="location">
                        Aberdeen NJ
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Samsung Subwoofer model # PS-WJ450">
<a href="https://www.craigslist.org/view/d/east-elmhurst-samsung-subwoofer-model/tTMTKaHpzvhXcjCQMpE61b">
<div class="title">Samsung Subwoofer model # PS-WJ450</div>
<div class="details">
<div class="price">$20</div>
<div class="location">
                        Elmhurst
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="I-Ecko  Eco Friendly Speakers">
<a href="https://www.craigslist.org/view/d/melville-ecko-eco-friendly-speakers/aUwMugztXAyWiY9zRYFkMw">
<div class="title">I-Ecko  Eco Friendly Speakers</div>
<div class="details">
<div class="price">$15</div>
<div class="location">
                        Melville
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="7ft. Pre-Lit Stella Pine Artificial Christmas Tree, Warm White LED Lig">
<a href="https://www.craigslist.org/view/d/east-elmhurst-7ft-pre-lit-stella-pine/5jQs9Nvn55BFGLhTBUettQ">
<div class="title">7ft. Pre-Lit Stella Pine Artificial Christmas Tree, Warm White LED Lig</div>
<div class="details">
<div class="price">$60</div>
<div class="location">
                        Elmhurst
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="SUB Woofers 8/2 design. Brand new still wrapped.">
<a href="https://www.craigslist.org/view/d/new-york-sub-woofers-2-design-brand-new/xd9CCMEyXmN6ZGJHhWQjeE">
<div class="title">SUB Woofers 8/2 design. Brand new still wrapped.</div>
<div class="details">
<div class="price">$10</div>
<div class="location">
                        Midtown East
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="DVDs of Movies">
<a href="https://www.craigslist.org/view/d/melville-dvds-of-movies/noya3f346K9rQBfMCn9QUX">
<div class="title">DVDs of Movies</div>
<div class="details">
<div class="price">$3</div>
<div class="location">
                        Melville
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="WHITE CALLIGARIS DINING ROOM SET EXTENDABLE TABLE AND 6 CHAIRS">
<a href="https://www.craigslist.org/view/d/staten-island-white-calligaris-dining/d1qynu7i7T6MqMkBApxMwr">
<div class="title">WHITE CALLIGARIS DINING ROOM SET EXTENDABLE TABLE AND 6 CHAIRS</div>
<div class="details">
<div class="price">$650</div>
<div class="location">
                        STATEN ISLAND
                    </div>
</div>
</a>
</li>
</ol>
</body></html>"""

# Mexico City: MXN prices, and every structured entry carrying Craigslist's
# (0, 0) placeholder where its coordinates would be. 244 of 244 entries on the
# full capture did, against 0 of 1,249 across the US and Canadian ones.
LISTING_MEXICOCITY = r"""<!doctype html><html><head><title>latam_mexicocity_forsale</title>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script type="application/ld+json" id="ld_searchpage_results">{"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [{"position": "0", "item": {"@context": "http://schema.org", "@type": "Product", "description": "", "offers": {"price": "250000.00", "availableAtOrFrom": {"@type": "Place", "address": {"postalCode": "", "addressCountry": "", "addressLocality": "", "streetAddress": "", "addressRegion": "", "@type": "PostalAddress"}, "geo": {"longitude": 0.0, "@type": "GeoCoordinates", "latitude": 0.0}}, "priceCurrency": "MXN", "@type": "Offer"}, "name": "YES ITS TRUE !!! LOOK !", "image": ["https://images.craigslist.org/00h0h_48nb1pYiFLK_0CI0t1_600x450.jpg", "https://images.craigslist.org/01212_jhgmtS8IRQh_0CI0t2_600x450.jpg", "https://images.craigslist.org/00h0h_48nb1pYiFLK_0CI0t1_600x450.jpg", "https://images.craigslist.org/01212_jhgmtS8IRQh_0CI0t2_600x450.jpg", "https://images.craigslist.org/00l0l_8vRo7Egdo2v_0eD08i_600x450.jpg"]}, "@type": "ListItem"}, {"position": "1", "item": {"description": "", "@type": "Product", "image": ["https://images.craigslist.org/00I0I_kEuuYKfZ25w_0CI0pK_600x450.jpg", "https://images.craigslist.org/00r0r_6VLhKCjpcOi_0CI0pK_600x450.jpg", "https://images.craigslist.org/00m0m_1Y3E1kSo8vM_0CI0pK_600x450.jpg", "https://images.craigslist.org/00u0u_gP9XN3j0iBi_0CI0pK_600x450.jpg", "https://images.craigslist.org/00v0v_klRByNOPjtp_0CI0pK_600x450.jpg", "https://images.craigslist.org/00T0T_92EsfAocdPj_0CI0pK_600x450.jpg", "https://images.craigslist.org/00l0l_8JG9qEBlFrJ_0CI0pK_600x450.jpg", "https://images.craigslist.org/00O0O_fvcNq9JcX5E_0CI0pK_600x450.jpg", "https://images.craigslist.org/00G0G_aqvTeZmPP7L_0CI0pK_600x450.jpg", "https://images.craigslist.org/00606_hsG3BrLbzii_0CI0pK_600x450.jpg", "https://images.craigslist.org/00k0k_7XdKEhYNYlU_0CI0pK_600x450.jpg", "https://images.craigslist.org/01414_5lt8GsDFopy_0CI0pK_600x450.jpg", "https://images.craigslist.org/01414_bPZvjidenQq_0CI0pK_600x450.jpg", "https://images.craigslist.org/00V0V_eIrwpZ0P9S0_0CI0pK_600x450.jpg", "https://images.craigslist.org/00h0h_gy74s45GND9_0CI0pK_600x450.jpg", "https://images.craigslist.org/00O0O_lAJAf0kDfbM_0CI0pK_600x450.jpg", "https://images.craigslist.org/00s0s_5KVRp8ODa4G_0CI0pK_600x450.jpg", "https://images.craigslist.org/00P0P_i2BBnRExfhj_0CI0pK_600x450.jpg", "https://images.craigslist.org/00A0A_fAk4LtonJwa_0CI0pK_600x450.jpg", "https://images.craigslist.org/00g0g_1RSHvSGng1g_0CI0pK_600x450.jpg", "https://images.craigslist.org/00303_8W01axzuozV_0CI0pK_600x450.jpg", "https://images.craigslist.org/01010_i8ctc6woneH_0CI0pK_600x450.jpg", "https://images.craigslist.org/01414_h9mIhw6tVgX_0CI0pK_600x450.jpg"], "name": "RESTAUNTRANT BAR AND GRILL LEASE TO BUY OPTION", "offers": {"price": "8600.00", "availableAtOrFrom": {"@type": "Place", "address": {"@type": "PostalAddress", "addressRegion": "", "postalCode": "", "streetAddress": "", "addressLocality": "", "addressCountry": ""}, "geo": {"@type": "GeoCoordinates", "latitude": 0.0, "longitude": 0.0}}, "priceCurrency": "MXN", "@type": "Offer"}, "@context": "http://schema.org"}, "@type": "ListItem"}, {"@type": "ListItem", "item": {"description": "", "@type": "Product", "name": "VENDO LIBRO MANUAL JAGUAR E- TYPE", "image": ["https://images.craigslist.org/00A0A_eSJF08I15nv_0Ba0rS_600x450.jpg", "https://images.craigslist.org/00M0M_7vfC3Bh3Is9_0Ba0rS_600x450.jpg", "https://images.craigslist.org/00303_epBsaFwWw6v_0Ba0rS_600x450.jpg", "https://images.craigslist.org/00E0E_aQ8U5mcMEBF_0Ba0rS_600x450.jpg", "https://images.craigslist.org/00L0L_ayFido71zo6_0lM0t2_600x450.jpg", "https://images.craigslist.org/01313_fszRLGWFF9v_0Ba0rS_600x450.jpg", "https://images.craigslist.org/00p0p_hynOMXy3YMj_0Ba0rS_600x450.jpg", "https://images.craigslist.org/00p0p_dcEJ3zxaCXe_0Ba0rS_600x450.jpg"], "offers": {"@type": "Offer", "priceCurrency": "MXN", "availableAtOrFrom": {"geo": {"longitude": 0.0, "latitude": 0.0, "@type": "GeoCoordinates"}, "@type": "Place", "address": {"postalCode": "", "streetAddress": "", "addressCountry": "", "addressLocality": "", "@type": "PostalAddress", "addressRegion": ""}}, "price": "980.00"}, "@context": "http://schema.org"}, "position": "2"}, {"@type": "ListItem", "item": {"@type": "Product", "description": "", "image": ["https://images.craigslist.org/00V0V_asfprtxQNAm_0aL0fu_600x450.jpg", "https://images.craigslist.org/00H0H_lcZsWV7uOme_0aS0fu_600x450.jpg", "https://images.craigslist.org/00303_lNorykcCKyH_0d20fu_600x450.jpg", "https://images.craigslist.org/00L0L_aLBbEIAwrIu_0b10fu_600x450.jpg"], "name": "Venta Estampa de la Inmaculada Concepci\u00f3n de 1912.", "offers": {"priceCurrency": "MXN", "@type": "Offer", "price": "600.00", "availableAtOrFrom": {"address": {"addressRegion": "", "@type": "PostalAddress", "postalCode": "", "addressCountry": "", "addressLocality": "", "streetAddress": ""}, "@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": 0.0, "longitude": 0.0}}}, "@context": "http://schema.org"}, "position": "3"}, {"position": "4", "item": {"image": ["https://images.craigslist.org/00E0E_jsJrW6FCewZ_0rO0t2_600x450.jpg", "https://images.craigslist.org/00808_jad12cH6Zlr_0lM0t2_600x450.jpg", "https://images.craigslist.org/00606_68CjJjQufQo_0lM0t2_600x450.jpg", "https://images.craigslist.org/00J0J_e2OEg2szIFA_0lM0t2_600x450.jpg", "https://images.craigslist.org/01616_cAdOnJiaEFC_0lM0t2_600x450.jpg", "https://images.craigslist.org/00202_jdV2ZPT0yGo_0lM0t2_600x450.jpg", "https://images.craigslist.org/00K0K_cMCOdMcHHlo_0CI0t2_600x450.jpg", "https://images.craigslist.org/00u0u_285idqHWDbu_0qk0t2_600x450.jpg", "https://images.craigslist.org/00P0P_Oy2GHIFBMg_0lM0t2_600x450.jpg", "https://images.craigslist.org/00A0A_9p09w1mo4NO_0lM0t2_600x450.jpg", "https://images.craigslist.org/00y0y_6iKMoiFNAcm_0CI0t2_600x450.jpg", "https://images.craigslist.org/00k0k_gHumttLBatf_0CI0t2_600x450.jpg"], "name": "** OFERTA ** PORCELANA FINA DE DRESDEN A MITAD DE PRECIO", "offers": {"@type": "Offer", "priceCurrency": "MXN", "availableAtOrFrom": {"geo": {"longitude": 0.0, "@type": "GeoCoordinates", "latitude": 0.0}, "address": {"addressLocality": "", "addressCountry": "", "streetAddress": "", "postalCode": "", "addressRegion": "", "@type": "PostalAddress"}, "@type": "Place"}, "price": "12500.00"}, "@type": "Product", "description": "", "@context": "http://schema.org"}, "@type": "ListItem"}, {"@type": "ListItem", "item": {"@context": "http://schema.org", "offers": {"availableAtOrFrom": {"geo": {"longitude": 0.0, "@type": "GeoCoordinates", "latitude": 0.0}, "address": {"@type": "PostalAddress", "addressRegion": "", "streetAddress": "", "addressCountry": "", "addressLocality": "", "postalCode": ""}, "@type": "Place"}, "price": "1700.00", "@type": "Offer", "priceCurrency": "MXN"}, "image": ["https://images.craigslist.org/00s0s_3ezUo8LsXS0_0CI0t2_600x450.jpg", "https://images.craigslist.org/00N0N_lfS4hjJ1WlQ_0CI0t2_600x450.jpg", "https://images.craigslist.org/00t0t_1E8ZNDhJiYd_0CI0t2_600x450.jpg", "https://images.craigslist.org/00c0c_hWVdTsFPMDy_0CI0t2_600x450.jpg", "https://images.craigslist.org/00O0O_L7fu39JHi0_0lM0t2_600x450.jpg"], "name": "Vendo Hermoso Cuadro de Litograf\u00eda", "description": "", "@type": "Product"}, "position": "5"}, {"@type": "ListItem", "item": {"name": "$8,900 - 2016 Subaru Crosstrek 2.0i Premium AWD - Smog OK + Tags 2026", "offers": {"availableAtOrFrom": {"address": {"addressRegion": "", "@type": "PostalAddress", "postalCode": "", "addressLocality": "", "addressCountry": "", "streetAddress": ""}, "@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": 0.0, "longitude": 0.0}}, "price": "8900.00", "@type": "Offer", "priceCurrency": "MXN"}, "image": ["https://images.craigslist.org/00000_8jN1y9Jt0py_0iN0gS_600x450.jpg", "https://images.craigslist.org/00N0N_hA888uDPpYm_0iS0gF_600x450.jpg", "https://images.craigslist.org/00H0H_cjYpOkLgHb2_0kE0fu_600x450.jpg", "https://images.craigslist.org/00z0z_dmMlyohSdeq_0fu0kE_600x450.jpg", "https://images.craigslist.org/00n0n_kY8RQhiuz3q_0bq06z_600x450.jpg", "https://images.craigslist.org/00404_928QERrmb09_09x05q_600x450.jpg", "https://images.craigslist.org/01212_6pffQIKyC6A_09x05q_600x450.jpg", "https://images.craigslist.org/00000_dZufLUYxmEA_0e20kE_600x450.jpg", "https://images.craigslist.org/00X0X_iaLBQl8OAKb_0jm0pO_600x450.jpg"], "@type": "Product", "description": "", "@context": "http://schema.org"}, "position": "6"}, {"@type": "ListItem", "position": "7", "item": {"name": "2016 Ford Transit con THERMOKING", "image": ["https://images.craigslist.org/01616_icI32PcnC7O_0pO0jm_600x450.jpg", "https://images.craigslist.org/00J0J_2IhJ01ccNb2_0pO0js_600x450.jpg", "https://images.craigslist.org/00O0O_ho7XTYbSeC3_0pO0jm_600x450.jpg", "https://images.craigslist.org/00U0U_4gcYRiVnbmM_0pO0jm_600x450.jpg", "https://images.craigslist.org/00o0o_NTHeQVkAkx_0pO0jm_600x450.jpg", "https://images.craigslist.org/00b0b_akelPEm8B3I_0pO0n2_600x450.jpg", "https://images.craigslist.org/01515_gkKCkUxlV0Q_0pO0jm_600x450.jpg", "https://images.craigslist.org/00k0k_lam38Kztj7d_0pO0p7_600x450.jpg", "https://images.craigslist.org/00v0v_4Vl4p6gtai3_0pO0jm_600x450.jpg", "https://images.craigslist.org/00V0V_6Znwbvt1OBf_0pO0kM_600x450.jpg", "https://images.craigslist.org/01111_kEzj3NRfH7T_0pO0mJ_600x450.jpg", "https://images.craigslist.org/00o0o_82gs94qgh8O_0pO0jm_600x450.jpg", "https://images.craigslist.org/00P0P_4JrKB5AcLTi_0pO0jm_600x450.jpg", "https://images.craigslist.org/00000_98fFc4YjGhA_0pO0jm_600x450.jpg", "https://images.craigslist.org/00a0a_3s32sffrFJU_0pO0jm_600x450.jpg", "https://images.craigslist.org/00L0L_h33hKLE7v5U_0pO0jm_600x450.jpg", "https://images.craigslist.org/00D0D_fSnPnPSY5QO_0pO0jm_600x450.jpg", "https://images.craigslist.org/00b0b_cH1mm160tpU_0pO0jm_600x450.jpg", "https://images.craigslist.org/00n0n_lg5jJrW0GF_0pO0jm_600x450.jpg"], "offers": {"price": "14900.00", "availableAtOrFrom": {"geo": {"@type": "GeoCoordinates", "latitude": 0.0, "longitude": 0.0}, "address": {"addressRegion": "", "@type": "PostalAddress", "postalCode": "", "addressCountry": "", "addressLocality": "", "streetAddress": ""}, "@type": "Place"}, "priceCurrency": "MXN", "@type": "Offer"}, "description": "", "@type": "Product", "@context": "http://schema.org"}}, {"@type": "ListItem", "position": "8", "item": {"@context": "http://schema.org", "@type": "Product", "description": "", "offers": {"@type": "Offer", "priceCurrency": "MXN", "availableAtOrFrom": {"geo": {"longitude": 0.0, "latitude": 0.0, "@type": "GeoCoordinates"}, "address": {"@type": "PostalAddress", "addressRegion": "", "postalCode": "", "streetAddress": "", "addressLocality": "", "addressCountry": ""}, "@type": "Place"}, "price": "1.00"}, "image": ["https://images.craigslist.org/00B0B_hxpW4m8rKjj_08b08a_600x450.jpg"], "name": "Vibrador Interno De Alta Frecuencia IEC 58/120/8 US Wacker Neuson"}}]}</script>
</head><body class="no-js">
<div class="cl-static-header">
<a href="/">craigslist</a>
<h1>For Sale in Mexico City</h1>
</div>
<ol class="cl-static-search-results">
<li class="cl-static-hub-links"><div>see also</div></li>
<li class="cl-static-search-result" title="YES ITS TRUE !!! LOOK !">
<a href="https://www.craigslist.org/view/d/yes-its-true-look/6XdHghX9tvwmw9avWxXv8R">
<div class="title">YES ITS TRUE !!! LOOK !</div>
<div class="details">
<div class="price">$250,000</div>
<div class="location">
                        BUFFALO NY USA
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="MUEBLES GRATIS, ME MUDO DEL PAIS">
<a href="https://www.craigslist.org/view/d/muebles-gratis-me-mudo-del-pais/jL6RFXRBu2tWJZqcJRxRty">
<div class="title">MUEBLES GRATIS, ME MUDO DEL PAIS</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        MEXICO CITY
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="RESTAUNTRANT BAR AND GRILL LEASE TO BUY OPTION">
<a href="https://www.craigslist.org/view/d/restauntrant-bar-and-grill-lease-to-buy/mFUP1YGx5enCYXuQnMx8xC">
<div class="title">RESTAUNTRANT BAR AND GRILL LEASE TO BUY OPTION</div>
<div class="details">
<div class="price">$8,600</div>
<div class="location">
                        San Antonio
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="VENDO LIBRO MANUAL JAGUAR E- TYPE">
<a href="https://www.craigslist.org/view/d/vendo-libro-manual-jaguar-type/3EPDdW24tBboumgQKhucmk">
<div class="title">VENDO LIBRO MANUAL JAGUAR E- TYPE</div>
<div class="details">
<div class="price">$980</div>
<div class="location">
                        ROMA NORTE, CDMX
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Venta Estampa de la Inmaculada Concepción de 1912.">
<a href="https://www.craigslist.org/view/d/venta-estampa-de-la-inmaculada/qjiXS2Fiy4SzApYnqRcFh6">
<div class="title">Venta Estampa de la Inmaculada Concepción de 1912.</div>
<div class="details">
<div class="price">$600</div>
<div class="location">
                        ROMA NORTE, CDMX
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="** OFERTA ** PORCELANA FINA DE DRESDEN A MITAD DE PRECIO">
<a href="https://www.craigslist.org/view/d/oferta-porcelana-fina-de-dresden-mitad/gBe4PbRVcuLMJAuuCAXdob">
<div class="title">** OFERTA ** PORCELANA FINA DE DRESDEN A MITAD DE PRECIO</div>
<div class="details">
<div class="price">$12,500</div>
<div class="location">
                        ROMA NORTE, CDMX
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Vendo Hermoso Cuadro de Litografía">
<a href="https://www.craigslist.org/view/d/vendo-hermoso-cuadro-de-litografa/sRnW8JYrSugzF1snDqA4a4">
<div class="title">Vendo Hermoso Cuadro de Litografía</div>
<div class="details">
<div class="price">$1,700</div>
<div class="location">
                        ROMA NORTE, CDMX
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="$8,900 - 2016 Subaru Crosstrek 2.0i Premium AWD - Smog OK + Tags 2026">
<a href="https://www.craigslist.org/view/d/subaru-crosstrek-20i-premium-awd-smog/bkEXf9nuGc3vs81w1xjAsg">
<div class="title">$8,900 - 2016 Subaru Crosstrek 2.0i Premium AWD - Smog OK + Tags 2026</div>
<div class="details">
<div class="price">$8,900</div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="2016 Ford Transit con THERMOKING">
<a href="https://www.craigslist.org/view/d/2016-ford-transit-con-thermoking/j49ZFV1WoXhAxKwdcnZ62a">
<div class="title">2016 Ford Transit con THERMOKING</div>
<div class="details">
<div class="price">$14,900</div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Vibrador Interno De Alta Frecuencia IEC 58/120/8 US Wacker Neuson">
<a href="https://www.craigslist.org/view/d/vibrador-interno-de-alta-frecuencia-iec/9V4HVpnuj6HFxj3yJV4rF7">
<div class="title">Vibrador Interno De Alta Frecuencia IEC 58/120/8 US Wacker Neuson</div>
<div class="details">
<div class="price">$1</div>
<div class="location">
                        ECATEPEC DE MORELOS
                    </div>
</div>
</a>
</li>
</ol>
</body></html>"""

# Tokyo: JPY, and the advert whose own published price is 2,500,013,000 yen
# for a set of IKEA shelves. The site says so in both its views, so the parser
# reports it -- pinned here so nobody "fixes" it into disagreeing with the
# site.
LISTING_TOKYO = r"""<!doctype html><html><head><title>asia_tokyo_forsale</title>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script type="application/ld+json" id="ld_searchpage_results">{"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [{"item": {"image": ["https://images.craigslist.org/00V0V_c6mUNNkBqAy_0t20CI_600x450.jpg", "https://images.craigslist.org/00s0s_ax6aYRM62xt_0t20CI_600x450.jpg", "https://images.craigslist.org/00909_l0KztAXnwwT_0t20CI_600x450.jpg"], "description": "", "@type": "Product", "@context": "http://schema.org", "name": "Karate Gloves", "offers": {"priceCurrency": "JPY", "availableAtOrFrom": {"@type": "Place", "address": {"streetAddress": "", "postalCode": "", "addressLocality": "", "addressRegion": "", "addressCountry": "", "@type": "PostalAddress"}, "geo": {"@type": "GeoCoordinates", "latitude": 0.0, "longitude": 0.0}}, "price": "2000.00", "@type": "Offer"}}, "@type": "ListItem", "position": "23"}, {"position": "24", "@type": "ListItem", "item": {"name": "DK Geography of the world", "@context": "http://schema.org", "@type": "Product", "image": ["https://images.craigslist.org/00D0D_9mfLwXZM7pK_0gl0t2_600x450.jpg", "https://images.craigslist.org/00e0e_iQhAtEaMqpB_0gl0t2_600x450.jpg", "https://images.craigslist.org/00F0F_bFEHsqepICg_0gl0t2_600x450.jpg", "https://images.craigslist.org/00O0O_f0c5n8UQ8vD_0gl0t2_600x450.jpg"], "description": "", "offers": {"availableAtOrFrom": {"address": {"addressCountry": "", "@type": "PostalAddress", "postalCode": "", "streetAddress": "", "addressRegion": "", "addressLocality": ""}, "@type": "Place", "geo": {"longitude": 0.0, "@type": "GeoCoordinates", "latitude": 0.0}}, "price": "1000.00", "priceCurrency": "JPY", "@type": "Offer"}}}, {"item": {"offers": {"priceCurrency": "JPY", "price": "2500013000.00", "availableAtOrFrom": {"geo": {"longitude": 0.0, "latitude": 0.0, "@type": "GeoCoordinates"}, "@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "", "addressRegion": "", "addressLocality": "", "streetAddress": "", "postalCode": ""}}, "@type": "Offer"}, "name": "IKEA IVAR Shelving Units \u2013 \u00a513,000 each / \u00a525,000 pair", "@type": "Product", "image": ["https://images.craigslist.org/00T0T_g58gVLR5Ank_0eL0hq_600x450.jpg", "https://images.craigslist.org/00q0q_aS4Nwc97dZw_0gn0hq_600x450.jpg", "https://images.craigslist.org/00p0p_i6NFcjiCU5C_084084_600x450.jpg"], "description": "", "@context": "http://schema.org"}, "@type": "ListItem", "position": "25"}, {"position": "26", "@type": "ListItem", "item": {"offers": {"availableAtOrFrom": {"geo": {"longitude": 0.0, "@type": "GeoCoordinates", "latitude": 0.0}, "@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "", "addressRegion": "", "addressLocality": "", "postalCode": "", "streetAddress": ""}}, "price": "1100.00", "priceCurrency": "JPY", "@type": "Offer"}, "name": "Designers' Guild Kids YAMAKA child's set --\u30c7\u30b6\u30a4\u30ca\u30fc\u30ba\u30ae\u30eb\u30c9 \u30e4\u30de", "@context": "http://schema.org", "@type": "Product", "image": ["https://images.craigslist.org/00A0A_toYgC6dDT9_0jm0eq_600x450.jpg", "https://images.craigslist.org/00S0S_k7j90S5bqFK_0ew0jm_600x450.jpg", "https://images.craigslist.org/00f0f_3it5Abn8X2O_0jm0ew_600x450.jpg", "https://images.craigslist.org/00d0d_dOIYrJB2BSP_0jm0ew_600x450.jpg", "https://images.craigslist.org/00G0G_kVHxoPtbGHz_0jm0ew_600x450.jpg", "https://images.craigslist.org/00f0f_kc2awGc4pnC_0pD0je_600x450.jpg", "https://images.craigslist.org/00Q0Q_3XwW3jhjqD1_0ew0jm_600x450.jpg", "https://images.craigslist.org/00707_5HGl0rt4YKf_0ew0jm_600x450.jpg", "https://images.craigslist.org/00j0j_7Osow5yquLe_0hq0ne_600x450.jpg", "https://images.craigslist.org/00r0r_kuuex3vhfYH_0ew0jm_600x450.jpg", "https://images.craigslist.org/00r0r_ahr3v39iYM8_0c8096_600x450.jpg"], "description": ""}}, {"item": {"@context": "http://schema.org", "@type": "Product", "image": ["https://images.craigslist.org/00B0B_gocuYmyc9j1_0ew0jm_600x450.jpg"], "description": "", "name": "Sarah by Leroy, J. T.", "offers": {"@type": "Offer", "priceCurrency": "JPY", "availableAtOrFrom": {"geo": {"latitude": 0.0, "@type": "GeoCoordinates", "longitude": 0.0}, "address": {"addressCountry": "", "@type": "PostalAddress", "streetAddress": "", "postalCode": "", "addressLocality": "", "addressRegion": ""}, "@type": "Place"}, "price": "500.00"}}, "position": "27", "@type": "ListItem"}, {"@type": "ListItem", "position": "28", "item": {"offers": {"availableAtOrFrom": {"@type": "Place", "address": {"addressCountry": "", "@type": "PostalAddress", "streetAddress": "", "postalCode": "", "addressRegion": "", "addressLocality": ""}, "geo": {"longitude": 0.0, "@type": "GeoCoordinates", "latitude": 0.0}}, "price": "1800.00", "priceCurrency": "JPY", "@type": "Offer"}, "@context": "http://schema.org", "description": "", "image": ["https://images.craigslist.org/01717_2eiwRYdzcrg_0ew0jm_600x450.jpg", "https://images.craigslist.org/00E0E_jH6FhuA2Ati_0ew0jm_600x450.jpg"], "@type": "Product", "name": "The Gosford Files: UFOs over the NSW Central Coast"}}]}</script>
</head><body class="no-js">
<div class="cl-static-header">
<a href="/">craigslist</a>
<h1>For Sale in Tokyo</h1>
</div>
<ol class="cl-static-search-results">
<li class="cl-static-hub-links"><div>see also</div></li>
<li class="cl-static-search-result" title="Karate Gloves">
<a href="https://www.craigslist.org/view/d/karate-gloves/wPFYZGgS7rcuSmR1deka7H">
<div class="title">Karate Gloves</div>
<div class="details">
<div class="price">¥2,000</div>
<div class="location">
                        Meguro
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="DK Geography of the world">
<a href="https://www.craigslist.org/view/d/dk-geography-of-the-world/3cF5YpkJ9DTUqcxwFavat9">
<div class="title">DK Geography of the world</div>
<div class="details">
<div class="price">¥1,000</div>
<div class="location">
                        Ebisu/ Meguro
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="IKEA IVAR Shelving Units – ¥13,000 each / ¥25,000 pair">
<a href="https://www.craigslist.org/view/d/ikea-ivar-shelving-units-each-pair/qPav4voWSrBkRtvazvh4oW">
<div class="title">IKEA IVAR Shelving Units – ¥13,000 each / ¥25,000 pair</div>
<div class="details">
<div class="price">¥2,500,013,000</div>
<div class="location">
                        KAGURAZAKA
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Designers' Guild Kids YAMAKA child's set --デザイナーズギルド ヤマ">
<a href="https://www.craigslist.org/view/d/designers-guild-kids-yamaka-childs-set/in27FEuXroPBz8kZcGns3j">
<div class="title">Designers' Guild Kids YAMAKA child's set --デザイナーズギルド ヤマ</div>
<div class="details">
<div class="price">¥1,100</div>
<div class="location">
                        Between Oji and Nishi-sugamo Stations
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Sarah by Leroy, J. T.">
<a href="https://www.craigslist.org/view/d/sarah-by-leroy-t/iWDbZ9yLLkSjeg6N1BmjEb">
<div class="title">Sarah by Leroy, J. T.</div>
<div class="details">
<div class="price">¥500</div>
<div class="location">
                        Between Nishi-sugamo and Oji stations
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="The Gosford Files: UFOs over the NSW Central Coast">
<a href="https://www.craigslist.org/view/d/the-gosford-files-ufos-over-the-nsw/5HkiJLQ3gr9ZCCRat6J899">
<div class="title">The Gosford Files: UFOs over the NSW Central Coast</div>
<div class="details">
<div class="price">¥1,800</div>
<div class="location">
                        Between Oji and Nishi-sugamo
                    </div>
</div>
</a>
</li>
</ol>
</body></html>"""

# A jobs listing: six entries and NO JSON-LD ItemList at all. Whole categories
# publish none -- jobs, services, community -- so these rows come from the
# served list alone, with no currency and no coordinates. That is the site's
# design, not a parsing failure.
LISTING_JOBS = r"""<!doctype html><html><head><title>us_newyork_jobs</title>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script type="application/ld+json" id="ld_searchpage_results">{"@context": "https://schema.org", "@type": "ItemList", "itemListElement": []}</script>
</head><body class="no-js">
<div class="cl-static-header">
<a href="/">craigslist</a>
<h1>jobs in New York City</h1>
</div>
<ol class="cl-static-search-results">
<li class="cl-static-hub-links"><div>see also</div></li>
<li class="cl-static-search-result" title="Roofer, Sider and board ups">
<a href="https://www.craigslist.org/view/d/staten-island-roofer-sider-and-board-ups/rfSB7va5Vn4SdEBpSiTT9g">
<div class="title">Roofer, Sider and board ups</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        Staten Island
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Server needed at two8two Bar &amp; Burger">
<a href="https://www.craigslist.org/view/d/brooklyn-server-needed-at-two8two-bar/kaVdbMGHwpVVh23kqzCMhe">
<div class="title">Server needed at two8two Bar &amp; Burger</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        Boerum Hill, Brooklyn
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Deli/counter person">
<a href="https://www.craigslist.org/view/d/hewlett-deli-counter-person/aLt4PLKoRU1RUypJkrtJBL">
<div class="title">Deli/counter person</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        long island
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="*** PREP COOK***">
<a href="https://www.craigslist.org/view/d/brooklyn-prep-cook/7V4jGzHizXDNuhUcbo9UfG">
<div class="title">*** PREP COOK***</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        Bay Ridge
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Line cook">
<a href="https://www.craigslist.org/view/d/hewlett-line-cook/tCHCtjJMb1JGqAtZgsFrPu">
<div class="title">Line cook</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        long island
                    </div>
</div>
</a>
</li>
<li class="cl-static-search-result" title="Helper or Installer of metal/wood">
<a href="https://www.craigslist.org/view/d/astoria-helper-or-installer-of-metal/gscGZ88LkoDWL5d5rHEopM">
<div class="title">Helper or Installer of metal/wood</div>
<div class="details">
<div class="price">$0</div>
<div class="location">
                        Astoria
                    </div>
</div>
</a>
</li>
</ol>
</body></html>"""

# A cars advert: eight attributes including the unlabelled `year` and
# `makemodel` pair, 24 images across two galleries, both timestamps, and the
# numeric post id the served listing does not carry.
#
# SCRUBBED: the seller's phone number in the body, replaced with
# 555-0100-SCRUBBED. Everything the site generates around it is verbatim.
POSTING_CARS = r"""<!doctype html><html><head>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script id="ld_breadcrumb_data" type="application/ld+json">
    {"@type":"BreadcrumbList","@context":"https://schema.org","itemListElement":[{"@type":"ListItem","position":1,"name":"craigslist: new york","item":"https://www.craigslist.org/area/newyork"},{"item":"https://www.craigslist.org/subarea/brk","position":2,"@type":"ListItem","name":"brooklyn"},{"name":"for sale","position":3,"@type":"ListItem","item":"https://www.craigslist.org/search/subarea/brk?cat=sss"},{"item":"https://www.craigslist.org/search/subarea/brk?cat=cta&purveyor=dealer","position":4,"@type":"ListItem","name":"cars+trucks"},{"item":"https://www.craigslist.org/view/d/brooklyn-for-sale2021-toyota-sequoia/kfvqiXscPUVb9PyC34B4YN","position":5,"@type":"ListItem","name":"🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles"}]}
</script>
<script id="ld_posting_data" type="application/ld+json">
    {"image":["https://images.craigslist.org/00g0g_dq2dbt7p7mR_0sU0gg_600x450.jpg","https://images.craigslist.org/00q0q_5hyskRrHwHB_0sU0gg_600x450.jpg","https://images.craigslist.org/01111_3u0N58EoJVh_0sU0gg_600x450.jpg","https://images.craigslist.org/00Z0Z_j7C5pP1TfNL_0sU0gg_600x450.jpg","https://images.craigslist.org/00v0v_ginFyDYmErG_0sU0gg_600x450.jpg","https://images.craigslist.org/00000_7Vk83YHpXOX_0sU0gg_600x450.jpg","https://images.craigslist.org/00U0U_YbMUuPhVw2_0sU0gg_600x450.jpg","https://images.craigslist.org/00N0N_5q1Q5PKjull_0sU0gg_600x450.jpg","https://images.craigslist.org/00M0M_eRD29UCgau3_0sU0gg_600x450.jpg","https://images.craigslist.org/00Z0Z_ifNS7mm7k3e_0sU0gg_600x450.jpg","https://images.craigslist.org/01111_1iw0tPWb0Wv_0sU0gg_600x450.jpg","https://images.craigslist.org/00d0d_9puaZCnF8sC_0sU0gg_600x450.jpg","https://images.craigslist.org/00q0q_emtUE6BwcED_0sU0gg_600x450.jpg","https://images.craigslist.org/01717_jUbY6LaYvbZ_0sU0gg_600x450.jpg","https://images.craigslist.org/01111_3gx2BR2wgXt_0sU0gg_600x450.jpg","https://images.craigslist.org/00909_4xUTMZqGucF_0sU0gg_600x450.jpg","https://images.craigslist.org/01616_3DOnkNw5Aw6_0sU0gg_600x450.jpg","https://images.craigslist.org/00Z0Z_fylHZPc3IXM_0sU0gg_600x450.jpg","https://images.craigslist.org/00D0D_lrISUTE063i_0sU0gg_600x450.jpg","https://images.craigslist.org/00808_3AEcdAo6v0m_0sU0gg_600x450.jpg","https://images.craigslist.org/00n0n_d5hWrW1afcY_0sU0gg_600x450.jpg","https://images.craigslist.org/00n0n_gsx88z1AXKl_0sU0gg_600x450.jpg","https://images.craigslist.org/00707_3V3ZLEarujX_0sU0gg_600x450.jpg","https://images.craigslist.org/00A0A_g0pg0wFSm5S_0sU0gg_600x450.jpg"],"name":"🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles","@context":"http://schema.org","@type":"Product","description":"2021 Toyota Sequoia Platinum – 62K Miles | $36,995    Powerful 5.7L i-FORCE V8 Engine  Premium Leather Interior  Power Moonroof  Apple CarPlay® &amp; Android Auto™  Toyota Safety Sense™  Heated Front Seats  JBL® Premium Audio System  Third-Row Seating  Tow Package  Power Liftgate  Premium Alloy Wheels    Call or Text: 555-0100-SCRUBBED  Visit our website: www.seewaldcars.com ","offers":{"price":"36995.00","priceCurrency":"USD","@type":"Offer","availableAtOrFrom":{"@type":"Place","geo":{"@type":"GeoCoordinates","longitude":"-73.936700","latitude":"40.670000"},"address":{"addressCountry":"US","postalCode":"11213","addressRegion":"NY","@type":"PostalAddress","streetAddress":"","addressLocality":"Brooklyn"}}}}
</script>
</head><body>
<span class="postingtitletext">
<span id="titletextonly">🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles</span> - <span class="price">$36,995</span> </span>
<section id="postingbody">
<div class="print-information print-qrcode-container">
<p class="print-qrcode-label">QR Code Link to This Post</p>
<div class="print-qrcode" data-location="https://www.craigslist.org/view/d/brooklyn-for-sale2021-toyota-sequoia/kfvqiXscPUVb9PyC34B4YN">
</div>
</div>
2021 Toyota Sequoia Platinum – 62K Miles | $36,995<br/>
<br/>
Powerful 5.7L i-FORCE V8 Engine<br/>
Premium Leather Interior<br/>
Power Moonroof<br/>
Apple CarPlay® &amp; Android Auto™<br/>
Toyota Safety Sense™<br/>
Heated Front Seats<br/>
JBL® Premium Audio System<br/>
Third-Row Seating<br/>
Tow Package<br/>
Power Liftgate<br/>
Premium Alloy Wheels<br/>
<br/>
Call or Text: 555-0100-SCRUBBED<br/>
Visit our website: www.seewaldcars.com
    </section>
<div class="attrgroup">
<div class="attr important">
<span class="valu year">2021</span>
<span class="valu makemodel"> <a href="https://www.craigslist.org/search/area/newyork?auto_make_model=toyota%20sequoia%20platinum&amp;cat=cta">toyota sequoia platinum</a>
</span>
</div>
</div>
<div class="attrgroup">
<div class="attr condition">
<span class="labl">condition:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=cta&amp;condition=30">excellent</a>
</span>
</div>
<div class="attr auto_fuel_type">
<span class="labl">fuel:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?auto_fuel_type=1&amp;cat=cta">gas</a>
</span>
</div>
<div class="attr auto_miles">
<span class="labl">odometer:</span>
<span class="valu">62,000</span>
</div>
<div class="attr auto_title_status">
<span class="labl">title status:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?auto_title_status=3&amp;cat=cta">rebuilt</a>
</span>
</div>
<div class="attr auto_transmission">
<span class="labl">transmission:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?auto_transmission=2&amp;cat=cta">automatic</a>
</span>
</div>
<div class="attr auto_bodytype">
<span class="labl">type:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?auto_bodytype=10&amp;cat=cta">SUV</a>
</span>
</div>
</div>
<p class="attrgroup">
<span class="otherpostings">
<a href="https://www.craigslist.org/search/area/newyork?cat=sss&amp;userpostingid=7966144398">
          more ads by this seller
        </a>
</span>
</p>
<div class="postinginfos">
<p class="postinginfo">post id: 7966144398</p>
<p class="postinginfo reveal">posted: <time class="date timeago" datetime="2026-09-14T08:44:24-0400">2026-09-14 08:44</time></p>
<p class="postinginfo reveal">updated: <time class="date timeago" datetime="2026-09-14T08:44:25-0400">2026-09-14 08:44</time></p>
<p class="postinginfo">
<a class="bestof-link" href="https://post.craigslist.org/flag" title="nominate for best-of-CL">
<span class="bestof-icon">♥ </span><span class="bestof-text">best of</span>
</a> <sup>[<a href="https://www.craigslist.org/about/best-of-craigslist">?</a>]</sup>
</p>
</div>
<div class="viewposting" data-accuracy="22" data-latitude="40.670000" data-longitude="-73.936700" id="map"></div>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 1" onerror="this.onerror = null; this.style.width='650px'; this.style.height='400px'; this.src='/images/no_image.png'" src="https://images.craigslist.org/00g0g_dq2dbt7p7mR_0sU0gg_600x450.jpg" title="1"/>
<a class="thumb" data-imgid="dq2dbt7p7mR_0sU0gg" href="https://images.craigslist.org/00g0g_dq2dbt7p7mR_0sU0gg_600x450.jpg" id="1_thumb_dq2dbt7p7mR_0sU0gg" title="1"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 1 thumbnail" class="selected" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00g0g_dq2dbt7p7mR_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 1 thumbnail" class="selected" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00g0g_dq2dbt7p7mR_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="5hyskRrHwHB_0sU0gg" href="https://images.craigslist.org/00q0q_5hyskRrHwHB_0sU0gg_600x450.jpg" id="2_thumb_5hyskRrHwHB_0sU0gg" title="2"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 2 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00q0q_5hyskRrHwHB_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 2 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00q0q_5hyskRrHwHB_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="3u0N58EoJVh_0sU0gg" href="https://images.craigslist.org/01111_3u0N58EoJVh_0sU0gg_600x450.jpg" id="3_thumb_3u0N58EoJVh_0sU0gg" title="3"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 3 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_3u0N58EoJVh_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 3 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_3u0N58EoJVh_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="j7C5pP1TfNL_0sU0gg" href="https://images.craigslist.org/00Z0Z_j7C5pP1TfNL_0sU0gg_600x450.jpg" id="4_thumb_j7C5pP1TfNL_0sU0gg" title="4"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 4 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00Z0Z_j7C5pP1TfNL_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 4 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00Z0Z_j7C5pP1TfNL_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="ginFyDYmErG_0sU0gg" href="https://images.craigslist.org/00v0v_ginFyDYmErG_0sU0gg_600x450.jpg" id="5_thumb_ginFyDYmErG_0sU0gg" title="5"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 5 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00v0v_ginFyDYmErG_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 5 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00v0v_ginFyDYmErG_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="7Vk83YHpXOX_0sU0gg" href="https://images.craigslist.org/00000_7Vk83YHpXOX_0sU0gg_600x450.jpg" id="6_thumb_7Vk83YHpXOX_0sU0gg" title="6"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 6 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00000_7Vk83YHpXOX_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 6 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00000_7Vk83YHpXOX_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="YbMUuPhVw2_0sU0gg" href="https://images.craigslist.org/00U0U_YbMUuPhVw2_0sU0gg_600x450.jpg" id="7_thumb_YbMUuPhVw2_0sU0gg" title="7"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 7 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00U0U_YbMUuPhVw2_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 7 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00U0U_YbMUuPhVw2_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="5q1Q5PKjull_0sU0gg" href="https://images.craigslist.org/00N0N_5q1Q5PKjull_0sU0gg_600x450.jpg" id="8_thumb_5q1Q5PKjull_0sU0gg" title="8"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 8 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00N0N_5q1Q5PKjull_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 8 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00N0N_5q1Q5PKjull_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="eRD29UCgau3_0sU0gg" href="https://images.craigslist.org/00M0M_eRD29UCgau3_0sU0gg_600x450.jpg" id="9_thumb_eRD29UCgau3_0sU0gg" title="9"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 9 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00M0M_eRD29UCgau3_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 9 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00M0M_eRD29UCgau3_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="ifNS7mm7k3e_0sU0gg" href="https://images.craigslist.org/00Z0Z_ifNS7mm7k3e_0sU0gg_600x450.jpg" id="10_thumb_ifNS7mm7k3e_0sU0gg" title="10"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 10 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00Z0Z_ifNS7mm7k3e_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 10 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00Z0Z_ifNS7mm7k3e_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="1iw0tPWb0Wv_0sU0gg" href="https://images.craigslist.org/01111_1iw0tPWb0Wv_0sU0gg_600x450.jpg" id="11_thumb_1iw0tPWb0Wv_0sU0gg" title="11"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 11 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_1iw0tPWb0Wv_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 11 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_1iw0tPWb0Wv_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="9puaZCnF8sC_0sU0gg" href="https://images.craigslist.org/00d0d_9puaZCnF8sC_0sU0gg_600x450.jpg" id="12_thumb_9puaZCnF8sC_0sU0gg" title="12"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 12 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00d0d_9puaZCnF8sC_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 12 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00d0d_9puaZCnF8sC_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="emtUE6BwcED_0sU0gg" href="https://images.craigslist.org/00q0q_emtUE6BwcED_0sU0gg_600x450.jpg" id="13_thumb_emtUE6BwcED_0sU0gg" title="13"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 13 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00q0q_emtUE6BwcED_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 13 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00q0q_emtUE6BwcED_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="jUbY6LaYvbZ_0sU0gg" href="https://images.craigslist.org/01717_jUbY6LaYvbZ_0sU0gg_600x450.jpg" id="14_thumb_jUbY6LaYvbZ_0sU0gg" title="14"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 14 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01717_jUbY6LaYvbZ_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 14 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01717_jUbY6LaYvbZ_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="3gx2BR2wgXt_0sU0gg" href="https://images.craigslist.org/01111_3gx2BR2wgXt_0sU0gg_600x450.jpg" id="15_thumb_3gx2BR2wgXt_0sU0gg" title="15"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 15 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_3gx2BR2wgXt_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 15 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_3gx2BR2wgXt_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="4xUTMZqGucF_0sU0gg" href="https://images.craigslist.org/00909_4xUTMZqGucF_0sU0gg_600x450.jpg" id="16_thumb_4xUTMZqGucF_0sU0gg" title="16"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 16 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00909_4xUTMZqGucF_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 16 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00909_4xUTMZqGucF_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="3DOnkNw5Aw6_0sU0gg" href="https://images.craigslist.org/01616_3DOnkNw5Aw6_0sU0gg_600x450.jpg" id="17_thumb_3DOnkNw5Aw6_0sU0gg" title="17"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 17 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01616_3DOnkNw5Aw6_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 17 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01616_3DOnkNw5Aw6_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="fylHZPc3IXM_0sU0gg" href="https://images.craigslist.org/00Z0Z_fylHZPc3IXM_0sU0gg_600x450.jpg" id="18_thumb_fylHZPc3IXM_0sU0gg" title="18"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 18 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00Z0Z_fylHZPc3IXM_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 18 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00Z0Z_fylHZPc3IXM_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="lrISUTE063i_0sU0gg" href="https://images.craigslist.org/00D0D_lrISUTE063i_0sU0gg_600x450.jpg" id="19_thumb_lrISUTE063i_0sU0gg" title="19"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 19 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00D0D_lrISUTE063i_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 19 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00D0D_lrISUTE063i_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="3AEcdAo6v0m_0sU0gg" href="https://images.craigslist.org/00808_3AEcdAo6v0m_0sU0gg_600x450.jpg" id="20_thumb_3AEcdAo6v0m_0sU0gg" title="20"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 20 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00808_3AEcdAo6v0m_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 20 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00808_3AEcdAo6v0m_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="d5hWrW1afcY_0sU0gg" href="https://images.craigslist.org/00n0n_d5hWrW1afcY_0sU0gg_600x450.jpg" id="21_thumb_d5hWrW1afcY_0sU0gg" title="21"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 21 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00n0n_d5hWrW1afcY_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 21 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00n0n_d5hWrW1afcY_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="gsx88z1AXKl_0sU0gg" href="https://images.craigslist.org/00n0n_gsx88z1AXKl_0sU0gg_600x450.jpg" id="22_thumb_gsx88z1AXKl_0sU0gg" title="22"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 22 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00n0n_gsx88z1AXKl_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 22 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00n0n_gsx88z1AXKl_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="3V3ZLEarujX_0sU0gg" href="https://images.craigslist.org/00707_3V3ZLEarujX_0sU0gg_600x450.jpg" id="23_thumb_3V3ZLEarujX_0sU0gg" title="23"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 23 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00707_3V3ZLEarujX_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 23 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00707_3V3ZLEarujX_0sU0gg_50x50c.jpg"/>
<a class="thumb" data-imgid="g0pg0wFSm5S_0sU0gg" href="https://images.craigslist.org/00A0A_g0pg0wFSm5S_0sU0gg_600x450.jpg" id="24_thumb_g0pg0wFSm5S_0sU0gg" title="24"><img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 24 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00A0A_g0pg0wFSm5S_0sU0gg_50x50c.jpg"/></a>
<img alt="🔴FOR SALE!2021 Toyota Sequoia Platinum – 62K Miles 24 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00A0A_g0pg0wFSm5S_0sU0gg_50x50c.jpg"/>
</body></html>"""

# A housing advert. Its structured data carries NO price -- the whole category
# does not -- so the price here comes from the DOM, and the images live on the
# thumbnail strip's hrefs rather than on img tags.
#
# SCRUBBED: a named broker, their agency and two real licence numbers, all
# replaced with obvious placeholders. Republishing a person's name and licence
# is a separate act from the site showing it on its own page.
POSTING_HOUSING = r"""<!doctype html><html><head>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script id="ld_breadcrumb_data" type="application/ld+json">
    {"itemListElement":[{"@type":"ListItem","name":"craigslist: new york","item":"https://www.craigslist.org/area/newyork","position":1},{"position":2,"item":"https://www.craigslist.org/subarea/brk","name":"brooklyn","@type":"ListItem"},{"item":"https://www.craigslist.org/search/subarea/brk?cat=hhh","@type":"ListItem","name":"housing","position":3},{"position":4,"item":"https://www.craigslist.org/search/subarea/brk?cat=apa","name":"apartments / housing for rent","@type":"ListItem"},{"item":"https://www.craigslist.org/view/d/brooklyn-boerum-hill-brooklyn-apartment/5EmuzWqvSj2bkWUKihATkC","@type":"ListItem","name":"Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St","position":5}],"@type":"BreadcrumbList","@context":"https://schema.org"}
</script>
<script id="ld_posting_data" type="application/ld+json">
    {"@context":"http://schema.org","petsAllowed":true,"numberOfBedrooms":"1","longitude":"-73.949800","@type":"Apartment","address":{"@type":"PostalAddress","addressLocality":"Brooklyn","postalCode":"11222","addressRegion":"NY","addressCountry":"US","streetAddress":""},"latitude":"40.727200","numberOfBathroomsTotal":1,"name":"Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St"}
</script>
</head><body>
<span class="postingtitletext">
<span class="price">$1,700</span> <span class="housing">/ 1br - </span><span id="titletextonly">Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St</span><span> (Boerum Hill)</span> </span>
<section id="postingbody">
<div class="print-information print-qrcode-container">
<p class="print-qrcode-label">QR Code Link to This Post</p>
<div class="print-qrcode" data-location="https://www.craigslist.org/view/d/brooklyn-boerum-hill-brooklyn-apartment/5EmuzWqvSj2bkWUKihATkC">
</div>
</div>
Comfortable apartment available in Boerum Hill, Brooklyn near 35 Wyckoff St. The home offers a bright living space, comfortable rooms, a clean kitchen, full bathroom, hardwood flooring, and good closet/storage space. Conveniently located near public transportation, grocery stores, restaurants, cafés, shops, and parks. If interested, please reply with name, number and your preferred time to view the apartment.<br/>
</section>
<div class="attrgroup">
<span class="attr important">
                1BR / 1Ba
            </span>
</div>
<div class="attrgroup">
<span class="labl important">open house dates:</span>
<div class="attr">
<span><a class="valu" href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;sale_date=2026-09-14">
                                monday 2026-09-14
                            </a></span>
</div>
<div class="attr">
<span><a class="valu" href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;sale_date=2026-09-15">
                                tuesday 2026-09-15
                            </a></span>
</div>
<div class="attr">
<span><a class="valu" href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;sale_date=2026-09-16">
                                wednesday 2026-09-16
                            </a></span>
</div>
</div>
<div class="attrgroup">
<div class="attr application_fee_explained">
<span class="labl">application fee details:</span>
<span class="valu">PERSON-SCRUBBED - AGENCY-SCRUBBED - LICENCE-SCRUBBED</span>
</div>
<div class="attr broker_fee_explained">
<span class="labl">broker fee details:</span>
<span class="valu">AGENCY-SCRUBBED (DRE #LICENCE-SCRUBBED).</span>
</div>
<div class="attr broker_name">
<span class="labl">listed by:</span>
<span class="valu">AGENCY-SCRUBBED, DRE License: LICENCE-SCRUBBED</span>
</div>
<div class="attr rent_period">
<span class="labl">rent period:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;rent_period=3">monthly</a>
</span>
</div>
</div>
<div class="attrgroup">
<div class="attr pets_cat">
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;pets_cat=1">cats are OK - purrr</a>
</span>
</div>
<div class="attr">
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;housing_type=1">apartment</a>
</span>
</div>
<div class="attr pets_dog">
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;pets_dog=1">dogs are OK - wooof</a>
</span>
</div>
<div class="attr">
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;laundry=3">laundry on site</a>
</span>
</div>
<div class="attr">
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=apa&amp;parking=5">street parking</a>
</span>
</div>
</div>
<div class="postinginfos">
<p class="postinginfo">post id: 7966146819</p>
<p class="postinginfo reveal">posted: <time class="date timeago" datetime="2026-09-14T08:50:39-0400">2026-09-14 08:50</time></p>
<p class="postinginfo">
<a class="bestof-link" href="https://post.craigslist.org/flag" title="nominate for best-of-CL">
<span class="bestof-icon">♥ </span><span class="bestof-text">best of</span>
</a> <sup>[<a href="https://www.craigslist.org/about/best-of-craigslist">?</a>]</sup>
</p>
</div>
<div class="viewposting" data-accuracy="22" data-latitude="40.727200" data-longitude="-73.949800" id="map"></div>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 1" onerror="this.onerror = null; this.style.width='650px'; this.style.height='400px'; this.src='/images/no_image.png'" src="https://images.craigslist.org/00r0r_65qy1T3G2J0_07s0ai_600x450.jpg" title="1"/>
<a class="thumb" data-imgid="65qy1T3G2J0_07s0ai" href="https://images.craigslist.org/00r0r_65qy1T3G2J0_07s0ai_600x450.jpg" id="1_thumb_65qy1T3G2J0_07s0ai" title="1"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 1 thumbnail" class="selected" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00r0r_65qy1T3G2J0_07s0ai_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 1 thumbnail" class="selected" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00r0r_65qy1T3G2J0_07s0ai_50x50c.jpg"/>
<a class="thumb" data-imgid="bXZIPjxcVzw_07f0ae" href="https://images.craigslist.org/00k0k_bXZIPjxcVzw_07f0ae_600x450.jpg" id="2_thumb_bXZIPjxcVzw_07f0ae" title="2"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 2 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00k0k_bXZIPjxcVzw_07f0ae_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 2 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00k0k_bXZIPjxcVzw_07f0ae_50x50c.jpg"/>
<a class="thumb" data-imgid="jkPnbYFevY7_06S0a8" href="https://images.craigslist.org/00R0R_jkPnbYFevY7_06S0a8_600x450.jpg" id="3_thumb_jkPnbYFevY7_06S0a8" title="3"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 3 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00R0R_jkPnbYFevY7_06S0a8_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 3 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00R0R_jkPnbYFevY7_06S0a8_50x50c.jpg"/>
<a class="thumb" data-imgid="2MtEmU3Bikz_07k09B" href="https://images.craigslist.org/00d0d_2MtEmU3Bikz_07k09B_600x450.jpg" id="4_thumb_2MtEmU3Bikz_07k09B" title="4"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 4 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00d0d_2MtEmU3Bikz_07k09B_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 4 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00d0d_2MtEmU3Bikz_07k09B_50x50c.jpg"/>
<a class="thumb" data-imgid="kEkuDtxaDcV_07m0ak" href="https://images.craigslist.org/00W0W_kEkuDtxaDcV_07m0ak_600x450.jpg" id="5_thumb_kEkuDtxaDcV_07m0ak" title="5"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 5 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00W0W_kEkuDtxaDcV_07m0ak_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 5 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00W0W_kEkuDtxaDcV_07m0ak_50x50c.jpg"/>
<a class="thumb" data-imgid="1BXP70FVS9A_07c0al" href="https://images.craigslist.org/00C0C_1BXP70FVS9A_07c0al_600x450.jpg" id="6_thumb_1BXP70FVS9A_07c0al" title="6"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 6 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00C0C_1BXP70FVS9A_07c0al_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 6 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00C0C_1BXP70FVS9A_07c0al_50x50c.jpg"/>
<a class="thumb" data-imgid="k2PNS4aQk6U_07U0ar" href="https://images.craigslist.org/01111_k2PNS4aQk6U_07U0ar_600x450.jpg" id="7_thumb_k2PNS4aQk6U_07U0ar" title="7"><img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 7 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_k2PNS4aQk6U_07U0ar_50x50c.jpg"/></a>
<img alt="Boerum Hill Brooklyn Apartment – Bright Home Near Wyckoff St 7 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01111_k2PNS4aQk6U_07U0ar_50x50c.jpg"/>
</body></html>"""

# A Berlin advert: EUR written the German way (1.400), and NO map element at
# all -- so no coordinates, which is what every non-US posting captured looks
# like.
POSTING_BERLIN = r"""<!doctype html><html><head>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script id="ld_breadcrumb_data" type="application/ld+json">
    {"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"position":1,"name":"craigslist: berlin","@type":"ListItem","item":"https://www.craigslist.org/area/berlin"},{"position":2,"@type":"ListItem","name":"for sale","item":"https://www.craigslist.org/search/area/berlin?cat=sss"},{"item":"https://www.craigslist.org/search/area/berlin?cat=pta&purveyor=owner","@type":"ListItem","name":"auto parts - by owner","position":3},{"item":"https://www.craigslist.org/view/d/2025-vw-golf-mk-85-sitze/jffhVDqeR7k7DdiCre4Ps4","@type":"ListItem","name":"2025 VW Golf R MK 8.5 Sitze","position":4}]}
</script>
<script id="ld_posting_data" type="application/ld+json">
    {"description":"Sieh dir an, wie schön sie sind! Warum nicht ein Upgrade vornehmen? ","image":["https://images.craigslist.org/00707_7Lxr9ukAvao_0hq0gN_600x450.jpg","https://images.craigslist.org/00E0E_1uWF7casg9c_0hq0gv_600x450.jpg","https://images.craigslist.org/00X0X_3ZUQ1dXfSVl_0hq0gV_600x450.jpg","https://images.craigslist.org/01414_d86Xudkgp3G_0hq0h1_600x450.jpg"],"name":"2025 VW Golf R MK 8.5 Sitze","@context":"http://schema.org","@type":"Product","offers":{"priceCurrency":"EUR","@type":"Offer","availableAtOrFrom":{"geo":{"latitude":null,"longitude":null,"@type":"GeoCoordinates"},"@type":"Place","address":{"postalCode":"","addressLocality":"","@type":"PostalAddress","streetAddress":"","addressRegion":"","addressCountry":""}},"price":"1400.00"}}
</script>
</head><body>
<span class="postingtitletext">
<span id="titletextonly">2025 VW Golf R MK 8.5 Sitze</span> - <span class="price">€1.400</span> </span>
<section id="postingbody">
<div class="print-information print-qrcode-container">
<p class="print-qrcode-label">QR Code Link to This Post</p>
<div class="print-qrcode" data-location="https://www.craigslist.org/view/d/2025-vw-golf-mk-85-sitze/jffhVDqeR7k7DdiCre4Ps4">
</div>
</div>
Sieh dir an, wie schön sie sind! Warum nicht ein Upgrade vornehmen?<br/>
</section>
<div class="attrgroup">
<div class="attr condition">
<span class="labl">condition:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/berlin?cat=pta&amp;condition=20">like new</a>
</span>
</div>
</div>
<div class="attrgroup">
<div class="attr crypto_currency_ok">
<span class="valu"> <a href="https://www.craigslist.org/search/area/berlin?cat=pta&amp;crypto_currency_ok=1">cryptocurrency ok</a>
</span>
</div>
<div class="attr delivery_available">
<span class="valu"> <a href="https://www.craigslist.org/search/area/berlin?cat=pta&amp;delivery_available=1">delivery available</a>
</span>
</div>
</div>
<p class="attrgroup">
<span class="otherpostings">
<a href="https://www.craigslist.org/search/area/berlin?cat=sss&amp;userpostingid=7955721762">
          more ads by this seller
        </a>
</span>
</p>
<div class="postinginfos">
<p class="postinginfo">post id: 7955721762</p>
<p class="postinginfo reveal">posted: <time class="date timeago" datetime="2026-08-25T13:10:53+0200">2026-08-25 13:10</time></p>
<p class="postinginfo reveal">updated: <time class="date timeago" datetime="2026-09-12T18:49:29+0200">2026-09-12 18:49</time></p>
<p class="postinginfo">
<a class="bestof-link" href="https://post.craigslist.org/flag" title="nominate for best-of-CL">
<span class="bestof-icon">♥ </span><span class="bestof-text">best of</span>
</a> <sup>[<a href="https://www.craigslist.org/about/best-of-craigslist">?</a>]</sup>
</p>
</div>
<img alt="2025 VW Golf R MK 8.5 Sitze 1" onerror="this.onerror = null; this.style.width='650px'; this.style.height='400px'; this.src='/images/no_image.png'" src="https://images.craigslist.org/00707_7Lxr9ukAvao_0hq0gN_600x450.jpg" title="1"/>
<a class="thumb" data-imgid="7Lxr9ukAvao_0hq0gN" href="https://images.craigslist.org/00707_7Lxr9ukAvao_0hq0gN_600x450.jpg" id="1_thumb_7Lxr9ukAvao_0hq0gN" title="1"><img alt="2025 VW Golf R MK 8.5 Sitze 1 thumbnail" class="selected" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00707_7Lxr9ukAvao_0hq0gN_50x50c.jpg"/></a>
<img alt="2025 VW Golf R MK 8.5 Sitze 1 thumbnail" class="selected" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00707_7Lxr9ukAvao_0hq0gN_50x50c.jpg"/>
<a class="thumb" data-imgid="1uWF7casg9c_0hq0gv" href="https://images.craigslist.org/00E0E_1uWF7casg9c_0hq0gv_600x450.jpg" id="2_thumb_1uWF7casg9c_0hq0gv" title="2"><img alt="2025 VW Golf R MK 8.5 Sitze 2 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00E0E_1uWF7casg9c_0hq0gv_50x50c.jpg"/></a>
<img alt="2025 VW Golf R MK 8.5 Sitze 2 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00E0E_1uWF7casg9c_0hq0gv_50x50c.jpg"/>
<a class="thumb" data-imgid="3ZUQ1dXfSVl_0hq0gV" href="https://images.craigslist.org/00X0X_3ZUQ1dXfSVl_0hq0gV_600x450.jpg" id="3_thumb_3ZUQ1dXfSVl_0hq0gV" title="3"><img alt="2025 VW Golf R MK 8.5 Sitze 3 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00X0X_3ZUQ1dXfSVl_0hq0gV_50x50c.jpg"/></a>
<img alt="2025 VW Golf R MK 8.5 Sitze 3 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/00X0X_3ZUQ1dXfSVl_0hq0gV_50x50c.jpg"/>
<a class="thumb" data-imgid="d86Xudkgp3G_0hq0h1" href="https://images.craigslist.org/01414_d86Xudkgp3G_0hq0h1_600x450.jpg" id="4_thumb_d86Xudkgp3G_0hq0h1" title="4"><img alt="2025 VW Golf R MK 8.5 Sitze 4 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01414_d86Xudkgp3G_0hq0h1_50x50c.jpg"/></a>
<img alt="2025 VW Golf R MK 8.5 Sitze 4 thumbnail" onerror="this.onerror = null; this.src='/images/no_image.png'" src="https://images.craigslist.org/01414_d86Xudkgp3G_0hq0h1_50x50c.jpg"/>
</body></html>"""

# A jobs advert: no price, no images, and a category-shaped attribute bag
# (compensation, employment type, experience level, job title).
POSTING_JOBS = r"""<!doctype html><html><head>
<script type="text/javascript" src="https://www.craigslist.org/static/d/x.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/y.js"></script>
<script type="text/javascript" src="https://www.craigslist.org/static/d/z.js"></script>
<script id="ld_breadcrumb_data" type="application/ld+json">
    {"itemListElement":[{"@type":"ListItem","item":"https://www.craigslist.org/area/newyork","name":"craigslist: new york","position":1},{"item":"https://www.craigslist.org/subarea/stn","@type":"ListItem","name":"staten island","position":2},{"position":3,"item":"https://www.craigslist.org/search/subarea/stn?cat=jjj","@type":"ListItem","name":"jobs"},{"name":"skilled trades","@type":"ListItem","item":"https://www.craigslist.org/search/subarea/stn?cat=trd","position":4},{"name":"Roofer, Sider and board ups","item":"https://www.craigslist.org/view/d/staten-island-roofer-sider-and-board-ups/rfSB7va5Vn4SdEBpSiTT9g","@type":"ListItem","position":5}],"@type":"BreadcrumbList","@context":"https://schema.org"}
</script>
<script id="ld_posting_data" type="application/ld+json">
    {"datePosted":"2026-09-14T12:51:25+0000","@type":"JobPosting","jobLocation":{"geo":{"longitude":"-74.229783","@type":"GeoCoordinates","latitude":"40.545969"},"address":{"addressRegion":"NY","@type":"PostalAddress","postalCode":"10309","addressLocality":"Staten Island","addressCountry":"US","streetAddress":"220 Industrial Loop"},"@type":"Place"},"@context":"http://schema.org","validThrough":"2026-10-14","employmentType":"FULL_TIME","hiringOrganization":{"name":"Rainbow Restoration","@type":"Organization"},"description":"Looking for person who knows Roofing, siding and board ups. Other home improvement knowledge a plus.<br>\n","title":"Roofer"}
</script>
</head><body>
<span class="postingtitletext">
<span id="titletextonly">Roofer, Sider and board ups</span><span> (Staten Island)</span> </span>
<section id="postingbody">
<div class="print-information print-qrcode-container">
<p class="print-qrcode-label">QR Code Link to This Post</p>
<div class="print-qrcode" data-location="https://www.craigslist.org/view/d/staten-island-roofer-sider-and-board-ups/rfSB7va5Vn4SdEBpSiTT9g">
</div>
</div>
Looking for person who knows Roofing, siding and board ups. Other home improvement knowledge a plus.<br/>
</section>
<div class="attrgroup">
<div class="attr remuneration">
<span class="labl">compensation:</span>
<span class="valu">Compensation based on knowledge and experience</span>
</div>
<div class="attr employment_type">
<span class="labl">employment type:</span>
<span class="valu"> <a href="https://www.craigslist.org/search/area/newyork?cat=trd&amp;employment_type=1">full-time</a>
</span>
</div>
<div class="attr experience_level">
<span class="labl">experience level:</span>
<span class="valu">mid level</span>
</div>
<div class="attr job_title">
<span class="labl">job title:</span>
<span class="valu">Roofer</span>
</div>
</div>
<div class="postinginfos">
<p class="postinginfo">post id: 7966147143</p>
<p class="postinginfo reveal">posted: <time class="date timeago" datetime="2026-09-14T08:51:25-0400">2026-09-14 08:51</time></p>
<p class="postinginfo">
<a class="bestof-link" href="https://post.craigslist.org/flag" title="nominate for best-of-CL">
<span class="bestof-icon">♥ </span><span class="bestof-text">best of</span>
</a> <sup>[<a href="https://www.craigslist.org/about/best-of-craigslist">?</a>]</sup>
</p>
</div>
<div class="viewposting" data-accuracy="10" data-latitude="40.545969" data-longitude="-74.229783" id="map"></div>
</body></html>"""

# A query that matched nothing. The site's own results container is PRESENT
# and holds only its "see also" item -- there is no "no results" sentence in
# any locale, so the container's emptiness is the signal.
EMPTY_RESULTS = r"""<!doctype html><html><head><title>new york for sale - craigslist</title>
<script src="https://www.craigslist.org/static/d/x.js"></script>
<script src="https://www.craigslist.org/static/d/y.js"></script>
<script src="https://www.craigslist.org/static/d/z.js"></script>
</head><body class="no-js">
<div class="cl-static-header">
<a href="/">craigslist</a>
<h1>For Sale "qqzzxxnosuchthing" in New York City</h1>
</div>
<ol class="cl-static-search-results">
<li class="cl-static-hub-links">
<div>see also</div>
</li>
</ol>
</body></html>"""

# The page caught between its two views: the application has emptied the
# results container and has not yet painted its grid. Indistinguishable from
# EMPTY_RESULTS by the container alone -- the JSON-LD is what separates them,
# and getting this wrong made a live run report 0 rows and exit 4 on a page
# holding 329 structured items.
MID_HANDOVER = r"""<!doctype html><html><head><title>new york for sale - craigslist</title>
<script src="https://www.craigslist.org/static/d/x.js"></script>
<script src="https://www.craigslist.org/static/d/y.js"></script>
<script src="https://www.craigslist.org/static/d/z.js"></script>
<script type="application/ld+json" id="ld_searchpage_results">{"@context": "https://schema.org", "itemListElement": [{"item": {"offers": {"availableAtOrFrom": {"address": {"streetAddress": "", "addressRegion": "NY", "@type": "PostalAddress", "addressLocality": "Brooklyn", "postalCode": "", "addressCountry": ""}, "@type": "Place", "geo": {"latitude": 40.6258560756741, "longitude": -73.9563797750841, "@type": "GeoCoordinates"}}, "price": "30.00", "priceCurrency": "USD", "@type": "Offer"}, "image": ["https://images.craigslist.org/00P0P_h57ibSyQVav_0lM0t2_600x450.jpg", "https://images.craigslist.org/00h0h_lUDTglQd80y_0lM0t2_600x450.jpg", "https://images.craigslist.org/00Z0Z_aA0YTjBr2d_0lM0t2_600x450.jpg"], "description": "", "@type": "Product", "@context": "http://schema.org", "name": "Portable Bariatric Bedside Toilet Seat Elderly Disabled Commode Comode"}, "@type": "ListItem", "position": "0"}, {"@type": "ListItem", "position": "1", "item": {"@context": "http://schema.org", "name": "Cosco Kids Infant Toddler Play Feeding Table High Folding Chair Seat", "@type": "Product", "image": ["https://images.craigslist.org/00y0y_hywRjSaeh5A_0t20CI_600x450.jpg", "https://images.craigslist.org/00i0i_44Wz162xPxV_0t20CI_600x450.jpg", "https://images.craigslist.org/00Y0Y_iQ7sArxIOKY_0t20CI_600x450.jpg", "https://images.craigslist.org/00T0T_5906nOLmmBY_0t20CI_600x450.jpg", "https://images.craigslist.org/00F0F_95Co7CD7jMs_0t20CI_600x450.jpg"], "description": "", "offers": {"availableAtOrFrom": {"geo": {"longitude": -73.9563935260711, "latitude": 40.6257294520014, "@type": "GeoCoordinates"}, "address": {"@type": "PostalAddress", "addressRegion": "NY", "streetAddress": "", "addressCountry": "", "addressLocality": "Brooklyn", "postalCode": ""}, "@type": "Place"}, "price": "30.00", "@type": "Offer", "priceCurrency": "USD"}}}, {"@type": "ListItem", "position": "2", "item": {"@type": "Product", "image": ["https://images.craigslist.org/00T0T_2mcBdlVvld3_0iS0iL_600x450.jpg", "https://images.craigslist.org/00W0W_4Fn6j0lvKi_0iS0iJ_600x450.jpg"], "description": "", "offers": {"@type": "Offer", "priceCurrency": "USD", "price": "30.00", "availableAtOrFrom": {"address": {"postalCode": "", "addressLocality": "Brooklyn", "addressCountry": "", "streetAddress": "", "addressRegion": "NY", "@type": "PostalAddress"}, "@type": "Place", "geo": {"@type": "GeoCoordinates", "latitude": 40.6257294520014, "longitude": -73.9563935260711}}}, "name": "New Compact Portable External CD DVD RW Disk PC Computer USB Drive", "@context": "http://schema.org"}}], "@type": "ItemList"}</script>
</head><body>
<ol class="cl-static-search-results">
</ol>
</body></html>"""

# Six cards from the hydrated grid, with the site's own "1 - 6 of 10,000+"
# counter. This is what a walking run harvests, and what a run over a remote
# profile falls back to when the served list has already been removed.
RENDERED_GRID = r"""<!doctype html><html><head><title>new york for sale - craigslist</title>
<script src="https://www.craigslist.org/static/d/x.js"></script>
<script src="https://www.craigslist.org/static/d/y.js"></script>
<script src="https://www.craigslist.org/static/d/z.js"></script>
</head><body>
<div class="visible-counts"><span>1 - 9 </span><span> of 10,000+</span></div>
<div class="cl-search-result cl-search-view-mode-gallery" data-pid="7930913089" title="2003 Raleigh C40 Women's Hybrid Bicycle"><div class="gallery-card"><div class="cl-gallery"><div class="gallery-inner"><a class="main" href="https://www.craigslist.org/view/d/jericho-2003-raleigh-c40-womens-hybrid/jeZ6NuhyjSSSWU17z25Tts"><div class="swipe" style="visibility: visible;"><div class="swipe-wrap" style="width: 4728px;"><div data-index="0" style="width: 394px; left: 0px; transition-duration: 0ms; transform: translateX(0px);"><span class="loading icom-"></span><img alt="2003 Raleigh C40 Women's Hybrid Bicycle 1" data-image-index="0" src="https://images.craigslist.org/d/7930913089/00505_4WjMLWkwJ8Q_0ak07K_300x300.jpg"/></div><div data-index="1" style="width: 394px; left: -394px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="2" style="width: 394px; left: -788px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="3" style="width: 394px; left: -1182px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="4" style="width: 394px; left: -1576px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="5" style="width: 394px; left: -1970px; transition-duration: 0ms; transform: translateX(-394px);"></div></div></div><div class="slider-back-arrow icom-"></div><div class="slider-forward-arrow icom-"></div></a></div><div class="dots"><span class="dot selected">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span></div><img class="cl-gallery-image-preload" src="https://images.craigslist.org/d/7930913089/00505_4WjMLWkwJ8Q_0ak07K_600x450.jpg"/></div><a class="cl-app-anchor cl-search-anchor text-only posting-title" href="https://www.craigslist.org/view/d/jericho-2003-raleigh-c40-womens-hybrid/jeZ6NuhyjSSSWU17z25Tts" tabindex="0"><span class="label">2003 Raleigh C40 Women's Hybrid Bicycle</span></a><div class="meta-line"><button class="bd-button cl-favorite-button icon-only" tabindex="0" title="add to favorites list" type="button"><span class="icon icom-"></span></button><div class="meta"><span class="result-posted-date">&lt;1hr ago</span><span class="separator"></span><span class="result-location">Jericho</span></div><button class="bd-button cl-banish-button icon-only" tabindex="0" title="hide posting" type="button"><span class="icon icom-"></span></button></div><span class="priceinfo">$180</span></div></div>
<div class="cl-search-result cl-search-view-mode-gallery" data-pid="7931097354" title="SSL sigma mixer with mogami breakout cable"><div class="gallery-card"><div class="cl-gallery empty"><div class="gallery-inner"><a class="main" href="https://www.craigslist.org/view/d/new-york-ssl-sigma-mixer-with-mogami/9MwNFevg5NmwJdtU2ArjCJ"><div class="icom-"></div><p>no image</p><img alt="" src="https://www.craigslist.org/images/d/7931097354/empty.png"/></a></div></div><a class="cl-app-anchor cl-search-anchor text-only posting-title" href="https://www.craigslist.org/view/d/new-york-ssl-sigma-mixer-with-mogami/9MwNFevg5NmwJdtU2ArjCJ" tabindex="0"><span class="label">SSL sigma mixer with mogami breakout cable</span></a><div class="meta-line"><button class="bd-button cl-favorite-button icon-only" tabindex="0" title="add to favorites list" type="button"><span class="icon icom-"></span></button><div class="meta"><span class="result-posted-date">&lt;1hr ago</span><span class="separator"></span><span class="result-location">New York</span></div><button class="bd-button cl-banish-button icon-only" tabindex="0" title="hide posting" type="button"><span class="icon icom-"></span></button></div><span class="priceinfo">$2,350</span></div></div>
<div class="cl-search-result cl-search-view-mode-gallery" data-pid="7929969215" title='Fender Pro Reverb 2-Channel 40-Watt 2x12" Guitar Combo 1972 Silverface'><div class="gallery-card"><div class="cl-gallery"><div class="gallery-inner"><a class="main" href="https://www.craigslist.org/view/d/new-york-fender-pro-reverb-channel-40/ktvj9u2SWPeAXR3y6puFTq"><div class="swipe" style="visibility: visible;"><div class="swipe-wrap" style="width: 3152px;"><div data-index="0" style="width: 394px; left: 0px; transition-duration: 0ms; transform: translateX(0px);"><span class="loading icom-"></span><img alt='Fender Pro Reverb 2-Channel 40-Watt 2x12" Guitar Combo 1972 Silverface 1' data-image-index="0" src="https://images.craigslist.org/d/7929969215/00d0d_l5NIszuPYab_0t20CI_300x300.jpg"/></div><div data-index="1" style="width: 394px; left: -394px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-cloned="true" data-index="2" style="width: 394px; left: -788px; transition-duration: 0ms; transform: translateX(394px);"><span class="loading icom-"></span><img alt='Fender Pro Reverb 2-Channel 40-Watt 2x12" Guitar Combo 1972 Silverface 1' data-image-index="0" data-loading="true" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAAXNSR0IArs4c6QAAAAtJREFUGFdjYAACAAAFAAGq1chRAAAAAElFTkSuQmCC"/></div><div data-cloned="true" data-index="3" style="width: 394px; left: -1182px; transition-duration: 0ms; transform: translateX(-394px);"></div></div></div><div class="slider-back-arrow icom-"></div><div class="slider-forward-arrow icom-"></div></a></div><div class="dots"><span class="dot selected">•</span><span class="dot">•</span></div></div><a class="cl-app-anchor cl-search-anchor text-only posting-title" href="https://www.craigslist.org/view/d/new-york-fender-pro-reverb-channel-40/ktvj9u2SWPeAXR3y6puFTq" tabindex="0"><span class="label">Fender Pro Reverb 2-Channel 40-Watt 2x12" Guitar Combo 1972 Silverface</span></a><div class="meta-line"><button class="bd-button cl-favorite-button icon-only" tabindex="0" title="add to favorites list" type="button"><span class="icon icom-"></span></button><div class="meta"><span class="result-posted-date">&lt;1hr ago</span><span class="separator"></span><span class="result-location">New York</span></div><button class="bd-button cl-banish-button icon-only" tabindex="0" title="hide posting" type="button"><span class="icon icom-"></span></button></div><span class="priceinfo">$950</span></div></div>
<div class="cl-search-result cl-search-view-mode-gallery" data-pid="7966164516" title="Fender Rumble 25 Bass Amp — Near Mint, Living room Use Only"><div class="gallery-card"><div class="cl-gallery"><div class="gallery-inner"><a class="main" href="https://www.craigslist.org/view/d/new-york-fender-rumble-25-bass-amp-near/cVho9orp2upYDsMxZUKDJC"><div class="swipe" style="visibility: visible;"><div class="swipe-wrap" style="width: 8668px;"><div data-index="0" style="width: 394px; left: 0px; transition-duration: 0ms; transform: translateX(0px);"><span class="loading icom-"></span><img alt="Fender Rumble 25 Bass Amp — Near Mint, Living room Use Only 1" data-image-index="0" src="https://images.craigslist.org/d/7966164516/00202_heNgsJh06I4_0t20CI_300x300.jpg"/></div><div data-index="1" style="width: 394px; left: -394px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="2" style="width: 394px; left: -788px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="3" style="width: 394px; left: -1182px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="4" style="width: 394px; left: -1576px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="5" style="width: 394px; left: -1970px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="6" style="width: 394px; left: -2364px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="7" style="width: 394px; left: -2758px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="8" style="width: 394px; left: -3152px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="9" style="width: 394px; left: -3546px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="10" style="width: 394px; left: -3940px; transition-duration: 0ms; transform: translateX(-394px);"></div></div></div><div class="slider-back-arrow icom-"></div><div class="slider-forward-arrow icom-"></div></a></div><div class="dots"><span class="dot selected">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span></div></div><a class="cl-app-anchor cl-search-anchor text-only posting-title" href="https://www.craigslist.org/view/d/new-york-fender-rumble-25-bass-amp-near/cVho9orp2upYDsMxZUKDJC" tabindex="0"><span class="label">Fender Rumble 25 Bass Amp — Near Mint, Living room Use Only</span></a><div class="meta-line"><button class="bd-button cl-favorite-button icon-only" tabindex="0" title="add to favorites list" type="button"><span class="icon icom-"></span></button><div class="meta"><span class="result-posted-date">&lt;1hr ago</span><span class="separator"></span><span class="result-location">Greenwich Village</span></div><button class="bd-button cl-banish-button icon-only" tabindex="0" title="hide posting" type="button"><span class="icon icom-"></span></button></div><span class="priceinfo">$140</span></div></div>
<div class="cl-search-result cl-search-view-mode-gallery" data-pid="7956106251" title='AIWA TV-C1300 13" color CRT monitor LIKE NEW IN BOX'><div class="gallery-card"><div class="cl-gallery"><div class="gallery-inner"><a class="main" href="https://www.craigslist.org/view/d/brooklyn-aiwa-tv-color-crt-monitor-like/7AH35qA8QPgC5Fudhv7HMm"><div class="swipe" style="visibility: visible;"><div class="swipe-wrap" style="width: 7092px;"><div data-index="0" style="width: 394px; left: 0px; transition-duration: 0ms; transform: translateX(0px);"><span class="loading icom-"></span><img alt='AIWA TV-C1300 13" color CRT monitor LIKE NEW IN BOX 1' data-image-index="0" src="https://images.craigslist.org/d/7956106251/01717_gHUdoged6rb_0ul0t2_300x300.jpg"/></div><div data-index="1" style="width: 394px; left: -394px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="2" style="width: 394px; left: -788px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="3" style="width: 394px; left: -1182px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="4" style="width: 394px; left: -1576px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="5" style="width: 394px; left: -1970px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="6" style="width: 394px; left: -2364px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="7" style="width: 394px; left: -2758px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="8" style="width: 394px; left: -3152px; transition-duration: 0ms; transform: translateX(-394px);"></div></div></div><div class="slider-back-arrow icom-"></div><div class="slider-forward-arrow icom-"></div></a></div><div class="dots"><span class="dot selected">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span></div><img class="cl-gallery-image-preload" src="https://images.craigslist.org/d/7956106251/01717_gHUdoged6rb_0ul0t2_600x450.jpg"/></div><a class="cl-app-anchor cl-search-anchor text-only posting-title" href="https://www.craigslist.org/view/d/brooklyn-aiwa-tv-color-crt-monitor-like/7AH35qA8QPgC5Fudhv7HMm" tabindex="0"><span class="label">AIWA TV-C1300 13" color CRT monitor LIKE NEW IN BOX</span></a><div class="meta-line"><button class="bd-button cl-favorite-button icon-only" tabindex="0" title="add to favorites list" type="button"><span class="icon icom-"></span></button><div class="meta"><span class="result-posted-date">&lt;1hr ago</span><span class="separator"></span><span class="result-location">Ridgewood</span></div><button class="bd-button cl-banish-button icon-only" tabindex="0" title="hide posting" type="button"><span class="icon icom-"></span></button></div><span class="priceinfo">$400</span></div></div>
<div class="cl-search-result cl-search-view-mode-gallery" data-pid="7942765525" title="Recording Studio Music Gear SALE - Eurorack, pedals, Stylophone"><div class="gallery-card"><div class="cl-gallery"><div class="gallery-inner"><a class="main" href="https://www.craigslist.org/view/d/brooklyn-recording-studio-music-gear/2rADg9AyoafGtp9yaMBir6"><div class="swipe" style="visibility: visible;"><div class="swipe-wrap" style="width: 4728px;"><div data-index="0" style="width: 394px; left: 0px; transition-duration: 0ms; transform: translateX(0px);"><span class="loading icom-"></span><img alt="Recording Studio Music Gear SALE - Eurorack, pedals, Stylophone 1" data-image-index="0" src="https://images.craigslist.org/d/7942765525/00505_eEV5OQWJB8l_0mW0t2_300x300.jpg"/></div><div data-index="1" style="width: 394px; left: -394px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="2" style="width: 394px; left: -788px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="3" style="width: 394px; left: -1182px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="4" style="width: 394px; left: -1576px; transition-duration: 0ms; transform: translateX(394px);"></div><div data-index="5" style="width: 394px; left: -1970px; transition-duration: 0ms; transform: translateX(-394px);"></div></div></div><div class="slider-back-arrow icom-"></div><div class="slider-forward-arrow icom-"></div></a></div><div class="dots"><span class="dot selected">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span></div></div><a class="cl-app-anchor cl-search-anchor text-only posting-title" href="https://www.craigslist.org/view/d/brooklyn-recording-studio-music-gear/2rADg9AyoafGtp9yaMBir6" tabindex="0"><span class="label">Recording Studio Music Gear SALE - Eurorack, pedals, Stylophone</span></a><div class="meta-line"><button class="bd-button cl-favorite-button icon-only" tabindex="0" title="add to favorites list" type="button"><span class="icon icom-"></span></button><div class="meta"><span class="result-posted-date">&lt;1hr ago</span><span class="separator"></span><span class="result-location">Ridgewood</span></div><button class="bd-button cl-banish-button icon-only" tabindex="0" title="hide posting" type="button"><span class="icon icom-"></span></button></div><span class="priceinfo">$1</span></div></div>
</body></html>"""

# Chromium's OWN network-error page, captured through a dead proxy.
#
# It carries `<title>www.craigslist.org</title>` -- the site's hostname -- so
# a title check calls it a real page, and it carries no vendor marker of any
# kind. The only thing that answers correctly is "was this built out of the
# site's own assets?", which is why detection here is positive rather than
# marker-based.
CHROMIUM_ERROR = r"""<!doctype html><html><head><title>www.craigslist.org</title></head><body>
<div class="interstitial-wrapper" id="main-frame-error">
<div id="main-content">
<div class="icon icon-generic"></div>
<div id="main-message">
<h1>
<span>No Internet</span>
</h1>
<!--?lit$912151161$-->
<p>There is something wrong with the proxy server or the address is incorrect.</p>
<!--?lit$912151161$-->
<div id="suggestions-list">
<p><!--?lit$912151161$-->Try:</p>
<ul class="">
<!--?lit$912151161$--><!-- -->
<li>Contacting the system admin</li>
<!-- --><!-- -->
<li><a href="#buttons" onclick="toggleHelpBox()">Checking the proxy address</a></li>
<!-- -->
</ul>
</div>
<div class="error-code"><!--?lit$912151161$-->ERR_PROXY_CONNECTION_FAILED</div>
<!--?lit$912151161$-->
</div>
</div>
<div class="nav-wrapper suggested-right" id="buttons">
<div hidden="" id="control-buttons">
<!--?lit$912151161$-->
<!--?lit$912151161$-->
</div>
<!--?lit$912151161$-->
<button class="secondary-button text-button small-link singular" id="details-button">
<!--?lit$912151161$-->Details
          </button>
</div>
<!--?lit$912151161$-->
<div id="details">
<!--?lit$912151161$--><!-- -->
<div class="suggestions">
<div class="suggestion-header">If you use a proxy server…</div>
<div class="suggestion-body">Go to Applications &gt; System Settings &gt; Network, select the
          active network, click the Details… button and deselect any proxies
          that may have been selected.</div>
</div>
<!-- -->
</div>
</div>
</body></html>"""

# ==========================================================================
# URLs
# ==========================================================================

def test_urls():
    section("URLs")
    ok = True

    # The id alphabet is base64url and the length is exact. A first draft used
    # [A-Za-z0-9] and silently dropped 54 of 350 entries on a cars listing,
    # reporting success -- so both characters get their own check.
    ok &= check("sku from a posting URL",
                sku_from_url(POSTING_URL) == "kfvqiXscPUVb9PyC34B4YN")
    ok &= check("a sku containing '-' is not dropped",
                sku_from_url("https://www.craigslist.org/view/d/x/"
                             "pHzcejmw8RGi-qIn1GerIg") == "pHzcejmw8RGi-qIn1GerIg")
    ok &= check("a sku containing '_' is not dropped",
                sku_from_url("https://www.craigslist.org/view/d/x/"
                             "vju7Iziw8RG_DOMqaDoTwg") == "vju7Iziw8RG_DOMqaDoTwg")
    ok &= check("a 21-character token is not a sku",
                sku_from_url("https://www.craigslist.org/view/d/x/"
                             "vju7Iziw8RG_DOMqaDoTw") is None)
    ok &= check("a listing URL has no sku", sku_from_url(LISTING_URL) is None)
    ok &= check("posting slug", posting_slug(POSTING_URL) ==
                "brooklyn-for-sale2021-toyota-sequoia")

    ok &= check("area from a search URL", area_from_url(LISTING_URL) == "newyork")
    ok &= check("area from a subarea URL",
                area_from_url("https://www.craigslist.org/search/subarea/lgi?cat=sss") == "lgi")
    ok &= check("area kind is named",
                (area_kind(LISTING_URL),
                 area_kind("https://www.craigslist.org/search/subarea/lgi?cat=sss"))
                == ("area", "subarea"))

    ok &= check("category code and name",
                (category_code(LISTING_URL), category_from_url(LISTING_URL))
                == ("sss", "for sale"))
    # An unmeasured code passes through verbatim rather than being dropped or
    # named by guesswork: the site has dozens and inventing names for them
    # would put a guess in a data column.
    ok &= check("an unmeasured cat code passes through verbatim",
                category_from_url("https://www.craigslist.org/search/area/newyork?cat=zzz")
                == "zzz")
    ok &= check("no cat parameter means no category",
                category_from_url("https://www.craigslist.org/search/area/newyork") is None)

    ok &= check("listing kinds",
                (listing_kind(LISTING_URL), listing_kind(POSTING_URL),
                 listing_kind("https://www.craigslist.org/area/newyork"))
                == ("search", "posting", "hub"))

    ok &= check("the canonical host is supported",
                is_supported_host(LISTING_URL) and site_host(LISTING_URL) == CANONICAL_HOST)
    # The legacy per-city subdomains 301 to the canonical host. Accepted
    # rather than refused: a user with one in a script should not be told it
    # is not a Craigslist URL.
    ok &= check("a legacy city subdomain is accepted",
                is_supported_host("https://newyork.craigslist.org/search/sss"))
    ok &= check("another site is refused, with a reason",
                not is_supported_host("https://www.gumtree.com/search")
                and "not a Craigslist hostname" in
                (unsupported_reason("https://www.gumtree.com/search") or ""))
    ok &= check("the posting application is refused by name",
                "posting/account application" in
                (unsupported_reason("https://post.craigslist.org/c/nyc") or ""))

    ok &= check("tracking parameters are stripped, real ones kept",
                strip_tracking("https://www.craigslist.org/search/area/newyork"
                               "?cat=sss&lang=en&min_price=25#search=2~gallery~0")
                == "https://www.craigslist.org/search/area/newyork?cat=sss&min_price=25")
    return ok


# ==========================================================================
# Pagination -- there is none, and that is the check
# ==========================================================================

def test_pagination():
    section("Pagination")
    ok = True
    # Measured: ?page=2 and ?s=296 return a BYTE-IDENTICAL response. The
    # family's usual "reconstruct ?page=N" layer would therefore re-fetch the
    # first batch, find no new sku, and report a COMPLETE run holding a
    # fraction of the data -- the bug this family shipped once before.
    ok &= check("no Craigslist listing paginates by URL",
                not paginates_by_url(LISTING_URL)
                and not paginates_by_url(JOBS_URL)
                and not paginates_by_url(POSTING_URL))
    ok &= check("page_url refuses to invent an address",
                page_url(LISTING_URL, 2) is None and page_url(LISTING_URL, 99) is None)
    ok &= check("pagination_is_addressable is False",
                not page_flow.pagination_is_addressable(LISTING_URL))
    ok &= check("no next-page candidates are ever offered",
                page_flow.next_page_candidates(LISTING_URL, ["/search/area/newyork?cat=sss&page=2"])
                == [])
    ok &= check("the next-page selector is empty, so nothing greps for one",
                page_flow.next_page_selector(1) == "")
    ok &= check("concurrency is capped at 1",
                page_flow.concurrency_limit(LISTING_URL) == 1)
    refusal = page_flow.concurrency_refusal(LISTING_URL)
    ok &= check("the refusal names ?page= and ?s= rather than saying 'not supported'",
                "?page=" in refusal and "?s=" in refusal)
    ok &= check("the result cap is the site's own, not a guess", RESULT_CAP == 10000)
    return ok


# ==========================================================================
# Prices
# ==========================================================================

def test_prices():
    section("Prices")
    ok = True
    cases = [
        # Every grouping convention this site actually serves, one per locale.
        ("$70", [70.0]),
        ("$1,234", [1234.0]),                 # three trailing digits: a grouping
        ("$4,100", [4100.0]),
        ("¥150,000", [150000.0]),
        ("€150", [150.0]),
        ("€1.400", [1400.0]),                 # German dot grouping
        ("$0", [0.0]),                        # the site's own "free"
        ("$2,500,013,000", [2500013000.0]),   # the site's own published nonsense
        ("$1,234.56", [1234.56]),
        ("$249 $79", [249.0, 79.0]),          # reading order preserved
        ("", []),
        ("no price here", []),
    ]
    for text, want in cases:
        got = prices_in(text)
        ok &= check("prices_in(%r) == %r" % (text, want), got == want)

    # Percentages come out BEFORE prices are matched. A rejected match has
    # still consumed the currency symbol, so filtering afterwards loses the
    # real price with it.
    ok &= check("a percentage badge does not become a price",
                prices_in("-16% $249") == [249.0])
    ok &= check("a percent-before-number badge does not steal the symbol",
                prices_in("-%10,34 $25.999") == [25999.0])
    return ok


# ==========================================================================
# Listing parsing -- VALUES, on real captures
# ==========================================================================

def test_listing_values():
    section("Listing parsing: values")
    ok = True
    rows = parse_products(LISTING_NEWYORK, LISTING_URL, page=1)
    ok &= check("every static entry became a row", len(rows) == 14)
    ok &= check("every sku is present and unique",
                len({r.sku for r in rows}) == 14 and all(r.sku for r in rows))
    ok &= check("page and position are unique together",
                len({(r.page, r.position) for r in rows}) == 14)
    ok &= check("position starts at 1 and runs in the site's order",
                [r.position for r in rows] == list(range(1, 15)))

    first = rows[0]
    ok &= check("first row's sku", first.sku == "vAjGnkwp5v35FdiHDf8H9X")
    ok &= check("first row's title",
                first.title == "Plates Edwin Knowles")
    ok &= check("first row's price and currency",
                (first.price, first.currency) == (30000.0, "USD"))
    ok &= check("first row's price came from structured data",
                first.price_source == "jsonld")
    ok &= check("first row's coordinates are the page's own",
                (round(first.latitude, 4), round(first.longitude, 4))
                == (40.7817, -73.8317))
    ok &= check("first row's region", first.region == "NY")
    ok &= check("area comes from the URL, not the page", first.area == "newyork")
    ok &= check("source is the family's column, not the area",
                first.source == SOURCE_DEFAULT == "craigslist.org")

    # The columns this site does not publish. Null on EVERY row, and that is
    # a property of Craigslist rather than a parsing failure -- asserted so
    # nobody quietly fills one with a guess.
    for column in ("brand", "original_price", "discount_pct", "rating",
                   "review_count", "in_stock"):
        ok &= check("%s is null on every row, as the site publishes none" % column,
                    all(getattr(r, column) is None for r in rows))
    return ok


def test_subsequence_alignment():
    section("Listing parsing: the JSON-LD subsequence")
    ok = True
    rows = parse_products(LISTING_NEWYORK, LISTING_URL, page=1)
    enriched = [r for r in rows if r.price_source == "jsonld"]
    plain = [r for r in rows if r.price_source == "static"]
    # 14 entries, 13 published, and the one it skips is at index 11 -- with
    # rows AFTER it, which is what makes the next check mean anything.
    ok &= check("the structured data covers 13 of the 14 entries",
                (len(enriched), len(plain)) == (13, 1))
    ok &= check("the skipped entry still has its own printed price",
                plain[0].price is not None)
    ok &= check("the skipped entry has no coordinates to borrow",
                plain[0].latitude is None and plain[0].longitude is None)

    # THE failure this alignment exists to prevent. If the rows were matched
    # by position instead, the entries after the skip would each carry the
    # NEXT advert's price and coordinates -- a row wearing its neighbour's
    # data, which is worse than a row with none.
    skip_index = rows.index(plain[0])
    ok &= check("the skip is not the last entry, so there is something after it",
                skip_index < len(rows) - 1)
    after = rows[skip_index + 1]
    ok &= check("an entry after the skip keeps its OWN price and coordinates",
                after.price_source == "jsonld" and after.latitude is not None)
    ok &= check("and it is not the skipped entry's neighbour's data",
                after.sku != plain[0].sku)

    # When the two views cannot be reconciled the enrichment is dropped
    # wholesale rather than guessed at.
    broken = LISTING_NEWYORK.replace("Plates Edwin Knowles", "Something Else", 1)
    broken_rows = parse_products(broken, LISTING_URL, page=1)
    ok &= check("a misaligned page still yields every row",
                len(broken_rows) == 14)
    ok &= check("a misaligned page carries NO structured enrichment at all",
                all(r.price_source != "jsonld" for r in broken_rows)
                and all(r.latitude is None for r in broken_rows))
    ok &= check("a misaligned page still carries its printed prices",
                sum(1 for r in broken_rows if r.price is not None) >= 13)
    return ok


def test_no_structured_data():
    section("Listing parsing: categories with no structured data")
    ok = True
    rows = parse_products(LISTING_JOBS, JOBS_URL, page=1)
    ok &= check("a jobs listing still yields every row", len(rows) == 6)
    ok &= check("no row claims a structured price",
                all(r.price_source != "jsonld" for r in rows))
    # Null rather than defaulted. The `$` printed on these pages is ambiguous
    # across the USD, CAD and MXN areas this site serves, so a symbol-derived
    # currency would be a guess wearing the look of a fact.
    ok &= check("currency is NULL rather than guessed from the symbol",
                all(r.currency is None for r in rows))
    ok &= check("no row invents coordinates",
                all(r.latitude is None and r.longitude is None for r in rows))
    ok &= check("the page publishes no currency of its own",
                page_currency(LISTING_JOBS) is None)
    ok &= check("category comes through as the site's code",
                rows[0].category == "jobs")
    return ok


def test_null_island():
    section("Listing parsing: the (0,0) placeholder")
    ok = True
    rows = parse_products(LISTING_MEXICOCITY, MEXICO_URL, page=1)
    ok &= check("every entry became a row", len(rows) == 10)
    ok &= check("the currency is the page's own, from structured data",
                {r.currency for r in rows if r.currency} == {"MXN"})
    # 632 of 2,210 structured entries carry (0,0) across the captures, and
    # which ones is decided entirely by the area. Reported as written, every
    # one of these adverts lands in the Gulf of Guinea on a column that reads
    # as fully populated.
    ok &= check("no row carries the (0,0) placeholder as a coordinate",
                not any(r.latitude == 0.0 and r.longitude == 0.0 for r in rows))
    ok &= check("those rows report no coordinates at all",
                all(r.latitude is None and r.longitude is None for r in rows))
    ok &= check("but they keep their prices and ids",
                all(r.sku for r in rows)
                and sum(1 for r in rows if r.price is not None) == 10)
    return ok


def test_tokyo_publishes_its_own_nonsense():
    section("Listing parsing: the site's own data is not corrected")
    ok = True
    # A WINDOW of the Tokyo listing rather than its first entries: the advert
    # this check is about is the 27th on the page.
    rows = parse_products(LISTING_TOKYO, TOKYO_URL, page=1)
    ok &= check("JPY comes from the page, not from the exit country",
                {r.currency for r in rows if r.currency} == {"JPY"})
    shelves = [r for r in rows if "IVAR" in (r.title or "")]
    ok &= check("the IKEA advert is in the fixture", len(shelves) == 1)
    if shelves:
        # The site's own JSON-LD says "2500013000.00" and its own result entry
        # prints ¥2,500,013,000, while the title says ¥13,000 each. Both views
        # agree, so the parser is right and the poster typed a number nobody
        # checked. Pinned so nobody "fixes" this into disagreeing with the
        # site for reasons no consumer could audit.
        ok &= check("its price is reported as the site publishes it",
                    shelves[0].price == 2500013000.0)
    return ok


# ==========================================================================
# Postings
# ==========================================================================

def test_posting_values():
    section("Posting parsing: values")
    ok = True
    row = parse_posting(POSTING_CARS, POSTING_URL)
    ok &= check("a posting page yields a row", row is not None)
    if row is None:
        return False
    ok &= check("sku from the URL", row.sku == "kfvqiXscPUVb9PyC34B4YN")
    # The classic 10-digit id, which a served LISTING does not carry at all --
    # it appears only on a rendered card, or here.
    ok &= check("the numeric post id comes off the page", row.post_id == "7966144398")
    ok &= check("price and currency", (row.price, row.currency) == (36995.0, "USD"))
    ok &= check("price_source says the DOM", row.price_source == "dom")
    ok &= check("both timestamps, by LABEL not position",
                row.posted_at == "2026-09-14T08:44:24-0400"
                and row.updated_at == "2026-09-14T08:44:25-0400")
    ok &= check("every image, deduped across size renditions", row.image_count == 24)
    ok &= check("image_url is the advert's FIRST image, in document order",
                row.image_url == row.images[0]
                and row.image_url.endswith("00g0g_dq2dbt7p7mR_0sU0gg_600x450.jpg"))
    ok &= check("coordinates off the map element",
                (row.latitude, row.longitude) == (40.67, -73.9367))
    # The unlabelled `.attr.important` pair is where cars put the year and the
    # make/model -- skipping it would drop the two fields most worth having.
    attrs = row.attributes or {}
    ok &= check("unlabelled attributes are keyed by their own class",
                attrs.get("year") == "2021"
                and attrs.get("makemodel") == "toyota sequoia platinum")
    ok &= check("labelled attributes are keyed by their label",
                attrs.get("condition") == "excellent"
                and attrs.get("odometer") == "62,000")
    ok &= check("the category comes from the breadcrumb, not the URL",
                row.category == "cars+trucks")
    # `#postingbody` opens with a print-only QR block. Read without removing
    # it, every body in the output starts with "QR Code Link to This Post".
    ok &= check("the body does not start with the print-only QR label",
                not (row.body or "").startswith("QR Code"))
    ok &= check("the body is the advert's own words",
                (row.body or "").startswith("2021 Toyota Sequoia Platinum"))
    return ok


def test_posting_variants():
    section("Posting parsing: what varies by category and locale")
    ok = True

    housing = parse_posting(POSTING_HOUSING, "https://www.craigslist.org/view/d/"
                            "brooklyn-boerum-hill-brooklyn-apartment/5EmuzWqvSj2bkWUKihATkC")
    # Housing's structured data carries NO price -- the whole category's does
    # not -- so this one comes from the DOM, and its currency is null rather
    # than assumed from the `$`.
    ok &= check("a housing advert's price comes from the DOM",
                housing.price == 1700.0 and housing.price_source == "dom")
    ok &= check("its images live on the thumbnail strip, and all are found",
                housing.image_count == 7)
    ok &= check("its category is the breadcrumb's",
                housing.category == "apartments / housing for rent")
    ok &= check("a scrubbed licence number is not in the fixture",
                "00353466" not in POSTING_HOUSING and "02241060" not in POSTING_HOUSING)
    ok &= check("but the attribute STRUCTURE survived scrubbing",
                set(housing.attributes or {}) ==
                {"application fee details", "broker fee details", "listed by",
                 "rent period"})

    berlin = parse_posting(POSTING_BERLIN, "https://www.craigslist.org/view/d/"
                           "2025-vw-golf-mk-85-sitze/jffhVDqeR7k7DdiCre4Ps4")
    ok &= check("a German price is read with dot grouping",
                berlin.price == 1400.0 and berlin.currency == "EUR")
    # Present on all four US postings captured and none of the three
    # international ones. Recorded as an observation rather than a rule.
    ok &= check("a non-US advert has no map element, so no coordinates",
                berlin.latitude is None and berlin.longitude is None)

    jobs = parse_posting(POSTING_JOBS, "https://www.craigslist.org/view/d/"
                         "staten-island-roofer-sider-and-board-ups/rfSB7va5Vn4SdEBpSiTT9g")
    ok &= check("a jobs advert has no price and no images",
                jobs.price is None and jobs.currency is None
                and jobs.image_count is None)
    ok &= check("but it does have a category-shaped attribute bag",
                (jobs.attributes or {}).get("job title") == "Roofer")

    ok &= check("a page that is not a posting yields None",
                parse_posting("<html><body>nothing</body></html>", POSTING_URL) is None)
    return ok


# ==========================================================================
# Page state
# ==========================================================================

def test_page_state():
    section("Page state")
    ok = True
    cases = [
        ("a served listing", LISTING_NEWYORK, 200, "content"),
        ("a rendered grid", RENDERED_GRID, 200, "content"),
        ("a posting", POSTING_CARS, 200, "content"),
        ("a query that matched nothing", EMPTY_RESULTS, 200, "empty"),
        ("a page caught mid-handover", MID_HANDOVER, 200, "unpainted"),
        ("Chromium's own error page", CHROMIUM_ERROR, None, "blocked"),
        ("a 403", "<html><body>no</body></html>", 403, "blocked"),
        ("a 503", "<html><body>no</body></html>", 503, "blocked"),
    ]
    for label, html, status, want in cases:
        got = detect_page_state(html, status, LISTING_URL)
        ok &= check("%s classifies as %s" % (label, want), got == want)

    # THE distinction that a live run got wrong. Both pages have the site's
    # results container and both containers are EMPTY; only the structured
    # data separates them, and calling the second one "empty" made a run
    # report 0 rows and exit 4 on a page holding 329 structured items.
    ok &= check("an empty result set and a mid-handover page are not confused",
                detect_page_state(EMPTY_RESULTS, 200, LISTING_URL) !=
                detect_page_state(MID_HANDOVER, 200, LISTING_URL))
    ok &= check("is_no_results agrees with the empty fixture",
                is_no_results(EMPTY_RESULTS) and not is_no_results(LISTING_NEWYORK))
    return ok


def test_chromium_error_page():
    section("Page state: the case inverted detection exists for")
    ok = True
    # Captured through a dead proxy. It carries the SITE'S OWN HOSTNAME in its
    # title, so a title check calls it a real page, and it carries no vendor
    # marker of any kind -- so a marker list has nothing to match either.
    ok &= check("it names the site in its title, as a real page would",
                "<title>www.craigslist.org</title>" in CHROMIUM_ERROR)
    ok &= check("no challenge vendor is detectable in it",
                detect_bot_challenge(CHROMIUM_ERROR, LISTING_URL) is None)
    ok &= check("it was not built out of the site's own assets",
                not served_by_craigslist(CHROMIUM_ERROR))
    ok &= check("a real page WAS", served_by_craigslist(LISTING_NEWYORK))
    ok &= check("so it is reported as blocked, not as content",
                detect_page_state(CHROMIUM_ERROR, None, LISTING_URL) == "blocked")
    return ok


def test_markers_match_no_good_page():
    section("Page state: markers")
    ok = True
    # A marker that matches a page the site plainly served is worse than no
    # marker: it turns every successful run into a reported block. A sibling
    # repo shipped `akamai` and reported exit 3 on a 191 KB page holding the
    # full catalogue, because the site's own performance script names an
    # Akamai host.
    good_pages = {"listing": LISTING_NEWYORK, "posting": POSTING_CARS,
                  "grid": RENDERED_GRID, "empty": EMPTY_RESULTS,
                  "mid-handover": MID_HANDOVER}
    for name, html in good_pages.items():
        hit = detect_bot_challenge(html, LISTING_URL)
        ok &= check("no marker fires on a good page (%s)" % name, hit is None)
    # Measured across 22 captures: zero occurrences of every vendor marker,
    # and zero occurrences of the word "captcha" itself. The set is empty
    # BECAUSE of that measurement, not by omission.
    ok &= check("the marker set is empty, and deliberately",
                BOT_CHALLENGE_MARKERS == {})
    ok &= check("the asset check needs more than one reference",
                not served_by_craigslist(
                    '<script src="https://www.craigslist.org/static/d/x.js"></script>'))
    return ok


# ==========================================================================
# The rendered grid, and walking it
# ==========================================================================

def test_rendered_grid():
    section("The rendered grid")
    ok = True
    rows = parse_rendered_cards(RENDERED_GRID, LISTING_URL, page=2, currency="USD")
    ok &= check("every card became a row", len(rows) == 6)
    ok &= check("cards carry the classic numeric id",
                all(r.post_id and r.post_id.isdigit() for r in rows))
    ok &= check("and the URL token as the sku", all(r.sku for r in rows))
    ok &= check("price_source says the DOM on every one",
                all(r.price_source == "dom" for r in rows if r.price is not None))
    ok &= check("a card publishes no coordinates, and none are invented",
                all(r.latitude is None and r.longitude is None for r in rows))
    # The site serves its OWN placeholder for an advert with no photograph.
    # Letting it through gives a column that is 100% populated and half
    # placeholder, which is worse than one that is honestly sparse.
    ok &= check("the site's empty.png placeholder is not reported as an image",
                not any("empty.png" in (r.image_url or "") for r in rows))
    ok &= check("the currency is the one passed in, not re-read per harvest",
                {r.currency for r in rows if r.price is not None} == {"USD"})
    ok &= check("the page's own total is read from the rendered header",
                total_results(RENDERED_GRID) == 10000)
    ok &= check("a served page states no total, and none is invented",
                total_results(LISTING_NEWYORK) is None)
    ok &= check("the served page states its own description instead",
                search_header(LISTING_NEWYORK) == "craigslist For Sale in New York City")

    # The fallback that a live run needed: over a remote profile the
    # application had already removed the served list by the time the first
    # snapshot was taken, and parse_products returned 0 rows from a page with
    # 200 cards on it.
    fallback = parse_products(RENDERED_GRID, LISTING_URL, page=1)
    ok &= check("parse_products falls back to the grid when the list is gone",
                len(fallback) == 6)
    return ok


class _FakeGrid:
    """A virtualised list, without a browser.

    Models the two things that matter and nothing else: the window slides by a
    number of items proportional to the scroll distance, and only the items
    inside it are readable -- everything else has been recycled out of the
    document.
    """

    # 139 px per item is the real ratio, measured: the site's counter advanced
    # about 18 positions per 2,500 px step against a 200-item window, which is
    # why a healthy walk overlaps heavily and a slow one does not.
    def __init__(self, total=10000, window=200, px_per_item=139.0, cap=10000):
        self.total, self.window = total, window
        self.px_per_item, self.cap = px_per_item, cap
        self.top = 0.0
        self.harvested = set()
        self.reads = 0

    def count(self, selector):
        return min(self.window, self.total - int(self.top))

    def text(self, selector):
        high = min(int(self.top) + self.window, self.cap)
        return "%d - %d of %d+" % (int(self.top) + 1, high, self.cap)

    def scroll_by(self, px):
        self.top = min(self.top + px / self.px_per_item, self.total - 1)

    def sleep(self, ms):
        pass

    def harvest(self):
        self.reads += 1
        lo = int(self.top)
        seen = set(range(lo, min(lo + self.window, self.total)))
        self.harvested |= seen
        return seen


def test_walk_machinery():
    section("Walking the virtualised list")
    ok = True

    # A walk that keeps up collects everything it passes.
    grid = _FakeGrid()
    result = page_flow.walk_virtualised(
        count=grid.count, text=grid.text, scroll_by=grid.scroll_by,
        sleep=grid.sleep, harvest=grid.harvest, want_items=900)
    ok &= check("the walk reaches what it was asked for",
                result["counter_reached"] >= 900)
    ok &= check("and opens no gaps while it keeps up", result["gaps"] == 0)
    ok &= check("collecting every item it passed",
                len(grid.harvested) >= result["counter_reached"])

    # THE failure this machinery exists to catch. Two consecutive live runs of
    # one URL harvested 1000 and 360 rows at the same counter position: when a
    # harvest takes longer than a scroll step covers, the window moves past
    # rows nobody reads and nothing in the result says so.
    narrow = _FakeGrid(window=20, px_per_item=1.0)   # window far smaller than a step
    gapped = page_flow.walk_virtualised(
        count=narrow.count, text=narrow.text, scroll_by=narrow.scroll_by,
        sleep=narrow.sleep, harvest=narrow.harvest, want_items=5000)
    ok &= check("a walk that outruns its harvest COUNTS the gaps",
                gapped["gaps"] > 0)
    ok &= check("and halves its step when it does",
                gapped["final_step_px"] < page_flow.SCROLL_STEP_PX)
    ok &= check("the step never falls below its floor",
                gapped["final_step_px"] >= page_flow.SCROLL_STEP_MIN)

    # The site's own ceiling ends the walk, rather than the step budget.
    capped = _FakeGrid(total=10000, cap=10000)
    result = page_flow.walk_virtualised(
        count=capped.count, text=capped.text, scroll_by=capped.scroll_by,
        sleep=capped.sleep, harvest=capped.harvest, want_items=99999)
    ok &= check("the walk stops at the site's 10,000-result ceiling",
                result["hit_cap"] and result["counter_reached"] == RESULT_CAP)

    # A counter that stops advancing ends the walk rather than spending the
    # whole step budget on a list that has finished.
    short = _FakeGrid(total=300, cap=300)
    result = page_flow.walk_virtualised(
        count=short.count, text=short.text, scroll_by=short.scroll_by,
        sleep=short.sleep, harvest=short.harvest, want_items=99999)
    ok &= check("a stalled counter ends the walk early",
                result["steps"] < page_flow.WALK_STEPS_MAX)

    ok &= check("window_high reads the site's own counter",
                (page_flow.window_high("1 - 6 of 10,000+"),
                 page_flow.window_high("9,997 - 10,000 of 10,000+"),
                 page_flow.window_high(None),
                 page_flow.window_high("retrieving more"))
                == (6, 10000, None, None))
    return ok


def test_page_flow_policy():
    section("Page-flow policy")
    ok = True
    ok &= check("content parses and does not retry",
                page_flow.should_parse("content") and not page_flow.should_retry("content"))
    # An empty result set is a correct ANSWER. Retrying it spends a fetch on a
    # question already answered, and reporting it as blocked sends the reader
    # looking for a proxy problem.
    ok &= check("empty parses, does not retry, and is not blocked",
                page_flow.should_parse("empty") and not page_flow.should_retry("empty")
                and not page_flow.counts_as_blocked("empty"))
    # `unpainted` WAITS. Retrying throws away a page that is about to be fine;
    # solving buys a token for a challenge that is not there.
    ok &= check("unpainted waits rather than retrying or spending",
                page_flow.should_wait("unpainted")
                and not page_flow.should_retry("unpainted")
                and not page_flow.should_solve("unpainted"))
    ok &= check("blocked retries from another exit and buys nothing",
                page_flow.should_retry("blocked") and not page_flow.should_solve("blocked")
                and page_flow.counts_as_blocked("blocked"))
    ok &= check("challenge is the only state that may spend",
                page_flow.should_solve("challenge"))
    ok &= check("an unknown state is treated as blocked, not as content",
                not page_flow.should_parse("something-new")
                and page_flow.counts_as_blocked("something-new"))
    ok &= check("javascript is off for one batch and on for more",
                not page_flow.javascript_needed(1) and page_flow.javascript_needed(2))
    ok &= check("the ready selector covers BOTH views of a listing",
                SELECTORS["static_result"] in page_flow.READY_SELECTOR_LISTING
                and SELECTORS["rendered_card"] in page_flow.READY_SELECTOR_LISTING)
    # Waiting for ONE match resolves on an unrelated link long before a list
    # arrives.
    ok &= check("more than one match is required to call a page ready",
                page_flow.MIN_CARD_MATCHES > 1)
    return ok


# ==========================================================================
# The output contract
# ==========================================================================

def test_output_contract():
    section("Output contract")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family's prefix, in the family's order. A consumer written against
    # another repo in this family reads these columns by name, so the prefix
    # is a contract rather than a convenience.
    prefix = ["source", "scraped_at", "url", "sku", "title", "brand", "price",
              "currency", "original_price", "discount_pct", "rating",
              "review_count", "in_stock", "image_url", "category",
              "price_source", "page", "position"]
    ok &= check("the family's 18-column prefix is intact and in order",
                names[:len(prefix)] == prefix)
    ok &= check("this site's columns are appended after it, not interleaved",
                names[len(prefix):len(prefix) + 3] == ["post_id", "area", "location"])
    ok &= check("the modes map to a row class",
                set(ROW_CLASS_BY_MODE) == {"listing", "posting"})
    ok &= check("both modes are one row per sku, so both can be deduped and diffed",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "posting"})
    ok &= check("exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_API_ERROR, EXIT_PARTIAL)
                == (3, 4, 5, 6))
    return ok


def test_writers():
    section("Writers")
    ok = True
    rows = parse_products(LISTING_NEWYORK, LISTING_URL, page=1)[:3]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")
        with redirect_stdout(io.StringIO()):
            save(rows, prefix, fmt="both")
        loaded = json.load(open(prefix + ".json"))
        ok &= check("JSON round-trips every row", len(loaded) == 3)
        ok &= check("and every column, in order",
                    list(loaded[0]) == [f.name for f in fields(Product)])
        with open(prefix + ".csv") as fh:
            read = list(csv.DictReader(fh))
        ok &= check("CSV round-trips every row", len(read) == 3)
        ok &= check("CSV and JSON agree on the columns",
                    list(read[0]) == list(loaded[0]))

        # A consumer should read a table with no rows, not fail on a
        # zero-byte file.
        empty_prefix = os.path.join(d, "empty")
        write_csv([], empty_prefix + ".csv", row_cls=Product)
        with open(empty_prefix + ".csv") as fh:
            header = fh.readline().strip()
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A run that finds nothing must not replace last night's good output
        # with []. A consumer cannot tell an empty listing from a failed run,
        # and the failure destroys the last known good data.
        good = os.path.join(d, "guard")
        with redirect_stdout(io.StringIO()):
            save(rows, good, fmt="json")
        before = open(good + ".json").read()
        with redirect_stdout(io.StringIO()):
            save([], good, fmt="json")
        ok &= check("a run that finds nothing does not overwrite good output",
                    open(good + ".json").read() == before)
        with redirect_stdout(io.StringIO()):
            save([], good, fmt="json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out",
                    json.load(open(good + ".json")) == [])
    return ok


def test_finish_run():
    section("finish_run: status and exit codes")
    ok = True
    rows = parse_products(LISTING_NEWYORK, LISTING_URL, page=1)[:2]
    with tempfile.TemporaryDirectory() as d:
        def run(**kw):
            args = dict(rows=rows, out_prefix=os.path.join(d, kw.pop("name")),
                        fmt="json", pages_requested=1, pages_completed=1,
                        pages_failed=[], blocked=False, stop_reason="completed",
                        start_url=LISTING_URL, final_url=LISTING_URL,
                        mode="listing", allow_empty=False)
            args.update(kw)
            with redirect_stdout(io.StringIO()):
                return finish_run(**args)

        ok &= check("a complete run exits 0", run(name="a") == 0)
        ok &= check("a blocked run exits 3",
                    run(name="b", rows=[], blocked=True,
                        stop_reason="blocked_not-served") == EXIT_BLOCKED)
        ok &= check("a run that found nothing exits 4",
                    run(name="c", rows=[], stop_reason="completed") == EXIT_NO_PRODUCTS)
        ok &= check("a partial run exits 6",
                    run(name="d", pages_requested=3, pages_completed=1,
                        pages_failed=[2], stop_reason="page_load_timeout")
                    == EXIT_PARTIAL)
        # Blocked, empty and partial are three DIFFERENT answers. A consumer
        # that cannot tell them apart cannot tell a dead exit from an empty
        # category.
        ok &= check("blocked, empty and partial are three distinct codes",
                    len({EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL}) == 3)

        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run leaves no sidecar contradicting good data",
                    not os.path.exists(os.path.join(d, "b.meta.json")))
        meta = json.load(open(os.path.join(d, "a.meta.json")))
        ok &= check("the sidecar records the mode, since the repo no longer implies it",
                    meta.get("mode") == "listing")
        ok &= check("and which pages failed BY NUMBER, not as a count",
                    isinstance(meta.get("pages_failed"), list))
    return ok


def test_diff():
    section("diff_runs")
    ok = True
    a = parse_products(LISTING_NEWYORK, LISTING_URL, page=1)[:3]
    b = [Product(**{f.name: getattr(r, f.name) for f in fields(Product)}) for r in a]
    b[0].price = (b[0].price or 0) + 100
    result = diff_products([r.__dict__ for r in a], [r.__dict__ for r in b])
    changed = result.get("changed") or result.get("price_changed") or []
    ok &= check("a price change is reported", len(changed) == 1)

    # A price change that comes with a price_source change says something
    # about our own two snapshots, not about the site -- one run read the
    # served list and the other the rendered grid.
    c = [Product(**{f.name: getattr(r, f.name) for f in fields(Product)}) for r in a]
    c[0].price = (c[0].price or 0) + 100
    c[0].price_source = "dom"
    result = diff_products([r.__dict__ for r in a], [r.__dict__ for r in c])
    ok &= check("a change that comes with a price_source change is separated out",
                len(result.get("source_changed") or []) == 1
                and len(result.get("changed") or []) == 0)
    return ok


# ==========================================================================
# The three engines
# ==========================================================================

def _engine_source(name):
    return open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()


def _engine_flags(name):
    """Every `--flag` the engine's CLI defines, read from its AST.

    From the SOURCE rather than by importing and running parse_args, so this
    works with no engine library installed -- which is the whole point of the
    suite being importable without one.

    Scoped to `parse_args`, because Chrome's own options object has an
    `add_argument` too: an unscoped walk collected `--headless=new`,
    `--no-sandbox` and `--window-size=1600,1000` from the Selenium engine and
    reported them as CLI flags its twins were missing.
    """
    tree = ast.parse(_engine_source(name))
    parser_fn = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "parse_args"),
                     None)
    if parser_fn is None:
        return set()
    flags = set()
    for node in ast.walk(parser_fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    flags.add(arg.value)
    return flags


def test_engine_flag_parity():
    section("Engines: the flag contract")
    ok = True
    # The family's contract. Sixteen of these were documented nowhere in a
    # sibling repo until a check like this was written.
    contract = {"--url", "--pages", "--category", "--format", "--out", "--delay",
                "--retries", "--retry-delay", "--concurrency", "--proxy",
                "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
                "--proxy-block-retries", "--twocaptcha-key", "--captcha-api",
                "--solve-captcha", "--min-score", "--cdp-endpoint",
                "--allow-empty", "--dump-html", "--mode", "--fingerprint"}
    per_engine = {name: _engine_flags(name) for name in ENGINES}
    for name, flags in per_engine.items():
        missing = sorted(contract - flags)
        ok &= check("%s carries every contract flag%s"
                    % (name, "" if not missing else " (missing %s)" % missing),
                    not missing)

    # BOTH directions. A missing flag fails, and so does closing a difference
    # the README documents -- the three are not allowed to drift apart
    # quietly in either direction.
    # Differences the README documents, and only these. A missing flag fails
    # above; closing one of these without updating the README fails here.
    documented_differences = {
        "playwright_scraper.py": {"--locale"},
        "selenium_scraper.py": set(),
        "puppeteer_scraper.py": {"--chromium-path"},
    }
    common = set.intersection(*per_engine.values())
    for name, flags in per_engine.items():
        extra = flags - common - documented_differences.get(name, set())
        ok &= check("%s has no undocumented extra flags%s"
                    % (name, "" if not extra else " (%s)" % sorted(extra)),
                    not extra)
    return ok


def test_engines_import_their_driver_at_module_level():
    section("Engines: driver imports")
    ok = True
    # For "the suite passes with no engine installed" to MEAN anything, each
    # engine has to fail to import when its driver is absent. A sibling repo
    # imported pyppeteer inside the launch path, so the module imported
    # cleanly with nothing installed: the group never skipped, and the CI job
    # that exists to fail on unexpected skips could not have caught a broken
    # import.
    expected = {"playwright_scraper.py": "playwright",
                "selenium_scraper.py": "selenium",
                "puppeteer_scraper.py": "pyppeteer"}
    for name, module in expected.items():
        tree = ast.parse(_engine_source(name))
        at_module_level = any(
            (isinstance(n, ast.Import) and any(a.name.split(".")[0] == module for a in n.names))
            or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == module)
            for n in tree.body)
        ok &= check("%s imports %s at module level" % (name, module), at_module_level)
    return ok


def test_engine_calls_bind_against_the_real_signatures():
    section("Engines: shared-module call sites")
    ok = True
    # The check that matters because nothing else makes it. A sibling repo had
    # `classify(html, status, url)` called as `classify(html, url=...)` in two
    # of three engines; both crashed on their FIRST fetch, and it was
    # invisible to import, --help, compileall, the undefined-name walk and 400+
    # green assertions -- because none of those calls a function the way a live
    # run does.
    modules = {"product_parser": product_parser, "page_flow": page_flow,
               "output_writer": sys.modules["output_writer"],
               "proxy_pool": proxy_pool}

    class Placeholder:
        def __repr__(self):
            return "<x>"

    problems = []
    for name in ENGINES:
        tree = ast.parse(_engine_source(name))
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in modules:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                target = imported[node.func.id]
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in modules):
                target = (node.func.value.id, node.func.attr)
            if not target:
                continue
            fn = getattr(modules[target[0]], target[1], None)
            if not callable(fn) or inspect.isclass(fn):
                continue
            if (any(isinstance(a, ast.Starred) for a in node.args)
                    or any(k.arg is None for k in node.keywords)):
                continue
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            try:
                sig.bind(*[Placeholder() for _ in node.args],
                         **{k.arg: Placeholder() for k in node.keywords})
            except TypeError as exc:
                problems.append("%s:%d %s.%s -- %s"
                                % (name, node.lineno, target[0], target[1], exc))
    ok &= check("every engine call into a shared module binds%s"
                % ("" if not problems else ": " + "; ".join(problems[:3])),
                not problems)
    return ok


def test_shared_constants_are_used_consistently():
    section("Engines: shared constants")
    ok = True
    # A constant whose TYPE changes is invisible to import, to --help, to
    # compileall and to the signature walk above, because nothing about it is
    # a function call. PRICE_FLOOR went from a per-page-kind dict to an int
    # here; two engines were updated and the third kept `.get(kind, 0)`, which
    # is an AttributeError on the line that runs after a successful parse.
    for name in ENGINES:
        src = _engine_source(name)
        ok &= check("%s does not call .get() on the scalar PRICE_FLOOR" % name,
                    "PRICE_FLOOR.get(" not in src)
        ok &= check("%s treats PRICE_FLOOR as a number" % name,
                    "PRICE_FLOOR" in src)
    # Every engine must consult the policy rather than reimplementing it.
    for name in ENGINES:
        src = _engine_source(name)
        ok &= check("%s asks page_flow whether a state is blocked" % name,
                    "counts_as_blocked" in src)
        ok &= check("%s asks page_flow whether to wait" % name,
                    "should_wait" in src)
    return ok


def test_policy_constants_have_consumers():
    section("Policy: no constant that nothing reads")
    ok = True
    # A policy constant nothing consults is the same defect as dead code, and
    # harder to see: the prose beside it reads like enforcement. A sibling
    # repo's RETRY_ON_BLOCKED carried a paragraph of measured justification
    # and no engine consulted it, so setting it False changed nothing.
    sources = {name: _engine_source(name) for name in ENGINES}
    sources["product_parser.py"] = open(
        os.path.join(REPO_ROOT, "product_parser.py"), encoding="utf-8").read()
    for const in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                  "SOLVES_PER_PAGE", "RESULT_CAP", "BATCH_HINT",
                  "MIN_CARD_MATCHES"):
        consumers = [n for n, src in sources.items() if const in src]
        ok &= check("page_flow.%s is read outside its own module%s"
                    % (const, "" if consumers else " -- NOTHING reads it"),
                    bool(consumers))
    return ok


# ==========================================================================
# Wording, names and dead code
# ==========================================================================

SHIPPED_FILES = [f for f in sorted(os.listdir(REPO_ROOT))
                 if f.endswith((".py", ".md", ".txt", ".yml", ".yaml", ".toml",
                                ".example"))]


def _shipped_text():
    for name in SHIPPED_FILES:
        path = os.path.join(REPO_ROOT, name)
        if os.path.isfile(path):
            yield name, open(path, encoding="utf-8", errors="replace").read()


def test_wording():
    section("Wording")
    ok = True
    # Product naming, enforced rather than remembered. The phrases on the
    # left name things that either do not exist or belong to a competitor.
    banned = {
        "cloud browser": "Scraping Browser API",
        "antidetect browser": "Scraping Browser API",
        "anti-detect browser": "Scraping Browser API",
        "gate.2prx.com": "2captcha.com/proxy",
        "--antidetect": "removed -- that endpoint was a placeholder",
        "ANTIDETECT_LOCAL_API": "removed",
    }
    for name, text in _shipped_text():
        if name == os.path.basename(__file__):
            continue
        low = text.lower()
        for phrase, instead in banned.items():
            ok &= check("%s does not say %r (use: %s)" % (name, phrase, instead),
                        phrase.lower() not in low)
    return ok


def test_removed_flags_stay_removed():
    section("Removed flags")
    ok = True
    # A --country flag on a scraper could only disagree with the URL: the area
    # is a path segment here and the currency follows it, measured across
    # eight areas through one exit. Scoped to the ENGINES, because
    # fingerprint_client.py's --fp-country is legitimate and names something
    # else entirely.
    for name in ENGINES:
        src = _engine_source(name)
        ok &= check("%s defines no --country" % name,
                    '"--country"' not in src and "'--country'" not in src)
    return ok


def test_no_undefined_names():
    section("Names")
    ok = True
    # compileall proves a file PARSES, not that its names RESOLVE. A sibling
    # repo's engine died with NameError on a line reached only while fetching,
    # after an import was removed: the module imported cleanly, --help worked,
    # compileall passed and the whole offline suite was green.
    #
    # Deliberately coarse -- one pool of bindings per module, no scope
    # tracking -- so it under-reports rather than inventing problems.
    for name in SHIPPED_FILES:
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(REPO_ROOT, name),
                              encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__",
                                        "__spec__", "__builtins__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Global):
                defined.update(node.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - defined)
        ok &= check("%s resolves every name it uses%s"
                    % (name, "" if not missing else " (%s)" % missing),
                    not missing)
    return ok


def test_dockerfile_copies_what_it_runs():
    section("Dockerfile")
    ok = True
    # All three repos in this family once shipped an image that died with
    # ModuleNotFoundError on EVERY invocation, --help included, because one
    # module was missing from an explicit COPY list. An explicit list is
    # right -- the image should carry no test suite and no stray .env -- but
    # it falls behind, and CI never builds the image.
    dockerfile = open(os.path.join(REPO_ROOT, "Dockerfile"), encoding="utf-8").read()
    # Line continuations joined first: the COPY list spans three lines, and a
    # line-by-line reader sees only the first third of it.
    joined = dockerfile.replace("\\\n", " ")
    copied_files = set()
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            copied_files.update(part for part in line.split()
                                if part.endswith(".py"))

    # The image ships ONE engine, named by its ENTRYPOINT, so the graph to
    # walk is that engine's rather than all three. Read from the Dockerfile
    # rather than hard-coded, so switching the image to another engine moves
    # this check with it instead of leaving it asserting the wrong thing.
    entry = re.search(r'ENTRYPOINT\s+\[([^\]]*)\]', dockerfile)
    entry_scripts = re.findall(r'"([^"]+\.py)"', entry.group(1) if entry else "")
    ok &= check("the Dockerfile names a Python entrypoint", bool(entry_scripts))
    if not entry_scripts:
        return False

    local_modules = {f[:-3] for f in SHIPPED_FILES if f.endswith(".py")}
    needed, queue = set(), list(entry_scripts)
    while queue:
        current = queue.pop()
        if current in needed:
            continue
        needed.add(current)
        path = os.path.join(REPO_ROOT, current)
        if not os.path.exists(path):
            continue
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            module = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[0]
                    if module in local_modules:
                        queue.append(module + ".py")
            elif isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[0]
                if module in local_modules:
                    queue.append(module + ".py")
    needed.discard("smoke_test.py")
    missing = sorted(m for m in needed if m not in copied_files)
    ok &= check("the image copies every module %s imports%s"
                % (", ".join(entry_scripts),
                   "" if not missing else " (missing %s)" % missing),
                not missing)
    ok &= check("and does not copy the test suite into the image",
                "smoke_test.py" not in copied_files)
    return ok


# ==========================================================================
# Configuration, proxies and credentials
# ==========================================================================

def test_env_config():
    section("env_config")
    ok = True
    example = os.path.join(REPO_ROOT, ".env.example")
    ok &= check(".env.example exists", os.path.exists(example))
    if not os.path.exists(example):
        return False
    text = open(example, encoding="utf-8").read()
    documented = set(re.findall(r'^([A-Z][A-Z0-9_]+)=', text, re.M))
    read_by_code = set(env_config.ENV_KEYS)
    # Both directions. A documented-but-unread variable is worse than an
    # undocumented one: it looks configurable and is not.
    ok &= check("every variable .env.example documents is one the code reads%s"
                % ("" if documented <= read_by_code
                   else " (extra: %s)" % sorted(documented - read_by_code)),
                documented <= read_by_code)
    ok &= check("every variable the code reads is documented%s"
                % ("" if read_by_code <= documented
                   else " (missing: %s)" % sorted(read_by_code - documented)),
                read_by_code <= documented)

    # A copied .env.example must read as UNSET, not as configured. The two
    # credentialled URLs are documented the way the vendor documents them --
    # `ws://{login}-zone-...` -- and a literal placeholder list did not match
    # either, so `cp .env.example .env` followed by a run connected with the
    # string "{login}-zone-..." as its username and got a 401 a long way from
    # its cause.
    def looks_unset(value):
        return (value in env_config._PLACEHOLDERS
                or bool(env_config._BRACED_PLACEHOLDER_RE.search(value)))

    values = dict(pair for pair in
                  (env_config._parse_line(line) for line in text.splitlines())
                  if pair)
    ok &= check("the example documents every variable as a line the loader parses",
                set(values) == documented)
    for key, value in sorted(values.items()):
        if key == "CRAIGSLIST_URL":
            continue   # a real default, and the one variable meant to work
        ok &= check("a copied %s reads as unset, not as a credential" % key,
                    looks_unset(value))
    ok &= check("the default URL in a copied .env is usable as it stands",
                not looks_unset(values.get("CRAIGSLIST_URL", "")))
    # The braced form is what a literal placeholder LIST misses: the vendor
    # documents its endpoints as `ws://{login}-zone-...`, which matches no
    # entry in a list of example values, so `cp .env.example .env` followed by
    # a run once connected with the string "{login}-zone-..." as its username
    # and got a 401 a long way from its cause.
    braced = "ws://{login}-zone-scraping_browser:{password}@host:9222"
    # Assembled rather than written out, so this file carries no string that
    # LOOKS like a credentialled URL for the repo's own secret scan to trip
    # over. The scan is right to be blunt about that shape.
    filled = braced.replace("{login}", "exampleaccount").replace("{password}", "examplepw")
    ok &= check("a braced vendor placeholder is recognised as unset",
                looks_unset(braced))
    ok &= check("but a filled-in endpoint is not", not looks_unset(filled))

    # --out has a non-empty default, so a variable mapped onto it would be
    # silently inert: the loader only fills UNSET values.
    ok &= check("no variable is mapped onto a flag with a non-empty default",
                "out" not in set(env_config.ENV_KEYS.values()))
    return ok


def test_proxy_pool():
    section("proxy_pool")
    ok = True
    pool = ProxyPool(["http://u:p@a.example:1", "http://u:p@b.example:2",
                      "socks5://u:p@c.example:3"])
    ok &= check("a pool holds its exits", len(pool) == 3)
    # Which exit a run used is the POINT of the log and is not the secret.
    masked = mask("http://user:hunter2@exit.example:8080")
    ok &= check("masking keeps the host and port", "exit.example:8080" in masked)
    ok &= check("and drops the password", "hunter2" not in masked)
    ok &= check("and drops the username", "user" not in masked)
    # Globally, not once: a masker that handles the first occurrence prints
    # the password the other four times and looks like it is working.
    twice = mask("http://u:secret@a:1 and http://u:secret@b:2")
    ok &= check("masking is global, not first-occurrence",
                "secret" not in twice)
    scrubbed, creds = split_credentials("http://user:pw@exit.example:8080")
    ok &= check("credentials split off the URL",
                scrubbed == "http://exit.example:8080" and creds == ("user", "pw"))
    return ok


def test_credentials_never_reach_a_log():
    section("Credentials")
    ok = True
    # `requests` puts the FULL URL -- query string included -- into the text
    # of HTTPError and of every connection error, and Playwright repeats a CDP
    # endpoint five times in one message. So any endpoint that takes its key
    # as a query parameter leaks it the moment anything goes wrong.
    #
    # The key used here is FABRICATED, and assembled rather than written out,
    # so this file contains no 32-hex literal of its own. Writing a real key
    # into a test is how a live credential reaches a public repo -- it
    # happened while this very check was being written, and the repo's own
    # secret scan is what caught it.
    secret = "deadbeef" * 4
    for name in ENGINES:
        src = _engine_source(name)
        ok &= check("%s masks the CDP endpoint before logging it" % name,
                    "_mask_credentials" in src)
    # And the masker itself has to take a KEY out of a query string, not just
    # a password out of a userinfo.
    import fingerprint_client
    redacted = getattr(fingerprint_client, "_redact", None)
    if redacted is None:
        redacted = getattr(captcha_solver, "_redact", None)
    if redacted is not None:
        text = redacted("https://api.2captcha.com/fingerprint?key=%s&tags=Windows"
                        % secret)
        ok &= check("an API key in a query string is redacted", secret not in text)
    else:
        ok &= check("a redactor exists for query-string keys", False)

    # No real key committed anywhere, including as a test fixture. A 32-hex
    # string in a public repo reads as a live credential to every scanner
    # that looks.
    for name, text in _shipped_text():
        if name == os.path.basename(__file__):
            continue
        ok &= check("%s commits no live-looking API key" % name,
                    secret not in text)
    return ok


def test_no_capture_leaks():
    section("Fixtures: no personal data")
    ok = True
    # Guarded by PATTERN rather than by the old literals, so the NEXT capture
    # is caught too. A real posting carries a phone number in its body and a
    # broker's name and licence in its attribute bag.
    fixtures = {"POSTING_CARS": POSTING_CARS, "POSTING_HOUSING": POSTING_HOUSING,
                "POSTING_BERLIN": POSTING_BERLIN, "POSTING_JOBS": POSTING_JOBS,
                "LISTING_NEWYORK": LISTING_NEWYORK,
                "LISTING_MEXICOCITY": LISTING_MEXICOCITY,
                "LISTING_TOKYO": LISTING_TOKYO, "LISTING_JOBS": LISTING_JOBS}
    patterns = {
        "a US phone number": r'\b(?<!\d)(?:\d{3}[-.\s]\d{3}[-.\s]\d{4})(?!\d)\b',
        "an email address": r'[\w.+-]+@[\w-]+\.[A-Za-z]{2,}',
        "a DRE licence number": r'DRE\s*(?:#|License:)\s*\d{6,}',
        "a session id": r'sessionId|anti-csrftoken',
    }
    for fixture_name, text in fixtures.items():
        for label, pattern in patterns.items():
            hits = [h for h in re.findall(pattern, text)
                    if "SCRUBBED" not in str(h)]
            ok &= check("%s carries no %s%s"
                        % (fixture_name, label,
                           "" if not hits else " (%s)" % hits[:2]),
                        not hits)
    ok &= check("the scrubbed fixtures say so, with placeholders that show",
                "SCRUBBED" in POSTING_CARS and "SCRUBBED" in POSTING_HOUSING)
    return ok


def test_ci_checks_is_actually_wired_up():
    section("CI")
    ok = True
    # One implementation, invoked from both CI and this suite. Three repos in
    # this family carried a .github/ci_checks.py that NOTHING ran, while the
    # workflow reimplemented a narrower version of the same job inline -- and
    # the two disagreed in the direction that matters.
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("the credential check exists as a script", os.path.exists(script))
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow) and os.path.exists(script):
        text = open(workflow, encoding="utf-8").read()
        ok &= check("the workflow CALLS it rather than reimplementing it",
                    "ci_checks.py" in text)
        # And it has to pass on this repo's own tree. A check that fails on
        # its own repository is a check nobody can read.
        result = subprocess.run([sys.executable, script, "--all"], cwd=REPO_ROOT,
                                capture_output=True, text=True)
        ok &= check("and it passes on this repo%s"
                    % ("" if result.returncode == 0
                       else ": " + (result.stdout + result.stderr)[-300:]),
                    result.returncode == 0)
    return ok


def test_engines(skips):
    section("Engines: importable, and their entry points")
    ok = True
    for name in ENGINES:
        module_name = name[:-3]
        try:
            module = __import__(module_name)
        except ImportError as exc:
            # Recorded, not silently passed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (module_name, exc))
            print("  SKIP  %s -- engine library absent" % module_name)
            continue
        ok &= check("%s exposes scrape() and parse_args()" % module_name,
                    callable(getattr(module, "scrape", None))
                    and callable(getattr(module, "parse_args", None)))
        # Test the PUBLIC entry point, not only its internals: a signature
        # drifted from its callers in this family while every check exercised
        # the private helpers underneath.
        sig = inspect.signature(module.scrape)
        ok &= check("%s.scrape takes exactly the parsed args" % module_name,
                    list(sig.parameters) == ["args"])
    return ok
