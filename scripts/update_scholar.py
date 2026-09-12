#!/usr/bin/env python3
"""Sync the Google Scholar citation stats shown on the site.

Fetches the author's current citation count / h-index / i10-index and writes
them into the static HTML plus scholar-stats.json. Designed to run unattended
from GitHub Actions; it never writes numbers it could not verify.

Two sources, tried in order:
  1. SerpApi  -- used when SERPAPI_KEY is set. Structured, reliable, rate-limited.
  2. Direct   -- plain HTTPS fetch of the public profile page. Free, but Google
                 serves a CAPTCHA to datacenter IPs often enough that this is
                 treated as best-effort.

Usage:
    python scripts/update_scholar.py [--dry-run] [--strict]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

AUTHOR_ID = "Ts2F72QAAAAJ"
ROOT = Path(__file__).resolve().parent.parent
STATS_FILE = ROOT / "scholar-stats.json"

# A CAPTCHA page or a partial parse can yield absurd numbers. Citation counts
# only ever creep upward, apart from occasional small corrections by Google,
# so anything below this fraction of the last known value is treated as bad data.
MIN_RATIO_OF_PREVIOUS = 0.95

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Blocked(Exception):
    """Source refused to serve the data (CAPTCHA, rate limit, network)."""


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #

def fetch_serpapi(key: str) -> dict:
    url = ("https://serpapi.com/search.json?engine=google_scholar_author"
           f"&author_id={AUTHOR_ID}&api_key={key}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise Blocked(f"SerpApi returned HTTP {e.code}") from e
    except OSError as e:
        raise Blocked(f"SerpApi unreachable: {e}") from e

    if "error" in data:
        raise Blocked(f"SerpApi error: {data['error']}")

    rows = data.get("cited_by", {}).get("table", [])
    out = {}
    for row in rows:
        for key_name, target in (("citations", "citations"),
                                 ("h_index", "h_index"),
                                 ("i10_index", "i10_index")):
            if key_name in row:
                out[target] = int(row[key_name]["all"])
    if not out:
        raise Blocked("SerpApi response contained no citation table")
    return out


def fetch_direct() -> dict:
    url = f"https://scholar.google.com/citations?user={AUTHOR_ID}&hl=en"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise Blocked(f"Scholar returned HTTP {e.code}") from e
    except OSError as e:
        raise Blocked(f"Scholar unreachable: {e}") from e

    if "gs_captcha" in html or "unusual traffic" in html:
        raise Blocked("Scholar served a CAPTCHA")

    # Each stats row is: a label cell, then the "All" figure, then "Since YYYY".
    # Keying off the label rather than row order keeps this stable if Google
    # reorders or adds a row.
    rows = re.findall(
        r'<td class="gsc_rsb_sc1">.*?>([^<]+)</a></td>'
        r'<td class="gsc_rsb_std">(\d+)</td>',
        html,
    )
    wanted = {"Citations": "citations", "h-index": "h_index", "i10-index": "i10_index"}
    out = {wanted[label]: int(value) for label, value in rows if label in wanted}

    if "citations" not in out or "h_index" not in out:
        raise Blocked("could not locate the stats table in the profile page")
    return out


def fetch_stats() -> tuple[dict, str]:
    key = os.environ.get("SERPAPI_KEY", "").strip()
    errors = []
    if key:
        try:
            return fetch_serpapi(key), "serpapi"
        except Blocked as e:
            errors.append(str(e))
    try:
        return fetch_direct(), "direct"
    except Blocked as e:
        errors.append(str(e))
    raise Blocked("; ".join(errors))


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def load_previous() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def validate(new: dict, prev: dict) -> None:
    for field in ("citations", "h_index"):
        if new.get(field, 0) <= 0:
            raise Blocked(f"refusing to publish {field}={new.get(field)!r}")

    old = prev.get("citations")
    if old and new["citations"] < old * MIN_RATIO_OF_PREVIOUS:
        raise Blocked(
            f"citations dropped {old} -> {new['citations']}, "
            "which looks like bad data rather than a correction"
        )


# --------------------------------------------------------------------------- #
# rewriting
# --------------------------------------------------------------------------- #

def rewrite(text: str, cit: str, h: str) -> tuple[str, int]:
    """Replace the stats wherever they appear, anchored on surrounding context
    rather than on the old values, so this keeps working as the numbers move."""
    total = 0
    rules = [
        # hero stat tiles: <span class="num">2,226+</span><span class="lbl">Citations</span>
        (r'(<span class="num">)[\d,]+\+(</span><span class="lbl">Citations</span>)',
         rf'\g<1>{cit}+\g<2>'),
        (r'(<span class="num">)\d+(</span><span class="lbl">h-index</span>)',
         rf'\g<1>{h}\g<2>'),
        # prose: "...publications (2,226+ citations, h-index 25), edited..."
        (r'\([\d,]+\+ citations, h-index \d+\)',
         f'({cit}+ citations, h-index {h})'),
        # meta description: "...109 papers, 2226+ citations, h-index 25."
        (r'[\d,]+\+ citations, h-index \d+\.',
         f'{cit}+ citations, h-index {h}.'),
        # page heading: "109 Papers · 2,226+ Citations"
        (r'(· )[\d,]+\+( Citations)',
         rf'\g<1>{cit}+\g<2>'),
    ]
    for pattern, repl in rules:
        text, n = re.subn(pattern, repl, text)
        total += n
    return text, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when the source is unavailable")
    args = ap.parse_args()

    prev = load_previous()
    try:
        stats, source = fetch_stats()
        validate(stats, prev)
    except Blocked as e:
        # A blocked fetch is expected occasionally and must not fail the site.
        print(f"::warning::Scholar stats not updated: {e}")
        return 1 if args.strict else 0

    print(f"fetched via {source}: {stats}")

    cit = f"{stats['citations']:,}"
    h = str(stats["h_index"])

    changed = []
    for path in sorted(ROOT.glob("*.html")):
        original = path.read_text(encoding="utf-8")
        updated, hits = rewrite(original, cit, h)
        if hits and updated != original:
            changed.append(f"{path.name} ({hits} spot{'s' if hits > 1 else ''})")
            if not args.dry_run:
                path.write_text(updated, encoding="utf-8")

    record = {
        "author_id": AUTHOR_ID,
        "profile_url": f"https://scholar.google.com/citations?user={AUTHOR_ID}&hl=en",
        "citations": stats["citations"],
        "h_index": stats["h_index"],
        "i10_index": stats.get("i10_index"),
        "source": source,
    }
    if not args.dry_run:
        STATS_FILE.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    if changed:
        print("updated: " + ", ".join(changed))
    else:
        print("already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
