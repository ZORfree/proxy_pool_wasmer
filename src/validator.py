"""
Validator module - re-validates existing proxies in the database.

For each proxy already in DB:
  - Try to access VALIDATE_URL through the proxy
  - If success: increase score, update country + latency
  - If fail: decrease score
  - Remove proxies with score <= 0

Uses httpx for proxy-aware HTTP requests with fallback to urllib.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from .storage import get_storage
from .config import VALIDATE_URL, VALIDATE_TIMEOUT, MAX_VALIDATE_CONCURRENCY

logger = logging.getLogger("proxy_pool.validator")


def _validate_single(proxy: Dict, validate_url: str, timeout: int) -> Dict:
    """
    Validate a single proxy by making a request through it to validate_url.

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
            logger.info(
                "  ✗ INVALID %s:%d (%s) → response was None",
                ip, port, protocol
            )
    except Exception as exc:
        logger.info(
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


def run_validate(target_proxies: Optional[List[Dict]] = None) -> Dict:
    """
    Run a full validation cycle on all proxies currently in the database.

    For each proxy:
      - Try to access VALIDATE_URL through the proxy
      - If success: increase score, update country + latency
      - If fail: decrease score
      - Remove proxies with score <= 0

    Returns a summary dict.
    """
    storage = get_storage()

    # Read settings from DB
    try:
        concurrency_str = storage.get_setting("max_concurrency")
        concurrency = int(concurrency_str) if concurrency_str else MAX_VALIDATE_CONCURRENCY
    except Exception:
        concurrency = MAX_VALIDATE_CONCURRENCY

    try:
        validate_url = storage.get_setting("validate_url") or VALIDATE_URL
    except Exception:
        validate_url = VALIDATE_URL

    try:
        timeout_str = storage.get_setting("validate_timeout")
        timeout = int(timeout_str) if timeout_str else VALIDATE_TIMEOUT
    except Exception:
        timeout = VALIDATE_TIMEOUT

    try:
        interval_str = storage.get_setting("validate_interval")
        check_interval = int(interval_str) if interval_str else 600
    except Exception:
        check_interval = 600

    if target_proxies:
        proxies = target_proxies
    else:
        all_proxies = storage.get_all()
        proxies = []
        now_ts = datetime.now(timezone.utc).timestamp()

        for p in all_proxies:
            last_check = p.get("last_check", "")
            if not last_check:
                proxies.append(p)
                continue
            try:
                dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if now_ts - dt.timestamp() >= check_interval:
                    proxies.append(p)
            except Exception:
                proxies.append(p)

    if not proxies:
        if target_proxies:
            logger.info("[RE-VALIDATE] No target proxies to validate.")
        else:
            logger.info("[RE-VALIDATE] No proxies need validation at this time (checked %d).", len(all_proxies))
        return {"total": 0, "valid": 0, "invalid": 0, "removed": 0}

    logger.info("=" * 60)
    logger.info("[RE-VALIDATE] Starting re-validation of %d existing proxies", len(proxies))
    logger.info("[RE-VALIDATE] Validate URL: %s", validate_url)
    logger.info("[RE-VALIDATE] Timeout: %ds, Concurrency: %d", timeout, concurrency)
    logger.info("=" * 60)

    valid_count = 0
    invalid_count = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Use thread pool for concurrent validation
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_validate_single, p, validate_url, timeout): p
            for p in proxies
        }

        done_count = 0
        total = len(futures)

        for future in as_completed(futures):
            done_count += 1
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
                logger.warning("[RE-VALIDATE] Future exception: %s: %s", type(exc).__name__, exc)
                invalid_count += 1

            # Progress log every 20 or at completion
            if done_count % 20 == 0 or done_count == total:
                logger.info(
                    "[RE-VALIDATE] Progress: %d/%d done (%d valid, %d invalid)",
                    done_count, total, valid_count, invalid_count
                )

    # Remove dead proxies (score <= 0)
    removed = storage.remove_low_score()

    logger.info("=" * 60)
    logger.info("[RE-VALIDATE] COMPLETE")
    logger.info("[RE-VALIDATE]   Total: %d", len(proxies))
    logger.info("[RE-VALIDATE]   Valid: %d", valid_count)
    logger.info("[RE-VALIDATE]   Invalid: %d", invalid_count)
    logger.info("[RE-VALIDATE]   Removed (score<=0): %d", removed)
    logger.info("=" * 60)

    return {
        "total": len(proxies),
        "valid": valid_count,
        "invalid": invalid_count,
        "removed": removed,
    }
