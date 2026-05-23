"""
Validator module - re-validates existing proxies in the database.

For each proxy already in DB:
  - Try to access VALIDATE_URL through the proxy
  - If success: increase score, update country + latency
  - If fail: decrease score
  - Remove proxies with score <= 0

Uses aiohttp and asyncio for high performance proxy checking.
"""
import logging
import time
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, List

import aiohttp
from aiohttp_socks import ProxyConnector

from .storage import get_storage
from .config import VALIDATE_URL, VALIDATE_TIMEOUT, MAX_VALIDATE_CONCURRENCY
from .score import calculate_risk_score
from .proxy_url import build_proxy_url

logger = logging.getLogger("proxy_pool.validator")

async def _request_via_proxy(proxy_url: str, target_url: str, timeout: int) -> Optional[Dict]:
    try:
        if proxy_url.startswith('socks'):
            connector = ProxyConnector.from_url(proxy_url)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(target_url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, proxy=proxy_url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.json()
    except Exception:
        return None

async def _validate_single(proxy: Dict, validate_url: str, timeout: int, semaphore: asyncio.Semaphore) -> Dict:
    async with semaphore:
        ip = proxy["ip"]
        port = proxy["port"]
        protocol = proxy.get("protocol", "http")
        proxy_url = build_proxy_url(proxy)

        result = {
            "ip": ip,
            "port": port,
            "protocol": protocol,
            "username": proxy.get("username", ""),
            "password": proxy.get("password", ""),
            "valid": False,
            "country": proxy.get("country", ""),
            "latency": -1,
            "score": 0,
        }

        try:
            start = time.monotonic()
            data = await _request_via_proxy(proxy_url, validate_url, timeout)
            elapsed = (time.monotonic() - start) * 1000

            if data is not None:
                result["valid"] = True
                result["latency"] = round(elapsed, 1)
                result["score"] = calculate_risk_score(data)
                location = data.get("location", {})
                country_code = location.get("country_code", "")
                if country_code:
                    result["country"] = country_code.upper()
                logger.info("  ✓ VALID  %s:%d (%s) → %s, %.0fms", ip, port, protocol, result["country"] or "??", elapsed)
            else:
                logger.debug("  ✗ INVALID %s:%d (%s) → response was None", ip, port, protocol)
        except Exception as exc:
            logger.debug("  ✗ INVALID %s:%d (%s) → %s", ip, port, protocol, exc)

        return result

async def async_run_validate(target_proxies: Optional[List[Dict]] = None) -> Dict:
    storage = get_storage()

    try:
        concurrency_str = await asyncio.to_thread(storage.get_setting, "max_concurrency")
        concurrency = int(concurrency_str) if concurrency_str else MAX_VALIDATE_CONCURRENCY
    except Exception:
        concurrency = MAX_VALIDATE_CONCURRENCY

    try:
        validate_url = await asyncio.to_thread(storage.get_setting, "validate_url") or VALIDATE_URL
    except Exception:
        validate_url = VALIDATE_URL

    try:
        timeout_str = await asyncio.to_thread(storage.get_setting, "validate_timeout")
        timeout = int(timeout_str) if timeout_str else VALIDATE_TIMEOUT
    except Exception:
        timeout = VALIDATE_TIMEOUT

    try:
        interval_str = await asyncio.to_thread(storage.get_setting, "validate_interval")
        check_interval = int(interval_str) if interval_str else 600
    except Exception:
        check_interval = 600

    if target_proxies:
        proxies = target_proxies
    else:
        all_proxies = await asyncio.to_thread(storage.get_all)
        proxies = []
        now_ts = datetime.now().timestamp()

        for p in all_proxies:
            last_check = p.get("last_check", "")
            if not last_check:
                proxies.append(p)
                continue
            try:
                dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                if now_ts - dt.timestamp() >= check_interval:
                    proxies.append(p)
            except Exception:
                proxies.append(p)

    if not proxies:
        if target_proxies:
            logger.info("[RE-VALIDATE] No target proxies to validate.")
        else:
            logger.info("[RE-VALIDATE] No proxies need validation at this time.")
        return {"total": 0, "valid": 0, "invalid": 0, "removed": 0}

    logger.info("=" * 60)
    logger.info("[RE-VALIDATE] Starting re-validation of %d existing proxies", len(proxies))
    logger.info("[RE-VALIDATE] Validate URL: %s", validate_url)
    logger.info("[RE-VALIDATE] Timeout: %ds, Concurrency: %d", timeout, concurrency)
    logger.info("=" * 60)

    valid_count = 0
    invalid_count = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_validate_single(p, validate_url, timeout, semaphore) for p in proxies]
    
    done_count = 0
    total = len(tasks)

    for coro in asyncio.as_completed(tasks):
        result = await coro
        done_count += 1
        
        ip = result["ip"]
        port = result["port"]
        protocol = result["protocol"]

        try:
            if result["valid"]:
                valid_count += 1
                await asyncio.to_thread(storage.increase_score, ip, port, protocol)
                await asyncio.to_thread(storage.update_proxy, {
                    "ip": ip,
                    "port": port,
                    "protocol": protocol,
                    "username": result.get("username", ""),
                    "password": result.get("password", ""),
                    "country": result["country"],
                    "latency": result["latency"],
                    "score": result["score"],
                    "last_check": now,
                })
            else:
                invalid_count += 1
                await asyncio.to_thread(storage.delete_proxy, ip, port, protocol)
        except Exception as exc:
            logger.warning("[RE-VALIDATE] DB exception: %s", exc)

        if done_count % 20 == 0 or done_count == total:
            logger.info("[RE-VALIDATE] Progress: %d/%d done (%d valid, %d invalid)", done_count, total, valid_count, invalid_count)

    removed = invalid_count

    logger.info("=" * 60)
    logger.info("[RE-VALIDATE] COMPLETE")
    logger.info("[RE-VALIDATE]   Total: %d", len(proxies))
    logger.info("[RE-VALIDATE]   Valid: %d", valid_count)
    logger.info("[RE-VALIDATE]   Invalid: %d", invalid_count)
    logger.info("[RE-VALIDATE]   Removed (invalid): %d", removed)
    logger.info("=" * 60)

    return {
        "total": len(proxies),
        "valid": valid_count,
        "invalid": invalid_count,
        "removed": removed,
    }

