"""
Fetcher module - crawls public proxy sources and extracts IP:Port pairs.
Supports API endpoints (JSON/plain text) and web pages (regex extraction).
Uses httpx (pure Python) with fallback to urllib.
"""
import logging
import re
from typing import List, Dict, Set, Tuple

from .storage import get_storage

logger = logging.getLogger("proxy_pool.fetcher")

# ---------------------------------------------------------------------------
# Built-in Free Proxy Sources
# ---------------------------------------------------------------------------

# Format: (name, url, type, pattern)
# type: "api" = plain text IP:Port per line, "json" = JSON response, "web" = HTML with regex
BUILTIN_SOURCES = [
    # --- Plain text API sources (IP:Port per line) ---
    (
        "ProxyScrape HTTP",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all",
        "api", "",
    ),
    (
        "ProxyScrape SOCKS5",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all",
        "api", "",
    ),
    (
        "TheSpeedX HTTP",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
        "api", "",
    ),
    (
        "TheSpeedX SOCKS5",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
        "api", "",
    ),
    (
        "Monosans HTTP",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "api", "",
    ),
    (
        "Monosans SOCKS5",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "api", "",
    ),
    (
        "ShiftyTR HTTP",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "api", "",
    ),
    (
        "ShiftyTR SOCKS5",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
        "api", "",
    ),
    (
        "Hookzof HTTP",
        "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "api", "",
    ),
    (
        "Clarketm HTTP",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "api", "",
    ),
]

# Regex pattern for matching IP:Port
IP_PORT_PATTERN = re.compile(
    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})"
)


def _detect_protocol_from_url(url: str) -> str:
    """Guess proxy protocol based on the source URL name."""
    url_lower = url.lower()
    if "socks5" in url_lower or "socks" in url_lower:
        return "socks5"
    if "socks4" in url_lower:
        return "socks4"
    return "http"


def _fetch_url(url: str, timeout: int = 15) -> str:
    """Fetch URL content using httpx with fallback to urllib."""
    try:
        import httpx
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except ImportError:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 ProxyPool/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")


def _extract_proxies(text: str, pattern: str = "") -> Set[Tuple[str, int]]:
    """Extract unique (ip, port) pairs from text."""
    results = set()
    regex = re.compile(pattern) if pattern else IP_PORT_PATTERN
    for match in regex.finditer(text):
        ip = match.group(1)
        port_str = match.group(2)
        try:
            port = int(port_str)
            if 1 <= port <= 65535:
                # Basic IP validation
                parts = ip.split(".")
                if all(0 <= int(p) <= 255 for p in parts):
                    results.add((ip, port))
        except (ValueError, IndexError):
            continue
    return results


def _fetch_single_source(
    name: str, url: str, src_type: str, pattern: str
) -> List[Dict]:
    """Fetch and extract proxies from a single source."""
    proxies = []
    try:
        logger.info("Fetching source: %s (%s)", name, url)
        text = _fetch_url(url)
        pairs = _extract_proxies(text, pattern)
        protocol = _detect_protocol_from_url(url)

        for ip, port in pairs:
            proxies.append({
                "ip": ip,
                "port": port,
                "protocol": protocol,
                "source": name,
            })
        logger.info("Source [%s] yielded %d proxies.", name, len(proxies))
    except Exception as exc:
        logger.warning("Failed to fetch source [%s]: %s", name, exc)
    return proxies


def run_fetch() -> Dict:
    """
    Run the full fetch cycle:
    1. Load sources from DB (+ built-in sources)
    2. Fetch each source
    3. Deduplicate and store new proxies

    Returns a summary dict.
    """
    storage = get_storage()

    # Combine built-in sources with DB custom sources
    all_sources = []
    for name, url, src_type, pattern in BUILTIN_SOURCES:
        all_sources.append((name, url, src_type, pattern))

    db_sources = storage.get_sources(active_only=True)
    for s in db_sources:
        all_sources.append((s["name"], s["url"], s["type"], s.get("pattern", "")))

    # Fetch all sources
    seen = set()  # (ip, port, protocol) dedup
    total_fetched = 0
    total_new = 0
    source_count = len(all_sources)

    for name, url, src_type, pattern in all_sources:
        proxies = _fetch_single_source(name, url, src_type, pattern)
        for p in proxies:
            key = (p["ip"], p["port"], p["protocol"])
            if key not in seen:
                seen.add(key)
                total_fetched += 1
                if storage.add_proxy(p):
                    total_new += 1

    logger.info(
        "Fetch complete: %d sources, %d proxies found, %d new added.",
        source_count, total_fetched, total_new,
    )
    return {
        "sources_crawled": source_count,
        "proxies_found": total_fetched,
        "new_added": total_new,
    }
