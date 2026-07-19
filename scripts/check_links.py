#!/usr/bin/env python3
"""Check that hardware purchase links (AliExpress and others) are still alive.

AliExpress short links (a.aliexpress.com/_xxxx) silently expire when a seller
removes a listing: the link keeps returning HTTP 200 but redirects to the site
homepage or an "item not found" page. This script catches both hard failures
(4xx/5xx, connection errors) and those soft failures (redirect to a homepage /
error page) so a maintainer knows a link needs updating.

Sources checked:
  * docs/hardware.yml  -> the `url` of every component (source of truth)
  * README.md          -> every http(s) link (so nothing slips through)

Usage:
    python scripts/check_links.py            # check everything
    python scripts/check_links.py --json     # machine-readable output
    python scripts/check_links.py README.md  # check specific file(s)

Exit code is non-zero if any link is DEAD (or SUSPECT, unless --lenient),
so it doubles as a CI gate.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
HARDWARE_YML = REPO_ROOT / "docs" / "hardware.yml"
DEFAULT_SOURCES = [HARDWARE_YML, REPO_ROOT / "README.md"]

TIMEOUT = 20  # seconds
# A real browser UA — AliExpress returns 403 / bot walls to python-urllib.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://[^\s)\]\"'>`]+")

# Hosts that are examples/placeholders, not real links to verify.
IGNORE_HOST_SUBSTRINGS = (
    "localhost",
    "127.0.0.1",
    "example.com",
    "example.org",
    "your-",
)
# Private (RFC 1918) / link-local address ranges used in docs examples.
PRIVATE_HOST_RE = re.compile(
    r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)"
)

# When a dead AliExpress listing redirects here, the link is effectively broken.
SUSPECT_FINAL_PATHS = ("/", "")
SUSPECT_FINAL_HINTS = (
    "errorpage",
    "error.htm",
    "login.htm",
    "notfound",
    "gw.aliexpress.com",
)

# Result statuses
OK = "OK"
DEAD = "DEAD"
SUSPECT = "SUSPECT"
SKIP = "SKIP"


def load_yaml(path: Path) -> dict:
    """Load a YAML file, falling back to a tiny parser if PyYAML is absent."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ModuleNotFoundError:
        # Minimal fallback: we only need the `url:` values for the checker.
        urls = re.findall(r"^\s*url:\s*(\S+)", text, flags=re.MULTILINE)
        return {"components": [{"url": u.strip("'\"")} for u in urls]}


def collect_urls(sources: list[Path]) -> dict[str, list[str]]:
    """Return {url: [source labels]} for every http(s) URL found in sources."""
    found: dict[str, list[str]] = {}

    def add(url: str, label: str) -> None:
        url = url.rstrip(".,;")
        host = urlparse(url).hostname or ""
        if not host or "." not in host and host != "localhost":
            return  # malformed / not a real hostname
        if any(s in host for s in IGNORE_HOST_SUBSTRINGS) or PRIVATE_HOST_RE.match(host):
            return  # example/placeholder host, nothing to verify
        found.setdefault(url, [])
        if label not in found[url]:
            found[url].append(label)

    for src in sources:
        if not src.exists():
            print(f"warning: source not found, skipping: {src}", file=sys.stderr)
            continue
        if src.suffix in (".yml", ".yaml"):
            data = load_yaml(src)
            for comp in data.get("components", []) or []:
                url = comp.get("url")
                if url:
                    cid = comp.get("id", "?")
                    add(url, f"{src.name}#{cid}")
        else:
            for m in URL_RE.finditer(src.read_text(encoding="utf-8")):
                add(m.group(0), src.name)
    return found


def check_url(url: str) -> tuple[str, str]:
    """Return (status, detail) for a single URL."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            final = resp.geturl()
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        return DEAD, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return DEAD, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - never let one URL crash the run
        return DEAD, f"{type(exc).__name__}: {exc}"

    if code >= 400:
        return DEAD, f"HTTP {code}"

    # Soft-failure detection: did we land somewhere useless?
    original_host = urlparse(url).netloc
    final_parsed = urlparse(final)
    final_low = final.lower()

    if any(hint in final_low for hint in SUSPECT_FINAL_HINTS):
        return SUSPECT, f"redirected to error/login page: {final}"

    # For AliExpress, a redirect from a product short-link to the bare homepage
    # means the listing is gone.
    if "aliexpress" in original_host and final_parsed.path in SUSPECT_FINAL_PATHS:
        return SUSPECT, f"redirected to homepage (listing likely removed): {final}"

    detail = f"HTTP {code}"
    if final != url:
        detail += f" -> {final}"
    return OK, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help="Files to scan (default: manifest + README).")
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="Treat SUSPECT (soft-failure) links as warnings, not failures.",
    )
    args = parser.parse_args(argv)

    sources = [Path(s) for s in args.sources] if args.sources else DEFAULT_SOURCES
    urls = collect_urls(sources)

    results = []
    for url in sorted(urls):
        status, detail = check_url(url)
        results.append(
            {"url": url, "status": status, "detail": detail, "sources": urls[url]}
        )

    dead = [r for r in results if r["status"] == DEAD]
    suspect = [r for r in results if r["status"] == SUSPECT]

    if args.json:
        print(json.dumps({"results": results, "dead": len(dead), "suspect": len(suspect)}, indent=2))
    else:
        symbol = {OK: "✅", DEAD: "❌", SUSPECT: "⚠️ ", SKIP: "➖"}
        for r in results:
            print(f"{symbol.get(r['status'], '?')} {r['status']:8} {r['url']}")
            print(f"      {r['detail']}  [{', '.join(r['sources'])}]")
        print(
            f"\n{len(results)} link(s) checked — "
            f"{len(results) - len(dead) - len(suspect)} OK, "
            f"{len(suspect)} suspect, {len(dead)} dead."
        )

    if dead or (suspect and not args.lenient):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