def run_validate(target_proxies: Optional[List[Dict]] = None) -> Dict:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(async_run_validate(target_proxies), loop)
        return future.result()
    else:
        return asyncio.run(async_run_validate(target_proxies))

async def async_validate_and_add(new_proxies: List[Dict]) -> Dict:
    storage = get_storage()

    try:
        concurrency_str = await asyncio.to_thread(storage.get_setting, "max_concurrency")
        concurrency = int(concurrency_str) if concurrency_str else MAX_VALIDATE_CONCURRENCY
    except Exception:
        concurrency = MAX_VALIDATE_CONCURRENCY

    try:
        validate_url = await asyncio.to_thread(storage.get_setting, "validate_url") or VALIDATE_URL
    except Exception:
        validate_url = VALIDATE_URL

    try:
        timeout_str = await asyncio.to_thread(storage.get_setting, "validate_timeout")
        timeout = int(timeout_str) if timeout_str else VALIDATE_TIMEOUT
    except Exception:
        timeout = VALIDATE_TIMEOUT

    logger.info("=" * 60)
    logger.info("[BATCH-ADD] Starting instant validation for %d new proxies", len(new_proxies))
    logger.info("=" * 60)

    valid_count = 0
    invalid_count = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_validate_single(p, validate_url, timeout, semaphore) for p in new_proxies]
    
    for coro in asyncio.as_completed(tasks):
        result = await coro
        
        ip = result["ip"]
        port = result["port"]
        protocol = result["protocol"]

        if result["valid"]:
            valid_count += 1
            proxy_data = {
                "ip": ip,
                "port": port,
                "protocol": protocol,
                "username": result.get("username", ""),
                "password": result.get("password", ""),
                "country": result["country"],
                "latency": result["latency"],
                "score": result["score"],
                "last_check": now,
                "added_time": now,
            }
            # Insert if not exists
            await asyncio.to_thread(storage.add_proxy, proxy_data)
            # Update metrics in case it already existed
            await asyncio.to_thread(storage.update_proxy, proxy_data)
        else:
            invalid_count += 1

    logger.info("[BATCH-ADD] COMPLETE. Valid: %d, Invalid: %d", valid_count, invalid_count)
    return {
        "total": len(new_proxies),
        "valid": valid_count,
        "invalid": invalid_count
    }
