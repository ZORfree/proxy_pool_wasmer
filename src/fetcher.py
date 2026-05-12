"""
Fetcher module - crawls public proxy sources, validates each proxy,
and only stores validated proxies to the database.

Flow: Fetch IP:Port list → Validate via ipapi.is → Store valid ones only.
Uses httpx (pure Python) with fallback to urllib.
"""
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Dict, Set, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .storage import get_storage
from .config import VALIDATE_URL, VALIDATE_TIMEOUT, MAX_VALIDATE_CONCURRENCY

logger = logging.getLogger("proxy_pool.fetcher")

# ---------------------------------------------------------------------------
# Built-in Free Proxy Sources
# ---------------------------------------------------------------------------

# Format: (name, url, type, pattern)
# type: "api" = plain text IP:Port per line, "web" = HTML with regex
BUILTIN_SOURCES = []

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
        logger.debug("[HTTP] GET %s (timeout=%ds)", url, timeout)
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        logger.debug("[HTTP] GET %s → %d, %d bytes", url, resp.status_code, len(resp.text))
        return resp.text
    except ImportError:
        logger.info("[HTTP] httpx not available, falling back to urllib")
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 ProxyPool/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            logger.debug("[HTTP] urllib GET %s → %d bytes", url, len(body))
            return body


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


def _validate_proxy(proxy: Dict, validate_url: str, timeout: int) -> Dict:
    """
    Validate a single proxy by making a request through it to validate_url.
    Returns dict with: valid, country, latency, and original proxy info.
    """
    ip = proxy["ip"]
    port = proxy["port"]
    protocol = proxy.get("protocol", "http")
    proxy_url = f"{protocol}://{ip}:{port}"

    result = {
        "ip": ip,
        "port": port,
        "protocol": protocol,
        "source": proxy.get("source", ""),
        "valid": False,
        "country": "",
        "latency": -1,
    }

    try:
        start = time.monotonic()
        data = _request_via_proxy(proxy_url, validate_url, timeout)
        elapsed = (time.monotonic() - start) * 1000  # ms

        if data is not None:
            result["valid"] = True
            result["latency"] = round(elapsed, 1)
            # Extract country code from ipapi.is response
            location = data.get("location", {})
            country_code = location.get("country_code", "")
            if country_code:
                result["country"] = country_code.upper()
            logger.info(
                "  ✓ VALID  %s:%d (%s) → %s, %.0fms",
                ip, port, protocol, result["country"] or "??", elapsed
            )
        else:
            logger.debug(
                "  ✗ INVALID %s:%d (%s) → response was None",
                ip, port, protocol
            )
    except Exception as exc:
        logger.debug(
            "  ✗ INVALID %s:%d (%s) → %s: %s",
            ip, port, protocol, type(exc).__name__, exc
        )

    return result


def _request_via_proxy(
    proxy_url: str, target_url: str, timeout: int
) -> Optional[Dict]:
    """
    Send a GET request to target_url through the given proxy.
    Returns parsed JSON dict on success, None on failure.
    """
    try:
        import httpx
        with httpx.Client(
            proxy=proxy_url,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.get(target_url)
            resp.raise_for_status()
            return resp.json()
    except ImportError:
        # Fallback to urllib
        import urllib.request
        import json
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        })
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "Mozilla/5.0 ProxyPool/1.0"},
        )
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return json.loads(body)
    except Exception:
        return None


def _fetch_single_source(
    name: str, url: str, src_type: str, pattern: str
) -> List[Dict]:
    """Fetch and extract raw proxies from a single source."""
    proxies = []
    try:
        logger.info("[FETCH] Fetching source: %s", name)
        logger.info("[FETCH]   URL: %s", url)
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
        logger.info("[FETCH]   Result: %d raw proxies extracted from [%s]", len(proxies), name)
    except Exception as exc:
        logger.warning("[FETCH]   FAILED to fetch [%s]: %s: %s", name, type(exc).__name__, exc)
    return proxies


