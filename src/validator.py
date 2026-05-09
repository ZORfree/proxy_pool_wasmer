"""
Validator module - verifies proxy connectivity and extracts geo-location
by sending requests through each proxy to https://api.ipapi.is.

Uses httpx for proxy-aware HTTP requests with fallback to urllib.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .storage import get_storage
from .config import VALIDATE_URL, VALIDATE_TIMEOUT, MAX_VALIDATE_CONCURRENCY

logger = logging.getLogger("proxy_pool.validator")


def _validate_single(proxy: Dict) -> Dict:
    """
    Validate a single proxy by making a request through it to VALIDATE_URL.

    Returns a dict with validation results:
      - valid: bool
      - country: str (country code from ipapi.is)
      - latency: float (milliseconds)
    """
    ip = proxy["ip"]
    port = proxy["port"]
    protocol = proxy.get("protocol", "http")
    proxy_url = f"{protocol}://{ip}:{port}"

    result = {
        "ip": ip,
        "port": port,
        "protocol": protocol,
        "valid": False,
        "country": proxy.get("country", ""),
        "latency": -1,
    }

    try:
        start = time.monotonic()
        data = _request_via_proxy(proxy_url, VALIDATE_URL, VALIDATE_TIMEOUT)
        elapsed = (time.monotonic() - start) * 1000  # ms

        if data is not None:
            result["valid"] = True
            result["latency"] = round(elapsed, 1)
            # Extract country code from ipapi.is response
            location = data.get("location", {})
            country_code = location.get("country_code", "")
            if country_code:
                result["country"] = country_code.upper()
            logger.debug(
                "Proxy %s:%s OK (%.0fms, %s)", ip, port, elapsed, country_code
            )
    except Exception as exc:
        logger.debug("Proxy %s:%s FAIL: %s", ip, port, exc)

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
        # httpx uses 'proxy' parameter for proxy configuration
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


def run_validate() -> Dict:
    """
    Run a full validation cycle on all proxies in the database.

    For each proxy:
      - Try to access VALIDATE_URL through the proxy
      - If success: increase score, update country + latency
      - If fail: decrease score
      - Remove proxies with score <= 0

    Returns a summary dict.
    """
    storage = get_storage()
    proxies = storage.get_all()

    if not proxies:
        logger.info("No proxies to validate.")
        return {"total": 0, "valid": 0, "invalid": 0, "removed": 0}

    logger.info("Validating %d proxies (concurrency=%d)...", len(proxies), MAX_VALIDATE_CONCURRENCY)

    valid_count = 0
    invalid_count = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Use thread pool for concurrent validation
    with ThreadPoolExecutor(max_workers=MAX_VALIDATE_CONCURRENCY) as executor:
        future_map = {
            executor.submit(_validate_single, p): p
            for p in proxies
        }
        for future in as_completed(future_map):
            try:
                result = future.result()
                ip = result["ip"]
                port = result["port"]
                protocol = result["protocol"]

                if result["valid"]:
                    valid_count += 1
                    storage.increase_score(ip, port, protocol)
                    storage.update_proxy({
                        "ip": ip,
                        "port": port,
                        "protocol": protocol,
                        "country": result["country"],
                        "latency": result["latency"],
                        "last_check": now,
                    })
                else:
                    invalid_count += 1
                    storage.decrease_score(ip, port, protocol)
            except Exception as exc:
                logger.debug("Validation future error: %s", exc)
                invalid_count += 1

    # Remove dead proxies (score <= 0)
    removed = storage.remove_low_score()

    logger.info(
        "Validation complete: %d valid, %d invalid, %d removed.",
        valid_count, invalid_count, removed,
    )
    return {
        "total": len(proxies),
        "valid": valid_count,
        "invalid": invalid_count,
        "removed": removed,
    }
