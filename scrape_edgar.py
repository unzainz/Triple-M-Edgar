"""
TRIPLE M — EDGAR Form D scraper (GitHub Actions version)
Replaces the Apps Script scraper that SEC blocked at the IP level.

What it does, identically to the original design:
  1. Searches EDGAR full-text search for Form D filings mentioning
     multifamily/apartments in the 14 target states, last 14 days.
  2. Pulls each filing's primary_doc.xml for issuer name, HQ, offering
     size, and related persons.
  3. Skips anything already in the Syndicators tab, anything under $5M,
     and tags likely developers/REITs as 'out'.
  4. POSTs new rows to the existing Apps Script web app
     (action=addSyndicator), which appends to the Sheet -> hub updates
     automatically on next load.

Requires one environment variable: APPS_SCRIPT_URL (the /exec URL).
"""

import os
import re
import sys
import time
import json
from datetime import datetime, timedelta, timezone

import requests
import xml.etree.ElementTree as ET

SCRIPT_URL = os.environ["APPS_SCRIPT_URL"]

UA = "Triple M Investments research cooper@tripleminvestments.com"
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

STATES = ["AZ", "NV", "UT", "ID", "CO", "TX", "OK", "TN", "SC", "NC", "KY", "IN", "OH", "MI"]
STATE_MKT = {
    "AZ": "Phoenix", "NV": "Las Vegas", "UT": "Salt Lake City", "ID": "Boise",
    "CO": "Denver", "TX": "Dallas\u2013Fort Worth", "OK": "Oklahoma City",
    "TN": "Nashville", "SC": "Charleston", "NC": "Charlotte", "KY": "Louisville",
    "IN": "Indianapolis", "OH": "Columbus", "MI": "Detroit",
}
MIN_OFFERING = 5_000_000
LOOKBACK_DAYS = 14
MAX_HITS_PER_STATE = 40


def get_existing_names():
    """Read the Syndicators tab through the existing Apps Script data endpoint."""
    r = requests.get(SCRIPT_URL, params={"action": "data"}, headers=HEADERS, timeout=60)
    r.raise_for_status()
    names = set()
    for s in r.json().get("syndicators", []):
        n = str(s.get("f", "")).lower().strip()
        if n:
            names.add(n)
    return names


def strip_namespaces(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def fetch_detail(cik, accession):
    """Parse the filing's primary_doc.xml for issuer details."""
    url = (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession.replace('-', '')}/primary_doc.xml"
    )
    r = requests.get(url, headers=HEADERS, timeout=60)
    if r.status_code != 200:
        print(f"    detail HTTP {r.status_code} for CIK {cik}")
        return None
    try:
        root = strip_namespaces(ET.fromstring(r.content))
    except ET.ParseError:
        print(f"    detail XML parse fail for CIK {cik}")
        return None

    def text(path):
        el = root.find(path)
        return el.text.strip() if el is not None and el.text else ""

    people = []
    for p in root.findall("relatedPersonsList/relatedPersonInfo")[:5]:
        fn = p.findtext("relatedPersonName/firstName", default="") or ""
        ln = p.findtext("relatedPersonName/lastName", default="") or ""
        full = f"{fn} {ln}".strip()
        if full:
            people.append(full)

    return {
        "name": text("primaryIssuer/entityName"),
        "city": text("primaryIssuer/issuerAddress/city"),
        "state": text("primaryIssuer/issuerAddress/stateOrCountry"),
        "offering": text("offeringData/offeringSalesAmounts/totalOfferingAmount"),
        "people": " | ".join(people),
    }


def push_row(payload):
    """POST one new syndicator to the Apps Script web app."""
    r = requests.post(
        SCRIPT_URL,
        data=json.dumps(payload),
        headers={**HEADERS, "Content-Type": "text/plain;charset=utf-8"},
        timeout=60,
    )
    return r.status_code in (200, 302)


def main():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    print(f"Window: {start} to {end}")

    existing = get_existing_names()
    print(f"Existing syndicators in Sheet: {len(existing)}")

    added = 0
    blocked_states = 0

    for st in STATES:
        params = {
            "q": '"multifamily" OR "apartments"',
            "forms": "D",
            "locationCodes": st,
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
        }
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params, headers=HEADERS, timeout=60,
        )
        if r.status_code != 200:
            print(f"{st}: HTTP {r.status_code}")
            blocked_states += 1
            time.sleep(1)
            continue

        hits = r.json().get("hits", {}).get("hits", [])
        print(f"{st}: {len(hits)} hits")

        for h in hits[:MAX_HITS_PER_STATE]:
            src = h.get("_source", {})
            disp = (src.get("display_names") or [""])[0]
            name_key = disp.lower().split("(cik")[0].strip()
            if not name_key or name_key in existing or disp.lower() in existing:
                continue

            cik = src.get("entity_id", "")
            accession = (h.get("_id", "")).split(":")[0]
            if not cik or not accession:
                continue

            time.sleep(0.3)  # be polite to SEC
            det = fetch_detail(cik, accession)
            if not det or not det["name"]:
                continue

            amt = float(re.sub(r"[^\d.]", "", det["offering"]) or 0)
            if amt and amt < MIN_OFFERING:
                continue

            devish = bool(re.search(
                r"reit|developer|development|construction|homebuild",
                det["name"].lower(),
            ))

            payload = {
                "action": "addSyndicator",
                "f": det["name"] or disp,
                "who": det["people"],
                "hq": (det["city"] + ", " if det["city"] else "") + det["state"],
                "mkt": STATE_MKT.get(st, ""),
                "raises": "auto",
                "off": f"${round(amt / 1e6)}M" if amt else "",
                "last": src.get("file_date", ""),
                "tier": "out" if devish else "new",
                "note": (
                    f"Auto-added from EDGAR Form D {src.get('file_date', '')} "
                    "via GitHub Actions. "
                    + ("Name suggests dev/REIT \u2014 likely screen out."
                       if devish else
                       "Unscreened \u2014 verify against GP criteria.")
                ),
                "cik": str(cik),
                "filed": src.get("file_date", ""),
            }

            if push_row(payload):
                existing.add(name_key)
                added += 1
                print(f"  + {payload['f']} ({payload['hq']}, {payload['off']})")
            else:
                print(f"  ! push failed for {payload['f']}")

        time.sleep(0.6)

    print(f"Done. {added} new syndicators appended.")

    if blocked_states == len(STATES):
        print("ALL STATES BLOCKED \u2014 SEC is rejecting GitHub's IPs too.")
        print("Fall back to the manual Monday routine in the handoff doc.")
        sys.exit(1)


if __name__ == "__main__":
    main()