def run_fetch() -> Dict:
    """
    Run the full fetch-validate-store cycle:
    1. Load sources from DB (+ built-in sources)
    2. Fetch raw IP:Port lists from each source
    3. Deduplicate
    4. Validate each proxy concurrently
    5. Only store validated proxies to DB

    Returns a summary dict.
    """
    storage = get_storage()

    # Read concurrency setting from DB (or fallback to config default)
    try:
        concurrency_str = storage.get_setting("max_concurrency")
        concurrency = int(concurrency_str) if concurrency_str else MAX_VALIDATE_CONCURRENCY
    except Exception:
        concurrency = MAX_VALIDATE_CONCURRENCY

    # Read validate URL from DB settings
    try:
        validate_url = storage.get_setting("validate_url") or VALIDATE_URL
    except Exception:
        validate_url = VALIDATE_URL

    # Read validate timeout from DB settings
    try:
        timeout_str = storage.get_setting("validate_timeout")
        timeout = int(timeout_str) if timeout_str else VALIDATE_TIMEOUT
    except Exception:
        timeout = VALIDATE_TIMEOUT

    logger.info("=" * 60)
    logger.info("[FETCH] Starting fetch cycle")
    logger.info("[FETCH] Validate URL: %s", validate_url)
    logger.info("[FETCH] Validate timeout: %ds", timeout)
    logger.info("[FETCH] Concurrency: %d", concurrency)
    logger.info("=" * 60)

    # --- Step 1: Combine sources ---
    all_sources = []
    for name, url, src_type, pattern in BUILTIN_SOURCES:
        all_sources.append((name, url, src_type, pattern))

    try:
        db_sources = storage.get_sources(active_only=True)
        logger.info("[FETCH] DB custom sources: %d", len(db_sources))
        for s in db_sources:
            all_sources.append((s["name"], s["url"], s["type"], s.get("pattern", "")))
    except Exception as exc:
        logger.warning("[FETCH] Failed to load DB sources: %s", exc)

    logger.info("[FETCH] Total sources to crawl: %d (built-in: %d)", len(all_sources), len(BUILTIN_SOURCES))

    # --- Step 2: Fetch all sources ---
    raw_proxies = []
    seen_keys = set()  # (ip, port, protocol) dedup

    for i, (name, url, src_type, pattern) in enumerate(all_sources, 1):
        logger.info("[FETCH] [%d/%d] Fetching: %s", i, len(all_sources), name)
        source_proxies = _fetch_single_source(name, url, src_type, pattern)
        for p in source_proxies:
            key = (p["ip"], p["port"], p["protocol"])
            if key not in seen_keys:
                seen_keys.add(key)
                raw_proxies.append(p)

    logger.info("[FETCH] Total raw proxies after dedup: %d", len(raw_proxies))

    if not raw_proxies:
        logger.warning("[FETCH] No raw proxies fetched from any source. Aborting.")
        return {
            "sources_crawled": len(all_sources),
            "proxies_found": 0,
            "validated": 0,
            "stored": 0,
        }

    # --- Step 3: Validate concurrently ---
    logger.info("[VALIDATE] Starting validation of %d proxies (concurrency=%d)...", len(raw_proxies), concurrency)
    valid_proxies = []
    invalid_count = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_validate_proxy, p, validate_url, timeout): p
            for p in raw_proxies
        }

        done_count = 0
        total = len(futures)

        for future in as_completed(futures):
            done_count += 1
            try:
                result = future.result()
                if result["valid"]:
                    valid_proxies.append(result)
                else:
                    invalid_count += 1
            except Exception as exc:
                invalid_count += 1
                logger.warning("[VALIDATE] Future exception: %s: %s", type(exc).__name__, exc)

            # Progress log every 50 or at completion
            if done_count % 50 == 0 or done_count == total:
                logger.info(
                    "[VALIDATE] Progress: %d/%d done (%d valid, %d invalid)",
                    done_count, total, len(valid_proxies), invalid_count
                )

    logger.info(
        "[VALIDATE] Validation complete: %d valid / %d invalid out of %d total",
        len(valid_proxies), invalid_count, len(raw_proxies)
    )

    # --- Step 4: Store valid proxies ---
    stored_count = 0
    for vp in valid_proxies:
        try:
            ok = storage.add_proxy({
                "ip": vp["ip"],
                "port": vp["port"],
                "protocol": vp["protocol"],
                "country": vp["country"],
                "latency": vp["latency"],
                "source": vp["source"],
                "last_check": now,
                "score": 50,  # initial score
            })
            if ok:
                stored_count += 1
                logger.debug("[STORE] Stored: %s:%d (%s, %s)", vp["ip"], vp["port"], vp["protocol"], vp["country"])
        except Exception as exc:
            logger.warning("[STORE] Failed to store %s:%d: %s", vp["ip"], vp["port"], exc)

    logger.info("=" * 60)
    logger.info("[FETCH] CYCLE COMPLETE")
    logger.info("[FETCH]   Sources crawled: %d", len(all_sources))
    logger.info("[FETCH]   Raw proxies found: %d", len(raw_proxies))
    logger.info("[FETCH]   Validated: %d", len(valid_proxies))
    logger.info("[FETCH]   Stored to DB: %d", stored_count)
    logger.info("=" * 60)

    return {
        "sources_crawled": len(all_sources),
        "proxies_found": len(raw_proxies),
        "validated": len(valid_proxies),
        "stored": stored_count,
    }
